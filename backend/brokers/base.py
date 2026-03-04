from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Callable
import pandas as pd


@dataclass
class OrderResult:
    order_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    status: str
    raw: dict


@dataclass
class Position:
    symbol: str
    side: str           # long | short
    quantity: float
    entry_price: float
    current_price: float
    unrealized_pnl: float
    asset_class: str


@dataclass
class Balance:
    total: float
    available: float
    currency: str = "USD"


class AbstractBroker(ABC):
    """
    Unified interface all broker connectors must implement.
    Strategies and execution engine only interact with this interface —
    never directly with broker-specific code.
    """

    name: str = "abstract"
    asset_class: str = "unknown"

    # ── Market Data ──────────────────────────────────────

    @abstractmethod
    async def get_price(self, symbol: str) -> float:
        """Get current market price for a symbol."""
        ...

    @abstractmethod
    async def get_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 500,
        since: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV candles.
        Returns DataFrame with columns: [timestamp, open, high, low, close, volume]
        """
        ...

    @abstractmethod
    async def get_orderbook(self, symbol: str) -> dict:
        """
        Get current orderbook.
        Returns: {"bids": [[price, qty], ...], "asks": [[price, qty], ...]}
        """
        ...

    # ── Account ──────────────────────────────────────────

    @abstractmethod
    async def get_balance(self) -> Balance:
        """Get account balance."""
        ...

    @abstractmethod
    async def get_positions(self) -> List[Position]:
        """Get all currently open positions."""
        ...

    # ── Order Management ─────────────────────────────────

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "market",
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        **kwargs,
    ) -> OrderResult:
        """Place an order. order_type: market | limit | stop | stop_limit | bracket"""
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel an open order. Returns True on success."""
        ...

    @abstractmethod
    async def get_order_status(self, order_id: str, symbol: str) -> OrderResult:
        """Get current status of an order."""
        ...

    # ── Streaming ────────────────────────────────────────

    @abstractmethod
    async def stream_prices(
        self,
        symbols: List[str],
        callback: Callable,
    ) -> None:
        """
        Subscribe to live price stream.
        callback(symbol, price) is called on every price update.
        """
        ...

    # ── Connection ────────────────────────────────────────

    async def connect(self) -> None:
        """Optional: establish connection before use (e.g. IBKR TWS). No-op by default."""
        pass

    # ── Helpers ──────────────────────────────────────────

    def is_paper(self) -> bool:
        """Returns True if this connector is in paper/testnet mode."""
        return getattr(self, "_paper", True)
