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
            "3d":    TimeFrame.Day,    # Alpaca has no native 3-day bar — use 1d
            "1w":    TimeFrame.Week,
        }
        return mapping.get(tf, TimeFrame.Hour)

    async def get_price(self, symbol: str) -> float:
        loop = asyncio.get_running_loop()
        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        resp = await loop.run_in_executor(None, lambda: self.data.get_stock_latest_trade(req))
        return float(resp[symbol].price)

    async def get_bid_ask(self, symbol: str) -> tuple[float, float]:
        """Return current (bid, ask) from the latest quote. Falls back to get_price() on failure."""
        loop = asyncio.get_running_loop()
        try:
            req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
            resp = await loop.run_in_executor(None, lambda: self.data.get_stock_latest_quote(req))
            q = resp[symbol]
            bid = float(q.bid_price)
            ask = float(q.ask_price)
            if bid > 0 and ask > 0:
                return bid, ask
        except Exception:
            pass
        price = await self.get_price(symbol)
        return price, price

    # Minutes per timeframe string � used to compute start date from limit
    _TF_MINUTES: dict[str, int] = {
        "1m": 1, "5m": 5, "15m": 15, "30m": 30,
        "1h": 60, "1Hour": 60, "4h": 240, "1d": 1440,
        "3d": 1440, "1w": 1440,  # use daily minutes for start-date calculation
    }

    async def get_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 500,
        since: Optional[int] = None,
    ) -> pd.DataFrame:
        loop = asyncio.get_running_loop()
        # Alpaca requires an explicit start date; derive it from limit when not given.
        # Stock markets trade only ~390 min/day (Mon-Fri 09:30-16:00 ET). The old
        # formula (timedelta minutes = minutes * limit) assumed 24/7 operation, so
        # for 1h+limit=200 it only went back 8 calendar days (~39 trading hours=39
        # bars) � below the strategy's 50-bar minimum, causing "Insufficient data".
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
        loop = asyncio.get_running_loop()
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        resp = await loop.run_in_executor(None, lambda: self.data.get_stock_latest_quote(req))
        q = resp[symbol]
        return {
            "bids": [[float(q.bid_price), float(q.bid_size)]],
            "asks": [[float(q.ask_price), float(q.ask_size)]],
        }

    async def get_balance(self) -> Balance:
        loop = asyncio.get_running_loop()
        account: Any = await loop.run_in_executor(None, self.trading.get_account)
        return Balance(
            total=float(account.portfolio_value),
            available=float(account.cash),  # use cash, not buying_power (which includes margin)
            currency="USD",
        )

    async def get_positions(self) -> List[Position]:
        loop = asyncio.get_running_loop()
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
        # entry_price is passed by ForwardEngine so we can reanchor stale SL/TP
        # to current market price if Alpaca rejects the bracket (error 42210000).
        entry_price: Optional[float] = kwargs.get("entry_price")

        loop = asyncio.get_running_loop()
        logger.info(f"[Alpaca] {order_type.upper()} {side.upper()} {quantity} {symbol}")
        order_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        tif = TimeInForce.GTC

        def _build_req(sl: Optional[float], tp: Optional[float]):
            """Build the appropriate MarketOrderRequest given current sl/tp values."""
            if sl and tp:
                return MarketOrderRequest(
                    symbol=symbol, qty=quantity, side=order_side, time_in_force=tif,
                    order_class=OrderClass.BRACKET,
                    stop_loss=StopLossRequest(stop_price=sl),
                    take_profit=TakeProfitRequest(limit_price=tp),
                )
            elif sl and not tp:
                return MarketOrderRequest(
                    symbol=symbol, qty=quantity, side=order_side, time_in_force=tif,
                    order_class=OrderClass.OTO,
                    stop_loss=StopLossRequest(stop_price=sl),
                )
            elif tp and not sl:
                return MarketOrderRequest(
                    symbol=symbol, qty=quantity, side=order_side, time_in_force=tif,
                    order_class=OrderClass.OTO,
                    take_profit=TakeProfitRequest(limit_price=tp),
                )
            elif order_type == "limit" and price:
                return LimitOrderRequest(
                    symbol=symbol, qty=quantity, side=order_side, time_in_force=tif,
                    limit_price=price,
                )
            else:
                return MarketOrderRequest(
                    symbol=symbol, qty=quantity, side=order_side,
                    time_in_force=TimeInForce.DAY,
                )

        req = _build_req(stop_price, take_profit_price)
        try:
            raw_result: Any = await loop.run_in_executor(None, lambda: self.trading.submit_order(req))
        except Exception as _bracket_err:
            # ── Stale SL/TP bracket rejection (Alpaca code 42210000) ─────────
            # When a signal is generated and then executed later, the market may
            # have moved enough that the precomputed stop_loss is on the wrong
            # side of current price.  Alpaca validates against `base_price`
            # (current market) and rejects with:
            #   stop_loss.stop_price must be >= base_price + 0.01  (SHORT)
            #   stop_loss.stop_price must be <= base_price - 0.01  (BUY)
            # Fix: extract base_price from the error JSON, reanchor the
            # SL/TP using the same ATR distance from the original signal's
            # entry_price, and retry.  If that also fails, fall back to a
            # plain market order so the trade is never fully lost.
            import json as _json, re as _re
            _err_str = str(_bracket_err)
            _code_match = _re.search(r'"code"\s*:\s*42210000', _err_str)
            if _code_match and (stop_price or take_profit_price):
                # Extract base_price from the Alpaca error JSON payload
                _base_price: Optional[float] = None
                _bp_match = _re.search(r'"base_price"\s*:\s*"?([\d.]+)"?', _err_str)
                if _bp_match:
                    try:
                        _base_price = float(_bp_match.group(1))
                    except ValueError:
                        pass
                if _base_price and entry_price:
                    # Preserve the ATR distance from the original signal
                    _is_short = order_side == OrderSide.SELL
                    _sl_dist = abs(stop_price - entry_price) if stop_price and entry_price else 0.0
                    _tp_dist = abs(take_profit_price - entry_price) if take_profit_price and entry_price else 0.0
                    _min_tick = 0.02  # ensure Alpaca's >= base + 0.01 is satisfied
                    new_sl: Optional[float] = None
                    new_tp: Optional[float] = None
                    if _is_short:
                        # SHORT: SL above market, TP below market
                        if stop_price is not None:
                            new_sl = round(_base_price + max(_sl_dist, _min_tick), 4)
                        if take_profit_price is not None:
                            new_tp = round(_base_price - max(_tp_dist, _min_tick), 4)
                    else:
                        # BUY: SL below market, TP above market
                        if stop_price is not None:
                            new_sl = round(_base_price - max(_sl_dist, _min_tick), 4)
                        if take_profit_price is not None:
                            new_tp = round(_base_price + max(_tp_dist, _min_tick), 4)
                    logger.warning(
                        f"[Alpaca] Bracket rejected (stale SL/TP): {symbol} {side} "
                        f"base_price={_base_price}, original entry={entry_price}, "
                        f"original SL={stop_price}→{new_sl}, TP={take_profit_price}→{new_tp}. Retrying."
                    )
                    req = _build_req(new_sl, new_tp)
                    try:
                        raw_result = await loop.run_in_executor(None, lambda: self.trading.submit_order(req))
                    except Exception as _retry_err:
                        # Adjusted bracket also failed — fall back to plain market order
                        logger.warning(
                            f"[Alpaca] Adjusted bracket also failed for {symbol}: {_retry_err}. "
                            f"Placing plain market order (no bracket)."
                        )
                        req = _build_req(None, None)
                        raw_result = await loop.run_in_executor(None, lambda: self.trading.submit_order(req))
                else:
                    # No base_price or entry_price — fall back to plain market order
                    logger.warning(
                        f"[Alpaca] Bracket rejected (code 42210000) for {symbol} "
                        f"and cannot reanchor SL/TP (missing base_price or entry_price). "
                        f"Placing plain market order."
                    )
                    req = _build_req(None, None)
                    raw_result = await loop.run_in_executor(None, lambda: self.trading.submit_order(req))
            else:
                raise  # unrelated error — propagate normally
        order_id = str(raw_result.id)
        fill_price: Optional[float] = None

        # ── Poll for fill confirmation (up to 10 s for market orders) ──────────
        # Market orders on Alpaca fill almost instantly during session hours.
        # We poll get_order_by_id until status is 'filled' or timeout.
        _FILL_TIMEOUT = 10.0   # seconds
        _POLL_INTERVAL = 0.5
        _elapsed = 0.0
        while _elapsed < _FILL_TIMEOUT:
            await asyncio.sleep(_POLL_INTERVAL)
            _elapsed += _POLL_INTERVAL
            try:
                _status_raw: Any = await loop.run_in_executor(
                    None, lambda: self.trading.get_order_by_id(order_id)
                )
                if str(_status_raw.status) == "filled":
                    fill_price = float(_status_raw.filled_avg_price) if _status_raw.filled_avg_price else None
                    logger.info(f"[Alpaca] Order {order_id} filled @ {fill_price}")
                    break
                elif str(_status_raw.status) in ("canceled", "expired", "rejected"):
                    raise RuntimeError(
                        f"Alpaca order {order_id} ended with status '{_status_raw.status}' — not filled"
                    )
            except RuntimeError:
                raise
            except Exception as _pe:
                logger.debug(f"[Alpaca] Poll {order_id}: {_pe}")
        else:
            logger.warning(f"[Alpaca] Order {order_id} not confirmed filled within {_FILL_TIMEOUT}s — treating as pending")

        return OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=float(raw_result.limit_price) if raw_result.limit_price else 0.0,
            status="filled" if fill_price is not None else str(raw_result.status),
            raw=raw_result.model_dump(),
            fill_price=fill_price,
        )

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: self.trading.cancel_order_by_id(order_id))
            return True
        except Exception as e:
            logger.error(f"[Alpaca] Cancel failed: {e}")
            return False

    async def get_order_status(self, order_id: str, symbol: str) -> OrderResult:
        loop = asyncio.get_running_loop()
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

    async def update_stop_loss(
        self,
        symbol: str,
        side: str,
        quantity: float,
        new_sl_price: float,
    ) -> bool:
        """
        Find the open SL stop child order for *symbol* and replace its stop price.
        Uses Alpaca's replace_order_by_id which modifies in-place (no cancel needed).
        G1 guarantees only one active stop order per symbol at a time.
        """
        loop = asyncio.get_running_loop()
        try:
            from alpaca.trading.requests import GetOrdersRequest, ReplaceOrderRequest
            from alpaca.trading.enums import QueryOrderStatus

            _norm_sym = symbol.replace("/", "").upper()
            # The SL exit side is opposite to entry: BUY position exits via SELL stop
            exit_side = "sell" if side.lower() == "buy" else "buy"

            open_orders: Any = await loop.run_in_executor(
                None,
                lambda: self.trading.get_orders(filter=GetOrdersRequest(
                    status=QueryOrderStatus.OPEN,
                    symbols=[_norm_sym],
                )),
            )
            stop_orders = [
                o for o in open_orders
                if str(getattr(o, "type", "")).lower() in ("stop", "stop_limit")
                and str(getattr(o, "side", "")).lower() == exit_side
            ]
            if not stop_orders:
                logger.debug(f"[Alpaca] update_stop_loss: no open stop order for {symbol}")
                return False

            for o in stop_orders:
                await loop.run_in_executor(
                    None,
                    lambda oid=str(o.id): self.trading.replace_order_by_id(
                        oid, ReplaceOrderRequest(stop_price=new_sl_price)
                    ),
                )
            logger.info(f"[Alpaca] ⚡ Trailing stop synced: {symbol} SL → {new_sl_price}")
            return True
        except Exception as exc:
            logger.warning(f"[Alpaca] update_stop_loss failed for {symbol}: {exc}")
            return False
    async def stream_prices(self, symbols: List[str], callback: Callable) -> None:        # Use credentials matching the current mode (paper vs live)
        api_key = settings.alpaca_api_key if self._paper else (settings.alpaca_api_key_live or settings.alpaca_api_key)
        api_secret = settings.alpaca_api_secret if self._paper else (settings.alpaca_api_secret_live or settings.alpaca_api_secret)
        stream = StockDataStream(
            api_key=api_key,
            secret_key=api_secret,
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
