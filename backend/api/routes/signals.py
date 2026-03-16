from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, update, or_, and_
from typing import Optional
from datetime import datetime, timedelta, timezone

from db.database import get_db
from db.models import Signal, ExecutionMode, SignalType, BrokerName
from core.auth import get_current_user

router = APIRouter()


def _signal_dict(s: Signal) -> dict:
    return {
        "id": s.id,
        "symbol": s.symbol,
        "signal": s.signal.value if hasattr(s.signal, "value") else s.signal,
        "entry_price": s.entry_price,
        "stop_loss": s.stop_loss,
        "take_profit": s.take_profit,
        "confidence": s.confidence,
        "timeframe": s.timeframe,
        "strategy_name": s.strategy_name,
        "regime": s.regime,
        "asset_class": s.asset_class.value if hasattr(s.asset_class, "value") else s.asset_class,
        "broker": s.broker.value if hasattr(s.broker, "value") else s.broker,
        "execution_mode": s.execution_mode.value if hasattr(s.execution_mode, "value") else s.execution_mode,
        "reasons": s.reasons,
        "acted_on": s.acted_on,
        "dismissed": s.dismissed,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        # Options fields (None for equity signals)
        "iv_rank":      s.iv_rank,
        "delta":        s.delta,
        "theta":        s.theta,
        "vega":         s.vega,
        "options_meta": s.options_meta,
    }


@router.get("/")
async def list_signals(
    symbol: Optional[str] = None,
    broker: Optional[str] = None,
    strategy_prefix: Optional[str] = None,
    limit: int = Query(default=50, le=500),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get recent signals. Admins see all; regular users see system signals + their own (F-059)."""
    query = (
        select(Signal)
        .where(Signal.dismissed == False)  # noqa: E712
        .where(Signal.signal != SignalType.HOLD)  # HOLDs are never actionable; cleared by dismiss-expired
        .order_by(desc(Signal.created_at))
        .limit(limit)
    )
    if not current_user.is_admin:
        # Non-admins see: signals they created OR system-generated signals (user_id=NULL)
        query = query.where(
            or_(Signal.user_id == current_user.id, Signal.user_id.is_(None))
        )
    if symbol:
        query = query.where(Signal.symbol == symbol.upper())
    if broker:
        try:
            query = query.where(Signal.broker == BrokerName(broker.lower()))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown broker: {broker}")
    if strategy_prefix:
        query = query.where(Signal.strategy_name.like(f"{strategy_prefix}%"))
    result = await db.execute(query)
    return {"signals": [_signal_dict(s) for s in result.scalars().all()]}


@router.post("/dismiss-expired")
async def dismiss_expired_signals(
    older_than_hours: int = Query(default=24, ge=1, le=168),
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """
    Dismiss signals that are no longer useful:
    - ALL HOLD signals (never actionable, any age)
    - Un-acted-on signals older than `older_than_hours` (default 24h)
    Signals are hidden from Recent Signals but kept in DB for ML auditing.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=older_than_hours)
    result = await db.execute(
        update(Signal)
        .where(
            Signal.dismissed == False,  # noqa: E712
            or_(
                Signal.signal == SignalType.HOLD,  # always dismiss HOLDs
                # dismiss old un-acted-on signals of any type
                (Signal.acted_on == False) & (Signal.created_at < cutoff),  # noqa: E712
            ),
        )
        .values(dismissed=True)
    )
    await db.commit()
    count = result.rowcount
    return {"dismissed": count, "message": f"{count} signal(s) cleared from Recent Signals."}


@router.get("/pending")
async def list_pending_signals(db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    """Return all semi-auto signals waiting for manual approval."""
    result = await db.execute(
        select(Signal)
        .where(
            Signal.execution_mode == ExecutionMode.SEMI_AUTO.value,
            Signal.acted_on == False,  # noqa: E712
            Signal.dismissed == False,  # noqa: E712
        )
        .order_by(desc(Signal.created_at))
    )
    return {"signals": [_signal_dict(s) for s in result.scalars().all()]}


@router.post("/{signal_id}/approve")
async def approve_signal(signal_id: int, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    """
    DEPRECATED: Thin redirect to /api/forward-test/execute-signal/{id}.
    This endpoint is kept only for backwards compatibility with any external callers.
    All frontend code now calls the forward-test endpoint directly.
    """
    from api.routes.forward_test import execute_signal as _execute_signal
    return await _execute_signal(signal_id=signal_id, db=db)


@router.post("/{signal_id}/reject")
async def reject_signal(signal_id: int, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    """
    Reject a pending semi-auto signal (mark as acted-on without placing a trade).
    """
    sig_row = (await db.execute(select(Signal).where(Signal.id == signal_id))).scalar_one_or_none()
    if not sig_row:
        raise HTTPException(status_code=404, detail="Signal not found")
    if sig_row.acted_on:
        raise HTTPException(status_code=400, detail="Signal already acted on")

    sig_row.acted_on = True
    await db.commit()
    return {"message": "Signal rejected.", "signal_id": signal_id}


@router.get("/{signal_id}")
async def get_signal(signal_id: int, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    """Get a single signal by ID."""
    result = await db.execute(select(Signal).where(Signal.id == signal_id))
    signal = result.scalar_one_or_none()
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found")
    return _signal_dict(signal)
