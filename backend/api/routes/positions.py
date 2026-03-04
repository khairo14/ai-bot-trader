from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional

from db.database import get_db
from db.models import Trade, OrderStatus

router = APIRouter()


@router.get("/open")
async def get_open_positions(db: AsyncSession = Depends(get_db)):
    """Get all currently open (paper or live) positions."""
    result = await db.execute(
        select(Trade).where(Trade.status == OrderStatus.OPEN)
    )
    trades = result.scalars().all()
    return {"positions": trades}


@router.get("/history")
async def get_trade_history(db: AsyncSession = Depends(get_db)):
    """Get all completed trades."""
    result = await db.execute(
        select(Trade).where(Trade.status == OrderStatus.FILLED)
    )
    trades = result.scalars().all()
    return {"trades": trades}


class ClosePositionRequest(BaseModel):
    trade_id: int
    reason: Optional[str] = "manual_close"


@router.post("/close")
async def close_position(request: ClosePositionRequest, db: AsyncSession = Depends(get_db)):
    """Manually close an open position at market price."""
    result = await db.execute(select(Trade).where(Trade.id == request.trade_id))
    trade = result.scalar_one_or_none()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    if trade.status != OrderStatus.OPEN:
        raise HTTPException(status_code=400, detail="Trade is not open")

    from core.engine.forward_engine import ForwardEngine
    engine = ForwardEngine()
    await engine.close_position(trade, reason=request.reason or "manual_close")
    await db.commit()

    return {"message": f"Position {request.trade_id} closed.", "trade_id": request.trade_id}


@router.post("/emergency-stop")
async def emergency_stop(db: AsyncSession = Depends(get_db)):
    """
    EMERGENCY STOP: Close all open positions and halt all strategies immediately.
    """
    from core.engine.forward_engine import ForwardEngine
    engine = ForwardEngine()
    closed = await engine.emergency_stop()
    return {"message": "Emergency stop executed.", "positions_closed": closed}
