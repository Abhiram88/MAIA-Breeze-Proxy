    clearTimeout(t);
  }

  // propagate meaningful statuses
  if (!response.ok) {
    // Attempt to detect common failure reasons
    if (response.status === 401 || response.status === 403) {
      throw new HttpError(401, "BREEZE_UNAUTHORIZED", text || "Unauthorized", { upstream_status: response.status });
    }
    if (response.status === 429) {
      // try honor Retry-After if present
      const ra = response.headers.get("retry-after");
      throw new HttpError(429, "UPSTREAM_RATE_LIMIT", text || "Rate limited", {
        upstream_status: 429,
        retry_after_s: ra ? Number(ra) : 2,
      });
    }
    if (response.status >= 500) {
      throw new HttpError(502, "UPSTREAM_5XX", text || "Upstream error", { upstream_status: response.status });
    }
    throw new HttpError(response.status, "UPSTREAM_ERROR", text || "Upstream error", { upstream_status: response.status });
  }

  try {
    return JSON.parse(text);
  } catch {
    throw new HttpError(502, "UPSTREAM_BAD_JSON", "Upstream returned non-JSON response");
  }
};

/* =========================
   ROUTES
========================= */
app.get("/", (_, res) => res.send("Breeze Proxy running ✅"));

/**
 * Simple health for your UI
 */
app.get("/api/health", (_, res) =>
  res.json({
    ok: true,
    keys_configured: !!(BREEZE_APP_KEY && BREEZE_SECRET_KEY),
    session_token_set: !!BREEZE_SESSION_TOKEN,
    server_time: new Date().toISOString(),
  })
);

// keep compatibility with old endpoint
app.get("/api/breeze/health", (_, res) =>
  res.json({
    ok: true,
    keys_configured: !!(BREEZE_APP_KEY && BREEZE_SECRET_KEY),
    session_token_set: !!BREEZE_SESSION_TOKEN,
    server_time: new Date().toISOString(),
  })
);

/**
 * Observability: queue + rate limit stats
 */
app.get("/api/limits", (_, res) => {
  refillTokens();
  const totalQ = queueByLane.P0.length + queueByLane.P1.length + queueByLane.P2.length + queueByLane.P3.length;
  const sum = summarizeRing();
  res.json({
    ok: true,
    server_time: new Date().toISOString(),
    tokens: {
      available: Math.floor(tokens * 1000) / 1000,
      capacity: RATE_BUCKET_CAPACITY,
      refill_per_sec: RATE_TOKENS_PER_SEC,
    },
    in_flight: inflightGlobal,
    in_flight_by_lane: inflightLane,
    queue: {
      size: totalQ,
      by_lane: {
        P0: queueByLane.P0.length,
        P1: queueByLane.P1.length,
        P2: queueByLane.P2.length,
        P3: queueByLane.P3.length,
      },
      max_size: MAX_QUEUE_SIZE,
      max_age_ms: MAX_QUEUE_AGE_MS,
    },
    last_60s: sum,
    caps: {
      inflight_global: MAX_INFLIGHT_GLOBAL,
      inflight_p0: MAX_INFLIGHT_P0,
      inflight_p1: MAX_INFLIGHT_P1,
      inflight_p2: MAX_INFLIGHT_P2,
      inflight_p3: MAX_INFLIGHT_P3,
    },
  });
});

/**
 * ADMIN: store daily API_SESSION (session token)
 */
app.post("/api/breeze/admin/api-session", (req, res) => {
  try {
    requireAdminKey(req);
    const { api_session } = req.body;
    if (!api_session) return res.status(400).json({ ok: false, error: "api_session required" });
    BREEZE_SESSION_TOKEN = api_session;
    res.json({ ok: true, message: "Session stored" });
  } catch (e) {
    const status = e?.status || 500;
    res.status(status).json({ ok: false, error: e?.code || "ERROR", message: e?.message || String(e) });
  }
});

/**
 * QUOTES (P0 for index, else P1)
 * TTL 2s
 */
