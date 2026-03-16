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
        .where(Signal.strategy_name.like("scalp_%"))
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
                TradeOutcome.strategy_name.like("scalp_%"),
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
                Signal.strategy_name.like("scalp_%"),
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
    enabled:            Optional[bool]  = None
    timeframe:          Optional[str]   = None
    risk_per_trade_pct: Optional[float] = None
    sl_atr_mult:        Optional[float] = None
    tp_atr_mult:        Optional[float] = None
    min_volume_ratio:   Optional[float] = None
    max_spread_pct:     Optional[float] = None
    min_score:          Optional[int]   = None
    ml_veto_threshold:  Optional[float] = None
    sr_tp_snap:         Optional[bool]  = None
    session_filter:     Optional[dict]  = None


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
