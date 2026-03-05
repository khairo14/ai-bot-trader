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
- `tasks/ml_retrain.py` — Celery task scheduled weekly (Sunday 02:00 UTC); calls `ModelTrainer.retrain_all()`, reloads MLScorer cache in the worker process, then calls `POST /internal/ml/reload` via httpx to flush the FastAPI server's cache too
- `api/routes/ml.py` — `GET /api/ml/status` (model registry, outcome counts, win rate, avg P&L), `POST /api/ml/resolve` (manual trigger), `POST /api/ml/retrain` (manual trigger), `POST /api/ml/reload` (flush server-side MLScorer cache — useful after manual retrain)
- `main.py` — `POST /internal/ml/reload` (no auth, Celery-only internal endpoint that flushes the FastAPI process MLScorer cache)
- Dashboard shows feedback loop status, win rate, and avg signal P&L once first batch resolves

**Priority:** High — this is the most impactful Phase 2 item.

---

## ML-02 · Market Regime Detector ✅ COMPLETE

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

**What was built:**
- `backend/core/regime_classifier.py` — heuristic RegimeClassifier (deterministic rules on ADX, normalised ATR, BB width, EMA slope); singleton `regime_classifier`
- Per-regime `min_score` adjustments and ATR multipliers (`REGIME_SCORE_ADJUSTMENTS`, `REGIME_ATR_MULTIPLIERS`)
- Integrated into `hybrid.py`, `momentum.py`, `mean_reversion.py` — all strategies now call `regime_classifier.classify(data)` and adjust thresholds + SL/TP accordingly
- Mean-reversion strategy auto-suppressed when regime is `trending_up` or `trending_down`
- `Signal.regime` field now populated on every signal
- `backend/api/routes/regime.py` — `GET /api/regime?symbol=&timeframe=&broker=` endpoint
- Dashboard regime badge in ML Feedback Loop card (colour-coded by regime type)

---

## ML-03 · Multi-Symbol Portfolio Optimization ✅

**What it is:**
Instead of trading each strategy at equal size, this feature computes Sharpe-weighted capital allocations from historical trade outcomes — strategies with better risk-adjusted returns receive a larger weight.

**What was built:**
- `backend/models/portfolio_optimizer.py` — pure-Python Sharpe-ratio optimizer. Groups resolved `TradeOutcome` rows by `strategy_name`, computes Sharpe = `mean(returns)/std(returns)` per strategy, clips negatives to 0, normalises to 1.0 total, applies Pearson correlation penalty (`CORR_THRESHOLD=0.75`, `CORR_PENALTY=0.6`) for correlated strategies, writes `weight` back into each `Strategy.parameters`
- `backend/api/routes/portfolio_optimizer.py` — three endpoints:
  - `POST /api/portfolio-optimizer/run` — manual trigger
  - `GET /api/portfolio-optimizer/weights` — returns weighted + unweighted strategies with total, sorted by weight
  - `DELETE /api/portfolio-optimizer/weights` — clears all weight keys
- `backend/tasks/portfolio_rebalancer.py` — Celery task `tasks.portfolio_rebalancer.rebalance` for scheduled runs
- `backend/celery_app.py` — `portfolio-rebalance-weekly` beat task: every Sunday 03:00 UTC; `tasks.portfolio_rebalancer` in `include` list
- Portfolio weight multiplier applied in **both** execution paths: `signal_runner.py` (live) and `forward_test._run_one_strategy()` (paper)
- `frontend/src/pages/Dashboard.tsx` — **Portfolio Allocation widget** after ML Feedback Loop card:
  - Horizontal bar chart of strategy weights (CSS animated bars, colour-coded)
  - "Optimize Now" button triggers `POST /api/portfolio-optimizer/run` and refreshes weights inline
  - Empty state message when no trade history exists yet

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

## EX-02 · Trailing Stop ✅

**What it is:**
The execution engine currently places static stop loss and take profit. This adds dynamic trailing stops that follow price up (locking in profit as the trade moves favorably).

**What was built:**
- `trailing_stop_pct` field added to `TradeOutcome` DB model (e.g. `2.0` = trail by 2%)
- Alembic migration `e5f6a7b8c9d0` — `ADD COLUMN trailing_stop_pct FLOAT` to `trade_outcomes`
- `tasks/signal_runner.py` — reads `strategy.parameters["trailing_stop_pct"]` when creating each `TradeOutcome` row (live strategies); `api/routes/forward_test.py` does the same for paper strategies
- `tasks/outcome_resolver.py` — `_resolve_outcome()` now tracks `peak_high` / `trough_low` per candle; computes `effective_stop = peak * (1 - pct/100)` for longs (or `trough * (1 + pct/100)` for shorts); uses the better of trailing vs fixed stop; if trailing stop is triggered above entry → resolves as **win** (profit locked)
- `frontend/src/pages/Strategies.tsx` — "Trailing Stop %" number input in the Create/Edit strategy modal; displays confirmation hint when set; persisted into `strategy.parameters`

---

## UI-01 · Performance Analytics Dashboard ✅

**What it is:**
A dedicated analytics page showing historical performance across all strategies and symbols — beyond the basic P&L on the Dashboard.

