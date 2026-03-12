"""
Shared feature engineering for ML training and inference.

Single source of truth for the feature vector used by both
ModelTrainer (training) and MLScorer (inference) so they never drift apart.
"""

from typing import Optional
import numpy as np
import pandas as pd

FEATURE_COLS = [
    "rsi",
    "macd_hist",
    "atr_norm",
    "vol_ratio",
    "bb_pct",
    "log_ret",
    # IMP-22: additional features for richer multi-asset signal quality
    "stoch_k",      # Stochastic %K (14,3) — momentum oscillator, complements RSI
    "williams_r",   # Williams %R (14) — overbought/oversold, independent of RSI
    "roc_10",       # Rate of Change over 10 periods — trend momentum
    "vwap_ratio",   # close / VWAP — price relative to volume-weighted mean (NaN for forex)
    # G5 FIX: encode market regime so model can learn regime-specific signal quality.
    # Integer encoding: 0=ranging, 1=trending_up, 2=trending_down, 3=high_volatility, 4=low_volatility
    # Computed via sliding window in trainer.py; uses current regime in ml_scorer.py.
    "regime_code",
]


# G5: Integer encoding for market regime — must match regime_classifier constants.
_REGIME_ENCODING: dict[str, int] = {
    "ranging":          0,
    "trending_up":      1,
    "trending_down":    2,
    "high_volatility":  3,
    "low_volatility":   4,
}


def compute_features(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """
    Compute the feature vector for ML training and inference.

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV DataFrame with columns: open, high, low, close, volume.
        Index should be datetime-like.

    Returns
    -------
    pd.DataFrame with columns in FEATURE_COLS, or None if df is too short (< 30 rows).
    """
    if len(df) < 30:
        return None

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    # RSI-14
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))

    # MACD histogram (12/26/9)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_hist = (ema12 - ema26) - (ema12 - ema26).ewm(span=9, adjust=False).mean()

    # ATR-14 normalised by close
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr_norm = tr.rolling(14).mean() / close.replace(0, np.nan)

    # Volume ratio vs 20-period mean.
    # Forex/CFD pairs have volume=0 — keep as NaN rather than filling with 1.0.
    # XGBoost handles NaN natively via its missing-value split logic, so this is
    # the correct approach. Filling with 1.0 would teach the model that "forex
    # always has normal volume", which is a spurious feature correlation.
    vol_sma = volume.rolling(20).mean()
    vol_ratio = volume / vol_sma.replace(0, np.nan)
    # Do NOT fillna here — let NaN propagate so XGBoost uses its missing-value path.

    # Bollinger Band position (0 = lower band, 1 = upper band)
    bb_sma = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    bb_range = (bb_sma + 2 * bb_std) - (bb_sma - 2 * bb_std)
    bb_pct = (close - (bb_sma - 2 * bb_std)) / bb_range.replace(0, np.nan)

    # 1-period log return
    log_ret = np.log(close / close.shift(1))

    # IMP-22 — Stochastic %K (14,3): (close − low14) / (high14 − low14) smoothed over 3
    low14  = low.rolling(14).min()
    high14 = high.rolling(14).max()
    raw_k  = (close - low14) / (high14 - low14).replace(0, np.nan) * 100
    stoch_k = raw_k.rolling(3).mean()   # fast %K smoothed → %K

    # IMP-22 — Williams %R (14): (high14 − close) / (high14 − low14) × −100
    williams_r = (high14 - close) / (high14 - low14).replace(0, np.nan) * -100

    # IMP-22 — Rate of Change (10 periods): captures trend momentum independently
    # of oscillator-based features already present (RSI, Stoch).
    roc_10 = (close / close.shift(10).replace(0, np.nan) - 1) * 100

    # IMP-22 — VWAP ratio: close relative to the volume-weighted average price
    # over the trailing 20 periods. Proxy for "is price above/below the fair-value
    # anchor that institutions use?"  For forex (volume=0) this produces NaN,
    # which XGBoost handles natively.
    typical_price = (high + low + close) / 3
    tpv = typical_price * volume
    vwap = tpv.rolling(20).sum() / volume.rolling(20).sum().replace(0, np.nan)
    vwap_ratio = close / vwap.replace(0, np.nan)

    return pd.DataFrame({
        "rsi":        rsi,
        "macd_hist":  macd_hist,
        "atr_norm":   atr_norm,
        "vol_ratio":  vol_ratio,
        "bb_pct":     bb_pct,
        "log_ret":    log_ret,
        "stoch_k":    stoch_k,
        "williams_r": williams_r,
        "roc_10":     roc_10,
        "vwap_ratio": vwap_ratio,
        # regime_code is NOT computed here — it requires a sliding-window classifier
        # call which is expensive and creates a circular import.
        # • trainer.py adds regime_code via _add_regime_codes() on the full DataFrame.
        # • ml_scorer.py adds regime_code for the latest candle before inference.
        # The column is left absent here; callers that need it add it themselves.
    })
