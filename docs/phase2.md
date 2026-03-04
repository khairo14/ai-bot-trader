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

## Priority Order

| # | Item | Effort | Impact |
|---|---|---|---|
| 1 | **ML-01** Feedback loop | Medium | Very High |
| 2 | **OPS-01** VPS deployment | Low | High |
| 3 | **OPS-02** Auth / login | Low | High (required for VPS) |
| 4 | **ML-02** Regime detector | High | High |
| 5 | **UI-01** Analytics dashboard | Medium | Medium |
| 6 | **EX-01** Options execution | High | Medium |
| 7 | **EX-02** Trailing stops | Low | Medium |
| 8 | **UI-02** Multi-timeframe | Medium | Medium |
| 9 | **ML-03** Portfolio optimization | High | Medium |
