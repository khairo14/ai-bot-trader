import pandas as pd
from tools.base import BaseTool, ToolOutput


class VWAP(BaseTool):
    """
    Volume Weighted Average Price.
    Fair value anchor used heavily by institutional traders.

    Signals:
      price_above → Bullish intraday bias
      price_below → Bearish intraday bias
      near_vwap   → Potential mean reversion to VWAP
    """
    name = "VWAP"

    def calculate(self, data: pd.DataFrame, **params) -> ToolOutput:
        self.validate_data(data, min_rows=5)

        typical_price = (data["high"] + data["low"] + data["close"]) / 3
        vwap = (typical_price * data["volume"]).cumsum() / data["volume"].cumsum()

        current_close = float(data["close"].iloc[-1])
        current_vwap = float(vwap.iloc[-1])

        distance_pct = (current_close - current_vwap) / current_vwap * 100

        if distance_pct > 0.5:
            signal = "price_above"
        elif distance_pct < -0.5:
            signal = "price_below"
        else:
            signal = "near_vwap"

        return ToolOutput(
            tool=self.name,
            value=round(current_vwap, 4),
            signal=signal,
            strength=round(min(abs(distance_pct) / 2, 1.0), 3),
            metadata={
                "vwap": round(current_vwap, 4),
                "current_price": current_close,
                "distance_pct": round(distance_pct, 4),
            },
        )
