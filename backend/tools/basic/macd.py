import pandas as pd
from tools.base import BaseTool, ToolOutput


class MACD(BaseTool):
    """
    Moving Average Convergence Divergence.
    Trend direction and momentum; crossovers signal entry/exit.

    Signals:
      bullish_crossover  → MACD crosses above signal line (BUY)
      bearish_crossover  → MACD crosses below signal line (SELL/SHORT)
      histogram_positive → Bullish momentum
      histogram_negative → Bearish momentum
    """
    name = "MACD"

    def calculate(
        self,
        data: pd.DataFrame,
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9,
    ) -> ToolOutput:
        self.validate_data(data, min_rows=slow_period + signal_period)

        fast_ema = data["close"].ewm(span=fast_period, adjust=False).mean()
        slow_ema = data["close"].ewm(span=slow_period, adjust=False).mean()
        macd_line = fast_ema - slow_ema
        signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
        histogram = macd_line - signal_line

        current_hist = float(histogram.iloc[-1])
        prev_hist = float(histogram.iloc[-2])
        current_macd = float(macd_line.iloc[-1])
        current_signal = float(signal_line.iloc[-1])

        # Detect crossover
        prev_macd = float(macd_line.iloc[-2])
        prev_signal_val = float(signal_line.iloc[-2])

        if prev_macd < prev_signal_val and current_macd > current_signal:
            signal = "bullish_crossover"
            strength = min(abs(current_hist) / (abs(current_hist) + 0.001), 1.0)
        elif prev_macd > prev_signal_val and current_macd < current_signal:
            signal = "bearish_crossover"
            strength = min(abs(current_hist) / (abs(current_hist) + 0.001), 1.0)
        elif current_hist > 0:
            signal = "histogram_positive"
            strength = min(current_hist / (abs(current_macd) + 0.001), 1.0)
        else:
            signal = "histogram_negative"
            strength = min(abs(current_hist) / (abs(current_macd) + 0.001), 1.0)

        return ToolOutput(
            tool=self.name,
            value=round(current_hist, 6),
            signal=signal,
            strength=round(min(abs(strength), 1.0), 3),
            metadata={
                "macd_line": round(current_macd, 6),
                "signal_line": round(float(signal_line.iloc[-1]), 6),
                "histogram": round(current_hist, 6),
                "fast_period": fast_period,
                "slow_period": slow_period,
                "signal_period": signal_period,
            },
        )
