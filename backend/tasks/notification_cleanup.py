"""Tasks: weekly notification archival to prevent unbounded table growth."""
from celery_app import celery_app
import logging

logger = logging.getLogger(__name__)


@celery_app.task(name="tasks.notification_cleanup.cleanup_notifications", bind=True, max_retries=1)
def cleanup_notifications(self):
    """
    Delete read notifications older than 30 days.
    Unread notifications are always retained regardless of age.

    Scheduled: Sunday 04:00 UTC (after ml_retrain and portfolio_rebalance).
    """
    try:
        import asyncio
        from datetime import datetime, timezone, timedelta

        async def _cleanup() -> int:
            from db.database import AsyncSessionLocal
            from db.models import Notification
            from sqlalchemy import delete

            cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    delete(Notification).where(
                        Notification.is_read == True,
                        Notification.created_at < cutoff,
                    )
                )
                await session.commit()
                from sqlalchemy.engine import CursorResult
                return result.rowcount  # type: ignore[union-attr]  # delete() always returns CursorResult

        deleted = asyncio.run(_cleanup())
        logger.info(
            f"[NotificationCleanup] Deleted {deleted} read notifications older than 30 days."
        )
        return {"deleted": deleted}

    except Exception as exc:
        logger.error(f"[NotificationCleanup] Error: {exc}")
        raise self.retry(exc=exc, countdown=3600)  # retry in 1 hour
