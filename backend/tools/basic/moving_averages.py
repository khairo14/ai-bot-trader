import pandas as pd
from tools.base import BaseTool, ToolOutput


class MovingAverages(BaseTool):
    """
    EMA / SMA crossover analysis.
    Trend direction and dynamic support/resistance.

    Common setups:
      EMA(9) / EMA(21)   — short-term momentum
      EMA(21) / EMA(50)  — medium-term trend
      EMA(50) / EMA(200) — golden cross / death cross
    """
    name = "MovingAverages"

    def calculate(
        self,
        data: pd.DataFrame,
        fast_period: int = 9,
        slow_period: int = 21,
        ma_type: str = "ema",   # "ema" or "sma"
    ) -> ToolOutput:
        self.validate_data(data, min_rows=slow_period + 1)

        if ma_type == "ema":
            fast = data["close"].ewm(span=fast_period, adjust=False).mean()
            slow = data["close"].ewm(span=slow_period, adjust=False).mean()
        else:
            fast = data["close"].rolling(window=fast_period).mean()
            slow = data["close"].rolling(window=slow_period).mean()

        current_fast = float(fast.iloc[-1])
        current_slow = float(slow.iloc[-1])
        prev_fast = float(fast.iloc[-2])
        prev_slow = float(slow.iloc[-2])

        distance_pct = (current_fast - current_slow) / current_slow

        if prev_fast < prev_slow and current_fast > current_slow:
            signal = "golden_cross"
            strength = abs(distance_pct) * 10
        elif prev_fast > prev_slow and current_fast < current_slow:
            signal = "death_cross"
            strength = abs(distance_pct) * 10
        elif current_fast > current_slow:
            signal = "bullish"
            strength = min(abs(distance_pct) * 5, 1.0)
        else:
            signal = "bearish"
            strength = min(abs(distance_pct) * 5, 1.0)

        return ToolOutput(
            tool=self.name,
            value=round(distance_pct * 100, 4),  # % distance between MAs
            signal=signal,
            strength=round(min(strength, 1.0), 3),
            metadata={
                "fast_ma": round(current_fast, 4),
                "slow_ma": round(current_slow, 4),
                "fast_period": fast_period,
                "slow_period": slow_period,
                "ma_type": ma_type,
                "distance_pct": round(distance_pct * 100, 4),
            },
        )
