import asyncio
import random
import pandas as pd
from typing import List, Optional, Callable
from loguru import logger

from ib_insync import IB, Stock, Option, Contract, MarketOrder, LimitOrder, StopLimitOrder, Trade as IBTrade

from config import settings
from brokers.base import AbstractBroker, OrderResult, Position, Balance


def ibkr_balance_sync() -> "Balance":
    """
    Fetch IBKR account balance in an isolated event loop (safe to call from any thread).

    Uses a random clientId in the 50-99 range to avoid collision with the main
    IBKRClient instance (clientId from settings) or concurrent calls.

    ib_insync automatically subscribes to account updates during connectAsync;
    we sleep briefly to let the Gateway push the initial account-value snapshot
    into the local cache, then read ib.accountValues().
    """
    async def _fetch() -> Balance:
        ib = IB()
        client_id = random.randint(50, 99)
        try:
            await ib.connectAsync(
                host=settings.ibkr_host,
                port=settings.ibkr_port,
                clientId=client_id,
            )
            # ib_insync auto-subscribes on connect; give the Gateway time to push
            # the initial account-value snapshot (~188 values for paper accounts).
            # reqAccountUpdatesAsync() is a never-resolving subscription — do NOT await it.
            await asyncio.sleep(2.0)
            values = ib.accountValues()
            total, available = 0.0, 0.0
            for v in values:
                if v.tag == "NetLiquidation" and v.currency in ("USD", "BASE"):
                    total = float(v.value)
                if v.tag == "AvailableFunds" and v.currency in ("USD", "BASE"):
                    available = float(v.value)
            logger.debug(f"IBKR balance fetched via clientId={client_id}: total={total} available={available}")
            return Balance(total=total, available=available, currency="USD")
        finally:
            ib.disconnect()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_fetch())
    finally:
        loop.close()
        asyncio.set_event_loop(None)


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

    def __init__(self):
        self._paper = settings.ibkr_paper
        self.ib = IB()
        self._connected = False
        mode = "PAPER" if self._paper else "LIVE"
        logger.info(f"IBKRClient initialized in {mode} mode (not yet connected).")

    async def connect(self):
        """Connect to IB Gateway. Must be called before any other method."""
        if not self._connected:
            await self.ib.connectAsync(
                host=settings.ibkr_host,
                port=settings.ibkr_port,
                clientId=settings.ibkr_client_id,
            )
            self._connected = True
            logger.info(f"[IBKR] Connected to IB Gateway at {settings.ibkr_host}:{settings.ibkr_port}")

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
