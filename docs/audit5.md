# System Audit 5 — March 2026

Full codebase audit sweep #2, covering all major engine, risk, broker, task, and API files.
Every finding is documented with ID, severity, root cause, exact file:line reference, and planned fix.
Items are marked **✅ FIXED** when patched and **⏳ PENDING** until then.

---

## Summary Table

| ID | Type | File | Severity | Description | Status |
|---|---|---|---|---|---|
| BUG-1 | 🔴 Bug | `brokers/binance_client.py` ~line 274 | 🔴 High | Bracket TP order fails silently — SL locks full base asset first | ✅ FIXED |
| BUG-2 | 🔴 Bug | `core/risk_manager.py:322` | 🟡 Medium | R:R minimum uses hardcoded `1.5` instead of `self.default_rr_ratio` | ✅ FIXED |
| BUG-3 | 🔴 Bug | `core/risk_manager.py:237` | 🔴 High | Per-broker daily loss trips the global (cross-broker) circuit breaker | ✅ FIXED |
| BUG-4 | 🔴 Bug | `core/risk_manager.py:392` | 🟡 Medium | `record_outcome()` uses global consecutive-loss threshold; per-broker overrides ignored | ✅ FIXED |
| BUG-5 | 🔴 Bug | `frontend/src/pages/ChartPanel.tsx` | 🔴 High | FILLED trade exit marker not drawn when `exit_time > last candle` — trade #35/#32 | ✅ FIXED |
| GAP-1 | 🟡 Gap | `tasks/signal_runner.py:19` | 🟡 Medium | `_HIGHER_TF` missing 5 timeframes → multi-TF confluence bypassed for those TFs | ✅ FIXED |
| GAP-2 | 🟡 Gap | `celery_app.py:30` | 🟡 Medium | Celery beat `schedule: 300` (5 min) skips 80 % of 1m/3m live strategy candles | ✅ FIXED |
| GAP-3 | 🟡 Gap | `core/risk_manager.py:374` | 🟡 Medium | Any broker win resets the global consecutive-loss counter, masking another broker's losing streak | ✅ FIXED |
| GAP-4 | 🟡 Gap | `brokers/alpaca_client.py` | 🟡 Medium | All `run_in_executor` calls have no timeout — can block the event loop indefinitely | ✅ FIXED |
| GAP-5 | 🟢 Low | `api/routes/forward_test.py:213` | 🟢 Low | Paper balance shows $0.00 when all brokers are disconnected | ✅ FIXED |
| GAP-6 | 🟢 Low | `main.py` | 🟢 Low | `ForwardEngine` not a singleton — heartbeat and scheduler create separate instances | ✅ FIXED |
| GAP-7 | 🟢 Low | `core/engine/forward_engine.py:1070` | 🟢 Low | `monitor_sl_tp` fetches prices sequentially per trade when stream is down | ✅ FIXED |

---

### BUG-5 — FILLED trade exit marker missing on chart when trade closed after last candle

