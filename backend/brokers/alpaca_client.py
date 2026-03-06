import asyncio
import pandas as pd
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Callable
from loguru import logger

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    TakeProfitRequest,
    StopLossRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.live import StockDataStream
from alpaca.data.enums import DataFeed

from config import settings
from brokers.base import AbstractBroker, OrderResult, Position, Balance


class AlpacaClient(AbstractBroker):
    name = "alpaca"
    asset_class = "stock"

    def __init__(self, paper: bool | None = None):
        if paper is None:
            paper = "paper-api" in settings.alpaca_base_url
        self._paper = paper
        # Pick credentials based on mode
        if paper:
            api_key = settings.alpaca_api_key
            api_secret = settings.alpaca_api_secret
            base_url = settings.alpaca_base_url
        else:
            api_key = settings.alpaca_api_key_live or settings.alpaca_api_key
            api_secret = settings.alpaca_api_secret_live or settings.alpaca_api_secret
            base_url = settings.alpaca_base_url_live
        self.trading = TradingClient(
            api_key=api_key,
            secret_key=api_secret,
            paper=paper,
        )
        self.data = StockHistoricalDataClient(
            api_key=api_key,
            secret_key=api_secret,
        )
        mode = "PAPER" if self._paper else "LIVE"
        logger.info(f"AlpacaClient initialized in {mode} mode.")

    @staticmethod
    def _map_timeframe(tf: str) -> Any:
        mapping: dict[str, Any] = {
            "1m":    TimeFrame.Minute,
            "5m":    TimeFrame(5, TimeFrameUnit.Minute),  # type: ignore[arg-type]
            "15m":   TimeFrame(15, TimeFrameUnit.Minute),  # type: ignore[arg-type]
            "30m":   TimeFrame(30, TimeFrameUnit.Minute),  # type: ignore[arg-type]
            "1h":    TimeFrame.Hour,
            "1Hour": TimeFrame.Hour,
            "4h":    TimeFrame(4, TimeFrameUnit.Hour),  # type: ignore[arg-type]
            "1d":    TimeFrame.Day,
        }
        return mapping.get(tf, TimeFrame.Hour)

    async def get_price(self, symbol: str) -> float:
        loop = asyncio.get_event_loop()
        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        resp = await loop.run_in_executor(None, lambda: self.data.get_stock_latest_trade(req))
        return float(resp[symbol].price)

    # Minutes per timeframe string — used to compute start date from limit
    _TF_MINUTES: dict[str, int] = {
        "1m": 1, "5m": 5, "15m": 15, "30m": 30,
        "1h": 60, "1Hour": 60, "4h": 240, "1d": 1440,
    }

    async def get_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 500,
        since: Optional[int] = None,
    ) -> pd.DataFrame:
        loop = asyncio.get_event_loop()
        # Alpaca requires an explicit start date; derive it from limit when not given.
        # Stock markets trade only ~390 min/day (Mon-Fri 09:30-16:00 ET). The old
        # formula (timedelta minutes = minutes * limit) assumed 24/7 operation, so
        # for 1h+limit=200 it only went back 8 calendar days (~39 trading hours=39
        # bars) — below the strategy's 50-bar minimum, causing "Insufficient data".
        # Fix: convert trading minutes needed into calendar days with a safety buffer.
        if since:
            start = datetime.fromtimestamp(since / 1000, tz=timezone.utc)
        else:
            mins = self._TF_MINUTES.get(timeframe, 60)
            # Trading days needed: how many 390-min sessions fit the requested bars
            trading_days_needed = (limit * mins + 390) / 390
            # Convert to calendar days (+7 day buffer for weekends & holidays)
            calendar_days = int(trading_days_needed * 7 / 5) + 7
            start = datetime.now(tz=timezone.utc) - timedelta(days=calendar_days)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=self._map_timeframe(timeframe),
            start=start,
            limit=limit,
        )
        resp = await loop.run_in_executor(None, lambda: self.data.get_stock_bars(req))
        df: Any = resp.df  # type: ignore[union-attr]

        if df is None or df.empty:
            raise ValueError(f"No OHLCV data returned from Alpaca for '{symbol}' on {timeframe}")

        if isinstance(df.index, pd.MultiIndex):
            if symbol not in df.index.get_level_values("symbol"):
                raise ValueError(f"Symbol '{symbol}' not found in Alpaca response")
            df = df.xs(symbol, level="symbol")

        df.index = pd.to_datetime(df.index)

        missing = [c for c in ("open", "high", "low", "close", "volume") if c not in df.columns]
        if missing:
            raise ValueError(f"Alpaca response for '{symbol}' missing columns: {missing}. Got: {list(df.columns)}")

        df = df[["open", "high", "low", "close", "volume"]]
        df.index.name = "timestamp"
        return df

    async def get_orderbook(self, symbol: str) -> dict:
        loop = asyncio.get_event_loop()
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        resp = await loop.run_in_executor(None, lambda: self.data.get_stock_latest_quote(req))
        q = resp[symbol]
        return {
            "bids": [[float(q.bid_price), float(q.bid_size)]],
            "asks": [[float(q.ask_price), float(q.ask_size)]],
        }

    async def get_balance(self) -> Balance:
        loop = asyncio.get_event_loop()
        account: Any = await loop.run_in_executor(None, self.trading.get_account)
        return Balance(
            total=float(account.portfolio_value),
            available=float(account.buying_power),
            currency="USD",
        )

    async def get_positions(self) -> List[Position]:
        loop = asyncio.get_event_loop()
        raw: Any = await loop.run_in_executor(None, self.trading.get_all_positions)
        return [
            Position(
                symbol=p.symbol,
                side="long" if float(p.qty) > 0 else "short",
                quantity=abs(float(p.qty)),
                entry_price=float(p.avg_entry_price),
                current_price=float(p.current_price),
                unrealized_pnl=float(p.unrealized_pl),
                asset_class="stock",
            )
            for p in raw
        ]

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
        loop = asyncio.get_event_loop()
        logger.info(f"[Alpaca] {order_type.upper()} {side.upper()} {quantity} {symbol}")
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        tif = TimeInForce.GTC
        if stop_price and take_profit_price:
            req = MarketOrderRequest(
                symbol=symbol,
                qty=quantity,
                side=order_side,
                time_in_force=tif,
                order_class=OrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=stop_price),
                take_profit=TakeProfitRequest(limit_price=take_profit_price),
            )
        elif order_type == "limit" and price:
            req = LimitOrderRequest(
                symbol=symbol,
                qty=quantity,
                side=order_side,
                time_in_force=tif,
                limit_price=price,
            )
        else:
            req = MarketOrderRequest(
                symbol=symbol,
                qty=quantity,
                side=order_side,
                time_in_force=TimeInForce.DAY,
            )
        raw_result: Any = await loop.run_in_executor(None, lambda: self.trading.submit_order(req))
        return OrderResult(
            order_id=str(raw_result.id),
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=float(raw_result.limit_price) if raw_result.limit_price else 0.0,
            status=str(raw_result.status),
            raw=raw_result.model_dump(),
        )

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, lambda: self.trading.cancel_order_by_id(order_id))
            return True
        except Exception as e:
            logger.error(f"[Alpaca] Cancel failed: {e}")
            return False

    async def get_order_status(self, order_id: str, symbol: str) -> OrderResult:
        loop = asyncio.get_event_loop()
        raw_result: Any = await loop.run_in_executor(None, lambda: self.trading.get_order_by_id(order_id))
        return OrderResult(
            order_id=str(raw_result.id),
            symbol=symbol,
            side=str(raw_result.side),
            quantity=float(raw_result.qty),
            price=float(raw_result.filled_avg_price) if raw_result.filled_avg_price else 0.0,
            status=str(raw_result.status),
            raw=raw_result.model_dump(),
        )

    async def stream_prices(self, symbols: List[str], callback: Callable) -> None:
        stream = StockDataStream(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_api_secret,
            feed=DataFeed(settings.alpaca_data_feed),
        )

        async def on_trade(trade):
            price = float(trade.price)
            if asyncio.iscoroutinefunction(callback):
                await callback(trade.symbol, price)
            else:
                callback(trade.symbol, price)

        for symbol in symbols:
            stream.subscribe_trades(on_trade, symbol)
        logger.info(f"[Alpaca] Streaming: {symbols}")
        await stream._run_forever()
