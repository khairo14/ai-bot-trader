import pandas as pd
from typing import Optional
from loguru import logger

from core.strategies.base import BaseStrategy, Signal
from core.ml_scorer import ml_scorer
from core.regime_classifier import regime_classifier
from tools.basic.rsi import RSI
from tools.basic.macd import MACD
from tools.basic.bollinger_bands import BollingerBands
from tools.basic.moving_averages import MovingAverages
from tools.basic.volume_atr_adx import VolumeAnalysis, ATR, ADX


class HybridStrategy(BaseStrategy):
    """
    Hybrid Rule-Based + ML Signal Strategy.

    Step 1: Rule-based filter — all conditions must pass threshold
    Step 2: ML model scores P(BUY) — blended with rule confidence
    Step 3: Regime-adjusted signal output

    Entry (LONG):
      - RSI < 45 (not overbought)
      - MACD bullish or histogram_positive
      - EMA fast > EMA slow (trend aligned)
      - Volume above average
      - ADX > 15 (some trending)

    Entry (SHORT):
      - RSI > 55 (not oversold)
      - MACD bearish or histogram_negative
      - EMA fast < EMA slow
      - Volume above average
      - ADX > 15

    Exit signal generated when opposing conditions meet.
    """

    name = "hybrid_macd_rsi"
    description = "Hybrid rule-based strategy combining RSI, MACD, EMA crossover, Volume, and ADX."
    asset_class = "crypto"
    broker = "binance"

    def __init__(self):
        self.rsi = RSI()
        self.macd = MACD()
        self.bb = BollingerBands()
        self.ma = MovingAverages()
        self.vol = VolumeAnalysis()
        self.atr = ATR()
        self.adx = ADX()

    def generate_signal(
        self,
        data: pd.DataFrame,
        symbol: str,
        timeframe: str = "1h",
        tool_outputs: Optional[dict] = None,
        **kwargs,
    ) -> Signal:

        if len(data) < 50:
            return Signal(
                symbol=symbol, signal="HOLD", entry_price=float(data["close"].iloc[-1]),
                stop_loss=None, take_profit=None, confidence=0.0,
                timeframe=timeframe, strategy_name=self.name,
                asset_class=self.asset_class, broker=self.broker,
                reasons=["Insufficient data"]
            )

        # ── Regime classification ─────────────────────────────────────────
        regime_result = regime_classifier.classify(data)
        regime_name   = regime_result.regime
        score_adj     = regime_classifier.score_adjustment(regime_name)
        atr_mults     = regime_classifier.atr_multipliers(regime_name)
        logger.debug(f"[{self.name}] Regime: {regime_name} | adj={score_adj}")

        # ── Run tools ────────────────────────────────────
        rsi_out = self.rsi.calculate(data, period=14)
        macd_out = self.macd.calculate(data)
        ma_out = self.ma.calculate(data, fast_period=9, slow_period=21)
        vol_out = self.vol.calculate(data)
        atr_out = self.atr.calculate(data)
        adx_out = self.adx.calculate(data)
        bb_out = self.bb.calculate(data)

        current_price = float(data["close"].iloc[-1])
        atr_value = atr_out.value

        # ── Score signals (0–5 points each direction) ────
        long_score = 0
        short_score = 0
        reasons_long = []
        reasons_short = []

        # RSI
        if rsi_out.value < 45:
            long_score += 1
            reasons_long.append(f"RSI {rsi_out.value:.1f} (not overbought)")
        if rsi_out.value > 55:
            short_score += 1
            reasons_short.append(f"RSI {rsi_out.value:.1f} (not oversold)")
        if rsi_out.signal == "oversold":
            long_score += 1
            reasons_long.append(f"RSI oversold ({rsi_out.value:.1f})")
        if rsi_out.signal == "overbought":
            short_score += 1
            reasons_short.append(f"RSI overbought ({rsi_out.value:.1f})")

        # MACD
        if macd_out.signal in ("bullish_crossover", "histogram_positive"):
            long_score += 2 if "crossover" in macd_out.signal else 1
            reasons_long.append(f"MACD {macd_out.signal}")
        if macd_out.signal in ("bearish_crossover", "histogram_negative"):
            short_score += 2 if "crossover" in macd_out.signal else 1
            reasons_short.append(f"MACD {macd_out.signal}")

        # EMA crossover
        if ma_out.signal in ("golden_cross", "bullish"):
            long_score += 2 if ma_out.signal == "golden_cross" else 1
            reasons_long.append(f"EMA {ma_out.signal}")
        if ma_out.signal in ("death_cross", "bearish"):
            short_score += 2 if ma_out.signal == "death_cross" else 1
            reasons_short.append(f"EMA {ma_out.signal}")

        # Volume confirmation
        if vol_out.signal in ("spike", "above"):
            long_score += 1
            short_score += 1  # volume confirms either direction
            reasons_long.append(f"Volume {vol_out.signal} ({vol_out.value:.2f}x avg)")
            reasons_short.append(f"Volume {vol_out.signal} ({vol_out.value:.2f}x avg)")

        # ADX trend filter
        if adx_out.value > 20:
            long_score += 1
            short_score += 1
            reasons_long.append(f"ADX {adx_out.value:.1f} ({adx_out.signal})")
            reasons_short.append(f"ADX {adx_out.value:.1f} ({adx_out.signal})")

        # ── Step 2: ML probability score ──────────────────
        # ml_prob is P(price rises > 1.5×ATR within 24 candles)
        # If no model is trained yet, ml_prob is None and we fall back to rule-only.
        ml_prob: Optional[float] = None
        try:
            # G8: pass timeframe so scorer loads the TF-specific model
            ml_prob = ml_scorer.predict_proba(data, symbol, timeframe=timeframe)
        except Exception as _ml_err:
            logger.debug(f"[{self.name}] ML scorer skipped: {_ml_err}")

        # ── Determine signal ─────────────────────────────
        min_score = kwargs.get("min_score", 4)
        # Apply regime-based score adjustments so the classifier difficulty system
        # actually affects signal generation (was computed but never used before).
        min_score_long  = max(1, kwargs.get("min_score_long",  min_score) + score_adj.get("long_delta",  0))
        min_score_short = max(1, kwargs.get("min_score_short", min_score) + score_adj.get("short_delta", 0))
        total_possible = 8
        # ML gate: if model exists, veto signals where it strongly disagrees
        ML_VETO_BUY  = 0.35   # if P(buy) < this, suppress BUY even if rules agree
        ML_VETO_SHORT = 0.65  # if P(buy) > this, suppress SHORT even if rules agree

        if long_score >= min_score_long and long_score > short_score:
            signal_type = "BUY"
            rule_conf = long_score / total_possible
            if ml_prob is not None:
                if ml_prob < ML_VETO_BUY:
                    # ML strongly disagrees — downgrade to HOLD
                    signal_type = "HOLD"
                    confidence = 0.0
                    stop_loss = None
                    take_profit = None
                    reasons = reasons_long + [f"ML veto: P(buy)={ml_prob:.2f} < {ML_VETO_BUY}"]
                    logger.info(f"[{self.name}] BUY vetoed by ML (prob={ml_prob:.2f})")
                else:
                    # Blend: 60% rule confidence, 40% ML probability
                    confidence = round(0.6 * rule_conf + 0.4 * ml_prob, 3)
                    stop_loss = round(current_price - (atr_value * atr_mults["sl"]), 4)
                    take_profit = round(current_price + (atr_value * atr_mults["tp"]), 4)
                    reasons = reasons_long + [f"ML confirms: P(buy)={ml_prob:.2f}", f"Regime: {regime_name}"]
            else:
                confidence = round(rule_conf, 3)
                stop_loss = round(current_price - (atr_value * atr_mults["sl"]), 4)
                take_profit = round(current_price + (atr_value * atr_mults["tp"]), 4)
                reasons = reasons_long + [f"Regime: {regime_name}"]

        elif short_score >= min_score_short and short_score > long_score:
            signal_type = "SHORT"
            rule_conf = short_score / total_possible
            if ml_prob is not None:
                if ml_prob > ML_VETO_SHORT:
                    # ML strongly disagrees — downgrade to HOLD
                    signal_type = "HOLD"
                    confidence = 0.0
                    stop_loss = None
                    take_profit = None
                    reasons = reasons_short + [f"ML veto: P(buy)={ml_prob:.2f} > {ML_VETO_SHORT}"]
                    logger.info(f"[{self.name}] SHORT vetoed by ML (prob={ml_prob:.2f})")
                else:
                    # For SHORT: high ml_prob means bearish (inverted)
                    ml_short_conf = 1.0 - ml_prob
                    confidence = round(0.6 * rule_conf + 0.4 * ml_short_conf, 3)
                    stop_loss = round(current_price + (atr_value * atr_mults["sl"]), 4)
                    take_profit = round(current_price - (atr_value * atr_mults["tp"]), 4)
                    reasons = reasons_short + [f"ML bearish: P(buy)={ml_prob:.2f}", f"Regime: {regime_name}"]
            else:
                confidence = round(rule_conf, 3)
                stop_loss = round(current_price + (atr_value * atr_mults["sl"]), 4)
                take_profit = round(current_price - (atr_value * atr_mults["tp"]), 4)
                reasons = reasons_short + [f"Regime: {regime_name}"]

        else:
            signal_type = "HOLD"
            confidence = 0.0
            stop_loss = None
            take_profit = None
            reasons = ["Conditions not met for entry"]

        logger.debug(
            f"[{self.name}] {symbol} {timeframe} → {signal_type} "
            f"(long: {long_score}/{min_score_long}, short: {short_score}/{min_score_short}, "
            f"conf: {confidence}, ml: {ml_prob}, regime: {regime_name})"
        )

        # ── Feature 3: Confirmation candle ────────────────────────────────────
        # The previous *closed* candle must close in the direction of the signal.
        # A forming candle (last bar) is never green/red enough to trust alone.
        if signal_type in ("BUY", "SHORT") and len(data) >= 2:
            prev_close = float(data["close"].iloc[-2])
            prev_open  = float(data["open"].iloc[-2])
            if signal_type == "BUY" and prev_close <= prev_open:
                logger.info(
                    f"[{self.name}] {symbol} BUY → HOLD: prev candle bearish "
                    f"(close={prev_close:.5f} <= open={prev_open:.5f})"
                )
                signal_type = "HOLD"
                confidence  = 0.0
                stop_loss   = None
                take_profit = None
                reasons     = reasons + ["Awaiting confirmation candle (prev candle bearish)"]
            elif signal_type == "SHORT" and prev_close >= prev_open:
                logger.info(
                    f"[{self.name}] {symbol} SHORT → HOLD: prev candle bullish "
                    f"(close={prev_close:.5f} >= open={prev_open:.5f})"
                )
                signal_type = "HOLD"
                confidence  = 0.0
                stop_loss   = None
                take_profit = None
                reasons     = reasons + ["Awaiting confirmation candle (prev candle bullish)"]

        # ── Feature 1: Snap TP to nearest support / resistance level ─────────
        # For BUY: if a pivot resistance exists closer than the ATR TP, prefer it.
        # For SHORT: if a pivot support exists closer (higher) than the ATR TP, prefer it.
        if signal_type == "BUY" and take_profit is not None:
            resistance = self._find_resistance(data, current_price)
            if resistance is not None and resistance < take_profit:
                take_profit = round(resistance * 0.9998, 4)  # 2-pip buffer below resistance
                reasons = reasons + [f"TP snapped to resistance {resistance:.5f}"]
        elif signal_type == "SHORT" and take_profit is not None:
            support = self._find_support(data, current_price)
            if support is not None and support > take_profit:
                take_profit = round(support * 1.0002, 4)  # 2-pip buffer above support
                reasons = reasons + [f"TP snapped to support {support:.5f}"]

        # ── Feature 2: Trailing stop percentage ──────────────────────────────
        # Express as % of current price using 1.5× ATR so it adapts to volatility.
        # monitor_sl_tp reads this field and ratchets stop_loss with every tick.
        trailing_stop_pct: Optional[float] = None
        if signal_type in ("BUY", "SHORT") and atr_value and current_price:
            trailing_stop_pct = round(atr_value * 1.5 / current_price * 100, 4)

        return Signal(
            symbol=symbol,
            signal=signal_type,
            entry_price=current_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            confidence=confidence,
            timeframe=timeframe,
            strategy_name=self.name,
            asset_class=self.asset_class,
            broker=self.broker,
            reasons=reasons,
            regime=regime_name,
            trailing_stop_pct=trailing_stop_pct,
        )

    # S/R helpers (_find_resistance, _find_support) live in BaseStrategy and are inherited.
