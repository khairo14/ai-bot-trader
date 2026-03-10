"""
Celery application instance.
Workers: celery -A celery_app worker --loglevel=info
Beat:    celery -A celery_app beat   --loglevel=info
"""
from celery import Celery
from celery.schedules import crontab
from config import settings

celery_app = Celery(
    "ai_bot_trader",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["tasks.ml_retrain", "tasks.signal_runner", "tasks.outcome_resolver", "tasks.portfolio_rebalancer", "tasks.notification_cleanup"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)

# Scheduled tasks
celery_app.conf.beat_schedule = {
    # Run signal engine every 5 minutes during market hours
    "run-signals-every-5m": {
        "task": "tasks.signal_runner.run_signals",
        "schedule": 300,  # every 5 minutes
    },
    # Resolve pending trade outcomes nightly at 01:30 UTC
    "resolve-outcomes-nightly": {
        "task": "tasks.outcome_resolver.resolve_outcomes",
        "schedule": crontab(hour=1, minute=30),
    },
    # Retrain ML models weekly (Sunday 02:00 UTC — after outcomes are resolved)
    "ml-retrain-weekly": {
        "task": "tasks.ml_retrain.retrain_all",
        "schedule": crontab(hour=2, minute=0, day_of_week="sunday"),
    },
    # Rebalance portfolio weights weekly (Sunday 03:00 UTC — after ML retrain)
    "portfolio-rebalance-weekly": {
        "task": "tasks.portfolio_rebalancer.rebalance",
        "schedule": crontab(hour=3, minute=0, day_of_week="sunday"),
    },
    # Delete read notifications older than 30 days (Sunday 04:00 UTC)
    "cleanup-notifications-weekly": {
        "task": "tasks.notification_cleanup.cleanup_notifications",
        "schedule": crontab(hour=4, minute=0, day_of_week="sunday"),
    },
}


# ── Post-fork worker initialization ─────────────────────────────────────────
# Runs once in each Celery forked worker process immediately after the fork.
# Three concerns:
#   1. asyncpg engine: the parent's connection pool futures are bound to the
#      parent's event loop (now dead in the child) → dispose so the child
#      creates fresh asyncpg connections on its own event loop.
#   2. IBKR clientId: FastAPI uses ibkr_client_id (default 1). Every Celery
#      worker must use ibkr_client_id_celery (default 2) so they don't
#      collide with the FastAPI singleton connection (IBKR error 326).
#   3. Broker singletons: the cached IBKRClient from the parent (clientId 1)
#      must be evicted so the Celery task creates a fresh one with clientId 2.
from celery.signals import worker_process_init
import os


@worker_process_init.connect
def _init_worker_process(**kwargs):
    """One-time post-fork setup for each Celery worker process."""
    # 1. Mark this process as a Celery worker so IBKRClient picks up the
    #    celery-specific clientId instead of the FastAPI one.
    os.environ["CELERY_WORKER_PROCESS"] = "1"

    # 2. Evict broker singletons built in the parent (wrong clientId).
    try:
        from brokers import invalidate_broker_cache
        invalidate_broker_cache()
    except Exception:
        pass

    # 3. Dispose inherited asyncpg connections AND recreate the engine with
    #    NullPool so Celery tasks don't share pooled connections across
    #    multiple asyncio.run() calls (each call creates/destroys a loop,
    #    leaving pooled connections bound to a dead loop → "Future attached
    #    to a different loop").  NullPool opens a fresh DB connection per
    #    session and closes it immediately — correct for short-lived tasks.
    try:
        import asyncio
        import db.database as _db
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
        from sqlalchemy.pool import NullPool
        # Dispose old pool gracefully
        loop = asyncio.new_event_loop()
        loop.run_until_complete(_db.engine.dispose())
        loop.close()
        # Replace with a NullPool engine (no connection reuse between tasks)
        _db.engine = create_async_engine(
            _db.async_db_url,
            echo=False,
            poolclass=NullPool,
        )
        _db.AsyncSessionLocal = async_sessionmaker(
            _db.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    except Exception:
        pass


if __name__ == "__main__":
    celery_app.start()
