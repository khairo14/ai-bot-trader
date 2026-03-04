# System Architecture

## Overview

AI Bot Trader is a hybrid rule-based + ML trading bot that supports crypto, stocks, and options trading across Binance, Alpaca, and Interactive Brokers (IBKR). It is designed to run locally via Docker and optionally deploy to a VPS for 24/7 autonomous operation.

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        FRONTEND (React)                         │
│   Dashboard │ Signals │ Backtest │ Forward Test │ Settings      │
└─────────────────────────┬───────────────────────────────────────┘
                          │ HTTP / WebSocket
┌─────────────────────────▼───────────────────────────────────────┐
│                      BACKEND (FastAPI)                          │
│                                                                 │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────────┐  │
│  │ Signal      │  │ Backtest     │  │ Forward Test /        │  │
│  │ Engine      │  │ Engine       │  │ Live Execution Engine │  │
│  └──────┬──────┘  └──────┬───────┘  └──────────┬────────────┘  │
│         │                │                      │               │
│  ┌──────▼──────────────────────────────────────▼────────────┐  │
│  │                    Strategy Layer                         │  │
│  │    Rule-Based Signals + ML Models + Regime Detection      │  │
│  └──────────────────────────┬────────────────────────────────┘  │
│                             │                                   │
│  ┌──────────────────────────▼────────────────────────────────┐  │
│  │                     Tool Library                          │  │
│  │        Basic │ Advanced │ Custom Indicators               │  │
│  └──────────────────────────┬────────────────────────────────┘  │
│                             │                                   │
│  ┌──────────────────────────▼────────────────────────────────┐  │
│  │                     Data Engine                           │  │
│  │   OHLCV Fetcher │ Live Stream │ Options Chain Fetcher     │  │
│  └──────────────────────────┬────────────────────────────────┘  │
│                             │                                   │
│  ┌──────────────────────────▼────────────────────────────────┐  │
│  │                   Broker Connectors                       │  │
│  │        Binance │ Alpaca │ IBKR                            │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌─────────────────────┐  ┌──────────────────────────────────┐  │
│  │   Risk Manager      │  │   Portfolio Manager              │  │
│  └─────────────────────┘  └──────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────────┐
│                   PostgreSQL Database                           │
│   Trades │ Signals │ OHLCV │ Model Snapshots │ Audit Logs      │
└─────────────────────────────────────────────────────────────────┘
```

---

## Module Breakdown

### 1. Data Engine
- Fetches historical OHLCV data for backtesting
- Streams real-time price data via WebSocket for live/paper trading
- Fetches options chains (expiry, strike, IV, Greeks) from IBKR
- Normalizes data into a unified format regardless of broker

### 2. Tool Library
- **Basic:** RSI, MACD, Bollinger Bands, EMA, SMA, Volume analysis
- **Advanced:** VWAP, Order Flow, Market Profile, Implied Volatility surface, Options Greeks
- **Custom:** User-defined indicators, composite signals, ML feature pipelines
- All tools output a standardized signal object consumed by the Strategy Layer

### 3. Strategy Layer
- Combines tool outputs into entry/exit signals
- Runs rule-based logic (e.g. RSI < 30 + MACD crossover = buy signal)
- Passes features to ML models for probability scoring
- Detects market regime (trending / ranging / high-volatility) and switches strategy accordingly

### 4. Signal Engine
- Aggregates strategy outputs into a final signal: `BUY | SELL | SHORT | HOLD`
- Attaches entry price, stop loss, take profit, confidence score
- Stores signals to DB for audit and learning

### 5. Backtest Engine
- Replays historical data through the strategy layer
- Calculates: P&L, win rate, max drawdown, Sharpe ratio, profit factor
- Supports walk-forward testing to prevent overfitting
- Outputs a full report with trade-by-trade breakdown

### 6. Forward Test / Live Execution Engine
- Paper trading: simulates order fills using real-time prices, no real money
- Live trading: places real orders through broker APIs
- Mode is switchable per strategy: `suggestion | semi-auto | full-auto`

### 7. Risk Manager
- Position sizing based on account balance and risk percentage per trade
- Hard stop loss enforcement
- Max daily drawdown circuit breaker (halts all trading if triggered)
- Max open positions limit

### 8. Portfolio Manager
- Tracks all open and closed positions
- Calculates real-time P&L, exposure per asset class
- Handles multi-asset correlation awareness

### 9. Broker Connectors
- Unified interface: each broker implements the same abstract methods
- `get_price()`, `place_order()`, `cancel_order()`, `get_positions()`, `get_balance()`
- See [brokers.md](brokers.md) for full details

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11, FastAPI, SQLAlchemy 2.0, asyncpg |
| Frontend | React 18, Vite, TypeScript, TailwindCSS, Recharts |
| Database | PostgreSQL 15 (asyncpg driver) |
| Task Queue | Celery + Redis (scheduled signal runs + ML retraining) |
| ML Models | scikit-learn, XGBoost, joblib |
| Broker: Crypto | `ccxt` async (Binance spot/futures) |
| Broker: Stocks | `alpaca-py` v0.8+ (Alpaca paper + live) |
| Broker: Options | `ib_insync` (IB Gateway via TWS API) |
| Migrations | Alembic |
| Containerization | Docker, Docker Compose |
| Deployment | Local (Python venv + npm) or full Docker stack, optional VPS |

---

## Data Flow — Signal to Order

```
Market Data (WebSocket)
        ↓
  Tool Library runs indicators
        ↓
  Strategy Layer evaluates rules + ML model
        ↓
  Regime Detector adjusts strategy weight
        ↓
  Signal Engine generates: BUY/SELL/SHORT/HOLD
        ↓
  Risk Manager validates position size + exposure
        ↓
    ┌───┴───────────────────────────┐
    │ Mode Check (per strategy)     │
    │                               │
    │ suggestion → notify only      │
    │ semi-auto  → await 1-click    │
    │ full-auto  → execute now      │
    └───────────────────────────────┘
        ↓
  Broker Connector places order
        ↓
  Trade logged to DB
        ↓
  Portfolio Manager updates positions
