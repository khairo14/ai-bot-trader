from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from config import settings
from db.database import init_db
from api.routes import signals, positions, backtest, strategies, brokers, tools, portfolio


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    logger.info("Starting AI Bot Trader backend...")
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


@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "ok", "version": "0.1.0"}
