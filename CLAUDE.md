# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MAIA Breeze Proxy — a Flask/Socket.IO intermediary that keeps ICICI Breeze API credentials server-side, streams real-time NSE/BSE market ticks to browser clients via WebSocket, and runs AI analysis pipelines (Gemini) for stock deep-dives and Reg30 regulatory filing extraction. Deployed on Google Cloud Run.

## Development Commands

```bash
cd breeze-proxy
pip install -r requirements.txt
python breeze_proxy_app.py   # dev server on port 8080
```

### Environment Setup
Copy `.env.example` to `.env` and fill in credentials. The app resolves secrets in this order:
1. OS environment variables
2. `breeze-proxy/env.yaml` (YAML key-value pairs)
3. Falls back gracefully (no Google Secret Manager dependency)

Required variables: `BREEZE_API_KEY`, `BREEZE_API_SECRET`, `BREEZE_PROXY_ADMIN_KEY`, `GEMINI_API_KEY`, `SUPABASE_KEY`, `SUPABASE_URL`, `STOCKINSIGHTS_API_URL`, `STOCKINSIGHTS_API_KEY`

### Deployment (Google Cloud Run)
```bash
# Deploy from breeze-proxy/ subdirectory using Dockerfile
gcloud run deploy maia-breeze-proxy-service \
  --source breeze-proxy/ \
  --region us-central1 \
  --set-env-vars KEY=VALUE,...
```
Production uses eventlet gunicorn: `gunicorn --bind 0.0.0.0:$PORT --workers 1 --worker-class eventlet breeze_proxy_app:app`

## Architecture

Single file: `breeze-proxy/breeze_proxy_app.py` (~1640 lines). All logic lives here.

### Global State (module-level)
- `DAILY_SESSION_TOKEN` — stored Breeze session token (set via admin endpoint). **Resets to None on every Cloud Run restart/redeploy** — user must re-enter via BreezeTokenModal after each deploy.
- `breeze_client` — singleton BreezeConnect instance
- `_tick_dispatch_queue` — `SimpleQueue` bridging Breeze WebSocket thread → eventlet greenlet
- `_tick_registry` — `dict[symbol → set[sid]]` tracking which Socket.IO clients want each symbol
- `_registry_symbol_map` — Breeze raw code / stock_name → canonical frontend symbol (e.g. `"NIFTY 50"` → `"NIFTY"`)
- `_token_to_std_map` — numeric NSE token → canonical frontend symbol (e.g. `"2885"` → `"RELIANCE"`)
- `_subscribed_breeze_codes` — set of active Breeze feed subscriptions (cleared on each new `track_watchlist` call)
- `_symbol_prev_close` — REST-fetched previous close prices (fixes WebSocket `close` field quirk)
- `_session_validity_cache` — cached result of `get_customer_details()` (TTL: 180s)

### Tick Dispatch Architecture
BreezeConnect callbacks run in a **C-extension thread** where `socketio.emit()` silently drops events under gunicorn+eventlet. The fix:

```
Breeze WebSocket thread
  └→ _global_on_ticks() — puts raw tick dict into _tick_dispatch_queue (SimpleQueue, thread-safe)
                          NO logging here — per-tick I/O blocks the thread

eventlet greenlet (started on first Socket.IO connect via handle_connect)
  └→ _run_tick_dispatcher() — drains queue with socketio.sleep(0) cooperative yield
       └→ _dispatch_tick() — resolves symbol, normalizes, emits watchlist_update
```

**Performance rules — never violate these for algo-trading grade latency:**
- `_global_on_ticks` must do nothing except `_tick_dispatch_queue.put()` — no logging, no processing
- `_run_tick_dispatcher` uses `socketio.sleep(0)` when ticks were drained (zero artificial delay), `socketio.sleep(0.001)` when idle
- No `logger.info()` inside any tick hot path (`_global_on_ticks`, `_dispatch_tick`, `_run_tick_dispatcher`)

### BreezeConnect Tick Data Formats (confirmed from source v1.0.65)

**CRITICAL**: NSE equity exchange-quote ticks (`get_exchange_quotes=True`) have NO `stock_code` field. Field names differ completely by tick type:

