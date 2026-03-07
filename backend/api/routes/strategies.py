from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone

from db.database import get_db
from db.models import Strategy, ExecutionMode, AssetClass, BrokerName
from core.auth import get_current_user, require_admin, audit

router = APIRouter()


def _validate_enum_fields(broker: Optional[str], asset_class: Optional[str], execution_mode: Optional[str]) -> None:
    """Raise HTTP 422 if any field value is not a valid enum member."""
    if broker is not None:
        try:
            BrokerName(broker)
        except ValueError:
            valid = [e.value for e in BrokerName]
            raise HTTPException(status_code=422, detail=f"Invalid broker '{broker}'. Valid: {valid}")
    if asset_class is not None:
        try:
            AssetClass(asset_class)
        except ValueError:
            valid = [e.value for e in AssetClass]
            raise HTTPException(status_code=422, detail=f"Invalid asset_class '{asset_class}'. Valid: {valid}")
    if execution_mode is not None:
        try:
            ExecutionMode(execution_mode)
        except ValueError:
            valid = [e.value for e in ExecutionMode]
            raise HTTPException(status_code=422, detail=f"Invalid execution_mode '{execution_mode}'. Valid: {valid}")


class StrategyCreate(BaseModel):
    name: str
    description: Optional[str] = None
    asset_class: str
    broker: str
    execution_mode: str = "suggestion"
    parameters: Optional[dict] = None


class StrategyUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    broker: Optional[str] = None
    asset_class: Optional[str] = None
    execution_mode: Optional[str] = None
    is_active: Optional[bool] = None
    is_paper: Optional[bool] = None
    parameters: Optional[dict] = None


@router.get("/")
async def list_strategies(db: AsyncSession = Depends(get_db)):
    """List all configured strategies."""
    result = await db.execute(select(Strategy))
    strategies = result.scalars().all()
    return {"strategies": strategies}


@router.post("/")
async def create_strategy(payload: StrategyCreate, db: AsyncSession = Depends(get_db), _: object = Depends(require_admin)):
    """Create a new strategy configuration."""
    _validate_enum_fields(payload.broker, payload.asset_class, payload.execution_mode)
    strategy = Strategy(
        name=payload.name,
        description=payload.description,
        asset_class=payload.asset_class,
        broker=payload.broker,
        execution_mode=payload.execution_mode,
        parameters=payload.parameters or {},
        is_active=False,
        is_paper=True,
    )
    db.add(strategy)
    await db.commit()
    await db.refresh(strategy)
    return strategy


# ── Default seed templates ──────────────────────────────────────────────────
# NOTE: This must be defined and registered BEFORE /{strategy_id} routes so
#       Starlette does not swallow POST /seed as a 405 on the dynamic route.
_SEED_TEMPLATES = [
    # Binance / crypto
    {"name": "BTC Trend Follower",      "broker": "binance", "asset_class": "crypto",
     "description": "BTC/USDT 1h MACD-RSI trend following with ADX filter.",
     "symbol": "BTC/USDT", "timeframe": "1h"},
    {"name": "ETH Swing Trader",        "broker": "binance", "asset_class": "crypto",
     "description": "ETH/USDT 4h swing strategy — MACD histogram reversal + RSI(14).",
     "symbol": "ETH/USDT", "timeframe": "4h"},
    {"name": "BNB Momentum Scalp",      "broker": "binance", "asset_class": "crypto",
     "description": "BNB/USDT 1h EMA crossover momentum with volume spike filter.",
     "symbol": "BNB/USDT", "timeframe": "1h"},
    # Alpaca / US stocks
    {"name": "Apple Daily Trend",       "broker": "alpaca",  "asset_class": "stock",
     "description": "AAPL 1d MACD + EMA(21) slope trend — low-frequency, high-conviction.",
     "symbol": "AAPL",     "timeframe": "1d"},
    {"name": "SPY Index Follower",      "broker": "alpaca",  "asset_class": "stock",
     "description": "SPY 1d broad-market trend with ADX filter. Highest average win rate.",
     "symbol": "SPY",      "timeframe": "1d"},
    {"name": "NVDA Momentum Daily",     "broker": "alpaca",  "asset_class": "stock",
     "description": "NVDA 1d MACD histogram expansion — earnings-momentum and sector rotation.",
     "symbol": "NVDA",     "timeframe": "1d"},
    # IBKR / US stocks
    {"name": "MSFT Blue Chip Trend",    "broker": "ibkr",    "asset_class": "stock",
     "description": "MSFT 1d conservative trend — MACD-RSI confluence at clear inflection points.",
     "symbol": "MSFT",     "timeframe": "1d"},
    {"name": "TSLA Swing Play",         "broker": "ibkr",    "asset_class": "stock",
     "description": "TSLA 4h high-beta swing — RSI extremes + MACD confirm, wide R:R target.",
     "symbol": "TSLA",     "timeframe": "4h"},
    {"name": "Amazon Trend Follow",     "broker": "ibkr",    "asset_class": "stock",
     "description": "AMZN 1d breakout-continuation after Bollinger Band squeeze + MACD cross.",
     "symbol": "AMZN",     "timeframe": "1d"},
]


