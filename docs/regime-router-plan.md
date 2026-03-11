# Regime Router — Plan & Full System Audit

**Feature**: Automatic Strategy Switching Based on Market Regime  
**Status**: Pre-implementation audit  
**Date**: March 2026

---

## 1. What We Are Building (Agreed Plan)

### In Plain Terms
The bot currently runs one fixed strategy per symbol forever. We are adding a **Caddie** — a logic layer that reads the market condition every candle tick, decides which strategy is the best fit for that condition, and only lets that strategy evaluate the current candle. If the market is sideways, momentum strategies sit out. If the market is trending, mean-reversion strategies sit out.

### Agreed Execution Order (Final)
```
1. Candle gate          → skip if same candle already ran (no wasted processing)
2. Regime check         → classify market: trending_up / trending_down / ranging /
                          high_volatility / low_volatility    ← NEW
3. Strategy selection   → filter: is this strategy appropriate for this regime?  ← NEW
4. Strategy runs        → that strategy reads indicators, generates BUY/SHORT/HOLD
                          └─ ML veto embedded here (no change)
5. Persist to DB        → save signal for dashboard visibility (no change)
6. Multi-TF confluence  → do higher timeframes agree? suppress if not (no change)
7. Market hours gate    → is the broker open? (no change)
8. RiskManager          → R:R ≥ 2.0? position limits? circuit breakers? size the trade (no change)
9. ForwardEngine        → places the actual order (no change)
```

### Regime → Strategy Map
| Market Condition | Allowed Strategies |
|---|---|
| `trending_up` | `momentum_breakout`, `hybrid_macd_rsi`, `bull_call_spread` |
| `trending_down` | `momentum_breakout`, `hybrid_macd_rsi` |
| `ranging` | `mean_reversion_bb`, `iron_condor`, `covered_call` |
| `high_volatility` | `volatility_squeeze`, `iron_condor` |
| `low_volatility` | `volatility_squeeze`, `mean_reversion_bb` |

### Hysteresis Rule
Regime must be **stable for 3 consecutive candles** before a strategy switch is triggered. This prevents the bot from thrashing between strategies when ADX oscillates around the 25 threshold.

---

## 2. Build Phases

### Phase 1 — Regime Router + Hysteresis (Build First)
**Scope**: Backend only — `signal_runner.py` + minor addition to `regime_classifier.py`  
**What changes**: Before the strategy runs, classify the regime once per symbol. Compare against the regime map. Skip strategies that don't match. Track regime history in-memory (per symbol) to enforce the 3-candle hysteresis.  
**No DB schema changes. No frontend changes. No strategy code changes.**

### Phase 2 — Per-Row Regime Overrides (Polish)
**Scope**: Backend `signal_runner.py` only  
**What changes**: Read optional `regime_filter` key from `Strategy.parameters` JSON. If set, use that instead of the global regime map. Allows per-asset fine tuning without code changes, via the existing Edit Strategy UI.  
**Example**: `{ "regime_filter": ["trending_up", "ranging"] }` — this strategy only runs in those two regimes.  
**No DB schema changes. No frontend changes.**

### Phase 3 — ML Regime Model (After 3–6 Months Live Data)
**Scope**: New `regime_trainer.py` + updated `regime_classifier.py` + extended `ml_retrain.py`  
**What changes**: Train an XGBoost model on `TradeOutcome` data to predict regime earlier and with confidence scores. Replaces hard ADX ≥ 25 threshold with `P(regime) > 0.65`. Falls back to heuristic if confidence < 0.65.  
**Requires months of live `TradeOutcome` data first.**

---

## 3. Full System Audit

### 3.1 Backend — What Changes

#### `backend/tasks/signal_runner.py` ← ONLY FILE THAT CHANGES IN PHASE 1
- **Add**: `REGIME_STRATEGY_MAP` dict mapping each regime to its allowed strategy types
- **Add**: In-memory `_regime_history: dict[str, list[str]]` — tracks last N regime labels per symbol for hysteresis
- **Add**: `_check_regime_stable(symbol, new_regime, n=3)` — returns True if regime has been stable for N candles
- **Add**: After candle gate, before strategy runs: call `regime_classifier.classify(data)` once per symbol
- **Add**: Skip strategy if its `strategy_type` not in `REGIME_STRATEGY_MAP[current_regime]`
- **Add**: Log reason for skip: `"Regime-filtered: {strategy_type} skipped in {regime} regime"`
- **No removals** — all existing logic stays intact

