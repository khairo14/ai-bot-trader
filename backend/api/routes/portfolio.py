import asyncio
from datetime import date, datetime

from fastapi import APIRouter, Depends
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from db.database import get_db
from db.models import Trade, OrderStatus
from config import settings

router = APIRouter()

# ── IBKR cooldown: after a failure, skip retries for 60 s to stop log spam ──
_ibkr_last_fail: float = 0.0
_IBKR_COOLDOWN = 60.0  # seconds


async def _safe_balance(broker_name: str) -> dict:
    """Fetch broker balance with timeout; never raises."""
    global _ibkr_last_fail
    import time
    try:
        from brokers import get_broker, get_broker_modes
        if broker_name == "ibkr":
            # Skip immediately if IBKR failed recently — stops connection spam
            if time.monotonic() - _ibkr_last_fail < _IBKR_COOLDOWN:
                return {"broker": "ibkr", "total": 0.0, "available": 0.0,
                        "currency": "USD", "connected": False, "is_paper": True}
            from brokers.ibkr_client import ibkr_balance_sync
            import concurrent.futures
            _pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            loop = asyncio.get_event_loop()
            balance = await asyncio.wait_for(
                loop.run_in_executor(_pool, ibkr_balance_sync), timeout=10.0
            )
        else:
            broker = get_broker(broker_name)
            balance = await asyncio.wait_for(broker.get_balance(), timeout=8.0)

        # Use live runtime mode (respects toggles without needing a restart)
        is_paper = get_broker_modes().get(broker_name, "paper") == "paper"

        return {
            "broker": broker_name,
            "total": round(balance.total, 2),
            "available": round(balance.available, 2),
            "currency": balance.currency,
            "connected": True,
            "is_paper": is_paper,
        }
    except Exception as e:
        if broker_name == "ibkr":
            _ibkr_last_fail = time.monotonic()
        logger.warning(f"[portfolio] {broker_name} balance failed: {type(e).__name__}: {e}")
        return {
            "broker": broker_name,
            "total": 0.0,
            "available": 0.0,
            "currency": "USD",
            "connected": False,
            "is_paper": True,
        }


@router.get("/summary")
async def portfolio_summary(db: AsyncSession = Depends(get_db)):
    """
    Single call that returns:
    - Live balance from every connected broker (parallel, with timeout)
    - Open position count from DB
    - Today's realised P&L from DB
    """
    # Fetch all broker balances in parallel; IBKR requires Gateway so it will
    # simply return connected=False when not running.
    binance_task, alpaca_task, ibkr_task = await asyncio.gather(
        _safe_balance("binance"),
        _safe_balance("alpaca"),
        _safe_balance("ibkr"),
        return_exceptions=True,
    )

    broker_balances = []
    for b in [binance_task, alpaca_task, ibkr_task]:
        if isinstance(b, Exception):
            continue
        broker_balances.append(b)

    # Open positions count
    open_result = await db.execute(
        select(func.count(Trade.id)).where(Trade.status == OrderStatus.OPEN)
    )
    open_count = int(open_result.scalar() or 0)

    # Today's realised P&L (all trades closed since midnight UTC)
    today_start = datetime.combine(date.today(), datetime.min.time())
    pnl_result = await db.execute(
        select(func.coalesce(func.sum(Trade.pnl), 0.0))
        .where(Trade.status == OrderStatus.FILLED)
        .where(Trade.closed_at >= today_start)
    )
    today_pnl = round(float(pnl_result.scalar() or 0.0), 2)

    # Max open positions from config
    max_positions = settings.max_open_positions

    return {
        "brokers": broker_balances,
        "open_positions": open_count,
        "max_positions": max_positions,
        "today_pnl": today_pnl,
        "circuit_breaker_pct": settings.daily_circuit_breaker_pct,
    }
