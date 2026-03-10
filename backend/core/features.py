"""
Shared feature engineering for ML training and inference.

Single source of truth for the 6-feature vector used by both
ModelTrainer (training) and MLScorer (inference) so they never drift apart.
"""

from typing import Optional
import numpy as np
import pandas as pd

FEATURE_COLS = ["rsi", "macd_hist", "atr_norm", "vol_ratio", "bb_pct", "log_ret"]


def compute_features(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """
    Compute the 6-feature vector for ML training and inference.

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

    # Volume ratio vs 20-period mean
    vol_ratio = volume / volume.rolling(20).mean().replace(0, np.nan)

    # Bollinger Band position (0 = lower band, 1 = upper band)
    bb_sma = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    bb_range = (bb_sma + 2 * bb_std) - (bb_sma - 2 * bb_std)
    bb_pct = (close - (bb_sma - 2 * bb_std)) / bb_range.replace(0, np.nan)

    # 1-period log return
    log_ret = np.log(close / close.shift(1))

    return pd.DataFrame({
        "rsi": rsi,
        "macd_hist": macd_hist,
        "atr_norm": atr_norm,
        "vol_ratio": vol_ratio,
        "bb_pct": bb_pct,
        "log_ret": log_ret,
    })
