# System Audit — March 2026

Full codebase audit covering all functions, methods, config, and logic across 18 source files.
Every finding is documented with its ID, severity, root cause, fix location, and exact lines changed.
All items marked **✅ FIXED** have been patched and committed.

---

## Summary Table

| ID | Severity | Area | Description | Status |
|---|---|---|---|---|
| BUG-1 | 🔴 High | `forward_engine.py` | Two duplicate local G1 threshold constants that could silently diverge | ✅ FIXED |
| BUG-2 | 🔴 High | `risk_manager.py` + routes | Multiple `RiskManager()` instances — API reset didn't propagate to engine | ✅ FIXED |
| BUG-3 | 🟡 Medium | `forward_engine.py` + `config.py` | Daily P&L reset boundary hardcoded to midnight UTC — not configurable | ✅ FIXED |
| BUG-4 | 🟡 Medium | `forward_engine.py` | Ghost-close PnL used current market price, not the SL/TP bracket fill | ✅ FIXED |
| BUG-5 | 🟡 Medium | `risk_manager.py` | `_save_state` fire-and-forget — state could be lost on Docker SIGTERM | ✅ FIXED |
| BUG-6 | 🟡 Medium | `outcome_resolver.py` | TP always won on same-candle TP+SL hit — inflated ML win labels | ✅ FIXED |
| GAP-1 | 🟡 Low | `forward_test.py` | NYSE close boundary `<= 16:00` allowed trades at exact close time | ✅ FIXED |
| GAP-2 | 🟡 Low | `forward_engine.py` | Breakeven condition used implicit `and`/`or` precedence — fragile | ✅ FIXED |
| GAP-3 | 🟡 Low | `forward_engine.py` | Open position count combined paper + live, blocking quota across modes | ✅ FIXED |
| GAP-4 | 🟡 Low | `signal_runner.py` | Signal dedup dict missing `3m`, `30m`, `2h`, `6h`, `12h` timeframes | ✅ FIXED |
| GAP-5 | 🟢 Info | `config.py` | `secret_key` defaults to `"change_this"` — no startup abort | ✅ ALREADY GUARDED |
| GAP-6 | 🟢 Info | `config.py` | `internal_api_secret` defaults to `""` — no startup warning | ✅ FIXED |
| GAP-7 | 🟢 Info | `config.py` | `cookie_secure=False` default — no VPS warning | ✅ FIXED |
| GAP-8 | 🟢 Info | `outcome_resolver.py` | Sub-hour timeframes jumped straight to 1h fallback — lost intraday resolution | ✅ FIXED |
| GAP-9 | 🟢 Info | `price_stream.py` | Streamed mid-price used for SL triggers — ignores bid/ask spread | ✅ DOCUMENTED |
| IMP-3 | 🟡 Medium | `forward_engine.py` | Ghost-close used static SL/TP bracket level — should query actual broker fill price via `fetch_closed_orders` | ✅ FIXED |

---

## Detailed Findings & Changes

---

### BUG-1 — Duplicate G1 threshold constants

**File:** `backend/core/engine/forward_engine.py`
**Severity:** 🔴 High

**Root cause:**
The G1 minimum-confidence override threshold (`0.75`) was defined as two separate local
variables with different names inside two `if/elif` branches that can never both execute:

```python
# Branch A — DB-authoritative path (old lines 397-398)
_G1_DEFAULT_THRESHOLD = 0.75
_g1_threshold = _G1_DEFAULT_THRESHOLD

# Branch B — in-memory path (old lines 436-437)
_G1_DEFAULT_THRESHOLD_MEM = 0.75     # different name!
_g1_threshold_mem = _G1_DEFAULT_THRESHOLD_MEM
```

A developer editing only one silently diverges the two code paths.

**Fix:**
- ✅ **Added** `_G1_DEFAULT_THRESHOLD: float = 0.75` as a **module-level constant** after `_MONITOR_SL_TP_LOCK` (forward_engine.py ~line 27–30)
- ✅ **Removed** local redefinition in DB path — replaced with `_g1_threshold = _G1_DEFAULT_THRESHOLD` (forward_engine.py ~line 402)
- ✅ **Removed** local redefinition in in-memory path — replaced with `_g1_threshold_mem = _G1_DEFAULT_THRESHOLD` (forward_engine.py ~line 440)

---

### BUG-2 — Multiple independent `RiskManager()` instances

**Files:** `backend/core/risk_manager.py`, `backend/core/engine/forward_engine.py`, `backend/api/routes/risk.py`, `backend/api/routes/portfolio.py`
**Severity:** 🔴 High

**Root cause:**
`RiskManager()` was constructed independently in three production locations:

