from brokers.base import AbstractBroker
from brokers.binance_client import BinanceClient
from brokers.alpaca_client import AlpacaClient
from brokers.ibkr_client import IBKRClient
from config import settings

__all__ = ["AbstractBroker", "BinanceClient", "AlpacaClient", "IBKRClient",
           "get_broker", "get_broker_modes", "set_broker_mode"]

# ── Runtime broker mode store ─────────────────────────────────────────────
# Initialised from .env; can be toggled at runtime via the API without restart.
# "paper" → use paper/testnet credentials & endpoints
# "live"  → use live credentials & endpoints
_BROKER_MODES: dict[str, str] = {
    "binance": "paper" if settings.binance_testnet else "live",
    "alpaca":  "paper" if "paper-api" in settings.alpaca_base_url else "live",
    "ibkr":    "paper" if settings.ibkr_paper else "live",
}


def get_broker_modes() -> dict[str, str]:
    """Return current paper/live mode for every broker."""
    return dict(_BROKER_MODES)


def set_broker_mode(broker: str, mode: str) -> None:
    """Switch a broker between 'paper' and 'live' at runtime (no restart needed).
    New broker instances created by get_broker() will use the updated mode."""
    if broker not in _BROKER_MODES:
        raise ValueError(f"Unknown broker '{broker}'. Choose from: {list(_BROKER_MODES)}")
    if mode not in ("paper", "live"):
        raise ValueError("mode must be 'paper' or 'live'")
    _BROKER_MODES[broker] = mode


def get_broker(broker_name: str, force_paper: bool | None = None) -> AbstractBroker:
    """Factory: instantiate the correct broker client, wiring in the current mode.
    Pass force_paper=False to always get a live/data client (e.g. for backtesting)."""
    paper = _BROKER_MODES.get(broker_name, "paper") == "paper"
    if force_paper is not None:
        paper = force_paper
    factories = {
        "binance": lambda: BinanceClient(paper=paper),
        "alpaca":  lambda: AlpacaClient(paper=paper),
        "ibkr":    lambda: IBKRClient(paper=paper),
    }
    if broker_name not in factories:
        raise ValueError(f"Unknown broker: '{broker_name}'. Choose from: {list(factories)}")
    return factories[broker_name]()
