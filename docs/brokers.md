# Broker Integrations

## Overview

All brokers implement a unified abstract interface so strategies and the execution engine never need to know which broker they're talking to. Swapping or adding a broker never requires changing strategy code.

```
AbstractBroker
  ├── BinanceClient     → Crypto (BTC, ETH, altcoins)
  ├── AlpacaClient      → Stocks (US equities)
  └── IBKRClient        → Stocks + Options (global markets)
```

---

## Unified Broker Interface

Every broker connector must implement these methods:

| Method | Description |
|---|---|
| `get_price(symbol)` | Current market price |
| `get_ohlcv(symbol, timeframe, limit)` | Historical candle data |
| `get_orderbook(symbol)` | Live bid/ask orderbook |
| `get_balance()` | Account balance |
| `get_positions()` | All open positions |
| `place_order(symbol, side, qty, order_type, price)` | Place a new order |
| `cancel_order(order_id)` | Cancel an open order |
| `get_order_status(order_id)` | Check order fill status |
| `stream_prices(symbols, callback)` | WebSocket live price stream — used by `PriceStreamManager` |
| `get_bid_ask(symbol)` | Live best bid and ask prices |
| `update_stop_loss(symbol, side, quantity, new_sl_price)` | Move stop loss on an open position (trailing stop sync) |
| `close_position(symbol, side, quantity)` | Close an open position at market price |

---

## 1. Binance (Crypto)

**Asset Classes:** Spot crypto, Futures (leveraged)
**API Type:** Official REST + WebSocket
**Library:** `ccxt` (async) + direct WebSocket

### What You Can Trade
- Crypto pairs: BTC/USDT, ETH/USDT, SOL/USDT, and thousands more
- Spot (buy/sell actual crypto)
- Futures (leveraged long/short positions)