app.post("/api/breeze/quotes", async (req, res) => {
  const started = nowMs();
  try {
    const payload = normalizeQuotePayload(req.body || {});
    const lane = laneForQuote(payload);
    const key = `quotes|${payload.exchange_code}|${payload.product_type}|${payload.stock_code}`;

    const out = await scheduleRequest({
      key,
      lane,
      ttlMs: 2000,
      timeoutMs: 5000,
      fetcher: async (timeoutMs) => breezeCall("quotes", payload, timeoutMs),
    });

    bumpRing("quotes", 200, nowMs() - started);
    res.json(out);
  } catch (e) {
    const status = e?.status || 500;
    bumpRing("quotes", status, nowMs() - started);
    if (status === 429 && e?.meta?.retry_after_s) res.set("Retry-After", String(e.meta.retry_after_s));
    res.status(status).json({ ok: false, error: e?.code || "ERROR", message: e?.message || String(e), meta: e?.meta || {} });
  }
});

/**
 * DEPTH (derived from quotes fields; P2; TTL 20s)
 * Returns normalized best bid/ask fields even if upstream response shape changes.
 */
app.post("/api/breeze/depth", async (req, res) => {
  const started = nowMs();
  try {
    const payload = normalizeQuotePayload(req.body || {});
    const key = `depth|${payload.exchange_code}|${payload.product_type}|${payload.stock_code}`;

    const out = await scheduleRequest({
      key,
      lane: "P2",
      ttlMs: 20000,
      timeoutMs: 6000,
      fetcher: async (timeoutMs) => {
        const q = await breezeCall("quotes", payload, timeoutMs);
        const first = q?.Success?.[0] || q?.Success || q?.success?.[0] || q?.success || q?.[0] || q;

        const bestBid = Number(first?.best_bid_price ?? first?.bestBuyRate ?? first?.bid ?? 0) || 0;
        const bestBidQty = Number(first?.best_bid_quantity ?? first?.bestBuyQty ?? first?.bidQty ?? 0) || 0;
        const bestAsk = Number(first?.best_offer_price ?? first?.bestSellRate ?? first?.ask ?? 0) || 0;
        const bestAskQty = Number(first?.best_offer_quantity ?? first?.bestSellQty ?? first?.askQty ?? 0) || 0;
        const ltp = Number(first?.ltp ?? 0) || 0;

        return {
          symbol: payload.stock_code,
          bid: bestBid,
          bidQty: bestBidQty,
          ask: bestAsk,
          askQty: bestAskQty,
          ltp,
          raw: first, // keep raw for debugging if needed
        };
      },
    });

    bumpRing("depth", 200, nowMs() - started);
    res.json(out);
  } catch (e) {
    const status = e?.status || 500;
    bumpRing("depth", status, nowMs() - started);
    if (status === 429 && e?.meta?.retry_after_s) res.set("Retry-After", String(e.meta.retry_after_s));
    res.status(status).json({ ok: false, error: e?.code || "ERROR", message: e?.message || String(e), meta: e?.meta || {} });
  }
});

/**
 * CANDLES (historicalcharts wrapper)
 * TTL:
 * - interval 'day' -> 30 min
 * - intraday -> 60s
 */
app.post("/api/breeze/candles", async (req, res) => {
  const started = nowMs();
  try {
    const payload = normalizeHistoricalPayload(req.body || {});
    const interval = String(payload.interval || "day").toLowerCase();

    const ttlMs = interval === "day" ? 30 * 60 * 1000 : 60 * 1000;
    const key = `candles|${payload.exchange_code}|${payload.product_type}|${payload.stock_code}|${interval}|${payload.from_date}|${payload.to_date}`;

    const out = await scheduleRequest({
      key,
      lane: "P2",
      ttlMs,
      timeoutMs: 10000,
      fetcher: async (timeoutMs) => breezeCall("historicalcharts", payload, timeoutMs),
    });

    bumpRing("candles", 200, nowMs() - started);
    res.json(out);
  } catch (e) {
    const status = e?.status || 500;
    bumpRing("candles", status, nowMs() - started);
    if (status === 429 && e?.meta?.retry_after_s) res.set("Retry-After", String(e.meta.retry_after_s));
    res.status(status).json({ ok: false, error: e?.code || "ERROR", message: e?.message || String(e), meta: e?.meta || {} });
  }
});

