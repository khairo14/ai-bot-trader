"""Forward Test API — paper trading control, status and trade history."""
import asyncio as _asyncio
import csv
import hashlib as _hashlib
import io
import math as _math
import time as _time_module
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, func
from loguru import logger

from db.database import get_db
from db.models import (
    Strategy as StrategyModel,
    Trade,
    LiveTrade,
    Signal as SignalModel,
    OrderStatus,
    ExecutionMode,
)
from config import settings as _settings
from core.auth import get_current_user as _get_current_user, require_admin as _require_admin

router = APIRouter()

# ── Execution-state guard (prevents concurrent Run-Now / Scheduler overlap) ──
_exec_lock: "_asyncio.Lock | None" = None
_exec_state: dict = {
    "active": False,
    "strategy": None,   # strategy name currently being processed
    "trigger": None,    # "manual" | "scheduler"
    "started_at": None, # datetime UTC
}


def _get_exec_lock() -> "_asyncio.Lock":
    """Return (or lazily create) the module-level execution lock."""
    global _exec_lock
    if _exec_lock is None:
        _exec_lock = _asyncio.Lock()
    return _exec_lock


# Per-strategy execution locks (F-006) — each strategy has its own lock so
# a slow IBKR call on strategy A doesn't block strategies B, C, D.
_strategy_locks: dict[int, "_asyncio.Lock"] = {}


def _get_strategy_lock(strategy_id: int) -> "_asyncio.Lock":
    """Return (or lazily create) a per-strategy execution lock."""
    if strategy_id not in _strategy_locks:
        _strategy_locks[strategy_id] = _asyncio.Lock()
    return _strategy_locks[strategy_id]


# ─────────────────────────────────────────────────────────────────────────────
# Timeframe helpers
# ─────────────────────────────────────────────────────────────────────────────
from utils import TIMEFRAME_SECONDS as _TF_SECONDS, timeframe_to_seconds  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Market hours helpers
# ─────────────────────────────────────────────────────────────────────────────

from datetime import datetime as _dt, time as _time, date as _date
from zoneinfo import ZoneInfo as _ZI
import pandas as _pd

_ET             = _ZI("America/New_York")
_STOCK_BROKERS  = {"alpaca", "ibkr"}      # regulated session brokers
_MARKET_OPEN    = _time(9, 30)            # NYSE regular session
_MARKET_CLOSE   = _time(16, 0)

# NYSE calendar — covers all US market holidays (MLK Day, July 4th, Christmas, etc.)
# Loaded once at import time; covers dates far into the future.
try:
    import pandas_market_calendars as _pmc
    _NYSE_CAL = _pmc.get_calendar("NYSE")
    _USE_HOLIDAY_CAL = True
except Exception:
    _USE_HOLIDAY_CAL = False
    logger.warning("[Scheduler] pandas_market_calendars not available — holiday blocking disabled.")


def _is_nyse_trading_day(d: _date) -> bool:
    """Return True if `d` is a day the NYSE is open (excludes weekends + all US holidays)."""
    if not _USE_HOLIDAY_CAL:
        return d.weekday() < 5  # fallback: weekday only
    sched = _NYSE_CAL.schedule(
        start_date=d.strftime("%Y-%m-%d"),
        end_date=d.strftime("%Y-%m-%d"),
    )
    return not sched.empty


_FX_MARKET_CLOSE = _time(17, 0)  # 5 PM ET — same for open and close


def _is_forex_market_open() -> bool:
    """
    FX spot market hours: Sunday 17:00 ET → Friday 17:00 ET (continuous 24 h, DST-aware).
    Closed all day Saturday and Friday from 17:00 ET until Sunday 17:00 ET.
    """
    now_et = _dt.now(tz=_ET)
    wd = now_et.weekday()  # 0=Mon … 4=Fri, 5=Sat, 6=Sun
    if wd == 5:  # Saturday — fully closed
        return False
    if wd == 4 and now_et.time() >= _FX_MARKET_CLOSE:  # Friday ≥ 17:00
        return False
    if wd == 6 and now_et.time() < _FX_MARKET_CLOSE:   # Sunday < 17:00
        return False
    return True


def is_crypto_broker(broker_name: str) -> bool:
    """Return True for 24/7 crypto brokers (Binance, etc.)."""
    return broker_name.lower() not in _STOCK_BROKERS


