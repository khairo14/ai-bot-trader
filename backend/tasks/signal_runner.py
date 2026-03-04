"""Tasks: automated signal runner via Celery."""
from celery_app import celery_app
import logging

logger = logging.getLogger(__name__)


@celery_app.task(name="tasks.signal_runner.run_signals", bind=True, max_retries=3)
def run_signals(self):
    """
    Fetch latest OHLCV data for all active strategies and generate signals.
    Results are persisted to the DB and broadcast via WebSocket.
    """
    try:
        import asyncio
        from core.engine.signal_engine import SignalEngine
        from db.database import async_session_factory
        from db.models import Strategy as StrategyModel

        async def _run():
            async with async_session_factory() as session:
                from sqlalchemy import select
                result = await session.execute(
                    select(StrategyModel).where(StrategyModel.is_active == True)
                )
                active_strategies = result.scalars().all()
                engine = SignalEngine()
                for s in active_strategies:
                    try:
                        await engine.run(s)
                    except Exception as e:
                        logger.error(f"Signal run failed for strategy {s.id}: {e}")

        asyncio.run(_run())
        logger.info("Signal runner completed")
    except Exception as exc:
        logger.error(f"Signal runner task failed: {exc}")
        raise self.retry(exc=exc, countdown=60)
