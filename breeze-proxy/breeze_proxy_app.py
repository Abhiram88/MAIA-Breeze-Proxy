import secrets
import queue as _stdlib_queue
from flask import Flask, request, jsonify
from flask_cors import CORS, cross_origin
from flask_socketio import SocketIO
from breeze_connect import BreezeConnect
import os
import logging
from dotenv import load_dotenv
import json
import datetime
import pytz
import yaml
import re
from google import genai
from google.genai import types
from supabase import create_client, Client

# Load environment variables from .env file for local testing
load_dotenv()

# Load YAML config file (if present) as a fallback for secrets
_yaml_config = {}
_CONFIG_PATH = os.environ.get("CONFIG_PATH", "env.yaml")
if os.path.isfile(_CONFIG_PATH):
    try:
        with open(_CONFIG_PATH, "r") as f:
            _yaml_config = {k: v for k, v in (yaml.safe_load(f) or {}).items() if v}
        logging.info(f"Loaded config from {_CONFIG_PATH}")
    except Exception as e:
        logging.warning(f"Failed to load {_CONFIG_PATH}: {e}")

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*", "methods": ["GET", "POST", "OPTIONS"], "allow_headers": ["Content-Type", "X-Proxy-Key", "X-Proxy-Admin-Key"]}})
# Google Cloud Run notes:
# - async_mode='eventlet' is required for non-blocking WebSocket/long-poll support.
# - ping_timeout/ping_interval: keep connections alive through Cloud Run's 60s idle timeout.
# - allow_upgrades=True: allow HTTP long-poll sessions to upgrade to WebSocket after handshake.
#   Clients on Cloud Run must start with polling first; this allows the subsequent upgrade.
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='eventlet',
    ping_timeout=60,
    ping_interval=25,
    allow_upgrades=True,
    logger=False,
    engineio_logger=False,
)


@app.after_request
def add_cors_headers(response):
    """Ensure every response has CORS headers so preflight and errors still allow the frontend."""
    origin = request.origin if request.origin else "*"
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Proxy-Key, X-Proxy-Admin-Key"
    response.headers["Access-Control-Max-Age"] = "86400"
    return response


# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Global State & Cache ---
_secret_cache = {}
breeze_client = None
DAILY_SESSION_TOKEN = None
ai_client = None
supabase = None
mapping_cache = {}

# Hard-coded NSE-symbol → Breeze-short-code overrides.
# These are definitive: same set as breezeService.ts HARDCODED_MAPPINGS on the frontend.
# Applied before Supabase lookup so the correct Breeze code is always used.
# NOTE: NIFTY must stay here so get_breeze_symbol() never queries Supabase for it.
#       Supabase nse_master_list has multiple rows matching NIFTY, so maybe_single()
#       returns HTTP 406 (multiple rows), causing a 'NoneType' attribute error.
#       Breeze WebSocket accepts stock_code="NIFTY" directly for NSE NIFTY 50;
#       ticks come back with stock_code="NIFTY" — confirmed by local test.
BREEZE_SYMBOL_OVERRIDES: dict[str, str] = {
    'NIFTY':      'NIFTY',    # Keep here: Supabase returns 406 for this symbol
    'AHLUCONT':   'AHLCON',
    'AXISCADES':  'AXIIT',
    'MEDICO':     'MEDREM',
    'WAAREERTL':  'SANADV',
    'SANGHVIMOV': 'SANMOV',
}

# --- Real-time tick dispatch registry ---
# Maps canonical frontend symbol -> set of Socket.IO SIDs subscribed to that symbol.
# Allows multiple browser clients / components to share a single Breeze WebSocket feed.
_tick_registry: dict[str, set] = {}   # symbol -> set(sid)
_registry_symbol_map: dict[str, str] = {}   # canonical raw_symbol from Breeze -> frontend symbol
_subscribed_breeze_codes: set[str] = set()  # breeze stock_codes with active subscriptions

# Thread-safe queue: Breeze's WebSocket reader thread puts raw ticks here;
# the _run_tick_dispatcher greenlet drains the queue and calls socketio.emit.
# This bridges Breeze's native/green thread context to the Flask-SocketIO event loop.
_tick_dispatch_queue: "_stdlib_queue.SimpleQueue[dict]" = _stdlib_queue.SimpleQueue()
_tick_dispatcher_started: bool = False

# Per-symbol cache of the correct previous-day closing price, populated by the REST
# get_quotes() initial snapshot.  Breeze exchange-quote WebSocket ticks for indices (e.g.
# NIFTY) use `close` = the index value from the previous *second*, not yesterday's session
# close.  This causes `change` / `chng_per` in every WebSocket tick to be tiny (~-0.01%)
# rather than the real daily move.  By caching the correct previous_close from the REST
# snapshot we can recompute the proper daily change/% for every subsequent WebSocket tick.
_symbol_prev_close: dict[str, float] = {}


# ─────────────────────────────────────────────
# HOME
# ─────────────────────────────────────────────
@app.route("/", methods=["GET"])
@cross_origin()
def home():
    return jsonify({
        "message": "MAIA Breeze Proxy is Running",
        "endpoints": [
            "/api/breeze/health",
            "/api/breeze/quotes",
            "/api/gemini/summarize_market_outlook",
            "/api/gemini/stock-deep-dive",
            "/api/stockinsights/announcements",
            "/api/stockinsights/health"
        ]
    }), 200


# ─────────────────────────────────────────────
# SECRETS
# ─────────────────────────────────────────────
def get_secret(secret_name):
    """Fetch secrets from environment variables or YAML config file, with local caching."""
    if secret_name in _secret_cache:
        return _secret_cache[secret_name]

    val = os.environ.get(secret_name)
    if val is None:
        val = _yaml_config.get(secret_name)

    if val:
        logger.info(f"Loaded secret '{secret_name}' successfully.")
        _secret_cache[secret_name] = val
    else:
        logger.error(f"Failed to find secret '{secret_name}' in environment or config file.")

    return val


# ─────────────────────────────────────────────
# CLIENT INITIALIZERS
# ─────────────────────────────────────────────
def initialize_breeze():
    """Initializes the BreezeConnect client."""
    global breeze_client
    if breeze_client is None:
        try:
            api_key = get_secret("BREEZE_API_KEY")
            if not api_key:
                logger.error("BREEZE_API_KEY is missing!")
                return None
            breeze_client = BreezeConnect(api_key=api_key)
            logger.info("BreezeConnect initialized.")
        except Exception as e:
            logger.error(f"Breeze initialization error: {e}")
    return breeze_client


def initialize_supabase():
    """Initializes the Supabase client only."""
    global supabase
    if supabase is None:
        try:
            supabase_url = get_secret("SUPABASE_URL")
            supabase_key = get_secret("SUPABASE_KEY")
            if not supabase_url or not supabase_key:
                logger.error("Supabase URL or Key is missing!")
            else:
                supabase = create_client(supabase_url, supabase_key)
                logger.info("Supabase client initialized.")
        except Exception as e:
            logger.error(f"Supabase client initialization error: {e}")
    return supabase


def initialize_ai_clients():
    """Initializes the Gemini AI and Supabase clients."""
    global ai_client
    if ai_client is None:
        try:
            gemini_api_key = get_secret("GEMINI_API_KEY")
            if not gemini_api_key:
                logger.error("GEMINI_API_KEY is missing! Add it to env.yaml or .env file.")
            else:
                ai_client = genai.Client(api_key=gemini_api_key)
                logger.info("Gemini AI client initialized (API key).")
        except Exception as e:
            logger.error(f"Gemini AI client initialization error: {e}")

    # Always ensure Supabase is also initialized alongside Gemini
    initialize_supabase()


# ─────────────────────────────────────────────
# SESSION MANAGEMENT
# ─────────────────────────────────────────────
def ensure_breeze_session():
    """Validates the active Breeze session before processing data requests."""
    client = initialize_breeze()
    if not client:
        return None, jsonify({"error": "Breeze client not initialized"}), 500

    if not client.session_key and DAILY_SESSION_TOKEN:
        try:
            client.generate_session(
                api_secret=get_secret("BREEZE_API_SECRET"),
                session_token=DAILY_SESSION_TOKEN
            )
            logger.info("Breeze session regenerated.")
        except Exception as e:
            return None, jsonify({"error": f"Session generation failed: {e}"}), 401
    elif not client.session_key:
        return None, jsonify({"error": "Session token missing. Use /api/breeze/admin/api-session"}), 401

    return client, None, None


# ─────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────
def get_ist_now():
    return datetime.datetime.now(pytz.timezone('Asia/Kolkata'))


def extract_json(text):
    try:
        first_brace = text.find('{')
        last_brace = text.rfind('}')
        if first_brace != -1 and last_brace != -1:
            return json.loads(text[first_brace:last_brace + 1])
        return json.loads(text)
    except Exception as e:
        logger.error(f"JSON Extraction Error: {e}")
        return None


def normalize_breeze_response(res):
    """
    Breeze API returns inconsistent shapes:
      - dict with 'Success' key: {"Success": {...}, "Status": 200}
      - plain list: [{...}]
      - plain dict: {...}
    Returns a normalized dict or None.
    """
    if isinstance(res, dict):
        return res.get("Success") or res
    if isinstance(res, list) and len(res) > 0:
        return res[0]
    return None


def to_float(value, default=0.0):
    try:
        if value is None or value == "":
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def canonical_symbol(sym):
    s = str(sym or "").strip().upper()
    if s in {"NIFTY 50", "NIFTY50"}:
        return "NIFTY"
    return s


def wrap_success_payload(payload):
    """
    Ensure REST responses always match frontend expectation:
    { "Success": [ ... ] } or { "Success": { ... } }
    """
    if isinstance(payload, list):
        return {"Success": payload}
    return {"Success": [payload]} if payload is not None else {"Success": []}