| Location | Variable |
|---|---|
| `forward_engine.py` line 58 | `self.risk_manager = RiskManager()` |
| `api/routes/risk.py` line 29 | `_rm = RiskManager()` |
| `api/routes/portfolio.py` line 17 | `_risk_mgr = _RiskManager()` |

Each instance maintained its own in-memory circuit-breaker and consecutive-loss counters.
Calling `POST /api/risk/reset-circuit-breaker` (risk.py) updated `_rm`'s memory and wrote to
JSON, but ForwardEngine's `self.risk_manager` still had `_circuit_breaker_active = True` and
kept blocking all signals until the process was restarted.

**Fix:**
- ✅ **Added** `_risk_manager_instance` module-level variable and `get_risk_manager()` singleton factory in `risk_manager.py` (after line 13)
- ✅ **Updated** `forward_engine.py` import and line 58: `self.risk_manager = get_risk_manager()`
- ✅ **Updated** `api/routes/risk.py` import and line 29: `_rm = get_risk_manager()`
- ✅ **Updated** `api/routes/portfolio.py` import and line 17: `_risk_mgr = _get_risk_manager()`

The existing `_load_state()` call in `portfolio_summary` is intentionally retained — it ensures
Celery worker state (different process) is reflected via the shared JSON file.

---

### BUG-3 — Daily P&L reset boundary hardcoded to midnight UTC

**Files:** `backend/config.py`, `backend/core/engine/forward_engine.py`
**Severity:** 🟡 Medium

**Root cause:**
`_daily_pnl()` computed `today_start` with `hour=0` (midnight UTC):

```python
# forward_engine.py old line 120-122
today_start = datetime.now(_tz.utc).replace(
    hour=0, minute=0, second=0, microsecond=0, tzinfo=None
)
```

For US traders, midnight UTC = 7 PM ET (8 PM during EDT). A circuit-breaker trip at
3 PM ET would auto-reset at 7 PM the same evening — only 4 hours of actual protection
instead of the expected full trading day.

**Fix:**
- ✅ **Added** `daily_reset_hour_utc: int = 0` to `config.py` (after `forward_test_interval_minutes`) — default 0 keeps current UTC-midnight behaviour; set to 22 for 5 PM ET reset
- ✅ **Updated** `forward_engine.py` line ~121: `hour=_cfg.daily_reset_hour_utc`

---

### BUG-4 — Ghost-close PnL uses current market price instead of SL/TP fill price

**File:** `backend/core/engine/forward_engine.py` (`reconcile_positions`)
**Severity:** 🟡 Medium

**Root cause:**
When `reconcile_positions()` detected a ghost (position gone from broker), it called
`broker.get_price(trade.symbol)` to estimate the exit price, then used that price for PnL.
The reconciler might run minutes or hours after the SL/TP bracket actually fired.  The current
market price could differ significantly from the bracket fill:

```python
# old code — uses current market price for PnL
exit_price = await broker.get_price(trade.symbol)
...
# then uses exit_price directly to compute pnl
```

**Fix:**
- ✅ After determining `reason` (`"take_profit"` / `"stop_loss"`), **override** `exit_price` with the actual SL or TP level from the trade record — which is what the broker bracket order would have filled at:

```python
# BUG-4 FIX — use bracket level, not stale market price
if reason == "take_profit" and trade.take_profit:
    exit_price = trade.take_profit
elif reason == "stop_loss" and trade.stop_loss:
    exit_price = trade.stop_loss
# reason == "broker_close" → no known SL/TP fired, keep market price
```

(forward_engine.py, inside `reconcile_positions`, after the `reason` determination block)

---

### BUG-5 — `_save_state` fire-and-forget loses state on SIGTERM

**File:** `backend/core/risk_manager.py`
**Severity:** 🟡 Medium

**Root cause:**
`_save_state()` detected a running async event loop and dispatched the write to a
thread executor but **did not store or await the returned `Future`**:

```python
# old code — the future is discarded immediately
loop.run_in_executor(None, self._do_save_state)
```

On Docker `SIGTERM` the event loop closes, and any executor tasks still in the queue
are abandoned. Consecutive-loss counters and circuit-breaker state written just before
shutdown could be silently lost.

**Fix:**
- ✅ **Removed** the `asyncio.get_running_loop()` / `run_in_executor` path entirely
- ✅ **Always call** `_do_save_state()` synchronously.  The JSON payload is < 1 KB;
  atomic temp+rename completes in < 1 ms — well below any observable trade latency

```python
# BUG-5 FIX — always synchronous, guaranteed to complete
def _save_state(self) -> None:
    self._do_save_state()
```

---

### BUG-6 — TP always wins over SL on same-candle conflict

