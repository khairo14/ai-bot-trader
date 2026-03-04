import asyncio
import random
import pandas as pd
from typing import List, Optional, Callable
from loguru import logger

from ib_insync import IB, Stock, Option, Contract, MarketOrder, LimitOrder, StopLimitOrder, Trade as IBTrade

from config import settings
from brokers.base import AbstractBroker, OrderResult, Position, Balance


import asyncio
import threading
import time
import pandas as pd
from typing import List, Optional, Callable
from loguru import logger

from ib_insync import IB, Stock, Option, Contract, MarketOrder, LimitOrder, StopLimitOrder, Trade as IBTrade

from config import settings
from brokers.base import AbstractBroker, OrderResult, Position, Balance


# ── Persistent singleton IBKR connection ─────────────────────────────────────
#
# Problem: ib_insync requires its own asyncio event loop.  Calling
# ibkr_balance_sync() from FastAPI used to spin up a *new* event loop +
# IB connection on every request, producing the cascade of connect/disconnect
# cycles visible in IB Gateway logs.
#
# Fix: one dedicated background thread hosts a permanent asyncio loop that
# keeps a single IB instance connected.  Balance reads are cached for
# _CACHE_TTL seconds, so browser refreshes never trigger a new connection.

class _IBKRManager:
    _CACHE_TTL     = 60.0   # return cached balance for up to 60 s
    _SETTLE_SECS   = 2.0    # wait after connect for Gateway to push account data
    _CONNECT_TIMEOUT = 10   # seconds for connectAsync

    def __init__(self) -> None:
        self._ib: Optional[IB] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._cached: Optional[Balance] = None
        self._cache_ts: float = 0.0
        self._started = False

    # ── background thread / loop ─────────────────────────────────────────────

    def _start(self) -> None:
        """Lazily start the background thread (idempotent)."""
        with self._lock:
            if self._started:
                return
            self._started = True
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="ibkr-bg"
        )
        self._thread.start()

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro) -> Balance:
        """Run a coroutine on the background loop and block until done."""
        assert self._loop is not None
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)  # type: ignore[arg-type]
        return fut.result(timeout=30)

    # ── connection helpers ────────────────────────────────────────────────────

    async def _ensure_connected(self) -> bool:
        if self._ib is None:
            self._ib = IB()
        if self._ib.isConnected():
            return True
        try:
            await self._ib.connectAsync(
                host=settings.ibkr_host,
                port=settings.ibkr_port,
                clientId=settings.ibkr_client_id,
                timeout=self._CONNECT_TIMEOUT,
            )
            # Gateway pushes account data asynchronously — wait for it
            await asyncio.sleep(self._SETTLE_SECS)
            logger.info(
                f"[IBKR] Persistent connection established "
                f"(clientId={settings.ibkr_client_id}, port={settings.ibkr_port})"
            )
            return True
        except Exception as exc:
            logger.warning(f"[IBKR] Connect failed: {exc}")
            return False

    # ── balance fetch ─────────────────────────────────────────────────────────

    async def _fetch_async(self) -> Balance:
        fallback = self._cached or Balance(total=0.0, available=0.0, currency="USD")
        if not await self._ensure_connected():
            return fallback

        total = available = 0.0
        assert self._ib is not None
        for v in self._ib.accountValues():
            if v.tag == "NetLiquidation" and v.currency in ("USD", "BASE"):
                total = float(v.value)
            if v.tag == "AvailableFunds" and v.currency in ("USD", "BASE"):
                available = float(v.value)

        balance = Balance(total=total, available=available, currency="USD")
        self._cached = balance
        self._cache_ts = time.monotonic()
        logger.debug(f"[IBKR] Balance refreshed: total={total} available={available}")
        return balance

    # ── public API ────────────────────────────────────────────────────────────

    def get_balance(self) -> Balance:
        """
        Return IBKR balance.  Uses a 60-second cache so that every page
        refresh does NOT create a new Gateway connection.
        """
        self._start()
        if self._cached and (time.monotonic() - self._cache_ts) < self._CACHE_TTL:
            return self._cached
        try:
            return self._submit(self._fetch_async())
        except Exception as exc:
            logger.warning(f"[IBKR] Balance refresh failed: {exc}")
            return self._cached or Balance(total=0.0, available=0.0, currency="USD")

    def is_connected(self) -> bool:
        return bool(self._ib and self._ib.isConnected())


_manager = _IBKRManager()


