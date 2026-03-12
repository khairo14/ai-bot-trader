"""
Central notification dispatcher.

Usage
-----
    from notifications.notifier import dispatch

    await dispatch(
        db=db_session,
        title="BUY signal: BTC/USDT",
        message="Confidence 78% — entry $65,200",
        level="success",
        category="signal",
        metadata={"symbol": "BTC/USDT", "broker": "binance"},
        send_email=True,   # only actually sends when NOTIFY_EMAIL_ENABLED=true
    )

Both in-app (DB row) and email are fire-and-forget — exceptions are logged,
not re-raised, so a broken email config never crashes a trade path.
"""

from __future__ import annotations

import asyncio
from typing import Optional
from loguru import logger

from db.models import Notification, NotificationLevel, NotificationCategory


def _get_email_notifier():
    """Lazy-build the EmailNotifier from settings (avoids circular import at module load)."""
    try:
        from config import settings
        from notifications.email_notifier import EmailNotifier
        to_addr = getattr(settings, "notify_email_to", "") or getattr(settings, "gmail_user", "")
        return EmailNotifier(
            gmail_user=getattr(settings, "gmail_user", ""),
            app_password=getattr(settings, "gmail_app_password", ""),
            to_address=to_addr,
        )
    except Exception as exc:
        logger.warning(f"[Notifier] Could not build EmailNotifier: {exc}")
        return None


# Module-level singleton (built on first use)
_email_notifier = None


def _email():
    global _email_notifier
    if _email_notifier is None:
        _email_notifier = _get_email_notifier()
    return _email_notifier


async def dispatch(
    db,
    title: str,
    message: str,
    level: str = "info",
    category: str = "system",
    metadata: Optional[dict] = None,
    send_email: bool = False,
) -> Notification:
    """
    Create an in-app notification (DB row), optionally send an email,
    and broadcast via WebSocket.

    Parameters
    ----------
    db          : AsyncSession — required for DB persistence
    title       : Short notification title (shown in bell)
    message     : Full notification body
    level       : info | success | warning | error
    category    : signal | trade | emergency | system | ml
    metadata    : Dict of extra structured data (symbol, broker, etc.)
    send_email  : Whether to also send a Gmail notification

    Returns the saved Notification ORM object.
    """
    # ── 1. Persist to DB ────────────────────────────────────────────────────
    notif = Notification(
        level=NotificationLevel(level),
        category=NotificationCategory(category),
        title=title,
        message=message,
        extra_data=metadata or {},
    )
    try:
        db.add(notif)
        await db.flush()   # get the id without full commit (caller commits)
        await db.refresh(notif)
    except Exception as exc:
        logger.error(f"[Notifier] DB persist failed: {exc}")

    # ── 2. WebSocket broadcast ───────────────────────────────────────────────
    try:
        from api.websocket import manager as ws_manager
        await ws_manager.broadcast("notification", {
            "id": getattr(notif, "id", None),
            "level": level,
            "category": category,
            "title": title,
            "message": message,
            "metadata": metadata or {},
        })
    except Exception as exc:
        logger.debug(f"[Notifier] WS broadcast skipped: {exc}")

    # ── 3. Email (fire-and-forget) ───────────────────────────────────────────
    if send_email:
        try:
            from config import settings
            email_enabled = getattr(settings, "notify_email_enabled", False)
            if email_enabled:
                emailer = _email()
                if emailer and emailer.enabled:
                    # IMP-23 FIX: await the email directly instead of using
                    # asyncio.create_task().  create_task() spawns a background
                    # task that is cancelled when asyncio.run() returns in Celery
                    # workers — emails were silently dropped on every Celery tick.
                    # emailer.send() uses asyncio.to_thread() internally so it is
                    # already non-blocking and safe to await here.
                    ok = await emailer.send(title=title, message=message, level=level, metadata=metadata)
                    notif.email_sent = ok
        except Exception as exc:
            logger.warning(f"[Notifier] Email dispatch skipped: {exc}")

    return notif


class _NotifierFacade:
    """
    Convenience object so you can do:
        from notifications.notifier import notifier
        await notifier.signal(db, "BUY BTC/USDT", ...)
    """

    async def signal(self, db, title: str, message: str, metadata: Optional[dict] = None) -> Notification:
        return await dispatch(db, title, message, level="info", category="signal",
                              metadata=metadata, send_email=False)

    async def trade(self, db, title: str, message: str, metadata: Optional[dict] = None) -> Notification:
        return await dispatch(db, title, message, level="success", category="trade",
                              metadata=metadata, send_email=True)

    async def emergency(self, db, title: str, message: str, metadata: Optional[dict] = None) -> Notification:
        return await dispatch(db, title, message, level="error", category="emergency",
                              metadata=metadata, send_email=True)

    async def warning(self, db, title: str, message: str, metadata: Optional[dict] = None, category: str = "system") -> Notification:
        # BUG-12 FIX: accept an optional category so callers can distinguish
        # risk warnings, broker warnings, etc. from generic system events.
        # Defaults to "system" for backward compatibility.
        return await dispatch(db, title, message, level="warning", category=category,
                              metadata=metadata, send_email=True)

    async def ml(self, db, title: str, message: str, metadata: Optional[dict] = None) -> Notification:
        return await dispatch(db, title, message, level="info", category="ml",
                              metadata=metadata, send_email=False)


notifier = _NotifierFacade()
