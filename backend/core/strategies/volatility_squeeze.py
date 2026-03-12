"""
Volatility Squeeze Strategy
============================
Trades breakouts from low-volatility compression periods.

Concept (BB/Keltner Squeeze — John Carter):
  A squeeze occurs when Bollinger Bands narrow inside Keltner Channels.
  Price builds tension during the squeeze. When it fires, the move is
  typically fast and directional.

Entry logic
-----------
Squeeze condition:
  BB upper < KC upper  AND  BB lower > KC lower  (bands inside Keltner)

Trigger (squeeze fires):
  Was squeezed for ≥ SQUEEZE_MIN_BARS candles, now BB is expanding outside KC.

Direction scoring (0–5 points):
  +2  Price momentum: close above midpoint of 20-period range → LONG
                       close below midpoint              → SHORT
  +1  MACD histogram positive (LONG) / negative (SHORT)
  +1  Volume above average
  +1  ADX > 20 (trending, not ranging)

  Min score = 3 to generate signal.

Risk
----
  Stop-loss:   entry ± ATR_STOP_MULT × ATR(14)
  Take-profit: entry ± ATR_TP_MULT   × ATR(14)  (default 3 : 1.5 = 2:1 RR)
"""

import pandas as pd
from typing import Optional
from loguru import logger

from core.strategies.base import BaseStrategy, Signal
from core.regime_classifier import regime_classifier
from core.ml_scorer import ml_scorer
from tools.basic.bollinger_bands import BollingerBands
from tools.basic.macd import MACD
from tools.basic.volume_atr_adx import VolumeAnalysis, ATR, ADX


