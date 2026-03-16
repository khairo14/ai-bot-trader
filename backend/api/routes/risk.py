"""
Risk management API endpoints.

GET  /api/risk/status                         — global + per-broker circuit-breaker/loss state
POST /api/risk/reset-circuit-breaker          — reset global daily circuit breaker
POST /api/risk/reset-consecutive-losses       — reset global consecutive-loss counter
POST /api/risk/{broker}/reset-circuit-breaker — reset per-broker circuit breaker
POST /api/risk/{broker}/reset-consecutive-losses — reset per-broker consecutive-loss counter

GET  /api/risk/broker-settings                — list all per-broker risk settings rows
GET  /api/risk/broker-settings/{broker}       — get one broker's settings (nulls = use global)
PUT  /api/risk/broker-settings/{broker}       — upsert per-broker risk settings
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.risk_manager import RiskManager, get_risk_manager  # BUG-2 FIX: import singleton factory
from db.database import get_db
from db.models import BrokerRiskSettings, BrokerName

router = APIRouter()

# Shared singleton so resets propagate to the same state file used by ForwardEngine
_rm = get_risk_manager()  # BUG-2 FIX: process-wide singleton — reset propagates to ForwardEngine

_VALID_BROKERS = {"binance", "alpaca", "ibkr"}
# "scalp" is a virtual isolated CB key (no DB settings row);
# it supports reset-only endpoints so scalp losses never trip the shared Binance CB.
_VALID_CB_KEYS = _VALID_BROKERS | {"scalp"}


# ── Schema ────────────────────────────────────────────────────────────────────

class BrokerRiskSettingsSchema(BaseModel):
    risk_per_trade_pct: Optional[float] = None
    max_open_positions: Optional[int] = None
    daily_circuit_breaker_pct: Optional[float] = None
    max_consecutive_losses: Optional[int] = None
    max_exposure_per_asset_pct: Optional[float] = None
    max_exposure_per_class_pct: Optional[float] = None


def _brs_dict(row: BrokerRiskSettings) -> dict:
    return {
        "broker":                    row.broker.value,
        "risk_per_trade_pct":        row.risk_per_trade_pct,
        "max_open_positions":        row.max_open_positions,
        "daily_circuit_breaker_pct": row.daily_circuit_breaker_pct,
        "max_consecutive_losses":    row.max_consecutive_losses,
        "max_exposure_per_asset_pct":row.max_exposure_per_asset_pct,
        "max_exposure_per_class_pct":row.max_exposure_per_class_pct,
        "updated_at":                row.updated_at.isoformat() if row.updated_at else None,
    }


# ── Global state ──────────────────────────────────────────────────────────────

@router.get("/status")
async def risk_status():
    """Return current risk state: global + per-broker circuit breaker and consecutive losses."""
    _rm._load_state()
    return {
        "circuit_breaker_active":   _rm.is_circuit_breaker_active(),
        "consecutive_losses":        _rm.consecutive_losses,
        "max_consecutive_losses":   _rm.max_consecutive_losses,
        "daily_circuit_breaker_pct": _rm.daily_circuit_breaker_pct * 100,
        "per_broker": {
            broker: {
                "circuit_breaker_active": state.get("circuit_breaker_active", False),
                "consecutive_losses":     state.get("consecutive_losses", 0),
            }
            for broker, state in _rm._per_broker.items()
        },
    }


@router.post("/reset-circuit-breaker")
async def reset_circuit_breaker():
    """Manually reset the portfolio-wide daily circuit breaker."""
    _rm.reset_circuit_breaker()
    return {"message": "Circuit breaker reset. Trading is now allowed for today."}


@router.post("/reset-consecutive-losses")
async def reset_consecutive_losses():
    """Manually reset the portfolio-wide consecutive-loss counter."""
    _rm.reset_consecutive_losses()
    return {"message": "Consecutive-loss counter reset to 0."}


# ── Per-strategy state resets ─────────────────────────────────────────────────

@router.post("/strategy/{strategy_name}/reset-circuit-breaker")
async def reset_strategy_circuit_breaker(strategy_name: str):
    """Reset the per-strategy circuit breaker and consecutive-loss counter."""
    _rm.reset_strategy_circuit_breaker(strategy_name)
    return {"message": f"Strategy '{strategy_name}' circuit breaker reset. Trading is now allowed."}


@router.get("/strategy/status")
async def strategy_risk_status():
    """Return all per-strategy circuit-breaker and consecutive-loss states."""
    _rm._load_state()
    return {
        name: {
            "circuit_breaker_active":         s.get("circuit_breaker_active", False),
            "consecutive_losses":             s.get("consecutive_losses", 0),
            "circuit_breaker_date":           s.get("circuit_breaker_date"),
            "max_consecutive_losses_effective": s.get("max_consecutive_losses_effective"),
        }
        for name, s in _rm._per_strategy.items()
    }


# ── Per-broker state resets ───────────────────────────────────────────────────

@router.post("/{broker}/reset-circuit-breaker")
async def reset_broker_circuit_breaker(broker: str):
    """Reset the circuit breaker for one specific broker (or the scalp virtual CB key)."""
    if broker not in _VALID_CB_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown broker '{broker}'. Valid: binance, alpaca, ibkr, scalp")
    _rm.reset_broker_circuit_breaker(broker)
    return {"message": f"{broker} circuit breaker reset. Trading is now allowed for this broker."}


@router.post("/{broker}/reset-consecutive-losses")
async def reset_broker_consecutive_losses(broker: str):
    """Reset the consecutive-loss counter for one specific broker (or the scalp virtual CB key)."""
    if broker not in _VALID_CB_KEYS:
        raise HTTPException(status_code=400, detail=f"Unknown broker '{broker}'. Valid: binance, alpaca, ibkr, scalp")
    _rm.reset_broker_consecutive_losses(broker)
    return {"message": f"{broker} consecutive-loss counter reset to 0."}


# ── Per-broker risk settings CRUD ─────────────────────────────────────────────

@router.get("/broker-settings")
async def list_broker_settings(db: AsyncSession = Depends(get_db)):
    """Return all configured per-broker risk settings rows."""
    q = await db.execute(select(BrokerRiskSettings))
    rows = q.scalars().all()
    return [_brs_dict(r) for r in rows]


@router.get("/broker-settings/{broker}")
async def get_broker_settings(broker: str, db: AsyncSession = Depends(get_db)):
    """Return settings for one broker (all nulls = using global config defaults)."""
    if broker not in _VALID_BROKERS:
        raise HTTPException(status_code=400, detail=f"Unknown broker '{broker}'")
    q = await db.execute(
        select(BrokerRiskSettings).where(BrokerRiskSettings.broker == BrokerName(broker))
    )
    row = q.scalar_one_or_none()
    if row is None:
        return {
            "broker": broker,
            "risk_per_trade_pct": None, "max_open_positions": None,
            "daily_circuit_breaker_pct": None, "max_consecutive_losses": None,
            "max_exposure_per_asset_pct": None, "max_exposure_per_class_pct": None,
            "updated_at": None,
        }
    return _brs_dict(row)


@router.put("/broker-settings/{broker}")
async def upsert_broker_settings(
    broker: str,
    body: BrokerRiskSettingsSchema,
    db: AsyncSession = Depends(get_db),
):
    """
    Create or update per-broker risk settings.
    Set any field to null to fall back to the global config default.
    """
    if broker not in _VALID_BROKERS:
        raise HTTPException(status_code=400, detail=f"Unknown broker '{broker}'")
    q = await db.execute(
        select(BrokerRiskSettings).where(BrokerRiskSettings.broker == BrokerName(broker))
    )
    row = q.scalar_one_or_none()
    if row is None:
        row = BrokerRiskSettings(broker=BrokerName(broker))
        db.add(row)
    row.risk_per_trade_pct        = body.risk_per_trade_pct
    row.max_open_positions         = body.max_open_positions
    row.daily_circuit_breaker_pct  = body.daily_circuit_breaker_pct
    row.max_consecutive_losses     = body.max_consecutive_losses
    row.max_exposure_per_asset_pct = body.max_exposure_per_asset_pct
    row.max_exposure_per_class_pct = body.max_exposure_per_class_pct
    await db.commit()
    await db.refresh(row)
    return _brs_dict(row)

