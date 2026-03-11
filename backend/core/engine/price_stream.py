"""
Real-time price streaming cache for the SL/TP monitor.

Maintains one background asyncio Task per broker that calls `stream_prices()`
and caches the latest mid-price per symbol.  `monitor_sl_tp` uses the cache
first, falling back to REST `get_bid_ask()` if no streamed price is available.

Symbol lists are refreshed every RESUB_INTERVAL seconds so newly opened trades
are automatically included without a server restart.  A dead stream task is
restarted after RECONNECT_DELAY seconds.

Lifecycle
---------
    Instantiate once at module level → `from core.engine.price_stream import price_stream_manager`
    Start in main.py lifespan:        `asyncio.create_task(price_stream_manager.start(AsyncSessionLocal))`
    Cancel that task on shutdown — the manager propagates cancellation to each broker task.
"""

import asyncio
from typing import Optional

from loguru import logger


class PriceStreamManager:
    """
    One background streaming task per active broker.
    Thread-safe reads via Python GIL (dict assignment is atomic).
    """

    # How often to refresh the symbol list from the DB (adds new trades dynamically)
    RESUB_INTERVAL: int = 30       # seconds
    # How long to wait before reconnecting a dead stream
    RECONNECT_DELAY: int = 5       # seconds

    def __init__(self) -> None:
        # symbol → latest streamed mid-price (updated from callback)
        self._prices: dict[str, float] = {}
        # broker_name → running stream Task
        self._tasks: dict[str, asyncio.Task] = {}
        # broker_name → current subscribed symbol set (detect changes)
        self._symbols: dict[str, frozenset] = {}

    # ──────────────────────────────────────────────────────────────────────────

    def get_price(self, symbol: str) -> Optional[float]:
        """Return the latest streamed mid-price, or None if not yet received."""
        return self._prices.get(symbol)

    # ──────────────────────────────────────────────────────────────────────────

    async def start(self, db_session_factory) -> None:
        """
        Long-running coroutine.  Refreshes subscriptions every RESUB_INTERVAL s.
        Designed to be run as an asyncio.Task in the FastAPI lifespan.
        """
        logger.info("[PriceStream] Manager started.")
        while True:
            try:
                await self._refresh_subscriptions(db_session_factory)
            except asyncio.CancelledError:
                logger.info("[PriceStream] Manager cancelled — stopping all streams.")
                for task in self._tasks.values():
                    task.cancel()
                await asyncio.gather(*self._tasks.values(), return_exceptions=True)
                return
            except Exception as exc:
                logger.debug(f"[PriceStream] Subscription refresh error: {exc}")
            await asyncio.sleep(self.RESUB_INTERVAL)

    # ──────────────────────────────────────────────────────────────────────────

    async def _refresh_subscriptions(self, db_session_factory) -> None:
        """Query open trades and update stream tasks accordingly."""
        from db.models import Trade, LiveTrade, OrderStatus
        from sqlalchemy import select

        async with db_session_factory() as session:
            paper_q = await session.execute(
                select(Trade.broker, Trade.symbol).where(
                    Trade.status == OrderStatus.OPEN
                ).distinct()
            )
            live_q = await session.execute(
                select(LiveTrade.broker, LiveTrade.symbol).where(
                    LiveTrade.status == OrderStatus.OPEN
                ).distinct()
            )
            rows = list(paper_q.all()) + list(live_q.all())

        # Group symbols by broker
        _raw: dict[str, set[str]] = {}
        for row in rows:
            bn = row.broker.value if hasattr(row.broker, "value") else str(row.broker)
            _raw.setdefault(bn, set()).add(row.symbol)
        broker_symbols: dict[str, frozenset] = {k: frozenset(v) for k, v in _raw.items()}

        # Start or restart tasks where the symbol list changed or task died
        for broker_name, symbols in broker_symbols.items():
            running_task = self._tasks.get(broker_name)
            needs_restart = (
                running_task is None
                or running_task.done()
                or self._symbols.get(broker_name) != symbols
            )
            if needs_restart:
                if running_task and not running_task.done():
                    running_task.cancel()
                    try:
                        await running_task
                    except (asyncio.CancelledError, Exception):
                        pass
                self._symbols[broker_name] = symbols
                self._tasks[broker_name] = asyncio.create_task(
                    self._stream_worker(broker_name, list(symbols)),
                    name=f"price_stream_{broker_name}",
                )
                logger.info(
                    f"[PriceStream] Subscribed broker={broker_name} symbols={list(symbols)}"
                )

        # Cancel tasks for brokers that no longer have open trades
        for broker_name in list(self._tasks):
            if broker_name not in broker_symbols:
                self._tasks[broker_name].cancel()
                del self._tasks[broker_name]
                self._symbols.pop(broker_name, None)
                logger.info(f"[PriceStream] Unsubscribed broker={broker_name} (no open trades)")

    # ──────────────────────────────────────────────────────────────────────────

    async def _stream_worker(self, broker_name: str, symbols: list[str]) -> None:
        """
        Background task per broker.  Calls broker.stream_prices() and populates
        self._prices.  Reconnects on error.

        Always uses the live (non-paper) broker for the WebSocket connection so
        that real market prices are used for SL/TP evaluation even when all open
        trades are paper/testnet trades.  Binance testnet WebSocket is unreliable;
        the live feed provides accurate prices for both paper and live monitoring.
        """
        from brokers import get_broker

        while True:
            try:
                # force_paper=False: always stream from the live market feed.
                # Paper trade SL/TP should be evaluated against real prices.
                broker = get_broker(broker_name, force_paper=False)
                await broker.connect()

                async def _on_price(symbol: str, price: float) -> None:
                    if price > 0:
                        self._prices[symbol] = price

                await broker.stream_prices(symbols, _on_price)

            except asyncio.CancelledError:
                logger.info(f"[PriceStream] Stream task cancelled: broker={broker_name}")
                return
            except Exception as exc:
                logger.warning(
                    f"[PriceStream] Stream error broker={broker_name}: {exc} "
                    f"— reconnecting in {self.RECONNECT_DELAY}s"
                )
                await asyncio.sleep(self.RECONNECT_DELAY)


# Module-level singleton — imported by forward_engine and main.py
price_stream_manager = PriceStreamManager()