def normalize_tick_for_frontend(ticks, resolved_symbol):
    """
    Normalize a Breeze tick (REST quote or WebSocket exchange-quote) for the frontend.

    KEY ISSUE WITH BREEZE WEBSOCKET TICKS FOR INDICES (e.g. NIFTY):
    Exchange-quote WebSocket ticks (get_exchange_quotes=True) carry a `close` field that
    holds the index value from the *previous second* — NOT yesterday's session closing
    price.  This means `change` and `chng_per` in those ticks are tiny intra-second
    deltas (typically ≈ -0.01%) rather than the real daily move.

    FIX: The REST get_quotes() initial snapshot DOES return the correct previous-day
    close via `ltp_percent_change` and `close`.  We detect REST vs WebSocket ticks by
    checking for `ltp_percent_change` (present only in REST responses), cache the correct
    `previous_close` per symbol, and use it to recompute `change` and `%` for all
    subsequent WebSocket ticks.
    """
    last = to_float(ticks.get("last", ticks.get("ltp", ticks.get("last_traded_price", 0))))

    # REST get_quotes() always includes `ltp_percent_change`; WebSocket ticks never do.
    is_rest_quote = ticks.get("ltp_percent_change") is not None

    if is_rest_quote:
        # REST snapshot: change/% fields are correct (computed against yesterday's close).
        previous_close = to_float(ticks.get("close", ticks.get("previous_close", 0)))
        change = to_float(ticks.get("change", (last - previous_close) if previous_close else 0))
        pct = to_float(ticks.get("ltp_percent_change", ticks.get("percent_change", 0)))
        # Cache the correct previous-day close so WebSocket ticks can use it later.
        if previous_close > 0 and resolved_symbol:
            _symbol_prev_close[resolved_symbol] = previous_close
    else:
        # WebSocket tick: `close` is the intra-tick (prev-second) reference, not yesterday's
        # session close.  Use the cached correct previous_close to get real daily change/%.
        cached_close = _symbol_prev_close.get(resolved_symbol, 0) if resolved_symbol else 0
        if cached_close > 0:
            previous_close = cached_close
            change = last - previous_close
            pct = (change / previous_close * 100.0) if previous_close else 0.0
        else:
            # Cache not yet populated (first tick arrived before the REST snapshot ran).
            # Fall back to the tick's own values — will be corrected on the next tick after
            # the REST snapshot completes and populates the cache.
            previous_close = to_float(ticks.get("close", ticks.get("previous_close", 0)))
            change = to_float(ticks.get("change", (last - previous_close) if previous_close else 0))
            raw_pct = ticks.get("chng_per")
            pct = (to_float(raw_pct) if (raw_pct is not None and raw_pct != "")
                   else ((change / previous_close * 100.0) if previous_close else 0.0))

    vol = to_float(ticks.get("ttq", ticks.get("total_quantity_traded", ticks.get("total_volume", ticks.get("volume", 0)))))

    normalized = dict(ticks)
    normalized.update({
        "symbol": resolved_symbol,
        "stock_code": resolved_symbol,
        "ltp": last,
        "last_traded_price": last,
        "previous_close": previous_close,
        "change": change,
        "ltp_percent_change": pct,
        "percent_change": pct,
        "best_bid_price": to_float(ticks.get("bPrice", ticks.get("best_bid_price", 0))),
        "best_bid_quantity": to_float(ticks.get("bQty", ticks.get("best_bid_quantity", 0))),
        "best_offer_price": to_float(ticks.get("sPrice", ticks.get("best_offer_price", 0))),
        "best_offer_quantity": to_float(ticks.get("sQty", ticks.get("best_offer_quantity", 0))),
        "volume": vol,
        "total_quantity_traded": vol,
        "open": to_float(ticks.get("open", 0)),
        "high": to_float(ticks.get("high", 0)),
        "low": to_float(ticks.get("low", 0)),
    })
    return normalized


def _global_on_ticks(ticks):
    """
    Single Breeze on_ticks callback — assigned to breeze_client.on_ticks.

    Called from the Breeze WebSocket reader thread (green thread under gunicorn+eventlet).
    NEVER call socketio.emit() directly here: doing so from within Breeze's callback context
    is unreliable with gunicorn+eventlet and silently drops events.

    Instead, put the raw tick dict into _tick_dispatch_queue (thread-safe SimpleQueue).
    The _run_tick_dispatcher background greenlet drains the queue and does the actual emit
    from within the Flask-SocketIO event loop where socketio.emit() works correctly.
    """
    if ticks:
        logger.debug(f"[on_ticks] Tick received: stock_code={ticks.get('stock_code')!r} last={ticks.get('last')!r}")
        _tick_dispatch_queue.put(dict(ticks))


def _dispatch_tick(ticks: dict) -> None:
    """
    Resolve the Breeze tick's stock_code to the frontend symbol and emit
    a watchlist_update event to all subscribed Socket.IO SIDs.

    Runs from the _run_tick_dispatcher greenlet — safe to call socketio.emit() here.
    """
    raw = ticks.get("stock_code") or ticks.get("stock_name") or ticks.get("symbol") or ""
    # Breeze sometimes sends stock_code in token format: "4.1!NIFTY" or "1!NIFTY"
    token_match = re.match(r"^\d+\.?\d*!(.+)$", str(raw))
    if token_match:
        raw = token_match.group(1)

    resolved = _registry_symbol_map.get(canonical_symbol(raw)) or canonical_symbol(raw)
    payload = normalize_tick_for_frontend(ticks, resolved)

    targets = list(_tick_registry.get(resolved, set()))
    logger.debug(f"[dispatch] symbol={resolved!r} ltp={payload.get('ltp')} targets={targets}")

    for sid in targets:
        try:
            socketio.emit('watchlist_update', payload, to=sid, namespace='/')
        except Exception as e:
            logger.error(f"Tick dispatch error {resolved} -> {sid}: {e}")


def _run_tick_dispatcher():
    """
    Flask-SocketIO background task (eventlet greenlet) that drains _tick_dispatch_queue
    and calls _dispatch_tick for each tick.

    Runs for the lifetime of the server. Uses socketio.sleep() to yield cooperatively
    so other greenlets (HTTP handlers, Socket.IO heartbeats) can run between polls.
    """
    logger.info("[dispatcher] Tick dispatcher started.")
    while True:
        # Drain all pending ticks without blocking before yielding.
        while True:
            try:
                ticks = _tick_dispatch_queue.get_nowait()
                _dispatch_tick(ticks)
            except _stdlib_queue.Empty:
                break
        socketio.sleep(0.005)  # 5 ms cooperative yield — low latency with cooperative multitasking


def _register_tick_sid(symbol: str, sid: str) -> None:
    _tick_registry.setdefault(symbol, set()).add(sid)


def _unregister_sid(sid: str) -> None:
    """Remove a disconnected SID from all registry entries."""
    for sym_sids in _tick_registry.values():
        sym_sids.discard(sid)


def get_gemini_model_candidates():
    """
    Ordered fallback list for us-central1 and other regions.
    Try 2.5 first, then 2.0, then 1.5 so at least one model is available.
    """
    raw = os.environ.get("GEMINI_MODELS") or (_yaml_config.get("GEMINI_MODELS") if _yaml_config else "") or ""
    configured = [m.strip() for m in raw.split(",") if m and m.strip()]
    if configured:
        return configured
    return [
        "gemini-2.5-pro",
        "gemini-2.5-flash"
    ]


def generate_with_model_fallback(prompt, sys_instr):
    last_error = None
    for model_name in get_gemini_model_candidates():
        try:
            response = ai_client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=sys_instr,
                    tools=[types.Tool(google_search=types.GoogleSearch())]
                )
            )
            return response, model_name
        except Exception as e:
            last_error = e
            logger.warning(f"Gemini model failed ({model_name}): {e}")
            continue
    raise RuntimeError(f"All Gemini model attempts failed. Last error: {last_error}")


def get_breeze_symbol(standard_symbol):
    """
    Maps a canonical NSE symbol to the Breeze short code used for subscriptions.
    Priority: BREEZE_SYMBOL_OVERRIDES (hardcoded) → mapping_cache → Supabase lookup → original.
    """
    # Hardcoded overrides take priority (e.g. MEDICO→MEDREM, WAAREERTL→SANADV).
    if standard_symbol in BREEZE_SYMBOL_OVERRIDES:
        return BREEZE_SYMBOL_OVERRIDES[standard_symbol]

    # FIX: use initialize_supabase() directly — not initialize_ai_clients()
    if not supabase:
        initialize_supabase()
    if not supabase:
        logger.error("Supabase not initialized, cannot map symbol.")
        return standard_symbol

    if standard_symbol in mapping_cache:
        return mapping_cache[standard_symbol]

    try:
        response = (
            supabase.table('nse_master_list')
            .select('short_name')
            .eq('symbol', standard_symbol)
            .maybe_single()
            .execute()
        )
        if response.data:
            short_name = response.data['short_name']
            mapping_cache[standard_symbol] = short_name
            logger.info(f"Mapped {standard_symbol} -> {short_name}")
            return short_name
    except Exception as e:
        logger.error(f"Mapping Error for {standard_symbol}: {e}")

    return standard_symbol


# ─────────────────────────────────────────────
# HEALTH ROUTES
# ─────────────────────────────────────────────
@app.route("/api/", methods=["GET"])
@cross_origin()
def root_health():
    return jsonify({"status": "ok", "service": "maia-breeze-proxy"})


@app.route("/api/breeze/health", methods=["GET"])
@cross_origin()
def health():
    return jsonify({
        "status": "ok",
        "session_active": bool(DAILY_SESSION_TOKEN),
        "session_valid": bool(breeze_client and getattr(breeze_client, 'session_key', None))
    })


