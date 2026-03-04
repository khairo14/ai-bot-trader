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
        raw = await self.broker.get_ohlcv(symbol, timeframe, limit, since)
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
        """Fetch OHLCV for multiple symbols, return dict keyed by symbol."""
        result = {}
        for sym in symbols:
            try:
                result[sym] = await self.get_ohlcv(sym, timeframe, limit)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"Failed to fetch {sym}: {e}")
        return result