| Field | NSE Equity (exchange 4) | Index (exchange 3) | REST `get_quotes()` |
|---|---|---|---|
| LTP | `last` | `last_trade_price` | `ltp` |
| Change | `change` | `absolute_change` | `change` |
| Change % | *(compute from prev close)* | `percentage_change` | `ltp_percent_change` |
| Prev close | `close` *(quirky — prev-second value)* | `previous_close` *(correct)* | `close` *(correct)* |
| Volume | `ttq` | `total_traded_volume` | `total_quantity_traded` |
| Best bid price | `bPrice` | `bid_price` | `best_bid_price` |
| Best bid qty | `bQty` | `bid_quantity` | `best_bid_quantity` |
| Best ask price | `sPrice` | `offer_price` | `best_offer_price` |
| Best ask qty | `sQty` | `offer_quantity` | `best_offer_quantity` |
| Symbol identifier | `symbol="4.1!<token>"` | `stock_code="3.1!NIFTY 50"` | — |
| Company name | `stock_name` (after enrichment) | — | — |

`normalize_tick_for_frontend()` maps all these into a single consistent shape for the frontend.

### Tick Symbol Resolution (4-path chain in `_dispatch_tick`)

NSE equity ticks have no `stock_code`. Resolution:
1. Extract token from `symbol="4.1!2885"` → `"2885"`
2. **`_token_to_std_map.get("2885")`** → `"RELIANCE"` (pre-built at subscription time via `get_stock_token_value()`)
3. **`_registry_symbol_map.get(canonical_symbol(raw))`** → for index ticks where raw="NIFTY 50"
4. **Live reverse lookup** via `breeze_client.token_script_dict_list[1].get("2885")` → `["RELIND", "Reliance Industries"]` → map breeze_code back via registry. Caches result into `_token_to_std_map` for future ticks.
5. **`stock_name` fallback** → `canonical_symbol("Nifty 50")` = `"NIFTY"` → registry lookup

**Token map population (at subscription time, in `track_watchlist`):**
Use `client.get_stock_token_value(exchange_code, stock_code, ...)` — the SAME method `subscribe_feeds` uses internally. Returns `{"exchange_quotes_token": "4.1!2885", ...}`. Extract the part after `!` as the raw token. This is more reliable than `stock_script_dict_list[1][breeze_code]` which requires exact key format.

### Symbol Mapping (NSE symbol → Breeze code)
Resolution order in `get_breeze_symbol(std)`:
1. `BREEZE_SYMBOL_OVERRIDES` hardcoded dict (NIFTY, AHLUCONT→AHLCON, MEDICO→MEDREM, etc.)
2. Supabase `nse_master_list` table (HTTP lookup, module-level cached to avoid 406 errors from multiple row matches)

`canonical_symbol(s)` normalizes: uppercases, strips spaces/hyphens. Special case: `"NIFTY 50"` and `"NIFTY50"` → `"NIFTY"`. Always use before dict lookups.

`get_breeze_symbol(std)` returns the raw Breeze code — **do NOT call `canonical_symbol()` on its result** or `"NIFTY 50"` becomes `"NIFTY"` (wrong for `subscribe_feeds`).

### NIFTY `close` Field Fix
WebSocket ticks give the previous-second value in `close`, not yesterday's session close. On subscribe, the proxy fetches a REST snapshot and caches the correct `previous_close` in `_symbol_prev_close`. `normalize_tick_for_frontend()` substitutes this cached value.

### Cloud Run Session Token Management
`DAILY_SESSION_TOKEN` is in-memory only. After every Cloud Run deploy or restart:
1. User opens app → clicks key icon (BreezeTokenModal) → enters today's ICICI Direct session token → Save & Activate
2. Without this, `ensure_breeze_session()` returns 401, `track_watchlist` exits immediately with `watchlist_error`, nothing subscribes
3. After saving the token, the page needs a reload (or wait 30s for auto-retry) for the socket to re-subscribe

### AI Clients (Gemini)
`initialize_ai_clients()` (lazy, called per-request):
- Prefers Vertex AI on Cloud Run (`GOOGLE_CLOUD_PROJECT` env set → uses service account)
- Falls back to `GEMINI_API_KEY` for local dev