def is_market_open(broker_name: str, asset_class: str | None = None) -> bool:
    """
    Return True if trading is currently allowed for this broker / asset-class combo.

    - Crypto (Binance): always True — trades 24/7
    - Forex on IBKR:  FX market hours (Sun 17:00 ET → Fri 17:00 ET, continuous)
    - Stocks / Options (Alpaca, IBKR):
        * NYSE regular session 09:30–16:00 ET only
        * Blocked on weekends
        * Blocked on all US market holidays via pandas_market_calendars NYSE calendar
        * DST handled automatically via ZoneInfo / America/New_York
    """
    if is_crypto_broker(broker_name):
        return True
    # IBKR forex uses FX session hours, not NYSE stock hours
    if broker_name.lower() == "ibkr" and str(asset_class or "").upper() == "FOREX":
        return _is_forex_market_open()
    now_et = _dt.now(tz=_ET)
    if not _is_nyse_trading_day(now_et.date()):
        return False
    return _MARKET_OPEN <= now_et.time() < _MARKET_CLOSE  # GAP-1 FIX: strict < so 16:00:00 is closed


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _get_paper_stats(db: AsyncSession) -> dict:
    """Compute aggregated paper trading statistics from the DB."""

    # Active paper strategies
    strat_q = await db.execute(
        select(StrategyModel).where(
            StrategyModel.is_active == True,
            StrategyModel.is_paper == True,
        )
    )
    active_strategies = strat_q.scalars().all()
    active_brokers = sorted({s.broker.value for s in active_strategies})

    # Real broker API balances — capped at 5 s total so a slow IBKR connection
    # never delays the entire status response and makes Alpaca/Binance look offline.
    from api.routes.portfolio import _safe_balance
    _bal_tasks = {
        "binance": _asyncio.create_task(_safe_balance("binance")),
        "alpaca":  _asyncio.create_task(_safe_balance("alpaca")),
        "ibkr":    _asyncio.create_task(_safe_balance("ibkr")),
    }
    _done, _pending = await _asyncio.wait(list(_bal_tasks.values()), timeout=5.0)
    for _t in _pending:
        _t.cancel()   # IBKR timed out — leave as disconnected
    api_balances: dict = {}
    for _bname, _task in _bal_tasks.items():
        if _task in _done:
            try:
                _result = _task.result()
                if isinstance(_result, dict):
                    api_balances[_result.get("broker", _bname)] = _result
            except Exception:
                pass

    # Bug-10 FIX: use SQL SUM() so the realized P&L is always exact regardless
    # of how many closed trades exist (previously capped at 500 rows in Python).
    closed_pnl_q = await db.execute(
        select(func.coalesce(func.sum(Trade.pnl), 0.0)).where(
            Trade.is_paper == True, Trade.status == OrderStatus.FILLED
        )
    )
    _realized_pnl_total: float = float(closed_pnl_q.scalar_one() or 0.0)

    # For per-broker breakdown we still need the trade list, but only open trades
    # are needed for unrealized P&L; closed P&L comes from the SQL sum above.
    # Keep the 500-row limit for open trades (there should never be that many).
    closed = await db.execute(
        select(Trade).where(Trade.is_paper == True, Trade.status == OrderStatus.FILLED)
        .order_by(desc(Trade.closed_at)).limit(500)
    )
    closed_trades = closed.scalars().all()

    # Open paper trades
    open_q = await db.execute(
        select(Trade).where(Trade.is_paper == True, Trade.status == OrderStatus.OPEN)
        .limit(500)
    )
    open_trades = open_q.scalars().all()

    # ── All 3 brokers always present ─────────────────────────────────────────
    broker_breakdown = []
    for b in ["binance", "alpaca", "ibkr"]:
        api          = api_balances.get(b, {})
        b_closed_pnl = sum(t.pnl or 0.0 for t in closed_trades if t.broker.value == b)
        b_open_pnl   = sum(t.pnl or 0.0 for t in open_trades   if t.broker.value == b)
        b_open_count = sum(1              for t in open_trades   if t.broker.value == b)
        b_strats     = [s.name for s in active_strategies if s.broker.value == b]
        broker_breakdown.append({
            "broker":         b,
            "connected":      api.get("connected", False),
            "is_paper":       api.get("is_paper", True),
            "total":          api.get("total", 0.0),
            "available":      api.get("available", 0.0),
            "currency":       api.get("currency", "USD"),
            "pnl":            round(b_closed_pnl + b_open_pnl, 4),
            "open_positions": b_open_count,
            "strategies":     b_strats,
            "is_active":      b in active_brokers,
        })

    # ── Aggregates ────────────────────────────────────────────────────────────
    realized_pnl   = _realized_pnl_total  # Bug-10 FIX: exact SQL sum, not truncated Python sum
    unrealized_pnl = sum(t.pnl or 0.0 for t in open_trades)
    open_count     = len(open_trades)
    # Paper balance = ledger-based P&L accounting.
    # Calculated from initial capital + all closed P&L + any open-trade unrealised P&L.
    # This is stable and does not fluctuate with position cost the way broker.get_balance()
    # does on Binance testnet (where USDT drops by full position cost while crypto is held,
    # and the non-USDT ticker fetch can time out, making the balance look wrong).
    paper_balance = _settings.paper_initial_balance + realized_pnl + unrealized_pnl
    # Also retain the real broker API balance (for the broker_breakdown tiles).
    total_balance = (
        sum(bd["total"] for bd in broker_breakdown if bd["connected"] and bd["is_active"])
        or sum(bd["total"] for bd in broker_breakdown if bd["connected"])
        or _settings.paper_initial_balance
    )

    # Days running — from earliest paper trade
    first_q = await db.execute(
        select(func.min(Trade.opened_at)).where(Trade.is_paper == True)
    )
    first_opened: Optional[datetime] = first_q.scalar_one_or_none()
    days_running = 0
    if first_opened:
        # Strip tz safely — first_opened may be tz-aware or tz-naive depending on DB driver
        if hasattr(first_opened, 'tzinfo') and first_opened.tzinfo is not None:
            first_opened = first_opened.replace(tzinfo=None)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        days_running = max(0, (now - first_opened).days)

    # Next scheduled fire per strategy
    now_ts = _time_module.time()
    schedule_details = []
    for strat in active_strategies:
        params = strat.parameters or {}
        tf = params.get("timeframe", "1h")
        interval = timeframe_to_seconds(tf)
        last_close = _math.floor(now_ts / interval) * interval
        next_close_ts = last_close + interval
        next_fire_ts = next_close_ts + 30  # 30-second candle-close buffer
        schedule_details.append({
            "strategy": strat.name,
            "timeframe": tf,
            "next_fire": datetime.fromtimestamp(next_fire_ts, tz=timezone.utc).isoformat(),
        })
    next_scheduled = min(schedule_details, key=lambda x: x["next_fire"]) if schedule_details else None

    return {
        "active_strategies": len(active_strategies),
        "strategy_names": [s.name for s in active_strategies],
        "brokers": active_brokers,
        "broker_breakdown": broker_breakdown,
        "paper_balance": round(paper_balance, 2),
        "broker_balance": round(total_balance, 2),
        "initial_capital": round(_settings.paper_initial_balance, 2),
        "open_positions": open_count,
        "realized_pnl": round(realized_pnl, 4),
        "unrealized_pnl": round(unrealized_pnl, 4),
        "total_pnl": round(realized_pnl + unrealized_pnl, 4),
        "total_closed_trades": len(closed_trades),
        "days_running": days_running,
        "is_running": len(active_strategies) > 0,
        # Execution state
        "is_executing": _exec_state["active"],
        "executing_strategy": _exec_state["strategy"],
        "executing_trigger": _exec_state["trigger"],
        "execution_started_at": _exec_state["started_at"].isoformat() if _exec_state["started_at"] else None,
        # Scheduler
        "next_scheduled": next_scheduled,
        "schedule_details": schedule_details,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/market-status")
async def get_market_status():
    """
    Return current server time plus open/closed status for each broker.
    Polled by the frontend clock widget (~every 60 s).
    """
    now_utc = _dt.now(tz=_ZI("UTC"))
    now_et  = _dt.now(tz=_ET)

    def _next_nyse_open(from_et: _dt) -> _dt:
        """Return the next NYSE open datetime (ET) from the given moment."""
        candidate = from_et.replace(hour=9, minute=30, second=0, microsecond=0)
        # If we haven't passed today's open yet AND today is a trading day, use today
        if candidate > from_et and _is_nyse_trading_day(candidate.date()):
            return candidate
        # Otherwise advance day by day
        candidate = candidate + _pd.Timedelta(days=1)
        for _ in range(10):
            if _is_nyse_trading_day(candidate.date()):
                return candidate
            candidate += _pd.Timedelta(days=1)
        return candidate

    def _nyse_close_today(from_et: _dt) -> _dt:
        return from_et.replace(hour=16, minute=0, second=0, microsecond=0)

    sessions = []
    for broker in ["binance", "alpaca", "ibkr"]:
        if is_crypto_broker(broker):
            sessions.append({
                "broker": broker,
                "open": True,
                "label": "24 / 7",
                "next_event": None,
                "next_event_label": None,
            })
        else:
            open_now = is_market_open(broker)
            if open_now:
                close_et = _nyse_close_today(now_et)
                mins_left = int((close_et - now_et).total_seconds() / 60)
                sessions.append({
                    "broker": broker,
                    "open": True,
                    "label": "Open",
                    "next_event": close_et.strftime("%H:%M ET"),
                    "next_event_label": f"Closes in {mins_left} min" if mins_left < 120 else f"Closes {close_et.strftime('%H:%M ET')}",
                })
            else:
                next_open = _next_nyse_open(now_et)
                # If next open is today
                if next_open.date() == now_et.date():
                    mins_away = int((next_open - now_et).total_seconds() / 60)
                    event_label = f"Opens in {mins_away} min"
                else:
                    event_label = f"Opens {next_open.strftime('%a %H:%M ET')}"
                sessions.append({
                    "broker": broker,
                    "open": False,
                    "label": "Closed",
                    "next_event": next_open.strftime("%H:%M ET"),
                    "next_event_label": event_label,
                })

    return {
        "server_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "et_offset": now_et.strftime("%z"),  # e.g. "-0500" or "-0400"
        "sessions": sessions,
    }


@router.get("/status")
async def get_status(db: AsyncSession = Depends(get_db), _user=Depends(_get_current_user)):
    """Aggregate paper trading stats."""
    return await _get_paper_stats(db)


@router.get("/trades")
async def list_paper_trades(
    limit: int = 50,
    status: Optional[str] = None,
    mode: str = "paper",
    db: AsyncSession = Depends(get_db),
    _user=Depends(_get_current_user),
):
    """List trades, newest first.

    ``mode`` controls which trades are returned:
    - ``paper`` (default) – paper trades only (``is_paper == True``)
    - ``live``  – live/real trades only (``is_paper == False``)
    - ``all``   – both paper and live
    """
    q = select(Trade).where(Trade.is_paper == True).order_by(desc(Trade.opened_at)).limit(limit)

    if mode == "paper":
        pass  # Trade table is paper-only
    elif mode == "live":
        # Live trades are in a separate table
        q_live = select(LiveTrade).order_by(desc(LiveTrade.opened_at)).limit(limit)
        if status:
            try:
                q_live = q_live.where(LiveTrade.status == OrderStatus(status))
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
        live_result = await db.execute(q_live)
        trades = live_result.scalars().all()
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
                    "status": t.status.value if hasattr(t.status, "value") else t.status,
                    "execution_mode": t.execution_mode.value if hasattr(t.execution_mode, "value") else t.execution_mode,
                    "broker": t.broker.value if hasattr(t.broker, "value") else t.broker,
                    "asset_class": t.asset_class.value if hasattr(t.asset_class, "value") else t.asset_class,
                    "strategy_name": t.strategy_name,
                    "broker_order_id": t.broker_order_id,
                    "is_paper": t.is_paper,
                    "opened_at": t.opened_at.isoformat() if t.opened_at else None,
                    "closed_at": t.closed_at.isoformat() if t.closed_at else None,
                }
                for t in trades
            ]
        }
    elif mode == "all":
        # Combine paper (Trade) + live (LiveTrade)
        paper_result = await db.execute(select(Trade).order_by(desc(Trade.opened_at)).limit(limit))
        live_result  = await db.execute(select(LiveTrade).order_by(desc(LiveTrade.opened_at)).limit(limit))
        all_trades = sorted(
            paper_result.scalars().all() + live_result.scalars().all(),
            key=lambda t: t.opened_at or datetime.min,
            reverse=True,
        )[:limit]
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
                    "status": t.status.value if hasattr(t.status, "value") else t.status,
                    "execution_mode": t.execution_mode.value if hasattr(t.execution_mode, "value") else t.execution_mode,
                    "broker": t.broker.value if hasattr(t.broker, "value") else t.broker,
                    "asset_class": t.asset_class.value if hasattr(t.asset_class, "value") else t.asset_class,
                    "strategy_name": t.strategy_name,
                    "broker_order_id": t.broker_order_id,
                    "is_paper": t.is_paper,
                    "opened_at": t.opened_at.isoformat() if t.opened_at else None,
                    "closed_at": t.closed_at.isoformat() if t.closed_at else None,
                }
                for t in all_trades
            ]
        }

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
                "status": t.status.value if hasattr(t.status, "value") else t.status,
                "execution_mode": t.execution_mode.value if hasattr(t.execution_mode, "value") else t.execution_mode,
                "broker": t.broker.value if hasattr(t.broker, "value") else t.broker,
                "asset_class": t.asset_class.value if hasattr(t.asset_class, "value") else t.asset_class,
                "strategy_name": t.strategy_name,
                "broker_order_id": t.broker_order_id,
                "is_paper": t.is_paper,
                "opened_at": t.opened_at.isoformat() if t.opened_at else None,
                "closed_at": t.closed_at.isoformat() if t.closed_at else None,
            }
            for t in trades
        ]
    }


