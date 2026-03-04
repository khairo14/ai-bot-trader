from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from typing import Optional

from db.database import get_db
from db.models import Signal, Strategy, ExecutionMode

router = APIRouter()


def _signal_dict(s: Signal) -> dict:
    return {
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
        "execution_mode": s.execution_mode,
        "reasons": s.reasons,
        "acted_on": s.acted_on,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    }


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
    return {"signals": [_signal_dict(s) for s in result.scalars().all()]}


@router.get("/pending")
async def list_pending_signals(db: AsyncSession = Depends(get_db)):
    """Return all semi-auto signals waiting for manual approval."""
    result = await db.execute(
        select(Signal)
        .where(
            Signal.execution_mode == ExecutionMode.SEMI_AUTO.value,
            Signal.acted_on == False,
        )
        .order_by(desc(Signal.created_at))
    )
    return {"signals": [_signal_dict(s) for s in result.scalars().all()]}


@router.post("/{signal_id}/approve")
async def approve_signal(signal_id: int, db: AsyncSession = Depends(get_db)):
    """
    Approve a pending semi-auto signal and execute it immediately as full-auto.
    """
    sig_row = (await db.execute(select(Signal).where(Signal.id == signal_id))).scalar_one_or_none()
    if not sig_row:
        raise HTTPException(status_code=404, detail="Signal not found")
    if sig_row.acted_on:
        raise HTTPException(status_code=400, detail="Signal already acted on")

    # Look up the originating strategy to get is_paper
    strat_row = (await db.execute(
        select(Strategy).where(
            Strategy.name == sig_row.strategy_name,
            Strategy.broker == sig_row.broker,
        ).limit(1)
    )).scalar_one_or_none()
    is_paper = strat_row.is_paper if strat_row else True

    # Reconstruct a Signal dataclass and execute via ForwardEngine
    from core.strategies.base import Signal as SigDC
    from core.engine.forward_engine import ForwardEngine

    signal_dc = SigDC(
        symbol=sig_row.symbol,
        signal=sig_row.signal.value if hasattr(sig_row.signal, "value") else sig_row.signal,
        entry_price=sig_row.entry_price,
        stop_loss=sig_row.stop_loss,
        take_profit=sig_row.take_profit,
        confidence=sig_row.confidence or 1.0,
        timeframe=sig_row.timeframe,
        strategy_name=sig_row.strategy_name,
        asset_class=sig_row.asset_class.value if hasattr(sig_row.asset_class, "value") else sig_row.asset_class,
        broker=sig_row.broker.value if hasattr(sig_row.broker, "value") else sig_row.broker,
        regime=sig_row.regime,
        reasons=sig_row.reasons or [],
    )

    engine = ForwardEngine()
    await engine.initialize(db)

    trade = await engine.process_signal(
        signal=signal_dc,
        execution_mode=ExecutionMode.FULL_AUTO.value,
        is_paper=is_paper,
        db_session=db,
    )

    sig_row.acted_on = True
    if trade:
        trade.signal_id = sig_row.id
    await db.commit()

    return {
        "message": "Signal approved and executed.",
        "signal_id": signal_id,
        "trade_placed": trade is not None,
    }


@router.post("/{signal_id}/reject")
async def reject_signal(signal_id: int, db: AsyncSession = Depends(get_db)):
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
async def get_signal(signal_id: int, db: AsyncSession = Depends(get_db)):
    """Get a single signal by ID."""
    result = await db.execute(select(Signal).where(Signal.id == signal_id))
    signal = result.scalar_one_or_none()
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found")
    return _signal_dict(signal)
