"""
Data fetcher: retrieves historical OHLCV data from broker connectors.

Usage:
    from data.fetcher import DataFetcher
    fetcher = DataFetcher("binance")
    df = await fetcher.get_ohlcv("BTC/USDT", "1h", limit=200)
"""
import pandas as pd
from typing import Optional
from brokers import get_broker


class DataFetcher:
    """Thin wrapper around broker.get_ohlcv that returns a pandas DataFrame."""

    def __init__(self, broker_name: str):
        self.broker = get_broker(broker_name)

    async def get_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 200,
        since: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Fetch OHLCV candles and return as a DataFrame with columns:
        timestamp, open, high, low, close, volume
        """
        await self.broker.connect()  # no-op for Binance/Alpaca; ensures IBKR is live
        raw = await self.broker.get_ohlcv(symbol, timeframe, limit, since)
        # Broker clients return a DataFrame directly — don't re-wrap it.
        if isinstance(raw, pd.DataFrame):
            return raw
        # Legacy path: list of lists/tuples
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        df = df.astype(float)
        return df

    async def get_multi(
        self,
        symbols: list[str],
        timeframe: str = "1h",
        limit: int = 200,
    ) -> dict[str, pd.DataFrame]:
        """Fetch OHLCV for multiple symbols concurrently, return dict keyed by symbol."""
        import asyncio
        import logging

        async def _fetch_one(sym: str) -> tuple[str, pd.DataFrame | None]:
            try:
                return sym, await self.get_ohlcv(sym, timeframe, limit)
            except Exception as e:
                logging.getLogger(__name__).warning(f"Failed to fetch {sym}: {e}")
                return sym, None

        results = await asyncio.gather(*(_fetch_one(s) for s in symbols))
        return {sym: df for sym, df in results if df is not None}
