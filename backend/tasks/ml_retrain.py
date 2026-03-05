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
            # Reload scorer so next inference picks up the new model
            ml_scorer.reload()
            logger.info("MLScorer cache cleared — new model active")
            return report

        report = asyncio.run(_retrain())
        return report
    except Exception as exc:
        logger.error(f"ML retrain task failed: {exc}")
        raise self.retry(exc=exc, countdown=600)
