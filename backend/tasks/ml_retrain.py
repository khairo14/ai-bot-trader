"""Tasks: weekly ML model retraining (with live outcome labels)."""
from celery_app import celery_app
import logging

logger = logging.getLogger(__name__)


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
