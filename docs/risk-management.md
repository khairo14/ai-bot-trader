# Risk Management

## Philosophy

> The goal is not to maximize wins. The goal is to survive long enough for the edge to play out.

A single catastrophic loss can erase months of gains. Risk management is the most important module in the entire system. It runs before every order is placed and cannot be bypassed — not even in full-auto mode.

---

## Risk Hierarchy

```
Level 1: Per-Trade Risk         → Max loss per single trade
Level 2: Per-Strategy Risk      → Max daily loss per strategy
Level 3: Portfolio Risk         → Max total open exposure
Level 4: Daily Circuit Breaker  → Max total loss in one day
Level 5: Emergency Stop         → Manual or auto halt of all activity
```

Every order must pass all levels before execution.

---

## Level 1: Per-Trade Risk (Position Sizing)

### Fixed Fractional Method (Default)
Risk a fixed percentage of current account balance per trade.

```
Position Size Formula:
  risk_amount     = account_balance × risk_per_trade_pct
  stop_distance   = abs(entry_price - stop_loss_price)
  position_size   = risk_amount / stop_distance

Example:
  account_balance     = $10,000
  risk_per_trade_pct  = 2%          → risk_amount = $200
  entry_price         = $65,200
  stop_loss           = $63,500     → stop_distance = $1,700
  position_size       = $200 / $1,700 = 0.117 BTC
```

If the trade loses and stop is hit: **max loss = $200 = 2% of account.**

### Configuration

| Parameter | Default | Recommended Range |
|---|---|---|
| `risk_per_trade_pct` | 2% | 1% – 3% |
| `max_position_size_pct` | 10% | 5% – 20% |
| `min_stop_distance_pct` | 0.5% | Prevents stop too close to entry |

### Stop Loss Placement
Stop loss is always required. No signal without a defined stop will be executed.

| Method | Description |
|---|---|
| **ATR-based** | `stop = entry ± (ATR × multiplier)`. Default multiplier: 2.0x |
| **Structure-based** | Stop placed below recent swing low (long) or above swing high (short) |
| **Fixed %** | `stop = entry × (1 - stop_pct)`. Simple but less adaptive |

---

## Level 2: Per-Strategy Daily Risk Limit

Each strategy has its own daily loss limit. If hit, that strategy pauses for the rest of the day.

| Parameter | Default |
|---|---|
| `max_daily_loss_per_strategy_pct` | 5% of capital assigned to strategy |
| `max_consecutive_losses` | 3 (pause after 3 losses in a row) |

**Consecutive loss logic:** After 3 consecutive losses, the strategy pauses for 4 hours and waits for a higher-confidence signal on the next attempt.

---

## Level 3: Portfolio Exposure Limits

| Parameter | Default | Description |
|---|---|---|
| `max_open_positions` | 5 | Max simultaneous trades across all strategies |
| `max_exposure_per_asset` | 15% | Max % of total capital in one symbol |
| `max_exposure_per_class` | 40% | Max % in one asset class (crypto, stocks, options) |
| `max_correlated_positions` | 2 | Limit on highly correlated assets open simultaneously |

**Correlation check:** If BTC and ETH are both open (high correlation), the bot will not open a third crypto position until one is closed.

---

## Level 4: Daily Circuit Breaker

The most important safety net. If total account loses more than the configured % in a single day, **all trading halts** and no new orders are placed until manually reset.

| Parameter | Default |
|---|---|
| `daily_circuit_breaker_pct` | 5% |

```
Example:
  Account: $10,000
  Circuit breaker: 5% = $500 max daily loss

  If total P&L for the day hits -$500:
    → All strategies paused immediately
    → All pending orders cancelled
    → Open positions maintained (not force-closed)
    → Alert sent to dashboard + notifications
    → Manual reset required to resume
```

The circuit breaker does **not** force-close open positions (to avoid locking in losses unnecessarily). It only stops new orders.

---

## Level 5: Emergency Stop

Available from the dashboard at all times. One click:
1. All strategies immediately paused
2. All pending/open orders cancelled
3. All open positions closed at market price
4. Bot enters locked state
5. Manual unlock required to resume

**Keyboard shortcut in dashboard:** `Ctrl + Shift + E`

---

## Take Profit Rules

| Method | Description |
|---|---|
| **Fixed target** | `take_profit = entry × (1 + target_pct)` |
| **Risk/Reward ratio** | `take_profit = entry + (stop_distance × RR_ratio)`. Default: 2.0x |
| **Trailing stop** | Stop follows price upward, locks in gains as price rises |
| **Partial exits** | Close 50% at first target, move stop to breakeven, let rest run |

**Minimum Risk/Reward:** No trade is taken with R:R below 1.5. Signal with good probability but poor R:R is rejected.

---

## Options-Specific Risk Rules (IBKR)

| Rule | Value |
|---|---|
| Max risk on any options trade | Defined-risk trades only (spreads preferred over naked options) |
| Max % of account in options | 25% |
| Theta decay management | Close any short option at 21 DTE (days to expiration) |
| Assignment risk | Alert at 80 Delta on short options, auto-close if > 85 Delta |
| Earnings risk | No new options positions 2 days before / after earnings |

---

## Risk Parameters Configuration

All risk parameters live in `.env` and can also be adjusted from the dashboard under **Settings → Risk Management**:

```env
RISK_PER_TRADE_PCT=2.0
MAX_OPEN_POSITIONS=5
MAX_DAILY_LOSS_PCT=5.0
DAILY_CIRCUIT_BREAKER_PCT=5.0
MAX_EXPOSURE_PER_ASSET_PCT=15.0
MAX_EXPOSURE_PER_CLASS_PCT=40.0
DEFAULT_RR_RATIO=2.0
ATR_STOP_MULTIPLIER=2.0
MAX_CONSECUTIVE_LOSSES=3
```

---

## Risk Report

The dashboard shows a live risk summary:

```
┌─────────────────────────────────────────────────┐
│                  RISK SUMMARY                   │
│                                                 │
│  Account Balance:     $10,247.50                │
│  Available Capital:   $9,012.00                 │
│  Open Exposure:       $1,235.50 (12.1%)         │
│                                                 │
│  Daily P&L:           +$247.50 (+2.5%)          │
│  Daily Loss Limit:    -$500.00 (5%)  ← at 0%   │
│                                                 │
│  Open Positions:      2 / 5                     │
│  Crypto Exposure:     8.5%  / 40%               │
│  Stocks Exposure:     3.6%  / 40%               │
│                                                 │
│  Circuit Breaker:     ACTIVE (GREEN)            │
└─────────────────────────────────────────────────┘
```
