import express from "express";
import cors from "cors";
import crypto from "crypto";
import fetch from "node-fetch";

const app = express();
const port = process.env.PORT || 8080;

/* =========================
   ENV
========================= */
const BREEZE_APP_KEY = process.env.BREEZE_APP_KEY;
const BREEZE_SECRET_KEY = process.env.BREEZE_SECRET_KEY;
const PROXY_API_KEY = process.env.PROXY_API_KEY || "";

/**
 * Breeze documented limits: 100 calls/min and 5000/day. :contentReference[oaicite:1]{index=1}
 * We'll enforce a conservative token bucket by default.
 */
const RATE_TOKENS_PER_SEC = Number(process.env.RATE_TOKENS_PER_SEC || (100 / 60)); // ~1.666
const RATE_BUCKET_CAPACITY = Number(process.env.RATE_BUCKET_CAPACITY || 10);
const MAX_QUEUE_SIZE = Number(process.env.MAX_QUEUE_SIZE || 200);
const MAX_QUEUE_AGE_MS = Number(process.env.MAX_QUEUE_AGE_MS || 8000);

const MAX_INFLIGHT_GLOBAL = Number(process.env.MAX_INFLIGHT_GLOBAL || 4);
const MAX_INFLIGHT_P0 = Number(process.env.MAX_INFLIGHT_P0 || 2);
const MAX_INFLIGHT_P1 = Number(process.env.MAX_INFLIGHT_P1 || 2);
const MAX_INFLIGHT_P2 = Number(process.env.MAX_INFLIGHT_P2 || 1);
const MAX_INFLIGHT_P3 = Number(process.env.MAX_INFLIGHT_P3 || 1);

/* =========================
   STATE
========================= */
let BREEZE_SESSION_TOKEN = null;

/* =========================
   MIDDLEWARE
========================= */
app.use(
  cors({
    origin: true,
    credentials: true,
    methods: ["GET", "POST", "OPTIONS"],
    allowedHeaders: ["Content-Type", "Accept", "X-Proxy-Key", "X-SessionToken"],
  })
);
app.use(express.json({ limit: "2mb" }));

/* =========================
   HELPERS
========================= */
const isoTimestamp = () => {
  // ISO8601 UTC with 0 milliseconds: YYYY-MM-DDTHH:mm:ss.000Z
  const d = new Date();
  return d.toISOString().slice(0, 19) + ".000Z";
};

const sha256Hex = (s) => crypto.createHash("sha256").update(s, "utf8").digest("hex");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

class HttpError extends Error {
  constructor(status, code, message, meta = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.meta = meta;
  }
}

const requireAdminKey = (req) => {
  const key = req.header("X-Proxy-Key") || "";
  if (PROXY_API_KEY && key !== PROXY_API_KEY) {
    throw new HttpError(401, "UNAUTHORIZED", "Unauthorized");
  }
};

/**
 * Normalize payload so Breeze does not fail
 * Handles equity + index spot
 */
const normalizeQuotePayload = (payload) => {
  const p = { ...payload };
  const code = (p.stock_code || "").toUpperCase();

  if (!code) return p;

  // Index spot
  if (code === "NIFTY" || code === "BANKNIFTY" || code === "FINNIFTY" || code === "MIDCPNIFTY") {
    return {
      stock_code: code,
      exchange_code: "NSE",
      product_type: "cash",
    };
  }

  // Equity
  if (/^[A-Z0-9]{2,20}$/.test(code)) {
    return {
      stock_code: code,
      exchange_code: "NSE",
      product_type: "cash",
    };
  }

  return p;
};

const normalizeHistoricalPayload = (payload) => {
  // Breeze historicalcharts expects: interval, from_date, to_date, stock_code, exchange_code, product_type
  const p = { ...payload };
  const code = (p.stock_code || "").toUpperCase();
  if (!code) return p;

  return {
    interval: p.interval || "day",
    from_date: p.from_date,
    to_date: p.to_date,
    stock_code: code,
    exchange_code: p.exchange_code || "NSE",
    product_type: p.product_type || "Cash",
  };
};

const laneForQuote = (payload) => {
  const code = (payload?.stock_code || "").toUpperCase();
  // P0 for index-like symbols (Monitor card), else P1
  if (["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"].includes(code)) return "P0";
  return "P1";
};

const nowMs = () => Date.now();