# ─────────────────────────────────────────────
# ADMIN: SET SESSION
# ─────────────────────────────────────────────
@app.route("/api/breeze/admin/api-session", methods=["POST", "OPTIONS"])
@cross_origin()
def set_session():
    """Activate the daily Breeze session token."""
    if request.method == "OPTIONS":
        return "", 200

    global DAILY_SESSION_TOKEN
    data = request.get_json() or {}
    api_session = data.get("api_session")

    provided_key = request.headers.get('X-Proxy-Admin-Key', '').strip()
    ADMIN_KEY = get_secret("BREEZE_PROXY_ADMIN_KEY")

    if not ADMIN_KEY or not secrets.compare_digest(provided_key, ADMIN_KEY.strip()):
        return jsonify({"error": "Unauthorized"}), 401

    if not api_session:
        return jsonify({"error": "api_session required"}), 400

    client = initialize_breeze()
    if not client:
        return jsonify({"error": "Breeze client could not be initialized"}), 500

    try:
        api_secret = get_secret("BREEZE_API_SECRET")
        client.generate_session(api_secret=api_secret, session_token=api_session)
        DAILY_SESSION_TOKEN = api_session
        return jsonify({"status": "success", "message": "Daily session activated"}), 200
    except Exception as e:
        logger.error(f"Session Error: {e}")
        return jsonify({"error": "Failed to generate session", "details": str(e)}), 500


