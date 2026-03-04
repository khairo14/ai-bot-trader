"""Forward Test API — paper trading control, status and trade history."""
import csv
import io
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from loguru import logger

from db.database import get_db
from db.models import (
    Strategy as StrategyModel,
    Trade,
    Signal as SignalModel,
    OrderStatus,
    ExecutionMode,
)

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _get_paper_stats(db: AsyncSession) -> dict:
    """Compute aggregated paper trading statistics from the DB."""
    INITIAL_CAPITAL = 10_000.0

    # Sum closed paper trade P&L
    closed = await db.execute(
        select(Trade).where(Trade.is_paper == True, Trade.status == OrderStatus.FILLED)
    )
    closed_trades = closed.scalars().all()
    realized_pnl = sum(t.pnl or 0.0 for t in closed_trades)

    # Open paper trades
    open_q = await db.execute(
        select(Trade).where(Trade.is_paper == True, Trade.status == OrderStatus.OPEN)
    )
    open_trades = open_q.scalars().all()
    open_count = len(open_trades)
    unrealized_pnl = sum(t.pnl or 0.0 for t in open_trades)

    # Active paper strategies
    strat_q = await db.execute(
        select(StrategyModel).where(
            StrategyModel.is_active == True,
            StrategyModel.is_paper == True,
        )
    )
    active_strategies = strat_q.scalars().all()

    # Days running — from earliest trade
    first_q = await db.execute(
        select(func.min(Trade.opened_at)).where(Trade.is_paper == True)
    )
    first_opened: Optional[datetime] = first_q.scalar_one_or_none()
    days_running = 0
    if first_opened:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        days_running = max(0, (now - first_opened).days)

    return {
        "active_strategies": len(active_strategies),
        "strategy_names": [s.name for s in active_strategies],
        "paper_balance": round(INITIAL_CAPITAL + realized_pnl + unrealized_pnl, 2),
        "initial_capital": INITIAL_CAPITAL,
        "open_positions": open_count,
        "realized_pnl": round(realized_pnl, 4),
        "unrealized_pnl": round(unrealized_pnl, 4),
        "total_pnl": round(realized_pnl + unrealized_pnl, 4),
        "total_closed_trades": len(closed_trades),
        "days_running": days_running,
        "is_running": len(active_strategies) > 0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/status")
async def get_status(db: AsyncSession = Depends(get_db)):
    """Aggregate paper trading stats."""
    return await _get_paper_stats(db)


@router.get("/trades")
async def list_paper_trades(
    limit: int = 50,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """List paper trades, newest first."""
    q = (
        select(Trade)
        .where(Trade.is_paper == True)
        .order_by(desc(Trade.opened_at))
        .limit(limit)
    )
    if status:
        try:
            q = q.where(Trade.status == OrderStatus(status))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    result = await db.execute(q)
    trades = result.scalars().all()

    return {
        "trades": [
            {
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
                "status": t.status,
                "execution_mode": t.execution_mode,
                "broker": t.broker,
                "asset_class": t.asset_class,
                "strategy_name": t.strategy_name,
                "broker_order_id": t.broker_order_id,
                "opened_at": t.opened_at.isoformat() if t.opened_at else None,
                "closed_at": t.closed_at.isoformat() if t.closed_at else None,
            }
            for t in trades
        ]
    }


@router.get("/trades/export")
async def export_paper_trades(db: AsyncSession = Depends(get_db)):
    """Download all paper trades as CSV."""
    result = await db.execute(
        select(Trade)
        .where(Trade.is_paper == True)
        .order_by(desc(Trade.opened_at))
    )
    trades = result.scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "symbol", "side", "quantity", "entry_price", "exit_price",
        "stop_loss", "take_profit", "pnl", "pnl_pct", "status",
        "broker", "strategy_name", "opened_at", "closed_at",
    ])
    for t in trades:
        writer.writerow([
            t.id, t.symbol, t.side, t.quantity, t.entry_price, t.exit_price,
            t.stop_loss, t.take_profit, t.pnl, t.pnl_pct, t.status,
            t.broker, t.strategy_name,
            t.opened_at.isoformat() if t.opened_at else "",
            t.closed_at.isoformat() if t.closed_at else "",
        ])
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=paper_trades.csv"},
    )


