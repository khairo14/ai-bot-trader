# Scalping System Implementation Plan

> **Status:** ✅ COMPLETE — All 5 phases implemented, audited, and bug-free (March 2026)  
> **Branch approach:** All scalping work lives in new files. Minimal additive-only edits to `celery_app.py` and `main.py`.  
> **Guiding principle:** The existing swing/options system is never broken, never blocked, never aware of scalping activity.

---

## Decisions Log

| # | Decision | Rationale |
|---|---|---|
| 1 | **Shared `signals` + `trades` tables** | No separate table. Scalp signals are filtered in-app by `strategy_name LIKE 'scalp_%'`. Broker's `max_open_positions` is the shared gate — scalp trades count toward it. |
| 2 | **Increase broker rate limits** | Add a dedicated `SCALPING_RATE_LIMIT_PER_MIN` env var per broker. Binance: bump from default 1200 to full 6000 weight/min (we have live keys). Alpaca: increase burst limit. CCXT `rateLimit` override in `BinanceClient` constructor. |
| 3 | **Increase DB / Redis pool size** | `SCALPING_DB_POOL_SIZE` env var; `AsyncSessionLocal` pool_size raised from default 5 → 20, max_overflow 10 → 30. Redis connection pool maxconnections raised from default 10 → 50. Applied in `db/database.py` — single-line change. |
| 4 | **Binance live API keys for scalping ML data** | Use existing `BINANCE_API_KEY` / `BINANCE_API_SECRET` (live credentials, not testnet) in the scalping ML trainer. Binance live API gives unlimited 1m/5m OHLCV history. No new env vars needed. |
| 5 | **Separate ML models for scalping** | Scalping ML models stored under keys `"symbol:timeframe:scalp"` in `data/models/latest.json`. Entirely separate feature set, labels, and retrain schedule from the swing models. Swing `MLScorer` is not modified. |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                      EXISTING SWING SYSTEM                      │
│   signal_runner → SignalEngine → ForwardEngine → Broker         │
│   _sl_tp_heartbeat (60s)                                        │
│   ML models: "BTC/USDT:1h", "ETH/USDT:1h" …                    │
│   runtime/regime_settings.json                                  │
│   runtime/risk_state.json                                       │
│   WebSocket: /ws  (main signal feed)                            │
└─────────────────────────────────────────────────────────────────┘
         ✦ ZERO CHANGES to any file above ✦

┌─────────────────────────────────────────────────────────────────┐
│                      NEW SCALPING SYSTEM                        │
│   scalping_runner (Celery, every 60s) OR                        │
│   scalping_stream.py (WebSocket kline, Phase 5)                 │
│     → ScalpingEmaVwap → ForwardEngine (reused, unmodified)      │
│   _scalping_sl_tp_heartbeat (5s, new async task in main.py)     │
│   ScalpingMLScorer: models "BTC/USDT:5m:scalp" …               │
│   runtime/scalping_settings.json                                │
│   WebSocket: /ws/scalping (dedicated scalp signal feed)         │
└─────────────────────────────────────────────────────────────────┘

SHARED (read-only by scalping):
  brokers/binance_client.py, alpaca_client.py, ibkr_client.py
  core/engine/forward_engine.py
  core/engine/price_stream.py   (PriceStreamManager WebSocket cache)
  db/models.py  (signals + trades tables — scalp rows tagged scalp_*)
