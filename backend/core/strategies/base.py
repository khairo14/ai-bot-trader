from dataclasses import dataclass, field
from typing import Optional, List
import pandas as pd
from abc import ABC, abstractmethod

from tools.base import ToolOutput


@dataclass
class Signal:
    symbol: str
    signal: str              # BUY | SELL | SHORT | COVER | HOLD
    entry_price: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    confidence: float        # 0.0 – 1.0
    timeframe: str
    strategy_name: str
    asset_class: str
    broker: str
    regime: Optional[str] = None
    reasons: List[str] = field(default_factory=list)


class BaseStrategy(ABC):
    """
    Base class all strategies must inherit from.
    Implement generate_signal() to define entry/exit logic.
    """

    name: str = "base_strategy"
    description: str = ""
    asset_class: str = "crypto"     # crypto | stock | option
    broker: str = "binance"

    @abstractmethod
    def generate_signal(
        self,
        data: pd.DataFrame,
        symbol: str,
        timeframe: str,
        tool_outputs: Optional[dict] = None,
        **kwargs,
    ) -> Signal:
        """
        Core strategy logic.

        Args:
            data: OHLCV DataFrame
            symbol: Trading pair or ticker
            timeframe: Candle timeframe
            tool_outputs: Dict of {tool_name: ToolOutput} from tool library
            **kwargs: Additional strategy-specific params

        Returns:
            Signal object with entry, stop, target, and confidence
        """
        ...

    def get_default_tools(self) -> List[str]:
        """Return the list of tool names this strategy uses by default."""
        return ["RSI", "MACD", "BollingerBands", "MovingAverages", "VolumeAnalysis", "ATR", "ADX"]
