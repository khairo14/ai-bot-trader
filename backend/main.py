from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from config import settings
from db.database import init_db
from api.routes import signals, positions, backtest, strategies, brokers, tools, portfolio, forward_test, notifications
from api.websocket import ws_endpoint


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
    yield
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

# Register API routes
app.include_router(signals.router, prefix="/api/signals", tags=["Signals"])
app.include_router(positions.router, prefix="/api/positions", tags=["Positions"])
app.include_router(backtest.router, prefix="/api/backtest", tags=["Backtest"])
app.include_router(strategies.router, prefix="/api/strategies", tags=["Strategies"])
app.include_router(brokers.router, prefix="/api/brokers", tags=["Brokers"])
app.include_router(tools.router, prefix="/api/tools", tags=["Tools"])
app.include_router(portfolio.router, prefix="/api/portfolio", tags=["Portfolio"])
app.include_router(forward_test.router, prefix="/api/forward-test", tags=["ForwardTest"])
app.include_router(notifications.router, prefix="/api/notifications", tags=["Notifications"])

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