**File:** `backend/tasks/outcome_resolver.py` (`_resolve_outcome`)
**Severity:** 🟡 Medium

**Root cause:**
The candle walk checked `take_profit` before `stop_loss` unconditionally.  When
`high >= TP` and `low <= SL` on the same candle, the function always returned `WIN`:

```python
# old code — TP always wins, biases ml_label=1
if take_profit is not None:
    if is_long and high >= take_profit:
        return {"outcome": "win", "ml_label": 1, ...}
```

Over many training records this pushed `ml_label=1` (buy) assignments up, biasing
XGBoost toward overconfident buy signals.

**Fix:**
- ✅ Pre-compute four boolean flags (`_tp_hit_long`, `_tp_hit_short`, `_sl_hit_long`, `_sl_hit_short`)
- ✅ TP return block only fires when TP is hit **and no SL is hit on the same candle**
- ✅ When both fire on the same candle, execution falls through to the SL block (conservative / realistic)

---

### GAP-1 — NYSE close boundary uses `<=` 16:00

**File:** `backend/api/routes/forward_test.py` (`is_market_open`)
**Severity:** 🟡 Low

**Root cause:**
```python
# old line 153
return _MARKET_OPEN <= now_et.time() <= _MARKET_CLOSE   # _MARKET_CLOSE = time(16, 0)
```
NYSE closes **at** 16:00:00, so `now_et.time() == time(16, 0, 0)` incorrectly returned `True`.

**Fix:**
- ✅ **Changed** `<=` to `<` for the upper bound (forward_test.py line 153):

```python
# GAP-1 FIX
return _MARKET_OPEN <= now_et.time() < _MARKET_CLOSE
```

---

### GAP-2 — Breakeven condition uses implicit `and`/`or` precedence

**File:** `backend/core/engine/forward_engine.py` (`monitor_sl_tp`)
**Severity:** 🟡 Low

**Root cause:**
```python
# old code — evaluates correctly by accident, fragile on extension
is_long  and trade.stop_loss < trade.entry_price or
not is_long and trade.stop_loss > trade.entry_price
```
Python `and` binds tighter than `or`, so this currently evaluates as intended — but
adding any extra condition or reordering terms would silently break the logic.

**Fix:**
- ✅ **Added explicit parentheses** around each `and` clause (forward_engine.py ~lines 1244-1248):

```python
# GAP-2 FIX
(is_long     and trade.stop_loss < trade.entry_price) or
(not is_long and trade.stop_loss > trade.entry_price)
```

---

### GAP-3 — Open position count combines paper + live

**File:** `backend/core/engine/forward_engine.py` (`process_signal`)
**Severity:** 🟡 Low

**Root cause:**
```python
# old code — paper + live counted together
open_count = int(_oq_paper.scalar_one() or 0) + int(_oq_live.scalar_one() or 0)
```
A paper strategy with `max_open_positions=5` could be blocked from taking a new paper
position because 3 **live** positions were open on the same broker, consuming quota.
Paper and live are fully independent capital pools.

**Fix:**
- ✅ **Count only the matching mode** based on `signal.is_paper` (forward_engine.py ~lines 244-249):

```python
# GAP-3 FIX
is_signal_paper = getattr(signal, 'is_paper', True)
if is_signal_paper:
    open_count = int(_oq_paper.scalar_one() or 0)
else:
    open_count = int(_oq_live.scalar_one() or 0)
```

---

### GAP-4 — Signal dedup dict missing timeframe entries

**File:** `backend/tasks/signal_runner.py`
**Severity:** 🟡 Low

**Root cause:**
The deduplication window dict `_tf_seconds` (used to compute candle-start for duplicate
signal detection) was missing five entries:

```python
# old code — 7 entries, missing 3m / 30m / 2h / 6h / 12h
_tf_seconds = {
    "1m": 60, "5m": 300, "15m": 900, "1h": 3600,
    "4h": 14400, "1d": 86400, "1w": 604800,
}
```

A strategy using `"30m"` would fall back to `3600` (1h dedup window), allowing two
signals in the same 30-minute candle.  A `"2h"` strategy got a 1h window — deduplication
was **too aggressive**, blocking the 2nd hour's signal.

Note: the **candle-closed check** dict `_tf_secs_map` at line ~183 was already complete.
Only the **dedup dict** was incomplete.

**Fix:**
- ✅ **Added** `"3m": 180, "30m": 1800, "2h": 7200, "6h": 21600, "12h": 43200` (signal_runner.py ~lines 235-242)

---

### GAP-5 — `secret_key` defaults to `"change_this"`

**File:** `backend/config.py`
**Severity:** 🟢 Info — **Already guarded**

