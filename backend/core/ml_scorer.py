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

    def _load_model_short(self, symbol: str, timeframe: str = "1d") -> Optional[dict]:
        """G6: Load the dedicated SHORT model for `symbol:timeframe` from latest.json.
        Falls back to None if the short model was not trained (e.g. insufficient data).
        """
        if not _JOBLIB_OK:
            return None
        if not _LATEST_JSON.exists():
            return None
        try:
            registry: dict = json.loads(_LATEST_JSON.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        model_path = registry.get(f"{symbol}:{timeframe}:short")
        if not model_path or not pathlib.Path(model_path).exists():
            return None
        try:
            data = joblib.load(model_path)
            logger.info(f"[MLScorer] Loaded SHORT model for {symbol} from {model_path}")
            return data
        except Exception as exc:
            logger.warning(f"[MLScorer] Failed to load SHORT model for {symbol}: {exc}")
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

    def _get_model_short(self, symbol: str, timeframe: str = "1d") -> Optional[dict]:
        """G6: Cached load for the dedicated SHORT model."""
        cache_key = f"{symbol}:{timeframe}:short"
        with self._lock:
            if cache_key in self._models:
                return self._models[cache_key]
        model = self._load_model_short(symbol, timeframe)
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

        # GAP-07 FIX: warn when model is stale so operators know to trigger a retrain.
        # Staleness ≥ 14 days emits a warning; ≥ 30 days emits a critical alert.
        _trained_at = model_data.get("trained_at")
        if _trained_at:
            try:
                import datetime as _dt
                _trained_date = _dt.date.fromisoformat(str(_trained_at))
                _days_old = (_dt.date.today() - _trained_date).days
                if _days_old >= 30:
                    logger.warning(
                        f"[MLScorer] CRITICAL STALE MODEL: {symbol}:{timeframe} is {_days_old} days old "
                        f"(trained {_trained_at}). Predictions may be unreliable — retrain immediately."
                    )
                elif _days_old >= 14:
                    logger.warning(
                        f"[MLScorer] STALE MODEL: {symbol}:{timeframe} is {_days_old} days old "
                        f"(trained {_trained_at}). Consider triggering a retrain."
                    )
            except Exception:
                pass

        feats = _compute_features(df)
        if feats is None:
            return None

        # G5: add regime_code for the latest candle so inference matches training.
        # Compute regime on the last 50 rows (same window used in trainer._add_regime_codes).
        try:
            from core.regime_classifier import regime_classifier as _rc
            from core.features import _REGIME_ENCODING
            _window = df.tail(50) if len(df) >= 50 else df
            _regime_result = _rc.classify(_window)
            _regime_code = _REGIME_ENCODING.get(_regime_result.regime, 0)
            feats = feats.copy()
            feats["regime_code"] = _regime_code
        except Exception as _re:
            logger.debug(f"[MLScorer] regime_code computation skipped: {_re}")
            feats["regime_code"] = 0  # fallback to ranging

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

    def predict_proba_short(self, df: pd.DataFrame, symbol: str,
                            timeframe: str = "1d") -> Optional[float]:
        """
        G6: Return P(SHORT_WIN) ∈ [0, 1] using the dedicated SHORT XGBoost model.

        Uses a separate model trained on labels where price FALLS > 1.5×ATR,
        which is more accurate than inverting P(BUY).  Falls back to
        `1.0 - predict_proba()` when no SHORT model exists (e.g. pre-retrain).

        Parameters
        ----------
        df        : OHLCV DataFrame
        symbol    : Trading symbol
        timeframe : TF-specific model key
        """
        model_data = self._get_model_short(symbol, timeframe)
        if model_data is None:
            # Fallback: invert the BUY probability (old behaviour)
            buy_prob = self.predict_proba(df, symbol, timeframe)
            if buy_prob is None:
                return None
            return round(1.0 - buy_prob, 4)

        feats = _compute_features(df)
        if feats is None:
            return None
        try:
            from core.regime_classifier import regime_classifier as _rc
            from core.features import _REGIME_ENCODING
            _window = df.tail(50) if len(df) >= 50 else df
            _regime_result = _rc.classify(_window)
            feats = feats.copy()
            feats["regime_code"] = _REGIME_ENCODING.get(_regime_result.regime, 0)
        except Exception:
            feats["regime_code"] = 0

        row = feats.tail(1)
        if row.empty:
            return None

        model_features: list = model_data.get("features") or FEATURE_COLS
        missing = [c for c in model_features if c not in row.columns]
        if missing:
            return None

        try:
            model = model_data["model"]
            X = row[model_features].values
            prob = float(model.predict_proba(X)[0, 1])
            return round(prob, 4)
        except Exception as exc:
            logger.warning(f"[MLScorer] SHORT inference failed for {symbol}: {exc}")
            return None


# ── Singleton ────────────────────────────────────────────────────────────────
# Import this everywhere — models are loaded once and shared across strategies.
ml_scorer = MLScorer()