```

---

## Phase 1 — Strategy + Settings (no execution)

### New Files

#### `backend/core/strategies/base_scalping.py`
- Subclass of `BaseStrategy`
- **Overrides `_enhance_signal()`** to:
  - Skip confirmation candle filter entirely
  - Skip trailing stop assignment (scalps use fixed tight stops — trailing drags exits)
  - Still runs S/R TP snap (optional, configurable via `scalping_settings.json`)
- All other base logic (signal dataclass, asset_class, broker fields) inherited unchanged
- No modifications to `base.py`

#### `backend/core/strategies/scalping_ema_vwap.py` — `ScalpingEmaVwap`

**Class name:** `ScalpingEmaVwap`  
**Strategy key (DB `strategy_type`):** `scalp_ema_vwap`  
**Timeframe:** `5m` default (configurable; also suitable for `1m`, `3m`)  
**Broker:** `binance` default (Binance crypto; can be set to `alpaca` for stocks)  
**Asset class:** `crypto` default  
**Min candles:** 30  

**Indicators used:**
| Indicator | Config |
|---|---|
| EMA ribbon | fast=8, mid=13, slow=21 |
| VWAP | existing `tools/advanced/vwap.py` — 20-period rolling |
| ATR | 14-period |
| ROC (Rate of Change) | 5-period (momentum gate) |
| Volume | 20-period SMA ratio |

**Entry Logic (scored 0–6 each direction):**
| Condition | Points |
|---|---|
| EMA8 > EMA13 > EMA21 (ribbon stacked bullish) | +2 long |
| Price above VWAP (>0.1% distance) | +1 long |
| ROC-5 > 0 (positive momentum) | +1 long |
| Volume ≥ 1.2× average | +1 either direction |
| ATR in normal/high range (not dead market) | +1 either direction |
| Mirror conditions for short | +2/+1/+1 short |

**Threshold:** `min_score = 4` (regime-independent — scalping has its own session filter)

**Session filter** (configurable in `scalping_settings.json`):
- Crypto (Binance): no session filter — 24/7
- Stocks (Alpaca): NYSE active sessions only — `09:30–11:30` and `14:30–16:00` UTC-5

**Stop / Target:**
- `stop_loss = entry ± 0.8 × ATR`
- `take_profit = entry ± 1.6 × ATR` (2:1 R:R maintained)
- No trailing stop (overridden from base)

**ML Gate (Phase 4):** `ScalpingMLScorer` probability < 0.40 → suppress signal (slightly stricter than swing's 0.35 because scalp signals are more frequent)

**No multi-TF confluence** — scalping reacts to immediate price action; checking a higher-TF at the same moment adds too much latency.

**No regime classifier gate** — instead uses the session filter + spread check (max spread 0.05% configurable).

#### `backend/runtime/scalping_settings.json`
```json
{
  "timeframe": "5m",
  "risk_per_trade_pct": 0.5,
  "sl_atr_mult": 0.8,
  "tp_atr_mult": 1.6,
  "min_volume_ratio": 1.2,
  "max_spread_pct": 0.05,
  "min_score": 4,
  "session_filter": {
    "crypto": null,
    "stock": ["14:30-21:00"]
  },
  "sr_tp_snap": false,
  "ml_veto_threshold": 0.40,
  "enabled": true
}
```

---

## Phase 2 — Celery Task + Signal Persistence (suggestion mode)

### New Files

#### `backend/tasks/scalping_runner.py`
Core differences from `signal_runner.py`:

| Aspect | Swing `signal_runner` | Scalping `scalping_runner` |
|---|---|---|
| Strategies iterated | Any active strategy | Only `strategy_type LIKE 'scalp_%'` |
| Settings file | `runtime/regime_settings.json` | `runtime/scalping_settings.json` |
| Regime classifier | Full 5-regime ADX/ATR classifier | Skipped — uses session filter only |
| Confirmation candle gate | Applied in `_enhance_signal` | Skipped (`BaseScalpingStrategy` override) |
| In-memory candle dedup key | `_last_candle_fired` dict | `_last_scalp_candle_fired` dict (separate — no collision) |
| Multi-TF confluence | Yes (0.5 default) | Not applied (hardcoded 0.0) |
| Regime hysteresis Redis keys | `regime_hysteresis:*` | Never writes to these keys |
| Execution mode at Phase 2 | As configured | Forced `suggestion` until Phase 3 is validated |
| DB dedup lock prefix | `signal_dedup:` | `scalp_signal_dedup:` (separate advisory lock namespace) |

#### `backend/api/routes/scalping.py`
REST endpoints:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/scalping/signals` | Last N scalp signals (filtered `strategy_name LIKE scalp_%`) |
| `GET` | `/scalping/stats` | Win rate, avg hold time, avg PnL, count today |
| `GET` | `/scalping/settings` | Returns current `scalping_settings.json` |
| `POST` | `/scalping/settings` | Update settings at runtime (persisted to file) |
| `POST` | `/scalping/enable` | Set `enabled: true/false` in settings |

