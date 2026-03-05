"""
Risk management API endpoints.

GET  /api/risk/status                  — current circuit-breaker + consecutive-loss state
POST /api/risk/reset-circuit-breaker   — manually clear the daily circuit breaker
POST /api/risk/reset-consecutive-losses — manually clear the consecutive-loss counter
"""

from fastapi import APIRouter
from core.risk_manager import RiskManager

router = APIRouter()

# Shared singleton so resets propagate to the same state file used by ForwardEngine
_rm = RiskManager()


@router.get("/status")
async def risk_status():
    """Return current risk state (circuit breaker + consecutive losses)."""
    _rm._load_state()  # refresh from disk in case another process updated it
    return {
        "circuit_breaker_active": _rm.is_circuit_breaker_active(),
        "consecutive_losses": _rm.consecutive_losses,
        "max_consecutive_losses": _rm.max_consecutive_losses,
        "daily_circuit_breaker_pct": _rm.daily_circuit_breaker_pct * 100,
    }


@router.post("/reset-circuit-breaker")
async def reset_circuit_breaker():
    """Manually reset the daily circuit breaker so trading can resume."""
    _rm.reset_circuit_breaker()
    return {"message": "Circuit breaker reset. Trading is now allowed for today."}


@router.post("/reset-consecutive-losses")
async def reset_consecutive_losses():
    """Manually reset the consecutive-loss counter (e.g., after manual review)."""
    _rm.reset_consecutive_losses()
    return {"message": "Consecutive-loss counter reset to 0."}
