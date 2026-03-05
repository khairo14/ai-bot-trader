import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, Depends
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from config import settings
from db.database import init_db
from api.routes import signals, positions, backtest, strategies, brokers, tools, portfolio, forward_test, notifications, ml, charts, auth as auth_routes, regime, analytics, strategy_code, confluence, portfolio_optimizer
from api.websocket import ws_endpoint
from core.auth import get_current_user


async def _forward_test_scheduler():
    """
    Wall-clock-aligned, market-hours-aware scheduler.

    Tick: every 60 s.
    Fire rule (per strategy):
      1. UTC candle close has passed + 30 s buffer
      2. We haven't already fired for this candle close
      3. The broker's market is currently open
         - Crypto (Binance): always open (24/7)
         - Stocks (Alpaca) / Options (IBKR): NYSE 09:30–16:00 ET, Mon–Fri

    Stock intraday (e.g. 1h):
      Fires at each hour close during the session — 09:30:30, 10:00:30 … 15:00:30 ET.

    Stock daily (1d):
      The UTC midnight candle-close epoch (00:00 UTC) passes while the market is
      closed.  `last_fired` is NOT updated when the market is closed, so the
      strategy fires at the very first scheduler tick after market opens
      (09:30 ET) on the next trading day — using yesterday’s fully closed
      daily candle, which is exactly what backtests use.

    Crypto:
      Fires at every UTC candle-close boundary (1h → 01:00, 02:00 … UTC).
    """
    import math
    import time
    from api.routes.forward_test import (
        timeframe_to_seconds, _run_one_strategy, is_market_open,
        _get_exec_lock, _exec_state,
    )
    from db.database import AsyncSessionLocal
    from db.models import Strategy as StrategyModel
    from sqlalchemy import select
    from datetime import datetime, timezone as _tz

    last_fired: dict = {}   # strategy_id → UTC epoch of last candle close we fired on
    CLOSE_BUFFER = 30       # seconds after candle close before we fire

    logger.info("[Scheduler] Wall-clock-aligned, market-hours-aware scheduler started.")

    while True:
        await asyncio.sleep(60)
        try:
            async with AsyncSessionLocal() as session:
                q = await session.execute(
                    select(StrategyModel).where(
                        StrategyModel.is_active == True,
                        StrategyModel.is_paper == True,
                    )
                )
                strategies = q.scalars().all()

            now = time.time()
            for strat in strategies:
                params       = strat.parameters or {}
                tf           = params.get("timeframe", "1h")
                interval     = timeframe_to_seconds(tf)
                broker_name  = strat.broker.value

                # Last closed candle boundary (UTC epoch)
                last_close   = math.floor(now / interval) * interval
                already_fired = last_fired.get(strat.id, 0)

                if not (now >= last_close + CLOSE_BUFFER and already_fired < last_close):
                    continue  # candle hasn't closed yet, or already handled

                # ── Market-hours gate ─────────────────────────────
                # Do NOT mark last_fired if market is closed — we want to retry
                # each minute until the session opens (covers overnight + weekends).
                if not is_market_open(broker_name):
                    from datetime import datetime, timezone
                    now_et_str = datetime.now(
                        tz=__import__('zoneinfo').ZoneInfo('America/New_York')
                    ).strftime("%a %H:%M ET")
                    logger.debug(
                        f"[Scheduler] {strat.name} ({tf}) — "
                        f"market closed ({now_et_str}), will retry when open"
                    )
                    continue  # last_fired intentionally NOT updated

                # ── Fire! ──────────────────────────────────
                lock = _get_exec_lock()
                if lock.locked():
                    logger.debug(
                        f"[Scheduler] {strat.name} ({tf}) — "
                        f"skipping, another run already in progress"
                    )
                    continue

                from datetime import datetime, timezone
                close_dt = datetime.fromtimestamp(
                    last_close, tz=timezone.utc
                ).strftime("%H:%M UTC")
                logger.info(
                    f"[Scheduler] {strat.name} ({tf}) — "
                    f"candle closed at {close_dt}, running now"
                )
                async with lock:
                    _exec_state.update({
                        "active": True,
                        "strategy": strat.name,
                        "trigger": "scheduler",
                        "started_at": datetime.now(timezone.utc),
                    })
                    try:
                        from api.websocket import manager as _ws_mgr
                        await _ws_mgr.broadcast("run_started", {
                            "trigger": "scheduler",
                            "strategies": 1,
                            "strategy": strat.name,
                        })
                        await _run_one_strategy(strat)
                        await _ws_mgr.broadcast("run_finished", {
                            "trigger": "scheduler",
                            "strategies": 1,
                            "strategy": strat.name,
                        })
                    finally:
                        _exec_state.update({
                            "active": False,
                            "strategy": None,
                            "trigger": None,
                            "started_at": None,
                        })
                last_fired[strat.id] = last_close

        except Exception as exc:
            logger.error(f"[Scheduler] Tick error: {exc}", exc_info=True)
            logger.error(f"[Scheduler] Tick error: {exc}", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    logger.info("Starting AI Bot Trader backend...")

    # Run pending Alembic migrations automatically on startup (I-02)
    try:
        import subprocess, sys
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            logger.warning(f"Alembic upgrade warning: {result.stderr.strip()}")
        else:
            logger.info("Alembic: schema up to date.")
    except Exception as e:
        logger.warning(f"Alembic auto-upgrade skipped: {e}")

    await init_db()
    logger.info("Database initialized.")

    # Start auto-scheduler (skip if interval set to 0 — manual-only mode)
    scheduler_task = None
    if settings.forward_test_interval_minutes > 0:
        scheduler_task = asyncio.create_task(_forward_test_scheduler())
    else:
        logger.info("[Scheduler] Auto forward-test disabled (FORWARD_TEST_INTERVAL_MINUTES=0).")

    yield

    if scheduler_task:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
    logger.info("Shutting down AI Bot Trader backend...")


app = FastAPI(
    title="AI Bot Trader API",
    description="Hybrid rule-based + ML trading bot supporting Crypto, Stocks, and Options.",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — allow frontend to communicate with backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auth routes — no authentication required
app.include_router(auth_routes.router, prefix="/auth", tags=["Auth"])

# Protected API routes — all require a valid JWT
_auth = [Depends(get_current_user)]
app.include_router(signals.router, prefix="/api/signals", tags=["Signals"], dependencies=_auth)
app.include_router(positions.router, prefix="/api/positions", tags=["Positions"], dependencies=_auth)
app.include_router(backtest.router, prefix="/api/backtest", tags=["Backtest"], dependencies=_auth)
app.include_router(strategies.router, prefix="/api/strategies", tags=["Strategies"], dependencies=_auth)
app.include_router(brokers.router, prefix="/api/brokers", tags=["Brokers"], dependencies=_auth)
app.include_router(tools.router, prefix="/api/tools", tags=["Tools"], dependencies=_auth)
app.include_router(portfolio.router, prefix="/api/portfolio", tags=["Portfolio"], dependencies=_auth)
app.include_router(forward_test.router, prefix="/api/forward-test", tags=["ForwardTest"], dependencies=_auth)
app.include_router(notifications.router, prefix="/api/notifications", tags=["Notifications"], dependencies=_auth)
app.include_router(ml.router, prefix="/api/ml", tags=["ML"], dependencies=_auth)
app.include_router(charts.router, prefix="/api/charts", tags=["Charts"], dependencies=_auth)
app.include_router(regime.router, prefix="/api/regime", tags=["Regime"], dependencies=_auth)
app.include_router(analytics.router, prefix="/api/analytics", tags=["Analytics"], dependencies=_auth)
app.include_router(strategy_code.router, prefix="/api/strategy-code", tags=["StrategyCode"], dependencies=_auth)
app.include_router(confluence.router, prefix="/api/confluence", tags=["Confluence"], dependencies=_auth)
app.include_router(portfolio_optimizer.router, prefix="/api/portfolio-optimizer", tags=["PortfolioOptimizer"], dependencies=_auth)

# WebSocket endpoint for real-time signal/trade broadcasts
app.add_api_websocket_route("/ws", ws_endpoint)


@app.get("/health", tags=["Health"])
async def health_check(deep: bool = False):
    """Basic liveness check. Add ?deep=true for dependency status."""
    result: dict = {"status": "ok", "version": "0.1.0"}
    if not deep:
        return result

    # ── DB ping ───────────────────────────────────────────
    from db.database import AsyncSessionLocal
    from sqlalchemy import text as sa_text
    db_ok = False
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("SELECT 1"))
        db_ok = True
    except Exception as e:
        result["db_error"] = str(e)

    # ── Redis ping ────────────────────────────────────────
    redis_ok = False
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url, socket_connect_timeout=3)
        redis_ok = bool(await r.ping())
        await r.aclose()
    except Exception as e:
        result["redis_error"] = str(e)

    result["db"] = "ok" if db_ok else "error"
    result["redis"] = "ok" if redis_ok else "error"
    if not db_ok or not redis_ok:
        result["status"] = "degraded"
    return result