@router.get("/trades/export")
async def export_paper_trades(
    db: AsyncSession = Depends(get_db),
    _user=Depends(_get_current_user),
):
    """Download all paper trades as CSV."""
    result = await db.execute(
        select(Trade)
        .where(Trade.status == OrderStatus.FILLED)
        .order_by(desc(Trade.opened_at))
    )
    trades = result.scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "id", "symbol", "side", "quantity", "entry_price", "exit_price",
        "stop_loss", "take_profit", "pnl", "pnl_pct", "status",
        "broker", "strategy_name", "is_paper", "opened_at", "closed_at",
    ])
    for t in trades:
        writer.writerow([
            t.id, t.symbol, t.side, t.quantity,
            t.entry_price if t.entry_price is not None else "",
            t.exit_price if t.exit_price is not None else "",
            t.stop_loss if t.stop_loss is not None else "",
            t.take_profit if t.take_profit is not None else "",
            t.pnl if t.pnl is not None else "",
            t.pnl_pct if t.pnl_pct is not None else "",
            t.status.value if hasattr(t.status, "value") else t.status,
            t.broker.value if hasattr(t.broker, "value") else t.broker,
            t.strategy_name or "",
            t.is_paper,
            t.opened_at.isoformat() if t.opened_at else "",
            t.closed_at.isoformat() if t.closed_at else "",
        ])
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=paper_trades.csv"},
    )