**File:** `frontend/src/pages/ChartPanel.tsx` in `applyMarkersImpl()`
**Severity:** 🔴 High (visible to user — reported for trade #35 and #32)

**Root cause:**
The exit circle check in `applyMarkersImpl()` required `t.exit_time <= maxTime`:
```javascript
if (t.exit_time && t.exit_price && t.exit_time >= minTime && t.exit_time <= maxTime) {
  tradeMarkers.push({ time: t.exit_time as any, ... })
}
```
`maxTime` is the timestamp of the **last candle in the current chart view**. When a trade closes after that candle (e.g. the chart was last loaded 30 minutes ago and the SL/TP fired 5 minutes ago), `exit_time > maxTime` and the condition is false — the exit circle is silently dropped.

Meanwhile the entry marker shows the PnL tag (`+0.4%` visible on `[P] BUY #35 +0.4%`) because the entry check correctly reads `pnl_pct` from the trade row regardless — giving the confusing appearance of a trade that has a PnL but no exit point.

The entry marker already has **snap-to-last-candle** logic:
```javascript
const markerTime = Math.min(t.entry_time, maxTime)
```
The exit marker lacked the equivalent, so it was dropped instead of snapped.

**Fix:**
Remove `&& t.exit_time <= maxTime` from the condition and snap `t.exit_time` to `maxTime` when it falls beyond the visible range:
```javascript
if (t.exit_time && t.exit_price && t.exit_time >= minTime) {
  tradeMarkers.push({ time: Math.min(t.exit_time, maxTime) as any, ... })
}
```

---

## Detailed Findings

---

### BUG-1 — Binance bracket TP order fails silently

**File:** `backend/brokers/binance_client.py` ~line 274
**Severity:** 🔴 High

**Root cause:**
When both `stop_price` and `take_profit_price` are set (bracket order), the code places:
1. Market BUY entry → base asset received into free balance
2. `create_order("STOP_LOSS", exit_side, quantity)` → **locks the full base asset as reserved collateral**
3. `create_limit_order(exit_side, quantity, take_profit_price)` → **fails** — `free = 0.0`, everything is locked by the SL order above

Binance's spot API locks the asset when a sell-side STOP_LOSS order is placed, leaving nothing for the TP limit sell. The failure is caught by a broad `except Exception` that logs at ERROR level and continues — so execution returns an `OrderResult` as if everything succeeded. The position ends up with SL-only broker protection; the TP is silently dropped.

**Planned fix:**
Reverse the order: place the **TP limit order first** (spot limit sells do not lock the base asset as reserved), then place the **STOP_LOSS order second**. Alternatively, use a Binance OCO (One-Cancels-Other) order which handles both legs atomically.

---

### BUG-2 — R:R check uses hardcoded 1.5 instead of config value

**File:** `backend/core/risk_manager.py` line 322
**Severity:** 🟡 Medium

**Root cause:**
```python
# risk_manager.py __init__ — loads config correctly:
self.default_rr_ratio = settings.default_rr_ratio  # default 2.0, user-configurable

# risk_manager.py validate() — but then never uses it:
if rr < 1.5:   # ← hardcoded; self.default_rr_ratio is dead code
    ...
```
`self.default_rr_ratio` is loaded from `settings` but the `validate()` method always compares against `1.5`. Any user change to `default_rr_ratio` in `config.py` or the DB has no effect.

**Planned fix (one line):**
```python
if rr < self.default_rr_ratio:
```

---

### BUG-3 — Per-broker daily loss trips global circuit breaker

**File:** `backend/core/risk_manager.py` lines 237–254
**Severity:** 🔴 High

**Root cause:**
```python
# Inside the per-broker daily-loss check loop:
if daily_loss_pct <= -_daily_cb_pct:
    self._circuit_breaker_active = True   # ← WRONG: trips ALL brokers
    ...
    b["circuit_breaker_active"] = True    # per-broker is also set, but too late
```
The comment at that line says *"Also trip the per-broker CB so only this broker halts"* — but the implementation directly contradicts this: `self._circuit_breaker_active` is the **portfolio-wide** flag checked by `validate()` for every broker. A single Binance loss breach halts Alpaca and IBKR too, even if those brokers are profitable. The cross-broker (portfolio-level) daily loss check is separate and correctly implemented a few lines below.

**Planned fix:**
Remove `self._circuit_breaker_active = True` from the per-broker branch entirely. Only set `b["circuit_breaker_active"] = True` inside the per-broker block. Leave the existing separate portfolio-wide daily-loss check to handle `self._circuit_breaker_active`.

---

### BUG-4 — `record_outcome()` ignores per-broker consecutive-loss threshold

**File:** `backend/core/risk_manager.py` lines 392–418
**Severity:** 🟡 Medium

**Root cause:**
```python
# record_outcome() — always uses the global threshold:
if s["consecutive_losses"] >= self.max_consecutive_losses:
    s["circuit_breaker_active"] = True
```
`validate()` correctly looks up broker-specific `max_consecutive_losses` from `broker_risk_settings`:
```python
_max_consec = int(bs["max_consecutive_losses"]) if bs.get("max_consecutive_losses") is not None \
              else self.max_consecutive_losses
```
But `record_outcome()` — which actually **trips** the per-broker CB after losses — always uses `self.max_consecutive_losses` (global) regardless of what is stored in `broker_risk_settings`. A broker configured with a lower threshold (e.g. max 3 losses) will never trip its CB until the global threshold (default 5) is hit.

**Planned fix:**
Replicate `validate()`'s existing lookup logic inside `record_outcome()` before the threshold comparison.

---

### GAP-1 — `_HIGHER_TF` missing 5 timeframes → confluence bypassed

**File:** `backend/tasks/signal_runner.py` lines 19–27
**Severity:** 🟡 Medium

**Root cause:**
```python
_HIGHER_TF = {
    "1m":  ["5m", "1h"],
    "5m":  ["1h", "4h"],
    "15m": ["1h", "4h"],
    "1h":  ["4h", "1d"],
    "4h":  ["1d", "1w"],
    "1d":  [],
    "1w":  [],
    # MISSING: "3m", "30m", "2h", "6h", "12h"
}
```
For any strategy running on `3m`, `30m`, `2h`, `6h`, or `12h` timeframes, `_HIGHER_TF.get(primary_tf, [])` returns `[]`. An empty list causes `_confluence_score()` to return `1.0` (no higher-TF disagreement to detect), so the multi-TF confluence gate always passes — confluence filtering is silently disabled for these five timeframes.

**Planned fix:**
```python
"3m":  ["15m", "1h"],
"30m": ["4h", "1d"],
"2h":  ["1d", "1w"],
"6h":  ["1d", "1w"],
"12h": ["1d", "1w"],
```

---

### GAP-2 — Celery beat 5-minute schedule skips 80% of 1m candles

**File:** `backend/celery_app.py` line 30
**Severity:** 🟡 Medium

**Root cause:**
```python
"schedule": 300,   # fires every 5 minutes
```
A live strategy running on a 1-minute timeframe produces a new actionable signal every 60 seconds. The Celery beat trigger firing every 300 seconds means the engine only processes 1 out of every 5 candles — **80% of signals are permanently skipped** for live strategies.

Paper strategies are unaffected: they are driven by the in-process `_forward_test_scheduler` which loops every 60 seconds. Only live signals (Celery task path) are impacted.

The `run_signals` task already has candle-dedup logic (skips a strategy if the last candle timestamp hasn't advanced since the prior run), so changing to 60 s is safe and won't cause double-processing.

**Planned fix:**
```python
"schedule": 60,   # every 60 seconds
```

---

### GAP-3 — Global consecutive-loss counter cross-contaminates brokers

**File:** `backend/core/risk_manager.py` lines 374–382
**Severity:** 🟡 Medium

**Root cause:**
```python
def record_outcome(self, won: bool, ...):
    if won:
        self.consecutive_losses = 0   # ← resets GLOBAL counter
    else:
        self.consecutive_losses += 1
```
`self.consecutive_losses` is a single portfolio-wide counter. If Alpaca wins a trade, it resets the counter to 0 — even when Binance is on a 4-loss streak. The streak counter that feeds `validate()` and the global circuit breaker is effectively meaningless in a multi-broker setup, since any win on any broker resets it.

**Planned fix:**
The cleanest approach is to rely solely on per-broker and per-strategy counters (both already tracked in `_per_broker[b]["consecutive_losses"]` and `_per_strategy[s]["consecutive_losses"]`) and remove the global `self.consecutive_losses` field, or at minimum not reset it when the winning trade is on a different broker than the one that last lost.

---

### GAP-4 — Alpaca `run_in_executor` calls have no timeout

**File:** `backend/brokers/alpaca_client.py` (multiple methods)
**Severity:** 🟡 Medium

**Root cause:**
All Alpaca SDK calls use:
```python
result = await loop.run_in_executor(None, lambda: self._client.some_call())
```
`asyncio.run_in_executor` with `None` executor and no timeout wraps the call in the default `ThreadPoolExecutor` with no cancellation mechanism. If the Alpaca SDK hangs (network issue, rate-limit, or SDK-level deadlock), the coroutine blocks forever — the thread pool fills up, and new trades on any broker that shares the event loop begin queuing indefinitely.

Affected methods: `get_price`, `get_balance`, `get_positions`, `place_order`, `cancel_order`.

**Planned fix:**
```python
result = await asyncio.wait_for(
    loop.run_in_executor(None, lambda: self._client.some_call()),
    timeout=10.0,
)
```

---

### GAP-5 — Paper balance shows $0 when all brokers disconnected

**File:** `backend/api/routes/forward_test.py` lines 213–218
**Severity:** 🟢 Low

**Root cause:**
```python
total_balance = sum(b["balance"] for b in broker_balances if b["connected"] and b["balance"])
```
When no broker is connected, `total_balance` evaluates to `0` and the dashboard shows `paper_balance: $0.00`. This is visually identical to a wiped account — it can cause unnecessary alarm and makes the dashboard misleading during broker outages (VPS restart, API key rotation, etc.).

**Planned fix:**
Fall back to `settings.paper_initial_balance` when no broker is connected:
```python
total_balance = sum(...) or _cfg.paper_initial_balance
```

---

### GAP-6 — `ForwardEngine` not a singleton

**File:** `backend/main.py`
**Severity:** 🟢 Low

**Root cause:**
`_sl_tp_heartbeat()` creates `ForwardEngine()` once at module level as a persistent instance. `_forward_test_scheduler` → `_run_one_strategy` creates a fresh `ForwardEngine()` per strategy call. These are separate objects with separate `_paper_positions` dicts. Both hydrate their state from the DB before each operation (so correctness is maintained), but:
- Both instances hold duplicate in-memory state
- Cache invalidation across instances can cause stale data
- If any future code path writes to `_paper_positions` in one instance, the other doesn't see it until DB hydration

**Planned fix:**
Add a module-level `get_forward_engine()` singleton factory (mirroring the existing `get_risk_manager()` pattern). Both heartbeat and scheduler call `get_forward_engine()` to get the shared instance.

---

### GAP-7 — Sequential REST price fetches in `monitor_sl_tp` when stream is down

**File:** `backend/core/engine/forward_engine.py` lines 1070–1095
**Severity:** 🟢 Low

**Root cause:**
```python
for trade in open_trades:
    ...
    elif _cache_key in _price_cache:
        bid_price, ask_price = _price_cache[_cache_key]
    else:
        await broker.connect()
        bid_price, ask_price = await broker.get_bid_ask(trade.symbol)  # sequential REST
```
When `price_stream_manager` has no cached price (WebSocket is down or reconnecting), the monitor falls back to one `await broker.get_bid_ask()` per trade, sequentially. With 20 open IBKR trades at ~300 ms REST latency each, a full heartbeat tick takes 6+ seconds — delaying SL/TP detection and blocking the asyncio loop.

**Planned fix:**
Collect unique `(broker, symbol)` pairs across all open trades, fire them in parallel with `asyncio.gather()`, populate a local price dict, then iterate trades using that dict.

---

## Fix Implementation Notes

| ID | File | Lines affected | Complexity |
|---|---|---|---|
| BUG-1 | `brokers/binance_client.py` | ~5 lines | Low — swap SL/TP order, or use OCO |
| BUG-2 | `core/risk_manager.py` | 1 line | Trivial |
| BUG-3 | `core/risk_manager.py` | ~3 lines | Low — remove one assignment |
| BUG-4 | `core/risk_manager.py` | ~8 lines | Low — copy validate() lookup |
| GAP-1 | `tasks/signal_runner.py` | 5 lines | Trivial |
| GAP-2 | `celery_app.py` | 1 line | Trivial |
| GAP-3 | `core/risk_manager.py` | ~10 lines | Medium — design change |
| GAP-4 | `brokers/alpaca_client.py` | ~10 lines | Low — wrap with wait_for |
| GAP-5 | `api/routes/forward_test.py` | 1 line | Trivial |
| GAP-6 | `main.py` + `core/engine/forward_engine.py` | ~20 lines | Low — singleton factory |
| GAP-7 | `core/engine/forward_engine.py` | ~30 lines | Medium — asyncio.gather refactor |

---

*All 12 findings (BUG-1..5, GAP-1..7) implemented and committed following this document.*
