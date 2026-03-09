from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, update, or_, and_
from typing import Optional
from datetime import datetime, timedelta, timezone

from db.database import get_db
from db.models import Signal, Strategy, ExecutionMode, SignalType, BrokerName
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
    limit: int = Query(default=50, le=500),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get recent signals. Admins see all; regular users see system signals + their own (F-059)."""
    query = (
        select(Signal)
        .where(Signal.dismissed == False)  # noqa: E712
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
    result = await db.execute(query)
    return {"signals": [_signal_dict(s) for s in result.scalars().all()]}


@router.post("/dismiss-expired")
async def dismiss_expired_signals(
    older_than_hours: int = Query(default=24, ge=1, le=168),
    db: AsyncSession = Depends(get_db),
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
async def list_pending_signals(db: AsyncSession = Depends(get_db)):
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
async def approve_signal(signal_id: int, db: AsyncSession = Depends(get_db)):
    """
    Approve a pending semi-auto signal and execute it immediately as full-auto.
    """
    sig_row = (await db.execute(select(Signal).where(Signal.id == signal_id))).scalar_one_or_none()
    if not sig_row:
        raise HTTPException(status_code=404, detail="Signal not found")
    if sig_row.acted_on:
        raise HTTPException(status_code=400, detail="Signal already acted on")

    # Look up the originating strategy to get is_paper (needed for market-hours gate below)
    strat_row = (await db.execute(
        select(Strategy).where(
            Strategy.name == sig_row.strategy_name,
            Strategy.broker == sig_row.broker,
        ).limit(1)
    )).scalar_one_or_none()
    is_paper = strat_row.is_paper if strat_row else True

    # F-085: Market-hours gate — only block LIVE signal execution outside trading hours.
    # Paper signals are pure simulation and may be approved at any time.
    from api.routes.forward_test import is_market_open
    broker_name = sig_row.broker.value if hasattr(sig_row.broker, "value") else str(sig_row.broker)
    asset_cls = sig_row.asset_class.value if hasattr(sig_row.asset_class, "value") else str(sig_row.asset_class)
    if not is_paper and not is_market_open(broker_name, asset_cls):
        raise HTTPException(
            status_code=400,
            detail=f"Market is currently closed for {broker_name}/{asset_cls}. Signal approval blocked outside trading hours.",
        )

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
        # Restore options fields so ForwardEngine can pass IBKR option kwargs
        iv_rank=sig_row.iv_rank,
        delta=sig_row.delta,
        theta=sig_row.theta,
        vega=sig_row.vega,
        options_meta=sig_row.options_meta,
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
    from core.auth import audit
    audit("signal.approve", signal_id=signal_id, symbol=sig_row.symbol, trade_placed=trade is not None)

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