class VolatilitySqueezeStrategy(BaseStrategy):
    """Bollinger Band / Keltner Channel squeeze breakout strategy."""

    name = "volatility_squeeze"
    description = (
        "Identifies low-volatility squeeze periods (BB inside Keltner Channels) "
        "and enters when the squeeze fires in a directional move confirmed by "
        "MACD momentum, volume, and ADX."
    )
    asset_class = "crypto"
    broker = "binance"

    # ── Parameters ────────────────────────────────────────────────────────
    BB_PERIOD: int = 20
    BB_STDDEV: float = 2.0
    KC_PERIOD: int = 20          # Keltner EMA period
    KC_ATR_PERIOD: int = 20
    KC_ATR_MULT: float = 1.5     # Keltner band = EMA ± 1.5 × ATR
    SQUEEZE_MIN_BARS: int = 3    # minimum candles in squeeze before trading the break
    ADX_THRESHOLD: float = 20.0
    MIN_SCORE: int = 3
    ATR_STOP_MULT: float = 1.5
    ATR_TP_MULT: float = 3.0

    def __init__(self):
        self.bb = BollingerBands()
        self.macd_tool = MACD()
        self.vol = VolumeAnalysis()
        self.atr = ATR()
        self.adx = ADX()

    # ── Keltner Channel (inline) ──────────────────────────────────────────
    @staticmethod
    def _keltner(data: pd.DataFrame, period: int = 20, atr_period: int = 20,
                 atr_mult: float = 1.5):
        """Return (kc_upper Series, kc_lower Series, kc_mid Series)."""
        ema = data["close"].ewm(span=period, adjust=False).mean()
        high, low, close = data["high"], data["low"], data["close"]
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low  - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(atr_period).mean()
        return ema + atr_mult * atr, ema - atr_mult * atr, ema

    # ── Squeeze history helper ────────────────────────────────────────────
    def _squeeze_bars(self, data: pd.DataFrame) -> int:
        """Count consecutive candles (ending at -2, i.e. the closed candle before current)
        that were in a squeeze state.  Returns 0 if the previous candle was NOT squeezed."""
        bb_period = self.BB_PERIOD
        if len(data) < bb_period + self.SQUEEZE_MIN_BARS + 5:
            return 0

        sma = data["close"].rolling(bb_period).mean()
        std = data["close"].rolling(bb_period).std()
        bb_upper = sma + self.BB_STDDEV * std
        bb_lower = sma - self.BB_STDDEV * std
        kc_upper, kc_lower, _ = self._keltner(data, self.KC_PERIOD, self.KC_ATR_PERIOD, self.KC_ATR_MULT)

        squeeze = (bb_upper < kc_upper) & (bb_lower > kc_lower)
        # Walk backwards from index -2 counting consecutive squeeze candles
        idx = len(squeeze) - 2   # closed candle
        count = 0
        while idx >= 0 and squeeze.iloc[idx]:
            count += 1
            idx -= 1
        return count

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

        required = self.BB_PERIOD + self.SQUEEZE_MIN_BARS + 10
        if len(data) < required:
            return _hold([f"Insufficient data ({len(data)} < {required} candles)"])

        # ── Regime ────────────────────────────────────────────────────────
        regime_result = regime_classifier.classify(data)
        regime_name   = regime_result.regime
        score_adj     = regime_classifier.score_adjustment(regime_name)
        atr_mults     = regime_classifier.atr_multipliers(regime_name)

        # ── Tool calculations ─────────────────────────────────────────────
        try:
            bb_out  = self.bb.calculate(data, period=self.BB_PERIOD, std_dev=self.BB_STDDEV)
            macd_out = self.macd_tool.calculate(data)
            vol_out = self.vol.calculate(data)
            atr_out = self.atr.calculate(data)
            adx_out = self.adx.calculate(data)
        except Exception as exc:
            logger.warning(f"[{self.name}] tool error: {exc}")
            return _hold([f"Tool error: {exc}"])

        # ── Keltner Channel (current candle) ──────────────────────────────
        try:
            kc_upper_s, kc_lower_s, kc_mid_s = self._keltner(
                data, self.KC_PERIOD, self.KC_ATR_PERIOD, self.KC_ATR_MULT)
            kc_upper = float(kc_upper_s.iloc[-1])
            kc_lower = float(kc_lower_s.iloc[-1])
            kc_mid   = float(kc_mid_s.iloc[-1])
        except Exception as exc:
            logger.warning(f"[{self.name}] Keltner calc error: {exc}")
            return _hold([f"Keltner error: {exc}"])

        bb_upper = float(bb_out.metadata["upper"])
        bb_lower = float(bb_out.metadata["lower"])
        current_price = float(data["close"].iloc[-1])
        atr_val = atr_out.value

        # ── Squeeze detection ─────────────────────────────────────────────
        # Current candle: is BB still squeezed inside KC?
        currently_squeezed = (bb_upper < kc_upper) and (bb_lower > kc_lower)
        # Count how many prior closed candles were squeezed
        squeeze_bars = self._squeeze_bars(data)

        # We only trade when the squeeze has just FIRED:
        # - Was squeezed for ≥ SQUEEZE_MIN_BARS candles
        # - Current candle is NOT squeezed (breakout in progress) OR BB is expanding
        squeeze_fired = (squeeze_bars >= self.SQUEEZE_MIN_BARS) and not currently_squeezed
        # Also accept: squeeze is very recent (bars ≥ min) + BB expanding even while technically inside
        if not squeeze_fired:
            squeeze_fired = (squeeze_bars >= self.SQUEEZE_MIN_BARS) and (bb_out.signal == "expansion")

        if not squeeze_fired:
            reasons = []
            if squeeze_bars == 0:
                reasons.append("No active squeeze — waiting for compression")
            else:
                reasons.append(f"Squeeze active ({squeeze_bars} bars) — waiting for breakout")
            return _hold(reasons)

        # ── Direction scoring ─────────────────────────────────────────────
        # 20-period range midpoint for momentum
        range_high = float(data["high"].iloc[-self.BB_PERIOD:].max())
        range_low  = float(data["low"].iloc[-self.BB_PERIOD:].min())
        range_mid  = (range_high + range_low) / 2.0

        macd_hist = float(macd_out.metadata.get("histogram", 0.0))
        vol_ok     = vol_out.signal in ("spike", "above")
        adx_ok     = adx_out.value >= self.ADX_THRESHOLD

        long_score  = 0
        short_score = 0
        long_reasons:  list[str] = [f"Squeeze fired ({squeeze_bars} bars compressed)"]
        short_reasons: list[str] = [f"Squeeze fired ({squeeze_bars} bars compressed)"]

        # Momentum: position relative to range midpoint (+2)
        if current_price > range_mid:
            long_score += 2
            long_reasons.append(f"Price {current_price:.4f} above range mid {range_mid:.4f}")
        else:
            short_score += 2
            short_reasons.append(f"Price {current_price:.4f} below range mid {range_mid:.4f}")

        # MACD histogram direction (+1)
        if macd_hist > 0:
            long_score += 1
            long_reasons.append(f"MACD histogram positive ({macd_hist:.4f})")
        elif macd_hist < 0:
            short_score += 1
            short_reasons.append(f"MACD histogram negative ({macd_hist:.4f})")

        # Volume (+1)
        if vol_ok:
            long_score += 1
            short_score += 1
            long_reasons.append(f"Volume {vol_out.signal}")
            short_reasons.append(f"Volume {vol_out.signal}")

        # ADX trending (+1)
        if adx_ok:
            long_score += 1
            short_score += 1
            long_reasons.append(f"ADX {adx_out.value:.1f} (trending)")
            short_reasons.append(f"ADX {adx_out.value:.1f} (trending)")

        # Regime threshold adjustment
        long_threshold  = max(1, self.MIN_SCORE + score_adj["long_delta"])
        short_threshold = max(1, self.MIN_SCORE + score_adj["short_delta"])

        atr_sl = atr_mults["sl"] * atr_val
        atr_tp = atr_mults["tp"] * atr_val

        # ML gate: same veto logic as hybrid strategy
        ml_prob: Optional[float] = None
        try:
            ml_prob = ml_scorer.predict_proba(data, symbol, timeframe=timeframe)
        except Exception as _ml_err:
            logger.debug(f"[{self.name}] ML scorer skipped: {_ml_err}")

        ML_VETO_BUY   = 0.35
        ML_VETO_SHORT = 0.65

        if long_score >= long_threshold:
            if ml_prob is not None and ml_prob < ML_VETO_BUY:
                return _hold([f"ML veto: P(buy)={ml_prob:.2f} < {ML_VETO_BUY} — squeeze breakout unconfirmed"])
            confidence = round(min(long_score / 5.0, 1.0), 3)
            if ml_prob is not None:
                confidence = round(0.6 * confidence + 0.4 * ml_prob, 3)
            return self._enhance_signal(Signal(
                symbol=symbol, signal="BUY",
                entry_price=current_price,
                stop_loss=round(current_price - atr_sl, 6),
                take_profit=round(current_price + atr_tp, 6),
                confidence=confidence,
                timeframe=timeframe, strategy_name=self.name,
                asset_class=self.asset_class, broker=self.broker,
                reasons=long_reasons + [f"Regime: {regime_name}"] + ([f"ML P(buy)={ml_prob:.2f}"] if ml_prob is not None else []),
                regime=regime_name,
            ), data, atr_val)

        if short_score >= short_threshold:
            if ml_prob is not None and ml_prob > ML_VETO_SHORT:
                return _hold([f"ML veto: P(buy)={ml_prob:.2f} > {ML_VETO_SHORT} — squeeze breakdown unconfirmed"])
            confidence = round(min(short_score / 5.0, 1.0), 3)
            if ml_prob is not None:
                confidence = round(0.6 * confidence + 0.4 * (1.0 - ml_prob), 3)
            return self._enhance_signal(Signal(
                symbol=symbol, signal="SHORT",
                entry_price=current_price,
                stop_loss=round(current_price + atr_sl, 6),
                take_profit=round(current_price - atr_tp, 6),
                confidence=confidence,
                timeframe=timeframe, strategy_name=self.name,
                asset_class=self.asset_class, broker=self.broker,
                reasons=short_reasons + [f"Regime: {regime_name}"] + ([f"ML P(buy)={ml_prob:.2f}"] if ml_prob is not None else []),
                regime=regime_name,
            ), data, atr_val)

        hold_reasons = [
            f"Squeeze fired but score too low (long={long_score}, short={short_score}, "
            f"need {long_threshold}/{short_threshold})"
        ]
        return _hold(hold_reasons)
