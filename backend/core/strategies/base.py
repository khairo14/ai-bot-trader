from dataclasses import dataclass, field
from typing import Optional, List
import pandas as pd
from abc import ABC, abstractmethod
from loguru import logger

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
    # Trailing stop — if set (e.g. 1.5 = 1.5%), monitor_sl_tp ratchets stop_loss
    # up (long) or down (short) each tick to lock in profit.
    trailing_stop_pct: Optional[float] = None
    # Options-specific fields (None for equity/crypto signals)
    iv_rank: Optional[float] = None          # 0–100: IV Rank from historical volatility
    delta:   Optional[float] = None          # net delta of the position
    theta:   Optional[float] = None          # net daily theta
    vega:    Optional[float] = None          # net vega
    options_meta: Optional[dict] = None      # {strategy_type, expiry, legs, strikes, …}


class BaseStrategy(ABC):
    """
    Base class all strategies must inherit from.
    Implement generate_signal() to define entry/exit logic.
    """

    name: str = "base_strategy"
    description: str = ""
    asset_class: str = "crypto"     # crypto | stock | forex | option
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

    # ── Common signal-quality filters (shared across all strategies) ──────────

    def _enhance_signal(self, signal: "Signal", data: pd.DataFrame,
                        atr_val: Optional[float] = None) -> "Signal":
        """
        Apply three quality filters to any BUY / SHORT signal:
          1. Confirmation candle  — previous closed candle must close in direction of signal
          2. S/R TP snap          — snap take-profit to nearest pivot high/low if closer
          3. Trailing stop        — set trailing_stop_pct = 1.5 × ATR / price
        Options signals (asset_class == 'option') only get the confirmation candle;
        S/R and trailing stop are skipped (premium-based exit mechanics).
        HOLD / SELL / COVER pass through unchanged.
        """
        if signal.signal not in ("BUY", "SHORT"):
            return signal

        is_options = getattr(signal, "asset_class", None) == "option"
        signal_type = signal.signal
        current_price = signal.entry_price
        reasons = list(signal.reasons or [])

        # Feature 3: Confirmation candle
        if len(data) >= 2:
            prev_close = float(data["close"].iloc[-2])
            prev_open  = float(data["open"].iloc[-2])
            if signal_type == "BUY" and prev_close <= prev_open:
                logger.info(
                    f"[{self.name}] {signal.symbol} BUY → HOLD: "
                    f"prev candle bearish (close={prev_close:.5f} <= open={prev_open:.5f})"
                )
                signal.signal         = "HOLD"
                signal.confidence     = 0.0
                signal.stop_loss      = None
                signal.take_profit    = None
                signal.trailing_stop_pct = None
                signal.reasons        = reasons + ["Awaiting confirmation candle (prev candle bearish)"]
                return signal
            elif signal_type == "SHORT" and prev_close >= prev_open:
                logger.info(
                    f"[{self.name}] {signal.symbol} SHORT → HOLD: "
                    f"prev candle bullish (close={prev_close:.5f} >= open={prev_open:.5f})"
                )
                signal.signal         = "HOLD"
                signal.confidence     = 0.0
                signal.stop_loss      = None
                signal.take_profit    = None
                signal.trailing_stop_pct = None
                signal.reasons        = reasons + ["Awaiting confirmation candle (prev candle bullish)"]
                return signal

        # Options strategies: confirmation candle is sufficient — skip the rest
        if is_options:
            return signal

        # Feature 1: S/R TP snap (non-options only)
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

        # Feature 2: Trailing stop (non-options, non-mean-reversion only)
        # BUG-HIGH-01 FIX: mean_reversion_bb is a counter-trend strategy — the target
        # is the middle Bollinger Band, not an open-ended trend.  A trailing stop would
        # ratchet away from that fixed target and prevent the trade from reaching it.
        # Options signals also skip trailing stop (handled above via is_options guard).
        _is_mean_rev = getattr(self, "name", "") == "mean_reversion_bb"
        if atr_val and current_price and not _is_mean_rev:
            signal.trailing_stop_pct = round(atr_val * 1.5 / current_price * 100, 4)

        return signal

    def _find_resistance(
        self, data: pd.DataFrame, current_price: float,
        window: int = 5, lookback: int = 100,
    ) -> Optional[float]:
        """Nearest pivot high strictly above current_price, or None."""
        h = data["high"].values[-lookback:]
        candidates: list = []
        for i in range(window, len(h) - window):
            if (h[i] > current_price
                    and all(h[i] >= h[i - window: i])
                    and all(h[i] >= h[i + 1: i + window + 1])):
                candidates.append(float(h[i]))
        return min(candidates) if candidates else None

    def _find_support(
        self, data: pd.DataFrame, current_price: float,
        window: int = 5, lookback: int = 100,
    ) -> Optional[float]:
        """Nearest pivot low strictly below current_price, or None."""
        lo = data["low"].values[-lookback:]
        candidates: list = []
        for i in range(window, len(lo) - window):
            if (lo[i] < current_price
                    and all(lo[i] <= lo[i - window: i])
                    and all(lo[i] <= lo[i + 1: i + window + 1])):
                candidates.append(float(lo[i]))
        return max(candidates) if candidates else None