#### `backend/core/regime_classifier.py` ← NO CHANGES IN PHASE 1
- Already classifies regimes correctly
- Already has `score_adjustment()` and `atr_multipliers()` used inside strategies
- `classify()` already returns a `RegimeResult` dataclass with `.regime` and `.features`
- The API endpoint `GET /api/regime` already exposes this to the frontend
- **Phase 1 requires zero changes here**

#### `backend/tasks/signal_runner.py` ← PHASE 2 ADDITION
- Read `params.get("regime_filter")` from each strategy's parameters JSON
- If `regime_filter` is set and is a list, use it instead of the global map for that strategy row

#### `backend/core/engine/signal_engine.py` — NO CHANGE
#### `backend/core/engine/forward_engine.py` — NO CHANGE
#### `backend/core/risk_manager.py` — NO CHANGE
#### `backend/core/strategies/*.py` — NO CHANGE (all 7 strategies untouched)
#### `backend/api/routes/*.py` — NO CHANGE in Phases 1 & 2
#### `backend/db/models.py` — NO CHANGE (Strategy.parameters is already a JSON column — `regime_filter` is stored there without schema migration)
#### `backend/alembic/` — NO MIGRATION NEEDED for Phases 1 & 2

---

### 3.2 Frontend — What Changes

#### Strategy Page (`Strategies.tsx`) — IMPACT ANALYSIS

**Current behaviour**: The page shows each strategy row with its fixed `strategy_type` (e.g., `hybrid_macd_rsi`). That type is set once at creation and never changes at runtime.

**After Phase 1**: The row still shows the same `strategy_type` in the UI — because the row itself hasn't changed. What changes is that at runtime the bot may **skip** that strategy if the regime doesn't match. The UI has no visibility into this yet.

**What the user sees without any UI change**:
- The strategy row still shows `hybrid_macd_rsi` as active (green toggle)
- But in the logs and signals table, some candles will produce no signal from that strategy (because it was regime-filtered)
- This could be confusing — the strategy looks active but isn't firing

**Recommended UI addition (Phase 1, optional but good)**:
- The signal card / signal reason already stores a `reasons` array in the DB. Regime-filter skips can be logged separately in the backend logs. The dashboard's Recent Signals panel would just show fewer signals for that strategy during hostile regimes — which is correct behaviour.
- No breaking changes to the UI in Phase 1.

**Recommended UI addition (Phase 2)**:
- Add a read-only "Current Regime" pill next to each strategy row showing what regime the symbol is currently in
- Add an optional `Regime Filter` multi-select to the Edit Strategy modal (mapping to `regime_filter` in parameters)
- The regime data is already available from the existing `GET /api/regime` endpoint — no new backend work

**Files that would change for optional Phase 2 UI**:
- `frontend/src/pages/Strategies.tsx` — add regime pill to row, add regime_filter to edit modal
- `frontend/src/pages/StrategyEditor.tsx` — NO CHANGE (strategy code editor, unrelated)

#### Other Frontend Pages — NO CHANGE NEEDED
- `Dashboard.tsx` — signals still flow normally, no change
- `Backtest.tsx` — backtests run strategies in isolation, regime router is a live-only feature
- `MarketScanner.tsx` — uses regime endpoint already, no change
- `MultiTimeframe.tsx` — reads confluence data, no change
- `Analytics.tsx` — reads trade outcomes, no change

---

### 3.3 Broker Configuration — IMPACT ANALYSIS

#### Binance
- All crypto strategies (`hybrid_macd_rsi`, `momentum_breakout`, `mean_reversion_bb`, `volatility_squeeze`) — **fully compatible**
- Regime router applies normally
- No config changes needed

#### Alpaca
- Alpaca strategies currently all use `hybrid_macd_rsi` (stocks: GOOGL, TSLA, SHOP, SOFI, QQQ, MSFT as seen in screenshot)
- After Phase 1: if `hybrid_macd_rsi` is the configured strategy type, it will only fire in `trending_up` and `trending_down` regimes. In `ranging` or `high_volatility`, those 6 strategy rows will produce no signal.
- **This is correct behaviour** — hybrid_macd_rsi is a trend-following strategy and should not fire in ranging markets
- **No broker API changes** — purely a signal-generation filter

