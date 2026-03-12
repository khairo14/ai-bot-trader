"""
Market Regime Classifier (ML-02)
==================================
Pure heuristic classifier — no training required.  Deterministic rules based
on ADX, normalised ATR, Bollinger-Band width, and EMA slope.

Regimes
-------
  trending_up      ADX > 25 and EMA slope positive
  trending_down    ADX > 25 and EMA slope negative
  high_volatility  Normalised ATR above volatility threshold (regardless of ADX)
  low_volatility   BB width very tight (squeeze)
  ranging          Everything else

Per-regime strategy adjustments (applied by each strategy)
-----------------------------------------------------------
  trending_up    → favour BUY   — lower BUY min_score, raise SHORT min_score
  trending_down  → favour SHORT — raise BUY min_score, lower SHORT min_score
  ranging        → raise both   — mean-reversion works; breakouts are noise
  high_volatility→ raise both   — reduce position sizing confidence
  low_volatility → lower both slightly — tighter conditions
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional
from loguru import logger

from tools.basic.volume_atr_adx import ATR, ADX
from tools.basic.bollinger_bands import BollingerBands
from tools.basic.moving_averages import MovingAverages


# ── Regime constants ─────────────────────────────────────────────────────────

REGIME_TRENDING_UP    = "trending_up"
REGIME_TRENDING_DOWN  = "trending_down"
REGIME_HIGH_VOL       = "high_volatility"
REGIME_LOW_VOL        = "low_volatility"
REGIME_RANGING        = "ranging"

ALL_REGIMES = [
    REGIME_TRENDING_UP,
    REGIME_TRENDING_DOWN,
    REGIME_HIGH_VOL,
    REGIME_LOW_VOL,
    REGIME_RANGING,
]

# ── Per-regime min_score adjustments (delta from strategy default) ────────────
# Positive = harder to trigger; Negative = easier to trigger.
REGIME_SCORE_ADJUSTMENTS: dict[str, dict[str, int]] = {
    REGIME_TRENDING_UP: {
        "long_delta":  -1,   # BUY easier
        "short_delta": +1,   # SHORT harder
    },
    REGIME_TRENDING_DOWN: {
        "long_delta":  +1,   # BUY harder
        "short_delta": -1,   # SHORT easier
    },
    REGIME_RANGING: {
        "long_delta":  +1,   # both harder — let mean-reversion strategy handle it
        "short_delta": +1,
    },
    REGIME_HIGH_VOL: {
        "long_delta":  +1,   # both harder — wider spreads, more false signals
        "short_delta": +1,
    },
    REGIME_LOW_VOL: {
        "long_delta":  0,    # neutral — wait for volatility to expand
        "short_delta": 0,
    },
}

# ── ATR multipliers for SL/TP per regime ─────────────────────────────────────
REGIME_ATR_MULTIPLIERS: dict[str, dict[str, float]] = {
    REGIME_TRENDING_UP:   {"sl": 2.0, "tp": 4.0},
    REGIME_TRENDING_DOWN: {"sl": 2.0, "tp": 4.0},
    REGIME_RANGING:       {"sl": 1.5, "tp": 2.5},
    REGIME_HIGH_VOL:      {"sl": 2.5, "tp": 3.5},  # wider SL; reduced TP
    REGIME_LOW_VOL:       {"sl": 2.0, "tp": 3.0},  # raised from 1.5 — 1.5×ATR too easily swept by Forex intra-candle wicks
}


@dataclass
class RegimeResult:
    regime: str
    features: dict = field(default_factory=dict)
    confidence: float = 1.0   # reserved for future probabilistic extension


class RegimeClassifier:
    """
    Heuristic market-regime detector.

    Usage
    -----
        result = regime_classifier.classify(ohlcv_df)
        print(result.regime)          # e.g. "trending_up"
        print(result.features)        # {"adx": 32.1, "atr_norm": 0.012, ...}
    """

    # Thresholds (class-level, easily tunable)
    # IMP-30: per-asset-class ADX thresholds replace the single global value.
    # Crypto / FX trend more aggressively and need a lower threshold to catch
    # early trend entries; equities follow the textbook 25-level.
    ADX_TREND_THRESHOLD:   float = 25.0   # fallback (used when asset_class unknown)
    ADX_THRESHOLD_BY_CLASS: dict[str, float] = {
        "crypto":  20.0,   # fast-trending, high ADX sensitivity
        "forex":   22.0,   # trending but mean-reverts faster than equities
        "stock":   25.0,   # textbook threshold for equities
        "options": 25.0,   # options premium decays; use equity threshold
    }
    ATR_NORM_HIGH_VOL:     float = 0.035  # ATR/price above this → high volatility
    BB_WIDTH_LOW_VOL:      float = 0.02   # BB width ((upper-lower)/middle) below → squeeze
    EMA_SLOPE_PERIODS:     int   = 15     # candles used to compute EMA slope

    def __init__(self):
        self._atr = ATR()
        self._adx = ADX()
        self._bb  = BollingerBands()
        self._ma  = MovingAverages()

    def classify(self, data: pd.DataFrame, asset_class: str = "") -> RegimeResult:
        """
        Classify the current market regime from OHLCV data.

        Parameters
        ----------
        data : pd.DataFrame
            OHLCV DataFrame with columns: open, high, low, close, volume.
            Minimum 50 rows recommended; falls back to ``ranging`` if too short.
        asset_class : str
            Asset class hint for per-class ADX threshold selection.
            One of 'crypto', 'forex', 'stock', 'options', or '' (global default).

        Returns
        -------
        RegimeResult
        """
        if len(data) < 30:
            logger.debug("[Regime] Insufficient data — defaulting to 'ranging'")
            return RegimeResult(regime=REGIME_RANGING, features={}, confidence=0.0)

        try:
            return self._classify(data, asset_class=asset_class)
        except Exception as exc:
            logger.warning(f"[Regime] Classification error — defaulting to 'ranging': {exc}")
            return RegimeResult(regime=REGIME_RANGING, features={}, confidence=0.0)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _classify(self, data: pd.DataFrame, asset_class: str = "") -> RegimeResult:
        # IMP-30: select ADX threshold based on asset class
        _ac = asset_class.lower() if asset_class else ""
        adx_threshold = self.ADX_THRESHOLD_BY_CLASS.get(_ac, self.ADX_TREND_THRESHOLD)

        # ── Compute indicators ────────────────────────────────────────────
        atr_out = self._atr.calculate(data)
        adx_out = self._adx.calculate(data)
        bb_out  = self._bb.calculate(data, period=20, std_dev=2.0)
        ma_out  = self._ma.calculate(data, fast_period=9, slow_period=21)

        current_price = float(data["close"].iloc[-1])
        adx_val       = float(adx_out.value)

        # Normalised ATR: ATR as fraction of price
        atr_norm = float(atr_out.value) / current_price if current_price > 0 else 0.0

        # EMA slope: compute from a proper EMA series, not raw closes.
        # Raw closes are noisy; the Pandas EMA smooths out intraday wicks.
        _ema_period = 9
        _ema_series = data["close"].ewm(span=_ema_period, adjust=False).mean()
        if len(_ema_series) >= self.EMA_SLOPE_PERIODS + 1:
            _slope_now   = float(_ema_series.iloc[-1])
            _slope_past  = float(_ema_series.iloc[-self.EMA_SLOPE_PERIODS - 1])
        else:
            _slope_now  = float(data["close"].iloc[-1])
            _slope_past = float(data["close"].iloc[0])
        ema_slope = (_slope_now - _slope_past) / abs(_slope_past) if _slope_past != 0 else 0.0

        # BB width: (upper - lower) / middle
        upper  = bb_out.metadata.get("upper",  current_price * 1.02)
        lower  = bb_out.metadata.get("lower",  current_price * 0.98)
        middle = bb_out.metadata.get("middle", current_price)
        bb_width = (upper - lower) / middle if middle > 0 else 0.0

        features = {
            "adx":       round(adx_val, 2),
            "atr_norm":  round(atr_norm * 100, 4),   # in percent
            "bb_width":  round(bb_width * 100, 4),   # in percent
            "ema_slope": round(ema_slope * 100, 4),  # in percent per N candles
        }

        logger.debug(f"[Regime] features={features}")

        # ── Decision tree ─────────────────────────────────────────────────

        # Bug-16 FIX: check trending BEFORE high_volatility so that a strong
        # uptrend/downtrend in a volatile market (e.g. a crypto breakout with
        # elevated ATR) is labelled as trending, not just high_volatility.
        # High volatility without a clear trend direction is still high_volatility.

        # Trending regimes (ADX strength + EMA slope direction)
        if adx_val >= adx_threshold:
            if ema_slope >= 0:
                return RegimeResult(regime=REGIME_TRENDING_UP, features=features)
            else:
                return RegimeResult(regime=REGIME_TRENDING_DOWN, features=features)

        # High volatility — elevated ATR but no clear directional trend
        if atr_norm > self.ATR_NORM_HIGH_VOL:
            return RegimeResult(regime=REGIME_HIGH_VOL, features=features)

        # Low volatility / squeeze
        if bb_width < self.BB_WIDTH_LOW_VOL:
            return RegimeResult(regime=REGIME_LOW_VOL, features=features)

        # Default: ranging / consolidation
        return RegimeResult(regime=REGIME_RANGING, features=features)

    # ── Convenience helpers ───────────────────────────────────────────────────

    @staticmethod
    def score_adjustment(regime: str) -> dict[str, int]:
        """Return {'long_delta': int, 'short_delta': int} for the given regime."""
        return REGIME_SCORE_ADJUSTMENTS.get(regime, {"long_delta": 0, "short_delta": 0})

    @staticmethod
    def atr_multipliers(regime: str) -> dict[str, float]:
        """Return {'sl': float, 'tp': float} ATR multipliers for the given regime."""
        return REGIME_ATR_MULTIPLIERS.get(regime, {"sl": 2.0, "tp": 4.0})


# Singleton — import and use everywhere
regime_classifier = RegimeClassifier()