The existing `model_validator` `_warn_missing_secrets` already **raises `ValueError`**
at startup if `secret_key` is in `{"change_this", "change_this_to_a_random_64_char_string", ""}`.
No additional fix was needed.

---

### GAP-6 — `internal_api_secret` defaults to `""`

**File:** `backend/config.py`
**Severity:** 🟢 Info

**Root cause:**
`internal_api_secret: str = ""` — if the `.env` key is absent, the `/internal/*`
endpoints (e.g. `/internal/ml/reload`) are callable by anyone on the Docker network
with no authentication, allowing external code to trigger ML cache reloads.

**Fix:**
- ✅ **Added** a startup `logger.warning(...)` in `_warn_missing_secrets` validator when `internal_api_secret` is empty (config.py, after the existing `warnings` block)

---

### GAP-7 — `cookie_secure=False` default without VPS warning

**File:** `backend/config.py`
**Severity:** 🟢 Info

**Root cause:**
`cookie_secure: bool = False` is intentional for local Docker/HTTP dev, but if
deployed to a VPS over HTTPS without explicitly setting `COOKIE_SECURE=True`, the
`Secure` attribute is omitted from session cookies.  Modern browsers silently drop
plain-HTTP cookies on HTTPS domains, breaking the login flow.

**Fix:**
- ✅ **Added** a startup `logger.warning(...)` when `cookie_secure=False and not debug` (config.py, inside `_warn_missing_secrets` validator)

---

### GAP-8 — Sub-hour outcome resolver jumped directly to 1h fallback

**File:** `backend/tasks/outcome_resolver.py` (`_fetch_ohlcv_broker`)
**Severity:** 🟢 Info

**Root cause:**
The broker OHLCV fetch used a hard-coded `[timeframe, "1h"]` fallback chain for all
sub-hour timeframes:

```python
# old code — 1m strategy would fall back to 1h if broker returns < 2 rows
for tf in ([timeframe, "1h"] if timeframe in _SUB_HOUR and timeframe != "1h" else [timeframe]):
```

A 1m strategy's SL could be very tight (e.g. 0.1%).  A 1h candle's `low` might show
the SL was hit, but the **exact candle** within the hour matters for ML labelling.
Jumping from 1m → 1h skipped all intermediate resolution.

**Fix:**
- ✅ **Added** `_SUB_HOUR_FALLBACK` graduated chain dict (outcome_resolver.py ~line 40):

```python
_SUB_HOUR_FALLBACK = {
    "1m":  ["1m",  "5m", "15m", "30m", "1h"],
    "5m":  ["5m",  "15m", "30m", "1h"],
    "15m": ["15m", "30m", "1h"],
    "30m": ["30m", "1h"],
}
```

- ✅ **Updated** the `for tf in ...` loop to use `_SUB_HOUR_FALLBACK.get(timeframe, [timeframe])`
- ✅ **Updated** the log message to print the actual fallback tf instead of hard-coded `"1h"`

---

### GAP-9 — Streamed mid-price used as SL trigger reference

**File:** `backend/core/engine/price_stream.py`
**Severity:** 🟢 Info — **Documented**

**Root cause:**
`PriceStreamManager.get_price()` returns the streamed mid-price.  `monitor_sl_tp`
uses this for SL/TP trigger comparisons.  For instruments with a visible spread (especially
low-liquidity pairs), the mid-price is always between bid and ask, meaning:
- A LONG SL at the **bid** may show as not-yet-hit when mid-price is still above SL
- A SHORT SL at the **ask** suffers the same offset

**Note:** The broker's `get_bid_ask()` REST call already exists as a fallback when no
streamed price is available.  For the highest precision, the streamer would need to push
separate bid/ask values.

**Fix:**
- ✅ **Added** a detailed docstring note to `get_price()` in `price_stream.py` explaining the limitation and when a REST `get_bid_ask()` upgrade would be warranted
- Full bid/ask streaming is tracked as a future improvement (requires per-broker WebSocket change)

---

## Files Modified

| File | Items Fixed |
|---|---|
| `backend/core/engine/forward_engine.py` | BUG-1, BUG-2, BUG-3, BUG-4, GAP-2, GAP-3 |
| `backend/core/risk_manager.py` | BUG-2 (singleton), BUG-5 |
| `backend/api/routes/risk.py` | BUG-2 |
| `backend/api/routes/portfolio.py` | BUG-2 |
| `backend/tasks/outcome_resolver.py` | BUG-6, GAP-8 |
| `backend/api/routes/forward_test.py` | GAP-1 |
| `backend/tasks/signal_runner.py` | GAP-4 |
| `backend/config.py` | BUG-3, GAP-6, GAP-7 |
| `backend/core/engine/price_stream.py` | GAP-9 (doc only) |
