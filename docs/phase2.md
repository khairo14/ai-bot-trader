# Phase 2 Roadmap

Phase 1 is complete. All core infrastructure, execution, ML inference, and notifications are live.
Phase 2 focuses on making the system **smarter over time** and **production-hardened** for 24/7 autonomous operation.

---

## ML-01 · Automatic ML Feedback Loop (Closed-Loop Learning) ✅

**What it is:**
The ML model closes the loop: the bot monitors the real outcome of every signal it generated, then uses those outcomes to continuously improve the model.

**The loop:**
```
Signal generated (BUY BTC/USDT, confidence 0.78)
        ↓
Trade opened (entry $65,200)
        ↓
24 candles pass → outcome measured ($67,800 → +4.0% → WIN)
        ↓
(features → WIN) pair added to retraining dataset
        ↓
Nightly retrain: model updated with new outcome data
        ↓
Better predictions tomorrow
```

**What was built:**
- `TradeOutcome` DB table (`backend/db/models.py`) — created automatically when any non-HOLD signal fires; stores entry, SL/TP, symbol, timeframe, resolved flag
- `tasks/outcome_resolver.py` — async resolver that walks OHLCV forward from each signal's entry candle, detects SL/TP hits or measures 24-candle forward return; Celery beat task scheduled nightly at 01:30 UTC
- `models/trainer.py` — `_fetch_live_labels()` blends resolved live outcome rows (3× weighted) with historical yfinance heuristic labels; `MIN_AUC = 0.55` holdout gate before saving new model
- `tasks/ml_retrain.py` — Celery task scheduled weekly (Sunday 02:00 UTC); calls `ModelTrainer.retrain_all()` then hot-reloads `MLScorer` cache
- `api/routes/ml.py` — `GET /api/ml/status` (model registry, outcome counts, win rate, avg P&L), `POST /api/ml/resolve` (manual trigger), `POST /api/ml/retrain` (manual trigger)
- Dashboard shows feedback loop status, win rate, and avg signal P&L once first batch resolves

**Priority:** High — this is the most impactful Phase 2 item.

---

## ML-02 · Market Regime Detector (Automated)

**What it is:**
The strategy layer hard-codes regime logic (trending / ranging). This feature builds an automated ML regime classifier that detects the current market condition and dynamically weights strategy outputs.

**Regimes:**
| Regime | Characteristics | Best Strategies |
|---|---|---|
| Trending Up | Higher highs, higher lows, low VIX | Hybrid, Momentum Breakout |
| Trending Down | Lower highs, lower lows | Short-bias Hybrid |
| Ranging | Tight ATR, price oscillating | Mean Reversion BB |
| High Volatility | VIX spike, wide ATR | Reduce position size, widen stops |
| Low Volatility | Tight BB, low ATR | Options selling (iron condor) |

**What needs building:**
- `RegimeClassifier` (HMM or XGBoost on ATR, ADX, VIX, BB width, trend slope)
- Per-regime strategy weight multipliers applied in `SignalEngine`
- Regime shown on Dashboard as a badge

---

## ML-03 · Multi-Symbol Portfolio Optimization

**What it is:**
Instead of trading each symbol independently, this feature considers cross-asset correlations and allocates capital to maximize risk-adjusted return.

**What it does:**
- Tracks correlation matrix between all active symbols (updated daily)
- Limits simultaneous positions in highly correlated assets (e.g. BTC + ETH)
- Uses Modern Portfolio Theory (or a simpler Kelly sizing variant) to size positions
- Reduces max exposure when average correlation across portfolio is high

---

## EX-01 · Options Strategy Execution (IBKR)

**What it is:**
IBKR connection is live. This feature activates options-specific strategies.

**Strategies to implement:**
- **Covered Call:** Own stock, sell OTM call for income
- **Cash-Secured Put:** Sell OTM put to buy stock cheaper
- **Iron Condor:** Sell OTM call + put, buy wings — profits from low volatility
- **Bull Call Spread:** Defined-risk directional trade

**What needs building:**
- Options chain fetcher from IBKR (`reqSecDefOptParams` + `reqOptionChain`)
- IV Rank calculator (current IV vs. 52-week high/low)
- Greeks display on Dashboard (Delta, Theta, Vega per position)
- Options signal type additions to `SignalType` enum

