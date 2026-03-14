import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from pydantic import BaseModel
from typing import Optional
from loguru import logger

from db.database import get_db
from db.models import Trade, LiveTrade, OrderStatus
from core.auth import get_current_user, require_admin

router = APIRouter()


def _trade_dict(t: Trade | LiveTrade) -> dict:
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
async def get_open_positions(
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Get all currently open (paper or live) positions."""
    paper = list((await db.execute(select(Trade).where(Trade.status == OrderStatus.OPEN))).scalars().all())
    live  = list((await db.execute(select(LiveTrade).where(LiveTrade.status == OrderStatus.OPEN))).scalars().all())
    return {"positions": [_trade_dict(t) for t in paper + live]}


@router.get("/history")
async def get_trade_history(
    limit: int = 200,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Get completed trades, newest first. Defaults to last 200; use offset for pagination."""
    paper = list((await db.execute(
        select(Trade).where(Trade.status == OrderStatus.FILLED)
        .order_by(desc(Trade.closed_at)).limit(min(limit, 1000)).offset(offset)
    )).scalars().all())
    live  = list((await db.execute(
        select(LiveTrade).where(LiveTrade.status == OrderStatus.FILLED)
        .order_by(desc(LiveTrade.closed_at)).limit(min(limit, 1000)).offset(offset)
    )).scalars().all())
    all_trades = sorted(paper + live, key=lambda t: t.closed_at or t.opened_at or datetime.min, reverse=True)[:min(limit, 1000)]
    return {"trades": [_trade_dict(t) for t in all_trades]}


@router.get("/history/export")
async def export_trade_history(
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Download all completed trades as CSV."""
    paper = list((await db.execute(
        select(Trade).where(Trade.status == OrderStatus.FILLED).order_by(desc(Trade.closed_at))
    )).scalars().all())
    live  = list((await db.execute(
        select(LiveTrade).where(LiveTrade.status == OrderStatus.FILLED).order_by(desc(LiveTrade.closed_at))
    )).scalars().all())
    trades = sorted(paper + live, key=lambda t: t.closed_at or t.opened_at or datetime.min, reverse=True)

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
async def close_position(
    request: ClosePositionRequest,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Manually close an open position at market price."""
    # Filter by OPEN status in both queries to avoid the ID-collision case:
    # if Trade.id=N exists (status=FILLED) and LiveTrade.id=N also exists
    # (status=OPEN), looking up by id alone would find the closed paper trade
    # first and error with "Trade is not open", never reaching the live table.
    trade = (await db.execute(
        select(Trade).where(Trade.id == request.trade_id, Trade.status == OrderStatus.OPEN)
    )).scalar_one_or_none()
    if trade is None:
        trade = (await db.execute(
            select(LiveTrade).where(LiveTrade.id == request.trade_id, LiveTrade.status == OrderStatus.OPEN)
        )).scalar_one_or_none()
    if not trade:
        raise HTTPException(status_code=404, detail="Open trade not found")

    from core.engine.forward_engine import ForwardEngine
    engine = ForwardEngine()
    try:
        await engine.close_position(trade, reason=request.reason or "manual_close", db_session=db)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Broker rejected the closing order: {exc}")
    await db.commit()

    return {"message": f"Position {request.trade_id} closed.", "trade_id": request.trade_id}


@router.post("/emergency-stop")
async def emergency_stop(
    db: AsyncSession = Depends(get_db),
    _user=Depends(require_admin),
):
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


@router.post("/sync-broker")
async def sync_broker_positions(
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """
    Immediately pull all open positions from IBKR and create DB records for any
    that are not already tracked.  Also closes ghost DB positions that the broker
    has already exited.  This is the same logic that runs every 60 s via the
    Celery scheduler — calling this endpoint forces it to run right now.
    """
    from core.engine.forward_engine import ForwardEngine
    engine = ForwardEngine()
    try:
        synced = await engine.reconcile_positions(db)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Broker sync failed: {exc}")
    return {"message": f"Broker sync complete.", "positions_synced": synced}


@router.post("/settle-fx")
async def settle_fx_now(_user=Depends(get_current_user)):
    """
    Immediately convert any non-USD cash balances at IBKR into USD via spot
    market orders.  Call this after a forex position closes to avoid T+2
    settlement-period balance fluctuation.
    """
    from brokers.ibkr_client import IBKRClient
    broker = IBKRClient()
    try:
        await broker.connect()
        conversions = await broker.auto_convert_fx()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"FX settlement failed: {exc}")
    return {
        "message": "FX settlement triggered.",
        "conversions": conversions,
    }


@router.get("/live-pnl")
async def get_live_pnl(_user=Depends(get_current_user)):
    """
    Fetch real-time unrealized P&L for open positions directly from each broker.

    Returns a dict keyed by broker name, each containing a list of
    {symbol, unrealized_pnl, entry_price, current_price} dicts.
    Falls back to an empty list for any broker that errors (e.g. market closed,
    no connection).
    """
    result: dict = {}

    # Alpaca — broker positions include real-time unrealized_pl
    try:
        from brokers.alpaca_client import AlpacaClient
        alpaca = AlpacaClient()
        positions = await alpaca.get_positions()
        result["alpaca"] = [
            {
                "symbol": p.symbol,
                "entry_price": p.entry_price,
                "current_price": p.current_price,
                "unrealized_pnl": p.unrealized_pnl,
            }
            for p in positions
        ]
    except Exception as _e:
        logger.debug(f"[live-pnl] Alpaca fetch error: {_e}")
        result["alpaca"] = []

    # IBKR — connect then fetch positions
    try:
        from brokers.ibkr_client import IBKRClient as _IBKRClient
        ibkr = _IBKRClient()
        await ibkr.connect()
        ibkr_positions = await ibkr.get_positions()
        result["ibkr"] = [
            {
                "symbol": p.symbol,
                "entry_price": p.entry_price,
                "current_price": p.current_price,
                "unrealized_pnl": p.unrealized_pnl,
            }
            for p in ibkr_positions
        ]
    except Exception as _e:
        logger.debug(f"[live-pnl] IBKR fetch error: {_e}")
        result["ibkr"] = []

    return result
