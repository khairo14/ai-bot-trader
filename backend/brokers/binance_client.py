import asyncio
import ccxt.async_support as ccxt
import pandas as pd
from typing import List, Optional, Callable
from loguru import logger

from config import settings
from brokers.base import AbstractBroker, OrderResult, Position, Balance


class BinanceClient(AbstractBroker):
    """
    Binance broker connector.
    Supports: Spot and Futures crypto trading.
    Uses ccxt for REST and direct WebSocket for live price streaming.
    """

    name = "binance"
    asset_class = "crypto"

    def __init__(self, paper: bool | None = None):
        self._paper = paper if paper is not None else settings.binance_testnet
        # Pick keys based on mode
        if self._paper and settings.binance_api_key_testnet:
            api_key = settings.binance_api_key_testnet
            api_secret = settings.binance_api_secret_testnet
        else:
            api_key = settings.binance_api_key
            api_secret = settings.binance_api_secret
        exchange_config = {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "timeout": 10000,
            "options": {
                "defaultType": "spot",
                "fetchMarkets": ["spot"],
                "fetchCurrencies": False,
            },
        }
        # Route through system proxy when running behind a VPN locally
        if settings.http_proxy:
            exchange_config["aiohttp_proxy"] = settings.http_proxy
            logger.info(f"BinanceClient using proxy: {settings.http_proxy}")
        self.exchange = ccxt.binance(exchange_config)  # type: ignore[call-arg]
        if self._paper:
            self.exchange.set_sandbox_mode(True)
            logger.info("BinanceClient initialized in TESTNET (paper) mode.")
        else:
            logger.info("BinanceClient initialized in LIVE mode.")

    async def close(self) -> None:
        """Close the underlying ccxt aiohttp session."""
        try:
            await self.exchange.close()
        except Exception:
            pass

    async def _ensure_markets(self) -> None:
        """Load spot markets once; silently skip sapi/margin timeouts."""
        if self.exchange.markets:
            return
        try:
            await self.exchange.load_markets()
        except Exception as e:
            logger.warning(f"[Binance] Market pre-load partial error (non-fatal): {e}")
            # If markets still empty, try direct REST call (works on both live & testnet)
            if not self.exchange.markets:
                try:
                    raw = await self.exchange.publicGetApiV3ExchangeInfo()  # type: ignore[attr-defined]
                    symbols = raw.get("symbols", []) if isinstance(raw, dict) else []
                    markets: dict = {}
                    for s in symbols:
                        if s.get("status") != "TRADING":
                            continue
                        base = s.get("baseAsset", "")
                        quote = s.get("quoteAsset", "")
                        if base and quote:
                            markets[f"{base}/{quote}"] = s
                    if markets:
                        self.exchange.markets = markets
                        logger.info(f"[Binance] Loaded {len(markets)} spot markets via fallback.")
                except Exception:
                    pass  # will lazy-load per-request

    async def get_price(self, symbol: str) -> float:
        await self._ensure_markets()
        ticker = await self.exchange.fetch_ticker(symbol)
        return float(ticker["last"] or 0.0)  # type: ignore[arg-type]

    async def get_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 500,
        since: Optional[int] = None,
    ) -> pd.DataFrame:
        await self._ensure_markets()
        raw = await self.exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        return df

    async def get_orderbook(self, symbol: str) -> dict:
        await self._ensure_markets()
        book = await self.exchange.fetch_order_book(symbol)
        return {"bids": book["bids"][:20], "asks": book["asks"][:20]}

    async def get_balance(self) -> Balance:
        """
        Returns total spot portfolio value in USDT.
        Fetches raw balance directly (no market pre-load).
        Non-USDT assets are priced via a single batch fetch_tickers call
        to avoid per-asset rate-limit queuing that causes timeouts on testnet.
        """
        data = await self.exchange.fetch_balance({"type": "spot"})

        usdt_total = float((data.get("USDT") or {}).get("total") or 0.0)
        usdt_free  = float((data.get("USDT") or {}).get("free")  or 0.0)

        # Collect non-USDT assets with meaningful balance
        pending: dict[str, float] = {}
        for asset, info in data.items():
            if asset in ("USDT", "info", "free", "used", "total", "debt"):
                continue
            if not isinstance(info, dict):
                continue
            qty = float(info.get("total") or 0.0)
            if qty > 0:
                pending[f"{asset}/USDT"] = qty

        # Price them all in ONE batch request instead of N individual calls
        if pending:
            try:
                tickers = await asyncio.wait_for(
                    self.exchange.fetch_tickers(list(pending.keys())),
                    timeout=8.0,
                )
                for symbol, qty in pending.items():
                    t = tickers.get(symbol) or {}
                    price = float(t.get("last") or 0.0)
                    usdt_total += qty * price
            except Exception:
                pass  # non-fatal — USDT balance already captured above

        return Balance(
            total=round(usdt_total, 4),
            available=round(usdt_free, 4),
            currency="USDT",
        )

    async def get_positions(self) -> List[Position]:
        # Spot: positions are simply non-zero balances.
        # ccxt returns balance["info"]["balances"] as a LIST of {"asset", "free", "locked"} dicts.
        balance = await self.exchange.fetch_balance()
        raw_balances = balance.get("info", {}).get("balances", [])
        # Normalise: ccxt may return a dict (testnet) or a list (live)
        if isinstance(raw_balances, dict):
            items = raw_balances.items()   # {symbol: {free, locked, total}}
        else:
            items = ((b["asset"], b) for b in raw_balances if isinstance(b, dict))

        positions = []
        for asset, info in items:
            free = float(info.get("free", 0))
            if free > 0 and asset != "USDT":
                symbol = f"{asset}/USDT"
                try:
                    price = await self.get_price(symbol)
                    positions.append(Position(
                        symbol=symbol,
                        side="long",
                        quantity=free,
                        entry_price=0.0,  # spot has no tracked entry
                        current_price=price,
                        unrealized_pnl=0.0,
                        asset_class="crypto",
                    ))
                except Exception:
                    pass
        return positions

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
        logger.info(f"[Binance] Placing {order_type.upper()} {side.upper()} {quantity} {symbol}")
        params = {}
        if stop_price and take_profit_price:
            # OCO order
            result = await self.exchange.create_order(
                symbol, "oco", side, quantity,  # type: ignore[arg-type]
                price=take_profit_price,
                params={"stopPrice": stop_price, "stopLimitPrice": stop_price * 0.999}
            )
        elif order_type == "limit" and price:
            result = await self.exchange.create_limit_order(symbol, side, quantity, price)  # type: ignore[arg-type]
        else:
            result = await self.exchange.create_market_order(symbol, side, quantity)  # type: ignore[arg-type]

        return OrderResult(
            order_id=str(result["id"]),
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=float(result.get("price") or result.get("average") or 0.0),
            status=str(result.get("status") or "open"),
            raw=dict(result),  # type: ignore[arg-type]
        )

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        try:
            await self.exchange.cancel_order(order_id, symbol)
            return True
        except Exception as e:
            logger.error(f"[Binance] Cancel order failed: {e}")
            return False

    async def get_order_status(self, order_id: str, symbol: str) -> OrderResult:
        result = await self.exchange.fetch_order(order_id, symbol)
        return OrderResult(
            order_id=str(result["id"]),
            symbol=symbol,
            side=str(result["side"]),
            quantity=float(result["amount"] or 0.0),
            price=float(result.get("price") or result.get("average") or 0.0),
            status=str(result["status"]),
            raw=dict(result),  # type: ignore[arg-type]
        )

    async def stream_prices(
        self,
        symbols: List[str],
        callback: Callable[[str, float], None],
    ) -> None:
        """
        Real-time price streaming via Binance WebSocket.
        Connects to trade stream for each symbol.
        """
        import asyncio
        import json
        import websockets

        streams = "/".join([f"{s.replace('/', '').lower()}@aggTrade" for s in symbols])
        base_url = "wss://testnet.binance.vision/stream" if self._paper else "wss://stream.binance.com:9443/stream"
        url = f"{base_url}?streams={streams}"

        logger.info(f"[Binance] Starting price stream for: {symbols}")
        async with websockets.connect(url) as ws:
            while True:
                try:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    stream_data = data.get("data", data)
                    symbol_raw = stream_data.get("s", "")
                    price = float(stream_data.get("p", 0))
                    # Convert BTCUSDT → BTC/USDT
                    symbol = symbol_raw[:-4] + "/" + symbol_raw[-4:] if symbol_raw.endswith("USDT") else symbol_raw
                    if price > 0:
                        await callback(symbol, price) if asyncio.iscoroutinefunction(callback) else callback(symbol, price)
                except Exception as e:
                    logger.error(f"[Binance] Stream error: {e}")
                    await asyncio.sleep(1)

    async def close(self):
        await self.exchange.close()
