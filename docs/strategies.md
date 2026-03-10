# Strategies

## Overview

The strategy layer is the brain of the bot. It combines outputs from the tool library with ML model predictions to generate a final trading signal. Strategies are modular — you can create, enable, disable, and combine them independently.

---

## Signal Output Format

Every strategy produces a standardized signal object:

```json
{
  "symbol": "BTC/USDT",
  "signal": "BUY",           // BUY | SELL | SHORT | COVER | HOLD
  "entry_price": 65200.00,
  "stop_loss": 63500.00,
  "take_profit": 68500.00,
  "confidence": 0.78,        // 0.0 – 1.0, ML probability score
  "timeframe": "1h",
  "strategy_name": "hybrid_macd_rsi",
  "regime": "trending",
  "timestamp": "2026-03-04T12:00:00Z",
  "reasons": ["RSI oversold", "MACD bullish crossover", "ML score 0.78"],
  "trailing_stop_pct": 0.018 // Optional: ATR-derived trailing stop %; null if fixed SL only
}
```

---

## Strategy Architecture

```
Tool Library Output (indicators)
           ↓
  Rule-Based Filter
  (hard conditions that must be met)
           ↓
  ML Model Scorer
  (probability of success, 0.0 – 1.0)
           ↓
  Regime Detector
  (adjusts signal weight by market condition)
           ↓
  Signal Aggregator
  (combines into final BUY/SELL/SHORT/HOLD)
           ↓
  Risk Manager Validation
  (confirms position size, exposure limits)
           ↓
  Final Signal → Execution Engine
```

---

## Execution Modes (Per Strategy)

Each strategy has its own execution mode, switchable at any time from the dashboard:

| Mode | Behavior |
|---|---|
| `suggestion` | Signal shown on dashboard. Human decides to act. |
| `semi-auto` | Signal shown with a one-click confirm button. Human approves. |
| `full-auto` | Order placed immediately when signal meets all conditions. |

---

## Built-In Strategy Types

### 1. Trend Following
**Best for:** Trending markets (strong directional moves)

**Logic:**
- EMA crossover (fast EMA crosses above slow EMA → bullish)
- MACD line crosses above signal line
- Price above 200 EMA (trend confirmation)
- Volume above 20-period average (confirms move)

```
Entry (LONG):  Fast EMA > Slow EMA + MACD bullish cross + Price > 200EMA + Volume spike
Entry (SHORT): Fast EMA < Slow EMA + MACD bearish cross + Price < 200EMA + Volume spike
Exit:          Opposite crossover OR stop loss hit OR take profit hit
```

---

### 2. Mean Reversion
**Best for:** Ranging / sideways markets

**Logic:**
- RSI extremes (oversold = buy, overbought = sell)
- Price touches lower Bollinger Band (buy) or upper band (sell)
- Price returns to mean (VWAP or midline)

```
Entry (LONG):  RSI < 30 + price touches lower BB + VWAP below price
Entry (SHORT): RSI > 70 + price touches upper BB + VWAP above price
Exit:          RSI returns to 50 OR price reaches midline BB OR stop loss
```

---

### 3. Breakout
**Best for:** Range compression before big moves

**Logic:**
- Price breaks above resistance or below support
- Volume expansion confirms breakout
- ATR (Average True Range) confirms volatility expansion

```
Entry (LONG):  Price breaks above resistance + Volume > 2x average + ATR expanding
Entry (SHORT): Price breaks below support + Volume > 2x average + ATR expanding
Exit:          Measured move target (height of range projected) OR stop loss
```

---

### 4. Options-Specific (IBKR)
**Best for:** Low risk, defined-risk trades on stocks

**Strategies:**
- **Covered Call:** Own stock, sell call above current price (income generation)
- **Cash-Secured Put:** Sell put below price (buy stock cheaper or collect premium)
- **Bull Call Spread:** Buy lower call, sell higher call (limited risk bullish)
- **Iron Condor:** Sell OTM call + put, buy further OTM wings (range-bound premium collection)

**Signal triggers:**
- IV Rank > 50% → favor selling premium (high IV tends to revert)
- IV Rank < 30% → favor buying options (cheap premium)
- Delta targeting: 0.30 delta for short options (30% probability of being ITM)

---

### 5. Hybrid (Default — Recommended)
**Combines rules + ML scoring**

The hybrid strategy is fully wired with live ML inference.

```
Step 1: Rule-based filter (MACD + RSI + ATR)
        → hard conditions must pass or signal is HOLD
Step 2: ML scorer (XGBoost, trained on 365d of OHLCV)
        → returns P(BUY) ∈ [0, 1]
        → BUY vetoed if P < 0.35
        → SHORT vetoed if P > 0.65
Step 3: Blended confidence score
        → confidence = 0.60 × rule_score + 0.40 × ml_score
Step 4: Only signals with blended confidence > threshold are acted on

Thresholds (configurable):
  suggestion mode:  score > 0.55
  semi-auto mode:   score > 0.65
  full-auto mode:   score > 0.72
```

**ML feature set** (6 features, identical between training and inference):
| Feature | Description |
|---|---|
| `rsi` | RSI-14 |
| `macd_hist` | MACD histogram |
| `atr_norm` | ATR-14 normalised by close price |
| `vol_ratio` | Volume / 20-period average volume |
| `bb_pct` | Bollinger Band position (0=lower, 1=upper) |
| `log_ret` | Log return (close / prev close) |

**Training:** Run `ModelTrainer().retrain_all()` to retrain. Models saved to `backend/data/models/`. Registry at `data/models/latest.json`. Models below AUC 0.55 are rejected automatically.