@router.post("/run")
async def trigger_run(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    Manually trigger the signal engine for all active paper strategies right now.
    Runs in background so the request returns immediately.
    """
    strat_q = await db.execute(
        select(StrategyModel).where(
            StrategyModel.is_active == True,
            StrategyModel.is_paper == True,
        )
    )
    active = strat_q.scalars().all()
    if not active:
        raise HTTPException(
            status_code=400,
            detail="No active paper strategies. Activate a strategy in paper mode first.",
        )

    background_tasks.add_task(_run_signals_background)
    return {
        "status": "triggered",
        "strategies": len(active),
        "message": f"Signal run triggered for {len(active)} paper strategie(s).",
    }


async def _run_signals_background():
    """Run signal engine + forward engine for all active paper strategies."""
    from core.engine.signal_engine import SignalEngine
    from core.engine.forward_engine import ForwardEngine
    from db.database import AsyncSessionLocal
    from db.models import (
        SignalType, AssetClass, BrokerName,
    )
    from api.websocket import manager
    from sqlalchemy import select as sa_select

    signal_engine = SignalEngine()
    forward_engine = ForwardEngine()

    async with AsyncSessionLocal() as session:
        strat_q = await session.execute(
            sa_select(StrategyModel).where(
                StrategyModel.is_active == True,
                StrategyModel.is_paper == True,
            )
        )
        strategies = strat_q.scalars().all()

        for strat in strategies:
            params = strat.parameters or {}
            strategy_type = params.get("strategy_type") or params.get("strategy_name")
            symbol = params.get("symbol")
            timeframe = params.get("timeframe", "1h")
            limit = int(params.get("limit", 200))

            if not strategy_type or not symbol:
                logger.warning(
                    f"[ForwardTest] Strategy '{strat.name}' missing strategy_type/symbol — skip"
                )
                continue

            try:
                sig = await signal_engine.run(
                    strategy_name=strategy_type,
                    symbol=symbol,
                    broker_name=strat.broker.value,
                    timeframe=timeframe,
                    limit=limit,
                )

                # Persist signal
                try:
                    sig_type = SignalType(sig.signal)
                except ValueError:
                    sig_type = SignalType.HOLD

                try:
                    asset_cls = AssetClass(sig.asset_class)
                except ValueError:
                    asset_cls = strat.asset_class

                db_signal = SignalModel(
                    symbol=sig.symbol,
                    signal=sig_type,
                    entry_price=sig.entry_price,
                    stop_loss=sig.stop_loss,
                    take_profit=sig.take_profit,
                    confidence=sig.confidence,
                    timeframe=sig.timeframe,
                    strategy_name=sig.strategy_name,
                    regime=sig.regime,
                    asset_class=asset_cls,
                    broker=strat.broker,
                    reasons=sig.reasons,
                    acted_on=False,
                )
                session.add(db_signal)
                await session.flush()

                # Pipe through ForwardEngine
                trade = await forward_engine.process_signal(
                    signal=sig,
                    execution_mode=strat.execution_mode.value,
                    is_paper=True,
                    db_session=session,
                )

                if trade is not None:
                    trade.signal_id = db_signal.id
                    db_signal.acted_on = True

                await session.commit()

                # Broadcast via WebSocket
                await manager.broadcast("signal", {
                    "symbol": sig.symbol,
                    "signal": sig.signal,
                    "entry_price": sig.entry_price,
                    "confidence": sig.confidence,
                    "strategy": sig.strategy_name,
                    "timeframe": sig.timeframe,
                    "acted_on": db_signal.acted_on,
                })

                if trade is not None:
                    await manager.broadcast("trade", {
                        "symbol": trade.symbol,
                        "side": trade.side,
                        "quantity": trade.quantity,
                        "entry_price": trade.entry_price,
                        "broker": trade.broker.value if hasattr(trade.broker, "value") else trade.broker,
                        "strategy_name": trade.strategy_name,
                        "is_paper": True,
                    })

                logger.info(
                    f"[ForwardTest] ✓ {strat.name} | {symbol} → {sig.signal} "
                    f"@ {sig.entry_price} (conf={sig.confidence:.2f})"
                )

            except Exception as e:
                await session.rollback()
                logger.error(f"[ForwardTest] ✗ '{strat.name}': {e}", exc_info=True)


@router.post("/emergency-stop")
async def emergency_stop(db: AsyncSession = Depends(get_db)):
    """
    Close all open paper trades immediately (mark as FILLED with note).
    """
    q = await db.execute(
        select(Trade).where(Trade.is_paper == True, Trade.status == OrderStatus.OPEN)
    )
    open_trades = q.scalars().all()

    now = datetime.utcnow()
    for t in open_trades:
        t.status = OrderStatus.FILLED
        t.closed_at = now
        t.pnl = 0.0           # Closed flat — no price data available
        t.pnl_pct = 0.0

    await db.commit()

    # Deactivate all paper strategies
    strat_q = await db.execute(
        select(StrategyModel).where(
            StrategyModel.is_active == True,
            StrategyModel.is_paper == True,
        )
    )
    for s in strat_q.scalars().all():
        s.is_active = False

    await db.commit()

    from api.websocket import manager
    await manager.broadcast("emergency_stop", {"closed_trades": len(open_trades)})

    return {
        "status": "emergency_stop_executed",
        "closed_trades": len(open_trades),
        "message": f"Closed {len(open_trades)} open paper trade(s) and deactivated all paper strategies.",
    }