### Minimal Existing File Edits

#### `backend/celery_app.py` — additions only
```python
# Add to include list:
include=[..., "tasks.scalping_runner", "tasks.scalping_ml_retrain"]

# Add to beat_schedule:
"run-scalping-every-60s": {
    "task": "tasks.scalping_runner.run_scalping_signals",
    "schedule": 60,
    "options": {"expires": 55},
},
"scalp-ml-retrain-daily": {
    "task": "tasks.scalping_ml_retrain.retrain_scalp_models",
    "schedule": crontab(hour=4, minute=30),  # daily, after swing retrain
},
```

#### `backend/main.py` — additions only
```python
# Add import:
from api.routes import scalping as scalping_routes

# Add router registration (in lifespan or app.include_router block):
app.include_router(scalping_routes.router, prefix="/scalping", tags=["scalping"])

# Add task creation in lifespan (alongside existing heartbeat tasks):
asyncio.create_task(_scalping_sl_tp_heartbeat())   # Phase 3
asyncio.create_task(scalping_stream_manager.start(AsyncSessionLocal))  # Phase 5 only
```

---

## Phase 3 — Dedicated 5-Second SL/TP Heartbeat

### New async function in `backend/main.py`

**`_scalping_sl_tp_heartbeat()`** — runs every **5 seconds** (vs 60s for swing):
1. Queries only open trades where `strategy_name LIKE 'scalp_%'`
2. Reads prices from `PriceStreamManager` WebSocket cache (no REST call unless cache miss)
3. Falls back to REST `get_bid_ask()` only on cache miss
4. Calls `ForwardEngine.monitor_sl_tp()` with scalp-trade filter
5. **Does not touch swing trades** — filtered by strategy_name prefix

**Why this doesn't break the existing heartbeat:**
- Both heartbeats run as independent `asyncio.Task` objects
- They both call `ForwardEngine.monitor_sl_tp()` — this function holds `_MONITOR_SL_TP_LOCK`
- The 5s scalp heartbeat will queue behind the 60s swing heartbeat if they collide — maximum wait is the duration of one swing SL/TP check (typically <1s)
- A `strategy_prefix` filter argument is added to `monitor_sl_tp()` to scope each heartbeat's query — this is the **only addition to `forward_engine.py`** and it is backward-compatible (default `None` = no filter, existing behavior unchanged)

---

## Phase 4 — Scalping ML Model

### New Files

#### `backend/tasks/scalping_ml_retrain.py`

**Data source:** Binance live REST API (`BINANCE_API_KEY` / `BINANCE_API_SECRET`)  
Fetches 1m/5m OHLCV directly — full history, not limited like yfinance.

