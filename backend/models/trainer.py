"""
ML Model Trainer
=================
Full feature-engineering + XGBoost training pipeline.

Pipeline per symbol
-------------------
1.  Pull 90 days of daily OHLCV via yfinance (broker-agnostic historical data).
2.  Compute features: RSI-14, MACD histogram, ATR-14 (normalised), volume ratio,
    Bollinger Band position, 1-day log return.
3.  Label: 1 if price rises > 1.5 × ATR(14) within the next 24 candles, else 0.
4.  Train XGBoostClassifier (100 trees, max_depth=4) with StratifiedKFold(5) CV.
5.  Evaluate on 20 % holdout — minimum AUC 0.55 to accept model.
6.  Serialise to  data/models/<symbol_clean>_<YYYY-MM-DD>.pkl  with joblib.

Model path is also written to  data/models/latest.json  so HybridStrategy
can load it at runtime.
"""

import os
import json
import datetime
import pathlib
import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# B4: import shared feature computation — single source of truth with ml_scorer.py
from core.features import compute_features as _compute_features_shared, FEATURE_COLS

# ── optional heavy imports (gracefully degrade if not installed) ─────────────
try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False
    logger.warning("yfinance not installed — ModelTrainer will skip data fetching")

try:
    import xgboost as xgb
    from sklearn.model_selection import StratifiedKFold, train_test_split
    from sklearn.metrics import roc_auc_score
    from sklearn.calibration import CalibratedClassifierCV
    import joblib
    _ML_AVAILABLE = True
except ImportError:
    _ML_AVAILABLE = False
    logger.warning("xgboost / scikit-learn / joblib not installed — ModelTrainer disabled")

# ── paths ────────────────────────────────────────────────────────────────────
_BASE_DIR = pathlib.Path(__file__).resolve().parent.parent  # backend/
MODEL_DIR = _BASE_DIR / "data" / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)

# G8: per-timeframe yfinance fetch profile
# timeframe → (yf_interval, history_days, label_horizon_candles, min_labelled_rows)
_TF_PROFILE: dict[str, tuple[str, int, int, int]] = {
    "1m":  ("1m",   7,    60, 200),
    "5m":  ("5m",   60,   36, 200),
    "15m": ("15m",  60,   30, 100),
    "30m": ("30m",  60,   24,  80),
    "1h":  ("60m",  730,  24,  60),
    "2h":  ("60m",  730,  20,  60),   # fetch 1h then resample to 2h
    "4h":  ("60m",  730,  20,  60),   # fetch 1h then resample to 4h
    "1d":  ("1d",   365,  10,  50),
    "1w":  ("1wk", 1825,   5,  30),
}
_DEFAULT_TF_PROFILE: tuple[str, int, int, int] = ("1d", 365, 10, 50)


def _symbol_to_yf(symbol: str) -> str:
    """Convert exchange symbol format to yfinance ticker.

    BTC/USDT  →  BTC-USD      (crypto: base-quote)
    GBP/USD   →  GBPUSD=X     (forex: both legs are real FX currencies)
    AAPL      →  AAPL          (stocks pass through)
    """
    # Base token aliases: exchange ticker → yfinance ticker
    _BASE_ALIASES: dict[str, str] = {
        # POL (formerly MATIC / Polygon) — MATIC-USD was delisted on Yahoo Finance
        # when Polygon rebranded in 2023. The new POL-USD ticker is also not yet
        # coverage. Map to a known unavailable stub; the fetch will return "no data"
        # which is handled downstream as "insufficient OHLCV data" (acceptable).
    }
    # Real fiat currencies — if BOTH legs are in this set it's a forex pair
    _REAL_FX = {
        "USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD",
        "HKD", "SGD", "MXN", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF",
    }
    if "/" in symbol:
        base, quote = symbol.split("/", 1)
        base  = _BASE_ALIASES.get(base.upper(), base.upper())
        quote = quote.upper()
        # Forex pair: both legs are real currencies → yfinance uses BASEQUOTE=X
        if base in _REAL_FX and quote in _REAL_FX:
            return f"{base}{quote}=X"
        # Crypto: normalise stablecoins USDT/USDC → USD for yfinance
        quote_yf = "USD" if quote in ("USDT", "USDC", "BUSD") else quote
        return f"{base}-{quote_yf}"
    return symbol


