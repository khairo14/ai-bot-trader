# Tool Library

## Overview

Tools are the building blocks of strategies. Each tool takes OHLCV data (and optionally orderbook or options data) and returns a structured output consumed by the strategy layer.

Tools are organized into three tiers:

| Tier | Description |
|---|---|
| **Basic** | Standard technical indicators. Fast, widely understood, deterministic. |
| **Advanced** | Complex market microstructure and options analytics. Higher signal quality. |
| **Custom** | User-defined indicators, composite signals, ML feature pipelines. |

---

## Tool Output Format

Every tool returns a standardized output:

```json
{
  "tool": "RSI",
  "value": 28.5,
  "signal": "oversold",     // oversold | overbought | neutral | bullish | bearish
  "strength": 0.82,         // 0.0 – 1.0
  "metadata": {
    "period": 14,
    "timeframe": "1h"
  }
}
```

---

## Basic Tools

### RSI — Relative Strength Index
**Purpose:** Measures momentum and identifies overbought/oversold conditions.

| Parameter | Default | Description |
|---|---|---|
| `period` | 14 | Lookback window |
| `overbought` | 70 | Level considered overbought |
| `oversold` | 30 | Level considered oversold |

**Signal logic:**
- `RSI < 30` → `oversold` (potential long entry)
- `RSI > 70` → `overbought` (potential short entry or exit)
- `RSI crosses 50` → trend direction confirmation

---

### MACD — Moving Average Convergence Divergence
**Purpose:** Trend direction and momentum. Crossovers signal entry/exit.

| Parameter | Default | Description |
|---|---|---|
| `fast_period` | 12 | Fast EMA period |
| `slow_period` | 26 | Slow EMA period |
| `signal_period` | 9 | Signal line EMA period |

**Signal logic:**
- **Bullish crossover:** MACD line crosses above signal line → BUY signal
- **Bearish crossover:** MACD line crosses below signal line → SELL/SHORT signal
- **Histogram divergence:** Price makes new high but histogram lower → reversal warning

---

### Bollinger Bands
**Purpose:** Volatility measurement and mean reversion signals.

| Parameter | Default | Description |
|---|---|---|
| `period` | 20 | SMA period for midline |
| `std_dev` | 2.0 | Standard deviation multiplier for bands |

**Signal logic:**
- Price touches **lower band** → potential long entry (mean reversion)
- Price touches **upper band** → potential short entry or exit
- **Band squeeze** (bands narrowing) → breakout incoming, prepare breakout strategy
- **Band expansion** → strong trending move, ride the trend

---

### EMA — Exponential Moving Average
**Purpose:** Trend direction and dynamic support/resistance.

**Common configurations:**
| Cross Type | Description |
|---|---|
| EMA(9) / EMA(21) | Short-term momentum cross |
| EMA(21) / EMA(50) | Medium-term trend cross |
| EMA(50) / EMA(200) | Golden cross / Death cross (major trend) |

**Signal logic:**
- Fast EMA above slow EMA → uptrend
- Fast EMA below slow EMA → downtrend
- Price bouncing off EMA → dynamic support/resistance

---

### SMA — Simple Moving Average
**Purpose:** Smoothed trend reference. Slower to react than EMA, fewer false signals.

**Standard periods:** 20, 50, 100, 200

---

### Volume Analysis
**Purpose:** Confirms price moves. Price move + high volume = valid signal.

**Metrics:**
- Volume ratio: current volume / 20-period average
- **Volume spike** (ratio > 2.0): confirms breakout or reversal
- **Low volume move** (ratio < 0.5): weak signal, likely to fail

---

### ATR — Average True Range
**Purpose:** Measures volatility. Used for stop loss placement and position sizing.

| Parameter | Default |
|---|---|
| `period` | 14 |

**Usage:**
- Stop loss distance = `entry_price ± (ATR × multiplier)`
- Typical multiplier: 1.5x–2.5x ATR
- High ATR = wider stops needed, reduce position size
- Low ATR = tighter stops possible

---

### ADX — Average Directional Index
**Purpose:** Measures trend strength (not direction).

