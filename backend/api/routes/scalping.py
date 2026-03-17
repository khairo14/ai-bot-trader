"""
Scalping REST API routes
=========================
  GET  /scalping/signals      — latest N scalp signals
  GET  /scalping/stats        — win rate, avg hold time, PnL summary
  GET  /scalping/settings     — current scalping_settings.json contents
  POST /scalping/settings     — update settings at runtime
  POST /scalping/enable       — toggle enabled flag without full settings update
"""

import json
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user, require_admin
from db.database import get_db
from db.models import Signal, SignalType

router = APIRouter()

_SETTINGS_PATH = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "runtime" / "scalping_settings.json"
)

_SETTINGS_DEFAULTS = {
    "enabled": True,
    "timeframe": "5m",
    "risk_per_trade_pct": 0.5,
    "sl_atr_mult": 0.8,
    "tp_atr_mult": 1.6,
    "min_volume_ratio": 1.2,
    "max_spread_pct": 0.05,
    "min_score": 4,
    "ml_veto_threshold": 0.40,
    "sr_tp_snap": False,
    # Risk gate overrides (applied by ForwardEngine for scalp_ signals only)
    "max_consecutive_losses": 5,   # higher tolerance than swing (3)
    "session_filter": {"crypto": None, "stock": ["14:30-21:00"]},
}


def _read_settings() -> dict:
    try:
        if _SETTINGS_PATH.exists():
            data = json.loads(_SETTINGS_PATH.read_text())
            return {**_SETTINGS_DEFAULTS, **data}
    except (json.JSONDecodeError, OSError):
        pass
    return dict(_SETTINGS_DEFAULTS)


def _write_settings(data: dict) -> None:
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(json.dumps(data, indent=2))


def _signal_dict(s: Signal) -> dict:
    return {
        "id":            s.id,
        "symbol":        s.symbol,
        "signal":        s.signal.value if hasattr(s.signal, "value") else s.signal,
        "entry_price":   s.entry_price,
        "stop_loss":     s.stop_loss,
        "take_profit":   s.take_profit,
        "confidence":    s.confidence,
        "timeframe":     s.timeframe,
        "strategy_name": s.strategy_name,
        "regime":        s.regime,
        "asset_class":   s.asset_class.value if hasattr(s.asset_class, "value") else s.asset_class,
        "broker":        s.broker.value if hasattr(s.broker, "value") else s.broker,
        "execution_mode":s.execution_mode.value if s.execution_mode is not None and hasattr(s.execution_mode, "value") else s.execution_mode,
        "reasons":       s.reasons,
        "acted_on":      s.acted_on,
        "created_at":    s.created_at.isoformat() if s.created_at else None,
    }


# ── GET /scalping/signals ─────────────────────────────────────────────────────