# ── Edit open trade SL/TP ─────────────────────────────────────────────────────

class _TradePatchBody(BaseModel):
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


@router.patch("/trades/{trade_id}")
async def patch_trade(
    trade_id: int,
    body: _TradePatchBody,
    db: AsyncSession = Depends(get_db),
    _user=Depends(_get_current_user),
):
    """
    Update stop_loss and/or take_profit on an OPEN trade.
    Validates that SL/TP are logical relative to current entry_price and side.
    """
    from db.models import Trade, LiveTrade, OrderStatus
    t = None
    for model in (Trade, LiveTrade):
        q = await db.execute(select(model).where(model.id == trade_id))
        t = q.scalar_one_or_none()
        if t:
            break
    if not t:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")
    if t.status != OrderStatus.OPEN:
        raise HTTPException(status_code=400, detail=f"Trade {trade_id} is {t.status.value}, not open — cannot edit")

    entry = t.entry_price
    side = (t.side or "").lower()
    is_long = side in ("buy", "cover", "long")

    if body.stop_loss is not None:
        if body.stop_loss <= 0:
            raise HTTPException(status_code=400, detail="stop_loss must be > 0")
        if entry:
            if is_long and body.stop_loss >= entry:
                raise HTTPException(status_code=400, detail=f"BUY trade stop_loss ({body.stop_loss}) must be below entry ({entry})")
            if not is_long and body.stop_loss <= entry:
                raise HTTPException(status_code=400, detail=f"SELL trade stop_loss ({body.stop_loss}) must be above entry ({entry})")
        t.stop_loss = round(body.stop_loss, 8)

    if body.take_profit is not None:
        if body.take_profit <= 0:
            raise HTTPException(status_code=400, detail="take_profit must be > 0")
        if entry:
            if is_long and body.take_profit <= entry:
                raise HTTPException(status_code=400, detail=f"BUY trade take_profit ({body.take_profit}) must be above entry ({entry})")
            if not is_long and body.take_profit >= entry:
                raise HTTPException(status_code=400, detail=f"SELL trade take_profit ({body.take_profit}) must be below entry ({entry})")
        t.take_profit = round(body.take_profit, 8)

    await db.commit()
    logger.info(
        f"[ForwardTest] Trade {trade_id} updated — sl={t.stop_loss} tp={t.take_profit} by user"
    )

    # Push new SL to broker so its bracket/stop order reflects the new level.
    # Failure is non-fatal: software monitor_sl_tp enforces the DB value every tick.
    if body.stop_loss is not None:
        try:
            from brokers import get_broker
            _broker_name = t.broker.value if hasattr(t.broker, "value") else str(t.broker)
            _broker = get_broker(_broker_name, force_paper=t.is_paper)
            await _broker.connect()
            await _broker.update_stop_loss(t.symbol, side, float(t.quantity or 0), body.stop_loss)
        except Exception as _bk_err:
            logger.debug(f"[ForwardTest] Broker SL push failed for trade {trade_id}: {_bk_err}")

    return {"id": trade_id, "stop_loss": t.stop_loss, "take_profit": t.take_profit}