def _fetch_ohlcv(symbol: str, days: int = 365, interval: str = "1d",
                 resample_to: Optional[str] = None) -> Optional[pd.DataFrame]:
    """Download OHLCV from yfinance at the requested interval.

    Args:
        symbol      : Trading symbol (e.g. BTC/USDT or AAPL).
        days        : Number of calendar days of history to request.
        interval    : yfinance interval string (1m/5m/15m/30m/60m/1d/1wk).
        resample_to : Optional pandas offset alias to resample bar size after
                      download (e.g. '2h' or '4h' — not natively in yfinance).
    """
    if not _YF_AVAILABLE:
        return None
    ticker = _symbol_to_yf(symbol)
    try:
        # Use period= instead of start/end so Yahoo Finance's 730-day rolling
        # window for intraday intervals works correctly regardless of wall-clock.
        df = yf.download(ticker, period=f"{days}d",
                         interval=interval, progress=False, auto_adjust=True)
        if df.empty:
            logger.warning(f"[trainer] yfinance returned empty data for {ticker}")
            return None
        df = df.rename(columns=str.lower)
        df.index = pd.to_datetime(df.index)
        # Flatten MultiIndex columns that yfinance sometimes produces
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        df = df[["open", "high", "low", "close", "volume"]].dropna()
        # Resample to a wider bar size (e.g. 1h → 4h) when yfinance lacks native support
        if resample_to and not df.empty:
            df = df.resample(resample_to).agg({
                "open":   "first",
                "high":   "max",
                "low":    "min",
                "close":  "last",
                "volume": "sum",
            }).dropna()
        return df
    except Exception as exc:
        logger.warning(f"[trainer] yfinance fetch failed for {ticker}: {exc}")
        return None


