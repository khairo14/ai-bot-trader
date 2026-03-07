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
    import joblib
    _ML_AVAILABLE = True
except ImportError:
    _ML_AVAILABLE = False
    logger.warning("xgboost / scikit-learn / joblib not installed — ModelTrainer disabled")

# ── paths ────────────────────────────────────────────────────────────────────
_BASE_DIR = pathlib.Path(__file__).resolve().parent.parent  # backend/
MODEL_DIR = _BASE_DIR / "data" / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)


def _symbol_to_yf(symbol: str) -> str:
    """Convert exchange symbol format to yfinance ticker.

    BTC/USDT  →  BTC-USD
    ETH/BTC   →  ETH-BTC
    AAPL      →  AAPL        (stocks pass through)
    """
    # Base token aliases: exchange ticker → yfinance ticker
    _BASE_ALIASES: dict[str, str] = {
        "POL": "MATIC",   # Polygon rebranded POL → still listed as MATIC on yfinance
    }
    if "/" in symbol:
        base, quote = symbol.split("/", 1)
        base = _BASE_ALIASES.get(base.upper(), base)
        # Normalise stablecoins USDT/USDC → USD for yfinance
        quote_yf = "USD" if quote in ("USDT", "USDC", "BUSD") else quote
        return f"{base}-{quote_yf}"
    return symbol


def _fetch_ohlcv(symbol: str, days: int = 90) -> Optional[pd.DataFrame]:
    """Download daily OHLCV from yfinance. Returns None on failure."""
    if not _YF_AVAILABLE:
        return None
    ticker = _symbol_to_yf(symbol)
    try:
        end = datetime.date.today()
        start = end - datetime.timedelta(days=days)
        df = yf.download(ticker, start=str(start), end=str(end),
                         interval="1d", progress=False, auto_adjust=True)
        if df.empty:
            logger.warning(f"[trainer] yfinance returned empty data for {ticker}")
            return None
        df = df.rename(columns=str.lower)
        df.index = pd.to_datetime(df.index)
        # Flatten MultiIndex columns that yfinance sometimes produces
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        return df[["open", "high", "low", "close", "volume"]].dropna()
    except Exception as exc:
        logger.warning(f"[trainer] yfinance fetch failed for {ticker}: {exc}")
        return None


