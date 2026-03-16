"""
ScalpingMLScorer — inference utility dedicated to scalping strategies.

Loads XGBoost models trained by tasks/scalping_ml_retrain.py.
Model registry keys use the suffix ":scalp" to avoid collisions with the main
swing ML models (which use keys like "BTC/USDT:5m").

  Buy model:   registry key "BTC/USDT:5m:scalp"
  Short model: registry key "BTC/USDT:5m:scalp:short"

Until Phase 4 (scalping_ml_retrain.py) trains real models, all calls to
predict_proba_scalp() return None — the caller treats None as "no veto"
and the strategy falls back to pure rule-based scoring.  This is safe by
design: adding the ML gate later only *tightens* signal quality.
"""

import json
import pathlib
import threading
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

_BASE_DIR    = pathlib.Path(__file__).resolve().parent.parent  # backend/
_LATEST_JSON = _BASE_DIR / "data" / "models" / "latest.json"

try:
    import joblib
    _JOBLIB_OK = True
except ImportError:
    _JOBLIB_OK = False


# ── Feature columns (13) — superset of swing's 11 with scalp-specific extras ──
SCALP_FEATURE_COLS = [
    "rsi",
    "macd_hist",
    "atr_norm",
    "vol_ratio",
    "bb_pct",
    "log_ret",
    "ema_ribbon_spread",   # (EMA8 − EMA21) / close — ribbon tension
    "vwap_dist_pct",       # (close − VWAP) / VWAP × 100
    "roc_5",               # Rate of Change over 5 periods
    "spread_pct",          # (ask − bid) / mid (set to 0.0 when unavailable)
    "time_of_day_sin",     # sin(2π × minute_of_day / 1440)
    "time_of_day_cos",     # cos(2π × minute_of_day / 1440)
    "session_id",          # 0=Asia 1=London 2=NY 3=Off-hours
]


