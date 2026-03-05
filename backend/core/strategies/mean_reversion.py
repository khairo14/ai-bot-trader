"""
Mean Reversion Bollinger Band Strategy
=======================================
Fades price extremes when they reach the outer Bollinger Bands while RSI
confirms the overextended condition.

Entry logic
-----------
LONG:  BB lower_touch + RSI < 35 + volume confirmation
SHORT: BB upper_touch + RSI > 65 + volume confirmation

Risk
----
Stop-loss:   entry ± 1.0 × ATR               (just beyond the extreme)
Take-profit: middle Bollinger Band            (mean reversion target)
"""

import pandas as pd
from typing import Optional
from loguru import logger

from core.strategies.base import BaseStrategy, Signal
from core.regime_classifier import regime_classifier
from tools.basic.bollinger_bands import BollingerBands
from tools.basic.rsi import RSI
from tools.basic.volume_atr_adx import VolumeAnalysis, ATR


class MeanReversionBBStrategy(BaseStrategy):
    """Bollinger Band mean-reversion strategy."""

    name = "mean_reversion_bb"
    description = (
        "Fades oversold/overbought extremes using Bollinger Band touches confirmed by RSI "
        "and volume. Takes profit at the middle band (mean reversion)."
    )
    asset_class = "crypto"
    broker = "binance"

    # Strategy parameters
    RSI_OVERSOLD: float = 35.0
    RSI_OVERBOUGHT: float = 65.0
    ATR_STOP_MULT: float = 1.0

    def __init__(self):
        self.bb = BollingerBands()
        self.rsi_tool = RSI()
        self.vol = VolumeAnalysis()
        self.atr = ATR()

    def generate_signal(
        self,
        data: pd.DataFrame,
        symbol: str,
        timeframe: str = "1h",
        tool_outputs: Optional[dict] = None,
        **kwargs,
    ) -> Signal:
        _hold = lambda reasons: Signal(
            symbol=symbol, signal="HOLD",
            entry_price=float(data["close"].iloc[-1]),
            stop_loss=None, take_profit=None, confidence=0.0,
            timeframe=timeframe, strategy_name=self.name,
            asset_class=self.asset_class, broker=self.broker,
            reasons=reasons,
        )

        if len(data) < 30:
            return _hold([f"Insufficient data ({len(data)} < 30 candles)"])

        # ── Regime classification ─────────────────────────────────────────
        regime_result = regime_classifier.classify(data)
        regime_name   = regime_result.regime
        atr_mults     = regime_classifier.atr_multipliers(regime_name)
        # Mean-reversion works best in ranging / low-vol; suppress in strong trends
        from core.regime_classifier import REGIME_TRENDING_UP, REGIME_TRENDING_DOWN
        if regime_name in (REGIME_TRENDING_UP, REGIME_TRENDING_DOWN):
            return _hold([f"Regime '{regime_name}' — mean-reversion suppressed in trend"])

        # ── Compute indicators ──────────────────────────────────────────────
        try:
            bb_out = self.bb.calculate(data, period=20, std_dev=2.0)
            rsi_out = self.rsi_tool.calculate(data, period=14)
            vol_out = self.vol.calculate(data)
            atr_out = self.atr.calculate(data)
        except Exception as exc:
            logger.warning(f"[{self.name}] tool error: {exc}")
            return _hold([f"Tool error: {exc}"])

        current_price = float(data["close"].iloc[-1])
        atr_val = atr_out.value
        mid_band = bb_out.metadata.get("middle", current_price)
        rsi_val = rsi_out.value

        # ── Signal logic ─────────────────────────────────────────────────────
        at_lower = bb_out.signal == "lower_touch"
        at_upper = bb_out.signal == "upper_touch"
        vol_ok = vol_out.signal in ("spike", "above")

        reasons: list[str] = []

        if at_lower and rsi_val < self.RSI_OVERSOLD:
            score = 2  # BB touch + RSI
            reasons = [
                f"BB lower touch (band_pct={bb_out.value:.3f})",
                f"RSI oversold ({rsi_val:.1f})",
            ]
            if vol_ok:
                score += 1
                reasons.append(f"Volume {vol_out.signal}")

            confidence = min(score / 3, 1.0)
            return Signal(
                symbol=symbol, signal="BUY",
                entry_price=current_price,
                stop_loss=round(current_price - atr_mults["sl"] * atr_val, 6),
                take_profit=round(mid_band, 6),
                confidence=round(confidence, 3),
                timeframe=timeframe, strategy_name=self.name,
                asset_class=self.asset_class, broker=self.broker,
                reasons=reasons + [f"Regime: {regime_name}"],
                regime=regime_name,
            )

        if at_upper and rsi_val > self.RSI_OVERBOUGHT:
            score = 2
            reasons = [
                f"BB upper touch (band_pct={bb_out.value:.3f})",
                f"RSI overbought ({rsi_val:.1f})",
            ]
            if vol_ok:
                score += 1
                reasons.append(f"Volume {vol_out.signal}")

            confidence = min(score / 3, 1.0)
            return Signal(
                symbol=symbol, signal="SHORT",
                entry_price=current_price,
                stop_loss=round(current_price + atr_mults["sl"] * atr_val, 6),
                take_profit=round(mid_band, 6),
                confidence=round(confidence, 3),
                timeframe=timeframe, strategy_name=self.name,
                asset_class=self.asset_class, broker=self.broker,
                reasons=reasons + [f"Regime: {regime_name}"],
                regime=regime_name,
            )

        hold_reasons = [f"Price mid-band (band_pct={bb_out.value:.3f}), RSI={rsi_val:.1f}"]
        if bb_out.signal == "squeeze":
            hold_reasons.append("BB squeeze — watch for breakout")
        return _hold(hold_reasons)
