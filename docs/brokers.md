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
| `stream_prices(symbols, callback)` | WebSocket live price stream |

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
| `STOP_LOSS_LIMIT` | Triggered stop with limit price |
| `TAKE_PROFIT_LIMIT` | Triggered take profit with limit price |
| `OCO` | One-Cancels-the-Other (stop + take profit together) |

---

## 2. Alpaca (Stocks)

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
IBKR_PAPER=true         # Set to false for live trading
```

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

---

## Adding a New Broker

1. Create a new file in `backend/brokers/`
2. Inherit from `AbstractBroker` in `backend/brokers/base.py`
3. Implement all required interface methods
4. Register the broker in `backend/brokers/__init__.py`
5. Add credentials to `.env.example`

The strategy engine and execution layer will work with the new broker automatically.