| ADX Value | Interpretation |
|---|---|
| < 20 | Weak trend / ranging market |
| 20–25 | Developing trend |
| 25–50 | Strong trend |
| > 50 | Very strong trend (potentially exhausted) |

---

## Advanced Tools

### VWAP — Volume Weighted Average Price
**Purpose:** Fair value anchor used heavily by institutional traders.

- **Price above VWAP** → bullish intraday bias
- **Price below VWAP** → bearish intraday bias
- Price returning to VWAP after deviation → mean reversion target
- Bands around VWAP (1 std, 2 std) = institutional support/resistance zones

---

### Order Flow Analysis
**Purpose:** Reads the actual buy/sell pressure from the orderbook.

**Metrics:**
- **Delta:** Net buying minus selling pressure per candle
- **Cumulative Delta:** Running total; divergence from price = signal
- **Bid/Ask imbalance:** Large imbalance at a level = likely break through it
- **Absorption:** Large orders absorbing pressure without price moving = reversal

**Data source:** Binance WebSocket orderbook stream, IBKR Level 2 data

---

### Market Profile
**Purpose:** Identifies value areas and high-volume price nodes.

| Concept | Description |
|---|---|
| **POC (Point of Control)** | Price level with most volume traded — acts as magnet |
| **Value Area High (VAH)** | Upper boundary of 70% of volume — resistance |
| **Value Area Low (VAL)** | Lower boundary of 70% of volume — support |

---

### Implied Volatility (IV) Tools (IBKR Options)
**Purpose:** Options pricing efficiency and market fear/greed measure.

| Metric | Description |
|---|---|
| **IV Rank** | Current IV vs its 52-week range (0–100%). >50 = high IV |
| **IV Percentile** | % of days in past year IV was lower than today |
| **IV Surface** | 3D map of IV across strikes and expirations |
| **Skew** | Difference between put IV and call IV at same delta |

**Usage:**
- **IV Rank > 50%** → sell options (high premium, expected reversion)
- **IV Rank < 30%** → buy options (cheap premium)

---

### Options Greeks (IBKR)
**Purpose:** Measure options sensitivity to price, time, and volatility.

| Greek | Description | Usage |
|---|---|---|
| **Delta (Δ)** | Price change per $1 move in underlying | Position sizing, directional exposure |
| **Gamma (Γ)** | Rate of delta change | Risk near expiration |
| **Theta (Θ)** | Daily time decay of option value | Favor positive theta in ranging markets |
| **Vega (ν)** | Sensitivity to IV change | IV plays, earnings trades |
| **Rho (ρ)** | Sensitivity to interest rate change | Less relevant for short-term trades |

---

## Custom Tools

Custom tools allow you to define your own indicators, combine existing ones, or plug in ML feature pipelines.

### Creating a Custom Tool

1. Create a new file in `backend/tools/custom/`
2. Inherit from `BaseTool` in `backend/tools/base.py`
3. Implement the `calculate(data)` method
4. Return a `ToolOutput` object
5. Register in `backend/tools/custom/__init__.py`
6. Enable from the dashboard under **Tools → Custom**

### Example: Composite Momentum Tool

```python
from backend.tools.base import BaseTool, ToolOutput

class CompositeMomentum(BaseTool):
    name = "composite_momentum"

    def calculate(self, data):
        rsi = self.tools.rsi(data, period=14)
        macd = self.tools.macd(data)
        volume = self.tools.volume_ratio(data)

        score = (
            (1 - rsi.value / 100) * 0.4 +  # higher weight on oversold RSI
            (1 if macd.signal == "bullish" else 0) * 0.4 +
            min(volume.value / 3, 1.0) * 0.2
        )

        return ToolOutput(
            tool=self.name,
            value=score,
            signal="bullish" if score > 0.6 else "bearish" if score < 0.4 else "neutral",
            strength=score
        )
```

---

## Tool Registry

All enabled tools are listed in the dashboard under **Tools**. You can:
- Enable / disable any tool globally
- Override default parameters per strategy
- See real-time tool output for any symbol
- View historical tool accuracy (backtest correlation to profitable signals)
