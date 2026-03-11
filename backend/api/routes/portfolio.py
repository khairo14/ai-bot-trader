import asyncio
import concurrent.futures
from datetime import date, datetime

from fastapi import APIRouter, Depends
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from db.database import get_db
from db.models import Trade, LiveTrade, OrderStatus, BrokerName
from config import settings
from core.risk_manager import RiskManager as _RiskManager, get_risk_manager as _get_risk_manager  # BUG-2 FIX

# Singleton — reads risk_state.json the same way ForwardEngine does.
# _load_state() is called inside each request to refresh from disk.
_risk_mgr = _get_risk_manager()  # BUG-2 FIX: process-wide singleton

router = APIRouter()

# Module-level thread pool for IBKR sync calls — created once, reused across requests.
# Creating a new ThreadPoolExecutor per call leaks threads until GC collects them.
_ibkr_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

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
            loop = asyncio.get_running_loop()
            balance = await asyncio.wait_for(
                loop.run_in_executor(_ibkr_pool, ibkr_balance_sync), timeout=10.0
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
    open_result_paper = await db.execute(
        select(func.count(Trade.id)).where(Trade.status == OrderStatus.OPEN)
    )
    open_result_live = await db.execute(
        select(func.count(LiveTrade.id)).where(LiveTrade.status == OrderStatus.OPEN)
    )
    open_count = int(open_result_paper.scalar() or 0) + int(open_result_live.scalar() or 0)

    # Today's realised P&L (all trades closed since midnight UTC)
    from datetime import timezone as _tz_mod
    today_start = datetime.combine(
        datetime.now(_tz_mod.utc).date(),
        datetime.min.time(),
    )
    pnl_paper = await db.execute(
        select(func.coalesce(func.sum(Trade.pnl), 0.0))
        .where(Trade.status == OrderStatus.FILLED)
        .where(Trade.closed_at >= today_start)
    )
    pnl_live = await db.execute(
        select(func.coalesce(func.sum(LiveTrade.pnl), 0.0))
        .where(LiveTrade.status == OrderStatus.FILLED)
        .where(LiveTrade.closed_at >= today_start)
    )
    today_pnl = round(float(pnl_paper.scalar() or 0.0) + float(pnl_live.scalar() or 0.0), 2)

    # Per-broker daily P&L + per-broker open position count (same today window)
    today_pnl_by_broker: dict[str, float] = {}
    open_positions_by_broker: dict[str, int] = {}
    for _broker in [BrokerName.BINANCE, BrokerName.ALPACA, BrokerName.IBKR]:
        _pnl_p = await db.execute(
            select(func.coalesce(func.sum(Trade.pnl), 0.0))
            .where(Trade.status == OrderStatus.FILLED)
            .where(Trade.closed_at >= today_start)
            .where(Trade.broker == _broker)
        )
        _pnl_l = await db.execute(
            select(func.coalesce(func.sum(LiveTrade.pnl), 0.0))
            .where(LiveTrade.status == OrderStatus.FILLED)
            .where(LiveTrade.closed_at >= today_start)
            .where(LiveTrade.broker == _broker)
        )
        today_pnl_by_broker[_broker.value] = round(float(_pnl_p.scalar() or 0.0) + float(_pnl_l.scalar() or 0.0), 2)

        _cnt_p = await db.execute(
            select(func.count(Trade.id))
            .where(Trade.status == OrderStatus.OPEN)
            .where(Trade.broker == _broker)
        )
        _cnt_l = await db.execute(
            select(func.count(LiveTrade.id))
            .where(LiveTrade.status == OrderStatus.OPEN)
            .where(LiveTrade.broker == _broker)
        )
        _cnt = int((_cnt_p.scalar() or 0) + (_cnt_l.scalar() or 0))
        if _cnt > 0:
            open_positions_by_broker[_broker.value] = _cnt

    # Max open positions from config
    max_positions = settings.max_open_positions

    # Circuit breaker state — reload from disk so the latest tripped/reset state
    # is reflected without needing a server restart.
    _risk_mgr._load_state()
    cb_active = _risk_mgr.is_circuit_breaker_active()
    per_broker_cb: dict[str, dict] = {}
    for _b in ["binance", "alpaca", "ibkr"]:
        _bs = _risk_mgr._per_broker.get(_b, {})
        per_broker_cb[_b] = {
            "active": _bs.get("circuit_breaker_active", False),
            "consecutive_losses": _bs.get("consecutive_losses", 0),
        }

    return {
        "brokers": broker_balances,
        "open_positions": open_count,
        "open_positions_by_broker": open_positions_by_broker,
        "max_positions": max_positions,
        "today_pnl": today_pnl,
        "today_pnl_by_broker": today_pnl_by_broker,
        "circuit_breaker_pct": settings.daily_circuit_breaker_pct,
        "circuit_breaker_active": cb_active,
        "per_broker_circuit_breaker": per_broker_cb,
    }
