"""
Scalping ML Retrain Task
========================
Daily Celery task that retrains XGBoost models for every active scalping
strategy using recent Binance OHLCV data and resolved TradeOutcome labels.

Key differences from the swing ``tasks/ml_retrain.py``:

  Data source : Binance live REST API via ccxt (not yfinance) — full 5m history
  Features    : 13 scalp features from ``core.scalping_ml_scorer._compute_scalp_features()``
  Label       : 1 if price moves > 1.0 × ATR(14) in the **next 3 candles**
                (faster resolution than swing's 24-candle window)
  Registry key: ``"symbol:timeframe:scalp"`` (BUY) / ``"symbol:timeframe:scalp:short"``
  AUC gate    : 0.55 — same as swing
  Schedule    : daily at 04:30 UTC (after outcome_resolver at 01:30 and swing retrain at 02:00)
"""

import asyncio
import datetime
import json
import logging
import pathlib

import numpy as np
import pandas as pd

from celery_app import celery_app

logger = logging.getLogger(__name__)

MIN_AUC: float = 0.55
MIN_ROWS: int = 200          # need enough 5m candles for generalization
LABEL_HORIZON: int = 3       # how many future candles to look at for labeling
ATR_MULT: float = 1.0        # price must move > 1.0 × ATR to count as a win
LIVE_LABEL_WEIGHT: int = 3   # repeat live outcome rows to upweight ground truth

_BASE_DIR  = pathlib.Path(__file__).resolve().parent.parent
_MODEL_DIR = _BASE_DIR / "data" / "models"
_MODEL_DIR.mkdir(parents=True, exist_ok=True)
_LATEST_JSON = _MODEL_DIR / "latest.json"

# ── optional heavy imports ────────────────────────────────────────────────────
try:
    import xgboost as xgb
    from sklearn.model_selection import StratifiedKFold, train_test_split
    from sklearn.metrics import roc_auc_score
    from sklearn.calibration import CalibratedClassifierCV
    import joblib
    _ML_AVAILABLE = True
except ImportError:
    _ML_AVAILABLE = False
    logger.warning("[scalping_ml_retrain] xgboost/scikit-learn/joblib not installed — task disabled")


# ── Binance OHLCV fetcher (multi-page, public endpoint) ─────────────────────

