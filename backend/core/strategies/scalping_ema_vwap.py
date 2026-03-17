"""
ScalpingEmaVwap Strategy  (scalp_ema_vwap)
==========================================
A purpose-built scalping strategy for 1m / 3m / 5m timeframes.

Entry logic
-----------
Uses a 3-EMA ribbon (8/13/21) combined with VWAP, momentum (ROC-5) and
volume confirmation.  Signals are scored 0–7 per direction; the default
threshold is 4 (configurable in scalping_settings.json).

LONG  conditions (each +1 unless noted):
  +2  EMA8 > EMA13 > EMA21 (ribbon stacked bullish)
  +1  Price above VWAP (>0.1%)
  +1  ROC-5 > 0 (positive momentum)
  +1  Volume ≥ min_volume_ratio × avg (configurable, default 1.2×)
  +1  ATR signal is "normal" or "high_volatility" (not dead market)
  +1  EMA8/EMA13 formed a recent mini-crossover in the last 3 candles

SHORT conditions (mirror):
  +2  EMA8 < EMA13 < EMA21 (ribbon stacked bearish)
  +1  Price below VWAP (<-0.1%)
  +1  ROC-5 < 0 (negative momentum)
  +1  Volume ≥ min_volume_ratio × avg
  +1  ATR signal is "normal" or "high_volatility"
  +1  EMA8/EMA13 formed a recent mini-crossover downward in the last 3 candles

Risk
----
  stop_loss   = entry ± sl_atr_mult × ATR   (default 0.8×)
  take_profit = entry ± tp_atr_mult × ATR   (default 1.6×  →  2:1 R:R)
  trailing_stop_pct = None  (fixed stops — no trailing for scalping)

ML gate
-------
  ScalpingMLScorer.predict_proba_scalp(data, symbol, timeframe)
  Veto threshold: ml_veto_threshold (default 0.40, in scalping_settings.json).
  Falls back gracefully to rule-based signal when no model is available.

Spread check
------------
  max_spread_pct (default 0.05%) from settings.  If the current bid/ask spread
  exceeds this, the signal is demoted to HOLD (market too illiquid).
  The spread value is supplied by the caller (scalping_runner) as a kwarg.
"""

import pathlib
import json
from typing import Optional
from loguru import logger
import pandas as pd

from core.strategies.base_scalping import BaseScalpingStrategy
from core.strategies.base import Signal
from tools.basic.volume_atr_adx import VolumeAnalysis, ATR
from tools.advanced.vwap import VWAP

_SETTINGS_PATH = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "runtime" / "scalping_settings.json"
)


def _load_scalping_settings() -> dict:
    defaults = {
        "timeframe": "5m",
        "risk_per_trade_pct": 0.5,
        "sl_atr_mult": 0.8,
        "tp_atr_mult": 1.6,
        "min_volume_ratio": 1.2,
        "max_spread_pct": 0.05,
        "min_score": 4,
        "ml_veto_threshold": 0.40,
        "sr_tp_snap": False,
        "enabled": True,
    }
    try:
        if _SETTINGS_PATH.exists():
            data = json.loads(_SETTINGS_PATH.read_text())
            return {**defaults, **data}
    except (json.JSONDecodeError, OSError):
        pass
    return defaults