@router.get("/signals")
async def scalp_signals(
    symbol: Optional[str] = None,
    limit: int = Query(default=50, le=500),
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Latest N scalping signals (strategy_name LIKE 'scalp_%')."""
    query = (
        select(Signal)
        .where(Signal.strategy_name.ilike("%scalp%"))
        .where(Signal.dismissed == False)  # noqa: E712
        .where(Signal.signal != SignalType.HOLD)  # exclude noise — only trade signals
        .order_by(desc(Signal.created_at))
        .limit(limit)
    )
    if symbol:
        query = query.where(Signal.symbol == symbol.upper())
    result = await db.execute(query)
    return {"signals": [_signal_dict(s) for s in result.scalars().all()]}


# ── GET /scalping/stats ───────────────────────────────────────────────────────

@router.get("/stats")
async def scalp_stats(
    days: int = Query(default=7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """
    Scalping performance summary for the last N days.
    Based on resolved TradeOutcomes tied to scalp strategies.
    """
    from db.models import TradeOutcome

    since = datetime.now(timezone.utc) - timedelta(days=days)
    q = await db.execute(
        select(TradeOutcome).where(
            and_(
                TradeOutcome.strategy_name.ilike("%scalp%"),
                TradeOutcome.resolved == True,  # noqa: E712
                TradeOutcome.created_at >= since.replace(tzinfo=None),
            )
        )
    )
    outcomes = q.scalars().all()

    total      = len(outcomes)
    wins       = sum(1 for o in outcomes if o.ml_label == 1)
    win_rate   = round(wins / total * 100, 1) if total else 0.0
    _pnl_vals  = [o.pnl_pct for o in outcomes if o.pnl_pct is not None]
    avg_pnl    = round(sum(_pnl_vals) / len(_pnl_vals), 4) if _pnl_vals else 0.0
    _held_vals = [o.candles_held for o in outcomes if o.candles_held is not None]
    avg_hold   = round(sum(_held_vals) / len(_held_vals), 1) if _held_vals else 0.0

    # Signals generated today (UTC)
    today_utc  = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_q    = await db.execute(
        select(func.count(Signal.id)).where(
            and_(
                Signal.strategy_name.ilike("%scalp%"),
                Signal.signal != SignalType.HOLD,
                Signal.created_at >= today_utc.replace(tzinfo=None),
            )
        )
    )
    signals_today = today_q.scalar_one_or_none() or 0

    return {
        "period_days":    days,
        "total_outcomes": total,
        "wins":           wins,
        "losses":         total - wins,
        "win_rate_pct":   win_rate,
        "avg_pnl_pct":    avg_pnl,
        "avg_hold_candles": avg_hold,
        "signals_today":  signals_today,
    }


# ── GET /scalping/settings ────────────────────────────────────────────────────

@router.get("/settings")
async def get_scalp_settings(_user=Depends(get_current_user)):
    """Return current scalping_settings.json contents."""
    return _read_settings()


# ── POST /scalping/settings ───────────────────────────────────────────────────

class ScalpingSettingsUpdate(BaseModel):
    enabled:                Optional[bool]  = None
    timeframe:              Optional[str]   = None
    risk_per_trade_pct:     Optional[float] = None
    sl_atr_mult:            Optional[float] = None
    tp_atr_mult:            Optional[float] = None
    min_volume_ratio:       Optional[float] = None
    max_spread_pct:         Optional[float] = None
    min_score:              Optional[int]   = None
    ml_veto_threshold:      Optional[float] = None
    sr_tp_snap:             Optional[bool]  = None
    max_consecutive_losses: Optional[int]   = None
    session_filter:         Optional[dict]  = None


@router.post("/settings")
async def update_scalp_settings(
    payload: ScalpingSettingsUpdate,
    _admin=Depends(require_admin),
):
    """Update scalping_settings.json at runtime. Requires admin."""
    current = _read_settings()
    updates = payload.model_dump(exclude_none=True)

    # Validate timeframe if supplied
    valid_tfs = {"1m", "3m", "5m", "15m", "30m"}
    if "timeframe" in updates and updates["timeframe"] not in valid_tfs:
        raise HTTPException(
            status_code=422,
            detail=f"Scalping timeframe must be one of {sorted(valid_tfs)}"
        )

    # Validate risk bounds
    if "risk_per_trade_pct" in updates:
        v = updates["risk_per_trade_pct"]
        if not (0.1 <= v <= 5.0):
            raise HTTPException(status_code=422, detail="risk_per_trade_pct must be 0.1–5.0")

    if "ml_veto_threshold" in updates:
        v = updates["ml_veto_threshold"]
        if not (0.20 <= v <= 0.70):
            raise HTTPException(status_code=422, detail="ml_veto_threshold must be 0.20–0.70")

    if "max_consecutive_losses" in updates:
        v = updates["max_consecutive_losses"]
        if not (1 <= v <= 20):
            raise HTTPException(status_code=422, detail="max_consecutive_losses must be 1–20")

    merged = {**current, **updates}
    _write_settings(merged)
    return {"status": "ok", "settings": merged}


# ── POST /scalping/enable ─────────────────────────────────────────────────────

class EnablePayload(BaseModel):
    enabled: bool


@router.post("/enable")
async def toggle_scalping(
    payload: EnablePayload,
    _admin=Depends(require_admin),
):
    """Toggle the scalping system on or off without touching other settings."""
    current = _read_settings()
    current["enabled"] = payload.enabled
    _write_settings(current)
    state = "enabled" if payload.enabled else "disabled"
    return {"status": "ok", "scalping": state}


# ── GET /scalping/ml/status ───────────────────────────────────────────────────

_ML_LATEST_JSON = (
    pathlib.Path(__file__).resolve().parent.parent.parent
    / "data" / "models" / "latest.json"
)


class ScalpModelInfo(BaseModel):
    symbol: str               # e.g. "BTC/USDT:5m"  (":scalp" suffix stripped)
    model_path: str
    trained_date: Optional[str] = None
    has_short_model: bool = False


class ScalpMLStatusResponse(BaseModel):
    model_count: int
    models: list[ScalpModelInfo]
    last_retrain: Optional[str]
    outcomes_total: int
    outcomes_resolved: int
    outcomes_pending: int
    win_rate_pct: Optional[float]
    avg_pnl_pct: Optional[float]
    feedback_loop_active: bool


@router.get("/ml/status", response_model=ScalpMLStatusResponse)
async def scalp_ml_status(_user=Depends(get_current_user)):
    """Return scalping-specific ML model stats (keys ending with ':scalp' only)."""
    from db.database import AsyncSessionLocal
    from db.models import TradeOutcome
    from sqlalchemy import func as _f

    models: list[ScalpModelInfo] = []
    last_retrain: Optional[str] = None

    if _ML_LATEST_JSON.exists():
        try:
            registry: dict = json.loads(_ML_LATEST_JSON.read_text())
            mtime = datetime.fromtimestamp(_ML_LATEST_JSON.stat().st_mtime)
            last_retrain = mtime.isoformat(timespec="seconds")
            # Only scalp buy keys: ends with ":scalp" but NOT ":scalp:short"
            scalp_buy_keys = {
                k: v for k, v in registry.items()
                if k.endswith(":scalp") and v is not None
            }
            for key, path in scalp_buy_keys.items():
                p = pathlib.Path(path)
                # Filename: BTC_USDT_5m_2026-03-07_scalp_buy.pkl
                # parts[-3] = date  (buy=-1, scalp=-2, date=-3)
                parts = p.stem.split("_")
                trained_date = parts[-3] if len(parts) >= 4 else None
                display_symbol = key.removesuffix(":scalp")
                short_key = f"{key}:short"
                short_path = registry.get(short_key)
                has_short = bool(
                    short_path and pathlib.Path(short_path).exists()
                )
                models.append(ScalpModelInfo(
                    symbol=display_symbol,
                    model_path=str(p.name),
                    trained_date=trained_date,
                    has_short_model=has_short,
                ))
        except Exception:
            pass

    # Outcome stats scoped to scalp strategies
    async with AsyncSessionLocal() as session:
        total_r = await session.execute(
            select(_f.count()).select_from(TradeOutcome)
            .where(TradeOutcome.strategy_name.ilike("%scalp%"))
        )
        outcomes_total = total_r.scalar() or 0

        resolved_r = await session.execute(
            select(_f.count()).select_from(TradeOutcome)
            .where(TradeOutcome.strategy_name.ilike("%scalp%"))
            .where(TradeOutcome.resolved == True)  # noqa: E712
        )
        outcomes_resolved = resolved_r.scalar() or 0

        win_rate_pct: Optional[float] = None
        avg_pnl_pct: Optional[float] = None

        if outcomes_resolved > 0:
            wins_r = await session.execute(
                select(_f.count()).select_from(TradeOutcome)
                .where(TradeOutcome.strategy_name.ilike("%scalp%"))
                .where(TradeOutcome.resolved == True)  # noqa: E712
                .where(TradeOutcome.ml_label == 1)
            )
            wins = wins_r.scalar() or 0
            win_rate_pct = round(wins / outcomes_resolved * 100, 1)

            pnl_r = await session.execute(
                select(_f.avg(TradeOutcome.pnl_pct))
                .where(TradeOutcome.strategy_name.ilike("%scalp%"))
                .where(TradeOutcome.resolved == True)  # noqa: E712
            )
            avg_pnl_raw = pnl_r.scalar()
            if avg_pnl_raw is not None:
                avg_pnl_pct = round(float(avg_pnl_raw), 3)

    outcomes_pending = outcomes_total - outcomes_resolved

    return ScalpMLStatusResponse(
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


# ── POST /scalping/ml/retrain ─────────────────────────────────────────────────

@router.post("/ml/retrain", dependencies=[Depends(require_admin)])
async def trigger_scalp_retrain():
    """Manually trigger scalping ML retraining via Celery (admin only)."""
    try:
        from tasks.scalping_ml_retrain import retrain_scalp_models
        task = retrain_scalp_models.delay()
        return {"status": "queued", "task_id": task.id}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Celery unavailable: {exc}")
