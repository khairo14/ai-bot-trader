# Execution Engine

## Overview

The execution engine is responsible for translating signals into actual orders (paper or live). It handles all three execution modes, manages order lifecycle, and provides a full audit trail of every action taken.

## Implementation Status

| Component | Status | Notes |
|---|---|---|
| `ForwardEngine` class | ✅ Complete | `backend/core/engine/forward_engine.py` |
| `RiskManager` class | ✅ Complete | `backend/core/risk_manager.py` |
| Paper fill simulation | ✅ Complete | Uses real-time prices + slippage |
| Execution mode logic | ✅ Complete | suggestion / semi-auto / full-auto |
| Emergency stop | ✅ Complete | Queries DB, closes all open trades, fires email alert |
| Auto-scheduler | ✅ Complete | `main.py` — wall-clock aligned, per-strategy timeframe |
| Market hours gate | ✅ Complete | NYSE 09:30–16:00 ET (stocks); 24/7 (crypto) |
| NYSE holiday calendar | ✅ Complete | All 10 US market holidays via `pandas_market_calendars` |
| Order rejection safety | ✅ Complete | REJECTED status + notes saved; never crashes on broker error |
| Signal dismissal | ✅ Complete | "Clear Expired" hides HOLDs + stale signals from Recent Signals |
| Signal staleness guard | ✅ Complete | SignalCard blocks execution if signal > 5 min old |
| WebSocket signal push | ✅ Complete | `signal` + `trade` events broadcast after every fill |
| Semi-auto approve/reject | ✅ Complete | `POST /api/signals/{id}/approve` + `/reject` + Dashboard panel |
| ML inference | ✅ Complete | `MLScorer` blends into HybridStrategy (60% rule / 40% ML) |
| In-app notifications | ✅ Complete | DB-backed, WS real-time bell, Gmail SMTP alerts |
| Notification archival | ✅ Complete | `tasks/notification_cleanup.py` — weekly Sunday 04:00 UTC, 30-day TTL for read notifications |
| Real-time price streaming | ✅ Complete | `core/engine/price_stream.py` — `PriceStreamManager` singleton; one asyncio Task per broker |
| SL/TP concurrency lock | ✅ Complete | `_MONITOR_SL_TP_LOCK` in `forward_engine.py` — prevents duplicate monitor runs |
| Static SL enforcement | ✅ Fixed | Static stop loss now checked for all trades, not only trailing-stop trades (BUG-1) |

---

## Execution Modes

Each strategy can independently be set to one of three modes. The mode can be changed at any time from the dashboard without restarting the bot.

### Mode 1: Suggestion
```
Signal Generated
      ↓
Signal card appears on dashboard
      ↓
Bot does nothing else — human reviews
      ↓
Human manually opens trade on broker platform
      ↓
Bot logs outcome if position is manually entered
```
**Use when:** You want AI signal guidance but full manual control over execution.

---

### Mode 2: Semi-Auto
```
Signal Generated
      ↓
Signal card appears on dashboard WITH "Execute" button
      ↓
Push notification sent (if configured)
      ↓
Human clicks "Execute" to confirm
      ↓
Bot places the order through broker API
      ↓
Bot manages stop loss and take profit automatically
```
**Use when:** You want to stay in control of entries but let the bot manage the trade once opened.

---

### Mode 3: Full-Auto
```
Signal Generated
      ↓
Risk Manager validates position size and exposure
      ↓
Order placed immediately through broker API
      ↓
Stop loss and take profit orders placed simultaneously
      ↓
Position monitored continuously
      ↓
Exit when target hit, stop hit, or exit signal fires
      ↓
All actions logged to DB + dashboard updated
```
**Use when:** 24/7 autonomous operation. Human sets parameters and reviews performance.

---

## Order Flow (Full-Auto Detail)

```
Signal: BUY BTC/USDT at $65,200
Risk Manager:
  - Account balance: $10,000
  - Risk per trade: 2% = $200 max loss
  - Stop loss: $63,500 (distance: $1,700)
  - Position size: $200 / $1,700 = 0.117 BTC
  - Check: max open positions not exceeded
  - Check: total portfolio exposure not exceeded
        ↓
Broker Connector:
  - Place MARKET order: BUY 0.117 BTC
  - Place STOP_LOSS_LIMIT order: SELL 0.117 BTC at $63,500
  - Place TAKE_PROFIT_LIMIT order: SELL 0.117 BTC at $68,500
        ↓
Order Status Monitor:
  - Poll every 5 seconds until fill confirmed
  - On fill: log entry price, timestamp, order ID
        ↓
Position Tracker:
  - Open position added to portfolio
  - P&L calculated in real time
        ↓
Exit Trigger (whichever comes first):
  - Stop loss hit → close position
  - Take profit hit → close position
  - Exit signal from strategy → close position
  - Manual override from dashboard → close position
        ↓
Trade closed → logged to DB → portfolio updated
```

