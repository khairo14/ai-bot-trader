"""
Live price streamer: coordinates WebSocket streams from brokers
and broadcasts to connected frontend clients.

Architecture:
    Broker WebSocket → PriceStreamer → FastAPI WebSocket → Frontend
"""
import asyncio
import json
import logging
from typing import Any, Callable, Dict, Awaitable

logger = logging.getLogger(__name__)

PriceCallback = Callable[..., Any]


class PriceStreamer:
    """
    Manages live price subscriptions and fan-out to registered callbacks.
    Typically one global instance per FastAPI app.
    """

    def __init__(self):
        self._subscriptions: Dict[str, list[PriceCallback]] = {}
        self._tasks: list[asyncio.Task] = []

    def subscribe(self, symbol: str, callback: PriceCallback):
        """Register a callback to receive prices for symbol."""
        self._subscriptions.setdefault(symbol, []).append(callback)

    def unsubscribe(self, symbol: str, callback: PriceCallback):
        if symbol in self._subscriptions:
            self._subscriptions[symbol] = [
                cb for cb in self._subscriptions[symbol] if cb is not callback
            ]

    async def broadcast(self, symbol: str, price: float):
        """Fan out price update to all subscribers for symbol."""
        for cb in self._subscriptions.get(symbol, []):
            try:
                await cb(symbol, price)
            except Exception as e:
                logger.debug(f"Callback error for {symbol}: {e}")

    async def start_broker_stream(self, broker_name: str, symbols: list[str]):
        """Connect to broker WebSocket and start streaming prices."""
        from brokers import get_broker
        broker = get_broker(broker_name)

        async def _on_price(symbol: str, price: float):
            await self.broadcast(symbol, price)

        task = asyncio.create_task(
            broker.stream_prices(symbols, _on_price),
            name=f"stream-{broker_name}",
        )
        self._tasks.append(task)
        logger.info(f"Started price stream for {broker_name}: {symbols}")

    async def stop_all(self):
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()


# Singleton used by FastAPI app
price_streamer = PriceStreamer()
