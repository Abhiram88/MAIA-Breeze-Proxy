import secrets
import os
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO
from breeze_connect import BreezeConnect
from dotenv import load_dotenv

# 1. Load local .env (only used for local testing)
load_dotenv()

app = Flask(__name__)
# 2. CORS: Explicitly allow the origin from your logs to stop the 'preflight' error
CORS(app, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Global State ---
breeze_client = None
DAILY_SESSION_TOKEN = None

def get_config(key):
    """Priority: Environment Variable (Cloud) > .env file (Local)"""
    val = os.environ.get(key)
    if not val:
        logger.warning(f"⚠️ Configuration key '{key}' is missing!")
    return val

def initialize_breeze():
    """Initializes the Breeze client using your Spyder logic."""
    global breeze_client
    if breeze_client is None:
        try:
            api_key = get_config("BREEZE_API_KEY")
            if api_key:
                breeze_client = BreezeConnect(api_key=api_key)
                logger.info("✅ Breeze Client Initialized")
        except Exception as e:
            logger.error(f"❌ Breeze Init Error: {e}")
    return breeze_client

# --- API Routes (Matching your /api/breeze/ structure) ---

@app.route("/api/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "Server is live"})

@app.route("/api/breeze/admin/api-session", methods=["POST", "OPTIONS"])
def set_session():
    """Handshake route to activate the session token."""
    global DAILY_SESSION_TOKEN
    if request.method == "OPTIONS": return "", 200

    data = request.get_json() or {}
    api_session = data.get("api_session")
    provided_key = request.headers.get('X-Proxy-Admin-Key', '').strip()
    
    # 1. Verify Admin Key
    ADMIN_KEY = get_config("BREEZE_PROXY_ADMIN_KEY")
    if not ADMIN_KEY or not secrets.compare_digest(provided_key, ADMIN_KEY.strip()):
        return jsonify({"error": "Unauthorized"}), 401

    # 2. Generate Breeze Session (Spyder Logic)
    client = initialize_breeze()
    try:
        api_secret = get_config("BREEZE_API_SECRET")
        client.generate_session(api_secret=api_secret, session_token=api_session)
        DAILY_SESSION_TOKEN = api_session
        logger.info(f"🚀 Session Activated for token: {api_session}")
        return jsonify({"status": "success", "message": "Breeze session activated"}), 200
    except Exception as e:
        logger.error(f"❌ Session Activation Failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/breeze/quotes", methods=["POST"])
def get_quotes():
    """Route for market intelligence quotes."""
    if not DAILY_SESSION_TOKEN or not breeze_client:
        return jsonify({"error": "Session not active. Call /admin/api-session first."}), 401
    
    data = request.get_json() or {}
    try:
        res = breeze_client.get_quotes(
            stock_code=data.get("stock_code"),
            exchange_code=data.get("exchange_code", "NSE"),
            product_type="cash"
        )
        return jsonify(res), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- SocketIO Handlers ---
@socketio.on('connect')
def test_connect():
    logger.info('✅ Socket Client Connected')

# --- Start Server ---
if __name__ == "__main__":
    # Cloud Run always uses PORT=8080. Local use 8082 to avoid Jupyter conflict.
    port = int(os.environ.get("PORT", 8082))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)