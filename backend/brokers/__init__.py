import json
import os

from brokers.base import AbstractBroker
from brokers.binance_client import BinanceClient
from brokers.alpaca_client import AlpacaClient
from brokers.ibkr_client import IBKRClient
from config import settings

__all__ = ["AbstractBroker", "BinanceClient", "AlpacaClient", "IBKRClient",
           "get_broker", "get_broker_modes", "set_broker_mode", "reload_broker_modes"]

# ── Singleton client cache — avoids re-loading Binance markets on every tick ──
_BROKER_SINGLETONS: dict[str, AbstractBroker] = {}

# ── Persistent broker mode file ───────────────────────────────────────────────
# Both the FastAPI process and the Celery worker process load from this file on
# startup (and on explicit reload).  The FastAPI process writes to it whenever
# the UI toggle calls set_broker_mode().  This way, a restart of any process
# picks up the last-saved preference rather than reverting to .env defaults.
_MODES_FILE = os.path.join(os.path.dirname(__file__), "..", "runtime", "broker_modes.json")

# .env-derived defaults — used when no persisted file exists yet.
_ENV_DEFAULTS: dict[str, str] = {
    "binance": "paper" if settings.binance_testnet else "live",
    "alpaca":  "paper" if "paper-api" in settings.alpaca_base_url else "live",
    "ibkr":    "paper" if settings.ibkr_paper else "live",
}


def _load_modes_from_file() -> dict[str, str]:
    """Read broker modes from the runtime JSON file, falling back to .env defaults."""
    modes = dict(_ENV_DEFAULTS)
    try:
        if os.path.exists(_MODES_FILE):
            with open(_MODES_FILE, "r") as fh:
                saved: dict = json.load(fh)
            for broker, mode in saved.items():
                if broker in modes and mode in ("paper", "live"):
                    modes[broker] = mode
    except Exception:
        pass  # corrupt or missing file → .env defaults are fine
    return modes


def _save_modes_to_file() -> None:
    """Persist the current _BROKER_MODES dict to disk (non-fatal on failure)."""
    try:
        os.makedirs(os.path.dirname(_MODES_FILE), exist_ok=True)
        with open(_MODES_FILE, "w") as fh:
            json.dump(_BROKER_MODES, fh, indent=2)
    except Exception:
        pass


# ── Runtime broker mode store ─────────────────────────────────────────────
# Loaded from runtime/broker_modes.json if it exists, otherwise from .env.
# Call reload_broker_modes() at the top of long-running tasks (e.g. signal_runner)
# to pick up changes made via the API without requiring a process restart.
_BROKER_MODES: dict[str, str] = _load_modes_from_file()


def get_broker_modes() -> dict[str, str]:
    """Return current paper/live mode for every broker."""
    return dict(_BROKER_MODES)


def reload_broker_modes() -> None:
    """Re-read broker_modes.json from disk and update the in-process dict.
    Call this at the start of each Celery task so the worker picks up mode
    changes made via the UI (FastAPI) without needing a full restart."""
    fresh = _load_modes_from_file()
    for broker, mode in fresh.items():
        if _BROKER_MODES.get(broker) != mode:
            _BROKER_MODES[broker] = mode
            invalidate_broker_cache(broker)


def set_broker_mode(broker: str, mode: str) -> None:
    """Switch a broker between 'paper' and 'live' at runtime (no restart needed).
    Persists the change to runtime/broker_modes.json so that Celery workers
    pick it up on their next task run (via reload_broker_modes) or restart."""
    if broker not in _BROKER_MODES:
        raise ValueError(f"Unknown broker '{broker}'. Choose from: {list(_BROKER_MODES)}")
    if mode not in ("paper", "live"):
        raise ValueError("mode must be 'paper' or 'live'")
    _BROKER_MODES[broker] = mode
    invalidate_broker_cache(broker)
    _save_modes_to_file()  # ← persist so Celery picks up the change


def get_broker(broker_name: str, force_paper: bool | None = None) -> AbstractBroker:
    """Return a singleton broker client (F-060).
    Re-creates if the mode has changed since last access."""
    paper = _BROKER_MODES.get(broker_name, "paper") == "paper"
    if force_paper is not None:
        paper = force_paper

    # Cache key encodes broker + mode so switching paper→live gives a fresh instance
    cache_key = f"{broker_name}:{'paper' if paper else 'live'}"
    existing = _BROKER_SINGLETONS.get(cache_key)
    if existing is not None:
        return existing

    factories = {
        "binance": lambda: BinanceClient(paper=paper),
        "alpaca":  lambda: AlpacaClient(paper=paper),
        "ibkr":    lambda: IBKRClient(paper=paper),
    }
    if broker_name not in factories:
        raise ValueError(f"Unknown broker: '{broker_name}'. Choose from: {list(factories)}")

    instance = factories[broker_name]()
    _BROKER_SINGLETONS[cache_key] = instance
    return instance


def invalidate_broker_cache(broker_name: str | None = None) -> None:
    """Evict cached broker client(s). Call after mode change or credential update."""
    if broker_name is None:
        _BROKER_SINGLETONS.clear()
    else:
        for key in list(_BROKER_SINGLETONS):
            if key.startswith(f"{broker_name}:"):
                del _BROKER_SINGLETONS[key]