@router.post("/run")
async def trigger_run(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    _user=Depends(_get_current_user),
):
    """
    Manually trigger the signal engine for all active strategies right now.
    Runs in background so the request returns immediately.
    """
    if _exec_state["active"]:
        raise HTTPException(
            status_code=409,
            detail=f"A signal run is already in progress "
                   f"({_exec_state.get('trigger','?')} — {_exec_state.get('strategy','?')}). "
                   f"Please wait for it to finish.",
        )

    strat_q = await db.execute(
        select(StrategyModel).where(
            StrategyModel.is_active == True,
        )
    )
    active = strat_q.scalars().all()
    if not active:
        raise HTTPException(
            status_code=400,
            detail="No active strategies. Activate at least one strategy first.",
        )

    background_tasks.add_task(_run_signals_background)
    return {
        "status": "triggered",
        "strategies": len(active),
        "message": f"Signal run triggered for {len(active)} strateg{'y' if len(active)==1 else 'ies'}.",
    }


async def _run_one_strategy(strat, skip_monitor: bool = False) -> dict | None:
    """
    Run signal engine + forward engine for a single strategy.
    Opens its own DB session so it can be called independently by the scheduler.

    skip_monitor=True: skip reconcile+monitor (used by Run Now which does a
    single shared reconcile pass before spawning concurrent strategy tasks).

    Returns a result dict for tick-level summary roll-up in the scheduler.
    """
    from core.engine.signal_engine import SignalEngine
    from core.engine.forward_engine import ForwardEngine
    from db.database import AsyncSessionLocal
    from db.models import SignalType, AssetClass
    from api.websocket import manager

    params = strat.parameters or {}
    strategy_type = params.get("strategy_type") or params.get("strategy_name")
    symbol = params.get("symbol")
    timeframe = params.get("timeframe", "1h")
    limit = int(params.get("limit", 200))

    _result: dict = {
        "strategy": strat.name, "symbol": symbol or "?",
        "signal": None, "executed": False, "reason": None,
    }

    if not strategy_type or not symbol:
        logger.warning(
            f"[ForwardTest] Strategy '{strat.name}' missing strategy_type/symbol — skip"
        )
        _result["reason"] = "missing_config"
        return _result

    # Resolve asset-class string once — used in market-hours checks below
    _asset_cls_str: str | None = getattr(strat.asset_class, "value", None)

    # ── Early market-hours gate ─────────────────────────────────────────────
    # Skip signal computation when the broker session is closed to avoid
    # unnecessary broker API calls (e.g., IBKR data fetches during weekends).
    # This matches scheduler behaviour and prevents SSL-teardown timeouts from
    # blocking all subsequent strategies in a Run-Now pass.
    if not is_market_open(strat.broker.value, _asset_cls_str):
        _now_et_str = _dt.now(tz=_ET).strftime("%a %H:%M ET")
        logger.debug(
            f"[ForwardTest] {strat.name} ({timeframe}) — "
            f"market closed ({_now_et_str}), skip signal run"
        )
        _result["reason"] = "market_closed"
        return _result

    signal_engine = SignalEngine()
    forward_engine = ForwardEngine()

    try:
        sig = await signal_engine.run(
            strategy_name=strategy_type,
            symbol=symbol,
            broker_name=strat.broker.value,
            timeframe=timeframe,
            limit=limit,
            asset_class=_asset_cls_str,
        )
        # Always expose the human-readable strategy display name in signals/trades
        # so the UI and DB are consistent.  strategy_type is only used as the algo key.
        sig.strategy_name = strat.name

        logger.info(
            f"[ForwardTest] {sig.signal} {strat.name} | {symbol} "
            f"(conf={sig.confidence:.2f})"
        )
        _result["signal"] = sig.signal

        # Persist signal (including HOLD — useful for review and ML training)
        try:
            sig_type = SignalType(sig.signal)
        except ValueError:
            sig_type = SignalType.HOLD

        try:
            asset_cls = AssetClass(sig.asset_class)
        except ValueError:
            asset_cls = strat.asset_class

        # ── UI-02: Multi-timeframe confluence check ──────────────────────
        from tasks.signal_runner import (
            _confluence_score, MIN_CONFLUENCE,
            _TRACKABLE_SIGNALS, _STRATEGY_CONFLUENCE_DEFAULTS,
        )
        _strat_default = _STRATEGY_CONFLUENCE_DEFAULTS.get(strategy_type, MIN_CONFLUENCE)
        _min_conf = float(params.get("min_confluence", _strat_default))
        allow_execution = True
        conf = 1.0
        if sig.signal in _TRACKABLE_SIGNALS and _min_conf > 0.0:
            conf = await _confluence_score(
                signal_engine, strategy_type, symbol,
                strat.broker.value, timeframe, sig.signal,
            )
            if conf < _min_conf:
                allow_execution = False
                sig.reasons = (sig.reasons or []) + [
                    f"execution suppressed: low multi-TF confluence ({conf:.0%})"
                ]
                logger.info(
                    f"[ForwardTest] ⚠ Low confluence {conf:.0%} for "
                    f"{sig.signal} {symbol} on {timeframe} — not executing"
                )
                _result["reason"] = f"low_confluence:{conf:.0%}<{_min_conf:.0%}"

        # ── Market-hours gate (execution only) ─────────────────────────
        # Signals are always saved to DB — useful for review even overnight.
        # But actual trade placement is suppressed when the broker's session
        # is closed. Crypto (Binance) is 24/7 and always passes this check.
        if allow_execution and not is_market_open(strat.broker.value, _asset_cls_str):
            allow_execution = False
            _market_note = f"execution suppressed: {strat.broker.value} session closed"
            sig.reasons = (sig.reasons or []) + [_market_note]
            logger.info(
                f"[ForwardTest] ⏸ Market closed for {strat.broker.value} — "
                f"signal saved but trade suppressed"
            )
            _result["reason"] = "market_closed"

        # ── ML-03: Portfolio weight multiplier ───────────────────────────
        port_weight = 1.0
        try:
            w = params.get("weight")
            if w is not None:
                port_weight = max(0.05, float(w))
        except (TypeError, ValueError):
            pass

        async with AsyncSessionLocal() as session:
            # Hydrate engine state from DB before processing (balance, open positions)
            await forward_engine.initialize(session)

            # ── Reconcile + SL/TP monitor (B8: skipped when Run Now pre-runs these once) ──
            if not skip_monitor:
                # ── Reconcile broker positions FIRST
                try:
                    _ghosts = await forward_engine.reconcile_positions(session)
                    if _ghosts:
                        logger.info(f"[ForwardTest] Reconciled {_ghosts} ghost position(s) for {strat.name}")
                except Exception as _rec_err:
                    logger.debug(f"[ForwardTest] Reconcile error (non-fatal): {_rec_err}")

                # ── Software SL/TP enforcement
                try:
                    _sl_closed = await forward_engine.monitor_sl_tp(session)
                    if _sl_closed:
                        logger.info(f"[ForwardTest] SL/TP monitor closed {_sl_closed} position(s) for {strat.name}")
                except Exception as _mon_err:
                    logger.debug(f"[ForwardTest] SL/TP monitor error (non-fatal): {_mon_err}")

                # F-083: Commit monitor/reconcile changes before the deduplication check.
                try:
                    await session.commit()
                except Exception as _pre_commit_err:
                    logger.warning(f"[ForwardTest] Monitor/reconcile pre-commit failed: {_pre_commit_err}")

            # ── B3: Advisory lock prevents concurrent Run Now tasks from
            # double-inserting the same signal for the same strategy/symbol/timeframe.
            # pg_advisory_xact_lock is transaction-scoped; auto-released on commit.
            from sqlalchemy import text as _sql_text
            _lock_str = f"{strategy_type}:{sig.symbol}:{timeframe}"
            # LG-4 FIX: Python hash() is PYTHONHASHSEED-randomized per process; two
            # Celery workers would compute different lock keys, making the advisory
            # lock useless.  Use SHA-256 for a stable, deterministic key.
            _lock_int = int.from_bytes(
                _hashlib.sha256(_lock_str.encode()).digest()[:4], "big"
            ) % (2 ** 31)
            await session.execute(_sql_text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_int})

            # ── Deduplication: skip if an identical signal already exists within
            # one timeframe-period window to prevent double-saves on rapid Run Now
            _TF_DEDUP_MINUTES: dict[str, int] = {
                "1m": 2, "5m": 10, "15m": 20, "30m": 45,
                "1h": 75, "2h": 150, "4h": 300, "1d": 1440,
            }
            _dedup_window = timedelta(minutes=_TF_DEDUP_MINUTES.get(timeframe, 75))
            _cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - _dedup_window
            _existing = await session.execute(
                select(SignalModel).where(
                    SignalModel.symbol == sig.symbol,
                    SignalModel.strategy_name == sig.strategy_name,
                    SignalModel.signal == sig_type,
                    SignalModel.timeframe == sig.timeframe,
                    SignalModel.dismissed == False,  # noqa: E712
                    SignalModel.created_at >= _cutoff,
                ).limit(1)
            )
            if _existing.scalar_one_or_none() is not None:
                logger.info(
                    f"[ForwardTest] ⏭ Skipping duplicate signal: "
                    f"{sig.signal} {sig.symbol} (already saved within {timeframe} window)"
                )
                _result["reason"] = "duplicate"
                return _result

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
                execution_mode=strat.execution_mode.value,
                reasons=sig.reasons,
                acted_on=False,
                # Options fields (None for non-options signals)
                iv_rank=getattr(sig, "iv_rank", None),
                delta=getattr(sig, "delta", None),
                theta=getattr(sig, "theta", None),
                vega=getattr(sig, "vega", None),
                options_meta=getattr(sig, "options_meta", None),
            )
            session.add(db_signal)
            await session.flush()

            # ── ML-01: Create TradeOutcome for feedback loop ──────────────
            if sig.signal in _TRACKABLE_SIGNALS:
                from db.models import TradeOutcome as TradeOutcomeModel
                _trailing = None
                _t = params.get("trailing_stop_pct")
                if _t is not None:
                    try:
                        _trailing = float(_t)
                    except (TypeError, ValueError):
                        pass
                # Attach trailing_stop_pct to the signal so process_signal
                # stores it on the Trade record for live monitoring.
                if _trailing is not None:
                    sig.trailing_stop_pct = _trailing
                session.add(TradeOutcomeModel(
                    signal_id=db_signal.id,
                    symbol=sig.symbol,
                    timeframe=sig.timeframe,
                    strategy_name=sig.strategy_name,
                    signal_type=sig.signal,
                    entry_price=sig.entry_price,
                    stop_loss=sig.stop_loss,
                    take_profit=sig.take_profit,
                    trailing_stop_pct=_trailing,
                    resolved=False,
                    is_paper=strat.is_paper,
                ))

            # ── Pipe through ForwardEngine ────────────────────────────────
            trade = await forward_engine.process_signal(
                signal=sig,
                execution_mode=strat.execution_mode.value,
                is_paper=strat.is_paper,
                db_session=session,
                position_size_multiplier=port_weight,
            ) if allow_execution else None

            if trade is not None:
                trade.signal_id = db_signal.id
                db_signal.acted_on = True  # mark regardless of OPEN/REJECTED status
                _result["executed"] = True
            elif allow_execution and sig.signal in _TRACKABLE_SIGNALS:
                _result["reason"] = _result.get("reason") or "risk_rejected"
            else:
                _result["reason"] = _result.get("reason") or "hold"

            # Capture acted_on before commit — SQLAlchemy expires attributes on commit.
            _signal_acted_on = db_signal.acted_on

            await session.commit()

        # Broadcast signal via WebSocket
        await manager.broadcast("signal", {
            "symbol": sig.symbol,
            "signal": sig.signal,
            "entry_price": sig.entry_price,
            "confidence": sig.confidence,
            "strategy": sig.strategy_name,
            "timeframe": sig.timeframe,
            "acted_on": _signal_acted_on,
        })
        # F-083: process_signal() already broadcasts the "trade" WS event internally
        # (before commit, with the same data).  Re-broadcasting here after commit
        # would send a duplicate event to the frontend on every paper trade fill.

        logger.info(
            f"[ForwardTest] ✓ {strat.name} | {symbol} → {sig.signal} "
            f"@ {sig.entry_price} (conf={sig.confidence:.2f})"
        )
        return _result

    except Exception as e:
        logger.error(f"[ForwardTest] ✗ '{strat.name}': {e}", exc_info=True)
        return locals().get("_result")


