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
- Streams real-time price data via WebSocket for live/paper trading — managed by `PriceStreamManager` (`core/engine/price_stream.py`)
- Fetches options chains (expiry, strike, IV, Greeks) from IBKR
- Normalizes data into a unified format regardless of broker

**`PriceStreamManager`** maintains one persistent WebSocket stream per active broker. It refreshes subscriptions every 30s from the DB and auto-reconnects on failure. The SL/TP monitor reads from this cache first (`O(1)` dict lookup) before falling back to REST API calls, reducing stop-loss reaction latency from ~60s to sub-second.

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
- Hard stop loss enforcement (static SL checked independently of trailing stop)
- Max daily drawdown circuit breaker (halts all trading if triggered)
- Max open positions limit
- State persisted to `runtime/risk_state.json` using async-safe I/O (offloaded to thread pool on the event loop; direct call in Celery context)

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
| ML Models | scikit-learn, XGBoost, joblib (trained on yfinance OHLCV) |
| ML Inference | `MLScorer` singleton — lazy-loaded, thread-safe, per-symbol |
| Broker: Crypto | `ccxt` async (Binance spot/futures) |
| Broker: Stocks | `alpaca-py` v0.8+ (Alpaca paper + live) |
| Broker: Options | `ib_insync` (IB Gateway via TWS API) |
| Migrations | Alembic (auto-runs on startup via `lifespan()`) |
| Notifications | In-app DB bell + Gmail SMTP (App Password, async fire-and-forget) |
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
| FastAPI backend | ✅ Running | Port 8000, auto-migrates (Alembic) on startup |
| React frontend | ✅ Running | Port 5173 (Vite dev) / 3000 (Docker nginx) |
| PostgreSQL schema | ✅ Migrated | trades, strategies, signals, positions, trade_outcomes, users |
| Redis | ✅ Running | Celery broker + beat scheduler |
| Binance client | ✅ Connected | Live mode, full spot USDT portfolio value |
| Alpaca client | ✅ Connected | Paper mode, $100,000 balance; DataFrame guard on empty responses |
| IBKR client | ✅ Connected | Paper mode via IB Gateway, $1,000,000 balance |
| Portfolio API | ✅ Live | `/api/portfolio/summary` — all 3 brokers parallel |
| Dashboard UI | ✅ Live | Broker cards, P&L, positions, ML feedback, portfolio weights |
| Strategies UI | ✅ Live | List, create, edit, toggle, mode change via API + code editor |
| Signal Engine | ✅ Complete | HybridStrategy, momentum_breakout, mean_reversion_bb — fully wired |
| Forward Engine | ✅ Complete | Paper + live execution, wall-clock-aligned scheduler, market-hours gate |
| Risk Manager | ✅ Complete | 5-level hierarchy wired into both Celery and Forward Test paths |
| Celery signal task | ✅ Complete | `tasks.signal_runner.run_signals`, beat-scheduled, wall-clock aligned |
| WebSocket | ✅ Complete | `signal` + `trade` events pushed live to Dashboard |
| Forward Test UI | ✅ Complete | Live paper trading, approve/reject panel, Run Now, per-strategy dedup |
| Backtest UI | ✅ Complete | Walk-forward, metrics, trade table, equity curve, CSV export |
| Market Scanner | ✅ Complete | Parallel multi-symbol scan, broker-aware watchlists, confidence ranking |
| Analytics page | ✅ Complete | Equity curve, monthly returns, win rate by strategy/symbol/hour, Sharpe |
| Multi-Timeframe page | ✅ Complete | Confluence analysis, batch mode, SignalCard mini-check |
| Candlestick charts | ✅ Complete | OHLCV + signal markers, EMA/BB/RSI/MACD overlays, all 3 brokers |
| Strategy code editor | ✅ Complete | Monaco editor, hot-reload, upload, built-in protection |
| ML model training | ✅ Complete | XGBoost, AUC-gated ≥0.55, auto-retrain Sundays 02:00 UTC |
| ML inference | ✅ Complete | MLScorer singleton, 60/40 blend, veto at P<0.35 |
| ML feedback loop | ✅ Complete | TradeOutcome → 24-candle resolve → 3× weighted retrain |
| Regime detector | ✅ Complete | ADX/ATR/BB/EMA heuristic, all strategies regime-aware |
| Portfolio optimizer | ✅ Complete | Sharpe-weighted allocation, correlation penalty, weekly rebalance |
| Trailing stops | ✅ Complete | DB field, outcome resolver updated, Strategies UI input |
| Auth / login | ✅ Complete | JWT, bcrypt, all /api/* protected, login page |
| Notification system | ✅ Complete | In-app bell + DB log + Gmail SMTP alerts |
| Notification archival | ✅ Complete | `tasks/notification_cleanup.py` — 30-day TTL for read notifications, weekly Sunday |
| Signal deduplication | ✅ Complete | Dedup window per timeframe in Forward Test |
| Per-strategy confluence | ✅ Complete | Options strategies bypass confluence (0.0); mean_reversion_bb also bypasses; others default 0.5 |
| Real-time price streaming | ✅ Complete | `PriceStreamManager` — per-broker WebSocket tasks; feeds monitor_sl_tp sub-second prices |
| SL/TP concurrency guard | ✅ Complete | `_MONITOR_SL_TP_LOCK` — prevents duplicate in-process monitor runs |
| DB indexes | ✅ Complete | `ix_trades_broker`, `ix_trades_strategy_name`, `ix_trades_status_is_paper` (Alembic p6q7r8s9t0u1) |

### Pending (Phase 2 remaining)
- **OPS-01** VPS deployment guide (`docker-compose.prod.yml`, CI/CD, automated DB backup)

---

## Audit History

| Audit | Date | Findings | Status |
|---|---|---|---|
| [Audit 1](audit.md) | Mar 7, 2026 | 60 findings across all modules | ✅ All fixed |
| [Audit 2](audit2.md) | Jun 2025 | 8 bugs, 12 gaps, 9 improvements | ✅ All fixed |
| [Audit 3](audit3.md) | Mar 10, 2026 | 5 bugs, 5 gaps, 5 improvements | ✅ All fixed |
