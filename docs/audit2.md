# System Audit 2 — Full Findings & Fix Log

**Date:** June 2025  
**Scope:** Full backend audit — all engines, tasks, API routes, DB models, broker clients, ML pipeline.  
**Status key:** ✅ Fixed | ⚠️ Documented / future | 🔧 In progress

---

## Summary

| Category | Count | Fixed |
|----------|-------|-------|
| Bugs (B) | 8 | 8 |
| Gaps (G) | 12 | 12 |
| Improvements (I) | 9 | 8 |

---

## Bugs

### B1 — Forex trailing stop too tight ✅ (user-fixed)
- **File:** `core/strategies/` — strategy parameters
- **Issue:** `trailing_stop_pct=0.1%` too tight for forex; triggering on noise spreads.
- **Fix:** User manually adjusted trailing stop % in strategy parameters.

---

### B2 — Paper initial balance hardcoded ✅
- **File:** `core/engine/forward_engine.py`
- **Issue:** `PAPER_INITIAL_CAPITAL = 10_000.0` was used in `initialize()` to reconstruct per-broker paper balance, ignoring `settings.paper_initial_balance` from config.  Paper balance is always reconstructed as `PAPER_INITIAL_CAPITAL + realised_pnl`, making the config setting non-functional.
- **Fix:** Replaced `PAPER_INITIAL_CAPITAL` with `settings.paper_initial_balance` in `initialize()`. The old constant is removed. Balance fallback in `process_signal()` also updated.

---

### B3 — Signal dedup lacks DB-level lock ✅
- **File:** `tasks/signal_runner.py`, `api/routes/forward_test.py`
- **Issue:** Dedup check is SELECT + INSERT with no DB lock. Two concurrent Celery workers can both pass the SELECT and insert duplicate signals for the same (symbol, strategy, timeframe, candle).
- **Fix:** Added `pg_advisory_xact_lock()` call (keyed on a hash of `strategy:symbol:timeframe`) before the dedup SELECT in both files. Transaction-scoped lock is released automatically; no timeout risk.

---

### B4 — `_compute_features` duplicated between trainer and scorer ✅
- **File:** `models/trainer.py`, `core/ml_scorer.py`
- **Issue:** Both files independently define identical 6-feature computation (RSI-14, MACD-hist, ATR-norm, vol-ratio, BB-pct, log-ret). If one is updated, the other drifts — model trains on different features than those used at inference time.
- **Fix:** Extracted into `core/features.py` with a single `compute_features(df)` function. Both files now import from that module.

---

### B5 — Outcome resolver two-session race condition ✅
- **File:** `tasks/outcome_resolver.py`
- **Issue:** Pending outcomes are loaded in session 1, then resolved in session 2 per-group. Two concurrent resolver runs (e.g. Celery beat + manual trigger) load the same rows in session 1 and then both resolve them, producing duplicate resolution updates.
- **Fix:** Added `with_for_update(skip_locked=True)` to the pending outcomes query so concurrent runs skip rows already locked by another session. Resolution now commits within the same session as the lock.

---

### B6 — `/ws/kline` WebSocket has no authentication ✅
- **File:** `api/routes/kline_ws.py`
- **Issue:** The candlestick WebSocket at `/ws/kline` accepts all connections without any JWT check. Any unauthenticated client (or script) can connect and receive live market data.
- **Fix:** Added JWT validation from cookie or query-param `token` before `websocket.accept()`. Returns close code 4001 on auth failure.

---

### B7 — ML endpoints allow any authenticated user ✅
- **File:** `api/routes/ml.py`
- **Issue:** `/api/ml/resolve` and `/api/ml/retrain` endpoints require only login, not admin role. Any user could trigger a full DB outcome resolution or model retrain.
- **Fix:** Added `Depends(require_admin)` to both endpoints.

---

### B8 — reconcile_positions + monitor_sl_tp runs N times per tick ✅
- **File:** `api/routes/forward_test.py` → `_run_one_strategy()`
- **Issue:** `_run_one_strategy` calls `reconcile_positions` and `monitor_sl_tp` on its own `ForwardEngine` instance, but `_run_signals_background` already calls it once per strategy — resulting in N reconcile+monitor cycles (one per strategy) each scheduler tick. With 5 strategies = 5× broker API calls per tick.
- **Fix:** `_run_signals_background` now runs reconcile+monitor once before spawning per-strategy tasks. Removed per-strategy reconcile/monitor from `_run_one_strategy` (the heartbeat in `main.py` still covers the continuous SL/TP polling).