```

---

## Learning Loop (Continuous Improvement)

```
Live / Paper Trade
      ↓
Trade Results Logged to DB
      ↓
Weekly: ML model retrains on fresh data
      ↓
Backtest: new model vs current model
      ↓
If new model wins → deploy it
If not           → keep current
      ↓
Forward test confirms stability → stays live
```

---

## Deployment Modes

| Mode | Description |
|---|---|
| **Local (Docker)** | `docker-compose up` on your machine. Bot runs while machine is on. |
| **Local (venv)** | Python venv + `npm run dev`. Active development setup. |
| **VPS (24/7)** | Same `docker-compose.yml` copied to a Linux VPS. Runs autonomously. |
| **Installer** | Optional: package as a Windows installer for one-click local setup |

See [setup.md](setup.md) for full setup instructions.

---

## Development Status

| Component | Status | Notes |
|---|---|---|
| FastAPI backend | ✅ Running | Port 8000 |
| React frontend | ✅ Running | Port 5173 (Vite dev) / 3000 (Docker) |
| PostgreSQL schema | ✅ Migrated | 4 tables: trades, strategies, signals, positions |
| Redis | ✅ Running | Celery broker |
| Binance client | ✅ Connected | Live mode, full spot USDT portfolio value |
| Alpaca client | ✅ Connected | Paper mode, $100,000 balance |
| IBKR client | ✅ Connected | Paper mode via IB Gateway, $1,000,000 balance |
| Portfolio API | ✅ Live | `/api/portfolio/summary` — all 3 brokers parallel |
| Dashboard UI | ✅ Live | Broker cards, P&L, positions from API |
| Strategies UI | ✅ Live | List, toggle, mode change via API |
| Signal Engine | 🔧 Written | `HybridStrategy` (MACD+RSI) ready, Celery wiring next |
| Forward Engine | 🔧 Written | Paper + live execution logic ready, not yet triggered |
| Risk Manager | 🔧 Written | Full 5-level hierarchy ready, not yet wired into flow |
| Celery signal task | 🔧 In progress | Bug fix + scheduling needed |
| WebSocket | 🔧 Skeleton | Not yet broadcasting signals |
| Forward Test UI | 🔧 Stub | UI placeholder only |
| Backtest UI | 🔧 Stub | UI placeholder only |

### Next Steps (Priority Order)
1. Wire `run_signals` Celery task → `SignalEngine` → `ForwardEngine` → DB
2. Expose live Forward Test page (start/pause/stop, live P&L, positions table)
3. WebSocket for real-time signal broadcasts to Dashboard
4. Connect Backtest page to `BacktestEngine`
