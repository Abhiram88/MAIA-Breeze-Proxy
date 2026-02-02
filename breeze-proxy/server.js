import express from "express";
import crypto from "crypto";
import https from "https";

const app = express();
const port = process.env.PORT || 8080;

const BREEZE_APP_KEY = process.env.BREEZE_APP_KEY;
const BREEZE_SECRET_KEY = process.env.BREEZE_SECRET_KEY;
const ADMIN_KEY = process.env.PROXY_ADMIN_KEY || "";

// Stored session_token resolved via CustomerDetails
let SESSION_TOKEN = "";

// CORS + preflight
app.use((req, res, next) => {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, Accept, X-Proxy-Key");
  res.setHeader("Access-Control-Max-Age", "86400");
  if (req.method === "OPTIONS") return res.sendStatus(204);
  next();
});

app.use(express.json());

function isoTimestamp() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, ".000Z");
}
function sha256Hex(s) {
  return crypto.createHash("sha256").update(s, "utf8").digest("hex");
}

function httpsRequest({ url, method, headers, body }) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const req = https.request(
      { protocol: u.protocol, hostname: u.hostname, path: u.pathname + u.search, method, headers },
      (res) => {
        let data = "";
        res.on("data", (chunk) => (data += chunk));
        res.on("end", () => resolve({ status: res.statusCode || 500, body: data }));
      }
    );
    req.on("error", reject);
    if (body) req.write(body);
    req.end();
  });
}

// ---- Basic health
app.get("/", (_req, res) => res.send("Gateway Proxy is running ✅"));

app.get("/api/breeze/health", (_req, res) => {
  res.json({
    ok: true,
    keys_configured: !!(BREEZE_APP_KEY && BREEZE_SECRET_KEY),
    session_token_set: !!SESSION_TOKEN,
    server_time: new Date().toISOString()
  });
});

// ---- ADMIN: resolve Breeze session_token from daily API_Session
app.post("/api/breeze/admin/api-session", async (req, res) => {
  try {
    if (!ADMIN_KEY) return res.status(500).json({ ok: false, message: "PROXY_ADMIN_KEY not set" });
    const k = req.header("X-Proxy-Key") || "";
    if (k !== ADMIN_KEY) return res.status(401).json({ ok: false, message: "Unauthorized" });
    if (!BREEZE_APP_KEY) return res.status(500).json({ ok: false, message: "Missing BREEZE_APP_KEY" });

    const api_session = String(req.body?.api_session || "").trim();
    if (!api_session) return res.status(400).json({ ok: false, message: "Missing api_session" });

    const payload = JSON.stringify({ SessionToken: api_session, AppKey: BREEZE_APP_KEY });

    const r = await httpsRequest({
      url: "https://api.icicidirect.com/breezeapi/api/v1/customerdetails",
      method: "GET",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Content-Length": Buffer.byteLength(payload)
      },
      body: payload
    });

    let parsed = {};
    try { parsed = JSON.parse(r.body || "{}"); } catch {}

    const tok = parsed?.Success?.session_token;
    if (r.status !== 200 || !tok) {
      return res.status(502).json({
        ok: false,
        message: "CustomerDetails failed",
        upstream_http: r.status,
        upstream_body: parsed
      });
    }

    SESSION_TOKEN = tok;
    return res.json({ ok: true, message: "session_token resolved & stored", session_token_set: true });
  } catch (e) {
    return res.status(500).json({ ok: false, message: e?.message || "Proxy error" });
  }
});

// ---- Breeze quotes (POST from app; proxy calls Breeze signed GET+body)
app.post("/api/breeze/quotes", async (req, res) => {
  try {
    if (!SESSION_TOKEN) return res.status(401).json({ ok: false, message: "Session not set. Sync gateway first." });
    if (!BREEZE_APP_KEY || !BREEZE_SECRET_KEY) return res.status(500).json({ ok: false, message: "Missing Breeze keys" });

    const payload = JSON.stringify(req.body || {});
    const ts = isoTimestamp();
    const checksum = sha256Hex(ts + payload + BREEZE_SECRET_KEY);

    const r = await httpsRequest({
      url: "https://api.icicidirect.com/breezeapi/api/v1/quotes",
      method: "GET",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Checksum": `token ${checksum}`,
        "X-Timestamp": ts,
        "X-AppKey": BREEZE_APP_KEY,
        "X-SessionToken": SESSION_TOKEN,
        "Content-Length": Buffer.byteLength(payload)
      },
      body: payload
    });

    res.status(r.status).type("application/json").send(r.body);
  } catch (e) {
    res.status(500).json({ ok: false, message: e?.message || "Proxy error" });
  }
});

// ---- Attachment parser (NSE iXBRL/HTML)
function stripHtmlToText(html) {
  return html
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gim, "")
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gim, "")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

// GET for debugging: /api/attachment/parse?url=...
app.get("/api/attachment/parse", async (req, res) => {
  const url = String(req.query.url || "").trim();
  if (!url) return res.status(400).send("Please provide ?url=");

  try {
    const r = await fetch(url, {
      headers: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/"
      }
    });

    const html = await r.text();
    if (!r.ok) return res.status(502).json({ error: `NSE responded ${r.status}`, text: stripHtmlToText(html).slice(0, 300) });

    return res.json({ text: stripHtmlToText(html) });
  } catch (e) {
    return res.status(500).json({ error: e?.message || "Parse failed" });
  }
});

// POST used by your Studio app
app.post("/api/attachment/parse", async (req, res) => {
  const url = String(req.body?.url || "").trim();
  if (!url) return res.status(400).json({ error: "URL is required" });

  try {
    const r = await fetch(url, {
      headers: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/"
      }
    });

    const html = await r.text();
    if (!r.ok) return res.status(502).json({ error: `NSE responded ${r.status}`, text: stripHtmlToText(html).slice(0, 300) });

    return res.json({ text: stripHtmlToText(html) });
  } catch (e) {
    return res.status(500).json({ error: e?.message || "Parse failed" });
  }
});

app.listen(port, "0.0.0.0", () => console.log("Gateway Proxy listening on", port));