/* =========================
   OBSERVABILITY (rolling 60s)
========================= */
const makeRing = () => Array.from({ length: 60 }, () => ({ req: 0, err: {}, lat: { sum: 0, cnt: 0 } }));
let ring = makeRing();
let lastRingSec = Math.floor(Date.now() / 1000);

const bumpRing = (type, status, latencyMs) => {
  const sec = Math.floor(Date.now() / 1000);
  // advance ring if needed
  while (lastRingSec < sec) {
    lastRingSec++;
    ring[lastRingSec % 60] = { req: 0, err: {}, lat: { sum: 0, cnt: 0 } };
  }
  const slot = ring[sec % 60];
  slot.req++;
  if (status >= 400) {
    slot.err[status] = (slot.err[status] || 0) + 1;
  }
  slot.lat.sum += latencyMs;
  slot.lat.cnt += 1;
};

const summarizeRing = () => {
  let requests = 0;
  const errors = {};
  let latSum = 0;
  let latCnt = 0;

  for (const s of ring) {
    requests += s.req;
    for (const [k, v] of Object.entries(s.err)) errors[k] = (errors[k] || 0) + v;
    latSum += s.lat.sum;
    latCnt += s.lat.cnt;
  }
  return {
    requests,
    errors,
    avg_latency_ms: latCnt ? Math.round(latSum / latCnt) : 0,
  };
};

/* =========================
   TOKEN BUCKET RATE LIMITER
========================= */
let tokens = RATE_BUCKET_CAPACITY;
let lastRefill = nowMs();

const refillTokens = () => {
  const t = nowMs();
  const elapsedSec = (t - lastRefill) / 1000;
  if (elapsedSec <= 0) return;
  tokens = Math.min(RATE_BUCKET_CAPACITY, tokens + elapsedSec * RATE_TOKENS_PER_SEC);
  lastRefill = t;
};

/* =========================
   CACHE + DEDUPE
========================= */
const cache = new Map(); // key -> { value, fetchedAt, expiresAt }
const inflight = new Map(); // key -> Promise

const cacheGet = (key) => {
  const v = cache.get(key);
  if (!v) return null;
  if (v.expiresAt <= nowMs()) {
    cache.delete(key);
    return null;
  }
  return v;
};

const cacheSet = (key, value, ttlMs) => {
  if (!ttlMs || ttlMs <= 0) return;
  cache.set(key, { value, fetchedAt: nowMs(), expiresAt: nowMs() + ttlMs });
};

/* =========================
   SCHEDULER QUEUES + CONCURRENCY
========================= */
const lanes = ["P0", "P1", "P2", "P3"];
const lanePriority = { P0: 0, P1: 1, P2: 2, P3: 3 };

const queueByLane = {
  P0: [],
  P1: [],
  P2: [],
  P3: [],
};

let inflightGlobal = 0;
let inflightLane = { P0: 0, P1: 0, P2: 0, P3: 0 };

const laneCap = (lane) => {
  if (lane === "P0") return MAX_INFLIGHT_P0;
  if (lane === "P1") return MAX_INFLIGHT_P1;
  if (lane === "P2") return MAX_INFLIGHT_P2;
  return MAX_INFLIGHT_P3;
};

const rejectForQueuePressure = (lane) => {
  // When queue too big, reject low priority first
  const totalQ = queueByLane.P0.length + queueByLane.P1.length + queueByLane.P2.length + queueByLane.P3.length;
  if (totalQ < MAX_QUEUE_SIZE) return false;

  // If we're beyond capacity, drop P3 first, then P2, then P1; never drop P0 here (unless extreme)
  if (lane === "P3") return true;
  if (lane === "P2") return true;
  if (lane === "P1") return totalQ > MAX_QUEUE_SIZE * 1.2;
  return totalQ > MAX_QUEUE_SIZE * 2;
};