**What was built:**
- `GET /api/analytics/summary` single endpoint aggregating all analytics from `TradeOutcome` table (resolved trades only)
- `frontend/src/pages/Analytics.tsx` — full recharts dashboard at `/analytics`:
  - 6 stat cards: Total Trades, Win Rate, Avg P&L, Total P&L, Best Trade, Worst Trade
  - Line chart equity curve (cumulative P&L per trade, reference at y=0)
  - Bar chart monthly returns (color-coded: green >2%, light-green >0%, light-red >-2%, red <=-2%)
  - Win rate by strategy — horizontal progress bars
  - Win rate by symbol — horizontal progress bars
  - Win rate by hour (UTC) — bar chart, green ≥50%, red <50%, reference at y=50
  - Rolling Sharpe ratio (30-trade window, annualised ×√252), reference lines at y=0 and y=1
  - Empty state UI when no resolved trade outcomes exist
- Nav item added to sidebar: `TrendingUp` icon → `/analytics`

---

## UI-02 · Multi-Timeframe Signal View ✅

**What it is:**
Currently signals are generated on one timeframe per strategy. This adds multi-timeframe confluence — a BUY on 1h is stronger if 4h also shows BUY.

**What was built:**
- `backend/api/routes/confluence.py` — two endpoints:
  - `GET /api/confluence?symbol&broker&strategy_type&timeframes=1h,4h,1d` — runs `SignalEngine.run()` concurrently for each TF via `asyncio.gather`; returns `{ consensus, confluence_score (0–1), timeframes: [{ timeframe, signal, confidence, regime, reasons, agrees_with_consensus }] }`
  - `GET /api/confluence/batch?symbols=BTC/USDT,ETH/USDT&broker&strategy_type&timeframes` — same for up to 10 symbols
  - `_consensus()` logic: finds plurality direction; score < 50% → "MIXED"; score = fraction of timeframes agreeing
- Confluence suppression applied in **both** execution paths: `signal_runner.py` (live strategies, Celery) and `forward_test._run_one_strategy()` (paper strategies, in-process scheduler)
- `frontend/src/pages/MultiTimeframe.tsx` — full `/multi-timeframe` analysis page:
  - Single-symbol mode: broker + strategy + timeframe checkboxes (15m/1h/4h/1d/1w) + symbol input → "Run Confluence Analysis"
  - Batch mode: comma-separated symbols up to 10
  - Results rendered as `ConfluenceCard` components: consensus banner, score bar, per-TF breakdown table with colored BUY/SELL/HOLD chips and agree/diverge indicators, reasons list
  - Empty state with guidance copy
- `frontend/src/components/SignalCard.tsx` — **confluence mini-check** added:
  - "Check multi-TF confluence" clickable link at card bottom (only for non-HOLD signals)
  - On click: fetches `/api/confluence?…&timeframes=1h,4h,1d`
  - Shows: 3 colored dots (one per TF, dimmed if diverging) + "X/3 aligned" + consensus label
  - Loading state: pulsing GitBranch icon + "Checking…" text
- `frontend/src/App.tsx` — `GitBranch` icon nav item + route `/multi-timeframe`

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

## UI-03 · Strategy Code Editor (Upload / Live Edit) ✅

**What it is:**
A built-in code editor in the dashboard where you can write, upload, and hot-reload strategy files without touching the filesystem manually or restarting the server.

**What was built:**

*Backend (`backend/api/routes/strategy_code.py`):*
- `GET /api/strategy-code/registry` — all STRATEGY_REGISTRY entries + DB usage counts per key
- `GET /api/strategy-code/{key}` — raw Python source (PlainTextResponse)
- `PUT /api/strategy-code/{key}` — accepts `{code: str}` JSON; syntax-checks via `compile()`, exec-validates, overwrites file, hot-reloads
- `POST /api/strategy-code/upload` — multipart `.py` upload; same validation pipeline; saves to `core/strategies/`, hot-reloads
- `DELETE /api/strategy-code/{key}` — removes file + unregisters; blocks if DB rows reference it; blocks built-in keys
- Hot-reload: `importlib.reload()` + in-place mutation of `STRATEGY_REGISTRY` dict; no server restart needed
- Validation: `compile()` syntax check → `exec()` in fresh namespace → exactly one `BaseStrategy` subclass → `generate_signal` method → `name` attribute
- Built-in protected keys: `hybrid_macd_rsi`, `momentum_breakout`, `mean_reversion_bb` (cannot delete/overwrite)

*Frontend (`frontend/src/pages/StrategyEditor.tsx`):*
- Page at `/strategy-editor` with 3 tabs: **Registry | Editor | Upload**
- **Registry tab**: card list of all strategies — key, class_name, description, asset_class, broker, builtin badge, DB usage count, Edit/Delete buttons
- **Editor tab**: Monaco Editor (`@monaco-editor/react`, python language, vs-dark theme, 500px height); strategy selector via Registry→Edit; Save & Hot-Reload button; read-only when no strategy selected; error toasts on validation failure
- **Upload tab**: drag-and-drop zone + file input for `.py` files; success/error banner; auto-refreshes Registry
- `@monaco-editor/react` installed via npm
- Nav item added to sidebar: `Code2` icon → `/strategy-editor`

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
| 5 | ~~**ML-02** Regime detector~~ ✅ | High | High |
| 6 | **UI-03** Strategy code editor | Medium | High |
| 7 | **UI-01** Analytics dashboard | Medium | Medium |
| 8 | **EX-01** Options execution | High | Medium |
| 9 | ~~**EX-02** Trailing stops~~ ✅ | Low | Medium |
| 10 | ~~**UI-02** Multi-timeframe~~ ✅ | Medium | Medium |
| 11 | ~~**ML-03** Portfolio optimization~~ ✅ | High | Medium |