Model selection: `get_gemini_model_candidates()` reads `GEMINI_MODELS` env var (comma-separated) or defaults to `gemini-2.5-pro, gemini-2.5-flash`. `generate_with_model_fallback()` iterates until one succeeds.

## API Routes

All routes require `BREEZE_PROXY_ADMIN_KEY` header (`X-Proxy-Key`) **except** public endpoints.

### Admin / Session
- `POST /api/breeze/admin/api-session` — set `DAILY_SESSION_TOKEN`; also resets `_session_validity_cache`
- `GET /api/breeze/health` — returns `{ status, session_active, session_valid }` with cached live validation via `get_customer_details()` (TTL 180s)

### Market Data (REST)
- `GET /api/breeze/quotes` — live quote for one symbol
- `GET /api/breeze/historical` — OHLCV candles (1min/1day etc.)
- `GET /api/market/nifty` — Nifty50 snapshot

### Reg30 / NSE Filings
- `POST /api/nse/announcements` — fetches NSE corporate announcements, filters for order/contract keywords; date range capped at 90 days
- `POST /api/attachment/parse` — downloads and extracts text from NSE XBRL/iXBRL attachment URLs
- `POST /api/gemini/reg30-analyze` — Gemini forensic extraction on filing text; regex fallback for NSE Symbol / company name from document header
- `POST /api/gemini/reg30-narrative` — generates 2-4 paragraph tactical event analysis

### StockInsights (NSE Announcement Alternative)
- `POST /api/stockinsights/announcements` — fetches announcements from StockInsights API (alternative to direct NSE API)
- `GET /api/stockinsights/health` — StockInsights API connectivity check

### AI Analysis
- `POST /api/gemini/stock-deep-dive` — full technical + fundamental analysis; computes EMA20/50, RSI14, ATR14, 20d high/low from Breeze candles before calling Gemini
- `POST /api/gemini/summarize` — market outlook summary

### Socket.IO Events
- `subscribe_to_watchlist` (client→server): `{ stocks: string[], proxy_key: string }` — subscribes to real-time ticks; bootstraps UI with REST snapshot immediately
- `tick_update` (server→client): normalized tick per symbol
- `watchlist_error` (server→client): error string
- `disconnect`: cleans up `_tick_registry`, unsubscribes orphaned Breeze feeds

## Key Patterns

**`normalize_result()`**: Enforces sentiment/action consistency — if sentiment and action diverge (e.g. POSITIVE + SELL), overrides to MIXED.

**`extract_json(text)`**: Strips markdown code fences and extracts JSON from Gemini responses (which often wrap output in ```json blocks).

**REST snapshot bootstrap**: After `ws_connect()`, a 300ms sleep then REST quotes are fetched for each symbol and emitted as synthetic ticks. This prevents the UI showing "Awaiting..." on illiquid stocks.

**REST polling fallback**: `track_watchlist` polls REST `get_quotes()` every 10s for non-NIFTY stocks as a fallback when WebSocket ticks can't be resolved. This is NOT the primary feed — WebSocket is. Do not reduce the poll to less than 5s (REST rate limit: 100 calls/min, 5000/day across all requests).

**BreezeConnect WebSocket internals**:
- Single Socket.IO connection to `livestream.icicidirect.com`; all subscriptions share it
- Max 2000 scripts subscribed across all streams
- `subscribe_feeds()` internally calls `sio_rate_refresh_handler.watch(token)` which emits `join` event
- On reconnect, `rewatch()` auto-resubmits all tokens from `tokenlist` set
- `ws_connect()` forces `transports="websocket"` — no polling fallback, 3s timeout
- `token_script_dict_list[exchange_idx]` maps `token → [breeze_code, company_name]` (reverse of `stock_script_dict_list`)
- Both dicts are populated during `generate_session()` by downloading `SecurityMaster.zip`

**CORS**: All routes use `@cross_origin()`. OPTIONS preflights are handled explicitly at the top of each handler (`if request.method == 'OPTIONS': return jsonify(success=True)`). A missing route returns 404 which **fails CORS preflight** — always ensure routes exist before the frontend calls them.