def _compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Compute features using shared module + add labeling helpers (_atr14, _close)."""
    # B4: use shared compute_features; add helpers needed by _label()
    out = _compute_features_shared(df)
    if out is None:
        return pd.DataFrame()
    close = df["close"]
    high = df["high"]
    low = df["low"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    out["_atr14"] = tr.rolling(14).mean()
    out["_close"] = close
    return out


def _add_regime_codes(feat_df: pd.DataFrame, ohlcv_df: pd.DataFrame) -> pd.DataFrame:
    """G5 FIX: Compute market regime for every row in the training DataFrame.

    Uses a 50-candle rolling window to classify regime at each point in time,
    matching what the live system does at inference.  The regime is encoded as
    an integer (see features._REGIME_ENCODING) and stored in the `regime_code`
    column so XGBoost can learn regime-specific signal quality.

    Falls back gracefully: if the classifier raises for a window, that row's
    regime_code is set to 0 (ranging — the safe default).
    """
    from core.regime_classifier import regime_classifier as _rc
    from core.features import _REGIME_ENCODING

    WINDOW = 50
    codes: list[int] = []
    n = len(ohlcv_df)

    for i in range(n):
        if i < WINDOW - 1:
            codes.append(0)   # not enough history → default to ranging
            continue
        window_df = ohlcv_df.iloc[i - WINDOW + 1: i + 1]
        try:
            result = _rc.classify(window_df)
            codes.append(_REGIME_ENCODING.get(result.regime, 0))
        except Exception:
            codes.append(0)

    feat_df = feat_df.copy()
    feat_df["regime_code"] = codes
    return feat_df


def _label(feat_df: pd.DataFrame, horizon: int = 24) -> pd.Series:
    """Binary label: 1 if price rises > 1.5 × ATR within `horizon` candles."""
    close = feat_df["_close"]
    atr14 = feat_df["_atr14"]
    labels = pd.Series(0, index=feat_df.index, dtype=int)
    for i in range(len(feat_df) - horizon):
        threshold = 1.5 * atr14.iloc[i]
        future_max = close.iloc[i + 1: i + 1 + horizon].max()
        if future_max - close.iloc[i] > threshold:
            labels.iloc[i] = 1
    # Last `horizon` rows have no future — mark NaN so they're dropped
    labels.iloc[-horizon:] = np.nan
    return labels


def _label_short(feat_df: pd.DataFrame, horizon: int = 24) -> pd.Series:
    """G6: Binary label for SHORT model: 1 if price FALLS > 1.5 × ATR within `horizon` candles."""
    close = feat_df["_close"]
    atr14 = feat_df["_atr14"]
    labels = pd.Series(0, index=feat_df.index, dtype=int)
    for i in range(len(feat_df) - horizon):
        threshold = 1.5 * atr14.iloc[i]
        future_min = close.iloc[i + 1: i + 1 + horizon].min()
        if close.iloc[i] - future_min > threshold:
            labels.iloc[i] = 1
    labels.iloc[-horizon:] = np.nan
    return labels


class ModelTrainer:
    """Trains and evaluates ML models for each active trading symbol."""

    MODEL_DIR: pathlib.Path = MODEL_DIR
    MIN_AUC: float = 0.55
    MIN_ROWS: int = 50     # minimum labelled rows to attempt training
    LIVE_LABEL_WEIGHT: int = 3   # repeat each live row N times (upweights real outcomes)

    async def retrain_all(self) -> dict:
        """Main entry point called by the Celery retrain task.

        Queries active strategies from the DB, trains one model per unique
        symbol, and returns a summary report dict.
        """
        symbols: list[tuple[str, str]] = []

        try:
            from db.database import AsyncSessionLocal
            from db.models import Strategy as StrategyModel
            from sqlalchemy import select

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(StrategyModel).where(StrategyModel.is_active == True)  # noqa: E712
                )
                strategies = result.scalars().all()

            for strat in strategies:
                # Skip scalp strategies — they have their own retrain task
                if (strat.strategy_type or "").startswith("scalp_"):
                    continue
                params = strat.parameters or {}
                sym = params.get("symbol")
                tf = params.get("timeframe", "1d")
                broker = str(strat.broker.value) if hasattr(strat.broker, "value") else str(strat.broker)
                if sym:
                    symbols.append((sym, broker, tf))

        except Exception as exc:
            logger.error(f"[trainer] DB query failed: {exc}")

        # G8: deduplicate by (symbol, timeframe) — same symbol on 1h and 4h trains separate models
        seen: set[tuple[str, str]] = set()
        unique: list[tuple[str, str, str]] = []
        for sym, broker, tf in symbols:
            key = (sym, tf)
            if key not in seen:
                seen.add(key)
                unique.append((sym, broker, tf))

        if not unique:
            logger.info("[trainer] No active strategy symbols found — skipping training")
            return {
                "status": "skipped",
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "models_trained": 0,
                "message": "No active strategies in DB",
            }

        results = []
        for sym, broker, tf in unique:
            res = await self.train_symbol(sym, broker, timeframe=tf)
            results.append(res)

        trained = sum(1 for r in results if r.get("status") == "trained")
        return {
            "status": "complete",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "models_trained": trained,
            "symbols": results,
        }

    async def _fetch_live_labels(self, symbol: str, df: "pd.DataFrame") -> "Optional[pd.DataFrame]":
        """
        Query resolved TradeOutcome rows for `symbol` and extract feature rows
        from the already-fetched OHLCV `df`.  Returns a DataFrame with the same
        FEATURE_COLS + 'label' column, or None if no live labels exist.

        Each live outcome row is repeated LIVE_LABEL_WEIGHT times so the model
        up-weights ground-truth signal outcomes over heuristic yfinance labels.
        """
        try:
            from db.database import AsyncSessionLocal
            from db.models import TradeOutcome
            from sqlalchemy import select

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(TradeOutcome).where(
                        TradeOutcome.symbol == symbol,
                        TradeOutcome.resolved == True,  # noqa: E712
                        TradeOutcome.ml_label != None,  # noqa: E711
                        # Include paper outcomes — fills now use a real live-price
                        # anchor so they are realistic. Excluding paper means zero
                        # live labels are available during paper-only operation, so
                        # the model falls back to yfinance heuristics and never
                        # learns from actual SL/TP outcomes.
                    )
                )
                outcomes = result.scalars().all()

            if not outcomes:
                return None

            feat_df = _compute_features(df)
            feat_df = feat_df.dropna()

            live_rows = []

            for o in outcomes:
                if o.created_at is None:
                    continue
                # Find the closest date in df index on or before the signal date
                signal_date = o.created_at.date()
                import pandas as pd
                mask = feat_df.index.date <= signal_date
                if not mask.any():
                    continue
                row = feat_df[mask].iloc[-1]
                # Only require the columns that actually exist in feat_df
                # (some features like vwap_ratio may be NaN for forex — that is OK,
                #  XGBoost handles NaN natively).
                available = [c for c in FEATURE_COLS if c in row.index]
                if not available:
                    continue
                row_dict = {c: row[c] for c in available}
                row_dict["label"] = int(o.ml_label)
                # Upweight live labels by repeating rows
                for _ in range(self.LIVE_LABEL_WEIGHT):
                    live_rows.append(row_dict)

            if not live_rows:
                return None

            import pandas as pd
            live_df = pd.DataFrame(live_rows)
            logger.info(f"[trainer] {symbol}: loaded {len(outcomes)} live outcome labels "
                        f"({len(live_rows)} rows after weighting)")
            return live_df

        except Exception as exc:
            logger.warning(f"[trainer] Could not load live labels for {symbol}: {exc}")
            return None

    async def train_symbol(self, symbol: str, broker: str, timeframe: str = "1d") -> dict:
        """Train or update model for a single symbol+timeframe.

        Returns a result dict with keys: symbol, timeframe, status, auc, model_path.
        """
        if not _ML_AVAILABLE:
            return {"symbol": symbol, "timeframe": timeframe, "status": "skipped",
                    "reason": "ML packages not installed"}

        logger.info(f"[trainer] Training model for {symbol} @ {timeframe} …")

        # G8: resolve fetch profile for this timeframe
        yf_interval, history_days, label_horizon, tf_min_rows = _TF_PROFILE.get(
            timeframe, _DEFAULT_TF_PROFILE
        )
        resample_to = {"2h": "2h", "4h": "4h"}.get(timeframe)  # 4h/2h: fetch 1h then resample

        # ── 1. Fetch OHLCV ───────────────────────────────────────────────────
        df = _fetch_ohlcv(symbol, days=history_days, interval=yf_interval, resample_to=resample_to)
        min_candles = max(label_horizon + 10, 30)
        if df is None or len(df) < min_candles:
            return {"symbol": symbol, "timeframe": timeframe, "status": "error",
                    "reason": "insufficient OHLCV data", "auc": None}

        # ── 2. Feature engineering ───────────────────────────────────────────
        feat_df = _compute_features(df)
        # G5: add regime_code column via 50-candle rolling regime classifier
        feat_df = _add_regime_codes(feat_df, df)
        labels       = _label(feat_df, horizon=label_horizon)
        labels_short = _label_short(feat_df, horizon=label_horizon)  # G6: compute before dropna

        # Combine and drop NaN rows (drops last `horizon` rows for both label directions)
        feat_df["label"]       = labels
        feat_df["label_short"] = labels_short
        feat_df = feat_df.dropna()

        # Extract SHORT training arrays BEFORE live_df merge strips _close/_atr14 columns.
        # active_features is the same before and after the merge (live_df adds rows, not cols).
        active_features = [c for c in FEATURE_COLS if c in feat_df.columns]
        X_short_arr: np.ndarray = feat_df[active_features].values
        y_short_arr: np.ndarray = feat_df["label_short"].values.astype(int)

        # ── Merge live outcome labels from DB (ML feedback loop) ────────────
        live_df = await self._fetch_live_labels(symbol, df)
        if live_df is not None and not live_df.empty:
            import pandas as pd
            FEATURE_COLS_LABEL = FEATURE_COLS + ["label"]
            base_df = feat_df[[c for c in FEATURE_COLS_LABEL if c in feat_df.columns]].copy()
            feat_df = pd.concat([base_df, live_df], ignore_index=True)
            logger.info(f"[trainer] {symbol}: combined {len(base_df)} historical + {len(live_df)} live rows")

        effective_min_rows = max(self.MIN_ROWS, tf_min_rows)
        if len(feat_df) < effective_min_rows:
            return {
                "symbol": symbol, "timeframe": timeframe, "status": "error",
                "reason": f"only {len(feat_df)} labelled rows (need {effective_min_rows})",
                "auc": None,
            }

        # active_features computed above; recompute to pick up any changes post-merge.
        active_features = [c for c in FEATURE_COLS if c in feat_df.columns]

        def _train_one(X_all: np.ndarray, y_arr: np.ndarray, label_name: str) -> tuple:
            """Fit XGBoost + calibration for one direction. Returns (model, holdout_auc, cv_auc)."""
            # Time-ordered holdout — no shuffle
            X_tr, X_te, y_tr, y_te = train_test_split(X_all, y_arr, test_size=0.2, shuffle=False)
            _n_pos = int(sum(y_tr == 1))
            _n_neg = int(sum(y_tr == 0))
            scale_pos = max(1, int(_n_neg / max(_n_pos, 1)))
            base = xgb.XGBClassifier(
                n_estimators=100, max_depth=4, learning_rate=0.05,
                subsample=0.8, colsample_bytree=0.8, scale_pos_weight=scale_pos,
                use_label_encoder=False, eval_metric="logloss", verbosity=0, random_state=42,
            )
            cv_aucs: list[float] = []
            if _n_pos < 2 or _n_neg < 2:
                logger.warning(
                    f"[trainer] {symbol} ({label_name}): single-class data "
                    f"(pos={_n_pos}, neg={_n_neg}) — skipping CV"
                )
            else:
                cv = StratifiedKFold(n_splits=max(2, min(5, _n_pos, _n_neg)), shuffle=False)
                for tr_idx, vl_idx in cv.split(X_tr, y_tr):
                    base.fit(X_tr[tr_idx], y_tr[tr_idx])
                    proba = base.predict_proba(X_tr[vl_idx])[:, 1]
                    try:
                        cv_aucs.append(roc_auc_score(y_tr[vl_idx], proba))
                    except ValueError:
                        pass
            # Final fit on full training split
            base.fit(X_tr, y_tr)
            # G7: Platt / isotonic calibration so P=0.7 ≈ 70% empirical win-rate
            try:
                cal_model = CalibratedClassifierCV(base, method="isotonic", cv="prefit")
                cal_model.fit(X_te, y_te)   # calibrate on the holdout set
            except Exception as _cal_err:
                logger.warning(f"[trainer] {symbol} calibration failed ({_cal_err}) — using uncalibrated")
                cal_model = base  # type: ignore[assignment]
            # Holdout AUC on calibrated model
            try:
                te_proba = cal_model.predict_proba(X_te)[:, 1]
                h_auc = roc_auc_score(y_te, te_proba)
            except ValueError:
                h_auc = 0.0
            return cal_model, h_auc, float(np.mean(cv_aucs)) if cv_aucs else 0.0

        # ── 3–5. Train BUY model ─────────────────────────────────────────────
        y_buy = feat_df["label"].values.astype(int)
        buy_model, holdout_auc, mean_cv_auc = _train_one(feat_df[active_features].values, y_buy, "BUY")
        logger.info(f"[trainer] {symbol}: BUY CV AUC={mean_cv_auc:.3f}, holdout AUC={holdout_auc:.3f}")

        if holdout_auc < self.MIN_AUC:
            logger.warning(
                f"[trainer] {symbol}: BUY holdout AUC {holdout_auc:.3f} < {self.MIN_AUC} — model NOT saved"
            )
            # I7: dispatch in-app notification so operators know the model was rejected
            try:
                import asyncio as _asyncio
                from db.database import AsyncSessionLocal
                from notifications.notifier import dispatch as _dispatch

                async def _notify_auc_fail():
                    async with AsyncSessionLocal() as _ses:
                        await _dispatch(
                            _ses,
                            title=f"⚠️ ML Model Rejected — {symbol}",
                            message=(
                                f"Retrain for {symbol}: holdout AUC {holdout_auc:.3f} "
                                f"< threshold {self.MIN_AUC}.  Previous model kept."
                            ),
                            level="warning",
                            category="ml",
                            metadata={"symbol": symbol, "auc": holdout_auc, "threshold": self.MIN_AUC},
                        )
                        await _ses.commit()

                _loop = None
                try:
                    _loop = _asyncio.get_running_loop()
                except RuntimeError:
                    pass
                if _loop and _loop.is_running():
                    _loop.create_task(_notify_auc_fail())
                else:
                    _asyncio.run(_notify_auc_fail())
            except Exception as _n_err:
                logger.debug(f"[trainer] AUC-fail notification error: {_n_err}")

            return {
                "symbol": symbol, "status": "rejected",
                "auc": round(holdout_auc, 4),
                "cv_auc": round(mean_cv_auc, 4),
                "reason": f"AUC {holdout_auc:.3f} below threshold {self.MIN_AUC}",
            }

        # ── G6: Train dedicated SHORT model ──────────────────────────────────
        # X_short_arr / y_short_arr were extracted before the live_df merge so they
        # still have _close/_atr14; their length matches because both come from feat_df
        # after the single dropna() that removed NaN features AND NaN label rows.
        short_model, short_auc, _ = (None, 0.0, 0.0)
        if len(y_short_arr) >= effective_min_rows:
            short_model, short_auc, _ = _train_one(X_short_arr, y_short_arr, "SHORT")
            logger.info(f"[trainer] {symbol}: SHORT holdout AUC={short_auc:.3f}")
        else:
            logger.warning(
                f"[trainer] {symbol}: insufficient rows for SHORT model "
                f"({len(y_short_arr)} < {effective_min_rows}) — skipping"
            )

        # ── 6. Persist models ─────────────────────────────────────────────────
        today = datetime.date.today().isoformat()
        safe_sym = symbol.replace("/", "_").replace("-", "_")
        safe_tf  = timeframe.replace("/", "_")

        # BUY model (backward-compat key "symbol:timeframe" keeps old lookup working)
        buy_path = self.MODEL_DIR / f"{safe_sym}_{safe_tf}_{today}_buy.pkl"
        joblib.dump({"model": buy_model, "features": active_features, "symbol": symbol,
                     "timeframe": timeframe, "trained_at": today, "direction": "buy"},
                    buy_path)
        logger.info(f"[trainer] BUY model saved → {buy_path}")

        # SHORT model (only saved when it passes the min-rows check)
        short_path = None
        if short_model is not None:
            short_path = self.MODEL_DIR / f"{safe_sym}_{safe_tf}_{today}_short.pkl"
            joblib.dump({"model": short_model, "features": active_features, "symbol": symbol,
                         "timeframe": timeframe, "trained_at": today, "direction": "short"},
                        short_path)
            logger.info(f"[trainer] SHORT model saved → {short_path}")

        # G9: delete old .pkl files for this symbol+timeframe to prevent unbounded accumulation
        for _old in self.MODEL_DIR.glob(f"{safe_sym}_{safe_tf}_*.pkl"):
            if _old not in (buy_path, short_path):
                try:
                    _old.unlink(missing_ok=True)
                    logger.debug(f"[trainer] Removed old model: {_old.name}")
                except Exception as _del_err:
                    logger.debug(f"[trainer] Could not remove {_old.name}: {_del_err}")

        # Update latest.json registry
        latest_path = self.MODEL_DIR / "latest.json"
        try:
            latest = json.loads(latest_path.read_text()) if latest_path.exists() else {}
        except json.JSONDecodeError:
            latest = {}
        registry_key = f"{symbol}:{timeframe}"
        latest[registry_key] = str(buy_path)                         # BUY (default)
        latest[f"{registry_key}:short"] = str(short_path) if short_path else None
        latest_path.write_text(json.dumps(latest, indent=2))

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "status": "trained",
            "auc": round(holdout_auc, 4),
            "cv_auc": round(mean_cv_auc, 4),
            "short_auc": round(short_auc, 4),
            "model_path": str(buy_path),
            "rows_used": len(feat_df),
        }
