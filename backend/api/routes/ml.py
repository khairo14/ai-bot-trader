"""
ML Feedback Loop API
====================
Endpoints:
  GET  /api/ml/status      — model stats, outcome counts, last retrain
  POST /api/ml/resolve     — manually trigger outcome resolution (dev/admin)
  POST /api/ml/retrain     — manually trigger model retraining (dev/admin)
"""

from __future__ import annotations
import json
import pathlib
import datetime
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import select, func

router = APIRouter()

_BASE_DIR = pathlib.Path(__file__).resolve().parent.parent.parent   # backend/
_LATEST_JSON = _BASE_DIR / "data" / "models" / "latest.json"


# ── Response schemas ─────────────────────────────────────────────────────────

class ModelInfo(BaseModel):
    symbol: str
    model_path: str
    trained_date: Optional[str] = None


class MLStatusResponse(BaseModel):
    model_count: int
    models: list[ModelInfo]
    last_retrain: Optional[str]        # ISO datetime from latest.json mtime
    outcomes_total: int
    outcomes_resolved: int
    outcomes_pending: int
    win_rate_pct: Optional[float]      # from resolved outcomes
    avg_pnl_pct: Optional[float]
    feedback_loop_active: bool         # True once we have ≥1 resolved outcome


# ─────────────────────────────────────────────────────────────────────────────

@router.get("/status", response_model=MLStatusResponse)
async def ml_status():
    """Return ML model and feedback-loop stats."""
    from db.database import AsyncSessionLocal
    from db.models import TradeOutcome

    # ── Load model registry ───────────────────────────────────────────────
    models: list[ModelInfo] = []
    last_retrain: Optional[str] = None

    if _LATEST_JSON.exists():
        try:
            registry: dict = json.loads(_LATEST_JSON.read_text())
            mtime = datetime.datetime.fromtimestamp(_LATEST_JSON.stat().st_mtime)
            last_retrain = mtime.isoformat(timespec="seconds")
            for symbol, path in registry.items():
                p = pathlib.Path(path)
                # Extract date from filename: BTC_USDT_2026-03-05.pkl
                parts = p.stem.split("_")
                trained_date = parts[-1] if len(parts) > 1 else None
                models.append(ModelInfo(
                    symbol=symbol,
                    model_path=str(p.name),
                    trained_date=trained_date,
                ))
        except Exception:
            pass

    # ── Outcome stats from DB ─────────────────────────────────────────────
    async with AsyncSessionLocal() as session:
        total_r = await session.execute(select(func.count()).select_from(TradeOutcome))
        outcomes_total = total_r.scalar() or 0

        resolved_r = await session.execute(
            select(func.count()).select_from(TradeOutcome).where(TradeOutcome.resolved == True)  # noqa: E712
        )
        outcomes_resolved = resolved_r.scalar() or 0

        # Win rate + avg pnl (resolved only)
        win_rate_pct: Optional[float] = None
        avg_pnl_pct: Optional[float] = None

        if outcomes_resolved > 0:
            wins_r = await session.execute(
                select(func.count()).select_from(TradeOutcome).where(
                    TradeOutcome.resolved == True,  # noqa: E712
                    TradeOutcome.ml_label == 1,
                )
            )
            wins = wins_r.scalar() or 0
            win_rate_pct = round(wins / outcomes_resolved * 100, 1)

            pnl_r = await session.execute(
                select(func.avg(TradeOutcome.pnl_pct)).where(
                    TradeOutcome.resolved == True  # noqa: E712
                )
            )
            avg_pnl_raw = pnl_r.scalar()
            if avg_pnl_raw is not None:
                avg_pnl_pct = round(float(avg_pnl_raw), 3)

    outcomes_pending = outcomes_total - outcomes_resolved

    return MLStatusResponse(
        model_count=len(models),
        models=models,
        last_retrain=last_retrain,
        outcomes_total=outcomes_total,
        outcomes_resolved=outcomes_resolved,
        outcomes_pending=outcomes_pending,
        win_rate_pct=win_rate_pct,
        avg_pnl_pct=avg_pnl_pct,
        feedback_loop_active=outcomes_resolved > 0,
    )


@router.post("/resolve")
async def trigger_resolve():
    """Manually trigger outcome resolution (useful for testing)."""
    from tasks.outcome_resolver import resolve_pending_outcomes
    import asyncio
    result = await resolve_pending_outcomes()
    return {"status": "ok", "result": result}


@router.post("/retrain")
async def trigger_retrain():
    """Manually trigger ML retraining via Celery (async, returns task ID)."""
    try:
        from tasks.ml_retrain import retrain_all
        task = retrain_all.delay()
        return {"status": "queued", "task_id": task.id}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}
