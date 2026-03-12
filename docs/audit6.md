# AI Bot Trader — Full System Audit (v2.5.0)

**Scope:** Every flow — transaction, notification, signal, execution, broker, strategy, ML, risk, scheduler, WebSocket, API.  
**Date:** 2025  
**System Version:** 2.5.0  

---

## Table of Contents

1. [Audit Summary](#1-audit-summary)
2. [Critical Bugs](#2-critical-bugs)
3. [High-Severity Bugs](#3-high-severity-bugs)
4. [Medium-Severity Bugs](#4-medium-severity-bugs)
5. [Low-Severity Bugs](#5-low-severity-bugs)
6. [Gaps — Missing Functionality](#6-gaps--missing-functionality)
7. [Improvements Recommended](#7-improvements-recommended)
8. [Per-System Verification](#8-per-system-verification)
   - [Transaction / Trade Flow](#81-transaction--trade-flow)
   - [Signal Flow](#82-signal-flow)
   - [Notification Flow](#83-notification-flow)
   - [Broker Compatibility](#84-broker-compatibility)
   - [Strategy Implementations](#85-strategy-implementations)
   - [Risk Management](#86-risk-management)
   - [ML Pipeline](#87-ml-pipeline)
   - [Schedulers & Celery Tasks](#88-schedulers--celery-tasks)
   - [WebSocket & Real-time](#89-websocket--real-time)
   - [API Routes & Auth](#810-api-routes--auth)
9. [Severity Index](#9-severity-index)

---

## 1. Audit Summary

| Category | Count |
|---|---|
| Critical bugs | 4 |
| High bugs | 3 |
| Medium bugs | 5 |
| Low bugs | 5 |
| Gaps | 8 |
| Improvements | 13 |

**Most critical path at risk:** Live Binance trading with bracket (SL/TP) orders — if TP placement fails after the entry fills, the position is left open at the exchange with zero protection while the bot records it as REJECTED.

**Second most critical:** Multi-leg paper options (iron condor) are incorrectly executed as naked equity shorts, not options trades.

**Third most critical:** Bull Call Spread strategy is dead code — the MACD condition always evaluates False due to wrong attribute access. It will never generate a BUY signal.

---

## 2. Critical Bugs

---

### BUG-CRIT-01 · Binance bracket — orphaned exchange position after partial fill/SL placement failure

**File:** `backend/brokers/binance_client.py` — `place_order()` bracket path  
**Severity:** CRITICAL — Live capital unprotected

**What happens:**  
1. Market entry order is submitted and fills successfully at the exchange.  
2. TP limit order is placed (up to 2 attempts).  
3. If TP creation fails on both attempts → `raise RuntimeError` is thrown.  
4. SL order is never placed (it comes after TP in the sequence).  
5. `ForwardEngine.process_signal()` catches the exception as `order_err`, marks the trade `REJECTED` in the DB, and returns the REJECTED trade object.  

**Result:** The exchange holds an OPEN live position. The bot believes the trade was rejected. No SL, no TP. The position grows or shrinks with no protection and no bot oversight.

**Fix:**  
1. When the TP/SL placement phase fails, place an emergency market exit order to close the entry before propagating the error.
2. If even the emergency exit fails, emit an EMERGENCY_STOP notification and halt further trading for that symbol.
3. Alternatively, use Binance's OCO (One-Cancels-the-Other) order type instead of sequential placements — this atomically binds TP and SL at order creation.

```python
# binance_client.py — emergency close on TP/SL failure
except RuntimeError as tp_err:
    logger.critical(f"[Binance] TP/SL placement failed for {symbol}: {tp_err} — closing position")
    try:
        await self.exchange.create_order(symbol, "market", close_side, qty)
    except Exception as emergency_err:
        logger.critical(f"[Binance] Emergency close FAILED: {emergency_err} — MANUAL INTERVENTION NEEDED")
    raise  # re-raise so ForwardEngine marks REJECTED (position is now flat)
```

---

### BUG-CRIT-02 · Multi-leg options (iron condor) in paper mode execute as a naked equity short

**File:** `backend/core/engine/forward_engine.py` — `process_signal()` ~line 630  
**Severity:** CRITICAL — Wrong trade placed

**What happens:**  
The guard that blocks multi-leg options in live mode is:
```python
elif len(legs) > 1:
    logger.warning(...)
    if not is_paper:
        return None   # only blocks live!
```
For `is_paper=True`, execution continues with `option_kwargs = {}` (empty). The engine then calls:
```python
broker.place_order(symbol, side="sell", quantity=..., order_type="market")
```
This is a plain market sell of the underlying with no options contract information. For IBKR paper, this sends a market short order on the stock, not an iron condor on the options chain.

**Impact:** Every paper iron condor trade silently becomes a naked equity short position.

**Fix:**
```python
elif len(legs) > 1:
    logger.warning(f"[ForwardEngine] Multi-leg option for {signal.symbol} — paper simulation only")
    if not is_paper:
        logger.error("[ForwardEngine] LIVE multi-leg option rejected — use paper mode")
        return None
    # Paper multi-leg: use simulated execution (no broker API call)
    trade.status = OrderStatus.OPEN
    trade.entry_price = signal.entry_price   # net premium
    trade.broker_order_id = f"paper_options_{signal.symbol}_{int(datetime.now(timezone.utc).timestamp())}"
    # skip broker.place_order() entirely for multi-leg paper
    if db_session:
        db_session.add(trade)
        await db_session.commit()
        await db_session.refresh(trade)
    return trade
```

---

### BUG-CRIT-03 · Options position sizing uses premium as denominator — produces astronomical contract counts

**Files:** `backend/core/engine/forward_engine.py` — `process_signal()`, `backend/core/risk_manager.py` — `validate()`  
**Severity:** CRITICAL — Financial risk model broken for options

**What happens:**  
`RiskManager.validate()` computes position size as:
```
position_size = (balance × risk_per_trade_pct%) / entry_price
```
For an iron condor with `entry_price = 2.50` (net premium) on a $10,000 account at 2% risk:
```
position_size = $10,000 × 0.02 / $2.50 = 80 "contracts"
```
But the actual risk per iron condor contract is not $2.50 — it is `spread_width × 100` per contract minus premium collected (e.g. a $5-wide condor has max loss of $500 per contract minus $250 collected = $250 net risk per contract). The formula produces 80 contracts when the account can only safely trade 1.

**Impact:** First paper iron condor trade attempted would try to place 80 contracts. IBKR paper accounts have margin limits that would immediately reject this, but if approved it would represent $20,000+ in nominal exposure from a $10,000 account.

**Fix:** For options signals, risk sizing must use max loss as the denominator:
```python
# In RiskManager.validate() or ForwardEngine.process_signal():
options_meta = getattr(signal, "options_meta", None)
if options_meta:
    # For debit spreads: max_loss = net_debit
    # For credit spreads: max_loss = spread_width - premium_received (stored in options_meta)
    max_loss = options_meta.get("max_loss") or signal.stop_loss or signal.entry_price
    position_size = (balance * risk_pct) / max_loss
else:
    position_size = (balance * risk_pct) / signal.entry_price
```

---

### BUG-CRIT-04 · Bull Call Spread MACD check always evaluates False — strategy is permanently dead

**File:** `backend/core/strategies/bull_call_spread.py` — `generate_signal()`  
**Severity:** CRITICAL — Strategy produces zero signals

**What happens:**  
```python
macd_out = self.macd_tool.calculate(data)
histogram   = getattr(macd_out, "histogram", None)    # → None (stored in metadata, not direct attr)
macd_line   = getattr(macd_out, "macd", None)         # → None (no .macd attribute on ToolOutput)
signal_line = getattr(macd_out, "signal", None)       # → "bullish_crossover" string (signal type, NOT price!)
```
The MACD `ToolOutput` stores `histogram`, `macd_line`, and `signal_line` in `.metadata`, not as direct attributes. Access via `getattr` returns `None` for histogram and macd_line.

Both conditions fail:
1. `histogram is not None and histogram > 0` → **False** (`histogram` is None)
2. `macd_line is not None and ...` → **False** (`macd_line` is None)

Result: `macd_bullish` is always `False`. Execution falls through to:
```python
if not macd_bullish:
    return _hold([f"MACD not bullish — MACD neutral"])
```
The strategy **always returns HOLD**. No BUY signal is ever generated.

**Fix:**
```python
macd_out = self.macd_tool.calculate(data)
# Option A — use ToolOutput.value which IS the current histogram value
histogram = macd_out.value   # float, stored correctly as .value

# Option B — use the signal string directly
macd_bullish = macd_out.signal in ("bullish_crossover", "histogram_positive")
macd_reason  = f"MACD {macd_out.signal}"
```

---

## 3. High-Severity Bugs

---

### BUG-HIGH-01 · Mean reversion trailing stop applied — exits before reaching TP

**File:** `backend/core/strategies/mean_reversion.py` — calls `self._enhance_signal(signal, data)`  
**File:** `backend/core/strategies/base.py` — `_enhance_signal()`  
**Severity:** HIGH — Premature exits, systemically lower win rate

**What happens:**  
`_enhance_signal()` always sets `trailing_stop_pct = 1.5 × ATR(14) / price` if not already set. On a mean-reversion trade (BUY at lower Bollinger Band, TP = mid-band):
- Price initially moves against the trade (it's reverting, so it may first continue down slightly)
- Trailing stop starts at entry × (1 - 1.5% ATR)  
- Any small up-move locks in a tight trail  
- The trail may trigger before the price completes the reversion to the mid-band  

Trailing stops are designed for trend-following — the exact opposite of mean reversion.

**Fix:**  
Mean reversion strategy should NOT call `_enhance_signal()`, or `_enhance_signal()` should accept a parameter to skip trailing stop:

```python
# mean_reversion.py — skip _enhance_signal for counter-trend trades
# Return the signal directly after confirmation candle check
if data["close"].iloc[-2] < data["open"].iloc[-2]:  # bearish confirmation
    signal.reasons.append("Bearish confirmation candle — BUY blocked")
    signal.signal = "HOLD"
return signal   # no _enhance_signal
```

Alternatively, add a `skip_trailing_stop=True` flag to `_enhance_signal()`.

---

### BUG-HIGH-02 · Iron condor outcome resolved as directional short — corrupt ML labels

**File:** `backend/tasks/outcome_resolver.py` — `_resolve_outcome()`  
**Severity:** HIGH — ML training data poisoning

**What happens:**  
`_resolve_outcome()` determines P&L direction from `signal_type`:
```python
is_long  = signal_type in ("BUY", "COVER")
is_short = signal_type in ("SELL", "SHORT")
```
Iron condor (and covered call) emit `signal="SELL"`. The resolver therefore treats these as directional short trades — it walks the underlying's candle data looking for the price to drop below SL or rise above TP, measuring P&L as `(entry_price - exit_price) / entry_price`.

But iron condor `entry_price = net_premium` ($2.50) and `stop_loss = max_loss` ($8.00). The "price" here is the *options premium*, not the underlying. If the SPY drops -5%, the underlying price falls, `is_short and low <= effective_stop` fires, and the resolver marks it a WIN — but the iron condor may have actually lost money due to IV expansion from the move.

**Fix:**  
Add an `is_options` flag or check `options_meta` in `_resolve_outcome()`:

```python
# In resolve_pending_outcomes, pass the signal's options_meta
options_meta = outcome.options_meta  # store this on TradeOutcome model
if options_meta:
    # Options P&L: compare premium collected to premium at exit
    # For theta-decay: resolved after DTE passes, check premium decay
    # For directional options: check underlying price movement
    ...
else:
    # Standard directional P&L
    is_long  = signal_type in ("BUY", "COVER")
    is_short = signal_type in ("SELL", "SHORT")
```

---

### BUG-HIGH-03 · `cleanup_stale_pending_trades` marks PENDING trades REJECTED without confirming broker status

**File:** `backend/core/engine/forward_engine.py` — `cleanup_stale_pending_trades()`  
**Severity:** HIGH — Orphaned live exchange positions

**What happens:**  
Any trade stuck in PENDING status for >30 minutes is auto-resolved as REJECTED:
```python
t.status = OrderStatus.REJECTED
t.notes = "auto-resolved as REJECTED after 30m pending timeout"
```
A real-world scenario: IBKR fills the order 25 minutes after placement (slow Paper TWS fills are common). The trade is legitimately OPEN at the exchange. Then `cleanup_stale_pending_trades` runs and marks it REJECTED. Now:
- The bot no longer tracks this position (removed from `_paper_positions`)
- `reconcile_positions` was already called before this function in each tick
- But reconcile uses a DB session that may not yet reflect the change  
- On the next tick, reconcile WILL see the broker has a position but DB has REJECTED → it will NOT create a new trade for REJECTED status (it only processes OPEN/PENDING)
- The exchange position is now an orphan

Note: in `signal_runner.py` the order is: `reconcile` → `monitor_sl_tp` → `cleanup_stale`. This means reconcile runs BEFORE cleanup. If the fill arrived before cleanup runs, reconcile should catch it. But there's a race in the first 30 minutes for slow-fill scenarios.

**Fix:**  
Before marking REJECTED, query the broker for the actual order status:
```python
for t in stale:
    # Check broker status before auto-rejecting
    try:
        broker = get_broker(t.broker.value, force_paper=t.is_paper)
        order = await broker.get_order_status(t.broker_order_id)
        if order and order.status in ("filled", "partial"):
            t.status = OrderStatus.OPEN
            t.entry_price = order.fill_price or t.entry_price
            logger.info(f"[ForwardEngine] IMP-31: PENDING {t.symbol} id={t.id} confirmed FILLED — upgraded to OPEN")
            continue
    except Exception:
        pass
    # Only mark REJECTED if broker confirms no fill
    t.status = OrderStatus.REJECTED
```

---

## 4. Medium-Severity Bugs

---

### BUG-MED-01 · Double OHLCV fetch per strategy per tick (rate-limit waste)

**File:** `backend/tasks/signal_runner.py` — regime router block + `signal_engine.run()`  
**Severity:** MEDIUM — Doubles API call rate, rate-limit risk

**What happens:**  
For every active strategy in every Celery tick:
1. Regime router fetches `broker.get_ohlcv(symbol, timeframe, limit=200)` to classify the regime.
2. `signal_engine.run()` is called with no `data` argument → fetches `broker.get_ohlcv()` again.

Two identical API calls per strategy per 60 seconds. For 5 strategies across 3 brokers = 30 API calls per minute that could be 15.

**Impact:** Binance testnet has aggressive rate limits. IBKR has pacing delays that add 300ms per call. Under load this doubles latency and doubles rejection risk.

**Fix:** Pass the cached OHLCV dataframe to `signal_engine.run()`:
```python
# After regime fetch in signal_runner.py:
_ohlcv = await _broker_obj.get_ohlcv(symbol, timeframe, limit=200)
# ... regime logic ...

# Pass cached data to signal_engine to avoid re-fetch
sig = await signal_engine.run(
    strategy_name=_active_strategy_type,
    symbol=symbol,
    broker_name=strat.broker.value,
    timeframe=timeframe,
    limit=limit,
    data=_ohlcv,            # ← pass cached frame
    asset_class=...,
)
```

---

### BUG-MED-02 · Hybrid strategy confirmation candle check is duplicated

**File:** `backend/core/strategies/hybrid.py` — `generate_signal()` (inline check) + `_enhance_signal()` in base  
**Severity:** MEDIUM — Logic redundancy, maintenance risk

**What happens:**  
`hybrid.py` checks the confirmation candle inline before calling `_enhance_signal()`. Then `base.py::_enhance_signal()` runs the same confirmation candle check again. Any future change to the check in one place will not automatically apply to the other, creating potential inconsistencies.

**Fix:** Remove the inline confirmation candle in `hybrid.py` and rely solely on `_enhance_signal()` for the check.

---

### BUG-MED-03 · Covered call `stop_loss` and `take_profit` use per-share notation, not per-contract (×100)

**File:** `backend/core/strategies/covered_call.py` — `generate_signal()`  
**Severity:** MEDIUM — Incorrect P&L calculation

**What happens:**  
```python
entry_price  = round(premium, 4)           # e.g. $0.85 per share
stop_loss    = round(premium * 3.0, 4)     # $2.55 per share
take_profit  = round(premium * 0.10, 4)   # $0.085 per share
```
Standard US options contracts are 100 shares each. One contract with $0.85 premium = $85 received, max loss = $255 per contract. The ForwardEngine computes position size as `qty = (balance × risk_pct) / entry_price = $10,000 × 0.02 / $0.85 = ~235 contracts`.

This is the same scaling bug as BUG-CRIT-03 — position sizing is completely wrong for options.

**Fix:** Store per-contract values, or annotate options signals with contract multiplier information used in position sizing.

---

### BUG-MED-04 · Bull Call Spread erroneously applies trailing stop via `_enhance_signal()`

**File:** `backend/core/strategies/bull_call_spread.py` — calls `_enhance_signal(signal, data)`  
**Severity:** MEDIUM — Wrong exit mechanism for options spread  
**Note:** Currently inert due to BUG-CRIT-04 (strategy is dead), but needs fixing when MACD bug is patched.

**What happens:**  
The code comment says "confirmation candle only (options — no trailing stop / S/R snap)" but `_enhance_signal()` unconditionally sets `trailing_stop_pct = 1.5 × ATR / price` when not already set. A bull call spread should only be exited at the options TP (75% of max profit) or by expiry, not by a trailing stop on the underlying price.

**Fix:** Either skip `_enhance_signal()` for options strategies, or guard trailing stop by asset class:
```python
# In base.py _enhance_signal():
is_options = getattr(signal, "asset_class", "") == "option" or getattr(signal, "options_meta", None) is not None
if not is_options and trailing_stop_pct is None and atr_val > 0:
    signal.trailing_stop_pct = round(1.5 * atr_val / current_price * 100, 4)
```

---

### BUG-MED-05 · Price streaming uses mid-price for SL/TP triggers (directional inaccuracy)

**File:** `backend/core/engine/price_stream.py` — `_stream_worker()`, `PriceStreamManager.get_price()`  
**File:** `backend/core/engine/forward_engine.py` — `_monitor_sl_tp_inner()` streaming branch  
**Severity:** MEDIUM — Late SL triggers on illiquid assets

**What happens:**  
When a streamed price is available, `monitor_sl_tp` uses it as both bid and ask:
```python
streamed = _psm.get_price(trade.symbol)
if streamed is not None:
    bid_price, ask_price = streamed, streamed   # mid-price used for both
```
For a LONG position, the SL should fire when the BID price falls below the SL level. The mid-price is above the bid by half the spread. On illiquid IBKR stocks with a $0.10–$0.50 spread, the SL may trigger 25–250ms later than it should.

The REST fallback correctly fetches actual bid/ask via `broker.get_bid_ask()`. The inconsistency creates variable SL precision depending on whether streaming is active.

**Fix:** Have broker stream workers emit bid/ask pairs, not just mid-price:  
```python
async def _on_price(symbol: str, bid: float, ask: float) -> None:
    self._bid_prices[symbol] = bid
    self._ask_prices[symbol] = ask

def get_bid_ask(self, symbol: str):
    return self._bid_prices.get(symbol), self._ask_prices.get(symbol)
```

---

## 5. Low-Severity Bugs

---

### BUG-LOW-01 · `cleanup_stale_pending_trades` uses deprecated `datetime.utcnow()`

**File:** `backend/core/engine/forward_engine.py` — `cleanup_stale_pending_trades()` line ~1147  
**Severity:** LOW — Python compatibility issue

**Code:**
```python
cutoff = datetime.utcnow() - timedelta(minutes=timeout_minutes)
```
Python 3.12 deprecates `datetime.utcnow()`. Every other method in `forward_engine.py` already uses the correct pattern `datetime.now(timezone.utc).replace(tzinfo=None)`.

**Fix:**
```python
cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=timeout_minutes)
```

---

### BUG-LOW-02 · Iron condor RSIcontrol inconsistency — no RSI filter applied

**File:** `backend/core/strategies/iron_condor.py`  
**Severity:** LOW — Missing confirmation filter

**What happens:**  
Iron condor requires IV Rank > 50 and ADX < 25 for a non-directional market. There is no RSI filter. An iron condor entered when RSI is at an extreme (< 25 or > 75) risks the underlying making a directional move that blows out one side of the condor. Covered call includes RSI 40–60 filter; iron condor does not.

**Fix:** Add RSI 35–65 filter to iron condor:
```python
rsi_out = self.rsi_tool.calculate(data)
if not (35.0 <= rsi_out.value <= 65.0):
    return _hold([f"RSI {rsi_out.value:.1f} too directional — wait for RSI 35–65"])
```

---

### BUG-LOW-03 · Orphan position sync in `reconcile_positions` only covers IBKR

**File:** `backend/core/engine/forward_engine.py` — `reconcile_positions()` orphan-sync block  
**Severity:** LOW — Alpaca/Binance orphan recovery missing

**What happens:**  
If `ForwardEngine.process_signal()` successfully places an order at Alpaca or Binance, but the DB `commit()` fails immediately after (disconnection, deadlock), the exchange holds a real position while the DB has nothing. On restart, `reconcile_positions` implements IBKR orphan detection but NOT for Alpaca or Binance.

**Fix:** Extend the orphan-sync block to check all 3 brokers, not just IBKR.

---

### BUG-LOW-04 · `_enhance_signal()` S/R TP snap overrides options take_profit

**File:** `backend/core/strategies/base.py` — `_enhance_signal()`  
**Severity:** LOW — Options TP corrupted (only matters when options call `_enhance_signal`)

**What happens:**  
`_enhance_signal()` snaps the TP price to the nearest support/resistance level found by `_find_resistance()`. For a bull call spread with `take_profit = net_debit + max_profit × 0.75 = $3.17`, the snap might change this to a nearby S/R level on the underlying (e.g. $310 for SPY), completely replacing the options-specific TP.

**Fix:** Skip S/R snap for options signals (same guard as BUG-MED-04):
```python
if not is_options and take_profit_snap is not None:
    signal.take_profit = take_profit_snap
```

---

### BUG-LOW-05 · `_daily_pnl` sums paper + live P&L — paper losses can contribute to live circuit breaker

**File:** `backend/core/engine/forward_engine.py` — `_daily_pnl()`  
**Severity:** LOW — Incorrect circuit breaker inputs

**What happens:**  
`_daily_pnl()` sums `Trade.pnl` (paper) + `LiveTrade.pnl` (live), filtered only by broker. When `process_signal` is evaluating a live trade, it calls `_daily_pnl(broker="alpaca")` which includes both paper and live PnL for Alpaca. A bad paper day (-$500) + a flat live day ($0) = -$500 daily PnL, potentially tripping the 5% daily live circuit breaker.

**Fix:** Pass `is_paper` to `_daily_pnl` and filter accordingly:
```python
async def _daily_pnl(db_session, broker=None, is_paper=None) -> float:
    # When is_paper=True: sum only Trade rows
    # When is_paper=False: sum only LiveTrade rows
    # When is_paper=None: sum both (current behavior, used for portfolio view)
```

---

## 6. Gaps — Missing Functionality

---

### GAP-01 · Live multi-leg options execution not supported

**Impact:** HIGH — Iron Condor, Bull Call Spread, Covered Call are paper-only strategies in practice  
**Detail:** Multi-leg options require sending a combo order to IBKR (BAG/ComboOrder). Single-leg covered call is implemented for single-leg (`option_expiry`, `option_strike`, `option_right` kwargs), but multi-leg is explicitly blocked in live mode. There is no IBKR BAG order construction, no leg management, no options chain data fetching.

**Recommendation:** Implement IBKR `ComboOrder` for multi-leg execution, or clearly document paper-only limitation and hide live enable for iron condor in the UI.

---

### GAP-02 · Binance bracket recovery mechanism missing

**Impact:** HIGH — Extends BUG-CRIT-01  
**Detail:** There is no recovery flow that periodically scans for exchange positions that have no matching DB record on Binance (equivalent to IBKR orphan sync). If the Binance bracket placement partially fails, there is no automatic reconciliation.

**Recommendation:** Add Binance orphan detection in `reconcile_positions()` using `exchange.fetch_balance()` to detect non-zero holdings not tracked in DB.

---

### GAP-03 · IBKR `stream_prices()` method not implemented

**Impact:** MEDIUM — SL/TP monitoring falls back to REST (300ms per call) instead of WebSocket  
**Detail:** `price_stream.py` calls `broker.stream_prices(symbols, callback)` on the IBKR client. This method is not found in the reviewed `ibkr_client.py` code. If not implemented, the `_stream_worker` raises `AttributeError` every 5 seconds, constantly reconnecting and logging errors, and SL/TP always uses the slower REST poll.

**Recommendation:** Implement `stream_prices()` in `IBKRClient` using `ib_insync`'s `reqMktData` with a persistent callback, or raise `NotImplementedError` with a clear message and ensure `monitor_sl_tp` REST fallback is the only path for IBKR.

---

### GAP-04 · No email notification for emergency stop

**Impact:** MEDIUM — Critical event missed if operator not watching dashboard  
**Detail:** `ForwardEngine.emergency_stop()` logs a warning and sends in-app notifications only. `notifier.trade()` (which sends email) is not called. An emergency stop during off-hours would not wake the operator.

**Recommendation:** Call `notifier.emergency()` or `notifier.trade()` with `send_email=True` at the start of `emergency_stop()`.

---

### GAP-05 · `next_monthly_expiry()` for options ignores exchange holidays

**Impact:** MEDIUM — Options may be assigned/expire on wrong date  
**Detail:** Options expiry is computed as the third Friday of the month. If that Friday is a US market holiday, options actually expire on Thursday. The current implementation has no holiday calendar check.

**Recommendation:**
```python
from pandas_market_calendars import get_calendar
nyse = get_calendar("NYSE")
# Check if expiry_date is a holiday and adjust to prior trading day
```

---

### GAP-06 · `TradeOutcome` model lacking `options_meta` field

**Impact:** MEDIUM — Options outcome resolution is blind to strategy type  
**Detail:** `TradeOutcome` (DB model) does not store `options_meta`. The outcome resolver has no way to distinguish a directional SELL (e.g. crypto short) from a premium-collection SELL (iron condor) without it. This reinforces the root cause of BUG-HIGH-02.

**Recommendation:** Add `options_meta = Column(JSON, nullable=True)` to `TradeOutcome` and populate it when creating the record in `signal_runner.py`.

---

### GAP-07 · No ML model freshness alerting

**Impact:** LOW — Stale model degrades signal quality silently  
**Detail:** The ML model is retrained weekly (Celery Sunday 02:00 UTC). If the retraining task fails silently (no AUC improvement, all symbols skip), the model continues running with old weights indefinitely. There is no `model_trained_at` timestamp check or stale-model alert.

**Recommendation:** Log and notify when model files are older than 14 days. MLScorer should emit a warning if `predict_proba()` is called with a model file older than `N` days.

---

### GAP-08 · Alpaca `_map_timeframe()` raises `ValueError` for "3d"

**Impact:** LOW — Hard crash if strategy configured with 3-day timeframe  
**Detail:** In `alpaca_client.py`:
```python
timeframe_map = {...}   # no "3d" entry
if tf not in timeframe_map:
    raise ValueError(f"Unsupported Alpaca timeframe: {tf}")
```
Any strategy with `timeframe=3d` would crash the signal runner task. Other brokers silently skip unsupported timeframes or return an empty dataset.

**Recommendation:** Either add "3d" to the Alpaca map (map to Day(3)) or add validation at strategy configuration time to reject unsupported timeframe/broker combinations.

---

## 7. Improvements Recommended

---

### IMP-01 · Pass cached OHLCV from regime to signal engine (eliminates double-fetch)

Already described in BUG-MED-01. This is the most impactful single-line fix.

---

### IMP-02 · Add asset_class-aware guard to `_enhance_signal()` for trailing stop and S/R snap

Covers BUG-MED-01 (mean reversion), BUG-MED-04 (bull call spread), BUG-LOW-04 (S/R options snap). One-time fix in `base.py:_enhance_signal()` that protects all strategies.

```python
def _enhance_signal(self, signal: Signal, data: pd.DataFrame) -> Signal:
    is_options = (
        getattr(signal, "asset_class", "") == "option"
        or bool(getattr(signal, "options_meta", None))
    )
    # Confirmation candle — all strategies
    ...
    # S/R snap — skip for options (entry/TP are premium prices, not underlying price levels)
    if not is_options:
        ...snap TP to S/R...
    # Trailing stop — skip for options and mean-reversion
    is_mean_reversion = signal.strategy_name in ("mean_reversion_bb",)
    if not is_options and not is_mean_reversion and signal.trailing_stop_pct is None:
        signal.trailing_stop_pct = round(1.5 * atr_val / current_price * 100, 4)
```

---

### IMP-03 · IBKR orphan sync should cover all brokers (Alpaca + Binance)

Described in BUG-LOW-03. Extend `reconcile_positions()` orphan detection to Alpaca and Binance.

---

### IMP-04 · Add `signal_type_category` field to `TradeOutcome` for ML label disambiguation

Group signals as `directional` (BUY/SHORT), `momentum_exit` (COVER/SELL-to-close), or `premium_collection` (iron condor/covered call SELL). Resolver uses category instead of raw signal string.

---

### IMP-05 · Add regime-aware circuit breaker reset

When regime switches from high_volatility to trending or ranging, automatically reset the per-strategy circuit breaker if it was tripped solely due to volatility regime losses. This prevents strategies from sitting blocked after a regime normalises.

---

### IMP-06 · Atomic Binance bracket via OCO order type

Binance supports OCO (One-Cancels-the-Other) orders that bind SL and TP atomically. Replace the sequential `STOP_LOSS` + `TAKE_PROFIT_LIMIT` placement with a single `create_oco_order()` call. If OCO submission fails, the entry should NOT proceed.

```python
# In binance_client.py
oco = await self.exchange.create_order(
    symbol, "oco", side,
    qty, tp_price,
    params={"stopPrice": sl_price, "stopLimitPrice": sl_limit}
)
```

---

### IMP-07 · Implement `get_order_status()` on all broker clients

Needed for BUG-HIGH-03 fix in `cleanup_stale_pending_trades()`. All 3 brokers need a method to query fill status by `broker_order_id`.

---

### IMP-08 · Add `breakeven_after_hours` and `max_hold_hours` configuration to strategy parameters UI

These time-based exit parameters exist in `monitor_sl_tp` (strategy params cache lookup) but there is no UI for configuring them, and no validation that the strategy `parameters` dict contains them. Add to strategy configuration schema.

---

### IMP-09 · `signal_runner` OHLCV cache should use `since=` parameter for sub-hour timeframes

Currently `limit=200` fetches the last 200 candles from now, which is fine for 1h+. For 1m strategies, 200 candles is only 3.3 hours of data — insufficient for ATR(14) and ADX(14) lookback. The limit should scale with timeframe:
```python
limit = max(200, 14 * 3)  # minimum for lookback periods
# For 1m: 200 is fine (200 minutes = 3.3h). 
# For 1d, consider fetching 365 days.
```

---

### IMP-10 · Add a `strategy_health` table tracking last-signal-time per strategy

Operators need to know if a strategy has been silent for an unusual period (broker connectivity issue, regime filter blocking all signals, etc.). Track `last_signal_at` and alert if a strategy hasn't produced a signal in 24h for active strategies.

---

### IMP-11 · WebSocket `receive_text()` loop should respond to ping frames

Current WebSocket endpoint loops on `receive_text()` to keep the connection alive. It does not handle WebSocket ping frames (RFC 6455). Some proxy servers (Nginx, traefik) close idle WebSocket connections after 60s without pings. FastAPI/Starlette handles this automatically, but documenting it is important for VPS deployment.

---

### IMP-12 · Add `COVER` signal support to broker `close_position` side mapping

`ForwardEngine.close_position()` correctly maps `COVER` → `sell` direction. However, broker `place_order()` calls from `close_position()` pass `side = "sell" if trade.side in _long_sides else "buy"`. Verify that all 3 broker clients correctly handle `side="sell"` for `COVER` positions — Alpaca may need `position_intent="close"` flag.

---

### IMP-13 · Consider storing `effective_stop_price` and `effective_take_profit_price` per trade

When Alpaca reanchors SL/TP (code 42210000 retry), `ForwardEngine` updates `trade.stop_loss` and `trade.take_profit` in memory. This update IS persisted because `forward_engine.py` calls `db_session.commit()` after. However, the audit trail should also store the original signal levels separately so post-trade analysis can measure slippage from intended to actual bracket levels.

---

## 8. Per-System Verification

### 8.1 Transaction / Trade Flow

**Entry Flow:**  
`signal_runner (Celery/scheduler)` → `ForwardEngine.process_signal()` → `RiskManager.validate()` → `broker.place_order()` → `Trade/LiveTrade persist` → `WS broadcast` → `in-app notification`

**Verification findings:**

| Step | Status | Notes |
|---|---|---|
| Balance fetch from broker | ✅ OK | Correctly uses `force_paper=is_paper` |
| Open position count | ✅ OK | Counts both Trade (paper) + LiveTrade (live) by broker |
| Asset class exposure | ✅ OK | Fixed per bug annotation F-083 |
| Per-asset exposure | ✅ OK | Fixed per bug annotation BUG-5 |
| Risk manager validation | ✅ OK | 5-level validation chain complete |
| Anti-pyramiding G1 | ✅ OK | ML confidence override with DB re-check |
| IBKR forex min lot G5 | ✅ OK | 20,000 unit minimum enforced |
| Multi-leg options (paper) | ❌ BUG-CRIT-02 | Executes as naked underlying short |
| Multi-leg options (live) | ✅ OK | Correctly blocked |
| Single-leg options | ✅ OK | option_kwargs passed to broker |
| Position reversal | ✅ OK | Closes opposing position before opening new |
| PENDING vs OPEN status | ✅ OK | Only confirmed fills marked OPEN |
| WS broadcast on open | ✅ OK | Correct payload |
| In-app notification | ✅ OK | Correct level (trade/warning) per status |
| Rejection audit trail | ✅ OK | REJECTED trades saved to DB with reason |
| Paper/live credential routing | ✅ OK | `get_broker(force_paper=is_paper)` |

**Exit Flow:**  
`monitor_sl_tp / manual close` → `ForwardEngine.close_position()` → `broker.cancel_open_orders()` → `broker.place_order(market)` → `PnL compute` → `Trade persist FILLED` → `risk_manager.record_outcome()` → `in-app notification (email)`

| Step | Status | Notes |
|---|---|---|
| Atomic DB guard (double-close prevention) | ✅ OK | UPDATE status=PENDING WHERE status=OPEN |
| F-103 broker position verification | ✅ OK | Skips market order if already flat |
| Cancel standing bracket orders pre-close | ✅ OK | Prevents insufficient-balance error (Binance) |
| F-108 bracket partial-fill handling | ✅ OK | Weighted avg exit price computed |
| PnL computation | ✅ OK | `(exit - entry) × qty × direction` |
| COVER direction | ✅ OK | Maps to sell side correctly |
| Risk manager consecutive loss update | ✅ OK | `record_outcome()` called |
| Close notification (with email) | ✅ OK | `dispatch(send_email=True)` |
| FX auto-convert IBKR | ✅ OK | Fires after IBKR forex close |

---

### 8.2 Signal Flow

**Full pipeline:**  
`Strategy.generate_signal()` → `BaseStrategy._enhance_signal()` → `SignalEngine.run()` → `DB Signal persist` → `TradeOutcome create` → `WS broadcast` → `in-app signal notification` → `confluence check` → `ForwardEngine.process_signal()`

| Step | Status | Notes |
|---|---|---|
| Strategy OHLCV data fetch | ⚠️ BUG-MED-01 | Double-fetched per tick |
| Regime router classification | ✅ OK | Hysteresis applied, fixed/auto_switch modes |
| Regime auto-switch | ✅ OK | Falls back correctly when no candidate |
| Signal deduplication | ✅ OK | pg_advisory_xact_lock + candle-window check |
| TradeOutcome creation | ✅ OK | All trackable signals create feedback record |
| WS signal broadcast | ✅ OK | Correct payload |
| In-app signal notification | ✅ OK | Only actionable (non-HOLD) signals notified |
| Confluence multi-TF check | ✅ OK | Per-strategy threshold, suppression with reason |
| HOLD to DB (regime filtered) | ✅ OK | Persisted for dashboard visibility |
| `acted_on` flag set | ✅ OK | Set to True when execution attempted |

---

### 8.3 Notification Flow

**Notification types and delivery:**

| Event | In-App | WebSocket | Email |
|---|---|---|---|
| Signal generated (BUY/SHORT etc.) | ✅ | ✅ | ❌ (by design) |
| Trade opened (FILLED) | ✅ | ✅ | ✅ |
| Trade pending (not confirmed) | ✅ | ❌ | ❌ |
| Trade closed (SL/TP/manual) | ✅ | ❌ | ✅ |
| Broker reconcile close | ✅ | ❌ | ✅ |
| Risk blocked signal | ✅ | ❌ | ❌ |
| Order rejected | ✅ | ❌ | ❌ |
| Low confluence suppression | ✅ | ❌ | ❌ |
| Trailing stop moved | ❌ | ✅ | ❌ |
| PENDING trade timeout | ✅ | ❌ | ❌ |
| Emergency stop | ⚠️ | ❌ | ❌ GAP-04 |
| ML retrain complete | ✅ | ❌ | ❌ |
| ML cache flush failure | ✅ | ❌ | ❌ |
| IBKR reconnect failure | ✅ | ❌ | ❌ |

**Notification after-commit hook:** Correctly uses `after_commit` SQLAlchemy event to schedule WebSocket broadcast, preventing broadcast before DB commit. ✅

**Gmail delivery:** `notifier.dispatch()` with `send_email=True` correctly awaits the email coroutine. However, email is NOT sent as a background task — if SMTP is slow, it blocks the request/task. Consider `asyncio.create_task()` for email with error suppression.

---

### 8.4 Broker Compatibility

#### Alpaca

| Feature | Paper | Live | Status |
|---|---|---|---|
| Balance fetch | ✅ | ✅ | OK |
| OHLCV fetch | ✅ | ✅ | OK — uses NYSE trading-day calendar |
| Place market order with bracket | ✅ | ✅ | OK — code 42210000 retry implemented |
| Fill confirmation polling | ✅ | ✅ | OK — 10s window, 0.5s interval |
| Fractional share sell floor | ✅ | ✅ | OK |
| Cancel open orders | ✅ | ✅ | OK |
| Get positions | ✅ | ✅ | OK |
| Bid/ask fetch | ✅ | ✅ | OK |
| Update stop loss | ✅ | ✅ | OK |
| "3d" timeframe | ❌ | ❌ | GAP-08 — ValueError crash |
| Options (single-leg) | ❌ | ❌ | Not implemented for Alpaca |
| Stream prices | ? | ? | Needs verification |

#### Binance

| Feature | Testnet | Live | Status |
|---|---|---|---|
| Balance fetch | ✅ | ✅ | OK — uses free+locked balance |
| OHLCV fetch | ✅ | ✅ | OK — uses unauthenticated `_data_exchange` to avoid testnet limits |
| Place market entry | ✅ | ✅ | OK |
| TP limit order | ✅ | ✅ | 2-attempt retry |
| SL stop order | ✅ | ✅ | 2-attempt retry; placed after TP |
| Entry→SL/TP failure | ❌ | ❌ | BUG-CRIT-01 — unprotected exchange position |
| Cancel open orders | ✅ | ✅ | OK — atomic DELETE endpoint |
| LOT_SIZE normalization | ✅ | ✅ | OK |
| Get positions | ✅ | ✅ | OK |
| Bid/ask fetch | ✅ | ✅ | OK |
| Stream prices | ? | ? | Needs verification |

#### IBKR

| Feature | Paper | Live | Status |
|---|---|---|---|
| Balance fetch | ✅ | ✅ | OK — 60s cache, accountValues poll |
| OHLCV fetch | ✅ | ✅ | OK — RTH-aware, MIDPOINT for forex |
| Bracket order (market + TP + SL) | ✅ | ✅ | OK — 1.0s settle, parentId chain |
| Paper fill via reqMktData | ✅ | N/A | OK — bracket subscriptions maintained |
| Connection: reconnect loop | ✅ | ✅ | OK — exponential backoff 15s→300s |
| Error 326 (clientId conflict) | ✅ | ✅ | OK — 180s backoff guard |
| Error 200 (min lot) | ✅ | ✅ | OK — G5 pre-check 20,000 units FOREX |
| Forex auto-convert after close | ✅ | ✅ | OK — calls `auto_convert_fx()` |
| Stream prices | ❌ | ❌ | GAP-03 — method likely not implemented |
| Options (single-leg) | ✅ | ✅ | OK — `option_kwargs` supported |
| Options (multi-leg, live) | ❌ | ❌ | Blocked by design (pending ComboOrder) |
| Options (multi-leg, paper) | ❌ | ❌ | BUG-CRIT-02 — executes as naked short |
| Orphan sync | ✅ | ✅ | OK — reconcile detects IBKR orphans |

---

### 8.5 Strategy Implementations

| Strategy | Signal Types | Regime Aware | ML Veto | SL/TP | Status |
|---|---|---|---|---|---|
| `hybrid_macd_rsi` | BUY / SHORT | ✅ score adj | ✅ 35/65 | ATR-based | ✅ OK — duplicate confirmation candle (BUG-MED-02) |
| `momentum_breakout` | BUY / SHORT | ✅ threshold adj | ❌ | ATR-based + trailing | ✅ OK |
| `mean_reversion_bb` | BUY / SHORT | ✅ suppressed in trending | ❌ | BB middle | ⚠️ BUG-HIGH-01 trailing stop |
| `volatility_squeeze` | BUY / SHORT | ✅ | ❌ | ATR-based | ✅ OK — squeeze detection logic correct |
| `iron_condor` | SELL | IV+ADX | ❌ | Premium×1.5 / ×0.5 | ❌ BUG-CRIT-02 (paper), BUG-HIGH-02 (ML) |
| `covered_call` | SELL | IV+RSI | ❌ | Premium×3 / ×0.1 | ⚠️ BUG-MED-03 sizing |
| `bull_call_spread` | BUY | IV+MACD | ❌ | Debit / 75% max profit | ❌ BUG-CRIT-04 (dead), BUG-MED-04 (trailing) |

**ATR computation consistency:**
- `tools/basic/volume_atr_adx.py` ATR: uses EMA (`ewm(span=14)`) — standard exponential smoothing
- `core/features.py` ATR: uses SMA (`rolling(14).mean()`) — only for trainer label threshold

These serve different purposes (tools for live SL sizing, features for ML training labels) and different values are expected. ✅ No issue.

**RSI computation consistency:**
- `tools/basic/rsi.py`: Wilder's smoothing (`ewm(com=period-1)`)  
- `core/features.py`: SMA-based gain/loss (`rolling().mean()`)  

Rule-based signals (hybrid, mean_reversion, covered_call) use `tools/basic/rsi.py`. ML model training AND inference both use `core/features.py` via `compute_features()`. ✅ No mismatch — rule RSI and ML feature RSI differ by design and ML training/inference are internally consistent.

---

### 8.6 Risk Management

**5-Level validation chain (verified ✅):**
1. Per-broker daily circuit breaker (from `_daily_pnl()` — note: paper+live combined, BUG-LOW-05)
2. Per-strategy circuit breaker (consecutive losses)
3. Portfolio circuit breaker (portfolio-wide consecutive losses)
4. Consecutive loss counter
5. Open positions count gate
6. Asset class exposure gate (F-083 fix verified in code)
7. Per-asset exposure gate (BUG-5 fix verified in code)
8. Stop loss required gate
9. Position sizing (Kelly-variant: `balance × risk_pct / entry_price`)
10. R:R ratio check

**Risk state persistence:** Correctly atomically saves to `runtime/risk_state.json` via tempfile+rename pattern. ✅

**Circuit breaker trip recording:** `record_outcome()` updates portfolio + per-strategy + per-broker counters. Cross-broker contamination prevented via `_last_loss_broker` guard. ✅

**Position sizing for options:** ❌ BUG-CRIT-03 — uses premium as denominator, not max loss.

---

### 8.7 ML Pipeline

**Training flow:**  
`ModelTrainer.retrain_all()` → yfinance OHLCV → `compute_features()` (shared with inference) → `_label()` (1.5×ATR rise in N candles) → XGBoost StratifiedKFold(5) → AUC gate 0.55 → `joblib.dump()` → `latest.json`

**Inference flow:**  
`MLScorer.predict_proba(symbol, timeframe, df)` → `compute_features(df)` → cached model load → `bst.predict_proba(X)[-1, 1]`

| Component | Status |
|---|---|
| Feature computation (shared training/inference) | ✅ B4 fix verified |
| Model per-symbol:timeframe | ✅ OK — legacy fallback for old format |
| Thread-safe double-checked locking | ✅ OK |
| AUC gate (0.55 minimum) | ✅ OK |
| Live outcome label upweighting (3×) | ✅ OK |
| Multi-process cache sync (Celery→FastAPI) | ✅ OK — HTTP `/internal/ml/reload` call |
| XGBoost missing value handling | ✅ OK — NaN propagated for volume (forex) |
| VWAP feature forex-aware | ✅ OK — NaN for volume=0 |
| Event: new model fails HTTP reload | ✅ OK — in-app warning dispatched |
| `same-candle TP+SL` label bias | ✅ BUG-6 FIX verified in outcome_resolver |
| Options outcome ML labels | ❌ BUG-HIGH-02 — incorrect for iron condor/covered call |
| Model staleness alerting | ❌ GAP-07 |

---

### 8.8 Schedulers & Celery Tasks

**Dual scheduler architecture verified:**

| Scheduler | Handles | Frequency |
|---|---|---|
| `_forward_test_scheduler` (in-process) | `is_paper=True` strategies | Wall-clock candle boundaries |
| Celery `run_signals` | `is_paper=False` strategies | Every 60 seconds |
| Celery `outcome_resolver` | ML feedback labels | Nightly 01:30 UTC |
| Celery `retrain_all` | XGBoost model training | Sunday 02:00 UTC |
| Celery `rebalance` | Portfolio weights | Sunday 03:00 UTC |
| Celery `cleanup_notifications` | Old notification pruning | Sunday 04:00 UTC |

**Ordering in Celery `run_signals`:**  
`initialize()` → `reconcile_positions()` → `monitor_sl_tp()` → `cleanup_stale_pending_trades()` → `session.commit()` → per-strategy loop ← This ordering is correct and critical.

**`_forward_test_scheduler` ordering:**  
Mirrors Celery sequence for paper: reconcile → monitor → cleanup → strategies. Redis `last_fired` persists candle timing across restarts. ✅

**Candle deduplication F-010:** `_last_candle_fired[strat.id]` is module-level (process-local), wiped on worker restart. DB candle-window dedup + advisory lock provides the durable guard. ✅

**Portfolio rebalancer (ML-03):** Sharpe-weighted allocation writes to `Strategy.parameters.position_size_multiplier`. Verified compatible with `ForwardEngine.process_signal()`. ✅

**Notification cleanup:** Purges old DB notification records. No critical data loss risk (signals, trades are kept). ✅

---

### 8.9 WebSocket & Real-time

| Feature | Status |
|---|---|
| JWT auth before connection accept | ✅ OK — token from cookie or query param |
| Broadcast to all connected clients | ✅ OK — dead connection cleanup |
| Events: signal, trade, trailing_stop_moved, connected | ✅ OK |
| Active client count tracking | ✅ OK |
| Heartbeat / keep-alive | ⚠️ IMP-11 — server depends on client to send text to keep alive |
| Unauthenticated connection rejection (code 4001) | ✅ OK |
| Multi-process WebSocket (Celery→FastAPI) | ✅ OK — Celery uses HTTP to reload ML, not WS directly |

---

### 8.10 API Routes & Auth

| Route | Auth | Method | Status |
|---|---|---|---|
| `GET /signals/` | ✅ JWT | Admin sees all; users see own+system | ✅ OK |
| `POST /signals/dismiss-expired` | ✅ JWT | Bulk dismiss HOLD + old unacted | ✅ OK |
| `GET /positions/open` | ✅ JWT | All open paper+live | ✅ OK |
| `GET /positions/history` | ✅ JWT | Paginated FILLED trades | ✅ OK |
| `GET /positions/history/export` | ✅ JWT | CSV export | ✅ OK |
| `POST /positions/close/{id}` | ✅ JWT | Manual close | Needs verification |
| `POST /positions/emergency-stop` | ✅ JWT (admin) | Require admin | ✅ OK |
| `POST /auth/login` | ❌ public | bcrypt+SHA256 hash | ✅ OK |
| `GET /ws` | ✅ JWT token | WebSocket upgrade | ✅ OK |

**Authentication model:**
- Passwords: SHA-256 pre-hash → bcrypt hash (avoids 72-byte bcrypt limit). ✅
- Tokens: HS256 JWT, 7-day expiry, no refresh mechanism. Acceptable for most uses.
- Cookie: `httpOnly access_token` — `cookie_secure=False` default. **Must be set `True` for HTTPS production.**
- `require_admin` dependency exists for emergency stop. ✅

---

## 9. Severity Index

| ID | Description | File | Severity |
|---|---|---|---|
| BUG-CRIT-01 | Binance bracket: entry fills, TP/SL fails → orphaned exchange position | `binance_client.py` | CRITICAL |
| BUG-CRIT-02 | Multi-leg options (paper) execute as naked equity short | `forward_engine.py` | CRITICAL |
| BUG-CRIT-03 | Options position sizing uses premium not max loss | `risk_manager.py`, `forward_engine.py` | CRITICAL |
| BUG-CRIT-04 | Bull Call Spread MACD attribute access bug → always HOLD | `bull_call_spread.py` | CRITICAL |
| BUG-HIGH-01 | Mean reversion trailing stop causes premature exits | `mean_reversion.py`, `base.py` | HIGH |
| BUG-HIGH-02 | Iron condor P&L resolved as directional short → corrupt ML labels | `outcome_resolver.py` | HIGH |
| BUG-HIGH-03 | `cleanup_stale_pending_trades` marks PENDING→REJECTED without broker check | `forward_engine.py` | HIGH |
| BUG-MED-01 | Double OHLCV fetch per strategy per tick | `signal_runner.py` | MEDIUM |
| BUG-MED-02 | Hybrid confirmation candle check duplicated | `hybrid.py`, `base.py` | MEDIUM |
| BUG-MED-03 | Covered call sizing in per-share notation not per-contract | `covered_call.py` | MEDIUM |
| BUG-MED-04 | Bull call spread trailing stop applied (inappropriate for options) | `bull_call_spread.py` | MEDIUM |
| BUG-MED-05 | Price streaming uses mid-price not bid/ask for SL triggers | `price_stream.py`, `forward_engine.py` | MEDIUM |
| BUG-LOW-01 | `cleanup_stale_pending_trades` uses deprecated `datetime.utcnow()` | `forward_engine.py` | LOW |
| BUG-LOW-02 | Iron condor lacks RSI filter | `iron_condor.py` | LOW |
| BUG-LOW-03 | Orphan position sync only covers IBKR | `forward_engine.py` | LOW |
| BUG-LOW-04 | `_enhance_signal()` S/R snap overrides options TP | `base.py` | LOW |
| BUG-LOW-05 | `_daily_pnl` combines paper+live for circuit breaker | `forward_engine.py` | LOW |
| GAP-01 | Live multi-leg options execution not supported | `forward_engine.py`, `ibkr_client.py` | HIGH |
| GAP-02 | Binance orphan position recovery missing | `forward_engine.py` | HIGH |
| GAP-03 | IBKR `stream_prices()` not implemented | `ibkr_client.py` | MEDIUM |
| GAP-04 | Emergency stop has no email notification | `forward_engine.py` | MEDIUM |
| GAP-05 | Options expiry ignores exchange holidays | `iv_rank.py` | MEDIUM |
| GAP-06 | `TradeOutcome` model lacks `options_meta` field | `db/models.py` | MEDIUM |
| GAP-07 | No ML model freshness alerting | `ml_scorer.py`, `trainer.py` | LOW |
| GAP-08 | Alpaca `_map_timeframe` raises ValueError for "3d" | `alpaca_client.py` | LOW |

---

*End of Audit Report v2.5.0*
