import secrets
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO
from breeze_connect import BreezeConnect
import os
import json
import logging
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
# Robust CORS: Handles both standard HTTP and SocketIO handshakes
CORS(app, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*")

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Global State & Cache ---
_secret_cache = {}
breeze_client = None
DAILY_SESSION_TOKEN = None

def get_secret(secret_name):
    """Fetch secrets from environment variables with local caching."""
    if secret_name in _secret_cache:
        return _secret_cache[secret_name]
    
#<<<<<<< Updated upstream
    # Load from environment variables (loaded from .env file)
    val = os.environ.get(secret_name)
    
    if val:
        logger.info(f"Loaded secret '{secret_name}' from environment variable.")
        _secret_cache[secret_name] = val
    else:
        logger.error(f"Failed to find secret '{secret_name}' in environment variables.")
    
    return val
#=======
    # Use environment variable or fallback to your project ID
    project_id = os.environ.get("GCP_PROJECT_ID", "919207294606")
    
    try:
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        response = client.access_secret_version(request={"name": name})
        val = response.payload.data.decode("UTF-8")
        _secret_cache[secret_name] = val
        logger.info(f"Successfully fetched secret: {secret_name}")
        return val
    except Exception as e:
        logger.error(f"Failed to fetch secret '{secret_name}': {e}")
        # Final fallback to standard environment variables (e.g. for local .env)
        return os.environ.get(secret_name)
#>>>>>>> Stashed changes

def initialize_breeze():
    """Initializes the BreezeConnect client only if needed."""
    global breeze_client
    if breeze_client is None:
        try:
            api_key = get_secret("BREEZE_API_KEY") 
            if not api_key:
                logger.error("API Key is missing from Secret Manager/Env.")
                return None

            breeze_client = BreezeConnect(api_key=api_key)
            logger.info(f"BreezeConnect initialized (Key ending in: {api_key[-4:]})")
        except Exception as e:
            logger.error(f"Breeze initialization error: {e}")
    return breeze_client

def ensure_breeze_session():
    """Validates the active session before processing data requests."""
    client = initialize_breeze()
    if not client:
        return None, jsonify({"error": "Breeze client not initialized"}), 500
    
    # If we have a token but client hasn't been "logged in" yet
    if not client.session_key and DAILY_SESSION_TOKEN:
        try:
            client.generate_session(
                api_secret=get_secret("BREEZE_API_SECRET"), 
                session_token=DAILY_SESSION_TOKEN
            )
            logger.info("Breeze session regenerated successfully.")
        except Exception as e:
            return None, jsonify({"error": f"Session generation failed: {e}"}), 401
    elif not client.session_key:
        return None, jsonify({"error": "Session token missing. Use /api/breeze/admin/api-session"}), 401
    
    return client, None, None

# --- API Routes ---

@app.route("/api/", methods=["GET"])
def root_health():
    """Root health check for Cloud Run service monitoring."""
    return jsonify({
        "status": "ok",
        "service": "breeze-proxy",
        "version": "1.0.0",
        "session_active": bool(DAILY_SESSION_TOKEN)
    })

@app.route("/api/breeze/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok", 
        "session_active": bool(DAILY_SESSION_TOKEN),
        "client_ready": breeze_client is not None
    })

@app.route("/api/breeze/admin/api-session", methods=["POST", "OPTIONS"])
def set_session():
    """Endpoint to update the daily session token from the UI."""
    global DAILY_SESSION_TOKEN
    
    # Handle CORS preflight automatically via Flask-CORS, but defined for clarity
    if request.method == "OPTIONS":
        return "", 200

    data = request.get_json() or {}
    api_session = data.get("api_session")

    # 1. Admin Authorization
    provided_key = request.headers.get('X-Proxy-Admin-Key', '').strip()
    ADMIN_KEY = get_secret("BREEZE_PROXY_ADMIN_KEY")
    
    if not ADMIN_KEY or not secrets.compare_digest(provided_key, ADMIN_KEY.strip()):
        logger.warning(f"Unauthorized session attempt with key: {provided_key}")
        return jsonify({"error": "Unauthorized"}), 401

    if not api_session:
        return jsonify({"error": "api_session token is required"}), 400
    
    # 2. Token Exchange
    client = initialize_breeze()
    if not client:
        return jsonify({"error": "Breeze client initialization failed"}), 500
        
    try:
        client.generate_session(
            api_secret=get_secret("BREEZE_API_SECRET"),
            session_token=api_session
        )
        DAILY_SESSION_TOKEN = api_session
        logger.info("Daily session activated and stored.")
        return jsonify({"status": "success", "message": "Daily session activated"}), 200
    except Exception as e:
        logger.error(f"Failed to generate session: {e}")
        return jsonify({"error": "Failed to activate session", "details": str(e)}), 500

@app.route("/api/breeze/quotes", methods=["POST"])
def get_quotes():
    """Fetch real-time quotes for a specific stock."""
    client, err_resp, status_code = ensure_breeze_session()
    if err_resp: return err_resp, status_code

    data = request.get_json() or {}
    stock_code = data.get("stock_code")
    if not stock_code: 
        return jsonify({"error": "stock_code required"}), 400

    try:
        raw_data = client.get_quotes(
            stock_code=stock_code, 
            exchange_code=data.get("exchange_code", "NSE"), 
            product_type="cash"
        )
        
        if raw_data and raw_data.get("Success"):
            row = raw_data["Success"][0]
            # Mapping internal breeze keys to clean frontend names
            formatted = {
                "last_traded_price": float(row.get("ltp", 0)),
                "change": float(row.get("change", 0)),
                "percent_change": float(row.get("ltp_percent_change", 0)),
                "high": float(row.get("high", 0)),
                "low": float(row.get("low", 0)),
                "volume": float(row.get("total_quantity_traded", 0)),
                "previous_close": float(row.get("previous_close", 0))
            }
            return jsonify({"Success": formatted}), 200
        return jsonify({"error": "Breeze returned no data"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/breeze/depth", methods=["POST"])
def get_depth():
    """Fetch Level 2 Market Depth (Buy/Sell Ladders)."""
    client, err_resp, status_code = ensure_breeze_session()
    if err_resp: return err_resp, status_code

    data = request.get_json() or {}
    stock_code = data.get("stock_code")
    if not stock_code:
        return jsonify({"error": "stock_code required"}), 400

    try:
        # Using get_market_depth2 as per current Breeze API standards
        res = client.get_market_depth2(
            stock_code=stock_code,
            exchange_code=data.get("exchange_code", "NSE"),
            product_type="cash"
        )
        return jsonify(res), 200
    except Exception as e:
        logger.error(f"Market Depth Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/breeze/historical", methods=["POST"])
def get_historical():
    """Fetch historical OHLC data."""
    client, err_resp, status_code = ensure_breeze_session()
    if err_resp: return err_resp, status_code

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
        return jsonify(res), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- SocketIO Handlers ---

@socketio.on('connect')
def handle_connect():
    logger.info('Frontend Socket connected')

@socketio.on('disconnect')
def handle_disconnect():
    logger.info('Frontend Socket disconnected')

# --- Startup Execution ---

if __name__ == "__main__":
    # Standard Cloud Run logic: use the injected PORT or local safe 8081
    port = int(os.environ.get("PORT", 8081))
    
    logger.info("=" * 70)
    logger.info(f"🚀 MAIA Proxy starting on Port: {port}")
    logger.info("=" * 70)
    
    try:
        # Use socketio.run to support real-time features properly
        socketio.run(app, host="0.0.0.0", port=port, debug=False)
    except Exception as e:
        logger.error(f"❌ Server failed: {e}")