**Feature set (13 features — superset of swing's 11):**

| Feature | Description |
|---|---|
| `rsi` | RSI-14 |
| `macd_hist` | MACD histogram |
| `atr_norm` | ATR / close |
| `vol_ratio` | Volume / 20-SMA volume |
| `bb_pct` | Bollinger Band position 0–1 |
| `log_ret` | log(close/prev_close) |
| `ema_ribbon_spread` | (EMA8 − EMA21) / close — ribbon tension |
| `vwap_dist_pct` | (close − VWAP) / VWAP × 100 |
| `roc_5` | Rate of Change 5 periods |
| `spread_pct` | (ask − bid) / mid (from ticker) |
| `time_of_day_sin` | sin(2π × minute_of_day / 1440) — encodes time cyclically |
| `time_of_day_cos` | cos(2π × minute_of_day / 1440) |
| `session_id` | 0=Asia 1=London 2=NY 3=Off-hours |

**Label:** `1` if price moves > `1.0 × ATR` in the **next 3 candles** (faster resolution than swing's 24-candle window)

**Model registry keys:** `"BTC/USDT:5m:scalp"`, `"ETH/USDT:5m:scalp"`, etc.  
Stored in `data/models/latest.json` alongside swing keys — no collisions.

**AUC threshold:** 0.55 (same as swing) — must beat before deployment.

**Retrain schedule:** Daily at 04:30 UTC (after swing weekly retrain; daily because 5m models have shorter data windows and drift faster).

#### `backend/core/scalping_ml_scorer.py` — `ScalpingMLScorer`
- Separate singleton from `MLScorer`
- Same interface: `predict(features_df, symbol, timeframe)` → `float | None`
- Loads models with `:scalp` suffix key
- Thread-safe double-checked locking (same pattern as `MLScorer`)
- **No modification to `core/ml_scorer.py`**

---

## Phase 5 — WebSocket Kline Engine (genuine sub-minute reaction)

> This phase converts from REST-poll (60s lag) to live candle formation (<1s lag). Deploy only after Phase 1–4 are paper-tested and profitable.

### New Files

#### `backend/core/engine/scalping_stream.py` — `ScalpingStreamManager`

**What it does:**
1. Subscribes to Binance WebSocket kline stream: `wss://stream.binance.com/ws/{symbol}@kline_{interval}` for each active scalp strategy symbol
2. Assembles OHLCV candles in-memory as events arrive: updates `O/H/L/C/V` on every tick
3. On `event['k']['x'] == True` (candle closed): immediately fires `ScalpingEmaVwap.generate_signal()` on the completed candle
4. Calls `ForwardEngine.process_signal()` directly — no Celery task overhead
5. Manages reconnection with exponential backoff (same pattern as IBKR manager)
6. Maintains a `_last_candle_ts` dict to prevent duplicate fires on reconnect

**Coexistence with `PriceStreamManager`:**
- `PriceStreamManager` streams trade/ticker events for SL/TP prices (existing, unchanged)
- `ScalpingStreamManager` streams kline events for candle formation (new)
- These are separate WebSocket connections to different Binance endpoints — no conflict

**Fallback:** If Phase 5 stream is down, the Celery `scalping_runner` (Phase 2) continues polling every 60s via REST — built-in degradation with no manual intervention needed.

#### `backend/api/routes/scalping_ws.py`

WebSocket endpoint: `/ws/scalping`  
- Separate channel from `/ws` (swing feed)
- Only pushes events where `strategy_name LIKE 'scalp_%'`
- Payload mirrors existing WS signal format so frontend can reuse `SignalCard` component

**Why dedicated WS channel:**  
Scalp signals at 5m resolution on 5+ symbols = potentially many signals per hour. Flooding the main `/ws` feed used by the dashboard would degrade the swing signal display. Separate channel keeps both feeds clean.

---

## Infrastructure Changes (when to apply)

These changes apply **before Phase 2** is deployed (they're safe at any time):

### `backend/db/database.py` — pool size increase
```python
# Change pool_size and max_overflow:
engine = create_async_engine(
    settings.database_url,
    pool_size=20,          # was 5
    max_overflow=30,       # was 10
    pool_pre_ping=True,
)
```

### `backend/brokers/binance_client.py` — rate limit increase
```python
# In _get_exchange():
exchange_options = {
    "rateLimit": 100,      # was default 50ms (1200 req/min) → 100ms but weight-aware
    "enableRateLimit": True,
    # Use full 6000 weight/min budget with live keys
}
```

### `.env` — new scalping-specific env var
```dotenv
# ── Scalping ─────────────────────────────────────────────────────
SCALPING_ENABLED=true
SCALPING_TIMEFRAME=5m
SCALPING_RISK_PER_TRADE_PCT=0.5
SCALPING_MAX_SPREAD_PCT=0.05
```

---

## Database — No New Tables, Filter by Name

All scalp signals and trades are stored in the **existing `signals` and `trades` tables**.

**How to distinguish scalp vs swing rows:**
- `signals.strategy_name` will be `scalp_ema_vwap` (or any future `scalp_*` strategy)
- `trades.strategy_name` mirrors this
- Every API endpoint and query that is scalping-specific uses: `WHERE strategy_name LIKE 'scalp_%'`
- Every query that is swing-specific either uses `WHERE strategy_name NOT LIKE 'scalp_%'` OR (since they were there before scalping was added) implicitly excludes them because there were no scalp rows at the time

**Position counting:**
- Scalp trades count toward the broker's `max_open_positions` limit (shared gate as decided)
- The scalp heartbeat (5s) closes scalp trades fast enough that they rarely occupy slots for long
- In practice at 0.5% risk/trade with tight 0.8×ATR stops, scalp positions turn over in minutes

---

## Frontend / UI Changes

### Overview of what needs to change

The frontend needs to support three things:
1. **Creating / managing a scalping strategy** (new strategy type in the existing Strategies page)
2. **Viewing scalp signals separately** from swing signals (new dedicated Scalping page or tab)
3. **Scalping-specific settings panel** to control `scalping_settings.json` at runtime

---

### UI Change 1 — Strategies Page (`Strategies.tsx`)

**Current:** Strategy type dropdown lists `hybrid_macd_rsi`, `momentum_breakout`, `mean_reversion_bb`, `volatility_squeeze`, `iron_condor`, `bull_call_spread`, `covered_call`.

**Required changes:**
- Add `scalp_ema_vwap` to the strategy type list (pulled from `useStrategyRegistry` hook — backend auto-exposes it once the class is registered)
- When `scalp_ema_vwap` is selected:
  - **Timeframe dropdown** should default to `5m` and only show `1m`, `3m`, `5m`, `15m` options (higher TFs don't make sense for scalping)
  - **Broker** defaults to `binance`
  - **Asset class** auto-set to `crypto` (or `stock` for alpaca) — same logic as now
  - **Regime mode** field hidden (scalping doesn't use the regime router)
  - A yellow badge/callout appears: *"Scalping Strategy — uses dedicated 5s SL/TP monitor and separate ML model. Confirm candle filter is bypassed."*
- No new pages needed here — just conditional field display and a visual badge

**Badge component:** Reuse the existing `ModeBadge` pattern — add a `ScalpBadge` variant with amber/orange colour.

---

### UI Change 2 — New Scalping Page (`Scalping.tsx`)

**Nav item:** Add to sidebar nav in `App.tsx`:
```
{ to: '/scalping', label: 'Scalping', icon: Zap }   // Zap icon — already imported
```

**Page layout:**

```
┌────────────────────────────────────────────────────────────┐
│  ⚡ Scalping Monitor                   [Enable / Disable]  │
│                                                            │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐         │
│  │ Today   │ │Win Rate │ │ Avg PnL │ │ Avg Hold│         │
│  │ Signals │ │  68%    │ │ +0.18%  │ │  4.2m   │         │
│  └─────────┘ └─────────┘ └─────────┘ └─────────┘         │
│                                                            │
│  [ BTC/USDT ][ ETH/USDT ][ All Symbols ]   [Live ● ]     │
│                                                            │
│  ┌──────────────────────────────────────────────────────┐ │
│  │ Live Scalp Signal Feed  (WebSocket /ws/scalping)     │ │
│  │  ● BUY BTC/USDT  @ 84,210  SL 84,050  TP 84,530    │ │
│  │  ● SHORT ETH/USDT @ 3,210  SL 3,225   TP 3,182     │ │
│  └──────────────────────────────────────────────────────┘ │
│                                                            │
│  ┌── Scalp Trade History ────────────────────────────── ┐ │
│  │  Table: symbol / side / entry / exit / PnL / hold   │ │
│  │  time / status — filterable by date/symbol           │ │
│  └────────────────────────────────────────────────────── ┘ │
│                                                            │
│  ┌── ML Model Status ────────────────────────────────── ┐ │
│  │  Shows ScalpingMLScorer models (`:scalp` keys)        │ │
│  │  Last trained / AUC / data points                     │ │
│  └────────────────────────────────────────────────────── ┘ │
└────────────────────────────────────────────────────────────┘
```

**WebSocket connection:** Connects to `/ws/scalping` — separate from the main `/ws` used by Dashboard. Uses the existing `useWebSocket` hook with a different URL:
```typescript
const SCALP_WS_URL = `${proto}//${window.location.host}/ws/scalping`
```

**Reused components:**
- `SignalCard` component — used as-is; scalp signals have the same schema
- `SkeletonStat` / `SkeletonList` — loading states
- `MarketClock` — reused in header
- Recharts `LineChart` — mini equity curve for scalp-only PnL (reuse Analytics chart pattern)

**New components required (small, self-contained):**
- `ScalpHoldTimeChart` — bar chart of hold-time distribution (1m, 2m, 3m, 4m, 5m buckets)
- `ScalpSignalFeed` — live-updating list consuming `/ws/scalping`, auto-scrolls, max 50 rows before pagination

---

### UI Change 3 — Scalping Settings Panel

**Location:** Either a collapsible section at the bottom of the `Scalping.tsx` page, OR a dedicated tab inside the existing `Settings.tsx` page.

**Recommended:** Collapsible section in `Scalping.tsx` (keeps all scalping UI in one place).

**Fields exposed:**

| Field | Type | Bound to `scalping_settings.json` key |
|---|---|---|
| Enabled | Toggle | `enabled` |
| Timeframe | Select (1m / 3m / 5m / 15m) | `timeframe` |
| Risk per trade | Number input (0.1–2.0%) | `risk_per_trade_pct` |
| SL ATR multiplier | Slider (0.5–2.0) | `sl_atr_mult` |
| TP ATR multiplier | Slider (1.0–4.0) | `tp_atr_mult` |
| Min volume ratio | Number input | `min_volume_ratio` |
| Max spread % | Number input | `max_spread_pct` |
| S/R TP snap | Toggle | `sr_tp_snap` |
| ML veto threshold | Slider (0.30–0.55) | `ml_veto_threshold` |

**API calls:** `GET /scalping/settings` on load, `POST /scalping/settings` on save.  
Reuse existing Settings page save/toast patterns.

---

### UI Change 4 — Dashboard Integration (minor)

**Existing Dashboard** shows a combined signal feed. Two small changes:
1. Add a filter toggle: `[ All ] [ Swing ] [ Scalp ]` — filters `strategy_name` prefix client-side
2. The portfolio summary stat card `Open Positions: 3/5` already includes all trades — no change needed

---

### UI Change 5 — Analytics Page Filter

The existing `Analytics.tsx` `by_strategy` breakdown will automatically show `scalp_ema_vwap` alongside `hybrid_macd_rsi` etc. once scalp trades exist.

**Optional enhancement:** Add a `Mode` filter toggle `[ All ] [ Swing only ] [ Scalp only ]` to the Analytics page to view separated performance curves. This is low priority — the `by_strategy` table already separates them.

---

## File Change Inventory

### New Files (backend)
```
backend/core/strategies/base_scalping.py
backend/core/strategies/scalping_ema_vwap.py
backend/core/scalping_ml_scorer.py
backend/core/engine/scalping_stream.py          ← Phase 5 only
backend/tasks/scalping_runner.py
backend/tasks/scalping_ml_retrain.py            ← Phase 4
backend/api/routes/scalping.py
backend/api/routes/scalping_ws.py               ← Phase 5
backend/runtime/scalping_settings.json
```

### New Files (frontend)
```
frontend/src/pages/Scalping.tsx
frontend/src/components/ScalpSignalFeed.tsx
frontend/src/components/ScalpHoldTimeChart.tsx
```

### Existing Files — Additive Edits Only

| File | Change | Lines added |
|---|---|---|
| `backend/celery_app.py` | +2 entries to `include` list, +2 beat schedule blocks | ~15 |
| `backend/main.py` | +1 router import, +1 `include_router`, +1 `create_task` | ~6 |
| `backend/db/database.py` | Pool size config numbers only | ~2 |
| `backend/brokers/binance_client.py` | `rateLimit` value in exchange options | ~2 |
| `backend/.env` | 4 new scalping env vars | ~6 |
| `frontend/src/App.tsx` | +1 nav item, +1 route, +1 import | ~6 |
| `frontend/src/pages/Strategies.tsx` | Conditional badge + field hiding for `scalp_*` types | ~30 |
| `frontend/src/pages/Dashboard.tsx` | `[ All / Swing / Scalp ]` filter toggle | ~20 |

### Files Never Touched
```
backend/core/strategies/base.py
backend/core/strategies/hybrid.py
backend/core/strategies/momentum.py
backend/core/strategies/mean_reversion.py
backend/core/strategies/volatility_squeeze.py
backend/core/strategies/iron_condor.py
backend/core/strategies/bull_call_spread.py
backend/core/strategies/covered_call.py
backend/core/risk_manager.py
backend/core/ml_scorer.py
backend/core/regime_classifier.py
backend/core/engine/forward_engine.py      (additive edit only: `strategy_prefix` param added to `monitor_sl_tp` + `_monitor_sl_tp_inner`; default=None preserves all existing behaviour)
backend/core/engine/price_stream.py        (stream cache reused, not modified)
backend/core/engine/signal_engine.py
backend/core/engine/backtest_engine.py
backend/tasks/signal_runner.py
backend/tasks/ml_retrain.py
backend/tasks/outcome_resolver.py
backend/tasks/portfolio_rebalancer.py
backend/runtime/regime_settings.json
backend/runtime/risk_state.json
backend/runtime/broker_modes.json
frontend/src/pages/ForwardTest.tsx
frontend/src/pages/Backtest.tsx
frontend/src/pages/Analytics.tsx           (auto-shows scalp data, no code change)
frontend/src/pages/Settings.tsx
frontend/src/pages/MarketScanner.tsx
frontend/src/pages/Chart.tsx
frontend/src/pages/StrategyEditor.tsx
frontend/src/pages/StrategyLibrary.tsx
frontend/src/pages/MultiTimeframe.tsx
```

---

## Build Order and Gates

```
Phase 1  ─── Strategy + Settings files only
             Gate: unit-test ScalpingEmaVwap.generate_signal() on historical data
             → signals correct direction, no confirmation lag

Phase 2  ─── Celery task + REST routes + DB writes (suggestion mode only)
             Gate: paper-run for 1 week, inspect signal quality and frequency
             → win rate > 45% on backtested signals before moving to Phase 3

Phase 3  ─── 5s SL/TP heartbeat
             Gate: confirm scalp trades open and close within expected max hold time
             → no orphaned open trades after 30min

Phase 4  ─── Scalping ML model
             Gate: AUC ≥ 0.55 on holdout set before enabling ML veto
             → paper-run 1 week with ML veto on vs off, compare win rate

Phase 5  ─── WebSocket kline engine (optional, deploy when REST-poll system is profitable)
             Gate: WS engine fires within 2s of candle close on Binance testnet
             → latency log shows consistent sub-2s entry initiation
```

---

---

## Post-Implementation Audit Log

Three rounds of full audits were performed after implementation. All bugs resolved; zero errors in all files.

### Round 1 — Immediate post-Phase-5 audit (6 bugs)

| # | File | Bug | Fix |
|---|------|-----|-----|
| 1 | `scalping_stream.py` | `asyncio.sleep` outside `CancelledError` try block → orphaned child tasks | Moved sleep inside try |
| 2 | `scalping_stream.py` | No advisory lock, no Signal row, no DB dedup → duplicate trades possible | Full `scalping_runner` dedup pattern replicated |
| 3 | `scalping_stream.py` | `ForwardEngine()` fresh per candle | `get_forward_engine()` singleton |
| 4 | `scalping_stream.py` | `_TRACKABLE = {"BUY", "SELL"}` missing SHORT/COVER | `{"BUY", "SELL", "SHORT", "COVER"}` |
| 5 | `scalping_stream.py` | `strats[0].broker.value` — no enum guard | `hasattr(broker, "value")` guard |
| 6 | `scalping_ws.py` | `receive_text()` raises RuntimeError on binary frames | Changed to `receive()` |

### Round 2 — Deep full-codebase audit (10 bugs)

| # | File | Bug | Fix |
|---|------|-----|-----|
| 1 | `scalping.py` | `avg_hold / total` — wrong denominator (total includes rows without `candles_held`) | `/ len(_held_vals)` |
| 2 | `scalping.py` | `/signals` endpoint returned HOLD records (noise in feed) | Added `.where(Signal.signal != SignalType.HOLD)` |
| 3 | `scalping_runner.py` | HOLD signals acquired pg advisory lock + wrote a Signal row | Skip with `continue` before lock section |
| 4 | `scalping_runner.py` | `strat.broker.value` — no enum guard | `hasattr` guard |
| 5 | `scalping_runner.py` | `ForwardEngine()` fresh per strategy | `get_forward_engine()` singleton |
| 6 | `scalping_ml_retrain.py` | Live training labels mixed BUY + SHORT directions into one model | Added `direction` param + `signal_type == direction` filter to `_fetch_live_scalp_labels` |
| 7 | `scalping_ml_retrain.py` | SHORT model never received live label enrichment | Added second `_fetch_live_scalp_labels(direction="SHORT")` call |
| 8 | `scalping_ml_retrain.py` | Redundant double-ternary `… else 0 if … else 0` | Simplified |
| 9 | `scalping_ml_scorer.py` + `scalping_ema_vwap.py` | Live bid/ask spread never injected at inference time (trained as 0.0) | `spread_pct` param threaded through `_predict` → `predict_proba_scalp*` → `generate_signal` |
| 10 | `scalping_stream.py` | Stale frozen `strats` list — strategy changes never reflected without restart | DB query fresh on every candle close; `_refresh` only tracks stream keys |

### Round 3 — Flow, timing, and process audit (5 bugs)

| # | File | Bug | Fix |
|---|------|-----|-----|
| 1 | `scalping_stream.py` | `TradeOutcomeModel` missing `stop_loss` and `take_profit` — outcome resolver could not detect SL/TP hits | Added both fields |
| 2 | `scalping_stream.py` (×2) | `strat.execution_mode.value` — `execution_mode` is `nullable=True` on `StrategyModel`; crashes on `None` | `… if strat.execution_mode is not None else "SUGGESTION"` |
| 3 | `scalping_runner.py` (×2) | Same `execution_mode` null guard missing | Same fix |
| 4 | `scalping_stream.py` | `sig.strategy_name` left as class attribute `"scalp_ema_vwap"` instead of user-defined `strat.name` — dedup cross-check between stream and Celery runner failed | Added `sig.strategy_name = strat.name` after `generate_signal()` |
| 5 | `scalping_stream.py` | Hardcoded `ScalpingEmaVwap()` before strategy loop — future strategy types silently ran through wrong class | Replaced with `_SCALP_STRATEGY_REGISTRY` dict + per-strategy lookup with unknown-type warning (mirrors runner pattern) |
| 6 | `scalping_runner.py` | WS broadcast missing `"source": "celery"` field — frontend could not distinguish source from stream events | Added `"source": "celery"` to broadcast payload |
| 7 | `scalping_stream.py` | `enabled` flag in `scalping_settings.json` never read by stream — `POST /scalping/enable` disabled Celery runner but stream kept firing | Added settings file read on every candle close before OHLCV fetch |

---

## Risk / Known Gaps

| Risk | Mitigation |
|---|---|
| Scalp trades eat into swing `max_open_positions` | Set scalp strategies to fast TFs (5m) with tight stops so they close in minutes; optionally increase `MAX_OPEN_POSITIONS` per broker in Settings UI |
| Binance rate limits under dual-stream load (price stream + kline stream + REST) | Phase 5 kline stream replaces the per-strategy OHLCV REST fetch — net load is similar or lower |
| DB connection pool exhaustion under 5s SL/TP heartbeat | Pool size increase (Decision 3) covers this; heartbeat uses a single quick SELECT + conditional UPDATE |
| ML model quality on short timeframes | Daily retrain with fresh Binance data; AUC gate prevents bad models deploying; fallback = no ML veto (signals still pass rule-based filters) |
| Scalp WS `/ws/scalping` adds frontend reconnect complexity | Reuse existing `useWebSocket` hook; reconnect logic already implemented there |