#### IBKR
- IBKR strategies are options-based (`iron_condor`, `covered_call`, `bull_call_spread`)
- These are already mapped to `ranging` and `high_volatility` regimes (which is correct — options income strategies love low-trend environments)
- Options strategy confluence is already disabled (`0.0`) — no change
- **No broker API changes needed**

#### Per-Broker Regime Data Availability
| Broker | OHLCV Available | Regime Classifiable |
|---|---|---|
| Binance | Yes — real-time via testnet/live | Yes |
| Alpaca | Yes — via IEX feed | Yes |
| IBKR | Yes — via TWS API | Yes |

The regime classifier only needs OHLCV data (close, high, low, volume) which all three brokers already provide via their existing `get_ohlcv()` implementations.

---

### 3.4 DB Models — NO MIGRATION NEEDED

`Strategy.parameters` is already a `JSON` column. Adding `regime_filter` to it requires no schema migration — it's just a new key in the existing JSON object.

```json
// Example: existing parameters (no change to format)
{
  "strategy_type": "hybrid_macd_rsi",
  "symbol": "BTC/USDT",
  "timeframe": "1h",
  "limit": 200
}

// Phase 2: optional addition — no migration, no schema change
{
  "strategy_type": "hybrid_macd_rsi",
  "symbol": "BTC/USDT",
  "timeframe": "1h",
  "limit": 200,
  "regime_filter": ["trending_up", "trending_down"]
}
```

---

### 3.5 What Does NOT Change

| Component | Reason |
|---|---|
| `RiskManager` | Unchanged — still validates R:R, sizing, circuit breakers for every signal that passes the regime filter |
| All 7 strategy files | Unchanged — each strategy still generates its own signal, SL, TP, and direction independently |
| `ForwardEngine` | Unchanged — still handles paper/live execution, anti-pyramiding, opposing position close |
| `BacktestEngine` | Unchanged — backtests run strategies in isolation by design (regime router is a live runner concern) |
| `ml_scorer.py` | Unchanged — ML veto remains inside each strategy |
| `portfolio_optimizer.py` | Unchanged — Sharpe-based position sizing still applies after regime filter |
| `TradeOutcome` / ML feedback | Unchanged — outcomes still recorded for all executed trades |
| All API routes | Unchanged in Phases 1 & 2 |
| All Alembic migrations | No new migration in Phases 1 & 2 |

---

## 4. Risk of This Change

| Risk | Severity | Mitigation |
|---|---|---|
| Regime misclassification silences a profitable strategy | Medium | Hysteresis (3-candle stability) prevents false switches; per-row `regime_filter` override gives escape hatch |
| All Alpaca strategies go quiet in ranging markets | Low | Expected and correct — hybrid_macd_rsi is a trend strategy; it shouldn't trade ranges |
| Orphaned open positions when regime switches | Low | Regime filter only blocks *new* signals. Existing open positions continue to be managed by `monitor_sl_tp()` unchanged |
| Confusion from strategies "looking active but not firing" | Low | Backend logs will capture every skip with reason; Phase 2 UI pill makes it visible |

---

## 5. Files Touched Summary

### Phase 1 (Backend Only)
| File | Type of Change |
|---|---|
| `backend/tasks/signal_runner.py` | Add regime map, hysteresis buffer, filter logic |
| *(everything else)* | No change |

### Phase 2 (Backend + Optional UI)
| File | Type of Change |
|---|---|
| `backend/tasks/signal_runner.py` | Read `regime_filter` from strategy parameters |
| `frontend/src/pages/Strategies.tsx` | Add current regime pill + regime_filter field in edit modal |

### Phase 3 (ML Regime Model)
| File | Type of Change |
|---|---|
| `backend/models/regime_trainer.py` | New file — XGBoost regime trainer |
| `backend/core/regime_classifier.py` | Add `classify_ml()` alongside existing `classify()` |
| `backend/tasks/ml_retrain.py` | Extend to also retrain regime model |

---

## 6. Questions to Resolve Before Building

1. **Phase 1 only, or Phase 1 + 2 together?** Phase 2 adds per-row control via the UI — low effort, high value.
2. **Should skipped strategies be visible in the Signals table?** Current plan: no — skipped strategies don't generate a Signal DB row (they're silently bypassed before signal generation). Alternatively, we could write a HOLD signal with reason "regime-filtered" for full auditability.
3. **Hysteresis candle count**: 3 candles agreed. Adjustable as a constant, not hardcoded magic number.
