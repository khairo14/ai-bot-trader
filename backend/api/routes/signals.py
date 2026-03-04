from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from typing import Optional

from db.database import get_db
from db.models import Signal

router = APIRouter()


@router.get("/")
async def list_signals(
    symbol: Optional[str] = None,
    broker: Optional[str] = None,
    limit: int = Query(default=50, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Get recent signals, optionally filtered by symbol and broker."""
    query = select(Signal).order_by(desc(Signal.created_at)).limit(limit)
    if symbol:
        query = query.where(Signal.symbol == symbol.upper())
    if broker:
        query = query.where(Signal.broker == broker.lower())
    result = await db.execute(query)
    signals = result.scalars().all()
    return {"signals": [
        {
            "id": s.id,
            "symbol": s.symbol,
            "signal": s.signal,
            "entry_price": s.entry_price,
            "stop_loss": s.stop_loss,
            "take_profit": s.take_profit,
            "confidence": s.confidence,
            "timeframe": s.timeframe,
            "strategy_name": s.strategy_name,
            "regime": s.regime,
            "asset_class": s.asset_class,
            "broker": s.broker,
            "reasons": s.reasons,
            "acted_on": s.acted_on,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in signals
    ]}


@router.get("/{signal_id}")
async def get_signal(signal_id: int, db: AsyncSession = Depends(get_db)):
    """Get a single signal by ID."""
    result = await db.execute(select(Signal).where(Signal.id == signal_id))
    signal = result.scalar_one_or_none()
    if not signal:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Signal not found")
    return signal