async def _run_signals_background():
    """Run signal engine for ALL active paper strategies (used by 'Run Now' button)."""
    lock = _get_exec_lock()
    if lock.locked():
        logger.info("[ForwardTest] Run Now skipped — a run is already in progress.")
        return

    from db.database import AsyncSessionLocal
    from sqlalchemy import select as sa_select

    async with lock:
        _exec_state.update({
            "active": True,
            "trigger": "manual",
            "started_at": datetime.now(timezone.utc),
        })
        try:
            from api.websocket import manager as _ws_manager
            from db.database import AsyncSessionLocal
            from sqlalchemy import select as sa_select

            async with AsyncSessionLocal() as session:
                strat_q = await session.execute(
                    sa_select(StrategyModel).where(
                        StrategyModel.is_active == True,
                        StrategyModel.is_paper == True,  # F-097: never execute live strategies via "Run Now"
                    )
                )
                strategies = strat_q.scalars().all()

            await _ws_manager.broadcast("run_started", {
                "trigger": "manual",
                "strategies": len(strategies),
            })

            # B8: Run reconcile+monitor once before all strategies (not per-strategy).
            # Each _run_one_strategy will skip its own monitor pass (skip_monitor=True).
            from core.engine.forward_engine import ForwardEngine as _FE
            try:
                async with AsyncSessionLocal() as _pre_session:
                    _pre_engine = _FE()
                    await _pre_engine.initialize(_pre_session)
                    _g = await _pre_engine.reconcile_positions(_pre_session)
                    _c = await _pre_engine.monitor_sl_tp(_pre_session)
                    await _pre_session.commit()
                    if _g:
                        logger.info(f"[RunNow] Pre-run reconcile: {_g} ghost(s)")
                    if _c:
                        logger.info(f"[RunNow] Pre-run monitor: {_c} SL/TP close(s)")
            except Exception as _pre_err:
                logger.debug(f"[RunNow] Pre-run monitor/reconcile failed (non-fatal): {_pre_err}")

            # I3: Run strategies concurrently instead of sequentially
            _exec_state["strategy"] = f"{len(strategies)} strategies"
            _tasks = [_run_one_strategy(strat, skip_monitor=True) for strat in strategies]
            await _asyncio.gather(*_tasks, return_exceptions=True)

            await _ws_manager.broadcast("run_finished", {
                "trigger": "manual",
                "strategies": len(strategies),
            })
        finally:
            _exec_state.update({
                "active": False,
                "strategy": None,
                "trigger": None,
                "started_at": None,
            })


