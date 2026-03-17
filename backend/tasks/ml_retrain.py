"""Tasks: weekly ML model retraining (with live outcome labels)."""
from celery_app import celery_app
import logging

logger = logging.getLogger(__name__)

# Minimum number of newly-resolved live outcomes before triggering an
# opportunistic daily retrain.  Set low (3) so early-stage bots with few
# trades still benefit from fresh labels quickly.
MIN_NEW_OUTCOMES_FOR_RETRAIN = 3


@celery_app.task(name="tasks.ml_retrain.retrain_all", bind=True, max_retries=1)
def retrain_all(self):
    """
    Pull last 365 days of OHLCV + resolved live trade outcomes,
    blend heuristic and ground-truth labels, retrain XGBoost,
    evaluate on holdout (AUC gate 0.55), and save if improved.
    """
    try:
        import asyncio
        from models.trainer import ModelTrainer
        from core.ml_scorer import ml_scorer

        async def _retrain():
            trainer = ModelTrainer()
            report = await trainer.retrain_all()
            logger.info(f"ML retrain complete: {report}")

            # 1. Reload cache in THIS Celery worker process
            ml_scorer.reload()
            logger.info("[ml_retrain] Celery-process MLScorer cache cleared")

            # 2. Tell the FastAPI server process to also reload its cache.
            #    They are separate processes — in-memory reload above has no effect there.
            try:
                import httpx
                from config import settings
                # httpx.post is synchronous here (inside asyncio.run context already),
                # but runs in a thread executor to avoid blocking the event loop.
                import asyncio as _aio
                loop = _aio.get_running_loop()
                resp = await loop.run_in_executor(
                    None,
                    lambda: httpx.post(
                        f"{settings.api_internal_url}/internal/ml/reload",
                        headers={"X-Internal-Secret": settings.internal_api_secret},
                        timeout=5.0,
                    ),
                )
                if resp.status_code >= 300:
                    logger.warning(
                        f"[ml_retrain] FastAPI MLScorer cache flush FAILED: HTTP {resp.status_code} "
                        "— FastAPI process may still be using the old model until next restart or retrain"
                    )
                    try:
                        from db.database import AsyncSessionLocal
                        from notifications.notifier import dispatch as _notif_dispatch
                        async with AsyncSessionLocal() as _db:
                            await _notif_dispatch(
                                _db,
                                title="ML Model Cache Out of Sync",
                                message=(
                                    f"FastAPI cache flush returned HTTP {resp.status_code}. "
                                    "FastAPI may still be scoring signals with the old ML model. "
                                    "Retrain again or restart the backend to fix."
                                ),
                                level="warning",
                                category="ml",
                            )
                            await _db.commit()
                    except Exception as _notif_err:
                        logger.debug(f"[ml_retrain] Notification dispatch failed: {_notif_err}")
                else:
                    logger.info(
                        f"[ml_retrain] FastAPI MLScorer cache flush: HTTP {resp.status_code}"
                    )
            except Exception as _http_err:
                logger.warning(
                    f"[ml_retrain] FastAPI cache flush skipped "
                    f"(server may not be running): {_http_err}"
                )
                try:
                    from db.database import AsyncSessionLocal
                    from notifications.notifier import dispatch as _notif_dispatch
                    async with AsyncSessionLocal() as _db:
                        await _notif_dispatch(
                            _db,
                            title="ML Cache Flush Failed — Backend Unreachable",
                            message=(
                                f"Could not reach FastAPI to flush the ML model cache: {_http_err}. "
                                "FastAPI may still be scoring with the old ML model until next "
                                "restart or retrain."
                            ),
                            level="warning",
                            category="ml",
                        )
                        await _db.commit()
                except Exception as _notif_err:
                    logger.debug(f"[ml_retrain] Notification dispatch failed: {_notif_err}")

            return report

        report = asyncio.run(_retrain())
        return report
    except Exception as exc:
        logger.error(f"ML retrain task failed: {exc}")
        raise self.retry(exc=exc, countdown=600)


@celery_app.task(name="tasks.ml_retrain.retrain_if_new_outcomes", bind=True, max_retries=1)
def retrain_if_new_outcomes(self):
    """
    Daily opportunistic retrain — only fires when enough new resolved live
    outcomes have accumulated since the last model was saved.

    This ensures:
    - Models stay fresh in active trading periods without waiting for Sunday
    - Symbols with no new data are skipped (no wasted compute)
    - Cold-start bootstraps faster once first trades resolve
    """
    try:
        import asyncio
        import datetime
        import pathlib
        import json

        async def _check_and_retrain():
            from db.database import AsyncSessionLocal
            from db.models import TradeOutcome
            from sqlalchemy import select, func

            # Find symbols with outcomes resolved in the last 48 h
            cutoff = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(hours=48)

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(TradeOutcome.symbol, func.count(TradeOutcome.id).label("cnt"))
                    .where(
                        TradeOutcome.resolved == True,  # noqa: E712
                        TradeOutcome.ml_label != None,   # noqa: E711
                        TradeOutcome.resolved_at >= cutoff,
                        TradeOutcome.is_paper == False,  # DI-2 FIX: exclude paper outcomes
                    )
                    .group_by(TradeOutcome.symbol)
                    .having(func.count(TradeOutcome.id) >= MIN_NEW_OUTCOMES_FOR_RETRAIN)
                )
                rows = result.all()

            if not rows:
                logger.info("[ml_retrain] Opportunistic check: no symbols with enough new outcomes — skipping")
                return {"status": "skipped", "reason": "no_new_outcomes"}

            symbols_to_retrain = [r.symbol for r in rows]
            logger.info(f"[ml_retrain] Opportunistic retrain triggered for: {symbols_to_retrain}")

            from models.trainer import ModelTrainer
            from core.ml_scorer import ml_scorer

            trainer = ModelTrainer()

            # Fetch strategy configs to get broker/timeframe for each symbol
            from db.models import Strategy as StrategyModel
            async with AsyncSessionLocal() as session:
                strats = (await session.execute(
                    select(StrategyModel).where(StrategyModel.is_active == True)  # noqa: E712
                )).scalars().all()

            sym_config: dict[str, tuple[str, str]] = {}
            for s in strats:
                params = s.parameters or {}
                sym = params.get("symbol")
                if sym and sym in symbols_to_retrain:
                    broker = str(s.broker.value) if hasattr(s.broker, "value") else str(s.broker)
                    tf = params.get("timeframe", "1d")
                    sym_config[sym] = (broker, tf)

            results = []
            for sym in symbols_to_retrain:
                broker, tf = sym_config.get(sym, ("binance", "1d"))
                res = await trainer.train_symbol(sym, broker, timeframe=tf)
                results.append(res)

            trained = sum(1 for r in results if r.get("status") == "trained")
            if trained:
                ml_scorer.reload()
                # Best-effort cache flush on FastAPI process
                try:
                    import httpx
                    from config import settings as _settings
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(
                        None,
                        lambda: httpx.post(
                            f"{_settings.api_internal_url}/internal/ml/reload",
                            headers={"X-Internal-Secret": _settings.internal_api_secret},
                            timeout=5.0,
                        ),
                    )
                except Exception as _flush_err:
                    logger.debug(f"[ml_retrain] Opportunistic cache flush skipped: {_flush_err}")

            return {"status": "complete", "trained": trained, "symbols": results}

        report = asyncio.run(_check_and_retrain())
        logger.info(f"[ml_retrain] Opportunistic retrain result: {report}")
        return report
    except Exception as exc:
        logger.error(f"Opportunistic ML retrain failed: {exc}")
        raise self.retry(exc=exc, countdown=300)
