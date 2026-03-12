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
import asyncio
from typing import Optional
from loguru import logger

import numpy as np
import pandas as pd

_BASE_DIR = pathlib.Path(__file__).resolve().parent.parent  # backend/
_LATEST_JSON = _BASE_DIR / "data" / "models" / "latest.json"

# B4: shared feature computation — single source of truth with trainer.py
from core.features import compute_features as _compute_features, FEATURE_COLS

# ── optional heavy imports ───────────────────────────────────────────────────
try:
    import joblib
    _JOBLIB_OK = True
except ImportError:
    _JOBLIB_OK = False


# FEATURE_COLS and _compute_features imported from core.features above
# (B4: removed local duplicate)


class MLScorer:
    """
    Thread-safe, lazily-loaded ML scorer.

    One shared instance is used across all strategies (imported as singleton).
    Models are loaded on first use and cached until the process restarts.
    Call `reload()` after a retrain to pick up the latest model.

    G8: Models are stored per (symbol, timeframe) in latest.json under the key
    "symbol:timeframe" (e.g. "BTC/USDT:1h"). The scorer falls back to a symbol-only
    key for backward compatibility with models trained before this change.
    """

    def __init__(self):
        self._models: dict = {}        # "symbol:timeframe" → loaded model dict
        import threading
        self._lock = threading.Lock()

    def _load_model(self, symbol: str, timeframe: str = "1d") -> Optional[dict]:
        """Load the model for `symbol:timeframe` from latest.json registry.

        Lookup order:
        1. Exact key  "symbol:timeframe"  (new TF-aware format, G8)
        2. Legacy key  "symbol"            (models trained before G8 fix)
        3. Normalised symbol variants (BTC/USDT → BTC_USDT)
        """
        if not _JOBLIB_OK:
            return None
        if not _LATEST_JSON.exists():
            return None
        try:
            registry: dict = json.loads(_LATEST_JSON.read_text())
        except (json.JSONDecodeError, OSError):
            return None

        # 1. TF-aware key (new format: "BTC/USDT:1h")
        model_path = registry.get(f"{symbol}:{timeframe}")

        # 2. Legacy symbol-only key (old format: "BTC/USDT" or "BTC_USDT")
        if not model_path:
            model_path = registry.get(symbol) or registry.get(symbol.replace("/", "_"))

        # 3. Partial-match fallback: base+quote normalised (BTC/USDT ↔ BTC_USDT)
        if not model_path:
            parts = symbol.split("/")
            if len(parts) == 2:
                base, quote = parts
                for k, v in registry.items():
                    k_sym = k.split(":")[0] if ":" in k else k
                    k_norm = k_sym.replace("/", "_")
                    if k_norm == f"{base}_{quote}":
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

    def _get_model(self, symbol: str, timeframe: str = "1d") -> Optional[dict]:
        """Return cached model or load it (double-checked locking).

        L-7 FIX: _load_model() does file I/O so it must NOT be called while
        holding the lock — that would serialize all concurrent callers even
        when the model is already cached.  Load outside the lock, then
        re-check-and-cache under the lock so only one entry is stored.
        """
        cache_key = f"{symbol}:{timeframe}"
        # Fast path — already in cache
        with self._lock:
            if cache_key in self._models:
                return self._models[cache_key]
        # Slow path — load without holding the lock
        model = self._load_model(symbol, timeframe)
        # Store result; another thread may have beaten us — that's fine
        with self._lock:
            if cache_key not in self._models:
                self._models[cache_key] = model
            return self._models[cache_key]

    def reload(self):
        """Clear model cache so next call re-reads from disk."""
        with self._lock:
            self._models.clear()
        logger.info("[MLScorer] Model cache cleared — will reload on next prediction")

    def predict_proba(self, df: pd.DataFrame, symbol: str,
                      timeframe: str = "1d") -> Optional[float]:
        """
        Return P(BUY) ∈ [0, 1] using the trained XGBoost model.
        Returns None if no model is available (safe — strategy falls back to rule score).

        Parameters
        ----------
        df        : OHLCV DataFrame (columns: open/high/low/close/volume)
        symbol    : Trading symbol, used to find the right model file
        timeframe : G8 — selects the TF-specific model (e.g. '1h', '4h', '1d')
        """
        model_data = self._get_model(symbol, timeframe)
        if model_data is None:
            return None

        feats = _compute_features(df)
        if feats is None:
            return None

        # Take the last row for inference — do NOT dropna here.
        # XGBoost handles NaN natively via its missing-value split direction,
        # so passing NaN (e.g. vol_ratio=NaN for forex) is correct and safe.
        row = feats.tail(1)
        if row.empty:
            return None

        # Use the feature list saved with this model (may be a subset of FEATURE_COLS
        # when the model was trained on an asset class that lacks some features, e.g.
        # forex has no volume → vwap_ratio is always NaN and may be excluded).
        model_features: list = model_data.get("features") or FEATURE_COLS

        # Validate all required columns are present
        missing = [c for c in model_features if c not in row.columns]
        if missing:
            return None

        try:
            model = model_data["model"]
            X = row[model_features].values
            prob = float(model.predict_proba(X)[0, 1])
            return round(prob, 4)
        except Exception as exc:
            logger.warning(f"[MLScorer] Inference failed for {symbol}: {exc}")
            return None


# ── Singleton ────────────────────────────────────────────────────────────────
# Import this everywhere — models are loaded once and shared across strategies.
ml_scorer = MLScorer()