---

## EX-02 · Trailing Stop & OCO Orders

**What it is:**
The execution engine currently places static stop loss and take profit. This adds dynamic trailing stops that follow price up (locking in profit as the trade moves favorably).

**What needs building:**
- `TrailingStopManager` — monitors open positions, updates stop loss as price moves
- IBKR + Alpaca trailing stop order type integration
- Dashboard "modify stop" button per open position

---

## UI-01 · Performance Analytics Dashboard

**What it is:**
A dedicated analytics page showing historical performance across all strategies and symbols — beyond the basic P&L on the Dashboard.

**Charts to add:**
- Equity curve (cumulative P&L over time)
- Monthly return heatmap (calendar view)
- Win rate by strategy, by symbol, by time-of-day
- Average MAE/MFE per trade (how far against you before recovering)
- Rolling Sharpe ratio (30-day window)
- ML model accuracy over time

---

## UI-02 · Multi-Timeframe Signal View

**What it is:**
Currently signals are generated on one timeframe per strategy. This adds multi-timeframe confluence — a BUY on 1h is stronger if 4h also shows BUY.

**What needs building:**
- Signal aggregation across timeframes per symbol
- Confluence score displayed on Dashboard signal cards
- Optional: only execute when 2+ timeframes align

---

## OPS-01 · VPS / Cloud Deployment Guide

**What it is:**
A step-by-step guide and scripts for deploying the full stack to a VPS (e.g. DigitalOcean, Vultr, AWS EC2) for 24/7 autonomous operation without leaving your machine on.

**What needs building:**
- `docker-compose.prod.yml` with nginx reverse proxy + SSL (Certbot)
- GitHub Actions CI workflow (lint → test → deploy on push to `production`)
- Automated DB backup script (pg_dump → S3 or local)
- Uptime monitoring setup (UptimeRobot or self-hosted)
- `DEPLOYMENT.md` guide

---

## OPS-02 · Authentication & Multi-User Support ✅

**What it is:**
JWT-based authentication protecting all `/api/*` routes with a login page on the frontend, safe for hosting on a public VPS.

**What was built:**
- `User` DB model + alembic migration (`users` table: id, username, hashed_password, is_active, is_admin, created_at)
- `backend/core/auth.py` — bcrypt password hashing + JWT helpers + `get_current_user` FastAPI dependency
- `POST /auth/register` — first registered user auto-becomes admin; username ≥ 3 chars, password ≥ 8 chars
- `POST /auth/login` — returns `{access_token, token_type, username, is_admin}`; 7-day expiry
- `GET /auth/me` — returns current authenticated user info
- All `/api/*` routers protected via `dependencies=[Depends(get_current_user)]`; `/auth/*`, `/health`, `/ws` remain public
- `frontend/src/lib/auth.ts` — token storage helpers + axios interceptor (attaches Bearer header; redirects to `/login` on 401)
- `frontend/src/pages/Login.tsx` — login form with username/password, error display, show/hide password toggle
- `frontend/src/App.tsx` refactored into `ProtectedLayout` + auth-guarded `App` root with logout button in sidebar

---

## UI-03 · Strategy Code Editor (Upload / Live Edit)

**What it is:**
A built-in code editor in the dashboard where you can write, upload, and hot-reload strategy files without touching the filesystem manually or restarting the server.

**Viability:** ✅ Fully viable for a self-hosted bot. The app runs on your own machine/VPS so there's no multi-tenant security risk with executing uploaded Python code.

**What it does:**
- **Upload tab:** Drop a `.py` file → it's validated, saved to `backend/core/strategies/`, and registered into `STRATEGY_REGISTRY` — live, no restart
- **Editor tab:** Monaco Editor (same as VS Code) in the browser showing the current strategy source; edit inline and hit Save → hot-reloads
- **Registry tab:** Shows all currently loaded strategy types, their `name`, `description`, `asset_class`, which strategies in the DB use each one
- **Validation:** On save/upload, the backend imports the class in a sandboxed try/except; rejects if it doesn't subclass `BaseStrategy` or `generate_signal()` is missing; returns the error message to the UI