@router.post("/seed")
async def seed_default_strategies(db: AsyncSession = Depends(get_db), _: object = Depends(require_admin)):
    """
    Idempotently insert the 9 default curated strategies (3 per broker).
    Strategies whose name already exists are skipped.
    """
    existing_result = await db.execute(select(Strategy.name))
    existing_names = {row[0] for row in existing_result.all()}

    created, skipped = 0, 0
    for t in _SEED_TEMPLATES:
        if t["name"] in existing_names:
            skipped += 1
            continue
        strategy = Strategy(
            name=t["name"],
            description=t["description"],
            asset_class=t["asset_class"],
            broker=t["broker"],
            execution_mode="suggestion",
            parameters={
                "strategy_type": "hybrid_macd_rsi",
                "symbol": t["symbol"],
                "timeframe": t["timeframe"],
                "limit": 200,
            },
            is_active=False,
            is_paper=True,
        )
        db.add(strategy)
        created += 1

    if created:
        await db.commit()

    return {"created": created, "skipped": skipped, "total_templates": len(_SEED_TEMPLATES)}


# ── Per-strategy CRUD (must come AFTER /seed so it doesn't shadow it) ───────

@router.patch("/{strategy_id}")
async def update_strategy(
    strategy_id: int,
    payload: StrategyUpdate,
    db: AsyncSession = Depends(get_db),
    _: object = Depends(require_admin),
):
    """Update strategy settings (execution mode, active state, parameters)."""
    result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    _validate_enum_fields(payload.broker, payload.asset_class, payload.execution_mode)

    if payload.name is not None:
        strategy.name = payload.name  # type: ignore[assignment]
    if payload.description is not None:
        strategy.description = payload.description  # type: ignore[assignment]
    if payload.broker is not None:
        strategy.broker = payload.broker  # type: ignore[assignment]
    if payload.asset_class is not None:
        strategy.asset_class = payload.asset_class  # type: ignore[assignment]
    if payload.execution_mode is not None:
        strategy.execution_mode = payload.execution_mode  # type: ignore[assignment]
    if payload.is_active is not None:
        strategy.is_active = payload.is_active  # type: ignore[assignment]
    if payload.is_paper is not None:
        strategy.is_paper = payload.is_paper  # type: ignore[assignment]
    if payload.parameters is not None:
        strategy.parameters = payload.parameters  # type: ignore[assignment]
    strategy.updated_at = datetime.now(timezone.utc)  # type: ignore[assignment]

    await db.commit()
    await db.refresh(strategy)
    return strategy


@router.delete("/{strategy_id}")
async def delete_strategy(strategy_id: int, db: AsyncSession = Depends(get_db), _: object = Depends(require_admin)):
    """Delete a strategy."""
    result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    await db.delete(strategy)
    await db.commit()
    audit("strategy.delete", strategy_id=strategy_id)
    return {"message": f"Strategy {strategy_id} deleted."}