---

## Paper Trading (Forward Test)

Paper trading mode uses the same execution flow as live trading, but instead of calling the broker API, it simulates order fills using real-time market prices.

### Fill Simulation
- **Market orders:** Filled at current price + configured slippage
- **Limit orders:** Filled when market price crosses limit price
- **Stop orders:** Triggered and filled when stop price is reached
- Slippage applied: `fill_price = market_price * (1 + slippage_pct)` for buys

### Paper Account
- Initial paper balance configured in `.env` (default: $10,000)
- Paper positions and P&L tracked separately from any live account
- Can run paper and live simultaneously on different strategies

---

## Auto-Scheduler

The forward test scheduler runs automatically in the background without any user interaction. It fires each strategy at the correct candle-close boundary for its configured timeframe.

### How It Works

```
Every 60 seconds:
  For each active paper strategy:
    1. Calculate last candle-close epoch:
          last_close = floor(now / interval) * interval
    2. Wait 30 s buffer after close (ensures exchange has finalised candle)
    3. Check: already fired for this candle close? → skip
    4. Check: is market open for this broker?
         Crypto  → always open
         Stocks  → NYSE session only (see Market Hours below)
         If closed → skip, do NOT update last_fired
                     (retries every minute until market opens)
    5. Fire: _run_one_strategy(strat)
    6. Mark last_fired[strategy_id] = last_close
```

### Timing examples

| Strategy timeframe | Fires at |
|---|---|
| `1m` | Every minute (00:30, 01:30 … UTC) |
| `1h` | 00:00:30, 01:00:30, 02:00:30 … UTC |
| `4h` | 00:00:30, 04:00:30, 08:00:30 … UTC |
| `1d` | First scheduler tick ≥ 30 s after UTC midnight |

> **Daily stock strategies:** `1d` candle closes at UTC midnight while the NYSE is closed. The scheduler keeps retrying until 09:30 ET the next trading day, then fires once using the previous day's fully-closed candle — exactly the same data a backtest would use.

### Configuration (`.env`)

```
FORWARD_TEST_INTERVAL_MINUTES=1   # 0 = disabled; any non-zero = enabled
                                  # actual interval is derived from strategy timeframe
```

---

## Market Hours & Holiday Calendar

Stock/options brokers (Alpaca, IBKR) are gated to NYSE regular session hours only. Crypto (Binance) trades 24/7.

### Stock Market Gate

| Condition | Behaviour |
|---|---|
| NYSE session 09:30–16:00 ET, Mon–Fri, non-holiday | ✅ Signal fires normally |
| Before 09:30 ET | ⏳ Signal held; retries every minute |
| After 16:00 ET | ⏳ Signal held until next session |
| Weekend | ⏳ Signal held until Monday morning |
| US market holiday | ⏳ Signal held until next trading day |

### Holidays Blocked

All NYSE market holidays are blocked automatically via `pandas_market_calendars`:

- New Year's Day
- Martin Luther King Jr. Day
- Presidents' Day
- Good Friday
- Memorial Day
- Juneteenth National Independence Day
- Independence Day (July 4th)
- Labor Day
- Thanksgiving Day
- Christmas Day

DST (daylight saving time) is handled automatically using `ZoneInfo("America/New_York")`.

---

## Order Rejection Handling

When a live broker rejects an order or the exchange halts trading, the execution engine handles the failure gracefully — it never crashes the server.

### Causes of Rejection

- **NYSE circuit breakers:** L1 (7% drop), L2 (13%), L3 (20%) — entire market halted
- **LULD halts:** Individual stock limits-up/limits-down pause
- **Crypto maintenance windows:** Exchange scheduled downtime
- **Insufficient margin:** Account balance too low after market move
- **Invalid symbol:** Symbol delisted or renamed

### What Happens

```
live broker.place_order()
  ↓
  Success → trade.status = OPEN, broker_order_id saved
  ↓
  Exception
    → trade.status = REJECTED
    → trade.broker_order_id = "rejected_{symbol}_{timestamp}"
    → trade.notes = "Order rejected: {error message}"
    → REJECTED trade record saved to DB for audit trail
    → Returns None — no crash, no exception propagation
    → Warning logged: cause, symbol, timestamp
```

All REJECTED trades are visible in the Forward Test → Trades table.

---

## Signal Dismissal

The Recent Signals panel on the Dashboard is kept clean by dismissing signals that are no longer actionable. Dismissed signals are **hidden from the UI but retained in the database** for ML training.

### "Clear Expired" Button

Located in the Recent Signals header. When clicked:

1. Dismisses all **HOLD signals** (never actionable, any age)
2. Dismisses all **un-acted-on signals older than 24 hours**

### API Endpoint

```
POST /api/signals/dismiss-expired?older_than_hours=24
```

Returns: `{"dismissed": N, "message": "N signal(s) cleared from Recent Signals."}`