def _fetch_binance_ohlcv(symbol: str, timeframe: str, days: int = 60) -> pd.DataFrame | None:
    """
    Fetch historical OHLCV from Binance public REST API via ccxt.

    Uses the unauthenticated endpoint — no API key required for historical data.
    Paginates automatically to retrieve the full requested history.

    Args:
        symbol   : e.g. "BTC/USDT"
        timeframe: e.g. "5m"
        days     : calendar days of history to fetch (60 → ~17 280 rows at 5m)

    Returns:
        DataFrame with columns [open, high, low, close, volume] and DatetimeIndex,
        or None on failure.
    """
    try:
        import ccxt
    except ImportError:
        logger.warning("[scalping_ml_retrain] ccxt not installed — cannot fetch Binance OHLCV")
        return None

    try:
        exchange = ccxt.binance({"enableRateLimit": True, "timeout": 20000})  # type: ignore[call-arg]

        tf_ms = {
            "1m": 60_000, "3m": 180_000, "5m": 300_000,
            "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
        }
        interval_ms = tf_ms.get(timeframe, 300_000)

        since_ms = int(
            (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=days)).timestamp() * 1000
        )

        all_rows: list = []
        limit = 1000   # Binance max per call
        fetch_since = since_ms

        while True:
            batch = exchange.fetch_ohlcv(symbol, timeframe, since=fetch_since, limit=limit)
            if not batch:
                break
            all_rows.extend(batch)
            if len(batch) < limit:
                break                         # last page
            fetch_since = batch[-1][0] + interval_ms  # next page starts after last candle

        if not all_rows:
            logger.warning(f"[scalping_ml_retrain] No OHLCV returned for {symbol}/{timeframe}")
            return None

        df = pd.DataFrame(
            all_rows, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("timestamp").sort_index()
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        df = df[~df.index.duplicated(keep="last")]
        logger.info(
            f"[scalping_ml_retrain] Fetched {len(df)} candles for {symbol} {timeframe} "
            f"({days}d, {len(df) * interval_ms // 86_400_000:.0f}d actual)"
        )
        return df

    except Exception as exc:
        logger.warning(f"[scalping_ml_retrain] Binance fetch failed for {symbol}/{timeframe}: {exc}")
        return None


# ── Labeling ─────────────────────────────────────────────────────────────────

def _label_scalp(df: pd.DataFrame, horizon: int = LABEL_HORIZON, atr_mult: float = ATR_MULT) -> pd.Series:
    """
    BUY label: 1 if the high of the next ``horizon`` candles exceeds
    entry + atr_mult × ATR(14).  0 otherwise.
    Last ``horizon`` rows are set to NaN (no future data).
    """
    close = df["close"]
    high  = df["high"]
    low   = df["low"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr14 = tr.ewm(span=14, adjust=False).mean()

    labels = pd.Series(0, index=df.index, dtype=float)
    n = len(df)
    for i in range(n - horizon):
        threshold = atr_mult * float(atr14.iloc[i])
        future_high = float(df["high"].iloc[i + 1: i + 1 + horizon].max())
        if future_high - float(close.iloc[i]) > threshold:
            labels.iloc[i] = 1.0
    labels.iloc[-horizon:] = np.nan
    return labels


def _label_scalp_short(df: pd.DataFrame, horizon: int = LABEL_HORIZON, atr_mult: float = ATR_MULT) -> pd.Series:
    """
    SHORT label: 1 if the low of the next ``horizon`` candles drops below
    entry − atr_mult × ATR(14).  0 otherwise.
    """
    close = df["close"]
    high  = df["high"]
    low   = df["low"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr14 = tr.ewm(span=14, adjust=False).mean()

    labels = pd.Series(0, index=df.index, dtype=float)
    n = len(df)
    for i in range(n - horizon):
        threshold = atr_mult * float(atr14.iloc[i])
        future_low = float(df["low"].iloc[i + 1: i + 1 + horizon].min())
        if float(close.iloc[i]) - future_low > threshold:
            labels.iloc[i] = 1.0
    labels.iloc[-horizon:] = np.nan
    return labels


# ── Live outcome label enrichment ────────────────────────────────────────────

async def _fetch_live_scalp_labels(
    symbol: str, timeframe: str, feat_df: pd.DataFrame, direction: str = "BUY"
) -> pd.DataFrame | None:
    """
    Query resolved TradeOutcome rows for scalp strategies on this symbol/timeframe
    and produce extra training rows (ground-truth labels weighted 3×).

    ``direction`` must be "BUY" or "SHORT" — only outcomes matching that signal
    type are included so the BUY and SHORT models receive correctly-labelled data.
    """
    try:
        from db.database import AsyncSessionLocal
        from db.models import TradeOutcome
        from sqlalchemy import select
        from core.scalping_ml_scorer import SCALP_FEATURE_COLS, _compute_scalp_features

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(TradeOutcome).where(
                    TradeOutcome.symbol        == symbol,
                    TradeOutcome.timeframe     == timeframe,
                    TradeOutcome.strategy_name.like("%scalp%"),
                    TradeOutcome.signal_type   == direction,
                    TradeOutcome.resolved      == True,   # noqa: E712
                    TradeOutcome.ml_label      != None,   # noqa: E711
                )
            )
            outcomes = result.scalars().all()

        if not outcomes:
            return None

        scalp_feat = _compute_scalp_features(feat_df)
        if scalp_feat is None or scalp_feat.empty:
            return None

        live_rows: list[dict] = []
        for o in outcomes:
            if o.created_at is None:
                continue
            signal_dt = pd.Timestamp(o.created_at, tz="UTC")
            mask = scalp_feat.index <= signal_dt
            if not mask.any():
                continue
            row = scalp_feat[mask].iloc[-1]
            row_dict = {c: float(row[c]) for c in SCALP_FEATURE_COLS if c in row.index}
            row_dict["label"] = int(o.ml_label) if o.ml_label is not None else 0
            for _ in range(LIVE_LABEL_WEIGHT):
                live_rows.append(row_dict)

        if not live_rows:
            return None

        logger.info(
            f"[scalping_ml_retrain] {symbol}/{timeframe}: "
            f"{len(outcomes)} live labels → {len(live_rows)} weighted rows"
        )
        return pd.DataFrame(live_rows)

    except Exception as exc:
        logger.warning(f"[scalping_ml_retrain] Live label fetch failed for {symbol}/{timeframe}: {exc}")
        return None


# ── Training ─────────────────────────────────────────────────────────────────

def _train_direction(
    X: np.ndarray, y: np.ndarray, direction: str, symbol: str
) -> tuple:
    """
    Fit XGBoost + isotonic calibration for one direction.
    Returns (model, holdout_auc, mean_cv_auc).
    """
    n_pos = int((y == 1).sum())
    n_neg = int((y == 0).sum())
    if n_pos < 5 or n_neg < 5:
        logger.warning(
            f"[scalping_ml_retrain] {symbol} {direction}: "
            f"imbalanced labels (pos={n_pos} neg={n_neg}) — skipping"
        )
        return None, 0.0, 0.0

    X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.2, shuffle=False)
    scale_pos = max(1, int((y_tr == 0).sum() / max((y_tr == 1).sum(), 1)))

    base = xgb.XGBClassifier(
        n_estimators=100, max_depth=4, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8, scale_pos_weight=scale_pos,
        use_label_encoder=False, eval_metric="logloss", verbosity=0, random_state=42,
    )

    cv_aucs: list[float] = []
    n_splits = max(2, min(5, n_pos, n_neg))
    cv = StratifiedKFold(n_splits=n_splits, shuffle=False)
    for tr_idx, vl_idx in cv.split(X_tr, y_tr):
        base.fit(X_tr[tr_idx], y_tr[tr_idx])
        try:
            cv_aucs.append(float(roc_auc_score(y_tr[vl_idx], base.predict_proba(X_tr[vl_idx])[:, 1])))
        except ValueError:
            pass

    base.fit(X_tr, y_tr)
    try:
        cal = CalibratedClassifierCV(base, method="isotonic", cv="prefit")
        cal.fit(X_te, y_te)
    except Exception as _ce:
        logger.warning(f"[scalping_ml_retrain] {symbol} {direction} calibration failed: {_ce}")
        cal = base  # type: ignore[assignment]

    try:
        h_auc = roc_auc_score(y_te, cal.predict_proba(X_te)[:, 1])
    except ValueError:
        h_auc = 0.0

    return cal, h_auc, float(np.mean(cv_aucs)) if cv_aucs else 0.0


# ── Per-symbol trainer ────────────────────────────────────────────────────────

async def _train_scalp_symbol(symbol: str, timeframe: str) -> dict:
    """Train BUY + SHORT scalping models for one symbol/timeframe."""
    if not _ML_AVAILABLE:
        return {"symbol": symbol, "timeframe": timeframe, "status": "skipped",
                "reason": "ML packages not installed"}

    logger.info(f"[scalping_ml_retrain] Training {symbol} / {timeframe} …")

    # ── 1. Fetch data ─────────────────────────────────────────────────────────
    df = _fetch_binance_ohlcv(symbol, timeframe, days=60)
    if df is None or len(df) < MIN_ROWS + LABEL_HORIZON + 30:
        return {"symbol": symbol, "timeframe": timeframe, "status": "error",
                "reason": f"insufficient data ({0 if df is None else len(df)} rows)"}

    # ── 2. Compute features ───────────────────────────────────────────────────
    from core.scalping_ml_scorer import SCALP_FEATURE_COLS, _compute_scalp_features
    feat_df = _compute_scalp_features(df)
    if feat_df is None or feat_df.empty:
        return {"symbol": symbol, "timeframe": timeframe, "status": "error",
                "reason": "feature computation returned empty"}

    # ── 3. Label rows ─────────────────────────────────────────────────────────
    feat_df = feat_df.copy()
    feat_df["label_buy"]   = _label_scalp(df.loc[feat_df.index], LABEL_HORIZON)
    feat_df["label_short"] = _label_scalp_short(df.loc[feat_df.index], LABEL_HORIZON)

    # Save short arrays before dropna strips them (different NaN profile possible)
    feat_df_short = feat_df.dropna(subset=["label_short"] + SCALP_FEATURE_COLS)
    feat_df = feat_df.dropna(subset=["label_buy"] + SCALP_FEATURE_COLS)

    # ── 4. Merge live outcome labels ──────────────────────────────────────────
    live_df = await _fetch_live_scalp_labels(symbol, timeframe, df)
    if live_df is not None and not live_df.empty:
        base_buy = feat_df[SCALP_FEATURE_COLS + ["label_buy"]].rename(columns={"label_buy": "label"})
        live_df_buy = live_df.rename(columns={"label": "label"})
        combined_buy = pd.concat(
            [base_buy.rename(columns={"label_buy": "label"}) if "label_buy" in base_buy.columns else base_buy,
             live_df_buy],
            ignore_index=True,
        )
        feat_df = combined_buy
        logger.info(
            f"[scalping_ml_retrain] {symbol}/{timeframe}: {len(combined_buy)} total rows after merge"
        )

    effective_label_col = "label" if "label" in feat_df.columns else "label_buy"
    if len(feat_df) < MIN_ROWS:
        return {"symbol": symbol, "timeframe": timeframe, "status": "error",
                "reason": f"only {len(feat_df)} labelled rows (need {MIN_ROWS})"}

    active_cols = [c for c in SCALP_FEATURE_COLS if c in feat_df.columns]

    # ── 5. Train BUY model ────────────────────────────────────────────────────
    X_buy = feat_df[active_cols].values.astype(float)
    y_buy = np.asarray(feat_df[effective_label_col], dtype=int)
    buy_model, buy_auc, buy_cv_auc = _train_direction(X_buy, y_buy, "BUY", symbol)

    logger.info(
        f"[scalping_ml_retrain] {symbol}/{timeframe} BUY  CV={buy_cv_auc:.3f}  holdout={buy_auc:.3f}"
    )

    if buy_model is None or buy_auc < MIN_AUC:
        logger.warning(
            f"[scalping_ml_retrain] {symbol}/{timeframe} BUY rejected "
            f"(AUC={buy_auc:.3f} < {MIN_AUC}) — model NOT saved"
        )
        # Dispatch in-app notification
        try:
            from db.database import AsyncSessionLocal
            from notifications.notifier import dispatch as _dispatch
            async with AsyncSessionLocal() as _db:
                await _dispatch(
                    _db,
                    title=f"⚠️ Scalp ML Rejected — {symbol}/{timeframe}",
                    message=(
                        f"Scalp retrain BUY model for {symbol}/{timeframe}: "
                        f"holdout AUC {buy_auc:.3f} < {MIN_AUC}. Previous model kept."
                    ),
                    level="warning", category="ml",
                    metadata={"symbol": symbol, "timeframe": timeframe, "auc": buy_auc},
                )
                await _db.commit()
        except Exception as _ne:
            logger.debug(f"[scalping_ml_retrain] AUC-fail notification error: {_ne}")
        return {"symbol": symbol, "timeframe": timeframe, "status": "rejected",
                "auc": round(buy_auc, 4), "reason": f"AUC {buy_auc:.3f} < {MIN_AUC}"}

    # ── 6. Train SHORT model ──────────────────────────────────────────────────
    short_model, short_auc = None, 0.0
    if len(feat_df_short) >= MIN_ROWS:
        # Merge live outcome labels for SHORT direction (same enrichment as BUY)
        live_df_short = await _fetch_live_scalp_labels(symbol, timeframe, df, direction="SHORT")
        if live_df_short is not None and not live_df_short.empty:
            _base_short = feat_df_short[active_cols + ["label_short"]].rename(
                columns={"label_short": "label"}
            )
            feat_df_short = pd.concat([_base_short, live_df_short], ignore_index=True)
            logger.info(
                f"[scalping_ml_retrain] {symbol}/{timeframe}: "
                f"SHORT {len(feat_df_short)} total rows after live merge"
            )
        _short_label_col = "label" if "label" in feat_df_short.columns else "label_short"
        X_short = feat_df_short[active_cols].values.astype(float)
        y_short = np.asarray(feat_df_short[_short_label_col], dtype=int)
        short_model, short_auc, _ = _train_direction(X_short, y_short, "SHORT", symbol)
        logger.info(
            f"[scalping_ml_retrain] {symbol}/{timeframe} SHORT holdout={short_auc:.3f}"
        )
    else:
        logger.warning(
            f"[scalping_ml_retrain] {symbol}/{timeframe} SHORT: "
            f"only {len(feat_df_short)} rows — skipping"
        )

    # ── 7. Persist models ─────────────────────────────────────────────────────
    today    = datetime.date.today().isoformat()
    safe_sym = symbol.replace("/", "_").replace("-", "_")
    safe_tf  = timeframe.replace("/", "_")

    buy_path = _MODEL_DIR / f"{safe_sym}_{safe_tf}_{today}_scalp_buy.pkl"
    joblib.dump(
        {"model": buy_model, "features": active_cols, "symbol": symbol,
         "timeframe": timeframe, "trained_at": today, "direction": "scalp_buy"},
        buy_path,
    )
    logger.info(f"[scalping_ml_retrain] BUY model saved → {buy_path}")

    short_path = None
    if short_model is not None and short_auc >= MIN_AUC:
        short_path = _MODEL_DIR / f"{safe_sym}_{safe_tf}_{today}_scalp_short.pkl"
        joblib.dump(
            {"model": short_model, "features": active_cols, "symbol": symbol,
             "timeframe": timeframe, "trained_at": today, "direction": "scalp_short"},
            short_path,
        )
        logger.info(f"[scalping_ml_retrain] SHORT model saved → {short_path}")

    # Delete old scalp model files for this symbol+timeframe
    for _old in _MODEL_DIR.glob(f"{safe_sym}_{safe_tf}_*_scalp_*.pkl"):
        if _old not in filter(None, [buy_path, short_path]):
            try:
                _old.unlink(missing_ok=True)
                logger.debug(f"[scalping_ml_retrain] Removed old scalp model: {_old.name}")
            except Exception:
                pass

    # Update latest.json with :scalp suffix keys (no collision with swing keys)
    try:
        registry: dict = json.loads(_LATEST_JSON.read_text()) if _LATEST_JSON.exists() else {}
    except json.JSONDecodeError:
        registry = {}
    registry[f"{symbol}:{timeframe}:scalp"]       = str(buy_path)
    registry[f"{symbol}:{timeframe}:scalp:short"] = str(short_path) if short_path else None
    _LATEST_JSON.write_text(json.dumps(registry, indent=2))

    return {
        "symbol": symbol, "timeframe": timeframe, "status": "trained",
        "buy_auc": round(buy_auc, 4), "buy_cv_auc": round(buy_cv_auc, 4),
        "short_auc": round(short_auc, 4) if short_model else None,
        "model_path": str(buy_path),
        "rows_used": len(feat_df),
    }


# ── Celery task ───────────────────────────────────────────────────────────────

@celery_app.task(name="tasks.scalping_ml_retrain.retrain_scalp_models", bind=True, max_retries=1)
def retrain_scalp_models(self):
    """
    Pull the latest resolved scalp TradeOutcomes + 60 days of Binance OHLCV,
    retrain one XGBoost BUY + SHORT model per active scalp symbol/timeframe,
    gate on AUC >= 0.55, and reload the ScalpingMLScorer cache.

    Scheduled daily at 04:30 UTC (after swing retrain at 02:00).
    """
    try:
        async def _retrain():
            from db.database import AsyncSessionLocal
            from db.models import Strategy as StrategyModel
            from sqlalchemy import select

            # Find all scalp strategies
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(StrategyModel).where(StrategyModel.is_active == True)  # noqa: E712
                )
                all_strats = result.scalars().all()

            scalp_strats = [
                s for s in all_strats
                if (s.parameters or {}).get("strategy_type", "").startswith("scalp_")
            ]

            if not scalp_strats:
                logger.info("[scalping_ml_retrain] No active scalp strategies — skipping")
                return {"status": "skipped", "reason": "no_scalp_strategies"}

            # Unique (symbol, timeframe) pairs
            seen: set[tuple] = set()
            targets: list[tuple[str, str]] = []
            for s in scalp_strats:
                sym = (s.parameters or {}).get("symbol")
                tf  = (s.parameters or {}).get("timeframe", "5m")
                if sym and (sym, tf) not in seen:
                    seen.add((sym, tf))
                    targets.append((sym, tf))

            logger.info(f"[scalping_ml_retrain] Retraining {len(targets)} symbol/timeframe pairs: {targets}")
            results = []
            for sym, tf in targets:
                res = await _train_scalp_symbol(sym, tf)
                results.append(res)

            trained = sum(1 for r in results if r.get("status") == "trained")

            if trained > 0:
                # 1. Reload cache in this Celery worker process
                from core.scalping_ml_scorer import scalping_ml_scorer
                scalping_ml_scorer.reload()
                logger.info("[scalping_ml_retrain] ScalpingMLScorer cache cleared in Celery worker")

                # 2. Tell the FastAPI server process to reload its cache
                try:
                    import httpx
                    from config import settings as _cfg
                    loop = asyncio.get_running_loop()
                    resp = await loop.run_in_executor(
                        None,
                        lambda: httpx.post(
                            f"{_cfg.api_internal_url}/internal/scalping/ml/reload",
                            headers={"X-Internal-Secret": _cfg.internal_api_secret},
                            timeout=5.0,
                        ),
                    )
                    logger.info(
                        f"[scalping_ml_retrain] FastAPI ScalpingMLScorer flush: HTTP {resp.status_code}"
                    )
                except Exception as _flush_err:
                    logger.warning(
                        f"[scalping_ml_retrain] FastAPI cache flush skipped: {_flush_err}"
                    )

            report = {
                "status": "complete",
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "models_trained": trained,
                "symbols": results,
            }
            logger.info(f"[scalping_ml_retrain] Done: {report}")
            return report

        return asyncio.run(_retrain())

    except Exception as exc:
        logger.error(f"[scalping_ml_retrain] Task failed: {exc}", exc_info=True)
        raise self.retry(exc=exc, countdown=600)
