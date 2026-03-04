from brokers.base import AbstractBroker
from brokers.binance_client import BinanceClient
from brokers.alpaca_client import AlpacaClient
from brokers.ibkr_client import IBKRClient

__all__ = ["AbstractBroker", "BinanceClient", "AlpacaClient", "IBKRClient"]


def get_broker(broker_name: str) -> AbstractBroker:
    """Factory function: returns the correct broker client by name."""
    brokers = {
        "binance": BinanceClient,
        "alpaca": AlpacaClient,
        "ibkr": IBKRClient,
    }
    if broker_name not in brokers:
        raise ValueError(f"Unknown broker: '{broker_name}'. Choose from: {list(brokers.keys())}")
    return brokers[broker_name]()
