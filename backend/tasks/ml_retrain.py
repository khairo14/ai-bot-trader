"""Tasks: weekly ML model retraining."""
from celery_app import celery_app
import logging

logger = logging.getLogger(__name__)


@celery_app.task(name="tasks.ml_retrain.retrain_all", bind=True, max_retries=1)
def retrain_all(self):
    """
    Pull last 90 days of OHLCV data for each active strategy symbol,
    compute features, retrain classifier, evaluate on holdout, save model.
    """
    try:
        import asyncio
        from models.trainer import ModelTrainer

        async def _retrain():
            trainer = ModelTrainer()
            report = await trainer.retrain_all()
            logger.info(f"ML retrain complete: {report}")

        asyncio.run(_retrain())
    except Exception as exc:
        logger.error(f"ML retrain task failed: {exc}")
        raise self.retry(exc=exc, countdown=600)
