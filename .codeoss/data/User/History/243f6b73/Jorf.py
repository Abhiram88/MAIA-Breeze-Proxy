import secrets
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO
from breeze_connect import BreezeConnect
import os
import logging
from dotenv import load_dotenv

# Load environment variables from .env file for local testing
load_dotenv()

app = Flask(__name__)
# Enable CORS for all routes (essential for frontend connectivity)
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
    
    # Priority: Cloud Run Environment -> Local .env
    val = os.environ.get(secret_name)
    
    if val:
        logger.info(f"Loaded secret '{secret_name}' successfully.")
        _secret_cache[secret_name] = val
    else:
        logger.error(f"Failed to find secret '{secret_name}' in environment.")
    
    return val

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
            logger.info(f"BreezeConnect initialized.")
        except Exception as e:
            logger.error(f"Breeze initialization error: {e}")
    return breeze_client

def ensure_breeze_session():
    """Validates the active session before processing data requests."""
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

# --- API Routes ---

@app.route("/api/", methods=["GET"])
def root_health():
    """Service health check."""
    return jsonify({"status": "ok", "service": "maia-breeze-proxy"})

@app.route("/api/breeze/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "session_active": bool(DAILY_SESSION_TOKEN)})

@app.route("/api/breeze/admin/api-session", methods=["POST", "OPTIONS"])
def set_session():
    """Activate the daily session token."""
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
    try:
        api_secret = get_secret("BREEZE_API_SECRET")
        client.generate_session(api_secret=api_secret, session_token=api_session)
        DAILY_SESSION_TOKEN = api_session
        return jsonify({"status": "success", "message": "Daily session activated"}), 200
    except Exception as e:
        logger.error(f"Session Error: {e}")
        return jsonify({"error": "Failed to generate session", "details": str(e)}), 500

@app.route("/api/breeze/quotes", methods=["POST"])
def get_quotes():
    """Fetch real-time stock quotes."""
    client, err_resp, status_code = ensure_breeze_session()
    if err_resp: return err_resp, status_code

    data = request.get_json() or {}
    try:
        res = client.get_quotes(
            stock_code=data.get("stock_code"), 
            exchange_code=data.get("exchange_code", "NSE"), 
            product_type="cash"
        )
        return jsonify(res), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/breeze/depth", methods=["POST"])
def get_depth():
    """Fetch L2 Market Depth."""
    client, err_resp, status_code = ensure_breeze_session()
    if err_resp: return err_resp, status_code

    data = request.get_json() or {}
    try:
        res = client.get_market_depth2(
            stock_code=data.get("stock_code"),
            exchange_code=data.get("exchange_code", "NSE"),
            product_type="cash"
        )
        return jsonify(res), 200
    except Exception as e:
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

# --- Startup ---
if __name__ == "__main__":
    # Cloud Run injected port or local safe 8082
    port = int(os.environ.get("PORT", 8082))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)