@router.post("/emergency-stop")
async def emergency_stop(db: AsyncSession = Depends(get_db), _user=Depends(_require_admin)):
    """
    Close all open paper trades immediately — fetches current price from broker
    to compute real PnL before marking each trade as FILLED.
    """
    from core.engine.forward_engine import get_forward_engine
    engine = get_forward_engine()  # FIX: use singleton to share state with running scheduler
    await engine.initialize(db)

    q = await db.execute(
        select(Trade).where(Trade.status == OrderStatus.OPEN)
    )
    open_trades = q.scalars().all()

    closed_count = 0
    for t in open_trades:
        try:
            await engine.close_position(t, reason="emergency_stop", db_session=db)  # F-100: pass db_session so atomic guard fires
            closed_count += 1
        except Exception as _e:
            logger.warning(f"[ForwardTest] Emergency stop: failed to close {t.symbol} id={t.id}: {_e}")

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
    from notifications.notifier import notifier as _notify
    await _notify.emergency(
        db,
        title="⚠️ Paper Emergency Stop Activated",
        message=f"{closed_count} paper position(s) force-closed. All paper strategies deactivated.",
        metadata={"positions_closed": closed_count, "mode": "paper"},
    )
    await db.commit()
    await manager.broadcast("emergency_stop", {"closed_trades": closed_count})

    return {
        "status": "emergency_stop_executed",
        "closed_trades": closed_count,
        "message": f"Closed {closed_count} of {len(open_trades)} open paper trade(s) and deactivated all paper strategies.",
    }


