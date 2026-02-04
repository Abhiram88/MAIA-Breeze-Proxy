import secrets
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO
from breeze_connect import BreezeConnect
from google.cloud import secretmanager
import os
import json
import logging

app = Flask(__name__)
# Initialize CORS for the entire app, allowing all origins.
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
    """Fetch secrets from Google Secret Manager with local caching."""
    if secret_name in _secret_cache:
        return _secret_cache[secret_name]
    
    # Cloud Run provides the project ID via environment variable or use fallback
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
        logger.error(f"Failed to fetch secret '{secret_name}' from Secret Manager: {e}")
        # Fallback to standard environment variables (useful for local .env testing)
        val = os.environ.get(secret_name)
        if val:
            _secret_cache[secret_name] = val
        return val

def initialize_breeze():
    global breeze_client
    if breeze_client is None:
        try:
            api_key = get_secret("BREEZE_API_KEY") 
            if not api_key:
                logger.error("API Key is empty! Check your exports or Secret Manager.")
                return None

            breeze_client = BreezeConnect(api_key=api_key)
            logger.info(f"BreezeConnect initialized with key ending in: {api_key[-4:]}")
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
            client.generate_session(api_secret=get_secret("BREEZE_API_SECRET"), session_token=DAILY_SESSION_TOKEN)
            logger.info("Breeze session regenerated.")
        except Exception as e:
            return None, jsonify({"error": f"Session invalid: {e}"}), 401
    elif not client.session_key:
        return None, jsonify({"error": "Breeze session token not set. Use /api/breeze/admin/api-session"}), 401
    
    return client, None, None

# --- API Routes (Prefix fixed to /api to match frontend) ---

@app.route("/api/", methods=["GET"])
def root_health():
    """Root health check for Cloud Run."""
    return jsonify({
        "status": "ok",
        "service": "breeze-proxy",
        "version": "1.0.0"
    })

@app.route("/api/breeze/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "session_active": bool(DAILY_SESSION_TOKEN)})

@app.route("/api/breeze/admin/api-session", methods=["POST"])
def set_session():
    """Handshake from UI to activate the daily data pipe."""
    global DAILY_SESSION_TOKEN
    data = request.get_json() or {}
    api_session = data.get("api_session")

    # 1. Validate Admin Key
    provided_key = request.headers.get('X-Proxy-Admin-Key', '').strip()
    ADMIN_KEY = get_secret("BREEZE_PROXY_ADMIN_KEY")
    
    if not ADMIN_KEY:
        return jsonify({"error": "Server Error: BREEZE_PROXY_ADMIN_KEY not loaded"}), 500

    if not secrets.compare_digest(provided_key, ADMIN_KEY.strip()):
        return jsonify({"error": "Unauthorized"}), 401

    if not api_session:
        return jsonify({"error": "api_session is required"}), 400
    
    # 2. Initialize and Exchange Token
    client = initialize_breeze()
    if not client:
        return jsonify({"error": "Breeze client not initialized"}), 500
        
    try:
        api_secret = get_secret("BREEZE_API_SECRET")
        client.generate_session(api_secret=api_secret, session_token=api_session)
        DAILY_SESSION_TOKEN = api_session
        logger.info("Successfully generated and activated new session.")

        return jsonify({"status": "success", "message": "Daily session activated"}), 200
    except Exception as e:
        logger.error(f"Failed to generate session: {e}")
        return jsonify({"error": "Failed to generate session", "details": str(e)}), 500

@app.route("/api/breeze/quotes", methods=["POST"])
def get_quotes():
    client, err_resp, status_code = ensure_breeze_session()
    if err_resp: return err_resp, status_code

    data = request.get_json() or {}
    stock_code = data.get("stock_code")
    if not stock_code: return jsonify({"error": "stock_code required"}), 400

    try:
        raw_data = client.get_quotes(
            stock_code=stock_code, 
            exchange_code=data.get("exchange_code", "NSE"), 
            product_type="cash"
        )
        if raw_data and raw_data.get("Success"):
            row = raw_data["Success"][0]
            return jsonify({"Success": {
                "last_traded_price": float(row.get("ltp", 0)),
                "change": float(row.get("change", 0)),
                "percent_change": float(row.get("ltp_percent_change", 0)),
                "volume": float(row.get("total_quantity_traded", 0))
            }}), 200
        return jsonify({"error": "No data returned from Breeze"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/breeze/depth", methods=["POST"])
def get_depth():
    client, err_resp, status_code = ensure_breeze_session()
    if err_resp: return err_resp, status_code

    data = request.get_json() or {}
    stock_code = data.get("stock_code")
    if not stock_code: return jsonify({"error": "stock_code required"}), 400

    try:
        res = client.get_market_depth2(
            stock_code=stock_code,
            exchange_code=data.get("exchange_code", "NSE"),
            product_type="cash"
        )
        return jsonify(res), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@socketio.on('connect')
def handle_connect():
    logger.info('Client connected')

if __name__ == "__main__":
    # Standard Cloud Run port binding
    port = int(os.environ.get("PORT", 8080))
    
    logger.info("=" * 70)
    logger.info(f"🚀 Starting Breeze Proxy Server on Port {port}")
    logger.info("=" * 70)
    
    try:
        app.run(host="0.0.0.0", port=port, debug=False)
    except Exception as e:
        logger.error(f"❌ Startup Error: {e}")