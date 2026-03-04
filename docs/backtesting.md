# Backtesting Engine

## Overview

The backtesting engine replays historical market data through your strategy as if trades were being placed in real time. It measures performance accurately by simulating slippage, fees, and realistic fill conditions.

**Goal:** Validate that a strategy is profitable on historical data before risking any real or paper money.

---

## Backtesting Workflow

```
1. Select strategy + parameters
        ↓
2. Select symbol(s) + timeframe + date range
        ↓
3. Engine replays historical OHLCV candle by candle
        ↓
4. At each candle: tools run → strategy evaluates → signal generated
        ↓
5. If signal fires: simulate order fill (with slippage + fees)
        ↓
6. Track position, P&L, drawdown in real time
        ↓
7. At the end: generate full performance report
        ↓
8. Optionally run Walk-Forward Test for overfitting check
```

---

## Backtest Configuration

| Parameter | Description | Default |
|---|---|---|
| `symbol` | Trading pair (e.g. BTC/USDT, AAPL) | — |
| `timeframe` | Candle size (1m, 5m, 15m, 1h, 4h, 1d) | `1h` |
| `start_date` | Backtest start date | 2 years ago |
| `end_date` | Backtest end date | Yesterday |
| `initial_capital` | Starting account balance | `10,000 USD` |
| `commission` | Fee per trade (as % of trade value) | `0.1%` (Binance) |
| `slippage` | Estimated fill slippage (as % of price) | `0.05%` |
| `position_size_pct` | % of capital per trade | `2%` |
| `max_open_positions` | Max simultaneous trades | `3` |

---

## Performance Metrics

After every backtest, a full report is generated with the following metrics:

### Return Metrics
| Metric | Description |
|---|---|
| **Total Return %** | Overall profit/loss as % of starting capital |
| **Annualized Return %** | Return scaled to a 1-year period |
| **CAGR** | Compound Annual Growth Rate |
| **Profit Factor** | Gross profit / Gross loss (>1.5 is good, >2.0 is excellent) |

### Risk Metrics
| Metric | Description |
|---|---|
| **Max Drawdown %** | Largest peak-to-trough decline during the period |
| **Max Drawdown Duration** | Longest time spent in drawdown |
| **Sharpe Ratio** | Return per unit of risk (>1.0 acceptable, >2.0 strong) |
| **Sortino Ratio** | Like Sharpe but only penalizes downside volatility |
| **Calmar Ratio** | Annualized return / Max drawdown |

### Trade Metrics
| Metric | Description |
|---|---|
| **Total Trades** | Number of round-trip trades |
| **Win Rate %** | % of trades that were profitable |
| **Average Win** | Average profit of winning trades |
| **Average Loss** | Average loss of losing trades |
| **Risk/Reward Ratio** | Average win / Average loss (>1.5 is good) |
| **Longest Winning Streak** | Most consecutive profitable trades |
| **Longest Losing Streak** | Most consecutive losing trades |
| **Average Trade Duration** | How long positions are typically held |

### Monthly / Yearly Breakdown
A heatmap showing monthly returns across years. Identifies seasonal patterns or problematic months.

```
        Jan    Feb    Mar    Apr    May    Jun   ...
2024   +3.2%  -1.1%  +5.4%  +2.0%  -3.2%  +1.8%
2025   +1.5%  +4.2%  -0.8%  +3.3%  +2.1%  -2.0%
```

---

## Walk-Forward Testing (Overfitting Prevention)

Standard backtesting can produce misleadingly good results because the strategy was designed using the same data it's being tested on (overfitting). Walk-forward testing prevents this.

### How It Works

```
Full historical data (e.g. 2 years)
         ↓
Split into rolling windows

Window 1:  [Train: Jan–Sep 2024] → [Test: Oct–Dec 2024]
Window 2:  [Train: Apr–Dec 2024] → [Test: Jan–Mar 2025]
Window 3:  [Train: Jul 2024–Mar 2025] → [Test: Apr–Jun 2025]
         ↓
Strategy only "sees" training data
         ↓
Performance measured only on test data
         ↓
Results averaged across all test windows
```

**If walk-forward results are close to standard backtest results → strategy is robust.**
**If walk-forward results are much worse → strategy was overfitted to history.**

### Walk-Forward Configuration

| Parameter | Default | Description |
|---|---|---|
| `train_ratio` | 0.75 | % of window used for training |
| `test_ratio` | 0.25 | % of window used for testing |
| `window_size` | 90 days | Length of each window |
| `step_size` | 30 days | How far to advance each window |

---

## Multi-Asset Backtesting

Run a single strategy across multiple symbols simultaneously:
- Results per symbol
- Combined portfolio view (correlation-adjusted)
- Identifies which markets the strategy works best on

---

## Benchmark Comparison

Every backtest is compared against a benchmark:
- **Crypto:** Buy-and-hold BTC
- **Stocks:** Buy-and-hold SPY (S&P 500 ETF)
- **Options:** Risk-free rate (T-bills)

If the bot doesn't beat buy-and-hold after fees and risk adjustment, the strategy needs improvement.

---

## Backtest Quality Checklist

Before promoting a strategy to forward test, verify:

```
[ ] Profit Factor > 1.5
[ ] Max Drawdown < 20%
[ ] Win Rate > 45% OR Risk/Reward > 2.0
[ ] Sharpe Ratio > 1.0
[ ] Walk-forward results within 20% of standard backtest
[ ] Tested on minimum 1 year of data
[ ] Tested across at least 2 different market regimes (bull + bear)
[ ] Total trades > 50 (ensures statistical significance)
[ ] Results include realistic fees (0.1% per trade) and slippage
```

---

## From Backtest to Forward Test

Once a strategy passes the backtest quality checklist:

```
Backtest passes all checks
        ↓
Enable in Forward Test (paper trading mode)
        ↓
Run for minimum 30 trading days on real-time data
        ↓
Compare live win rate / drawdown vs backtest results
        ↓
If within acceptable variance → promote to live trading
If not                        → investigate and revise
```

See [execution.md](execution.md) for forward test and live execution details.
