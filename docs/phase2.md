# Phase 2 Roadmap

Phase 1 is complete. All core infrastructure, execution, ML inference, and notifications are live.
Phase 2 focuses on making the system **smarter over time** and **production-hardened** for 24/7 autonomous operation.

---

## ML-01 · Automatic ML Feedback Loop (Closed-Loop Learning)

**What it is:**
Right now the ML model is static — it's trained once and frozen. This feature closes the loop: the bot monitors the real outcome of every signal it generated, then uses those outcomes to continuously improve the model.

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

**What improves:**
- Model adapts to current market regime (no longer frozen on 2024 data)
- Feature weights re-ranked as market dynamics evolve
- Model learns which signals work in bull vs. bear vs. ranging conditions
- Accuracy compounds over time as live outcome data accumulates

**What needs building:**
- `TradeOutcome` DB table — links `Signal.id` → outcome (win/loss/pnl) after N candles
- Celery beat task that resolves pending outcomes nightly
- Retrain pipeline that uses live outcomes as labels (in addition to historical yfinance data)
- AUC gate maintained on holdout set — only deploy if new model is better

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

## OPS-02 · Authentication & Multi-User Support

**What it is:**
Currently the dashboard has no login. This feature adds JWT-based authentication so the app can be safely hosted on a public VPS.

**What needs building:**
- User model + `POST /auth/login` + `POST /auth/register`
- JWT token middleware protecting all `/api/*` routes
- Login page on the frontend
- `SECRET_KEY` in `.env` is already wired — just needs the auth routes

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

## UI-04 · Candlestick Chart with Signal Markers

**What it is:**
A TradingView-style candlestick chart for every symbol/timeframe being traded, showing exactly where the strategy placed each entry and exit signal — across all three brokers and all asset classes.

**Covers:**
- **Binance** — crypto pairs (BTC/USDT, ETH/USDT, etc.) — OHLCV from Binance REST
- **Alpaca** — US stocks (SPY, AAPL, etc.) — OHLCV from Alpaca Bars API
- **IBKR** — stocks, forex, and options underlyings — OHLCV via ib_insync historical data

**What the chart shows:**
- Candlestick OHLCV price action for any symbol + timeframe
- ▲ BUY / ▼ SELL markers at the exact candle where the signal fired (price + timestamp from DB)
- HOLD signals shown as a neutral dot (optional, toggleable)
- Indicator overlays: EMA lines, RSI panel below, Bollinger Bands — same indicators the strategy actually uses
- Stop loss and take profit lines for open/recent paper trades
- For options: shows the underlying asset chart (e.g. SPY candles for an SPY options strategy)

**Symbol/Broker selector:**
- Dropdown to pick broker → asset class → symbol → timeframe
- Crypto pairs grouped under Binance (e.g. BTC/USDT · 1h, 4h, 1d)
- Stock tickers grouped under Alpaca / IBKR
- Options strategies show underlying chart with an "Options" badge

**What needs building:**

*Backend:*
- `GET /api/charts/candles?symbol=BTC/USDT&timeframe=4h&broker=binance&limit=200` — returns OHLCV array; fetches live from broker on demand (not stored in DB — fresh fetch each time)
- `GET /api/charts/signals?symbol=BTC/USDT&timeframe=4h&limit=50` — returns signals from DB with `timestamp`, `price`, `signal_type`, `strategy_name`
- Broker routing: Binance → `BinanceClient.get_ohlcv()`, Alpaca → Alpaca Bars, IBKR → `ib_insync.reqHistoricalData()`

*Frontend:*
- New page `/charts` in sidebar navigation
- [Lightweight Charts](https://tradingview.github.io/lightweight-charts/) (TradingView open-source lib) for the candlestick + marker rendering
- Symbol/broker/timeframe selector dropdowns at top
- Marker series: BUY = green up-arrow, SELL = red down-arrow, HOLD = grey dot
- Sub-panel for RSI (line series below the main chart)
- Toggle checkboxes: Show Signals | Show EMA | Show RSI | Show BB
- Auto-refreshes markers when a new `signal` WS event arrives

---

## Priority Order

| # | Item | Effort | Impact |
|---|---|---|---|
| 1 | **ML-01** Feedback loop | Medium | Very High |
| 2 | **UI-04** Candlestick chart | Medium | High |
| 3 | **OPS-01** VPS deployment | Low | High |
| 4 | **OPS-02** Auth / login | Low | High (required for VPS) |
| 5 | **ML-02** Regime detector | High | High |
| 6 | **UI-03** Strategy code editor | Medium | High |
| 7 | **UI-01** Analytics dashboard | Medium | Medium |
| 8 | **EX-01** Options execution | High | Medium |
| 9 | **EX-02** Trailing stops | Low | Medium |
| 10 | **UI-02** Multi-timeframe | Medium | Medium |
| 11 | **ML-03** Portfolio optimization | High | Medium |
