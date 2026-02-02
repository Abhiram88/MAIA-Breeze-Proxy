# breeze_proxy_app.py
from flask import Flask, request, jsonify
from breeze_connect import BreezeConnect
from google.cloud import secretmanager
import os
import json
import pytz # Added for timezone handling
from datetime import datetime # Added for datetime handling

app = Flask(__name__)

# --- Helper to get secrets from Google Secret Manager ---
def get_secret(secret_name):
    project_id = os.environ.get("GCP_PROJECT_ID") # Set this env var in Cloud Run
    if not project_id:
        raise ValueError("GCP_PROJECT_ID environment variable not set.")
    
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")

# --- Global BreezeConnect instance and session management ---
breeze = None
DAILY_SESSION_TOKEN = None # This will be set via an admin endpoint

def initialize_breeze():
    global breeze
    if breeze is None:
        try:
            # Fetch secrets from Secret Manager
            breeze_api_key = get_secret("BREEZE_API_KEY")
            breeze_api_secret = get_secret("BREEZE_API_SECRET")
            
            breeze = BreezeConnect(api_key=breeze_api_key)
            print("BreezeConnect client initialized.")
        except Exception as e:
            print(f"Error initializing BreezeConnect: {e}")
            breeze = None
    return breeze

@app.route("/breeze/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok", "message": "Breeze proxy is running"})

@app.route("/breeze/admin/api-session", methods=["POST"])
def set_api_session():
    global DAILY_SESSION_TOKEN
    data = request.get_json()
    api_session = data.get("api_session")
    admin_key = request.headers.get("X-Proxy-Admin-Key") # Use a strong admin key for this endpoint

    expected_admin_key = get_secret("BREEZE_PROXY_ADMIN_KEY")
    if not expected_admin_key or admin_key != expected_admin_key:
        return jsonify({"error": "Unauthorized"}), 401

    if not api_session:
        return jsonify({"error": "api_session is required"}), 400
    
    breeze_client = initialize_breeze()
    if not breeze_client:
        return jsonify({"error": "BreezeConnect client not initialized"}), 500

    try:
        breeze_client.generate_session(api_session=api_session)
        DAILY_SESSION_TOKEN = api_session # Store the session token
        return jsonify({"status": "success", "message": "Daily Breeze session set"}), 200
    except Exception as e:
        return jsonify({"error": f"Failed to generate session: {e}"}), 500

def ensure_breeze_session():
    breeze_client = initialize_breeze()
    if not breeze_client:
        return None, jsonify({"error": "BreezeConnect client not initialized"}), 500
    
    # Check if session is active, if not try to re-generate if we have the token
    if not breeze_client.session_id and DAILY_SESSION_TOKEN:
        try:
            breeze_client.generate_session(api_session=DAILY_SESSION_TOKEN)
            print("Breeze session regenerated using stored token.")
        except Exception as e:
            print(f"Error regenerating Breeze session: {e}")
            return None, jsonify({"error": f"Breeze session expired or invalid: {e}"}), 401
    elif not breeze_client.session_id and not DAILY_SESSION_TOKEN:
        return None, jsonify({"error": "Breeze session token not set. Please set it via /admin/api-session"}), 401
    
    return breeze_client, None, None

@app.route("/breeze/quotes", methods=["POST"])
def get_quotes():
    breeze_client, error_response, status_code = ensure_breeze_session()
    if error_response:
        return error_response, status_code

    data = request.get_json()
    stock_code = data.get("stock_code")
    exchange_code = data.get("exchange_code", "NSE")
    product_type = data.get("product_type", "cash")

    if not stock_code:
        return jsonify({"error": "stock_code is required"}), 400

    try:
        quotes_data = breeze_client.get_quotes(stock_code=stock_code, exchange_code=exchange_code, product_type=product_type)
        
        if quotes_data and quotes_data.get("Success") and isinstance(quotes_data["Success"], list):
            row = next((item for item in quotes_data["Success"] if item.get("exchange_code") == exchange_code or item.get("stock_code") == stock_code), None)
            if row:
                ltp = float(row.get("ltp", 0))
                prev_close = float(row.get("previous_close", 0))
                change_val = float(row.get("change", ltp - prev_close if prev_close else 0))
                percent_change = float(row.get("ltp_percent_change", row.get("chng_per", 0)))

                formatted_quote = {
                    "last_traded_price": ltp,
                    "change": change_val,
                    "percent_change": percent_change,
                    "open": float(row.get("open", 0)),
                    "high": float(row.get("high", 0)),
                    "low": float(row.get("low", 0)),
                    "previous_close": prev_close,
                    "volume": float(row.get("total_volume", row.get("volume", 0))),
                    "stock_code": stock_code,
                    "best_bid_price": float(row.get("best_bid_price", 0)),
                    "best_bid_quantity": float(row.get("best_bid_quantity", 0)),
                    "best_offer_price": float(row.get("best_offer_price", 0)),
                    "best_offer_quantity": float(row.get("best_offer_quantity", 0))
                }
                return jsonify({"Success": formatted_quote}), 200
            else:
                return jsonify({"error": f"No quote data found for {stock_code}"}), 404
        else:
            return jsonify({"error": "Failed to retrieve quotes or malformed response"}), 500
    except Exception as e:
        return jsonify({"error": f"Breeze API error: {e}"}), 500

@app.route("/breeze/depth", methods=["POST"])
def get_market_depth():
    breeze_client, error_response, status_code = ensure_breeze_session()
    if error_response:
        return error_response, status_code

    data = request.get_json()
    stock_code = data.get("stock_code")
    exchange_code = data.get("exchange_code", "NSE")
    product_type = data.get("product_type", "cash")

    if not stock_code:
        return jsonify({"error": "stock_code is required"}), 400

    try:
        depth_data = breeze_client.get_market_depth(stock_code=stock_code, exchange_code=exchange_code, product_type=product_type)
        return jsonify(depth_data), 200
    except Exception as e:
        return jsonify({"error": f"Breeze API error fetching depth: {e}"}), 500

@app.route("/breeze/historical", methods=["POST"])
def get_historical_data():
    breeze_client, error_response, status_code = ensure_breeze_session()
    if error_response:
        return error_response, status_code

    data = request.get_json()
    stock_code = data.get("stock_code")
    exchange_code = data.get("exchange_code", "NSE")
    product_type = data.get("product_type", "cash")
    from_date = data.get("from_date") # YYYY-MM-DD
    to_date = data.get("to_date")     # YYYY-MM-DD
    interval = data.get("interval", "1day")

    if not all([stock_code, from_date, to_date]):
        return jsonify({"error": "stock_code, from_date, and to_date are required"}), 400

    try:
        historical_data = breeze_client.get_historical_data(
            stock_code=stock_code,
            exchange_code=exchange_code,
            product_type=product_type,
            from_date=from_date,
            to_date=to_date,
            interval=interval
        )
        if historical_data and historical_data.get("Success") and isinstance(historical_data["Success"], list):
            formatted_bars = []
            for bar in historical_data["Success"]:
                formatted_bars.append({
                    "datetime": bar.get("datetime"),
                    "open": float(bar.get("open", 0)),
                    "high": float(bar.get("high", 0)),
                    "low": float(bar.get("low", 0)),
                    "close": float(bar.get("close", 0)),
                    "volume": float(bar.get("volume", 0))
                })
            return jsonify({"Success": formatted_bars}), 200
        else:
            return jsonify({"error": "Failed to retrieve historical data or malformed response"}), 500
    except Exception as e:
        return jsonify({"error": f"Breeze API error fetching historical data: {e}"}), 500

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