---

## Gaps

### G1 — No same-direction duplicate position guard ✅
- **File:** `core/engine/forward_engine.py` → `process_signal()`
- **Issue:** A second BUY signal on the same symbol opens a second long while one is already OPEN — pyramiding allowed by default with no opt-in.
- **Fix:** In the existing-position loop (where reversal logic lives), a same-direction existing trade now causes early return with a log `"Same-direction duplicate blocked"`. Applies to both paper and live.

---

### G2 — Emergency stop only closes paper trades ✅
- **File:** `core/engine/forward_engine.py` → `emergency_stop()`
- **Issue:** `emergency_stop()` filtered `Trade.is_paper == True`, leaving all live IBKR/Alpaca/Binance positions untouched.
- **Fix:** Emergency stop now closes ALL open trades (both paper and live), with a WS broadcast and notification distinguishing live vs paper. Live trades go through `close_position()` same as paper.

---

### G3 — Outcomes with NULL signal_id are unresolvable ✅
- **File:** `tasks/outcome_resolver.py`
- **Issue:** If `signal_id` is NULL (e.g. orphaned `TradeOutcome` created without a parent signal), the resolver cannot look up a `broker_name` and uses a fallback heuristic (`"/" → binance`, else `alpaca`), which may be wrong for IBKR outcomes.
- **Fix:** Added a lookup chain: (1) signal's broker, (2) `TradeOutcome.broker` column if populated, (3) symbol-based heuristic. Also populates `TradeOutcome.broker` on creation so future resolutions have an explicit broker.

---

### G4 — Resolution horizon fixed at 24 regardless of timeframe ✅
- **File:** `tasks/outcome_resolver.py`
- **Issue:** `RESOLUTION_HORIZON = 24` candles is always used. For a 1d strategy, 24 daily candles = ~a month of forward look. For 1h, it's just one day.
- **Fix:** Added `_HORIZON_BY_TF` dict mapping timeframe → horizon. Used `_HORIZON_BY_TF.get(timeframe, RESOLUTION_HORIZON)` in the resolution call.

---

### G5 — No IBKR minimum lot pre-check ✅
- **File:** `core/engine/forward_engine.py` → `process_signal()`
- **Issue:** IBKR enforces a 25,000 base-currency minimum for forex. Small or loss-depleted accounts produce sub-minimum sizes that get rejected by IBKR silently (logged as FAILED with no explanation).
- **Fix:** Before placing an IBKR forex order, validate `effective_size >= 25_000`. If below, reject with `logger.warning` and `return None`.

---

### G6 — Login rate limiting is in-process only ✅
- **File:** `api/routes/auth.py`
- **Issue:** `_login_attempts` is a plain `dict[str, list[float]]`. With multiple Uvicorn workers each has its own dict, so the effective limit is `N_workers × max_attempts`. Also, failed IPs accumulate in memory forever.
- **Fix:** Rate limiting now uses Redis with key `login_fail:{ip}` and a 15-minute TTL expiry. Falls back to in-memory if Redis is unavailable.

---

### G7 — No index on `signals.created_at` ✅
- **File:** `db/models.py`, new Alembic migration
- **Issue:** Dedup queries filter on `SignalModel.created_at >= _cutoff`. Without an index this is a full table scan on every signal check. With thousands of signals this becomes noticeable.
- **Fix:** Added `index=True` to `Signal.created_at` mapping. Database migration creates `ix_signals_created_at`.

---

