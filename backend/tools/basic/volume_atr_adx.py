import pandas as pd
from tools.base import BaseTool, ToolOutput


class VolumeAnalysis(BaseTool):
    """
    Volume Analysis.
    Confirms price moves: high volume = valid signal, low volume = weak.

    Signals:
      spike    → Volume > 2x average (confirms breakout or reversal)
      above    → Volume above average (healthy move)
      low      → Volume below 0.5x average (unconfirmed move, caution)
      neutral  → Normal volume range
    """
    name = "VolumeAnalysis"

    def calculate(
        self,
        data: pd.DataFrame,
        period: int = 20,
        spike_threshold: float = 2.0,
        low_threshold: float = 0.5,
    ) -> ToolOutput:
        self.validate_data(data, min_rows=period)

        avg_volume = data["volume"].rolling(window=period).mean()
        current_volume = float(data["volume"].iloc[-1])
        current_avg = float(avg_volume.iloc[-1])

        ratio = current_volume / current_avg if current_avg > 0 else 1.0

        if ratio >= spike_threshold:
            signal = "spike"
            strength = min((ratio - 1) / spike_threshold, 1.0)
        elif ratio >= 1.0:
            signal = "above"
            strength = (ratio - 1.0) / (spike_threshold - 1.0)
        elif ratio < low_threshold:
            signal = "low"
            strength = (low_threshold - ratio) / low_threshold
        else:
            signal = "neutral"
            strength = 0.0

        return ToolOutput(
            tool=self.name,
            value=round(ratio, 3),
            signal=signal,
            strength=round(min(strength, 1.0), 3),
            metadata={
                "current_volume": current_volume,
                "avg_volume": round(current_avg, 2),
                "ratio": round(ratio, 3),
                "period": period,
            },
        )


class ATR(BaseTool):
    """
    Average True Range.
    Measures volatility. Used for stop loss placement and position sizing.

    Stop loss formula: entry_price ± (ATR × multiplier)
    Typical multiplier: 1.5x – 2.5x
    """
    name = "ATR"

    def calculate(
        self,
        data: pd.DataFrame,
        period: int = 14,
    ) -> ToolOutput:
        self.validate_data(data, min_rows=period + 1)

        high = data["high"]
        low = data["low"]
        close = data["close"]

        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)

        atr = tr.ewm(span=period, adjust=False).mean()

        current_atr = float(atr.iloc[-1])
        current_close = float(close.iloc[-1])
        atr_pct = current_atr / current_close * 100  # as % of price

        # High volatility if ATR > 3% of price
        # Low volatility threshold: 0.2% (not 0.5%).
        # Typical 5m crypto candle ATR is 0.1-0.3% of price in calm markets;
        # using 0.5% caused ALL symbols to be flagged low_volatility during
        # normal quiet periods, permanently blocking the ATR scoring point.
        if atr_pct > 3.0:
            signal = "high_volatility"
            strength = min(atr_pct / 6.0, 1.0)
        elif atr_pct < 0.2:
            signal = "low_volatility"
            strength = 1.0 - (atr_pct / 0.2)
        else:
            signal = "normal"
            strength = atr_pct / 3.0

        return ToolOutput(
            tool=self.name,
            value=round(current_atr, 4),
            signal=signal,
            strength=round(min(strength, 1.0), 3),
            metadata={
                "atr": round(current_atr, 4),
                "atr_pct": round(atr_pct, 4),
                "period": period,
            },
        )


class ADX(BaseTool):
    """
    Average Directional Index.
    Measures trend STRENGTH (not direction).

    Values:
      < 20  → Weak / ranging market
      20-25 → Developing trend
      25-50 → Strong trend
      > 50  → Very strong trend (potentially exhausted)
    """
    name = "ADX"

    def calculate(
        self,
        data: pd.DataFrame,
        period: int = 14,
    ) -> ToolOutput:
        self.validate_data(data, min_rows=period * 2)

        high = data["high"]
        low = data["low"]
        close = data["close"]

        plus_dm = high.diff().clip(lower=0)
        minus_dm = (-low.diff()).clip(lower=0)
        plus_dm[plus_dm < minus_dm] = 0
        minus_dm[minus_dm < plus_dm] = 0

        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)

        atr = tr.ewm(span=period, adjust=False).mean()
        plus_di = 100 * plus_dm.ewm(span=period, adjust=False).mean() / atr
        minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
        adx = dx.ewm(span=period, adjust=False).mean()

        current = float(adx.iloc[-1])
        current_plus = float(plus_di.iloc[-1])
        current_minus = float(minus_di.iloc[-1])

        if current > 50:
            signal = "very_strong_trend"
        elif current > 25:
            signal = "strong_trend"
        elif current > 20:
            signal = "developing_trend"
        else:
            signal = "ranging"

        return ToolOutput(
            tool=self.name,
            value=round(current, 2),
            signal=signal,
            strength=round(min(current / 50, 1.0), 3),
            metadata={
                "adx": round(current, 2),
                "plus_di": round(current_plus, 2),
                "minus_di": round(current_minus, 2),
                "period": period,
                "direction": "bullish" if current_plus > current_minus else "bearish",
            },
        )
