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
    include=["tasks.ml_retrain", "tasks.signal_runner", "tasks.outcome_resolver", "tasks.portfolio_rebalancer"],
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
}

if __name__ == "__main__":
    celery_app.start()
