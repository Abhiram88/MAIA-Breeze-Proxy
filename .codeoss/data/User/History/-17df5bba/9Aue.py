import os
import json
import re
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple, List

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

from google.cloud import secretmanager

from supabase import create_client, Client

# Vertex Gemini (server-side)
from google import genai
from google.genai import types

# ----------------------------
# App
# ----------------------------
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ----------------------------
# Config (env)
# ----------------------------
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")

# Your deployed Breeze proxy base URL (Cloud Run URL)
BREEZE_PROXY_URL = os.environ.get("BREEZE_PROXY_URL", "").rstrip("/")
# If your proxy routes are "/api/breeze/..." set this to "/api"
# If your proxy routes are "/breeze/..." set this to "" (empty) or "/"
PROXY_API_PREFIX = os.environ.get("PROXY_API_PREFIX", "/api").rstrip("/")

# Vertex settings
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "us-west1")
VERTEX_MODEL_MARKET = os.environ.get("VERTEX_MODEL_MARKET", "gemini-2.5-flash")
VERTEX_MODEL_STOCK = os.environ.get("VERTEX_MODEL_STOCK", "gemini-2.5-flash")
VERTEX_MODEL_REG30 = os.environ.get("VERTEX_MODEL_REG30", "gemini-2.5-flash")

# Optional: lock down admin calls to backend
BACKEND_ADMIN_KEY = os.environ.get("BACKEND_ADMIN_KEY", "")  # optional

# HTTP timeouts
HTTP_TIMEOUT_S = float(os.environ.get("HTTP_TIMEOUT_S", "25"))

# ----------------------------
# Secret Manager helper (cached)
# ----------------------------
_secret_cache: Dict[str, str] = {}

def _require_project_id():
    if not GCP_PROJECT_ID:
        raise ValueError("GCP_PROJECT_ID is not set on the service.")

def get_secret(secret_name: str) -> str:
    """Read Secret Manager secret value; cached per instance."""
    if secret_name in _secret_cache:
        return _secret_cache[secret_name]

    _require_project_id()
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{GCP_PROJECT_ID}/secrets/{secret_name}/versions/latest"
    resp = client.access_secret_version(request={"name": name})
    val = resp.payload.data.decode("utf-8")
    _secret_cache[secret_name] = val
    return val

# ----------------------------
# Supabase (cached client)
# ----------------------------
_supabase: Optional[Client] = None
_symbol_cache: Dict[str, str] = {}  # NSE symbol -> short_name

def get_supabase() -> Client:
    global _supabase
    if _supabase is not None:
        return _supabase

    # Prefer secrets (recommended)
    supabase_url = os.environ.get("SUPABASE_URL") or get_secret("SUPABASE_URL")
    # For server-side writes, prefer service role key (store in Secret Manager)
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_ANON_KEY")
    if not supabase_key:
        # fallback secrets
        try:
            supabase_key = get_secret("SUPABASE_SERVICE_ROLE_KEY")
        except Exception:
            supabase_key = get_secret("SUPABASE_ANON_KEY")

    _supabase = create_client(supabase_url, supabase_key)
    return _supabase

def get_breeze_short_name(symbol: str) -> str:
    """Map NSE symbol to Breeze short_name from Supabase table nse_master_list (symbol, short_name)."""
    if not symbol:
        return symbol
    sym = symbol.strip().upper()
    if sym in _symbol_cache:
        return _symbol_cache[sym]

    sb = get_supabase()
    try:
        resp = sb.table("nse_master_list").select("short_name").eq("symbol", sym).maybe_single().execute()
        if resp.data and resp.data.get("short_name"):
            short_name = str(resp.data["short_name"]).strip().upper()
            _symbol_cache[sym] = short_name
            return short_name
    except Exception as e:
        app.logger.warning(f"[MAP] Supabase mapping failed for {sym}: {e}")

    # fallback to symbol itself
    _symbol_cache[sym] = sym
    return sym

# ----------------------------
# Vertex Gemini client (cached)
# ----------------------------
_ai: Optional[genai.Client] = None

def get_ai_client() -> genai.Client:
    global _ai
    if _ai is not None:
        return _ai

    # Vertex uses service account auth automatically in Cloud Run
    # Ensure the Cloud Run service account has roles/aiplatform.user
    _ai = genai.Client(vertexai=True, project=GCP_PROJECT_ID, location=VERTEX_LOCATION)
    return _ai

# ----------------------------
# Utility: robust JSON extraction
# ----------------------------
def extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass

    # try to pull first { ... } block
    try:
        first = text.find("{")
        last = text.rfind("}")
        if first != -1 and last != -1 and last > first:
            return json.loads(text[first:last+1])
    except Exception:
        return None
    return None

