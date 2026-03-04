from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from db.database import get_db
from db.models import Strategy, ExecutionMode, AssetClass, BrokerName

router = APIRouter()


class StrategyCreate(BaseModel):
    name: str
    description: Optional[str] = None
    asset_class: str
    broker: str
    execution_mode: str = "suggestion"
    parameters: Optional[dict] = None


class StrategyUpdate(BaseModel):
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
async def create_strategy(payload: StrategyCreate, db: AsyncSession = Depends(get_db)):
    """Create a new strategy configuration."""
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


@router.patch("/{strategy_id}")
async def update_strategy(
    strategy_id: int,
    payload: StrategyUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update strategy settings (execution mode, active state, parameters)."""
    result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    if payload.execution_mode is not None:
        strategy.execution_mode = payload.execution_mode  # type: ignore[assignment]
    if payload.is_active is not None:
        strategy.is_active = payload.is_active  # type: ignore[assignment]
    if payload.is_paper is not None:
        strategy.is_paper = payload.is_paper  # type: ignore[assignment]
    if payload.parameters is not None:
        strategy.parameters = payload.parameters  # type: ignore[assignment]
    strategy.updated_at = datetime.utcnow()  # type: ignore[assignment]

    await db.commit()
    await db.refresh(strategy)
    return strategy


@router.delete("/{strategy_id}")
async def delete_strategy(strategy_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a strategy."""
    result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    await db.delete(strategy)
    await db.commit()
    return {"message": f"Strategy {strategy_id} deleted."}
