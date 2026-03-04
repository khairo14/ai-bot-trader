from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from db.database import get_db
from db.models import BacktestResult

router = APIRouter()


class BacktestRequest(BaseModel):
    strategy_name: str
    symbol: str
    timeframe: str = "1h"
    start_date: str          # ISO format: "2024-01-01"
    end_date: str            # ISO format: "2025-01-01"
    initial_capital: float = 10000.0
    commission_pct: float = 0.1
    slippage_pct: float = 0.05
    risk_per_trade_pct: float = 2.0
    broker: str = "binance"
    parameters: Optional[dict] = None


@router.post("/run")
async def run_backtest(request: BacktestRequest, db: AsyncSession = Depends(get_db)):
    """
    Trigger a backtest run for a strategy.
    Returns backtest result ID. Results are stored in DB.
    """
    from core.engine.backtest_engine import BacktestEngine

    engine = BacktestEngine()
    result = await engine.run(
        strategy_name=request.strategy_name,
        symbol=request.symbol,
        timeframe=request.timeframe,
        start_date=datetime.fromisoformat(request.start_date),
        end_date=datetime.fromisoformat(request.end_date),
        initial_capital=request.initial_capital,
        commission_pct=request.commission_pct,
        slippage_pct=request.slippage_pct,
        risk_per_trade_pct=request.risk_per_trade_pct,
        broker=request.broker,
        parameters=request.parameters or {},
    )

    db_result = BacktestResult(**result)
    db.add(db_result)
    await db.commit()
    await db.refresh(db_result)

    return {"backtest_id": db_result.id, "result": result}


@router.get("/results")
async def list_results(db: AsyncSession = Depends(get_db)):
    """List all backtest results."""
    query = select(BacktestResult).order_by(desc(BacktestResult.created_at)).limit(100)
    result = await db.execute(query)
    return {"results": result.scalars().all()}


@router.get("/results/{result_id}")
async def get_result(result_id: int, db: AsyncSession = Depends(get_db)):
    """Get a single backtest result."""
    result = await db.execute(
        select(BacktestResult).where(BacktestResult.id == result_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Backtest result not found")
    return item
