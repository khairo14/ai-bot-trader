import pandas as pd
from tools.base import BaseTool, ToolOutput


class RSI(BaseTool):
    """
    Relative Strength Index.
    Measures momentum and identifies overbought / oversold conditions.

    Values:
      < 30  → oversold  (potential long entry)
      > 70  → overbought (potential short entry or exit)
      ~ 50  → neutral
    """
    name = "RSI"

    def calculate(
        self,
        data: pd.DataFrame,
        period: int = 14,
        overbought: float = 70.0,
        oversold: float = 30.0,
    ) -> ToolOutput:
        self.validate_data(data, min_rows=period + 1)

        delta = data["close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)

        avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
        avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()

        rs = avg_gain / avg_loss.replace(0, 1e-10)
        rsi = 100 - (100 / (1 + rs))

        current = float(rsi.iloc[-1])

        if current < oversold:
            signal = "oversold"
            strength = (oversold - current) / oversold
        elif current > overbought:
            signal = "overbought"
            strength = (current - overbought) / (100 - overbought)
        else:
            signal = "neutral"
            strength = abs(current - 50) / 50

        return ToolOutput(
            tool=self.name,
            value=round(current, 2),
            signal=signal,
            strength=round(min(strength, 1.0), 3),
            metadata={"period": period, "overbought": overbought, "oversold": oversold},
        )
