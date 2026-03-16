"""
BaseScalpingStrategy
====================
Base class for all scalping strategies.

Key differences vs BaseStrategy._enhance_signal():
  1. NO confirmation candle filter — that filter adds a full 1-candle lag,
     which is fatal for sub-5-minute strategies (the move is already over).
  2. NO trailing stop — scalps use a fixed tight stop; a trailing stop would
     ratchet into the TP zone and prevent the trade from reaching its target.
  3. S/R TP snap is opt-in (sr_tp_snap_enabled = True on the subclass).
     Off by default because at 5m timeframes pivot pivots calculated on
     100-candle lookbacks are often too coarse to be useful.

Everything else (Signal dataclass, _find_resistance, _find_support, abstract
generate_signal) is inherited unchanged from BaseStrategy.
"""

from typing import Optional
import pandas as pd

from core.strategies.base import BaseStrategy, Signal


class BaseScalpingStrategy(BaseStrategy):
    """
    Abstract base for scalping strategies.

    Subclasses must implement ``generate_signal()`` and should call
    ``self._enhance_signal(signal, data, atr_val)`` on their output
    signal so this override is applied.

    Class-level attribute to override in subclass:
        sr_tp_snap_enabled: bool = True  # enable S/R TP snapping
    """

    name: str = "base_scalping"
    sr_tp_snap_enabled: bool = False  # off by default for scalping

    def _enhance_signal(
        self,
        signal: Signal,
        data: pd.DataFrame,
        atr_val: Optional[float] = None,
    ) -> Signal:
        """
        Scalping-safe signal quality filter.

        Intentionally skips:
        - Confirmation candle check  (adds 1-candle lag)
        - Trailing stop assignment   (fixed stops only for scalping)

        Optionally applies:
        - S/R TP snap (only when sr_tp_snap_enabled = True on the subclass)
        """
        if signal.signal not in ("BUY", "SHORT"):
            return signal

        if not self.sr_tp_snap_enabled:
            return signal

        # S/R TP snap — identical logic to BaseStrategy, just extracted here
        signal_type = signal.signal
        current_price = signal.entry_price
        reasons = list(signal.reasons or [])

        if signal.take_profit is not None:
            if signal_type == "BUY":
                resistance = self._find_resistance(data, current_price)
                if resistance is not None and resistance < signal.take_profit:
                    signal.take_profit = round(resistance * 0.9998, 6)
                    signal.reasons = reasons + [f"TP snapped to resistance {resistance:.5f}"]
            elif signal_type == "SHORT":
                support = self._find_support(data, current_price)
                if support is not None and support > signal.take_profit:
                    signal.take_profit = round(support * 1.0002, 6)
                    signal.reasons = reasons + [f"TP snapped to support {support:.5f}"]

        return signal