class ScalpingEmaVwap(BaseScalpingStrategy):
    """
    EMA Ribbon + VWAP scalping strategy.
    Designed for 1m, 3m, and 5m candles on Binance crypto or Alpaca stocks.
    """

    name = "scalp_ema_vwap"
    description = (
        "3-EMA ribbon (8/13/21) + VWAP + momentum scalping strategy. "
        "No confirmation candle filter, no trailing stop. "
        "Designed for 1m–5m timeframes."
    )
    asset_class = "crypto"
    broker = "binance"

    # Minimum candles required: EMA21 warm-up (21) + ROC lookback (5) + buffer
    MIN_CANDLES = 30

    # EMA ribbon periods
    EMA_FAST   = 8
    EMA_MID    = 13
    EMA_SLOW   = 21

    def __init__(self):
        self.vol  = VolumeAnalysis()
        self.atr  = ATR()
        self.vwap = VWAP()

    def generate_signal(
        self,
        data: pd.DataFrame,
        symbol: str,
        timeframe: str = "5m",
        tool_outputs: Optional[dict] = None,
        **kwargs,
    ) -> Signal:
        cfg = _load_scalping_settings()

        sl_mult:     float = float(cfg.get("sl_atr_mult", 0.8))
        tp_mult:     float = float(cfg.get("tp_atr_mult", 1.6))
        min_vol_r:   float = float(cfg.get("min_volume_ratio", 1.2))
        min_score:   int   = int(cfg.get("min_score", 4))
        ml_thresh:   float = float(cfg.get("ml_veto_threshold", 0.40))
        # S/R TP snap flag — write to instance so parent _enhance_signal picks it up
        self.sr_tp_snap_enabled = bool(cfg.get("sr_tp_snap", False))

        # Optional spread_pct passed by scalping_runner (from live ticker)
        spread_pct: Optional[float] = kwargs.get("spread_pct")
        max_spread: float = float(cfg.get("max_spread_pct", 0.05))

        def _hold(reasons: list) -> Signal:
            return Signal(
                symbol=symbol, signal="HOLD",
                entry_price=float(data["close"].iloc[-1]),
                stop_loss=None, take_profit=None,
                confidence=0.0, timeframe=timeframe,
                strategy_name=self.name,
                asset_class=self.asset_class, broker=self.broker,
                reasons=reasons,
            )

        if len(data) < self.MIN_CANDLES:
            return _hold([f"Insufficient data ({len(data)} < {self.MIN_CANDLES} candles)"])

        # ── Spread gate ────────────────────────────────────────────────────────
        if spread_pct is not None and spread_pct > max_spread:
            return _hold([f"Spread {spread_pct:.4f}% > max {max_spread:.4f}% — market illiquid"])

        # ── Compute indicators ─────────────────────────────────────────────────
        try:
            atr_out  = self.atr.calculate(data)
            vol_out  = self.vol.calculate(data, period=20, spike_threshold=2.0)
            vwap_out = self.vwap.calculate(data)
        except Exception as exc:
            logger.warning(f"[{self.name}] Tool error: {exc}")
            return _hold([f"Tool error: {exc}"])

        close  = data["close"]
        ema8   = close.ewm(span=self.EMA_FAST, adjust=False).mean()
        ema13  = close.ewm(span=self.EMA_MID,  adjust=False).mean()
        ema21  = close.ewm(span=self.EMA_SLOW, adjust=False).mean()

        current_price = float(close.iloc[-1])
        atr_val       = atr_out.value

        # Guard: ATR of 0 (possible for micro-priced tokens after 4-decimal round)
        # would produce SL == TP == entry_price — an instantly-triggered degenerate trade.
        if not atr_val or atr_val <= 0:
            return _hold(["ATR value is zero or unavailable — cannot compute SL/TP"])

        e8  = float(ema8.iloc[-1])
        e13 = float(ema13.iloc[-1])
        e21 = float(ema21.iloc[-1])

        # Ribbon stacked (primary direction signal)
        ribbon_bullish = e8 > e13 > e21
        ribbon_bearish = e8 < e13 < e21

        # Recent mini-crossover in last 3 candles (EMA8 crossing EMA13)
        recent_bull_cross = (
            float(ema8.iloc[-3]) <= float(ema13.iloc[-3]) and e8 > e13
        ) if len(data) >= 3 else False
        recent_bear_cross = (
            float(ema8.iloc[-3]) >= float(ema13.iloc[-3]) and e8 < e13
        ) if len(data) >= 3 else False

        # VWAP position
        vwap_bullish = vwap_out.signal == "price_above"
        vwap_bearish = vwap_out.signal == "price_below"

        # ROC-5
        roc5_bullish = False
        roc5_bearish = False
        if len(data) >= 6:
            roc5 = (float(close.iloc[-1]) - float(close.iloc[-6])) / float(close.iloc[-6]) * 100
            roc5_bullish = roc5 > 0
            roc5_bearish = roc5 < 0

        # Volume
        vol_ok = float(vol_out.value) >= min_vol_r

        # ATR market activity (not a dead / zero-volume market)
        atr_active = atr_out.signal in ("normal", "high_volatility")

        # ── Scoring ────────────────────────────────────────────────────────────
        long_score  = 0
        short_score = 0
        long_reasons:  list[str] = []
        short_reasons: list[str] = []

        if ribbon_bullish:
            long_score += 2
            long_reasons.append(f"EMA ribbon bullish (8>{e8:.4f} 13>{e13:.4f} 21>{e21:.4f})")
        if ribbon_bearish:
            short_score += 2
            short_reasons.append(f"EMA ribbon bearish (8<{e8:.4f} 13<{e13:.4f} 21<{e21:.4f})")

        if vwap_bullish:
            long_score += 1
            long_reasons.append(f"Price above VWAP ({vwap_out.metadata.get('distance_pct', 0):.3f}%)")
        if vwap_bearish:
            short_score += 1
            short_reasons.append(f"Price below VWAP ({vwap_out.metadata.get('distance_pct', 0):.3f}%)")

        if roc5_bullish:
            long_score += 1
            long_reasons.append("Momentum ROC-5 positive")
        if roc5_bearish:
            short_score += 1
            short_reasons.append("Momentum ROC-5 negative")

        if vol_ok:
            long_score  += 1
            short_score += 1
            long_reasons.append(f"Volume {vol_out.value:.2f}× avg")
            short_reasons.append(f"Volume {vol_out.value:.2f}× avg")

        if atr_active:
            long_score  += 1
            short_score += 1
            long_reasons.append(f"ATR active ({atr_out.signal})")
            short_reasons.append(f"ATR active ({atr_out.signal})")

        if recent_bull_cross:
            long_score += 1
            long_reasons.append("EMA8 crossed above EMA13 recently")
        if recent_bear_cross:
            short_score += 1
            short_reasons.append("EMA8 crossed below EMA13 recently")

        # ── ML gate ────────────────────────────────────────────────────────────
        ml_buy_prob:   Optional[float] = None
        ml_short_prob: Optional[float] = None
        try:
            from core.scalping_ml_scorer import scalping_ml_scorer
            _spread_for_ml = spread_pct or 0.0
            ml_buy_prob   = scalping_ml_scorer.predict_proba_scalp(data, symbol, timeframe, spread_pct=_spread_for_ml)
            ml_short_prob = scalping_ml_scorer.predict_proba_scalp_short(data, symbol, timeframe, spread_pct=_spread_for_ml)
        except Exception as _ml_err:
            logger.debug(f"[{self.name}] Scalp ML scorer unavailable: {_ml_err}")

        # ── Decision ───────────────────────────────────────────────────────────
        if long_score >= min_score and long_score > short_score:
            # ML veto
            if ml_buy_prob is not None and ml_buy_prob < ml_thresh:
                return _hold([f"ML veto: P(scalp_buy)={ml_buy_prob:.2f} < {ml_thresh}"])
            # Blend confidence
            confidence = min(long_score / 7.0, 1.0)
            if ml_buy_prob is not None:
                confidence = round(0.6 * confidence + 0.4 * ml_buy_prob, 3)
            else:
                confidence = round(confidence, 3)
            return self._enhance_signal(Signal(
                symbol=symbol,
                signal="BUY",
                entry_price=current_price,
                stop_loss=round(current_price - sl_mult * atr_val, 6),
                take_profit=round(current_price + tp_mult * atr_val, 6),
                confidence=confidence,
                timeframe=timeframe,
                strategy_name=self.name,
                asset_class=self.asset_class,
                broker=self.broker,
                reasons=long_reasons + (
                    [f"ML P(scalp_buy)={ml_buy_prob:.2f}"] if ml_buy_prob is not None else []
                ),
            ), data, atr_val)

        if short_score >= min_score and short_score > long_score:
            # ML veto
            if ml_short_prob is not None and ml_short_prob < ml_thresh:
                return _hold([f"ML veto: P(scalp_short)={ml_short_prob:.2f} < {ml_thresh}"])
            confidence = min(short_score / 7.0, 1.0)
            if ml_short_prob is not None:
                confidence = round(0.6 * confidence + 0.4 * ml_short_prob, 3)
            else:
                confidence = round(confidence, 3)
            return self._enhance_signal(Signal(
                symbol=symbol,
                signal="SHORT",
                entry_price=current_price,
                stop_loss=round(current_price + sl_mult * atr_val, 6),
                take_profit=round(current_price - tp_mult * atr_val, 6),
                confidence=confidence,
                timeframe=timeframe,
                strategy_name=self.name,
                asset_class=self.asset_class,
                broker=self.broker,
                reasons=short_reasons + (
                    [f"ML P(scalp_short)={ml_short_prob:.2f}"] if ml_short_prob is not None else []
                ),
            ), data, atr_val)

        # No signal — compile informative HOLD reason with full score breakdown
        hold_reasons: list[str] = []
        if not ribbon_bullish and not ribbon_bearish:
            hold_reasons.append(f"EMA ribbon mixed (8={e8:.4f} 13={e13:.4f} 21={e21:.4f})")
        if not vol_ok:
            hold_reasons.append(f"Volume below threshold ({vol_out.value:.2f}\u00d7 < {min_vol_r}\u00d7)")
        if not atr_active:
            hold_reasons.append(f"Market inactive (ATR signal: {atr_out.signal})")
        # Always append a full per-condition breakdown so the log shows exactly what's missing
        _best_score = max(long_score, short_score)
        _best_dir   = "long" if long_score >= short_score else "short"
        _r_ok  = ribbon_bullish if _best_dir == "long" else ribbon_bearish
        _v_ok  = vwap_bullish   if _best_dir == "long" else vwap_bearish
        _rc_ok = roc5_bullish   if _best_dir == "long" else roc5_bearish
        _cr_ok = recent_bull_cross if _best_dir == "long" else recent_bear_cross
        hold_reasons.append(
            f"Score {_best_score}/7 (need {min_score}) [{_best_dir}]: "
            f"ribbon={'Y' if _r_ok else 'N'} "
            f"vwap={'Y' if _v_ok else 'N'} "
            f"roc={'Y' if _rc_ok else 'N'} "
            f"vol={'Y' if vol_ok else 'N'}({vol_out.value:.2f}\u00d7) "
            f"atr={'Y' if atr_active else 'N'} "
            f"cross={'Y' if _cr_ok else 'N'}"
        )
        return _hold(hold_reasons)
