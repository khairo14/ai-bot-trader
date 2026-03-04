# Execution Engine

## Overview

The execution engine is responsible for translating signals into actual orders (paper or live). It handles all three execution modes, manages order lifecycle, and provides a full audit trail of every action taken.

## Implementation Status

| Component | Status | Notes |
|---|---|---|
| `ForwardEngine` class | ✅ Written | `backend/core/engine/forward_engine.py` |
| `RiskManager` class | ✅ Written | `backend/core/risk_manager.py` |
| Paper fill simulation | ✅ Written | Uses real-time prices + slippage |
| Execution mode logic | ✅ Written | suggestion / semi-auto / full-auto |
| Emergency stop | ✅ Written | Halts all strategies |
| Celery signal task | 🔧 In progress | `run_signals` wiring is next milestone |
| Celery schedule | 🔧 Pending | Beat schedule not yet configured |
| WebSocket signal push | 🔧 Pending | Skeleton in place, not broadcasting |

> The execution engine code is complete. The gap is the **Celery task wiring** that loops over active strategies, calls `SignalEngine.run()`, pipes results into `ForwardEngine.process_signal()`, and persists `Trade` records to the database.

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