### G8 — ML trains on daily OHLCV regardless of strategy timeframe ✅
- **File:** `models/trainer.py`, `core/ml_scorer.py`, `core/strategies/hybrid.py`
- **Issue:** `_fetch_ohlcv(symbol, days=365)` always fetched daily bars from yfinance. A 1h strategy's ML model was trained on daily RSI/MACD — fundamentally different granularity than what the strategy evaluates.
- **Fix:**
  - Added `_TF_PROFILE` dict in `trainer.py` mapping each timeframe to `(yf_interval, history_days, label_horizon_candles, min_rows)`. Examples: `1h → ("60m", 730 days, 24-candle horizon)`, `1d → ("1d", 365 days, 10-candle horizon)`, `1w → ("1wk", 1825 days)`.
  - `_fetch_ohlcv` now accepts `interval` and optional `resample_to` (2h/4h are fetched as 1h then resampled since yfinance lacks native support).
  - `retrain_all` collects `timeframe` from each strategy's parameters and deduplicates by `(symbol, timeframe)` — same symbol on 1h and 4h trains two separate models.
  - Model files are named `BTC_USDT_1h_2026-03-10.pkl`; `latest.json` key is `"BTC/USDT:1h"`.
  - `MLScorer.predict_proba` accepts `timeframe` param and looks up the TF-specific model first, falling back to legacy symbol-only key for backward compatibility.
  - `HybridStrategy` now passes `timeframe=timeframe` to `predict_proba`.

---

### G9 — Old .pkl model files accumulate without cleanup ✅
- **File:** `models/trainer.py` → `train_symbol()`
- **Issue:** Each retrain saves `<symbol>_<YYYY-MM-DD>.pkl`. After weeks of weekly retrains, the `models/saved/` directory accumulates unbounded files.
- **Fix:** After saving the new model, previous `.pkl` files for the same symbol (different date) are deleted. `latest.json` registry remains the canonical reference.

---

### G10 — Raw `ALTER TABLE` in `database.py` bypasses Alembic ✅
- **File:** `db/database.py`, new Alembic migration
- **Issue:** `init_db()` runs raw `ALTER TABLE trades ADD COLUMN IF NOT EXISTS notes VARCHAR(500)` and `ALTER TABLE signals ADD COLUMN IF NOT EXISTS dismissed BOOLEAN DEFAULT FALSE`. These changes are invisible to Alembic's version chain.
- **Fix:** Proper migration created. Raw `ALTER TABLE` statements removed from `init_db()`.

---

### G11 — No notification when trailing stop ratchets ✅
- **File:** `core/engine/forward_engine.py` → `monitor_sl_tp()`
- **Issue:** When `monitor_sl_tp` ratchets a trailing stop upward, only a `logger.info` is emitted. The frontend and user have no real-time visibility.
- **Fix:** Added WS broadcast (`ws_manager.broadcast("trailing_stop_moved", {...})`) and in-app notification dispatch when the stop is moved.

---

### G12 — IBKR EU stocks hardcoded to ~20 tickers ✅
- **File:** `brokers/ibkr_client.py`, **new** `config/ibkr_eu_stocks.json`
- **Issue:** `_EU_STOCKS` dict was hardcoded in Python source. Any unlisted EU stock fell through to `SMART/USD`, which IBKR rejects. Adding a new ticker required a code change and container rebuild.
- **Fix:** Extracted the routing map to `config/ibkr_eu_stocks.json` (expanded from 20 → 46 tickers, adding Xetra, Euronext Paris/Amsterdam, LSE, SWX, Madrid BM, Milan MIL). `ibkr_client.py` loads the file at startup via `_load_eu_stocks()` with a warning + empty-dict fallback if the file is missing. New EU tickers can now be added by editing the JSON file — no code change or rebuild needed.

---

## Improvements

### I1 — RegimeClassifier EMA slope window too short ✅
- **File:** `core/regime_classifier.py`
- **Issue:** `EMA_SLOPE_PERIODS = 3` computes trend slope over only 3 candles — extremely noisy, causing frequent regime flip-flops between BULL/BEAR/SIDEWAYS.
- **Fix:** Raised `EMA_SLOPE_PERIODS` to `15` for smoother, more stable regime classification.

---

### I2 — `Signal.created_at` typed Optional unnecessarily ✅
- **File:** `db/models.py`
- **Issue:** `created_at: Mapped[Optional[datetime]]` — column has a server default and is never NULL in practice. Optional type causes unnecessary null-checks downstream.
- **Fix:** Changed to `Mapped[datetime]` with `nullable=False`. Migration enforces NOT NULL.

---

### I3 — Run Now executes strategies sequentially ✅
- **File:** `api/routes/forward_test.py` → `_run_signals_background()`
- **Issue:** `for strat in strategies: await _run_one_strategy(strat)` — slow IBKR calls block all subsequent strategies. A 5-strategy run could take minutes if IBKR hangs.
- **Fix:** Changed to `await asyncio.gather(*tasks, return_exceptions=True)` using per-strategy locks that already exist (`_get_strategy_lock`). Strategies run concurrently.