### Setup
1. Create account at [binance.com](https://binance.com)
2. Go to **Account → API Management**
3. Create a new API key
4. Enable: **Enable Reading**, **Enable Spot & Margin Trading**, **Enable Futures** (if using futures)
5. Whitelist your IP if running from a fixed VPS IP (recommended for security)
6. Copy `API_KEY` and `API_SECRET` to your `.env` file

### Key Environment Variables
```env
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here
BINANCE_TESTNET=false  # true = Binance Testnet (testnet.binance.vision keys required)

# If the bot runs behind a VPN, route REST calls through the VPN's local proxy:
# HTTP_PROXY=http://127.0.0.1:61892   # Remove this line if not using a VPN
```

### Balance Reporting
`get_balance()` returns the **total spot portfolio value converted to USDT**, not just the raw USDT wallet balance. All non-zero spot assets (BNB, BTC, WLD, etc.) are fetched at current market price and summed. This means the dashboard value matches Binance's "Estimated Balance" in the Spot view.

### Testnet (Paper Trading)
Binance provides a free testnet at `testnet.binance.vision`. Set `BINANCE_TESTNET=true` and generate separate testnet API keys from the testnet site. The testnet provides ~10,000 USDT of fake funds.

### Rate Limits
- REST: 1200 requests/minute
- WebSocket: Unlimited price streams, max 1024 streams per connection
- Our bot respects these automatically via `ccxt`'s built-in rate limiter

### Supported Order Types
| Type | Description |
|---|---|
| `MARKET` | Instant fill at current price |
| `LIMIT` | Fill at specified price or better |
| `STOP_LOSS` | Market-on-trigger stop (guaranteed fill — used for bracket SL) |
| `STOP_LOSS_LIMIT` | Triggered stop with limit price (used for trailing stop updates only) |
| `TAKE_PROFIT_LIMIT` | Triggered take profit with limit price |
| `OCO` | One-Cancels-the-Other (stop + take profit together) |

### Bracket Order Behaviour (F-108)

When placing a bracket order (entry + SL + TP), the SL leg uses `STOP_LOSS` (market-on-trigger), **not** `STOP_LOSS_LIMIT`. This ensures the full position quantity is liquidated even when price gaps hard through the stop level.

**Problem solved:** `STOP_LOSS_LIMIT` only partially fills on fast gap moves. The unfilled base-asset quantity remains locked by the standing order, causing every subsequent market-sell attempt to fail with `insufficient balance`. The bot would retry indefinitely.

**Fix applied:**
- Bracket SL uses `STOP_LOSS` (market order triggered at `stopPrice`) — guaranteed full fill
- After SL fires, `cancel_open_orders()` uses the atomic `DELETE /api/v3/openOrders` endpoint, with a fetch-and-cancel fallback
- `get_positions()` counts `free + locked` balance so the engine sees the correct position size even while bracket orders hold the asset
- If a partial bracket fill is detected (F-108), the engine fetches the actual fill price from `fetch_closed_orders`, sells the remaining free balance as a market order, and computes a **weighted average exit price** across both fills for accurate PnL recording

### Testnet Notes
- Paper trading uses `testnet.binance.vision` (separate API keys required)
- WebSocket price stream uses `/ws/<stream>` path on testnet (different from production `/stream?streams=`)
- `PriceStreamManager` always connects to the **live** Binance WebSocket even for paper strategies, ensuring SL/TP is evaluated against real market prices

**Asset Classes:** US Stocks, ETFs, Crypto (limited)
**API Type:** Official REST + WebSocket
**Library:** `alpaca-py` (official v2 SDK)

### What You Can Trade
- US stocks and ETFs (NYSE, NASDAQ)
- Commission-free trading
- Fractional shares supported
- Extended hours trading available

### Setup
1. Create account at [alpaca.markets](https://alpaca.markets)
2. Paper trading is available immediately upon signup — no approval needed
3. Go to **Dashboard → Your API Keys → View**
4. Copy `API_KEY` and `API_SECRET`
5. Paper trading base URL: `https://paper-api.alpaca.markets`
6. Live trading base URL: `https://api.alpaca.markets`

### Key Environment Variables
```env
ALPACA_API_KEY=your_api_key_here
ALPACA_API_SECRET=your_api_secret_here
ALPACA_BASE_URL=https://paper-api.alpaca.markets   # Change to live URL when ready
```

### Market Data
- **Free tier:** 15-minute delayed data
- **Unlimited plan ($9/mo):** Real-time SIP data (full US market)
- For backtesting, free tier is sufficient (historical data has no delay)
- For live trading signals, real-time data is strongly recommended

### OHLCV Data Guard
`get_ohlcv()` includes defensive guards for Alpaca-specific edge cases:
- **Empty response** (symbol not covered, free-tier restrictions): raises `ValueError` with clear message instead of cryptic pandas crash
- **MultiIndex response** (symbol not in returned data): raises `ValueError` naming the missing symbol
- **Missing columns** (incomplete bar data): raises `ValueError` listing exactly which OHLCV columns are absent
All errors surface as human-readable messages in the Scanner errors panel and Forward Test logs.

### Rate Limits
- REST: 200 requests/minute
- WebSocket: Unlimited subscription to price updates

### Supported Order Types
| Type | Description |
|---|---|
| `market` | Instant fill at current price |
| `limit` | Fill at specified price or better |
| `stop` | Stop-loss trigger, converts to market order |
| `stop_limit` | Stop trigger, converts to limit order |
| `trailing_stop` | Dynamic stop that follows price |

### Trading Hours
- Regular: 9:30 AM – 4:00 PM ET
- Extended: 4:00 AM – 8:00 PM ET (available in settings)
- Bot automatically checks market hours before placing orders

---

## 3. Interactive Brokers / IBKR (Stocks + Options)

**Asset Classes:** Stocks, Options, Futures, Forex, Bonds (global)
**API Type:** TWS API via `ib_insync` Python library
**Requirement:** IB Gateway or TWS desktop app must be running

### What You Can Trade
- US and international stocks
- US and international options (calls and puts)
- Options chains with full Greeks (Delta, Gamma, Theta, Vega)
- Implied Volatility surface
- Multi-leg options strategies (spreads, straddles, iron condors)

### Why IBKR Requires a Desktop App
Unlike Binance and Alpaca, IBKR's API does not have a cloud endpoint. You must run their **IB Gateway** (lightweight, headless) or **Trader Workstation (TWS)** (full GUI) on the same machine or VPS as the bot. The Python library connects to this local app, which then communicates with IBKR's servers.

```
Our Bot (ib_insync) → IB Gateway (localhost:7497) → IBKR Servers → Markets
```

### Setup
1. Create account at [interactivebrokers.com](https://www.interactivebrokers.com)
2. Complete the application (approval takes 1–3 business days)
3. Download **IB Gateway** (recommended over full TWS for server use)
   - Download at: [ibkr.com/en/software/ib-gateway](https://www.interactivebrokers.com/en/software/ib-gateway)
4. Launch IB Gateway, log in with IBKR credentials
5. Go to **Configure → API → Settings**:
   - ✅ Enable `Enable ActiveX and Socket Clients`
   - Set `Socket port` to `7497` (paper) or `7496` (live)
   - ✅ Enable `Allow connections from localhost only` for security
   - ❌ **Read-Only API must be OFF** — if ticked, the client will connect successfully but order placement will fail silently
   - API type: **IB API** (not FIX CTCI)
6. Paper trading account is automatically provided by IBKR (separate login credentials)

### Key Environment Variables
```env
# Local development:
IBKR_HOST=127.0.0.1
# Full Docker stack:
# IBKR_HOST=host.docker.internal

IBKR_PORT=7497          # 7497 = paper trading, 7496 = live trading
IBKR_CLIENT_ID=1        # Unique ID per connection; balance fetches use random 50-99 to avoid collisions
IBKR_CLIENT_ID_CELERY=2 # Must differ from IBKR_CLIENT_ID — used by Celery workers (validated at startup)
IBKR_PAPER=true         # Set to false for live trading
```

> **IBKR Client ID Collision:** IB Gateway rejects a second connection that shares a client ID with an existing session (Error 326). The bot validates at startup that `IBKR_CLIENT_ID ≠ IBKR_CLIENT_ID_CELERY` and raises a `ValueError` if they match, preventing silent Celery order failures.

### Reconnection Resilience

The IBKR background reconnect loop uses exponential backoff and handles Error 326 (clientId conflict) separately from genuine network failures:

| Scenario | Behaviour |
|---|---|
| Transient network drop | Reconnects immediately; backoff resets on success |
| Repeated failures | Backoff doubles each cycle: 15s → 30s → 60s → 120s → 300s (cap) |
| Error 326 (clientId in use) | 120s cooldown — waits for stale TWS socket to release, then retries |
| 20 consecutive genuine failures | Loop exits with `CRITICAL` log; container restart recovers it |

- Backoff resets to 15s on every successful reconnect so short outages recover quickly
- Error 326 does **not** count toward the consecutive-failure cap — it's an expected transient state after a crash/restart until the old socket times out (~60–90s in TWS)
- Celery workers get `clientId = IBKR_CLIENT_ID_CELERY` (default: 2); FastAPI gets `IBKR_CLIENT_ID` (default: 1) — they never interfere with each other's TWS session

### Minimum Balance Requirements
- **Paper account:** No minimum (free, unlimited)
- **Live account:** $10,000 USD minimum for margin/options trading
- Commission: ~$0.65/options contract, ~$0.005/share for stocks

### Options-Specific Capabilities
| Feature | Available |
|---|---|
| Options chain fetching | Yes |
| Real-time Greeks (Delta, Gamma, Theta, Vega) | Yes |
| Implied Volatility | Yes |
| Historical IV | Yes |
| Multi-leg orders (spreads, etc.) | Yes |
| Early assignment detection | Yes |
| Expiration handling | Yes (automated) |

### Headless Mode (VPS / Server)
IB Gateway supports headless (no GUI) operation via `ibgateway` command. This is required for 24/7 VPS deployment. See [setup.md](setup.md) for full VPS configuration with IB Gateway headless.

---

## Broker Selection per Asset Class

| Asset Type | Recommended Broker | Reason |
|---|---|---|
| BTC, ETH, altcoins | Binance | Best liquidity, lowest fees, full API |
| US stocks | Alpaca | Commission-free, excellent API, easy setup |
| US options | IBKR | Best options API, full Greeks, multi-leg support |
| International stocks | IBKR | Global market access |

### Broker-Watchlist Filtering (Market Scanner)

The Market Scanner enforces a broker-to-watchlist mapping so crypto watchlists are never sent to stock brokers and vice-versa:

| Broker | Available Watchlists |
|---|---|
| `binance` | `crypto_major`, `crypto_mid` |
| `alpaca` | `us_stocks`, `us_stocks_mid` |
| `ibkr` | `us_stocks`, `us_stocks_mid` |

This is enforced in both the backend (`GET /api/scanner/watchlists?broker=binance`) and the frontend (`BROKER_WATCHLISTS` constant in `MarketScanner.tsx`).

---

## Real-Time Price Streaming (`PriceStreamManager`)

`core/engine/price_stream.py` implements a singleton `PriceStreamManager` that maintains one live WebSocket stream per active broker and feeds the SL/TP monitor with sub-second prices.

### How It Works

```
FastAPI lifespan startup
  → price_stream_manager.start(AsyncSessionLocal)
        ↓
  Every 30s: query DB for OPEN trades → group symbols by broker
        ↓
  For each broker with open trades:
    If no stream task running → launch _stream_worker(broker, symbols)
    If symbols changed → cancel old task, resubscribe
        ↓
  _stream_worker():
    calls broker.stream_prices(symbols, callback=_update_price)
    on error: sleep 5s, restart (auto-reconnect)
        ↓
  _update_price(symbol, price):
    _prices[symbol] = price   ← in-process dict, O(1) read
```

### Usage in `monitor_sl_tp`

```python
streamed = price_stream_manager.get_price(trade.symbol)
if streamed is not None:
    bid = ask = streamed          # use WebSocket mid-price
else:
    bid, ask = broker.get_bid_ask(trade.symbol)   # REST fallback
```

### Important Notes

- `PriceStreamManager` is process-local. Each uvicorn worker maintains its own set of streams. Cross-process state (whether a trade was actually closed) is managed via the PostgreSQL `trades.status` column.
- If a broker does not have any open trades, its stream is cleanly cancelled to avoid unnecessary connections.
- On shutdown, the stream background task is cancelled in the lifespan shutdown loop.

---

## Adding a New Broker

1. Create a new file in `backend/brokers/`
2. Inherit from `AbstractBroker` in `backend/brokers/base.py`
3. Implement all required interface methods
4. Register the broker in `backend/brokers/__init__.py`
5. Add credentials to `.env.example`

The strategy engine and execution layer will work with the new broker automatically.
