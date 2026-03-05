"""Tasks: weekly portfolio weight rebalancing (ML-03)."""
from celery_app import celery_app
import logging

logger = logging.getLogger(__name__)


@celery_app.task(name="tasks.portfolio_rebalancer.rebalance", bind=True, max_retries=1)
def rebalance(self):
    """
    Weekly Celery task — computes Sharpe-weighted portfolio allocation
    from resolved TradeOutcome data and writes weights to Strategy.parameters.
    """
    try:
        import asyncio
        from models.portfolio_optimizer import optimize_portfolio

        result = asyncio.run(optimize_portfolio())
        logger.info(f"[rebalancer] Complete: {result}")
        return result
    except Exception as exc:
        logger.error(f"[rebalancer] Failed: {exc}")
        raise self.retry(exc=exc, countdown=3600)