**What needs building:**

*Backend:*
- `GET /api/strategy-code/{name}` — returns raw source of a strategy file
- `POST /api/strategy-code/upload` — accepts `.py` file, validates, writes to disk, hot-reloads registry
- `PUT /api/strategy-code/{name}` — accepts raw code string, overwrites file, hot-reloads registry
- `DELETE /api/strategy-code/{name}` — removes file, unregisters (blocks if any DB strategy row uses it)
- `GET /api/strategy-code/registry` — returns all registered strategy names + metadata
- Hot-reload: `importlib.reload()` + rebuild `STRATEGY_REGISTRY` dict in-place

*Frontend:*
- New page `/strategy-editor` with 3 tabs: Upload | Edit | Registry
- Monaco Editor component (`@monaco-editor/react`) for the Edit tab
- File drag-and-drop zone for Upload tab
- Registry tab: table of strategy types with usage count + delete button

---

## UI-04 · Candlestick Chart with Signal Markers ✅

**What it is:**
A professional candlestick chart for every symbol/timeframe being traded, showing exactly where the strategy placed each entry and exit signal — across all three brokers and all asset classes.

**Covers:**
- **Binance** — crypto pairs (BTC/USDT, ETH/USDT, etc.) — OHLCV from Binance REST (production, full history)
- **Alpaca** — US stocks (SPY, AAPL, etc.) — OHLCV from Alpaca Bars API
- **IBKR** — stocks, forex, and options underlyings — OHLCV via ib_insync historical data

**What the chart shows:**
- Candlestick OHLCV price action for any symbol + timeframe
- ▲ BUY / ▼ SELL markers at the exact candle where the signal fired (price + timestamp from DB)
- Indicator overlays: EMA 20/50, Bollinger Bands (20,2), RSI 14, MACD (12,26,9) sub-panel
- Volume histogram overlay (toggleable)

**What was built:**

*Backend (`backend/api/routes/charts.py`):*
- `GET /api/charts/candles?symbol&timeframe&broker&since&until` — paginated OHLCV using unix-ms `since`/`until` params; loops Binance 1000-candle chunks up to 5000 candles total; always uses production Binance (not testnet) for full historical range
- `GET /api/charts/signals?symbol&timeframe&broker&limit` — returns non-HOLD signals from DB as chart markers
- `GET /api/charts/symbols?broker` — returns tradeable symbol list per broker (Binance: live USDT pairs, Alpaca/IBKR: curated defaults)
- Supported timeframes: `1m 5m 15m 1h 4h 1d 3d 1w`

*Frontend (`frontend/src/pages/`):*
- `Chart.tsx` — grid layout wrapper with 1×1 / 1×2 / 2×2 layout picker
- `ChartPanel.tsx` — fully self-contained chart panel; each panel has its own broker/symbol/timeframe/range state
- Date range presets: **1W / 1M / 3M / 6M / 1Y** + **Custom** (from/to date picker)
- Symbol combobox with searchable broker-specific symbol list
- Indicator toggles: EMA, BB, Volume; sub-panel switch: RSI ↔ MACD
- ResizeObserver for responsive layout inside grid cells
- Synchronized scroll/zoom between main and sub-panel charts
- TradingView attribution watermark hidden via CSS (`index.css`)

---

## Priority Order

| # | Item | Effort | Impact |
|---|---|---|---|
| 1 | ~~**ML-01** Feedback loop~~ ✅ | Medium | Very High |
| 2 | ~~**UI-04** Candlestick chart~~ ✅ | Medium | High |
| 3 | **OPS-01** VPS deployment | Low | High |
| 4 | ~~**OPS-02** Auth / login~~ ✅ | Low | High (required for VPS) |
| 5 | **ML-02** Regime detector | High | High |
| 6 | **UI-03** Strategy code editor | Medium | High |
| 7 | **UI-01** Analytics dashboard | Medium | Medium |
| 8 | **EX-01** Options execution | High | Medium |
| 9 | **EX-02** Trailing stops | Low | Medium |
| 10 | **UI-02** Multi-timeframe | Medium | Medium |
| 11 | **ML-03** Portfolio optimization | High | Medium |
