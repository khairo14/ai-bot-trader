"""
Regime Router Settings API
===========================
GET  /api/settings/regime  — read current global regime router settings
PUT  /api/settings/regime  — update settings (enabled, hysteresis_candles)
"""

import json
import pathlib
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter()

_SETTINGS_PATH = pathlib.Path(__file__).resolve().parent.parent.parent / "runtime" / "regime_settings.json"

_DEFAULTS = {
    "enabled": True,
    "hysteresis_candles": 3,
}


def _load() -> dict:
    try:
        if _SETTINGS_PATH.exists():
            data = json.loads(_SETTINGS_PATH.read_text())
            return {**_DEFAULTS, **data}
    except (json.JSONDecodeError, OSError):
        pass
    return _DEFAULTS.copy()


def _save(data: dict) -> None:
    try:
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SETTINGS_PATH.write_text(json.dumps(data, indent=2))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not save regime settings: {exc}")


class RegimeSettingsSchema(BaseModel):
    enabled: Optional[bool] = None
    hysteresis_candles: Optional[int] = None


@router.get("")
async def get_regime_settings():
    """Return current global regime router settings."""
    return _load()


@router.put("")
async def update_regime_settings(payload: RegimeSettingsSchema):
    """Update global regime router settings."""
    current = _load()
    if payload.enabled is not None:
        current["enabled"] = payload.enabled
    if payload.hysteresis_candles is not None:
        if not (1 <= payload.hysteresis_candles <= 20):
            raise HTTPException(status_code=422, detail="hysteresis_candles must be between 1 and 20")
        current["hysteresis_candles"] = payload.hysteresis_candles
    _save(current)
    return current
