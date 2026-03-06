"""Forward Test API — paper trading control, status and trade history."""
import asyncio as _asyncio
import csv
import io
import math as _math
import time as _time_module
from datetime import datetime, timedelta, timezone
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


# ─────────────────────────────────────────────────────────────────────────────
# Timeframe helpers
# ─────────────────────────────────────────────────────────────────────────────

_TF_SECONDS: dict = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
    "1d": 86400, "1w": 604800,
}


def timeframe_to_seconds(tf: str) -> int:
    """Convert '4h' → 14400, '1d' → 86400, etc. Defaults to 3600 (1h)."""
    return _TF_SECONDS.get(str(tf).lower(), 3600)


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


def is_crypto_broker(broker_name: str) -> bool:
    """Return True for 24/7 crypto brokers (Binance, etc.)."""
    return broker_name.lower() not in _STOCK_BROKERS


def is_market_open(broker_name: str) -> bool:
    """
    Return True if trading is currently allowed for this broker.

    - Crypto (Binance): always True — trades 24/7
    - Stocks (Alpaca) / Options (IBKR):
        * NYSE regular session 09:30–16:00 ET only
        * Blocked on weekends
        * Blocked on all US market holidays (MLK Day, Presidents Day, Good Friday,
          Memorial Day, Juneteenth, Independence Day, Labor Day, Thanksgiving,
          Christmas) via pandas_market_calendars NYSE calendar
        * DST handled automatically via ZoneInfo / America/New_York
    """
    if is_crypto_broker(broker_name):
        return True
    now_et = _dt.now(tz=_ET)
    if not _is_nyse_trading_day(now_et.date()):
        return False
    return _MARKET_OPEN <= now_et.time() <= _MARKET_CLOSE


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

    # Real broker API balances (same source as Dashboard broker cards)
    from api.routes.portfolio import _safe_balance
    binance_bal, alpaca_bal, ibkr_bal = await _asyncio.gather(
        _safe_balance("binance"),
        _safe_balance("alpaca"),
        _safe_balance("ibkr"),
        return_exceptions=True,
    )
    api_balances: dict = {}
    for b in [binance_bal, alpaca_bal, ibkr_bal]:
        if not isinstance(b, Exception):
            api_balances[b["broker"]] = b

    # Sum closed paper trade P&L
    closed = await db.execute(
        select(Trade).where(Trade.is_paper == True, Trade.status == OrderStatus.FILLED)
    )
    closed_trades = closed.scalars().all()

    # Open paper trades
    open_q = await db.execute(
        select(Trade).where(Trade.is_paper == True, Trade.status == OrderStatus.OPEN)
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
    realized_pnl   = sum(t.pnl or 0.0 for t in closed_trades)
    unrealized_pnl = sum(t.pnl or 0.0 for t in open_trades)
    open_count     = len(open_trades)
    # Total = sum of real API balances for active+connected brokers
    total_balance = sum(
        bd["total"] for bd in broker_breakdown
        if bd["connected"] and bd["is_active"]
    ) or sum(bd["total"] for bd in broker_breakdown if bd["connected"])

    # Days running — from earliest trade
    first_q = await db.execute(
        select(func.min(Trade.opened_at)).where(Trade.is_paper == True)
    )
    first_opened: Optional[datetime] = first_q.scalar_one_or_none()
    days_running = 0
    if first_opened:
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
        "paper_balance": round(total_balance, 2),
        "initial_capital": total_balance,
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


async def _run_one_strategy(strat) -> None:
    """
    Run signal engine + forward engine for a single strategy.
    Opens its own DB session so it can be called independently by the scheduler.
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

    if not strategy_type or not symbol:
        logger.warning(
            f"[ForwardTest] Strategy '{strat.name}' missing strategy_type/symbol — skip"
        )
        return

    signal_engine = SignalEngine()
    forward_engine = ForwardEngine()

    try:
        sig = await signal_engine.run(
            strategy_name=strategy_type,
            symbol=symbol,
            broker_name=strat.broker.value,
            timeframe=timeframe,
            limit=limit,
        )

        logger.info(
            f"[ForwardTest] {sig.signal} {strat.name} | {symbol} "
            f"(conf={sig.confidence:.2f})"
        )

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

            # ── Deduplication: skip if an identical signal already exists within
            # one timeframe-period window to prevent double-saves on rapid Run Now
            _TF_DEDUP_MINUTES: dict[str, int] = {
                "1m": 2, "5m": 10, "15m": 20, "30m": 45,
                "1h": 75, "2h": 150, "4h": 300, "1d": 1440,
            }
            _dedup_window = timedelta(minutes=_TF_DEDUP_MINUTES.get(timeframe, 75))
            _cutoff = datetime.utcnow() - _dedup_window
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
                return

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
                "is_paper": strat.is_paper,
            })

        logger.info(
            f"[ForwardTest] ✓ {strat.name} | {symbol} → {sig.signal} "
            f"@ {sig.entry_price} (conf={sig.confidence:.2f})"
        )

    except Exception as e:
        logger.error(f"[ForwardTest] ✗ '{strat.name}': {e}", exc_info=True)


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
                    )
                )
                strategies = strat_q.scalars().all()

            await _ws_manager.broadcast("run_started", {
                "trigger": "manual",
                "strategies": len(strategies),
            })

            for strat in strategies:
                _exec_state["strategy"] = strat.name
                await _run_one_strategy(strat)

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
async def emergency_stop(db: AsyncSession = Depends(get_db)):
    """
    Close all open paper trades immediately — fetches current price from broker
    to compute real PnL before marking each trade as FILLED.
    """
    from core.engine.forward_engine import ForwardEngine
    engine = ForwardEngine()
    await engine.initialize(db)

    q = await db.execute(
        select(Trade).where(Trade.is_paper == True, Trade.status == OrderStatus.OPEN)
    )
    open_trades = q.scalars().all()

    for t in open_trades:
        await engine.close_position(t, reason="emergency_stop")

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


@router.get("/pending-signals")
async def get_pending_signals(db: AsyncSession = Depends(get_db)):
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
async def execute_signal(signal_id: int, db: AsyncSession = Depends(get_db)):
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

    # Mark signal acted on
    db_signal.acted_on = True
    await db.commit()

    # Broadcast
    await manager.broadcast("trade", {
        "symbol": trade.symbol,
        "side": trade.side,
        "quantity": trade.quantity,
        "entry_price": trade.entry_price,
        "broker": trade.broker.value if hasattr(trade.broker, "value") else trade.broker,
        "strategy_name": trade.strategy_name,
        "is_paper": is_paper,
        "triggered_by": "manual_execute",
    })

    return {
        "status": "executed",
        "trade_id": trade.id,
        "symbol": trade.symbol,
        "side": trade.side,
        "quantity": trade.quantity,
        "entry_price": trade.entry_price,
        "is_paper": is_paper,
    }