/**
 * NSE ATTACHMENT PARSER
 * (Simple + safe; consider caching separately if needed)
 */
app.post("/api/attachment/parse", async (req, res) => {
  const started = nowMs();
  const { url } = req.body || {};
  if (!url) return res.status(400).json({ ok: false, error: "URL_REQUIRED", message: "URL required" });

  const controller = new AbortController();
  const t = setTimeout(() => controller.abort(), 15000);

  try {
    const r = await fetch(url, {
      headers: {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.nseindia.com/",
      },
      signal: controller.signal,
    });

    const html = await r.text();

    // Basic HTML->text extraction (fast)
    const text = html
      .replace(/<script[\s\S]*?<\/script>/gi, " ")
      .replace(/<style[\s\S]*?<\/style>/gi, " ")
      .replace(/<noscript[\s\S]*?<\/noscript>/gi, " ")
      .replace(/<[^>]+>/g, " ")
      .replace(/\s+/g, " ")
      .trim();

    bumpRing("attachment", 200, nowMs() - started);
    res.json({ ok: true, text });
  } catch (e) {
    const status = e?.name === "AbortError" ? 504 : 500;
    bumpRing("attachment", status, nowMs() - started);
    res.status(status).json({ ok: false, error: status === 504 ? "ATTACHMENT_TIMEOUT" : "ATTACHMENT_ERROR", message: e?.message || String(e) });
  } finally {
    clearTimeout(t);
  }
});

app.listen(port, () => console.log(`Breeze Proxy listening on ${port}`));
EOF

gcloud run deploy breeze-proxy --source . --region us-west1 --allow-unauthenticated
cd breeze-proxy
gcloud run deploy breeze-proxy   --source .   --region us-west1   --allow-unauthenticated
git
mkdir breeze-proxy-py && cd breeze-proxy-py
cd breeze-proxy
gcloud run deploy maia-breeze-proxy-service   --source .   --region us-central1   --allow-unauthenticated   --set-env-vars GCP_PROJECT_ID=gen-lang-client-0751458856   --set-secrets BREEZE_API_KEY=BREEZE_API_KEY:latest,BREEZE_API_SECRET=BREEZE_API_SECRET:latest,BREEZE_PROXY_ADMIN_KEY=BREEZE_PROXY_ADMIN_KEY:latest
cd breeze-proxy
gcloud run deploy maia-breeze-proxy-service   --source .   --region us-central1   --allow-unauthenticated   --set-env-vars GCP_PROJECT_ID=gen-lang-client-0751458856,GOOGLE_ENTRYPOINT="gunicorn -b :$PORT breeze_proxy_app:app"   --set-secrets BREEZE_API_KEY=BREEZE_API_KEY:latest,BREEZE_API_SECRET=BREEZE_API_SECRET:latest,BREEZE_PROXY_ADMIN_KEY=BREEZE_PROXY
gcloud run deploy maia-breeze-proxy-service   --source .   --region us-central1   --allow-unauthenticated   --set-env-vars GCP_PROJECT_ID=gen-lang-client-0751458856   --set-secrets BREEZE_API_KEY=BREEZE_API_KEY:latest,BREEZE_API_SECRET=BREEZE_API_SECRET:latest,BREEZE_PROXY_ADMIN_KEY=BREEZE_PROXY_ADMIN_KEY:latest
cd breeze-proxy
gcloud run deploy maia-breeze-proxy-service   --image gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service   --platform managed   --region us-central1   --allow-unauthenticated
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service Market-Intelligence-Testing/breeze-proxy
find ~ -maxdepth 3 -name "breeze-proxy" -type d
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service breeze-proxy
ls -a ~/breeze-proxy
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service Market-Intelligence-Testing/breeze-proxy
cd breeze-proxy
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service Market-Intelligence-Testing/breeze-proxy
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
ls
cd ..
cd breeze-proxy
ls
# Navigate to the folder that actually has the Dockerfile
cd ~/Market-Intelligence-Testing/breeze-proxy/
# Verify the file is there now
ls Dockerfile
cd ..
# Navigate to the folder that actually has the Dockerfile
cd ~/Market-Intelligence-Testing/breeze-proxy/
# Verify the file is there now
ls Dockerfile
cd ~/Market-Intelligence-Testing/breeze-proxy/
find ~ -type d -name "breeze-proxy"
cd /home/challapalli_abhiram/breeze-proxy
ls
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
cd ..
ls
cd ..
ls
cd ~
ls
find ~ -type d -name "Market-Intelligence-Testing" 2>/dev/null
find ~ -type d -iname "*Market*" 2>/dev/null
ls
clear
cd /home/challapalli_abhiram/breeze-proxy
cat <<EOF > Dockerfile
# Use an official Python runtime as a parent image
FROM python:3.9-slim-buster

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code into the container
COPY breeze_proxy_app.py .

