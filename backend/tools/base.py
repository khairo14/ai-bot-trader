from dataclasses import dataclass, field
from typing import Optional, Any
import pandas as pd
from abc import ABC, abstractmethod


@dataclass
class ToolOutput:
    tool: str
    value: float
    signal: str          # oversold | overbought | bullish | bearish | neutral
    strength: float      # 0.0 – 1.0
    metadata: dict = field(default_factory=dict)


class BaseTool(ABC):
    """Base class all tools must inherit from."""

    name: str = "base_tool"

    @abstractmethod
    def calculate(self, data: pd.DataFrame, **params) -> ToolOutput:
        """
        Calculate the indicator and return a ToolOutput.
        data: OHLCV DataFrame with columns [open, high, low, close, volume]
        """
        ...

    def validate_data(self, data: pd.DataFrame, min_rows: int = 20):
        if data is None or len(data) < min_rows:
            raise ValueError(f"{self.name}: needs at least {min_rows} candles, got {len(data) if data is not None else 0}")