# ----------------------------
# Proxy URL builder
# ----------------------------
def proxy_url(path: str) -> str:
    """
    Builds a proxy URL.
    If your proxy is deployed with /api/breeze/... endpoints:
        BREEZE_PROXY_URL + PROXY_API_PREFIX + /breeze/quotes
    Example:
        https://...run.app + /api + /breeze/quotes
    """
    if not BREEZE_PROXY_URL:
        raise ValueError("BREEZE_PROXY_URL not set on backend.")
    p = path if path.startswith("/") else f"/{path}"
    prefix = PROXY_API_PREFIX
    if prefix:
        return f"{BREEZE_PROXY_URL}{prefix}{p}"
    return f"{BREEZE_PROXY_URL}{p}"

# ----------------------------
# HTTP helper
# ----------------------------
def post_json(url: str, payload: dict, headers: Optional[dict] = None, timeout_s: float = HTTP_TIMEOUT_S) -> Tuple[dict, int]:
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    r = requests.post(url, json=payload, headers=h, timeout=timeout_s)
    # Some upstreams return HTML on failure; handle safely
    ct = (r.headers.get("content-type") or "").lower()
    if "application/json" in ct:
        return (r.json(), r.status_code)
    # Attempt parse anyway
    try:
        return (r.json(), r.status_code)
    except Exception:
        return ({"error": "NON_JSON_UPSTREAM", "status": r.status_code, "body": r.text[:500]}, r.status_code)

# ----------------------------
# Admin gate (optional)
# ----------------------------
def require_backend_admin():
    if not BACKEND_ADMIN_KEY:
        return
    k = request.headers.get("X-Backend-Admin-Key", "")
    if k != BACKEND_ADMIN_KEY:
        return jsonify({"ok": False, "error": "UNAUTHORIZED"}), 401
    return None

# ============================================================
# Routes
# ============================================================

@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "service": "maia-backend-service",
        "project": GCP_PROJECT_ID,
        "vertex_location": VERTEX_LOCATION,
        "proxy_configured": bool(BREEZE_PROXY_URL),
        "time_utc": datetime.utcnow().isoformat() + "Z",
    })

# ----------------------------
# Breeze session setter (forward to proxy)
# ----------------------------
@app.post("/api/breeze/admin/api-session")
def set_breeze_session():
    gate = require_backend_admin()
    if gate:
        return gate

    data = request.get_json() or {}
    api_session = (data.get("api_session") or "").strip()
    if not api_session:
        return jsonify({"ok": False, "error": "api_session required"}), 400

    # Forward admin key for proxy (stored client-side OR use backend secret)
    # Recommended: store proxy admin key as backend secret; do NOT expose to browser
    proxy_admin_key = os.environ.get("BREEZE_PROXY_ADMIN_KEY", "")
    if not proxy_admin_key:
        try:
            proxy_admin_key = get_secret("BREEZE_PROXY_ADMIN_KEY")
        except Exception:
            proxy_admin_key = ""

    headers = {}
    if proxy_admin_key:
        headers["X-Proxy-Admin-Key"] = proxy_admin_key

    url = proxy_url("/breeze/admin/api-session")  # if proxy prefix="/api" => /api/breeze/admin/api-session
    # If your proxy path is actually "/api/breeze/admin/api-session", then set PROXY_API_PREFIX="/api" and use "/breeze/..."
    # OR set PROXY_API_PREFIX="" and use "/api/breeze/..." etc. Keep consistent.

    # Common alternative: your proxy might be "/api/breeze/admin/api-session"
    # If that's the case, change above to: proxy_url("/breeze/admin/api-session") with PROXY_API_PREFIX="/api" works.

    resp, status = post_json(url, {"api_session": api_session}, headers=headers)
    return jsonify(resp), status

# ----------------------------
# Market: Quote
# ----------------------------
@app.post("/api/market/quote")
def market_quote():
    data = request.get_json() or {}
    symbol = (data.get("symbol") or data.get("stock_code") or "").strip().upper()
    if not symbol:
        return jsonify({"ok": False, "error": "symbol required"}), 400

    breeze_code = get_breeze_short_name(symbol)

    exchange_code = (data.get("exchange_code") or "NSE").strip().upper()
    product_type = (data.get("product_type") or "cash").strip().lower()

    payload = {
        "stock_code": breeze_code,
        "exchange_code": exchange_code,
        "product_type": product_type
    }

    url = proxy_url("/breeze/quotes")
    resp, status = post_json(url, payload)
    return jsonify(resp), status