---

### I4 — (Duplicate of B2) — see B2

---

### I5 — `_login_attempts` dict never evicts stale IPs ✅
- **File:** `api/routes/auth.py`
- **Issue:** Failed login IPs accumulate in `_login_attempts` forever (only trimmed on next access by the same IP). Long-running servers accumulate memory.
- **Fix:** Fixed as part of G6 — Redis TTL auto-expires entries after 15 minutes.

---

### I6 — Outcome resolver re-fetches OHLCV per outcome (same symbol) ✅
- **File:** `tasks/outcome_resolver.py`
- **Issue:** `_fetch_ohlcv_broker` is called once per outcome row, even when multiple outcomes share the same `(broker, symbol, timeframe)`. For 100 pending BTC/USDT outcomes = 100 identical API calls.
- **Fix:** Groups outcomes by `(symbol, timeframe, broker_name)` before fetching (already partially done). Verified that OHLCV is fetched once per group, not once per outcome.

---

### I7 — No alert when ML retrain fails AUC gate ✅
- **File:** `models/trainer.py` → `train_symbol()`
- **Issue:** When `holdout_auc < MIN_AUC`, only `logger.warning` fires. Operator has to check logs to know a model was rejected.
- **Fix:** Added in-app notification dispatch for rejected models via `notifications.notifier.dispatch()`.

---

### I8 — Celery `run_signals` fires every 5 min for daily strategies — no benefit to fix
- **File:** `celery_app.py`
- **Issue:** Celery beat runs `run_signals` every 5 minutes even for daily strategies.
- **Status:** No action needed. `signal_runner.py` line 185 does a single `_last_candle_fired.get(strat.id, 0) >= _last_close_ts` integer comparison per tick and hits `continue` immediately — no OHLCV fetch, no broker call, no DB query. The only "waste" is a dict lookup 287 times per day per daily strategy. There is no measurable CPU or network cost, and no benefit to adding per-strategy scheduling complexity.

---

### I9 — `Trade.user_id` not propagated from Signal ✅
- **File:** `core/engine/forward_engine.py` → `process_signal()`
- **Issue:** When creating a `Trade` record, `user_id` is not copied from `signal.user_id`. Impossible to audit which user triggered which live trade.
- **Fix:** Added `user_id=getattr(signal, "user_id", None)` to the `Trade(...)` constructor call.

---

## Migration Reference

**New migration:** `n4o5p6q7r8s9_audit2_schema_fixes.py`
- Creates index `ix_signals_created_at` on `signals(created_at)` (G7)
- Adds `trades.notes VARCHAR(500) NULL` (G10)
- Adds `signals.dismissed BOOLEAN NOT NULL DEFAULT FALSE` (G10)
- Changes `signals.created_at` to `NOT NULL` (I2)

**Previous head:** `l2m3n4o5p6q7_add_trailing_stop_to_trades`  
**New head:** `n4o5p6q7r8s9_audit2_schema_fixes`

---

## File Change Map

| File | Changes |
|------|---------|
| `core/features.py` | **NEW** — shared `compute_features()` |
| `core/engine/forward_engine.py` | B2, G1, G2, G5, G11, I9 |
| `core/ml_scorer.py` | B4, G8 — shared features + timeframe-aware model lookup |
| `config/ibkr_eu_stocks.json` | **NEW** — G12 EU stock routing config (46 tickers) |
| `core/regime_classifier.py` | I1 — EMA window 3→15 |
| `models/trainer.py` | B4, G8, G9, I7 |
| `tasks/outcome_resolver.py` | B5, G3, G4, I6 |
| `tasks/signal_runner.py` | B3 — advisory lock |
| `api/routes/forward_test.py` | B3, B8, I3 |
| `brokers/ibkr_client.py` | G12 — loads EU stocks from JSON config |
| `core/strategies/hybrid.py` | G8 — passes timeframe to predict_proba |
| `api/routes/kline_ws.py` | B6 — JWT auth |
| `api/routes/ml.py` | B7 — require_admin |
| `api/routes/auth.py` | G6, I5 — Redis rate limit |
| `db/models.py` | G7 — index; I2 — non-optional datetime |
| `db/database.py` | G10 — remove raw ALTER TABLE |
| `alembic/versions/n4o5p6q7r8s9_...py` | **NEW** — migration |
