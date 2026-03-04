"""
ML Scorer — inference utility for all strategies.

Loads the trained XGBoost model saved by ModelTrainer and returns a
BUY-probability score for a given OHLCV DataFrame.

Usage
-----
    from core.ml_scorer import MLScorer
    scorer = MLScorer()                  # loads once, cached in-process
    prob = scorer.predict_proba(df, symbol="BTC/USDT")
    # prob ∈ [0, 1] — probability the price will rise > 1.5×ATR in 24 candles
    # returns None if no model is available (safe fallback)
"""

import json
import pathlib
import threading
from typing import Optional
from loguru import logger

import numpy as np
import pandas as pd

_BASE_DIR = pathlib.Path(__file__).resolve().parent.parent  # backend/
_LATEST_JSON = _BASE_DIR / "data" / "models" / "latest.json"

# ── optional heavy imports ───────────────────────────────────────────────────
try:
    import joblib
    _JOBLIB_OK = True
except ImportError:
    _JOBLIB_OK = False


def _compute_features(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """
    Compute the same 6-feature vector used during training.
    Returns None if the DataFrame is too short.
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

    # Bollinger Band position (0 = lower, 1 = upper)
    bb_sma = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    bb_range = (bb_sma + 2 * bb_std) - (bb_sma - 2 * bb_std)
    bb_pct = (close - (bb_sma - 2 * bb_std)) / bb_range.replace(0, np.nan)

    # 1-period log return
    log_ret = np.log(close / close.shift(1))

    out = pd.DataFrame({
        "rsi": rsi,
        "macd_hist": macd_hist,
        "atr_norm": atr_norm,
        "vol_ratio": vol_ratio,
        "bb_pct": bb_pct,
        "log_ret": log_ret,
    })
    return out


FEATURE_COLS = ["rsi", "macd_hist", "atr_norm", "vol_ratio", "bb_pct", "log_ret"]


class MLScorer:
    """
    Thread-safe, lazily-loaded ML scorer.

    One shared instance is used across all strategies (imported as singleton).
    Models are loaded on first use and cached until the process restarts.
    Call `reload()` after a retrain to pick up the latest model.
    """

    def __init__(self):
        self._models: dict = {}        # symbol → loaded model dict
        self._lock = threading.Lock()

    def _load_model(self, symbol: str) -> Optional[dict]:
        """Load the model for `symbol` from latest.json registry."""
        if not _JOBLIB_OK:
            return None
        if not _LATEST_JSON.exists():
            return None
        try:
            registry: dict = json.loads(_LATEST_JSON.read_text())
        except (json.JSONDecodeError, OSError):
            return None

        # Try exact symbol first, then normalised (BTC/USDT → BTC_USDT)
        model_path = registry.get(symbol) or registry.get(symbol.replace("/", "_"))
        if not model_path:
            # Try fuzzy: check if any key starts with the base currency
            base = symbol.split("/")[0]
            for k, v in registry.items():
                if k.startswith(base):
                    model_path = v
                    break

        if not model_path or not pathlib.Path(model_path).exists():
            return None

        try:
            data = joblib.load(model_path)
            logger.info(f"[MLScorer] Loaded model for {symbol} from {model_path}")
            return data
        except Exception as exc:
            logger.warning(f"[MLScorer] Failed to load model for {symbol}: {exc}")
            return None

    def _get_model(self, symbol: str) -> Optional[dict]:
        """Return cached model or load it."""
        with self._lock:
            if symbol not in self._models:
                self._models[symbol] = self._load_model(symbol)
            return self._models[symbol]

    def reload(self):
        """Clear model cache so next call re-reads from disk."""
        with self._lock:
            self._models.clear()
        logger.info("[MLScorer] Model cache cleared — will reload on next prediction")

    def predict_proba(self, df: pd.DataFrame, symbol: str) -> Optional[float]:
        """
        Return P(BUY) ∈ [0, 1] using the trained XGBoost model.
        Returns None if no model is available (safe — strategy falls back to rule score).

        Parameters
        ----------
        df     : OHLCV DataFrame (columns: open/high/low/close/volume)
        symbol : Trading symbol, used to find the right model file
        """
        model_data = self._get_model(symbol)
        if model_data is None:
            return None

        feats = _compute_features(df)
        if feats is None:
            return None

        # Drop rows with NaN and take only the LAST row for inference
        row = feats.dropna().tail(1)
        if row.empty:
            return None

        # Validate all required columns are present
        missing = [c for c in FEATURE_COLS if c not in row.columns]
        if missing:
            return None

        try:
            model = model_data["model"]
            X = row[FEATURE_COLS].values
            prob = float(model.predict_proba(X)[0, 1])
            return round(prob, 4)
        except Exception as exc:
            logger.warning(f"[MLScorer] Inference failed for {symbol}: {exc}")
            return None


# ── Singleton ────────────────────────────────────────────────────────────────
# Import this everywhere — models are loaded once and shared across strategies.
ml_scorer = MLScorer()
