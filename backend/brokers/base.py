from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Callable
import asyncio
import pandas as pd


@dataclass
class OrderResult:
    order_id: str
    symbol: str
    side: str
    quantity: float
    price: float          # submitted/limit price
    status: str
    raw: dict
    fill_price: Optional[float] = None   # actual fill price once confirmed


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

    async def get_bid_ask(self, symbol: str) -> tuple[float, float]:
        """
        Get current bid and ask prices for a symbol.
        Returns (bid, ask).  Used by monitor_sl_tp for directionally-correct
        SL/TP checks: SHORT exits are filled at the ask, LONG exits at the bid.
        Broker subclasses override this for accuracy; default falls back to
        get_price() and returns the same value for both sides.
        """
        price = await self.get_price(symbol)
        return price, price

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

    async def close(self) -> None:
        """Release any open network sessions. Override in subclasses that hold async connections."""
        pass

    async def get_ohlcv_range(
        self,
        symbol: str,
        timeframe: str,
        start_dt: datetime,
        end_dt: datetime,
        batch_size: int = 1000,
    ) -> pd.DataFrame:
        """
        Fetch OHLCV candles across a date range by paginating requests.
        Brokers cap single requests (Binance = 1000 candles), so this loops
        using the `since` cursor until end_dt is covered.
        """
        since_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)
        chunks: list[pd.DataFrame] = []

        while True:
            chunk = await self.get_ohlcv(symbol, timeframe, limit=batch_size, since=since_ms)
            if chunk.empty:
                break
            chunks.append(chunk)
            # Advance cursor to 1 ms after the last candle timestamp
            last_ts = int(chunk.index[-1].timestamp() * 1000)
            if last_ts >= end_ms:
                break
            if last_ts <= since_ms:
                # No progress — avoid infinite loop
                break
            since_ms = last_ts + 1
            await asyncio.sleep(0.05)  # be nice to the rate limiter

        if not chunks:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = pd.concat(chunks)
        df = df[~df.index.duplicated(keep="first")]
        df.sort_index(inplace=True)
        start_ts = pd.Timestamp(start_dt, tz="UTC") if start_dt.tzinfo is None else pd.Timestamp(start_dt).tz_convert("UTC")
        end_ts   = pd.Timestamp(end_dt,   tz="UTC") if end_dt.tzinfo   is None else pd.Timestamp(end_dt).tz_convert("UTC")
        # Ensure index is UTC-aware for comparison
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df = df[(df.index >= start_ts) & (df.index <= end_ts)]
        return df