### Signal Staleness in SignalCard

Every signal card checks its age against the current time:

- Signal ≤ 5 minutes old → Execute button enabled
- Signal > 5 minutes old → Yellow warning: "Signal is Xm old — price levels may be stale. Click Run Now for a fresh signal."
- Execute button is visually disabled and blocked when stale

---

## Order Types Supported

| Order Type | Supported On | Description |
|---|---|---|
| `MARKET` | All brokers | Instant fill at current price |
| `LIMIT` | All brokers | Fill at specified price or better |
| `STOP_LOSS` | All brokers | Trigger stop, close position |
| `STOP_LIMIT` | All brokers | Stop trigger with limit price |
| `TAKE_PROFIT` | All brokers | Auto close at profit target |
| `OCO` | Binance, IBKR | One-Cancels-Other (stop + take profit in one) |
| `TRAILING_STOP` | Alpaca, IBKR | Dynamic stop that follows price |
| `BRACKET` | IBKR, Alpaca | Entry + stop + take profit in one order |

---

## Manual Override (Always Available)

Regardless of execution mode, the following manual controls are always available from the dashboard:

| Action | Effect |
|---|---|
| **Pause Strategy** | Stop new signals from being executed. Open positions remain. |
| **Stop Strategy** | Stop new signals AND close all open positions for this strategy. |
| **Close Position** | Immediately close a specific open position at market price. |
| **Emergency Stop** | Halt ALL strategies and close ALL open positions immediately. |
| **Modify Stop Loss** | Change stop loss price on an open position. |
| **Modify Take Profit** | Change take profit price on an open position. |

---

## Order Monitoring & Recovery

The bot handles connection issues gracefully:

- All placed orders are stored in DB before being sent to broker
- On restart, the bot checks all open positions and orders against broker state
- Orphaned orders (placed but not tracked) are detected and reconciled
- If broker connection drops during an order: retry logic with exponential backoff
- If reconnection fails after 5 minutes: send alert + fall back to suggestion mode

---

## Execution Logs

Every action the execution engine takes is logged:

```json
{
  "timestamp": "2026-03-04T14:32:01Z",
  "action": "ORDER_PLACED",
  "symbol": "BTC/USDT",
  "side": "BUY",
  "quantity": 0.117,
  "order_type": "MARKET",
  "price": 65204.50,
  "order_id": "binance_ABC123",
  "strategy": "hybrid_macd_rsi",
  "mode": "full-auto",
  "signal_confidence": 0.78
}
```

---

## SL/TP Monitor — Real-Time Price Streaming

The SL/TP heartbeat runs every 60 seconds and checks all open positions for stop-loss or take-profit triggers. Starting with audit 3, price data is sourced from a live WebSocket stream for much lower latency.

### Price Lookup Order

```
For each open trade:
  1. Check PriceStreamManager.get_price(symbol)  ← sub-second, WebSocket
        → if price available: use it immediately
  2. Fallback: _price_cache[symbol] + timestamp check ← 60s cached REST price
        → if still fresh (< lookback window): use cached
  3. Final fallback: broker.get_bid_ask(symbol)  ← live REST call
```

### PriceStreamManager (`core/engine/price_stream.py`)

| Property | Value |
|---|---|
| Subscription refresh interval | Every 30 seconds (auto-adds newly opened trades) |
| Reconnect delay on stream failure | 5 seconds |
| Price storage | `dict[symbol → latest_mid_price]` (in-process) |
| Lifecycle | Launched in FastAPI `lifespan` startup; cancelled cleanly at shutdown |

All three brokers implement `stream_prices(symbols, callback)` and `update_stop_loss(symbol, side, quantity, new_sl_price)`. `PriceStreamManager` calls broker-specific streams per the set of symbols with open trades for that broker.

### Concurrency Guard

`_MONITOR_SL_TP_LOCK` (module-level `asyncio.Lock`) prevents duplicate concurrent runs of `monitor_sl_tp` within the same FastAPI process. If a previous run is still in progress when a new call arrives, the new call returns immediately with `0` processed trades. This is process-local only — cross-process deduplication (Celery + FastAPI) is handled by a DB-level advisory lock inside `close_position`.

Full execution logs are viewable on the dashboard under **Activity → Execution Log**.

---

## Switching from Paper to Live

The mode switch from paper to live is a single toggle in the dashboard (per broker). The checklist enforced before allowing the switch:

```
[ ] Strategy has been running in paper mode for minimum 30 days
[ ] Paper mode win rate ≥ backtest win rate × 0.80
[ ] Paper mode max drawdown ≤ backtest max drawdown × 1.25
[ ] API keys for live trading are configured
[ ] Risk parameters reviewed and confirmed
[ ] Emergency stop tested and working
```

Once confirmed, the broker URL switches from paper endpoint to live endpoint. All other code remains identical.
