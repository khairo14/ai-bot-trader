"""
Strategy Code API (UI-03)
==========================
CRUD + hot-reload for strategy .py files.

Endpoints
---------
GET    /api/strategy-code/registry          list all registered strategies + metadata
GET    /api/strategy-code/{name}            return raw source of a strategy file
POST   /api/strategy-code/upload            upload new .py file (multipart)
PUT    /api/strategy-code/{name}            overwrite file with raw code string
DELETE /api/strategy-code/{name}            remove file + unregister
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import os
import sys
import textwrap
import traceback
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import get_db
from db.models import Strategy as StrategyModel
from core.strategies.base import BaseStrategy
from core.auth import require_admin

router = APIRouter()

# ── Strategy file location ───────────────────────────────────────────────────
STRATEGY_DIR = Path(__file__).resolve().parent.parent.parent / "core" / "strategies"

# Built-in protected strategies (cannot be deleted)
_BUILTIN_NAMES = {"hybrid_macd_rsi", "momentum_breakout", "mean_reversion_bb"}


# ── Registry helpers ──────────────────────────────────────────────────────────

def _get_registry() -> dict:
    """Return the live STRATEGY_REGISTRY dict from signal_engine."""
    from core.engine.signal_engine import STRATEGY_REGISTRY
    return STRATEGY_REGISTRY


def _validate_and_load(source: str, strategy_name: str) -> tuple[str, type]:
    """
    Validate Python source, find BaseStrategy subclass, return (class_name, cls).
    Raises ValueError with a human-readable message on failure.
    """
    # Compile first for syntax errors
    try:
        code = compile(source, f"<{strategy_name}>", "exec")
    except SyntaxError as e:
        raise ValueError(f"Syntax error: {e}")

    # Execute in a fresh namespace
    ns: dict = {}
    try:
        exec(code, ns)
    except Exception as e:
        raise ValueError(f"Runtime error on import: {e}")

    # Find a BaseStrategy subclass
    found: list[tuple[str, type]] = []
    for attr_name, obj in ns.items():
        if (
            inspect.isclass(obj)
            and issubclass(obj, BaseStrategy)
            and obj is not BaseStrategy
        ):
            found.append((attr_name, obj))

    if not found:
        raise ValueError(
            "No class inheriting from BaseStrategy found. "
            "Your strategy must subclass BaseStrategy."
        )
    if len(found) > 1:
        names = [n for n, _ in found]
        raise ValueError(
            f"Multiple BaseStrategy subclasses found: {names}. "
            "Upload one strategy per file."
        )

    cls_name, cls = found[0]

    if not hasattr(cls, "generate_signal") or not callable(getattr(cls, "generate_signal")):
        raise ValueError(f"Class {cls_name} must implement generate_signal()")

    if not hasattr(cls, "name") or not cls.name:
        raise ValueError(f"Class {cls_name} must define a `name` class attribute.")

    return cls_name, cls


def _hot_reload(strategy_key: str, cls: type) -> None:
    """Insert / replace a class in the live STRATEGY_REGISTRY."""
    from core.engine import signal_engine
    signal_engine.STRATEGY_REGISTRY[strategy_key] = cls


def _hot_remove(strategy_key: str) -> None:
    """Remove a key from the live STRATEGY_REGISTRY."""
    from core.engine import signal_engine
    signal_engine.STRATEGY_REGISTRY.pop(strategy_key, None)


def _file_for(strategy_key: str) -> Optional[Path]:
    """Find the .py file containing the strategy, by looking for `name = "...key..."` in each file."""
    for py in STRATEGY_DIR.glob("*.py"):
        if py.name.startswith("_"):
            continue
        try:
            content = py.read_text(encoding="utf-8")
            if f'name = "{strategy_key}"' in content or f"name = '{strategy_key}'" in content:
                return py
        except Exception:
            pass
    return None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/registry")
async def list_registry(db: AsyncSession = Depends(get_db)):
    """List all registered strategies with metadata and DB usage count."""
    registry = _get_registry()

    # Count how many DB strategy rows use each strategy_name
    q = await db.execute(select(StrategyModel))
    db_strategies = q.scalars().all()
    usage: dict[str, list] = {}
    for s in db_strategies:
        usage.setdefault(s.name, []).append({"id": s.id, "name": s.name, "active": s.is_active})

    result = []
    for key, cls in registry.items():
        py_file = _file_for(key)
        result.append({
            "key":         key,
            "class_name":  cls.__name__,
            "description": getattr(cls, "description", ""),
            "asset_class": getattr(cls, "asset_class", ""),
            "broker":      getattr(cls, "broker", ""),
            "file":        py_file.name if py_file else None,
            "builtin":     key in _BUILTIN_NAMES,
            "db_usages":   usage.get(key, []),
        })

    return {"registry": result, "total": len(result)}


@router.get("/{strategy_key}", response_class=PlainTextResponse)
async def get_source(strategy_key: str):
    """Return the raw Python source of the strategy file."""
    py_file = _file_for(strategy_key)
    if py_file is None:
        raise HTTPException(status_code=404, detail=f"No file found for strategy '{strategy_key}'")
    return py_file.read_text(encoding="utf-8")


class SaveCodeRequest(BaseModel):
    code: str
    strategy_key: Optional[str] = None  # override key; defaults to cls.name


@router.put("/{strategy_key}")
async def update_source(strategy_key: str, body: SaveCodeRequest, _admin=Depends(require_admin)):
    """Overwrite the strategy file with new source and hot-reload."""
    py_file = _file_for(strategy_key)
    if py_file is None:
        raise HTTPException(status_code=404, detail=f"No file found for strategy '{strategy_key}'")

    try:
        cls_name, cls = _validate_and_load(body.code, strategy_key)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    py_file.write_text(body.code, encoding="utf-8")
    _hot_reload(strategy_key, cls)

    return {"ok": True, "strategy_key": strategy_key, "class_name": cls_name, "message": "Saved and hot-reloaded."}


@router.post("/upload")
async def upload_strategy(file: UploadFile = File(...), _admin=Depends(require_admin)):
    """
    Upload a new .py strategy file.
    The file is validated, written to core/strategies/, and registered live.
    """
    if not file.filename or not file.filename.endswith(".py"):
        raise HTTPException(status_code=400, detail="Only .py files are accepted.")

    # Prevent path traversal — only allow a plain filename, no directory separators
    safe_name = Path(file.filename).name
    if not safe_name or safe_name != file.filename or '/' in safe_name or '\\' in safe_name:
        raise HTTPException(status_code=400, detail="Invalid filename. Use a simple filename without directory separators.")

    source = (await file.read()).decode("utf-8")

    try:
        cls_name, cls = _validate_and_load(source, safe_name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    strategy_key: str = cls.name  # type: ignore[attr-defined]

    if strategy_key in _BUILTIN_NAMES:
        raise HTTPException(status_code=400, detail=f"'{strategy_key}' is a built-in strategy and cannot be overwritten via upload. Use the editor instead.")

    dest = STRATEGY_DIR / safe_name
    dest.write_text(source, encoding="utf-8")
    _hot_reload(strategy_key, cls)

    return {
        "ok":           True,
        "strategy_key": strategy_key,
        "class_name":   cls_name,
        "file":         file.filename,
        "message":      f"Strategy '{strategy_key}' uploaded and registered.",
    }


@router.delete("/{strategy_key}")
async def delete_strategy_file(strategy_key: str, db: AsyncSession = Depends(get_db), _admin=Depends(require_admin)):
    """
    Remove a strategy file and unregister it.
    Blocked if any DB strategy row references this strategy name.
    Built-in strategies cannot be deleted.
    """
    if strategy_key in _BUILTIN_NAMES:
        raise HTTPException(status_code=400, detail=f"'{strategy_key}' is a built-in strategy and cannot be deleted.")

    # Block if in use
    q = await db.execute(select(StrategyModel).where(StrategyModel.name == strategy_key))
    in_use = q.scalars().first()
    if in_use:
        raise HTTPException(
            status_code=409,
            detail=f"Strategy '{strategy_key}' is used by DB strategy '{in_use.name}' (id={in_use.id}). Remove or remap it first.",
        )

    py_file = _file_for(strategy_key)
    if py_file is None:
        raise HTTPException(status_code=404, detail=f"No file found for strategy '{strategy_key}'")

    py_file.unlink()
    _hot_remove(strategy_key)

    return {"ok": True, "strategy_key": strategy_key, "message": "Deleted and unregistered."}
