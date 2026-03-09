import csv
import io

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from pydantic import BaseModel
from typing import Optional

from db.database import get_db
from db.models import Trade, OrderStatus

router = APIRouter()


def _trade_dict(t: Trade) -> dict:
    return {
        "id": t.id,
        "symbol": t.symbol,
        "side": t.side,
        "quantity": t.quantity,
        "entry_price": t.entry_price,
        "exit_price": t.exit_price,
        "stop_loss": t.stop_loss,
        "take_profit": t.take_profit,
        "pnl": t.pnl,
        "pnl_pct": t.pnl_pct,
        "broker": t.broker.value if hasattr(t.broker, "value") else t.broker,
        "strategy_name": t.strategy_name,
        "is_paper": t.is_paper,
        "status": t.status.value if hasattr(t.status, "value") else t.status,
        "opened_at": t.opened_at.isoformat() if t.opened_at else None,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
    }


@router.get("/open")
async def get_open_positions(db: AsyncSession = Depends(get_db)):
    """Get all currently open (paper or live) positions."""
    result = await db.execute(
        select(Trade).where(Trade.status == OrderStatus.OPEN)
    )
    trades = result.scalars().all()
    return {"positions": [_trade_dict(t) for t in trades]}


@router.get("/history")
async def get_trade_history(
    limit: int = 200,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Get completed trades, newest first. Defaults to last 200; use offset for pagination."""
    result = await db.execute(
        select(Trade)
        .where(Trade.status == OrderStatus.FILLED)
        .order_by(desc(Trade.closed_at))
        .limit(min(limit, 1000))   # hard cap at 1000 rows per call
        .offset(offset)
    )
    trades = result.scalars().all()
    return {"trades": [_trade_dict(t) for t in trades]}


@router.get("/history/export")
async def export_trade_history(db: AsyncSession = Depends(get_db)):
    """Download all completed trades as CSV."""
    result = await db.execute(
        select(Trade)
        .where(Trade.status == OrderStatus.FILLED)
        .order_by(desc(Trade.closed_at))
    )
    trades = result.scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "symbol", "side", "quantity", "entry_price", "exit_price",
        "pnl", "pnl_pct", "broker", "strategy_name", "is_paper",
        "opened_at", "closed_at",
    ])
    for t in trades:
        writer.writerow([
            t.id, t.symbol, t.side, t.quantity, t.entry_price, t.exit_price,
            t.pnl, t.pnl_pct, t.broker, t.strategy_name, t.is_paper,
            t.opened_at.isoformat() if t.opened_at else "",
            t.closed_at.isoformat() if t.closed_at else "",
        ])
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trade_history.csv"},
    )


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
    try:
        await engine.close_position(trade, reason=request.reason or "manual_close", db_session=db)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Broker rejected the closing order: {exc}")
    await db.commit()

    return {"message": f"Position {request.trade_id} closed.", "trade_id": request.trade_id}


@router.post("/emergency-stop")
async def emergency_stop(db: AsyncSession = Depends(get_db)):
    """
    EMERGENCY STOP: Close all open positions and halt all strategies immediately.
    """
    from core.engine.forward_engine import ForwardEngine
    from notifications.notifier import notifier as _notify
    engine = ForwardEngine()
    closed = await engine.emergency_stop(db_session=db)
    from core.auth import audit
    audit("emergency_stop", closed_positions=closed)
    await _notify.emergency(
        db,
        title="⚠️ Emergency Stop Activated",
        message=f"{closed} open position(s) were force-closed. All trading halted.",
        metadata={"positions_closed": closed},
    )
    await db.commit()
    return {"message": "Emergency stop executed.", "positions_closed": closed}