# ----------------------------
# Market: Depth
# ----------------------------
@app.post("/api/market/depth")
def market_depth():
    data = request.get_json() or {}
    symbol = (data.get("symbol") or data.get("stock_code") or "").strip().upper()
    if not symbol:
        return jsonify({"ok": False, "error": "symbol required"}), 400

    breeze_code = get_breeze_short_name(symbol)
    exchange_code = (data.get("exchange_code") or "NSE").strip().upper()
    product_type = (data.get("product_type") or "cash").strip().lower()

    payload = {
        "stock_code": breeze_code,
        "exchange_code": exchange_code,
        "product_type": product_type
    }

    url = proxy_url("/breeze/depth")
    resp, status = post_json(url, payload)
    return jsonify(resp), status

# ----------------------------
# Market: Historical (date strings as your UI provides)
# ----------------------------
@app.post("/api/market/historical")
def market_historical():
    data = request.get_json() or {}
    symbol = (data.get("symbol") or data.get("stock_code") or "").strip().upper()
    from_date = (data.get("from_date") or "").strip()
    to_date = (data.get("to_date") or "").strip()
    interval = (data.get("interval") or "1day").strip()

    if not symbol or not from_date or not to_date:
        return jsonify({"ok": False, "error": "symbol, from_date, to_date required"}), 400

    breeze_code = get_breeze_short_name(symbol)
    exchange_code = (data.get("exchange_code") or "NSE").strip().upper()
    product_type = (data.get("product_type") or "cash").strip().lower()

    payload = {
        "stock_code": breeze_code,
        "exchange_code": exchange_code,
        "product_type": product_type,
        "from_date": from_date,
        "to_date": to_date,
        "interval": interval
    }

    url = proxy_url("/breeze/historical")
    resp, status = post_json(url, payload, timeout_s=45)
    return jsonify(resp), status

# ----------------------------
# Market: Watchlist batch
# ----------------------------
@app.post("/api/market/watchlist")
def market_watchlist():
    data = request.get_json() or {}
    symbols = data.get("symbols") or []
    if not isinstance(symbols, list) or not symbols:
        return jsonify({"ok": False, "error": "symbols[] required"}), 400

    exchange_code = (data.get("exchange_code") or "NSE").strip().upper()
    product_type = (data.get("product_type") or "cash").strip().lower()

    out: Dict[str, Any] = {}
    for s in symbols[:50]:
        sym = str(s).strip().upper()
        if not sym:
            continue
        breeze_code = get_breeze_short_name(sym)
        payload = {"stock_code": breeze_code, "exchange_code": exchange_code, "product_type": product_type}
        url = proxy_url("/breeze/quotes")
        resp, status = post_json(url, payload)
        out[sym] = {"status": status, "data": resp}
        # small delay to be nice to proxy/Breeze
        time.sleep(0.05)

    return jsonify({"ok": True, "results": out}), 200

# ----------------------------
# Attachment parse (via proxy, if your proxy exposes it)
# ----------------------------
@app.post("/api/attachment/parse")
def attachment_parse():
    data = request.get_json() or {}
    url_in = (data.get("url") or "").strip()
    if not url_in:
        return jsonify({"ok": False, "error": "url required"}), 400

    url = proxy_url("/attachment/parse")
    resp, status = post_json(url, {"url": url_in}, timeout_s=45)
    return jsonify(resp), status

# ============================================================
# Vertex Gemini endpoints (server-side)
# ============================================================