@router.get("/pending-signals")
async def get_pending_signals(db: AsyncSession = Depends(get_db), _user=Depends(_get_current_user)):
    """
    Return unacted, non-HOLD signals from active suggestion / semi-auto strategies
    created in the last 6 hours.  These are the signals awaiting manual execution.
    """
    from datetime import timedelta
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=6)

    # Active strategies in suggestion or semi-auto mode
    strat_q = await db.execute(
        select(StrategyModel).where(
            StrategyModel.is_active == True,
            StrategyModel.execution_mode.in_(["suggestion", "semi-auto", "SUGGESTION", "SEMI_AUTO"]),
        )
    )
    active_strats = {s.name: s for s in strat_q.scalars().all()}
    if not active_strats:
        return {"pending_signals": []}

    sig_q = await db.execute(
        select(SignalModel).where(
            SignalModel.acted_on == False,
            SignalModel.dismissed == False,
            SignalModel.signal != SignalType.HOLD,
            SignalModel.strategy_name.in_(list(active_strats.keys())),
            SignalModel.created_at >= since,
        ).order_by(desc(SignalModel.created_at))
    )
    signals = sig_q.scalars().all()

    result = []
    for s in signals:
        strat = active_strats.get(s.strategy_name)
        result.append({
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
            "execution_mode": strat.execution_mode.value if strat and hasattr(strat.execution_mode, "value") else (strat.execution_mode if strat else None),
            "is_paper": strat.is_paper if strat else True,
            "reasons": s.reasons or [],
            "created_at": s.created_at.isoformat() if s.created_at else None,
        })
    return {"pending_signals": result}


@router.post("/execute-signal/{signal_id}")
async def execute_signal(signal_id: int, db: AsyncSession = Depends(get_db), _user=Depends(_get_current_user)):
    """
    Manually execute a pending signal (suggestion or semi-auto mode).
    Forces full-auto execution regardless of the strategy's execution_mode setting.
    Called when the user clicks 'Execute' / 'Confirm' on the pending signal panel.
    """
    from core.strategies.base import Signal as SignalDataclass
    from core.engine.forward_engine import ForwardEngine
    from api.websocket import manager

    # Fetch signal
    sig_q = await db.execute(select(SignalModel).where(SignalModel.id == signal_id))
    db_signal = sig_q.scalar_one_or_none()
    if not db_signal:
        raise HTTPException(status_code=404, detail="Signal not found.")
    if db_signal.acted_on:
        raise HTTPException(status_code=409, detail="Signal already executed.")
    if db_signal.signal == SignalType.HOLD:
        raise HTTPException(status_code=400, detail="Cannot execute a HOLD signal.")

    # Market hours gate — block live execution when session is closed.
    # Paper execution is always allowed (pure simulation, no real order).
    _exec_broker = db_signal.broker.value if hasattr(db_signal.broker, "value") else str(db_signal.broker)
    _exec_asset_cls = getattr(db_signal.asset_class, "value", None)
    if not is_market_open(_exec_broker, _exec_asset_cls):
        # Find strategy to determine paper vs live
        _strat_q_pre = await db.execute(
            select(StrategyModel).where(StrategyModel.name == db_signal.strategy_name, StrategyModel.is_active == True)
        )
        _strat_pre = _strat_q_pre.scalar_one_or_none()
        _is_paper_pre = _strat_pre.is_paper if _strat_pre else True
        if not _is_paper_pre:
            raise HTTPException(
                status_code=400,
                detail=f"Market is closed for {_exec_broker}. Live orders can only be placed during trading hours.",
            )

    # Find strategy to know is_paper
    strat_q = await db.execute(
        select(StrategyModel).where(
            StrategyModel.name == db_signal.strategy_name,
            StrategyModel.is_active == True,
        )
    )
    strat = strat_q.scalar_one_or_none()
    is_paper = strat.is_paper if strat else True

    # Reconstruct Signal dataclass from DB record
    sig = SignalDataclass(
        symbol=db_signal.symbol,
        signal=db_signal.signal.value if hasattr(db_signal.signal, "value") else db_signal.signal,
        entry_price=db_signal.entry_price,
        stop_loss=db_signal.stop_loss,
        take_profit=db_signal.take_profit,
        confidence=db_signal.confidence or 0.0,
        timeframe=db_signal.timeframe,
        strategy_name=db_signal.strategy_name,
        asset_class=db_signal.asset_class.value if hasattr(db_signal.asset_class, "value") else db_signal.asset_class,
        broker=db_signal.broker.value if hasattr(db_signal.broker, "value") else db_signal.broker,
        regime=db_signal.regime,
        reasons=db_signal.reasons or [],
    )

    # Force full-auto execution
    engine = ForwardEngine()
    await engine.initialize(db)
    trade = await engine.process_signal(
        signal=sig,
        execution_mode="full-auto",
        is_paper=is_paper,
        db_session=db,
    )

    if trade is None:
        raise HTTPException(status_code=400, detail="Trade rejected by risk manager (position limits, daily loss cap, or insufficient balance).")

    # Mark signal acted on (even for REJECTED — prevents duplicate execution attempts)
    db_signal.acted_on = True
    trade.signal_id = db_signal.id
    try:
        await db.commit()
    except Exception as _ie:
        # Bug-23 FIX: signal_id unique constraint violation from a concurrent
        # execute attempt that squeezed through acted_on check.
        await db.rollback()
        raise HTTPException(status_code=409, detail="Signal already executed by a concurrent request.")

    # F-085: process_signal() already broadcasts the "trade" WS event internally.
    # Re-broadcasting here would send 2× events to the frontend on every manual execute.

    if trade.status == OrderStatus.REJECTED:
        raise HTTPException(
            status_code=400,
            detail=f"Broker rejected the order: {trade.notes or 'unknown reason'}",
        )

    return {
        "status": "executed",
        "trade_id": trade.id,
        "symbol": trade.symbol,
        "side": trade.side,
        "quantity": trade.quantity,
        "entry_price": trade.entry_price,
        "is_paper": is_paper,
    }