def _compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Engineer features from OHLCV DataFrame."""
    out = pd.DataFrame(index=df.index)

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    # ── RSI-14 ────────────────────────────────────────────────────────────────
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    out["rsi"] = 100 - (100 / (1 + rs))

    # ── MACD histogram ───────────────────────────────────────────────────────
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    out["macd_hist"] = macd_line - signal_line

    # ── ATR-14 (normalised by close) ─────────────────────────────────────────
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean()
    out["atr_norm"] = atr14 / close  # dimensionless

    # ── Volume ratio (vs 20-period mean) ────────────────────────────────────
    vol_ma = volume.rolling(20).mean()
    out["vol_ratio"] = volume / vol_ma.replace(0, np.nan)

    # ── Bollinger Band position (0=lower, 1=upper) ───────────────────────────
    bb_sma = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    bb_upper = bb_sma + 2 * bb_std
    bb_lower = bb_sma - 2 * bb_std
    band_range = (bb_upper - bb_lower).replace(0, np.nan)
    out["bb_pct"] = (close - bb_lower) / band_range

    # ── 1-day log return ─────────────────────────────────────────────────────
    out["log_ret"] = np.log(close / close.shift(1))

    out["_atr14"] = atr14   # kept for labelling, dropped before fit
    out["_close"] = close

    return out


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
                params = strat.parameters or {}
                sym = params.get("symbol")
                broker = str(strat.broker.value) if hasattr(strat.broker, "value") else str(strat.broker)
                if sym:
                    symbols.append((sym, broker))

        except Exception as exc:
            logger.error(f"[trainer] DB query failed: {exc}")

        # Deduplicate by symbol
        seen: set[str] = set()
        unique: list[tuple[str, str]] = []
        for sym, broker in symbols:
            if sym not in seen:
                seen.add(sym)
                unique.append((sym, broker))

        if not unique:
            logger.info("[trainer] No active strategy symbols found — skipping training")
            return {
                "status": "skipped",
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "models_trained": 0,
                "message": "No active strategies in DB",
            }

        results = []
        for sym, broker in unique:
            res = await self.train_symbol(sym, broker)
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
                    )
                )
                outcomes = result.scalars().all()

            if not outcomes:
                return None

            feat_df = _compute_features(df)
            feat_df = feat_df.dropna()

            FEATURE_COLS = ["rsi", "macd_hist", "atr_norm", "vol_ratio", "bb_pct", "log_ret"]
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
                if row[FEATURE_COLS].isna().any():
                    continue
                row_dict = {c: row[c] for c in FEATURE_COLS}
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

    async def train_symbol(self, symbol: str, broker: str) -> dict:
        """Train or update model for a single symbol.

        Returns a result dict with keys: symbol, status, auc, model_path.
        """
        if not _ML_AVAILABLE:
            return {"symbol": symbol, "status": "skipped", "reason": "ML packages not installed"}

        logger.info(f"[trainer] Training model for {symbol} …")

        # ── 1. Fetch OHLCV ───────────────────────────────────────────────────
        df = _fetch_ohlcv(symbol, days=365)
        if df is None or len(df) < 60:
            return {"symbol": symbol, "status": "error", "reason": "insufficient OHLCV data", "auc": None}

        # ── 2. Feature engineering ───────────────────────────────────────────
        feat_df = _compute_features(df)
        labels = _label(feat_df, horizon=min(24, max(5, len(feat_df) // 10)))

        # Combine and drop NaN rows
        feat_df["label"] = labels
        feat_df = feat_df.dropna()

        # ── Merge live outcome labels from DB (ML feedback loop) ────────────
        live_df = await self._fetch_live_labels(symbol, df)
        if live_df is not None and not live_df.empty:
            import pandas as pd
            FEATURE_COLS_LABEL = ["rsi", "macd_hist", "atr_norm", "vol_ratio", "bb_pct", "log_ret", "label"]
            base_df = feat_df[FEATURE_COLS_LABEL].copy()
            feat_df = pd.concat([base_df, live_df], ignore_index=True)
            logger.info(f"[trainer] {symbol}: combined {len(base_df)} historical + {len(live_df)} live rows")

        if len(feat_df) < self.MIN_ROWS:
            return {
                "symbol": symbol, "status": "error",
                "reason": f"only {len(feat_df)} labelled rows (need {self.MIN_ROWS})",
                "auc": None,
            }

        FEATURE_COLS = ["rsi", "macd_hist", "atr_norm", "vol_ratio", "bb_pct", "log_ret"]
        X = feat_df[FEATURE_COLS].values
        y = feat_df["label"].values.astype(int)

        # ── 3. Holdout split ─────────────────────────────────────────────────
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, shuffle=False  # time-ordered — no shuffle
        )

        # ── 4. Cross-validated training ──────────────────────────────────────
        cv = StratifiedKFold(n_splits=min(5, sum(y_train == 1), sum(y_train == 0)),
                             shuffle=False)
        scale_pos = max(1, int(sum(y_train == 0) / max(sum(y_train == 1), 1)))
        model = xgb.XGBClassifier(
            n_estimators=100,
            max_depth=4,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            scale_pos_weight=scale_pos,
            use_label_encoder=False,
            eval_metric="logloss",
            verbosity=0,
            random_state=42,
        )

        cv_aucs = []
        for train_idx, val_idx in cv.split(X_train, y_train):
            model.fit(X_train[train_idx], y_train[train_idx])
            proba = model.predict_proba(X_train[val_idx])[:, 1]
            try:
                cv_aucs.append(roc_auc_score(y_train[val_idx], proba))
            except ValueError:
                pass  # single-class fold — skip

        # Final fit on full training set
        model.fit(X_train, y_train)

        # ── 5. Holdout evaluation ────────────────────────────────────────────
        try:
            test_proba = model.predict_proba(X_test)[:, 1]
            holdout_auc = roc_auc_score(y_test, test_proba)
        except ValueError:
            holdout_auc = 0.0

        mean_cv_auc = float(np.mean(cv_aucs)) if cv_aucs else 0.0
        logger.info(f"[trainer] {symbol}: CV AUC={mean_cv_auc:.3f}, holdout AUC={holdout_auc:.3f}")

        if holdout_auc < self.MIN_AUC:
            logger.warning(
                f"[trainer] {symbol}: holdout AUC {holdout_auc:.3f} < {self.MIN_AUC} — model NOT saved"
            )
            return {
                "symbol": symbol, "status": "rejected",
                "auc": round(holdout_auc, 4),
                "cv_auc": round(mean_cv_auc, 4),
                "reason": f"AUC {holdout_auc:.3f} below threshold {self.MIN_AUC}",
            }

        # ── 6. Persist model ─────────────────────────────────────────────────
        today = datetime.date.today().isoformat()
        safe_sym = symbol.replace("/", "_").replace("-", "_")
        model_path = self.MODEL_DIR / f"{safe_sym}_{today}.pkl"
        joblib.dump({"model": model, "features": FEATURE_COLS, "symbol": symbol, "trained_at": today},
                    model_path)
        logger.info(f"[trainer] Model saved → {model_path}")

        # Update latest.json registry
        latest_path = self.MODEL_DIR / "latest.json"
        try:
            latest = json.loads(latest_path.read_text()) if latest_path.exists() else {}
        except json.JSONDecodeError:
            latest = {}
        latest[symbol] = str(model_path)
        latest_path.write_text(json.dumps(latest, indent=2))

        return {
            "symbol": symbol,
            "status": "trained",
            "auc": round(holdout_auc, 4),
            "cv_auc": round(mean_cv_auc, 4),
            "model_path": str(model_path),
            "rows_used": len(feat_df),
        }
