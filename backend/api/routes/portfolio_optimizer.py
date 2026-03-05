"""
Portfolio Optimizer API Routes (ML-03)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from db.database import get_db
from db.models import Strategy

router = APIRouter()


@router.post("/run")
async def run_optimization(db: AsyncSession = Depends(get_db)):
    """
    Manually trigger portfolio optimization.
    Computes Sharpe-weighted strategy weights from resolved TradeOutcomes
    and writes them to Strategy.parameters["weight"].
    """
    try:
        from models.portfolio_optimizer import optimize_portfolio
        result = await optimize_portfolio()
        return result
    except Exception as exc:
        raise HTTPException(500, f"Optimization failed: {exc}")


@router.get("/weights")
async def get_weights(db: AsyncSession = Depends(get_db)):
    """
    Return current portfolio weights for all strategies that have one set.
    Also returns strategies without a weight (not yet optimized).
    """
    result = await db.execute(select(Strategy))
    strategies = list(result.scalars().all())

    weighted = []
    unweighted = []

    for s in strategies:
        params = s.parameters or {}
        w = params.get("weight")
        entry = {
            "id": s.id,
            "name": s.name,
            "strategy_type": params.get("strategy_type", s.name),
            "symbol": params.get("symbol", ""),
            "broker": s.broker.value if hasattr(s.broker, "value") else str(s.broker),
            "is_active": s.is_active,
        }
        if w is not None:
            entry["weight"] = float(w)
            weighted.append(entry)
        else:
            entry["weight"] = None
            unweighted.append(entry)

    # Sort by weight descending
    weighted.sort(key=lambda x: x["weight"], reverse=True)

    total_weight = round(sum(e["weight"] for e in weighted), 4)

    return {
        "weighted_strategies": weighted,
        "unweighted_strategies": unweighted,
        "total_weight": total_weight,
        "optimized": len(weighted) > 0,
    }


@router.delete("/weights")
async def clear_weights(db: AsyncSession = Depends(get_db)):
    """Remove all optimizer-assigned weights (reset to equal/manual)."""
    result = await db.execute(select(Strategy))
    strategies = list(result.scalars().all())

    cleared = 0
    for s in strategies:
        params = dict(s.parameters or {})
        if "weight" in params:
            del params["weight"]
            s.parameters = params
            cleared += 1

    await db.commit()
    return {"message": f"Cleared weights from {cleared} strategies"}