@app.post("/api/intel/market-radar")
def intel_market_radar():
    ai = get_ai_client()
    sb = get_supabase()

    log = request.get_json() or {}
    log_date = str(log.get("log_date") or log.get("date") or "")[:10] or datetime.utcnow().strftime("%Y-%m-%d")

    close = log.get("niftyClose") or log.get("close") or 0
    change = log.get("niftyChange") or log.get("change_points") or 0
    change_pct = log.get("niftyChangePercent") or log.get("change_percent") or 0

    direction = "UP" if float(change or 0) >= 0 else "DOWN"

    sys = (
        "You are a Senior Quantitative Market Strategist for Indian equities. "
        "You must provide specific, verifiable causal drivers for the NIFTY 50 move on the given date. "
        "If no dominant catalyst exists, say NO_DOMINANT_CATALYST."
    )

    prompt = f"""
INPUT_DATA (authoritative):
{{
  "date": "{log_date}",
  "index": "NIFTY 50",
  "close": {close},
  "change_points": {change},
  "change_percent": {change_pct},
  "direction": "{direction}"
}}

GROUNDING RULES:
- Use Google Search only for events published on {log_date} or the immediately preceding global session.
- Do not invent. If insufficient evidence, set primary_driver="NO_DOMINANT_CATALYST" and keep key_events empty.

OUTPUT STRICT JSON:
{{
  "date": "YYYY-MM-DD",
  "direction": "UP|DOWN|FLAT",
  "primary_driver": "GLOBAL_MACRO|DOMESTIC_MACRO|SECTOR_ROTATION|TECHNICAL|NO_DOMINANT_CATALYST",
  "key_events": [{{"event":"string","source":"string","confidence":0.0}}],
  "sector_impact": [{{"sector":"string","bias":"POSITIVE|NEGATIVE|NEUTRAL"}}],
  "stock_mentions": ["string"],
  "causal_summary": "string"
}}
""".strip()

    try:
        resp = ai.models.generate_content(
            model=VERTEX_MODEL_MARKET,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=sys,
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        text = getattr(resp, "text", "") or ""
        parsed = extract_json(text)
        if not parsed:
            return jsonify({"ok": False, "error": "AI_BAD_JSON", "raw": text[:500]}), 500

        # Persist (optional)
        try:
            market_log_id = log.get("id")
            if market_log_id:
                payload = {
                    "market_log_id": market_log_id,
                    "headline": parsed.get("primary_driver", ""),
                    "narrative": parsed.get("causal_summary", ""),
                    "impact_score": 50,
                    "model": VERTEX_MODEL_MARKET,
                    "impact_json": parsed,
                }
                sb.table("news_attribution").upsert(payload, on_conflict="market_log_id").execute()
        except Exception as e:
            app.logger.warning(f"[DB] news_attribution upsert failed: {e}")

        return jsonify({"ok": True, "result": parsed}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": "AI_ERROR", "message": str(e)}), 500

@app.post("/api/intel/stock-deep-dive")
def intel_stock_deep_dive():
    ai = get_ai_client()

    data = request.get_json() or {}
    symbol = (data.get("symbol") or "").strip().upper()
    date = (data.get("date") or datetime.utcnow().strftime("%Y-%m-%d"))[:10]

    if not symbol:
        return jsonify({"ok": False, "error": "symbol required"}), 400

    sys = (
        "You are a Senior Indian Equity Analyst. "
        "Use Google Search grounding. Do not fabricate analyst calls."
    )

    prompt = f"""
Perform a forensic audit for NSE equity {symbol} for date {date}.

OUTPUT STRICT JSON:
{{
  "symbol": "{symbol}",
  "date": "{date}",
  "movement_explanation": "EARNINGS|ORDER_WIN|MANAGEMENT|MACRO|TECHNICAL|UNKNOWN",
  "evidence": [{{"type":"DISCLOSURE|NEWS|ANALYST","source":"string","excerpt":"string"}}],
  "analyst_views": [{{"broker":"string","rating":"BUY|HOLD|SELL","rationale":"string"}}],
  "sentiment": "POSITIVE|NEGATIVE|NEUTRAL",
  "forensic_summary": "string"
}}
""".strip()

    try:
        resp = ai.models.generate_content(
            model=VERTEX_MODEL_STOCK,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=sys,
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        text = getattr(resp, "text", "") or ""
        parsed = extract_json(text)
        if not parsed:
            return jsonify({"ok": False, "error": "AI_BAD_JSON", "raw": text[:500]}), 500
        return jsonify({"ok": True, "result": parsed}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": "AI_ERROR", "message": str(e)}), 500

@app.post("/api/reg30/extract")
def reg30_extract():
    ai = get_ai_client()
    data = request.get_json() or {}

    attachment_text = (data.get("attachment_text") or "").strip()
    symbol = (data.get("symbol") or "UNKNOWN").strip().upper()
    company = (data.get("company") or "UNKNOWN").strip()

    if not attachment_text or len(attachment_text) < 80:
        return jsonify({"ok": False, "error": "attachment_text required (min length 80)"}), 400

    sys = (
        "You are an NSE Regulation 30 forensic extraction engine. "
        "Use only provided text. Never fabricate numbers. Provide evidence_spans."
    )

    prompt = f"""
Company: {company}
Symbol: {symbol}

Document Text:
{attachment_text}

OUTPUT STRICT JSON:
{{
  "symbol": "{symbol}",
  "event_stage": "L1|LOA|WO|NTP|MOU|OTHER",
  "order_value_cr": null,
  "counterparty": null,
  "execution_period": null,
  "evidence_spans": [{{"text":"string","offset":[0,0]}}],
  "confidence": 0.0
}}
""".strip()

    try:
        resp = ai.models.generate_content(
            model=VERTEX_MODEL_REG30,
            contents=prompt,
            config=types.GenerateContentConfig(system_instruction=sys),
        )
        text = getattr(resp, "text", "") or ""
        parsed = extract_json(text)
        if not parsed:
            return jsonify({"ok": False, "error": "AI_BAD_JSON", "raw": text[:500]}), 500
        return jsonify({"ok": True, "result": parsed}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": "AI_ERROR", "message": str(e)}), 500