# ─────────────────────────────────────────────
# BREEZE DATA ROUTES
# ─────────────────────────────────────────────
@app.route("/api/breeze/quotes", methods=["POST"])
@cross_origin()
def get_quotes():
    """Fetch real-time stock quotes."""
    client, err_resp, status_code = ensure_breeze_session()
    if err_resp:
        return err_resp, status_code

    data = request.get_json() or {}
    try:
        res = client.get_quotes(
            stock_code=data.get("stock_code"),
            exchange_code=data.get("exchange_code", "NSE"),
            product_type="cash"
        )
        normalized = normalize_breeze_response(res)
        if normalized:
            return jsonify(wrap_success_payload(normalized)), 200
        return jsonify({"error": "Empty response from Breeze", "raw": str(res)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/breeze/depth", methods=["POST"])
@cross_origin()
def get_depth():
    """Fetch L2 Market Depth."""
    client, err_resp, status_code = ensure_breeze_session()
    if err_resp:
        return err_resp, status_code

    data = request.get_json() or {}
    try:
        res = client.get_market_depth2(
            stock_code=data.get("stock_code"),
            exchange_code=data.get("exchange_code", "NSE"),
            product_type="cash"
        )
        normalized = normalize_breeze_response(res)
        if normalized:
            return jsonify(wrap_success_payload(normalized)), 200
        return jsonify({"error": "Empty response from Breeze", "raw": str(res)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/breeze/historical", methods=["POST"])
@cross_origin()
def get_historical():
    """Fetch historical OHLC data."""
    client, err_resp, status_code = ensure_breeze_session()
    if err_resp:
        return err_resp, status_code

    data = request.get_json() or {}
    try:
        res = client.get_historical_data(
            stock_code=data.get("stock_code"),
            exchange_code=data.get("exchange_code", "NSE"),
            product_type="cash",
            from_date=data.get("from_date"),
            to_date=data.get("to_date"),
            interval=data.get("interval", "1day")
        )
        if isinstance(res, dict) and "Success" in res:
            return jsonify({"Success": res.get("Success") or []}), 200
        if isinstance(res, list):
            return jsonify({"Success": res}), 200
        normalized = normalize_breeze_response(res)
        if normalized:
            return jsonify(wrap_success_payload(normalized)), 200
        return jsonify({"error": "Empty response from Breeze", "raw": str(res)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────
# STOCKINSIGHTS API ROUTES (NSE CORPORATE ANNOUNCEMENTS)
# ─────────────────────────────────────────────
@app.route("/api/stockinsights/announcements", methods=["GET", "OPTIONS"])
@cross_origin()
def fetch_stockinsights_announcements():
    """
    Fetch NSE corporate announcements from StockInsights API.
    
    Query Parameters:
        type (int): Announcement type (8=Contracts, 2=M&A, 13=Updates, 21=Litigation)
        from_date (str): Start date YYYY-MM-DD
        to_date (str): End date YYYY-MM-DD
        symbols (str): Optional comma-separated list of symbols
    
    Returns:
        JSON with announcements data
    """
    if request.method == "OPTIONS":
        return "", 200
    
    try:
        import requests as req
        
        # Get StockInsights API configuration from environment
        STOCKINSIGHTS_API_URL = get_secret("STOCKINSIGHTS_API_URL")
        STOCKINSIGHTS_API_KEY = get_secret("STOCKINSIGHTS_API_KEY")
        
        if not STOCKINSIGHTS_API_KEY:
            logger.error("STOCKINSIGHTS_API_KEY not configured")
            return jsonify({
                "success": False,
                "error": "StockInsights API key not configured"
            }), 500
        
        # Get parameters from frontend
        announcement_type = request.args.get('type', '8')  # Default: contracts
        start_date = request.args.get('from_date')
        end_date = request.args.get('to_date')
        symbols = request.args.get('symbols')  # Optional
        
        # Validate required parameters
        if not start_date or not end_date:
            return jsonify({
                "success": False,
                "error": "from_date and to_date are required"
            }), 400
        
        # Build request to StockInsights API
        params = {
            'type': announcement_type,
            'from_date': start_date,
            'to_date': end_date,
            'api_key': STOCKINSIGHTS_API_KEY
        }
        
        if symbols:
            params['symbols'] = symbols
        
        # Log the request
        logger.info(f"Fetching StockInsights announcements: type={announcement_type}, from={start_date}, to={end_date}")
        
        # Call StockInsights API
        api_url = f"{STOCKINSIGHTS_API_URL}/announcements" if STOCKINSIGHTS_API_URL else "https://api.stockinsights-ai.com/announcements"
        
        response = req.get(
            api_url,
            params=params,
            timeout=30
        )
        
        # Check response status
        if response.status_code == 200:
            data = response.json()
            logger.info(f"StockInsights API success: fetched {len(data) if isinstance(data, list) else 'N/A'} announcements")
            return jsonify({
                'success': True,
                'data': data
            }), 200
        else:
            logger.error(f"StockInsights API error: {response.status_code} - {response.text[:200]}")
            return jsonify({
                'success': False,
                'error': f'StockInsights API error: {response.status_code}',
                'details': response.text[:200]
            }), response.status_code
            
    except req.exceptions.Timeout:
        logger.error("StockInsights API request timeout")
        return jsonify({
            'success': False,
            'error': 'Request timeout - StockInsights API did not respond in time'
        }), 504
        
    except req.exceptions.RequestException as e:
        logger.error(f"StockInsights API request error: {e}")
        return jsonify({
            'success': False,
            'error': f'Network error: {str(e)}'
        }), 500
        
    except Exception as e:
        logger.exception("StockInsights API unexpected error")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route("/api/stockinsights/health", methods=["GET"])
@cross_origin()
def stockinsights_health():
    """Check if StockInsights API is configured"""
    STOCKINSIGHTS_API_KEY = get_secret("STOCKINSIGHTS_API_KEY")
    STOCKINSIGHTS_API_URL = get_secret("STOCKINSIGHTS_API_URL")
    
    return jsonify({
        "configured": bool(STOCKINSIGHTS_API_KEY),
        "api_url": STOCKINSIGHTS_API_URL or "https://api.stockinsights-ai.com",
        "status": "ready" if STOCKINSIGHTS_API_KEY else "not_configured"
    }), 200


# ─────────────────────────────────────────────
# GEMINI ROUTES
# ─────────────────────────────────────────────
@app.route('/api/gemini/summarize_market_outlook', methods=['POST', 'OPTIONS'])
@cross_origin()
def summarize_market_outlook():
    if request.method == 'OPTIONS':
        return jsonify(success=True)

    initialize_ai_clients()
    if not ai_client:
        return jsonify({"error": "Gemini AI client not initialized"}), 500

    log = request.json
    log_date = log.get('log_date', str(get_ist_now().date()))

    nifty_close = log.get('niftyClose') or log.get('ltp') or 0
    nifty_change = log.get('niftyChange') or log.get('points_change') or 0
    nifty_pct = log.get('niftyChangePercent') or log.get('change_percent') or 0
    have_live_data = bool(nifty_close and nifty_close != 0)

    direction = "upward (BULLISH)" if (nifty_change or 0) >= 0 else "downward (BEARISH)"

    if have_live_data:
        price_context = (
            f"The market closed at {nifty_close}, with a change of {nifty_change} points "
            f"({nifty_pct}%). The session trend was {direction}."
        )
    else:
        price_context = (
            "Live Nifty price data is not available at the moment. "
            "Use your Google Search capability to find the Nifty 50's actual closing price, "
            f"change, and the key market drivers for {log_date}."
        )

    sys_instr = (
        "You are a Senior Equity Analyst and Financial Journalist for a top-tier publication, "
        "specializing in the Indian Equity Markets. Your task is to synthesize a compelling and "
        "insightful market summary that explains the key drivers behind the Nifty 50's performance "
        "for a given day. You must provide a clear narrative, supported by data and specific events. "
        "When live price data is not provided, use Google Search to find the actual market data."
    )

    prompt = f"""Provide a comprehensive market summary for the Nifty 50 on {log_date}.
{price_context}

Your analysis should be a narrative of at least 300 words, explaining the 'why' behind the market's movement.

Your response must be in a STRICT JSON format with the following keys:
- "headline": A punchy, insightful headline summarizing the day's action.
- "narrative": A detailed narrative explaining the causal factors (e.g., policy announcements, corporate earnings, global cues, sector-specific news).
- "outlook": A brief forward-looking statement on what to expect in the near term.
- "affected_sectors": A list of the top 3-5 sectors that were most impacted.
- "key_stocks": A list of key stocks that were movers and shakers.
"""

    try:
        response, selected_model = generate_with_model_fallback(prompt, sys_instr)
        result = extract_json(response.text)
        if result:
            if supabase:
                payload = {
                    "market_log_id": log.get('id'),
                    "headline": result.get('headline'),
                    "narrative": result.get('narrative'),
                    "outlook": result.get('outlook'),
                    "model": selected_model,
                    "impact_json": {
                        "stocks": result.get('key_stocks'),
                        "sectors": result.get('affected_sectors'),
                    }
                }
                try:
                    supabase.table('news_attribution').upsert(payload, on_conflict='market_log_id').execute()
                except Exception as e:
                    logger.error(f"Supabase upsert error: {e}")
            return jsonify(result)
        return jsonify({"error": "Failed to parse AI response"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/gemini/stock-deep-dive', methods=['POST', 'OPTIONS'])
@cross_origin()
def analyze_stock():
    if request.method == 'OPTIONS':
        return jsonify(success=True)

    from datetime import datetime, timedelta
    import math

    initialize_ai_clients()
    if not ai_client:
        return jsonify({"error": "Gemini AI client not initialized"}), 500

    data = request.get_json(silent=True) or {}
    symbol = (data.get('symbol') or '').strip().upper()
    if not symbol:
        return jsonify({"error": "Missing required field: symbol"}), 400

    date = data.get('date', str(get_ist_now().date()))

    # Clamp future/invalid dates to today
    try:
        req_date = datetime.strptime(date, "%Y-%m-%d").date()
        today = get_ist_now().date()
        if req_date > today:
            date = str(today)
            req_date = today
    except Exception:
        date = str(get_ist_now().date())
        req_date = get_ist_now().date()

    # ---------- Helpers ----------
    def _safe_float(x):
        try:
            if x is None or x == "":
                return None
            return float(x)
        except Exception:
            return None

    def _get(row, *keys):
        for k in keys:
            if isinstance(row, dict) and k in row and row[k] not in (None, ""):
                return row[k]
        return None

    def ema(series, period):
        if not series or len(series) < period:
            return None
        k = 2 / (period + 1.0)
        e = series[0]
        for val in series[1:]:
            e = (val * k) + (e * (1 - k))
        return e

    def rsi(series, period=14):
        if not series or len(series) < period + 1:
            return None
        gains, losses = [], []
        for i in range(1, len(series)):
            d = series[i] - series[i - 1]
            gains.append(max(d, 0.0))
            losses.append(max(-d, 0.0))
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def atr(highs, lows, closes, period=14):
        if not closes or len(closes) < period + 1:
            return None
        trs = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)
        a = sum(trs[:period]) / period
        for i in range(period, len(trs)):
            a = (a * (period - 1) + trs[i]) / period
        return a

    def normalize_result(result):
        """
        Hard consistency rules so UI doesn't show bullish + avoid/sell without MIXED.
        """
        try:
            sr = result.get("swing_recommendation") or {}
            action = (sr.get("action") or "").upper().strip()
            sentiment = (result.get("sentiment") or "").upper().strip()

            if action in ("SELL", "AVOID") and sentiment == "BULLISH":
                result["sentiment"] = "MIXED"
            if action == "BUY" and sentiment == "BEARISH":
                result["sentiment"] = "MIXED"

            # If we had to change sentiment, make sure category isn't lying
            if result.get("sentiment") == "MIXED" and result.get("category") in ("SECTOR_TAILWIND", "EARNINGS", "ORDER_WIN"):
                # keep as-is, but MIXED is allowed; no forced overwrite
                pass

            return result
        except Exception:
            return result

    # ---------- Pull OHLC + LTP/Quote ----------
    ohlc_block = ""
    tech_ok = False

    last_close = None
    ltp = None
    prev_close = None

    try:
        client, err_resp, status_code = ensure_breeze_session()
        if err_resp:
            client = None

        # 1) Quotes/LTP (works even when market is closed; gives last traded/last available)
        if client:
            quote_res = None
            # Try common Breeze quote methods defensively
            for fn_name in ("get_quotes", "get_quote", "get_market_data", "get_stock_quote"):
                if hasattr(client, fn_name):
                    try:
                        fn = getattr(client, fn_name)
                        # Different SDKs have different arg names; try safest patterns
                        try:
                            quote_res = fn(stock_code=symbol, exchange_code="NSE", product_type="cash")
                        except TypeError:
                            try:
                                quote_res = fn(stock_code=symbol, exchange_code="NSE")
                            except TypeError:
                                quote_res = fn(symbol)
                        break
                    except Exception:
                        continue

            # Normalize quote payload
            q = None
            if isinstance(quote_res, dict):
                if "Success" in quote_res:
                    s = quote_res.get("Success")
                    if isinstance(s, list) and s:
                        q = s[0]
                    elif isinstance(s, dict):
                        q = s
                else:
                    q = quote_res

            if isinstance(q, dict):
                ltp = _safe_float(_get(q, "ltp", "LTP", "last_traded_price", "LastTradedPrice", "last"))
                prev_close = _safe_float(_get(q, "previous_close", "PrevClose", "prev_close", "previousClose", "close"))
                last_close = prev_close  # keep a baseline

        # 2) Historical candles to compute indicators
        candles = []
        if client:
            to_date = req_date
            from_date = to_date - timedelta(days=180)
            res = client.get_historical_data(
                stock_code=symbol,
                exchange_code="NSE",
                product_type="cash",
                from_date=str(from_date),
                to_date=str(to_date),
                interval="1day"
            )

            rows = []
            if isinstance(res, dict) and "Success" in res:
                rows = res.get("Success") or []
            elif isinstance(res, list):
                rows = res
            else:
                normalized = normalize_breeze_response(res)
                if normalized:
                    rows = normalized

            for r in (rows or []):
                o = _safe_float(_get(r, "open", "Open", "OPEN"))
                h = _safe_float(_get(r, "high", "High", "HIGH"))
                l = _safe_float(_get(r, "low", "Low", "LOW"))
                c = _safe_float(_get(r, "close", "Close", "CLOSE"))
                v = _safe_float(_get(r, "volume", "Volume", "VOLUME"))
                if None not in (o, h, l, c):
                    candles.append({"open": o, "high": h, "low": l, "close": c, "volume": v})

        if len(candles) >= 60:
            closes = [x["close"] for x in candles]
            highs = [x["high"] for x in candles]
            lows = [x["low"] for x in candles]
            vols = [x["volume"] for x in candles]

            last_close = closes[-1]  # authoritative from candles
            ema20 = ema(closes[-60:], 20)
            ema50 = ema(closes[-120:], 50) if len(closes) >= 120 else ema(closes, 50)
            rsi14 = rsi(closes, 14)
            atr14 = atr(highs, lows, closes, 14)

            lookback20 = closes[-20:]
            high20 = max(lookback20) if lookback20 else None
            low20 = min(lookback20) if lookback20 else None

            trend = "UPTREND" if (ema20 and ema50 and last_close > ema20 and ema20 > ema50) else \
                    "DOWNTREND" if (ema20 and ema50 and last_close < ema20 and ema20 < ema50) else "MIXED"

            vol_signal = "UNAVAILABLE"
            valid_vols = [v for v in vols[-30:] if isinstance(v, (int, float)) and v is not None and not math.isnan(v)]
            if len(valid_vols) >= 20 and vols[-1] is not None:
                avg20v = sum(valid_vols[-20:]) / 20
                vol_signal = "ABOVE_AVG" if vols[-1] > avg20v * 1.1 else "BELOW_AVG" if vols[-1] < avg20v * 0.9 else "NEAR_AVG"

            tech_ok = True

            ohlc_block = f"""
MARKET_SNAPSHOT (Breeze):
- last_close: {round(last_close, 2)}
- ltp: {round(ltp, 2) if ltp is not None else None}
- prev_close: {round(prev_close, 2) if prev_close is not None else None}

OHLC_TECHNICALS (computed from Breeze daily candles, recent ~6M):
- ema20: {round(ema20, 2) if ema20 is not None else None}
- ema50: {round(ema50, 2) if ema50 is not None else None}
- rsi14: {round(rsi14, 2) if rsi14 is not None else None}
- atr14: {round(atr14, 2) if atr14 is not None else None}
- 20d_high_close: {round(high20, 2) if high20 is not None else None}
- 20d_low_close: {round(low20, 2) if low20 is not None else None}
- trend: {trend}
- volume_signal: {vol_signal}

HARD RULES FOR SWING CALL:
- You MUST derive swing_recommendation from OHLC_TECHNICALS + MARKET_SNAPSHOT.
- If OHLC_TECHNICALS exists, you MUST NOT say "insufficient technical data".
- Use invalidation as a level: below ema20 or below 20d_low_close or ATR-based (e.g., entry - 1.5*ATR).
"""
        else:
            # Even if candles fail, still provide snapshot so model doesn't claim "no data" if LTP exists
            ohlc_block = f"""
MARKET_SNAPSHOT (Breeze):
- last_close: {round(last_close, 2) if last_close is not None else None}
- ltp: {round(ltp, 2) if ltp is not None else None}
- prev_close: {round(prev_close, 2) if prev_close is not None else None}

NOTE:
- OHLC candles were not sufficient to compute indicators. Swing recommendation must be conservative (HOLD/AVOID) and explain missing candles.
"""
    except Exception as e:
        logger.warning(f"OHLC/quote fetch failed for {symbol}: {e}")
        ohlc_block = ""

    sys_instr = (
        "You are a Senior Equity Analyst specializing in Indian Equities. "
        "You MUST be factual and internally consistent. "
        "If you are not able to verify a claim from reliable, recent sources, you must say so and omit it. "
        "Never invent analyst ratings/targets. Never use the word 'hypothetical'. "
        "All outputs must follow the JSON schema exactly."
    )

    prompt = f"""
As a Senior Equity Analyst, perform a FORENSIC AUDIT for the NSE stock symbol: {symbol} for the date: {date}.

{ohlc_block}

CRITICAL DATA RULES (MUST FOLLOW):
- Use only RECENT information (prefer last 30 days from {date}; max 90 days if needed and clearly label it).
- Do NOT fabricate news, events, prices, analyst calls, targets, or broker names.
- Never use the word "hypothetical".
- Prefer authoritative sources (NSE filings/announcements, company disclosures, major financial media).
- Brokerage/analyst calls: only include if publicly verifiable with a working source_url.

CONSISTENCY RULES (MUST FOLLOW):
- headline + narrative + sentiment + swing_recommendation MUST NOT contradict each other.
- If OHLC_TECHNICALS exists, swing_recommendation MUST be derived from it and MUST reference at least 2 indicators (e.g., ema20/ema50, rsi14, atr14, 20d levels).
- If fundamentals/news are bullish but technical swing setup is bearish (or vice-versa), set sentiment to "MIXED" and explain the divergence in narrative.

OBJECTIVES:
1) Identify primary price movement drivers for {symbol} based on verified recent news/events (include dates + why it mattered).
2) Provide 3–6 driver facts inside the narrative.
3) Analyst calls: only real, attributable calls with source_url; otherwise return analyst_calls: [].
4) Write a 300+ word causal narrative with risks + what to watch next 1–4 weeks.
5) Provide a swing trading recommendation (1D–1M) based on OHLC_TECHNICALS and MARKET_SNAPSHOT.

OUTPUT RULES:
Return STRICT JSON with EXACT keys:
headline, narrative, category, sentiment, impact_score, swing_recommendation, affected_stocks, affected_sectors, analyst_calls

NOW PRODUCE THE JSON ONLY. No extra text.
"""

    last_err = None

    # Grounded web first
    for model_name in get_gemini_model_candidates():
        try:
            response = ai_client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=sys_instr,
                    tools=[{"google_search": {}}],
                    temperature=0.2,
                    top_p=0.9,
                ),
            )
            result = extract_json(response.text)
            if result:
                result = normalize_result(result)
                return jsonify(result)
        except Exception as e:
            last_err = e
            logger.warning(f"Stock deep-dive grounded ({model_name}): {e}")
            continue

    # Fallback helper
    try:
        response, _ = generate_with_model_fallback(prompt, sys_instr)
        result = extract_json(response.text)
        if result:
            result = normalize_result(result)
            return jsonify(result)
    except Exception as e:
        last_err = e
        logger.warning(f"Stock deep-dive fallback failed: {e}")

    err_msg = str(last_err) if last_err else "No model succeeded"
    return jsonify({"error": f"Equity deep dive failed. {err_msg}"}), 500


@app.route('/api/attachment/parse', methods=['POST', 'OPTIONS'])
@cross_origin()
def parse_attachment():
    """Fetch a URL (e.g. NSE iXBRL) and return extracted text for Reg30 analysis."""
    if request.method == 'OPTIONS':
        return jsonify(success=True)
    try:
        import requests as req
        data = request.get_json(silent=True) or {}
        url = (data.get('url') or '').strip()
        if not url or not url.startswith('http'):
            return jsonify({"error": "Missing or invalid url"}), 400
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml,application/pdf,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        if 'nseindia.com' in url or 'nsearchives.nseindia.com' in url:
            headers['Referer'] = 'https://www.nseindia.com/'
        r = req.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        ct = r.headers.get('Content-Type', '').lower()
        is_pdf = 'pdf' in ct or url.lower().split('?')[0].endswith('.pdf')
        if is_pdf:
            # Extract text from PDF using pypdf
            try:
                import io
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(r.content))
                pages_text = []
                for page in reader.pages[:30]:  # cap at 30 pages
                    pages_text.append(page.extract_text() or '')
                text = '\n'.join(pages_text)
                text = re.sub(r'\s+', ' ', text).strip()
                return jsonify({"text": text[:100000] if text else ""})
            except Exception as pdf_err:
                logger.warning(f"PDF extraction failed for {url}: {pdf_err}")
                return jsonify({"text": "", "error": f"PDF extraction failed: {pdf_err}"})
        html = r.text
        # Strip tags and collapse whitespace for text extraction
        text = re.sub(r'<script[^>]*>[\s\S]*?</script>', ' ', html, flags=re.IGNORECASE)
        text = re.sub(r'<style[^>]*>[\s\S]*?</style>', ' ', text, flags=re.IGNORECASE)
        # Strip iXBRL header/hidden metadata blocks first — these contain large amounts of XBRL
        # context/unit definitions whose text content would otherwise pad out the beginning of the
        # extracted text and push the visible "General Information" section far past the window
        # used for both Gemini input and the regex fallback.
        text = re.sub(r'<ix:header[\s\S]*?</ix:header>', ' ', text, flags=re.IGNORECASE)
        text = re.sub(r'<ix:hidden[\s\S]*?</ix:hidden>', ' ', text, flags=re.IGNORECASE)
        # Preserve values from HTML input/select fields (some iXBRL renderers use form inputs)
        text = re.sub(r'<input[^>]+\bvalue=["\']([^"\']{1,200})["\'][^>]*/?>',
                      lambda m: f' {m.group(1)} ', text, flags=re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return jsonify({"text": text[:100000] if text else ""})
    except Exception as e:
        logger.warning(f"Attachment parse failed: {e}")
        return jsonify({"error": str(e), "text": ""}), 500


@app.route('/api/nse/announcements', methods=['POST', 'OPTIONS'])
@cross_origin()
def nse_announcements():
    """
    Fetch recent NSE corporate announcements for Reg30 order/contract analysis.
    Body: { from_date: "YYYY-MM-DD" }
    Returns: { announcements: [{ company_name, nse_ticker, published_date, source_link }] }
    """
    if request.method == 'OPTIONS':
        return jsonify(success=True)
    try:
        import requests
        from datetime import datetime, timedelta

        payload = request.get_json(silent=True) or {}
        from_date_str = payload.get('from_date', '')

        # Parse from_date (YYYY-MM-DD); default 7 days back; cap at 90 days
        try:
            from_dt = datetime.strptime(from_date_str, '%Y-%m-%d')
        except (ValueError, TypeError):
            from_dt = datetime.utcnow() - timedelta(days=7)
        to_dt = datetime.utcnow()
        if from_dt < to_dt - timedelta(days=90):
            from_dt = to_dt - timedelta(days=90)

        from_str = from_dt.strftime('%Y-%m-%d')
        to_str   = to_dt.strftime('%Y-%m-%d')

        api_url = 'https://stockinsights-ai-main-95a26a0.zuplo.app/api/in/v0/documents/announcement'
        api_key = get_secret('STOCKINSIGHTS_API_KEY')
        if not api_key:
            logger.error('STOCKINSIGHTS_API_KEY not configured')
            return jsonify({'error': 'StockInsights API key not configured', 'announcements': []}), 500

        req_headers = {'Authorization': f'Bearer {api_key}'}
        PAGE_LIMIT  = 20
        all_items   = []
        page        = 1

        while True:
            params = {
                'announcement_type_id': '8',
                'from_date': from_str,
                'to_date':   to_str,
                'page':      page,
                'limit':     PAGE_LIMIT,
            }
            logger.info(f'StockInsights page {page}: {from_str} → {to_str}')
            resp = requests.get(api_url, headers=req_headers, params=params, timeout=20)
            if resp.status_code != 200:
                logger.error(f'StockInsights HTTP {resp.status_code}: {resp.text[:200]}')
                break
            data    = resp.json()
            records = data.get('data', [])
            total   = data.get('meta', {}).get('total_count', 0)
            all_items.extend(records)
            if not records or len(all_items) >= total:
                break
            page += 1

        logger.info(f'StockInsights: {len(all_items)} total records fetched')

        announcements = []
        skipped       = 0
        for rec in all_items:
            nse_ticker = next(
                (t['ticker'] for t in rec.get('exchange_tickers', []) if t.get('exchange') == 'NSE'),
                ''
            )
            if not nse_ticker:
                skipped += 1
                continue
            ai = rec.get('ai_insights') or {}
            summary = ai.get('summary_text') or ai.get('summary_header') or ''
            # Append key_points bullet list to summary so Gemini has more extraction context
            key_points = ai.get('key_points') or ai.get('highlights') or []
            if key_points and isinstance(key_points, list):
                bullets = ' '.join(f'• {p}' for p in key_points if p)
                if bullets:
                    summary = f"{summary} {bullets}".strip()
            announcements.append({
                'company_name':   rec.get('company_name', ''),
                'nse_ticker':     nse_ticker,
                'published_date': (rec.get('published_date') or '')[:10],
                'source_link':    rec.get('source_link', ''),
                'summary_text':   summary,
                'sentiment':      ai.get('sentiment') or '',
            })

        logger.info(f'NSE announcements: {len(announcements)} NSE records, {skipped} BSE-only skipped')
        return jsonify({'announcements': announcements})

    except Exception as e:
        logger.exception(f'NSE announcements endpoint error: {e}')
        return jsonify({'error': str(e), 'announcements': []}), 500


# ─────────────────────────────────────────────────────────────────────────────
# REG30 HELPERS — ported from Bulk_reg30_processor.py
# ─────────────────────────────────────────────────────────────────────────────

_STAGE_ORDER = {"L1": 1, "LOA": 2, "WO": 3, "NTP": 4}

def _to_float(v, default=0.0):
    try:
        if v is None or v == "": return float(default)
        return float(str(v).replace(",", "").strip())
    except Exception:
        return float(default)

def _norm_date(raw):
    if not raw:
        return datetime.datetime.utcnow().strftime("%Y-%m-%d")
    for fmt in (None, "%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            if fmt is None:
                return datetime.datetime.fromisoformat(
                    raw.strip().replace("Z", "+00:00")).strftime("%Y-%m-%d")
            return datetime.datetime.strptime(raw.strip().split("T")[0], fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return raw.strip()[:10]

def _norm_datetime(raw):
    if not raw:
        return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+00:00")
    try:
        return datetime.datetime.fromisoformat(
            raw.strip().replace("Z", "+00:00")).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    except Exception:
        return _norm_date(raw) + "T00:00:00+00:00"

def _infer_months(text):
    if not text: return None
    t = text.lower().strip()
    if any(w in t for w in ("immediately", "spot", "forthwith", "on delivery", "working days", "within days")):
        return 1
    if "financial year" in t or "next year" in t: return 12
    word_nums = [
        ("one and a half", 18), ("one and half", 18), ("one-and-a-half", 18),
        ("twenty four", 24), ("twenty-four", 24), ("thirty six", 36), ("thirty-six", 36),
        ("eighteen", 18), ("fifteen", 15), ("sixteen", 16), ("seventeen", 17),
        ("nineteen", 19), ("fourteen", 14), ("thirteen", 13), ("twelve", 12),
        ("eleven", 11), ("twenty", 20), ("ten", 10), ("nine", 9), ("eight", 8),
        ("seven", 7), ("six", 6), ("five", 5), ("four", 4), ("three", 3), ("two", 2), ("one", 1),
    ]
    for word, num in word_nums:
        if word in t:
            if "year" in t:  return num * 12
            if "month" in t: return num
            if "week" in t:  return max(1, round(num / 4.3))
            if "day" in t:   return max(1, round(num / 30.4))
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-–]?\s*\d*\s*(months?|years?|weeks?|days?)", t, re.IGNORECASE)
    if m:
        val, unit = float(m.group(1)), m.group(2).lower()
        if "year"  in unit: return int(val * 12)
        if "month" in unit: return max(1, int(val))
        if "week"  in unit: return max(1, round(val / 4.3))
        if "day"   in unit: return max(1, round(val / 30.4))
    return None

def _classify_family(text, stage):
    t = (text or "").lower()
    if re.search(r"arbitrat|certif|settlement|termination|\bagm\b|\begm\b|board meeting|"
                 r"green building|empanell|empanel|network provider|enrollment", t):
        return "OTHER"
    if re.search(
        r"awarding|bagging|work order|letter of award|\bloa\b|l1 bidder|"
        r"lowest bidder|notice to proceed|\bntp\b|purchase order|\bpo\b|"
        r"contract.*award|rate contract|framework agreement|received.*order|"
        r"secured.*contract|won.*contract|awarded.*contract|bagged.*order", t
    ):
        return "ORDER_CONTRACT"
    if re.search(r"issuance|allotment|equity|rights issue|fundrais", t):
        return "DILUTION_CAPITAL"
    if re.search(r"dividend|buyback|bonus|stock split", t):
        return "SHAREHOLDER_RETURNS"
    if re.search(r"\brating\b|\bcrisil\b|\bicra\b|\bcare\b|\bfitch\b|\bmoody\b", t):
        return "CREDIT_RATING"
    if re.search(r"litigation|fine|court|penalty|arbitration|sebi notice", t):
        return "LITIGATION_REGULATORY"
    if stage and stage in _STAGE_ORDER:
        return "ORDER_CONTRACT"
    return "OTHER"

def _validate_extraction(ext, summary, family):
    issues = []
    e = dict(ext)
    # execution_months
    em = e.get("execution_months")
    if em is not None:
        try:
            em = int(float(str(em)))
            e["execution_months"] = em if 0 < em <= 600 else None
            if e["execution_months"] is None:
                issues.append(f"execution_months={em} out of range — cleared")
        except Exception:
            issues.append(f"execution_months={em!r} not numeric — cleared")
            e["execution_months"] = None
    if e.get("execution_months") is None and e.get("execution_period_text"):
        inferred = _infer_months(e["execution_period_text"])
        if inferred:
            e["execution_months"] = inferred
    if e.get("execution_months") is None and e.get("end_date"):
        try:
            datetime.datetime.strptime(str(e["end_date"])[:10], "%Y-%m-%d")
            e["_end_date_pending_resolution"] = e["end_date"]
            issues.append(f"execution_months pending — end_date={e['end_date']}")
        except Exception:
            pass
    # order_value_cr
    ov = e.get("order_value_cr")
    if ov is not None:
        ov_f = _to_float(ov)
        if ov_f <= 0:
            issues.append("order_value_cr <= 0 — cleared"); e["order_value_cr"] = None
        elif ov_f > 500_000:
            issues.append(f"order_value_cr={ov_f} suspiciously large — flagged")
        else:
            e["order_value_cr"] = ov_f
    if not e.get("order_value_cr") and family in ("ORDER_CONTRACT", "ORDER_PIPELINE"):
        m = re.search(r'(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d+)?)\s*(?:Cr(?:ore)?s?)', summary, re.IGNORECASE)
        if m:
            val = _to_float(m.group(1))
            if 0 < val < 500_000:
                e["order_value_cr"] = val
    # order_type
    ot = (e.get("order_type") or "").upper()
    valid_types = {"CONSTRUCTION", "SUPPLY", "SERVICES", "SUB-CONTRACT", "MIXED"}
    if ot not in valid_types:
        s = summary.lower()
        if any(w in s for w in ["civil","road","bridge","epc","construction","electrification","infrastructure","tunnel"]):
            e["order_type"] = "CONSTRUCTION"
        elif any(w in s for w in ["supply","equipment","material","transformer","hardware","manufacturing","solar module"]):
            e["order_type"] = "SUPPLY"
        elif any(w in s for w in ["software","it ","surveillance","maintenance","o&m","consulting","services","analytics","outsourcing"]):
            e["order_type"] = "SERVICES"
        else:
            e["order_type"] = "UNKNOWN"
    # stage
    stage = e.get("stage")
    if stage not in (None, "L1", "LOA", "WO", "NTP", "MOU", "OTHER"):
        issues.append(f"stage='{stage}' invalid — cleared"); e["stage"] = None
    # critical missing
    if family in ("ORDER_CONTRACT", "ORDER_PIPELINE"):
        if not e.get("order_value_cr"): issues.append("order_value_cr missing on ORDER event")
        if not e.get("stage"):          issues.append("stage missing on ORDER event")
        if not e.get("execution_months"): issues.append("execution_months could not be determined")
    if issues: e["_validation_issues"] = issues
    return e, issues

def _calculate_score(family, ext, confidence):
    impact = 0; direction = "NEUTRAL"; factors = []; conversion_bonus = 0; exec_months = None
    order_type = (ext.get("order_type") or "UNKNOWN").upper()
    def add(pts, msg):
        nonlocal impact
        impact += pts
        factors.append(f"{'+' if pts >= 0 else ''}{pts}: {msg}")
    if family in ("ORDER_CONTRACT", "ORDER_PIPELINE"):
        direction = "POSITIVE"
        add(20 if family == "ORDER_CONTRACT" else 15, f"Base weight for {family.replace('_',' ')}")
        order_cr = _to_float(ext.get("order_value_cr"))
        market_cap = _to_float(ext.get("market_cap_cr"))
        if order_cr > 0:
            if market_cap > 0:
                ratio = order_cr / market_cap
                bonus = 25 if ratio >= 0.15 else 18 if ratio >= 0.08 else 12 if ratio >= 0.03 else 6 if ratio >= 0.01 else 2
                add(bonus, f"Order/mktcap {ratio*100:.2f}% (Rs.{order_cr}Cr / Rs.{market_cap}Cr)")
            else:
                bonus = 30 if order_cr >= 1000 else 20 if order_cr >= 500 else 10 if order_cr >= 100 else 5
                add(bonus, f"Absolute value bonus (Rs.{order_cr}Cr)")
        else:
            add(-10, "Order value missing")
        stage = ext.get("stage") or ""
        stage_bonus = {"LOA": 20, "WO": 20, "NTP": 18, "L1": 12}.get(stage, 5)
        add(stage_bonus, f"Stage: {stage or 'General'}")
        contract_mode = (ext.get("contract_mode") or "").upper()
        if contract_mode == "HAM":
            exec_months = ext.get("construction_period_months") or ext.get("execution_months")
        if exec_months is None:
            exec_months = ext.get("execution_months")
        if exec_months is None and ext.get("execution_years"):
            exec_months = int(_to_float(ext["execution_years"]) * 12)
        if exec_months:
            exec_months = int(exec_months)
            conversion_bonus = 10 if exec_months <= 6 else 6 if exec_months <= 12 else 2 if exec_months <= 24 else 0
            if order_type == "SUPPLY":    conversion_bonus += 2
            elif order_type == "SERVICES": conversion_bonus += 1
            conversion_bonus = min(conversion_bonus, 10)
            if conversion_bonus > 0:
                add(conversion_bonus, f"Conversion bonus (~{exec_months}m, {order_type})")
        if ext.get("is_subsidiary_win"):
            add(-8, f"Subsidiary win ({ext.get('subsidiary_name','WOS')})")
    elif family == "CREDIT_RATING":
        action = (ext.get("rating_action") or "").lower()
        if "upgrade" in action:
            direction = "POSITIVE";  add(40, f"Rating upgrade: {action}")
        elif "downgrade" in action:
            direction = "NEGATIVE";  add(50, f"Rating downgrade: {action}")
        else:
            add(10, f"Rating action: {action or 'review'}")
    elif family == "LITIGATION_REGULATORY":
        direction = "NEGATIVE"; add(40, "Litigation / regulatory risk")
    else:
        add(10, f"Standard event: {family}")
    impact = min(max(impact, 0), 100)
    has_issues = bool(ext.get("_validation_issues"))
    if (has_issues and confidence < 0.7) or confidence < 0.65:
        rec = "NEEDS_MANUAL_REVIEW"
    elif impact >= 75:
        rec = "ACTIONABLE_BULLISH" if direction == "POSITIVE" else "ACTIONABLE_BEARISH_RISK"
    elif impact >= 55:
        rec = "HIGH_PRIORITY_WATCH"
    else:
        rec = "TRACK"
    return {"impact_score": impact, "direction": direction, "action_recommendation": rec,
            "scoring_factors": factors, "conversion_bonus": conversion_bonus,
            "execution_months": exec_months, "order_type": order_type}


# Semantic system instruction — from Bulk_reg30_processor.py
_EXTRACTION_SYSTEM = """You are an expert Indian equity analyst reading NSE Regulation 30 corporate announcements.

Your job is to extract structured data from the attached document or announcement summary. Use your full reasoning ability — do not just pattern-match to examples.

WHAT TO EXTRACT:
- Company identity: NSE symbol, company name
- Contract basics: who awarded it, domestic or international, nature of work
- Contract value: the total consideration/size in Indian Crores (convert if needed: 1 Crore = 10 million INR = 100 Lakhs; USD 1M ≈ 8.4 Cr)
- Contract stage: L1 (lowest bidder, not yet awarded) | LOA (Letter of Award received) | WO (Work Order / Purchase Order / Contract signed / awarded / secured / bagged) | NTP (Notice to Proceed) | MOU | OTHER
- Duration: how long the contract runs — convert to integer months
  (Two Years = 24, 18 months = 18, 90 days = 3, immediately = 1)
  For HAM/BOT infrastructure contracts, use only the construction period, not the O&M period
- Order type: CONSTRUCTION | SUPPLY | SERVICES | SUB-CONTRACT
  Infer from what is being done — civil/road/power/EPC = CONSTRUCTION, goods/equipment = SUPPLY,
  IT/O&M/surveillance/consulting = SERVICES, work for a main contractor = SUB-CONTRACT
- Whether the order was won by a subsidiary (not the listed parent company itself)
- Whether the announcement covers multiple separate orders

RULES:
1. Never fabricate numbers. If something is genuinely absent, return null.
2. For contract value, prefer the "Broad consideration" or "size of contract" field if present.
   Otherwise extract from any clear statement in the text (e.g. "valued at Rs. 500 crore").
3. Return STRICT JSON only — no markdown, no explanation outside the JSON.
4. Confidence: your honest 0.0–1.0 estimate of extraction quality.
   Use < 0.7 if the document is unclear, scanned poorly, or key fields are missing.
"""

_EXTRACTION_PROMPT = """Read this NSE Reg30 announcement and extract all fields.

Company (from dataset): {company_name}
NSE Ticker (from dataset): {nse_ticker}
Filing Date: {published_date}

Return this exact JSON structure:
{{
  "summary": "2-3 sentences describing what happened in plain English",
  "direction_hint": "POSITIVE",
  "confidence": 0.85,
  "missing_fields": [],
  "evidence_spans": ["short direct quotes (max 120 chars each) supporting key extractions"],
  "extracted": {{
    "nse_symbol": "NSE ticker symbol",
    "company_name": "Full legal company name",
    "customer": "Name of the entity that awarded the contract",
    "international": false,
    "order_value_cr": 500.0,
    "original_currency": null,
    "original_value": null,
    "stage": "WO",
    "execution_months": 18,
    "execution_period_text": "exact duration phrase from document",
    "construction_period_months": null,
    "contract_mode": "EPC",
    "order_type": "CONSTRUCTION",
    "is_subsidiary_win": false,
    "subsidiary_name": null,
    "multiple_orders": false,
    "new_customer": null,
    "conditionality": "LOW",
    "prior_stage": null,
    "rating_action": null,
    "market_cap_cr": null,
    "networth_cr": null
  }}
}}"""


@app.route('/api/gemini/reg30-analyze', methods=['POST', 'OPTIONS'])
@cross_origin()
def reg30_analyze():
    """
    Reg30 analysis endpoint — ported from Bulk_reg30_processor.py.
    Fetches PDF bytes directly, sends to Gemini multimodal, validates extraction,
    runs scoring server-side. Returns complete payload ready for Supabase upsert.
    """
    if request.method == 'OPTIONS':
        return jsonify(success=True)
    initialize_ai_clients()
    if not ai_client:
        return jsonify({"error": "Gemini AI client not initialized"}), 500
    try:
        import requests as _req
        data = request.get_json(silent=True) or {}
        candidate = data.get('candidate') or {}

        company_name   = (candidate.get('company_name') or 'Unknown').strip()
        symbol         = (candidate.get('symbol') or '').strip()
        source_link    = (candidate.get('attachment_link') or candidate.get('source_link') or '').strip()
        raw_text       = (candidate.get('raw_text') or '').strip()
        published_date = (candidate.get('event_date') or candidate.get('published_date') or '').strip()
        event_date     = _norm_date(published_date)
        event_datetime = _norm_datetime(published_date)

        # ── Fetch PDF bytes ────────────────────────────────────────────────────
        pdf_bytes = None
        if source_link and source_link.startswith('http'):
            try:
                r = _req.get(source_link,
                             headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/pdf,*/*'},
                             timeout=15)
                if r.status_code == 200 and len(r.content) >= 500 and r.content[:4] == b'%PDF':
                    pdf_bytes = r.content
                    logger.info(f"[Reg30] PDF fetched: {len(pdf_bytes)}b {source_link[:80]}")
                else:
                    logger.warning(f"[Reg30] PDF fetch failed HTTP {r.status_code} or not PDF: {source_link[:80]}")
            except Exception as e:
                logger.warning(f"[Reg30] PDF fetch error: {e}")

        # ── Build Gemini content parts ─────────────────────────────────────────
        prompt_text = _EXTRACTION_PROMPT.format(
            company_name=company_name,
            nse_ticker=symbol or company_name,
            published_date=event_date,
        )
        if pdf_bytes:
            content_parts = [
                types.Part(inline_data=types.Blob(data=pdf_bytes, mime_type='application/pdf')),
                types.Part(text=prompt_text),
            ]
        elif raw_text:
            if len(raw_text) < 20:
                return jsonify({"error": "No PDF accessible and summary too short"}), 400
            content_parts = [types.Part(text=f"Announcement Summary:\n{raw_text}\n\n{prompt_text}")]
        else:
            return jsonify({"error": "No PDF URL provided and no summary text available"}), 400

        # ── Gemini extraction ──────────────────────────────────────────────────
        result = None
        for model_name in get_gemini_model_candidates():
            try:
                response = ai_client.models.generate_content(
                    model=model_name,
                    contents=content_parts,
                    config=types.GenerateContentConfig(
                        system_instruction=_EXTRACTION_SYSTEM,
                        temperature=0.1,
                    ),
                )
                result = extract_json(response.text)
                if result and isinstance(result.get('summary'), str):
                    logger.info(f"[Reg30] Extracted via {model_name} for {symbol}")
                    break
            except Exception as e:
                logger.warning(f"[Reg30] Gemini ({model_name}): {e}")
                continue

        if not result:
            return jsonify({"error": "Reg30 analysis failed — Gemini returned no valid JSON"}), 500

        # ── Classify + Validate + Score ────────────────────────────────────────
        raw_ext    = result.get('extracted') or {}
        summary    = result.get('summary') or ''
        confidence = float(result.get('confidence') or 0.5)

        family = _classify_family(summary + ' ' + raw_text + ' ' + (raw_ext.get('order_type') or ''),
                                   raw_ext.get('stage'))
        # Upgrade OTHER if extraction has evidence of a contract
        if family == 'OTHER' and (
            raw_ext.get('order_value_cr') or
            raw_ext.get('stage') in ('L1', 'LOA', 'WO', 'NTP') or
            raw_ext.get('customer')
        ):
            family = 'ORDER_CONTRACT'

        ext, v_issues = _validate_extraction(raw_ext, summary, family)
        if v_issues:
            logger.info(f"[Reg30] {symbol} validation: {v_issues}")

        # Resolve end_date → execution_months when Gemini returned an end date
        if ext.get('_end_date_pending_resolution') and not ext.get('execution_months'):
            try:
                end   = datetime.datetime.strptime(str(ext['_end_date_pending_resolution'])[:10], '%Y-%m-%d')
                start = datetime.datetime.strptime(event_date, '%Y-%m-%d')
                months = max(1, round((end - start).days / 30.4))
                if 0 < months <= 600:
                    ext['execution_months'] = months
            except Exception:
                pass

        scoring = _calculate_score(family, ext, confidence)

        resolved_symbol  = (ext.get('nse_symbol') or ext.get('symbol') or symbol or '').strip().upper()
        resolved_company = (ext.get('company_name') or company_name or 'Unknown').strip()

        logger.info(
            f"[Reg30] {resolved_symbol} | family={family} stage={ext.get('stage')} "
            f"order_cr={ext.get('order_value_cr')} exec_months={scoring.get('execution_months')} "
            f"score={scoring['impact_score']} rec={scoring['action_recommendation']}"
        )

        return jsonify({
            **result,
            "symbol":                resolved_symbol,
            "company_name":          resolved_company,
            "event_date":            event_date,
            "event_datetime":        event_datetime,
            "extracted":             ext,
            "event_family":          family,
            "impact_score":          scoring["impact_score"],
            "direction":             scoring["direction"],
            "action_recommendation": scoring["action_recommendation"],
            "scoring_factors":       scoring["scoring_factors"],
            "conversion_bonus":      scoring.get("conversion_bonus", 0),
            "execution_months":      scoring.get("execution_months"),
            "order_type":            scoring.get("order_type"),
            "_validation_issues":    v_issues or [],
            "_proxy_scored":         True,
        })
    except Exception as e:
        logger.exception("Reg30 analyze error")
        return jsonify({"error": str(e)}), 500


@app.route('/api/gemini/reg30-narrative', methods=['POST', 'OPTIONS'])
@cross_origin()
def reg30_narrative():
    """Generate an elaborate tactical event analysis narrative (2-4 paragraphs)."""
    if request.method == 'OPTIONS':
        return jsonify(success=True)
    initialize_ai_clients()
    if not ai_client:
        return jsonify({"error": "Gemini AI client not initialized"}), 500
    try:
        data = request.get_json(silent=True) or {}
        symbol = (data.get('symbol') or '').strip()
        company_name = (data.get('company_name') or 'Unknown').strip()
        event_family = (data.get('event_family') or 'ORDER_CONTRACT').strip()
        stage = (data.get('stage') or '')
        order_value_cr = data.get('order_value_cr')
        customer = (data.get('customer') or '')
        summary = (data.get('summary') or '')[:2000]
        impact_score = int(data.get('impact_score') or 0)
        institutional_risk = (data.get('institutional_risk') or 'LOW').strip()
        policy_bias = (data.get('policy_bias') or 'NEUTRAL').strip()
        tactical_plan = (data.get('tactical_plan') or 'MOMENTUM_OK').strip()
        trigger_text = (data.get('trigger_text') or '').strip()

        sys_instr = (
            "You are a Senior Tactical Analyst for Indian Equities. "
            "Write a detailed, professional event analysis (2-4 short paragraphs) for tactical traders. "
            "Cover: (1) impact on revenue visibility and alignment with sectoral policy, "
            "(2) execution risk profile and deal magnitude as a liquidity event, "
            "(3) institutional profit-taking and sell-on-news risk, near-term pullback potential, "
            "(4) prudent entry strategies and what to guard against (e.g. distribution, bid-on-news). "
            "Use neutral, analytical tone. Do not invent prices or targets. "
            "Output STRICT JSON only: {\"event_analysis_text\": \"<full narrative>\", \"tone\": \"analytical\"}."
        )
        prompt = (
            f"Company: {company_name} (Symbol: {symbol or 'N/A'}). "
            f"Event: {event_family}, stage {stage}. "
            f"Order value: ₹{order_value_cr} Cr. Customer: {customer or 'Not disclosed'}. "
            f"Summary: {summary}\n\n"
            f"Impact score: {impact_score}. Institutional risk: {institutional_risk}. Policy bias: {policy_bias}. "
            f"Tactical plan: {tactical_plan}. Trigger: {trigger_text}\n\n"
            "Write the detailed event analysis as specified in the system instruction."
        )

        for model_name in get_gemini_model_candidates():
            try:
                response = ai_client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(system_instruction=sys_instr),
                )
                out = extract_json(response.text)
                if out and isinstance(out.get('event_analysis_text'), str):
                    return jsonify({
                        "event_analysis_text": out["event_analysis_text"].strip(),
                        "tone": out.get("tone") or "analytical"
                    })
            except Exception as e:
                logger.warning(f"Reg30 narrative ({model_name}): {e}")
                continue
        return jsonify({"error": "Narrative generation failed"}), 500
    except Exception as e:
        logger.exception("Reg30 narrative error")
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────
# SOCKET.IO HANDLERS
# ─────────────────────────────────────────────
@socketio.on('connect')
def handle_connect():
    global _tick_dispatcher_started
    logger.info(f"Client connected: {request.sid}")
    # Start the tick dispatcher greenlet on the very first client connection.
    # socketio.start_background_task spawns an eventlet greenlet where socketio.emit() works correctly.
    if not _tick_dispatcher_started:
        _tick_dispatcher_started = True
        socketio.start_background_task(_run_tick_dispatcher)


@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    logger.info(f"Client disconnected: {sid}")
    _unregister_sid(sid)


@socketio.on('subscribe_to_watchlist')
def handle_watchlist_subscription(data):
    sid = request.sid
    stock_list = data.get('stocks', [])
    proxy_key = data.get('proxy_key', '')
    logger.info(f"Client {sid} subscribed to watchlist: {stock_list}")
    socketio.start_background_task(track_watchlist, stock_list, proxy_key, sid)


def track_watchlist(stock_list, proxy_key, sid):
    """
    Uses Breeze WebSocket feeds for real-time watchlist updates.

    Uses a module-level _tick_registry so that multiple Socket.IO clients (e.g., the
    Nifty card socket and the Watchlist socket in the browser) each receive the ticks
    they subscribed to, without one overwriting the other's on_ticks callback.

    When the SID disconnects, its registry entries are cleaned up and any Breeze feeds
    that have no remaining subscribers are unsubscribed.
    """
    global _subscribed_breeze_codes

    client, err_resp, _ = ensure_breeze_session()
    if err_resp:
        logger.error(f"Could not get Breeze session for watchlist (sid={sid}).")
        socketio.emit('watchlist_error', {"error": "Breeze session not available"}, room=sid)
        return

    # Build the global symbol map used by _global_on_ticks to resolve tick symbols.
    # Key insight: Breeze WebSocket ticks carry the Breeze short code (e.g. "NIFTY 50", "MEDREM"),
    # so we must map BOTH the canonical frontend symbol AND the raw Breeze code to the frontend symbol.
    for symbol in stock_list:
        std = canonical_symbol(symbol)
        breeze_code = get_breeze_symbol(std)  # raw Breeze code, e.g. "NIFTY 50", "MEDREM"
        _registry_symbol_map[std] = std
        # Canonical form of the Breeze code (strips "NIFTY 50" → "NIFTY", "MEDREM" stays)
        _registry_symbol_map[canonical_symbol(breeze_code)] = std
        # Also map the raw Breeze code directly (e.g. "NIFTY 50" with space)
        _registry_symbol_map[breeze_code.strip().upper()] = std

    # Register this SID for each symbol so _global_on_ticks dispatches to it.
    for symbol in stock_list:
        _register_tick_sid(canonical_symbol(symbol), sid)

    # Assign the global dispatcher once (idempotent — any subsequent assignment is the same function).
    client.on_ticks = _global_on_ticks

    # Connect Breeze WebSocket.
    # Clear _subscribed_breeze_codes first: a reconnecting WebSocket has no existing
    # subscriptions, so we must not skip re-subscribing based on stale data.
    _subscribed_breeze_codes.clear()
    try:
        client.ws_connect()
        logger.info(f"Breeze WebSocket connected for {sid}")
    except Exception as e:
        # ws_connect raises if already connected; safe to continue — feeds will be added below.
        logger.warning(f"ws_connect for {sid}: {e}")

    # Subscribe to Breeze feeds.
    newly_subscribed = []
    for symbol in stock_list:
        std = canonical_symbol(symbol)
        # get_breeze_symbol returns the correct Breeze code (e.g. "NIFTY 50", "MEDREM").
        # Do NOT call canonical_symbol on the result — it would turn "NIFTY 50" back to "NIFTY".
        breeze_code = get_breeze_symbol(std)
        if breeze_code in _subscribed_breeze_codes:
            logger.info(f"Feed already active: {symbol} ({breeze_code}) — skipping")
            continue
        try:
            client.subscribe_feeds(
                exchange_code="NSE",
                stock_code=breeze_code,
                product_type="cash",
                get_exchange_quotes=True,
                get_market_depth=False
            )
            _subscribed_breeze_codes.add(breeze_code)
            newly_subscribed.append((breeze_code, symbol))
            logger.info(f"Subscribed to feed: {symbol} → Breeze code '{breeze_code}'")
        except Exception as e:
            logger.error(f"Failed to subscribe to {symbol} ({breeze_code}): {e}")

    # Emit an initial quote snapshot for every subscribed symbol via REST.
    # Breeze WebSocket only pushes ticks on NEW trade activity — illiquid small-caps
    # can go minutes without a tick, leaving the UI stuck on "Awaiting...".
    # Fetching the REST quote immediately after subscribing bootstraps the UI with
    # the last traded price, change, and bid/ask even before any WebSocket tick arrives.
    socketio.sleep(0.3)  # allow WebSocket subscribe ACKs to arrive before REST calls
    for symbol in stock_list:
        std = canonical_symbol(symbol)
        breeze_code = get_breeze_symbol(std)
        try:
            res = client.get_quotes(
                stock_code=breeze_code,
                exchange_code="NSE",
                product_type="cash"
            )
            raw = normalize_breeze_response(res)
            # get_quotes returns {"Success": [<single dict>]}.
            # normalize_breeze_response unwraps to the list; take the first (only) element.
            if isinstance(raw, list):
                raw = raw[0] if raw else None
            if raw and isinstance(raw, dict):
                payload = normalize_tick_for_frontend(dict(raw), std)
                socketio.emit('watchlist_update', payload, to=sid, namespace='/')
                logger.info(f"Initial quote emitted: {symbol} ltp={payload.get('ltp')}")
        except Exception as e:
            logger.warning(f"Initial quote fetch failed for {symbol}: {e}")

    # Keep background task alive while this SID is connected.
    # Ticks arrive via _global_on_ticks — no polling needed.
    while True:
        try:
            if not socketio.server.manager.is_connected(sid, '/'):
                logger.info(f"Client {sid} disconnected — cleaning up feeds.")
                break
        except Exception:
            break
        socketio.sleep(5)

    # SID disconnected: remove from registry (also done in handle_disconnect, but belt-and-suspenders).
    _unregister_sid(sid)

    # Unsubscribe feeds that now have no registered subscribers.
    for breeze_code, symbol in newly_subscribed:
        # Check if any other SID still needs this symbol.
        std = canonical_symbol(symbol)
        remaining = _tick_registry.get(std, set())
        if not remaining:
            try:
                client.unsubscribe_feeds(
                    exchange_code="NSE",
                    stock_code=breeze_code,
                    product_type="cash",
                    get_exchange_quotes=True,
                    get_market_depth=False
                )
                _subscribed_breeze_codes.discard(breeze_code)
                logger.info(f"Unsubscribed feed (no more subscribers): {symbol} ({breeze_code})")
            except Exception as e:
                logger.error(f"Error unsubscribing {symbol}: {e}")

    # Disconnect Breeze WebSocket only when there are no more active subscribers at all.
    if not any(s for sids in _tick_registry.values() for s in sids):
        try:
            client.ws_disconnect()
            logger.info("Breeze WebSocket disconnected — no more subscribers")
        except Exception as e:
            logger.error(f"Error disconnecting WebSocket: {e}")


# ─────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────
if __name__ == "__main__":
    initialize_ai_clients()
    port = int(os.environ.get("PORT", 8082))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)