def ibkr_balance_sync() -> Balance:
    """
    Thread-safe entry-point used by portfolio route.
    Returns a cached or freshly fetched IBKR balance WITHOUT spawning a new
    IB connection on every call.
    """
    return _manager.get_balance()


class IBKRClient(AbstractBroker):
    """
    Interactive Brokers connector via ib_insync.
    Supports: US Stocks, Options, and global markets.

    IMPORTANT: Requires IB Gateway or TWS to be running locally.
    Paper trading port: 7497
    Live trading port:  7496

    IB Gateway must be running and logged in BEFORE the bot starts.
    See docs/brokers.md for full setup instructions.
    """

    name = "ibkr"
    asset_class = "stock_options"

    def __init__(self, paper: bool | None = None):
        self._paper = paper if paper is not None else settings.ibkr_paper
        self._port = settings.ibkr_port if self._paper else settings.ibkr_port_live
        self.ib = IB()
        self._connected = False
        mode = "PAPER" if self._paper else "LIVE"
        logger.info(f"IBKRClient initialized in {mode} mode (not yet connected).")

    async def connect(self):
        """Connect to IB Gateway. Must be called before any other method."""
        if not self._connected:
            await self.ib.connectAsync(
                host=settings.ibkr_host,
                port=self._port,
                clientId=settings.ibkr_client_id,
            )
            self._connected = True
            mode = "PAPER" if self._paper else "LIVE"
            logger.info(f"[IBKR] Connected to IB Gateway at {settings.ibkr_host}:{self._port} ({mode})")

    async def disconnect(self):
        if self._connected:
            self.ib.disconnect()
            self._connected = False
            logger.info("[IBKR] Disconnected from IB Gateway.")

    def _ensure_connected(self):
        if not self._connected:
            raise ConnectionError(
                "IBKRClient is not connected. Call connect() first or ensure IB Gateway is running."
            )

    # ── Market Data ──────────────────────────────────────

    async def get_price(self, symbol: str) -> float:
        self._ensure_connected()
        contract = Stock(symbol, "SMART", "USD")
        self.ib.qualifyContracts(contract)
        ticker = self.ib.reqMktData(contract)
        await asyncio.sleep(1)  # wait for market data to arrive
        price = ticker.last or ticker.close or ticker.bid or 0.0
        self.ib.cancelMktData(contract)
        return float(price)

    async def get_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1 hour",
        limit: int = 500,
        since: Optional[int] = None,
    ) -> pd.DataFrame:
        self._ensure_connected()
        contract = Stock(symbol, "SMART", "USD")
        self.ib.qualifyContracts(contract)

        # IBKR bar size map
        bar_size_map = {
            "1m": "1 min",
            "5m": "5 mins",
            "15m": "15 mins",
            "1h": "1 hour",
            "1 hour": "1 hour",
            "4h": "4 hours",
            "1d": "1 day",
        }
        bar_size = bar_size_map.get(timeframe, "1 hour")

        # Duration based on limit
        duration = f"{max(1, limit // 24)} D" if "hour" in bar_size else f"{max(1, limit // 390)} D"

        bars = await self.ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="MIDPOINT",
            useRTH=True,
        )
        df = pd.DataFrame([
            {
                "timestamp": b.date,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in bars
        ])
        if not df.empty:
            df.set_index("timestamp", inplace=True)
        return df

    async def get_orderbook(self, symbol: str) -> dict:
        self._ensure_connected()
        contract = Stock(symbol, "SMART", "USD")
        self.ib.qualifyContracts(contract)
        ticker = self.ib.reqMktDepth(contract)
        await asyncio.sleep(1)
        bids = [[b.price, b.size] for b in ticker.domBids[:10]]
        asks = [[a.price, a.size] for a in ticker.domAsks[:10]]
        self.ib.cancelMktDepth(contract)
        return {"bids": bids, "asks": asks}

    # ── Options Chain ────────────────────────────────────

    async def get_options_chain(self, symbol: str) -> dict:
        """
        Fetch the full options chain for an underlying symbol.
        Returns strikes, expirations, IVs, and Greeks via IBKR.
        """
        self._ensure_connected()
        contract = Stock(symbol, "SMART", "USD")
        self.ib.qualifyContracts(contract)
        chains = await self.ib.reqSecDefOptParamsAsync(
            symbol, "", contract.secType, contract.conId
        )
        if not chains:
            return {}
        chain = chains[0]
        return {
            "expirations": list(chain.expirations),
            "strikes": list(chain.strikes),
            "exchange": chain.exchange,
        }

    # ── Account ──────────────────────────────────────────

    async def get_balance(self) -> Balance:
        self._ensure_connected()
        account_values = self.ib.accountValues()
        total = 0.0
        available = 0.0
        for v in account_values:
            if v.tag == "NetLiquidation" and v.currency == "USD":
                total = float(v.value)
            if v.tag == "AvailableFunds" and v.currency == "USD":
                available = float(v.value)
        return Balance(total=total, available=available, currency="USD")

    async def get_positions(self) -> List[Position]:
        self._ensure_connected()
        raw = self.ib.positions()
        positions = []
        for p in raw:
            contract = p.contract
            asset_class = "stock" if contract.secType == "STK" else "option"
            positions.append(Position(
                symbol=contract.symbol,
                side="long" if p.position > 0 else "short",
                quantity=abs(p.position),
                entry_price=float(p.avgCost),
                current_price=0.0,  # request separately if needed
                unrealized_pnl=0.0,
                asset_class=asset_class,
            ))
        return positions

    # ── Order Management ─────────────────────────────────

    async def place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "market",
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        option_expiry: Optional[str] = None,
        option_strike: Optional[float] = None,
        option_right: Optional[str] = None,  # "C" or "P"
        **kwargs,
    ) -> OrderResult:
        self._ensure_connected()
        logger.info(f"[IBKR] Placing {order_type.upper()} {side.upper()} {quantity} {symbol}")

        # Build contract
        if option_expiry and option_strike and option_right:
            contract = Option(symbol, option_expiry, option_strike, option_right, "SMART")
        else:
            contract = Stock(symbol, "SMART", "USD")
        self.ib.qualifyContracts(contract)

        # Build order
        action = "BUY" if side.lower() == "buy" else "SELL"
        if order_type == "limit" and price:
            order = LimitOrder(action, quantity, price)
        elif order_type in ("stop_limit", "stop") and stop_price and price:
            order = StopLimitOrder(action, quantity, price, stop_price)
        else:
            order = MarketOrder(action, quantity)

        trade: IBTrade = self.ib.placeOrder(contract, order)
        await asyncio.sleep(0.5)

        return OrderResult(
            order_id=str(trade.order.orderId),
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=float(price or 0),
            status=trade.orderStatus.status,
            raw={"order_id": trade.order.orderId, "status": trade.orderStatus.status},
        )

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        self._ensure_connected()
        try:
            open_trades = self.ib.openTrades()
            for trade in open_trades:
                if str(trade.order.orderId) == str(order_id):
                    self.ib.cancelOrder(trade.order)
                    return True
            return False
        except Exception as e:
            logger.error(f"[IBKR] Cancel order failed: {e}")
            return False

    async def get_order_status(self, order_id: str, symbol: str) -> OrderResult:
        self._ensure_connected()
        open_trades = self.ib.openTrades()
        for trade in open_trades:
            if str(trade.order.orderId) == str(order_id):
                return OrderResult(
                    order_id=order_id,
                    symbol=symbol,
                    side=trade.order.action.lower(),
                    quantity=float(trade.order.totalQuantity),
                    price=float(trade.order.lmtPrice or 0),
                    status=trade.orderStatus.status,
                    raw={"order_id": order_id},
                )
        raise ValueError(f"Order {order_id} not found.")

    async def stream_prices(
        self,
        symbols: List[str],
        callback: Callable[[str, float], None],
    ) -> None:
        """Stream real-time prices via IBKR market data subscription."""
        self._ensure_connected()
        contracts = [Stock(s, "SMART", "USD") for s in symbols]
        self.ib.qualifyContracts(*contracts)

        tickers = [self.ib.reqMktData(c) for c in contracts]
        logger.info(f"[IBKR] Starting price stream for: {symbols}")

        def on_pending_tickers(pending):
            for ticker in pending:
                price = ticker.last or ticker.close or 0.0
                if price > 0:
                    symbol = ticker.contract.symbol
                    asyncio.ensure_future(
                        callback(symbol, price) if asyncio.iscoroutinefunction(callback)
                        else asyncio.get_event_loop().run_in_executor(None, callback, symbol, price)
                    )

        self.ib.pendingTickersEvent += on_pending_tickers
        await asyncio.sleep(float("inf"))  # keep streaming until cancelled