def _compute_scalp_features(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Compute the scalping feature vector from an OHLCV DataFrame."""
    if len(df) < 30:
        return None

    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df["volume"]
    index  = df.index

    # RSI-14
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = 100 - (100 / (1 + rs))

    # MACD histogram (12/26/9)
    ema12     = close.ewm(span=12, adjust=False).mean()
    ema26     = close.ewm(span=26, adjust=False).mean()
    macd_hist = (ema12 - ema26) - (ema12 - ema26).ewm(span=9, adjust=False).mean()

    # ATR-14 normalised
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=14, adjust=False).mean()
    atr_norm = atr / close

    # Volume ratio
    vol_sma   = volume.rolling(20).mean()
    vol_ratio = volume / vol_sma.replace(0, np.nan)

    # Bollinger Band %B (0=lower band, 1=upper band)
    bb_mid  = close.rolling(20).mean()
    bb_std  = close.rolling(20).std()
    bb_up   = bb_mid + 2 * bb_std
    bb_low  = bb_mid - 2 * bb_std
    bb_pct  = (close - bb_low) / (bb_up - bb_low).replace(0, np.nan)

    # Log return
    log_ret = np.log(close / close.shift(1))

    # EMA ribbon spread  (EMA8 − EMA21) / close
    ema8  = close.ewm(span=8,  adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema_ribbon_spread = (ema8 - ema21) / close

    # Rolling VWAP (20-period)
    typical = (high + low + close) / 3
    vwap_roll = (typical * volume).rolling(20).sum() / volume.rolling(20).sum().replace(0, np.nan)
    vwap_dist_pct = (close - vwap_roll) / vwap_roll.replace(0, np.nan) * 100

    # ROC-5
    roc_5 = (close / close.shift(5) - 1) * 100

    # Spread — not available from OHLCV; default to 0 (filled from live data by trainer)
    spread_pct = pd.Series(0.0, index=index)

    # Time-of-day encoding from index
    # Guard: broker may return a RangeIndex (integer) — pd.DatetimeIndex() raises on that.
    try:
        dti = pd.DatetimeIndex(pd.to_datetime(index, utc=True))
        _hours   = dti.hour
        _minutes = dti.hour * 60 + dti.minute
    except Exception:
        # Fallback: treat all rows as UTC midnight (session=Asia, tod=0)
        _hours   = pd.array([0] * len(index), dtype="int64")
        _minutes = pd.array([0] * len(index), dtype="int64")
    tod = pd.Series(_minutes, index=index)
    tod_sin = np.sin(2 * np.pi * tod / 1440)
    tod_cos = np.cos(2 * np.pi * tod / 1440)

    # Session ID: 0=Asia(0-7h UTC), 1=London(7-15h UTC), 2=NY(13-21h UTC), 3=Off
    def _session(h: int) -> int:
        if 0  <= h < 7:   return 0   # Asia
        if 7  <= h < 13:  return 1   # London
        if 13 <= h < 21:  return 2   # NY
        return 3
    session_id = pd.Series([_session(int(h)) for h in _hours], index=index)

    feat = pd.DataFrame({
        "rsi":               rsi,
        "macd_hist":         macd_hist,
        "atr_norm":          atr_norm,
        "vol_ratio":         vol_ratio,
        "bb_pct":            bb_pct,
        "log_ret":           log_ret,
        "ema_ribbon_spread": ema_ribbon_spread,
        "vwap_dist_pct":     vwap_dist_pct,
        "roc_5":             roc_5,
        "spread_pct":        spread_pct,
        "time_of_day_sin":   tod_sin,
        "time_of_day_cos":   tod_cos,
        "session_id":        session_id,
    }, index=index)

    feat = feat.replace([np.inf, -np.inf], np.nan).ffill().bfill()
    return feat[SCALP_FEATURE_COLS]


class ScalpingMLScorer:
    """
    Thread-safe, lazily-loaded ML scorer for scalping models.

    Registry key format:
      Buy:   "SYMBOL:TIMEFRAME:scalp"           e.g. "BTC/USDT:5m:scalp"
      Short: "SYMBOL:TIMEFRAME:scalp:short"     e.g. "BTC/USDT:5m:scalp:short"

    Returns None when no model is available (safe — caller treats as no veto).
    """

    def __init__(self):
        self._models: dict = {}
        self._lock = threading.Lock()

    def _load(self, key: str) -> Optional[object]:
        if not _JOBLIB_OK or not _LATEST_JSON.exists():
            return None
        try:
            registry: dict = json.loads(_LATEST_JSON.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        model_path = registry.get(key)
        if not model_path:
            return None
        import pathlib as _pl
        if not _pl.Path(model_path).exists():
            return None
        try:
            model_data = joblib.load(model_path)
            logger.info(f"[ScalpingMLScorer] Loaded model '{key}' from {model_path}")
            return model_data
        except Exception as exc:
            logger.warning(f"[ScalpingMLScorer] Failed to load '{key}': {exc}")
            return None

    def _get(self, key: str) -> Optional[object]:
        """Double-checked locking — avoids holding lock during file I/O."""
        with self._lock:
            if key in self._models:
                return self._models[key]
        model = self._load(key)
        with self._lock:
            if key not in self._models:
                self._models[key] = model
            return self._models[key]

    def _predict(self, model_data: object, df: pd.DataFrame, spread_pct: float = 0.0) -> Optional[float]:
        feat = _compute_scalp_features(df)
        if feat is None or feat.empty:
            return None
        row = feat.iloc[[-1]].copy()
        # Inject the live bid/ask spread — trained with 0.0 for OHLCV-only rows,
        # so passing the real spread at inference gives the model truer input.
        row["spread_pct"] = spread_pct
        try:
            model   = model_data["model"]       # type: ignore[index]
            prob    = float(model.predict_proba(row)[0][1])
            return prob
        except Exception as exc:
            logger.debug(f"[ScalpingMLScorer] Inference error: {exc}")
            return None

    def predict_proba_scalp(
        self, df: pd.DataFrame, symbol: str, timeframe: str, spread_pct: float = 0.0
    ) -> Optional[float]:
        """P(scalp_win) for BUY direction. Returns None when model absent."""
        key   = f"{symbol}:{timeframe}:scalp"
        model = self._get(key)
        if model is None:
            return None
        return self._predict(model, df, spread_pct=spread_pct)

    def predict_proba_scalp_short(
        self, df: pd.DataFrame, symbol: str, timeframe: str, spread_pct: float = 0.0
    ) -> Optional[float]:
        """P(scalp_short_win) for SHORT direction. Returns None when model absent."""
        key   = f"{symbol}:{timeframe}:scalp:short"
        model = self._get(key)
        if model is None:
            return None
        return self._predict(model, df, spread_pct=spread_pct)

    def reload(self) -> None:
        """Evict all cached models — they'll be lazily reloaded on next predict."""
        with self._lock:
            self._models.clear()
        logger.info("[ScalpingMLScorer] Model cache cleared — will reload on next inference.")


# Module-level singleton — import and use this everywhere.
scalping_ml_scorer = ScalpingMLScorer()