# Expose the port the app runs on
EXPOSE 8081

# Define environment variables for Flask
ENV FLASK_APP=breeze_proxy_app.py
ENV FLASK_RUN_HOST=0.0.0.0
ENV FLASK_RUN_PORT=8081

# Run the application
CMD ["flask", "run"]
EOF

gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
cd /home/challapalli_abhiram/breeze-proxy
cat <<EOF > requirements.txt
flask
flask-cors
breeze_connect
google-cloud-secret-manager
requests
EOF

cat <<EOF > Dockerfile
FROM python:3.9-slim-buster

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Expose port 8081 for the Flask app
EXPOSE 8081

ENV FLASK_APP=breeze_proxy_app.py
ENV FLASK_RUN_HOST=0.0.0.0
ENV FLASK_RUN_PORT=8081

CMD ["flask", "run"]
EOF

gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
gcloud run deploy maia-breeze-proxy-service \\
gcloud run deploy maia-breeze-proxy-service --image gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service --platform managed --region us-central1 --allow-unauthenticated
cd /home/challapalli_abhiram/breeze-proxy
cat <<EOF > requirements.txt
flask
flask-cors
breeze_connect
google-cloud-secret-manager
requests
EOF

gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
gcloud run deploy maia-breeze-proxy-service --image gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service --platform managed --region us-central1 --allow-unauthenticated
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
gcloud run deploy maia-breeze-proxy-service --image gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service --platform managed --region us-central1 --allow-unauthenticated
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
gcloud run deploy maia-breeze-proxy-service --image gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service --platform managed --region us-central1 --allow-unauthenticated
gcloud logs read --service maia-breeze-proxy-service
gcloud run services logs read maia-breeze-proxy-service
git init
git remote -v
git remote remove origin
git remote -v
gh repo create breeze-proxy-maia --public --source=. --push
gh auth login
git config --global credential.helper store
git init
~/.local/bin/git-filter-repo --force --path .cache --invert-paths
git log --all -- .cache
git push origin main --force
git remote add origin https://github.com/Abhiram88/MAIA-Breeze-Proxy.git
git remote -v
git push -u origin main --force
git pull
git status
git fetch origin copilot/fix-404-error-on-deployment
git pull
git merge origin/copilot/fix-404-error-on-deployment
git rebase origin/copilot/fix-404-error-on-deployment
git status
# fix files
git rebase --continue
git checkout copilot/fix-404-error-on-deployment
git status
clear
gcloud run deploy maia-breeze-proxy-service   --image gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service   --platform managed   --region us-central1   --allow-unauthenticated
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
ls Dockerfile
cd ~/breeze-proxy
ls Dockerfile
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
gcloud run deploy maia-breeze-proxy-service   --image gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service   --platform managed   --region us-central1   --allow-unauthenticated
cd Market-Intelligence-Testing/breeze-proxy
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
cd Market-Intelligence-Testing/breeze-proxy
cd ..
cd Market-Intelligence-Testing/breeze-proxy
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
find ~ -type d -name "breeze-proxy"
cd /home/challapalli_abhiram/breeze-proxy
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
gcloud run deploy maia-breeze-proxy-service --image gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service --platform managed --region us-central1 --allow-unauthenticated
gcloud secrets versions access latest --secret="BREEZE_PROXY_ADMIN_KEY"
git remote -v
git filter-repo --path .git-credentials --invert-paths --force
git remote add origin https://github.com/Abhiram88/MAIA-Breeze-Proxy.git
git push origin copilot/fix-404-error-on-deployment --force
find ~ -type d -name "breeze-proxy"
cd /home/challapalli_abhiram/breeze-proxy
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
gcloud run deploy maia-breeze-proxy-service --image gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service --platform managed --region us-central1 --allow-unauthenticated --port 8080 --timeout 600
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
gcloud run deploy maia-breeze-proxy-service --image gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service --platform managed --region us-central1 --allow-unauthenticated --port 8080 --timeout 600
find ~ -type d -name "breeze-proxy"
cd /home/challapalli_abhiram/breeze-proxy
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
gcloud run deploy maia-breeze-proxy-service --image gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service --platform managed --region us-central1 --allow-unauthenticated
git add .
git status
git commit "API routing issue"
git commit -m "API routing issue"
git config --global user.name "Abhiram Challapalli"
git config --global user.email "challapalli.abhiram@gmail.com"
git commit -m "API routing issue"
git push
git rm --cached .git-credentials
echo ".git-credentials" >> .gitignore
git commit --amend
git commit --amend --no-edit
git push -u origin copilot/fix-404-error-on-deployment --force
find ~ -type d -name "breeze-proxy"
cd /home/challapalli_abhiram/breeze-proxy
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
gcloud run deploy maia-breeze-proxy-service --source . --platform managed --region us-central1 --allow-unauthenticated
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
cd /home/challapalli_abhiram/breeze-proxy
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
cd /home/challapalli_abhiram/breeze-proxy
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
cd /home/challapalli_abhiram/breeze-proxy
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
gcloud run deploy maia-breeze-proxy-service --source . --platform managed --region us-central1 --allow-unauthenticated
cd /home/challapalli_abhiram/breeze-proxy
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
gcloud run deploy maia-breeze-proxy-service --source . --platform managed --region us-central1 --allow-unauthenticated
curl -X POST https://maia-breeze-proxy-service-919207294606.us-central1.run.app/api/breeze/admin/api-session   -H "Content-Type: application/json"   -H "X-Proxy-Admin-Key: Waterloo1214$"   -d '{"api_session": "54592611"}'
git version -v
git -v
git reset --hard HEAD
git pull --tags origin copilot/fix-404-error-on-deployment
git checkout -- .gitignore
git pull --tags origin copilot/fix-404-error-on-deployment
nano .env
cd breeze-proxy
pip install -r requirements.txt
python breeze_proxy_app.py
cd breeze-proxy
pip install -r requirements.txt
curl -X POST https://maia-breeze-proxy-service-919207294606.us-central1.run.app/api/breeze/admin/api-session   -H "Content-Type: application/json"   -H "X-Proxy-Admin-Key: Waterloo1214$"   -d '{"api_session": "54592611"}'
python breeze_proxy_app.py
cd /home/challapalli_abhiram/breeze-proxy
gcloud builds submit --tag gcr.io/gen-lang-client-0751458856/maia-breeze-proxy-service .
gcloud run deploy maia-breeze-proxy-service --source . --platform managed --region us-central1 --allow-unauthenticated
curl -X POST https://maia-breeze-proxy-service-919207294606.us-central1.run.app/api/breeze/admin/api-session   -H "Content-Type: application/json"   -H "X-Proxy-Admin-Key: Waterloo1214$"   -d '{"api_session": "54592611"}'
git remote -v
git status
