import pandas as pd
from tools.base import BaseTool, ToolOutput


class BollingerBands(BaseTool):
    """
    Bollinger Bands.
    Volatility measurement and mean reversion signals.

    Signals:
      lower_touch  → Price near lower band (potential LONG)
      upper_touch  → Price near upper band (potential SHORT/exit)
      squeeze      → Bands narrowing, breakout incoming
      expansion    → Bands widening, strong trend
      neutral      → Price within bands
    """
    name = "BollingerBands"

    def calculate(
        self,
        data: pd.DataFrame,
        period: int = 20,
        std_dev: float = 2.0,
    ) -> ToolOutput:
        self.validate_data(data, min_rows=period)

        sma = data["close"].rolling(window=period).mean()
        std = data["close"].rolling(window=period).std()

        upper = sma + std_dev * std
        lower = sma - std_dev * std
        width = (upper - lower) / sma  # normalized band width

        current_close = float(data["close"].iloc[-1])
        current_upper = float(upper.iloc[-1])
        current_lower = float(lower.iloc[-1])
        current_mid = float(sma.iloc[-1])
        current_width = float(width.iloc[-1])
        prev_width = float(width.iloc[-2])

        # Band position: 0 = at lower, 1 = at upper
        band_range = current_upper - current_lower
        band_pct = (current_close - current_lower) / band_range if band_range > 0 else 0.5

        # Touch threshold: within 2% of band
        touch_threshold = 0.02

        if band_pct < touch_threshold:
            signal = "lower_touch"
            strength = 1 - band_pct
        elif band_pct > (1 - touch_threshold):
            signal = "upper_touch"
            strength = band_pct
        elif current_width < prev_width * 0.95:
            signal = "squeeze"
            strength = (prev_width - current_width) / prev_width
        elif current_width > prev_width * 1.05:
            signal = "expansion"
            strength = (current_width - prev_width) / prev_width
        else:
            signal = "neutral"
            strength = abs(band_pct - 0.5)

        return ToolOutput(
            tool=self.name,
            value=round(band_pct, 4),
            signal=signal,
            strength=round(min(strength, 1.0), 3),
            metadata={
                "upper": round(current_upper, 4),
                "middle": round(current_mid, 4),
                "lower": round(current_lower, 4),
                "band_width": round(current_width, 4),
                "band_pct": round(band_pct, 4),
                "period": period,
                "std_dev": std_dev,
            },
        )
