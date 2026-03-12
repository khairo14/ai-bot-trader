"""
Notifications API routes.

GET  /api/notifications/          — list recent notifications (paginated)
GET  /api/notifications/unread-count — count of unread
POST /api/notifications/{id}/read — mark one as read
POST /api/notifications/read-all  — mark all as read
DELETE /api/notifications/{id}    — delete one
DELETE /api/notifications/clear   — delete all read notifications
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update, delete
from typing import Optional

from db.database import get_db
from db.models import Notification
from core.auth import get_current_user

router = APIRouter(tags=["notifications"])


def _notif_dict(n: Notification) -> dict:
    # Timestamps are stored as naive UTC (tzinfo stripped by _utcnow).
    # Appending 'Z' makes JavaScript parse them as UTC, not local time,
    # so timeAgo() in the frontend is always correct regardless of the
    # user's browser timezone.
    created_iso = (n.created_at.isoformat() + 'Z') if n.created_at else None
    return {
        "id": n.id,
        "level": n.level.value if hasattr(n.level, "value") else n.level,
        "category": n.category.value if hasattr(n.category, "value") else n.category,
        "title": n.title,
        "message": n.message,
        "is_read": n.is_read,
        "email_sent": n.email_sent,
        "metadata": n.extra_data or {},
        "created_at": created_iso,
    }


@router.get("/")
async def list_notifications(
    limit: int = 50,
    offset: int = 0,
    unread_only: bool = False,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Return recent notifications, newest first."""
    q = select(Notification).order_by(Notification.created_at.desc()).limit(limit).offset(offset)
    if unread_only:
        q = q.where(Notification.is_read == False)  # noqa: E712
    result = await db.execute(q)
    notifications = result.scalars().all()
    return {"notifications": [_notif_dict(n) for n in notifications]}


@router.get("/unread-count")
async def unread_count(db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    """Return the count of unread notifications (for the bell badge)."""
    q = select(func.count()).where(Notification.is_read == False)  # noqa: E712
    count = (await db.execute(q)).scalar_one()
    return {"unread_count": count}


@router.post("/{notif_id}/read")
async def mark_read(notif_id: int, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    """Mark a single notification as read."""
    result = await db.execute(select(Notification).where(Notification.id == notif_id))
    n = result.scalar_one_or_none()
    if not n:
        raise HTTPException(status_code=404, detail="Notification not found")
    n.is_read = True
    await db.commit()
    return {"ok": True}


@router.post("/read-all")
async def mark_all_read(db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    """Mark all notifications as read."""
    await db.execute(
        update(Notification).where(Notification.is_read == False).values(is_read=True)  # noqa: E712
    )
    await db.commit()
    return {"ok": True}


@router.delete("/{notif_id}")
async def delete_notification(notif_id: int, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    """Delete a single notification."""
    await db.execute(delete(Notification).where(Notification.id == notif_id))
    await db.commit()
    return {"ok": True}


@router.delete("/clear/read")
async def clear_read(db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    """Delete all notifications that have been read."""
    await db.execute(delete(Notification).where(Notification.is_read == True))  # noqa: E712
    await db.commit()
    return {"ok": True}