---

## ML Model Layer

### Model Types Used

| Model | Purpose | Best For |
|---|---|---|
| **XGBoost** | Fast, robust classification | Signal probability scoring |
| **LSTM** | Sequential time-series | Price direction over next N candles |
| **Random Forest** | Ensemble, handles noise well | Feature importance + signal filtering |
| **Reinforcement Learning (future)** | Learns from own trades | Continuous self-improvement |

### Feature Set (Input to ML Models)

```
Price Features:
  - Returns (1, 5, 10, 20 periods)
  - OHLC ratios
  - Gap up/down from previous close

Indicator Features:
  - RSI (14), RSI (5)
  - MACD histogram
  - BB width (volatility)
  - EMA(9), EMA(21), EMA(50), EMA(200) slopes
  - ATR (normalized)
  - Volume ratio (current vs 20-period avg)
  - VWAP distance

Market Structure:
  - Higher highs / lower lows detection
  - Support / resistance proximity
  - Trend strength (ADX)

Options Features (IBKR only):
  - IV Rank
  - IV Percentile
  - Put/Call ratio
  - Delta, Theta
```

### Training Pipeline

```
Raw OHLCV data (minimum 2 years)
        ↓
Feature engineering
        ↓
Label generation (was trade profitable at N candles?)
        ↓
Train/Validation/Test split (70/15/15)
        ↓
Model training + hyperparameter tuning
        ↓
Walk-forward validation
        ↓
Out-of-sample test
        ↓
Deploy if performance > baseline
        ↓
Weekly retraining on new data
```

---

## Multi-Timeframe Confluence

Before any non-HOLD signal can reach the execution engine, it must pass a multi-timeframe agreement check. The primary timeframe's signal is compared against the two standard higher timeframes (e.g. 1h → checks 4h + 1d).

```
confluence_score = (agreeing timeframes) / (total timeframes checked)
```

If `confluence_score < min_confluence`, execution is suppressed and the reason is recorded in the signal's reasons list.

---

## Signal Enhancement (`_enhance_signal`)

After a strategy's `generate_signal()` returns a raw signal, `BaseStrategy._enhance_signal()` applies three automatic enhancements before the signal reaches the execution engine:

### 1. Confirmation Candle
Checks that the most recent closed candle confirms the signal direction (bullish close for BUY, bearish close for SHORT/SELL). If the candle disagrees, the signal is downgraded to HOLD with reason `"Confirmation candle not aligned"`.

### 2. Support/Resistance TP Snap
Scans nearby historical swing highs/lows. If the computed take profit is within 0.5% of a significant S/R level, the TP is snapped to that level. This prevents the bot from placing a TP that will frustratingly fail within a few ticks of a major level.

### 3. ATR-Based Dynamic Trailing Stop
If the strategy's `parameters["trailing_stop_pct"]` is set, `_enhance_signal` recomputes it using current ATR:
```python
dynamic_trailing = (ATR-14 * 1.5) / entry_price
sig.trailing_stop_pct = dynamic_trailing
```
This makes the trailing distance adaptive to current volatility rather than fixed. The computed value is stored in `sig.trailing_stop_pct` and also recorded in `TradeOutcome.trailing_stop_pct` for ML training continuity.

---

## Per-Strategy Confluence Defaults

| Strategy | Default `min_confluence` | Rationale |
|---|---|---|
| `hybrid_macd_rsi` | `0.5` | Trend-following — benefits from higher-TF alignment |
| `momentum_breakout` | `0.5` | Breakouts confirm better when multiple TFs agree |
| `mean_reversion_bb` | `0.0` | **Counter-trend by design** — always disagrees with higher TFs; bypass entirely |
| `covered_call` | `0.0` | **Income strategy** — SELL signal by design; higher-TF HOLD would suppress every trade |
| `iron_condor` | `0.0` | **Range-bound income** — same reason as covered call |
| `bull_call_spread` | `0.0` | **Defined-risk directional** — direction encoded in signal, not confluence |

The threshold is a three-level lookup:
1. `strategy.parameters["min_confluence"]` — DB-level override (highest priority)
2. `_STRATEGY_CONFLUENCE_DEFAULTS[strategy_type]` — per-strategy default
3. `MIN_CONFLUENCE` (0.5) — global fallback

When `min_confluence == 0.0`, the higher-TF API calls are skipped entirely for performance.

---

## Regime Detection

The regime detector runs continuously and classifies the current market state. Strategies are weighted accordingly.

| Regime | Detection Method | Strategy Weights |
|---|---|---|
| `trending_up` | ADX > 25, Price > EMA200, slope positive | Trend following ↑, Mean reversion ↓ |
| `trending_down` | ADX > 25, Price < EMA200, slope negative | Trend following (short) ↑, Mean reversion ↓ |
| `ranging` | ADX < 20, price bouncing between levels | Mean reversion ↑, Breakout standby ↑ |
| `volatile` | ATR > 2x 20-period avg, IV spike | Reduce position sizing, widen stops |
| `breakout` | Volume spike + ATR expansion + level breach | Breakout strategy ↑ |

---

## Creating a Custom Strategy

1. Create a new file in `backend/core/strategies/`
2. Inherit from `BaseStrategy` in `backend/core/strategies/base.py`
3. Implement `generate_signal(data, tools)` method
4. Return a `Signal` object
5. Register in `backend/core/strategies/__init__.py`
6. Enable from the dashboard under **Tools → Custom Strategies**

---

## Backtesting Your Strategy

See [backtesting.md](backtesting.md) for full instructions on validating any strategy before enabling it in forward test or live mode.
