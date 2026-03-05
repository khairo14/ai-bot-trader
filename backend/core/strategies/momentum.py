"""
Momentum Breakout Strategy
===========================
Detects price breakouts above/below a rolling channel (highest-high / lowest-low)
confirmed by volume and ADX trend strength.

Entry logic
-----------
LONG:  close > N-period highest high (previous candle), volume spike, ADX > 20
SHORT: close < N-period lowest  low  (previous candle), volume spike, ADX > 20

Risk
----
Stop-loss:   entry ± 1.5 × ATR
Take-profit: entry ± 3.0 × ATR  (2:1 RR)
"""

import pandas as pd
from typing import Optional
from loguru import logger

from core.strategies.base import BaseStrategy, Signal
from core.regime_classifier import regime_classifier
from tools.basic.volume_atr_adx import VolumeAnalysis, ATR, ADX
from tools.basic.moving_averages import MovingAverages


class MomentumBreakoutStrategy(BaseStrategy):
    """ATR-channel momentum breakout strategy."""

    name = "momentum_breakout"
    description = (
        "Enters on N-period channel breakouts confirmed by volume and ADX trend strength. "
        "Uses ATR multiples for dynamic stop-loss and take-profit."
    )
    asset_class = "crypto"
    broker = "binance"

    # Strategy parameters
    CHANNEL_PERIOD: int = 20   # rolling high/low lookback
    ADX_THRESHOLD: float = 20.0
    ATR_STOP_MULT: float = 1.5
    ATR_TP_MULT: float = 3.0

    def __init__(self):
        self.vol = VolumeAnalysis()
        self.atr = ATR()
        self.adx = ADX()
        self.ma = MovingAverages()

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

        required = self.CHANNEL_PERIOD + 10
        if len(data) < required:
            return _hold([f"Insufficient data ({len(data)} < {required} candles)"])

        # ── Compute indicators ──────────────────────────────────────────────
        # ── Regime classification ─────────────────────────────────────────
        regime_result = regime_classifier.classify(data)
        regime_name   = regime_result.regime
        score_adj     = regime_classifier.score_adjustment(regime_name)
        atr_mults     = regime_classifier.atr_multipliers(regime_name)

        try:
            atr_out = self.atr.calculate(data)
            adx_out = self.adx.calculate(data)
            vol_out = self.vol.calculate(data)
            ma_out = self.ma.calculate(data, fast_period=9, slow_period=21)
        except Exception as exc:
            logger.warning(f"[{self.name}] tool error: {exc}")
            return _hold([f"Tool error: {exc}"])

        current_price = float(data["close"].iloc[-1])
        atr_val = atr_out.value

        # Channel: use candles BEFORE the current one to avoid look-ahead
        lookback = data["close"].iloc[-(self.CHANNEL_PERIOD + 1):-1]
        highest_high = float(data["high"].iloc[-(self.CHANNEL_PERIOD + 1):-1].max())
        lowest_low = float(data["low"].iloc[-(self.CHANNEL_PERIOD + 1):-1].min())

        # ── ADX filter ──────────────────────────────────────────────────────
        adx_ok = adx_out.value >= self.ADX_THRESHOLD
        # ── Volume confirmation ─────────────────────────────────────────────
        vol_ok = vol_out.signal in ("spike", "above")
        # ── Trend filter (EMA) ──────────────────────────────────────────────
        uptrend = ma_out.signal in ("golden_cross", "bullish")
        downtrend = ma_out.signal in ("death_cross", "bearish")

        reasons: list[str] = []
        score = 0

        if adx_ok:
            score += 1
            reasons.append(f"ADX {adx_out.value:.1f} (trending)")
        if vol_ok:
            score += 1
            reasons.append(f"Volume {vol_out.signal}")

        long_score = short_score = score

        # Breakout checks
        broke_up = current_price > highest_high
        broke_down = current_price < lowest_low

        if broke_up:
            long_score += 2
            reasons_long = reasons + [f"Breakout above {highest_high:.4f}"]
            if uptrend:
                long_score += 1
                reasons_long.append("EMA uptrend confirmed")
        if broke_down:
            short_score += 2
            reasons_short = reasons + [f"Breakdown below {lowest_low:.4f}"]
            if downtrend:
                short_score += 1
                reasons_short.append("EMA downtrend confirmed")

        LONG_THRESHOLD  = max(1, 3 + score_adj["long_delta"])
        SHORT_THRESHOLD = max(1, 3 + score_adj["short_delta"])

        if broke_up and long_score >= LONG_THRESHOLD:
            confidence = min(long_score / 5, 1.0)
            return Signal(
                symbol=symbol, signal="BUY",
                entry_price=current_price,
                stop_loss=round(current_price - atr_mults["sl"] * atr_val, 6),
                take_profit=round(current_price + atr_mults["tp"] * atr_val, 6),
                confidence=round(confidence, 3),
                timeframe=timeframe, strategy_name=self.name,
                asset_class=self.asset_class, broker=self.broker,
                reasons=reasons_long + [f"Regime: {regime_name}"],
                regime=regime_name,
            )

        if broke_down and short_score >= SHORT_THRESHOLD:
            confidence = min(short_score / 5, 1.0)
            return Signal(
                symbol=symbol, signal="SHORT",
                entry_price=current_price,
                stop_loss=round(current_price + atr_mults["sl"] * atr_val, 6),
                take_profit=round(current_price - atr_mults["tp"] * atr_val, 6),
                confidence=round(confidence, 3),
                timeframe=timeframe, strategy_name=self.name,
                asset_class=self.asset_class, broker=self.broker,
                reasons=(reasons_short if broke_down else reasons) + [f"Regime: {regime_name}"],
                regime=regime_name,
            )

        hold_reasons = [f"No breakout (high {highest_high:.4f} / low {lowest_low:.4f})"]
        if not adx_ok:
            hold_reasons.append(f"ADX {adx_out.value:.1f} below threshold")
        if not vol_ok:
            hold_reasons.append(f"Volume weak ({vol_out.signal})")
        return _hold(hold_reasons)