const pump = async () => {
  // prevent re-entrant pumping storms
  if (pump._running) return;
  pump._running = true;

  try {
    refillTokens();

    // Keep taking tasks while we have tokens + capacity
    while (tokens >= 1 && inflightGlobal < MAX_INFLIGHT_GLOBAL) {
      // pick next lane with something queued
      let pickedLane = null;
      for (const ln of lanes) {
        if (queueByLane[ln].length > 0 && inflightLane[ln] < laneCap(ln)) {
          pickedLane = ln;
          break;
        }
      }
      if (!pickedLane) break;

      const task = queueByLane[pickedLane].shift();
      if (!task) break;

      const age = nowMs() - task.createdAt;
      if (age > MAX_QUEUE_AGE_MS) {
        inflight.delete(task.key);
        task.reject(new HttpError(429, "PROXY_QUEUE_TIMEOUT", "Request queued too long", { retry_after_s: 2 }));
        continue;
      }

      tokens -= 1;
      inflightGlobal += 1;
      inflightLane[pickedLane] += 1;

      (async () => {
        const started = nowMs();
        try {
          const data = await task.fetcher(task.timeoutMs);
          const meta = {
            lane: pickedLane,
            cache_hit: false,
            duration_ms: nowMs() - started,
          };
          cacheSet(task.key, data, task.ttlMs);
          inflight.delete(task.key);
          task.resolve({ ok: true, data, meta });
        } catch (err) {
          inflight.delete(task.key);
          task.reject(err);
        } finally {
          inflightGlobal -= 1;
          inflightLane[pickedLane] -= 1;
          // Continue pumping if work remains
          setImmediate(pump);
        }
      })();
    }
  } finally {
    pump._running = false;
  }
};
pump._running = false;

// periodic refill/pump
setInterval(() => {
  refillTokens();
  pump();
}, 250);

/**
 * scheduleRequest() - central API for all Breeze calls
 */
const scheduleRequest = ({ key, lane, ttlMs, timeoutMs, fetcher }) => {
  // Cache first
  const cached = cacheGet(key);
  if (cached) {
    return Promise.resolve({
      ok: true,
      data: cached.value,
      meta: { lane, cache_hit: true, age_ms: nowMs() - cached.fetchedAt, duration_ms: 0 },
    });
  }

  // Dedupe inflight
  const existing = inflight.get(key);
  if (existing) return existing;

  if (rejectForQueuePressure(lane)) {
    const p = Promise.reject(new HttpError(429, "PROXY_OVERLOADED", "Proxy overloaded. Try later.", { retry_after_s: 2 }));
    inflight.set(key, p);
    // remove immediately to avoid poisoning dedupe
    inflight.delete(key);
    return p;
  }

  const p = new Promise((resolve, reject) => {
    const task = {
      key,
      lane,
      createdAt: nowMs(),
      ttlMs,
      timeoutMs,
      fetcher,
      resolve,
      reject,
    };
    queueByLane[lane].push(task);
    pump();
  });

  inflight.set(key, p);
  return p;
};

/* =========================
   BREEZE UPSTREAM CALLS
========================= */
const breezeCall = async (endpoint, payload, timeoutMs = 6000) => {
  if (!BREEZE_APP_KEY || !BREEZE_SECRET_KEY) {
    throw new HttpError(500, "BREEZE_KEYS_MISSING", "Breeze keys not configured");
  }
  if (!BREEZE_SESSION_TOKEN) {
    throw new HttpError(401, "BREEZE_SESSION_EXPIRED", "Breeze session token not set");
  }

  const body = JSON.stringify(payload ?? {});
  const ts = isoTimestamp();
  const checksum = sha256Hex(ts + body + BREEZE_SECRET_KEY);

  const controller = new AbortController();
  const t = setTimeout(() => controller.abort(), timeoutMs);

  let response;
  let text = "";
  const url = `https://api.icicidirect.com/breezeapi/api/v1/${endpoint}`;

  try {
    response = await fetch(url, {
      method: "GET", // Breeze docs show GET with JSON body for many endpoints, including quotes/historicalcharts. :contentReference[oaicite:2]{index=2}
      headers: {
        "Content-Type": "application/json",
        "X-Checksum": `token ${checksum}`,
        "X-Timestamp": ts,
        "X-AppKey": BREEZE_APP_KEY,
        "X-SessionToken": BREEZE_SESSION_TOKEN,
      },
      body,
      signal: controller.signal,
    });
    text = await response.text();
  } catch (e) {
    if (e?.name === "AbortError") {
      throw new HttpError(504, "UPSTREAM_TIMEOUT", "Breeze upstream timed out");
    }
    throw new HttpError(502, "UPSTREAM_NETWORK_ERROR", `Upstream network error: ${e?.message || String(e)}`);
  } finally {
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
