import asyncio
import math
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
        # Separate unauthenticated exchange for public market-data (OHLCV).
        # Testnet has very limited historical candles; always use the production
        # endpoint for OHLCV regardless of paper/live mode.
        self._data_exchange = ccxt.binance({"enableRateLimit": True, "timeout": 10000})  # type: ignore[call-arg]

    async def close(self) -> None:
        """Close the underlying ccxt aiohttp sessions."""
        for ex in (self.exchange, self._data_exchange):
            try:
                await ex.close()
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
        price = float(ticker["last"]) if ticker and ticker.get("last") else 0.0
        if price <= 0:
            raise ValueError(f"[Binance] get_price returned zero/None for {symbol} — API may be unavailable")
        return price

    async def get_bid_ask(self, symbol: str) -> tuple[float, float]:
        """Return current (bid, ask) from the full ticker. Falls back to (last, last)."""
        await self._ensure_markets()
        ticker = await self.exchange.fetch_ticker(symbol)
        bid = float(ticker.get("bid") or 0.0)  # type: ignore[union-attr]
        ask = float(ticker.get("ask") or 0.0)  # type: ignore[union-attr]
        if bid > 0 and ask > 0:
            return bid, ask
        last = float(ticker.get("last") or 0.0)  # type: ignore[union-attr]
        return last, last

    async def get_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 500,
        since: Optional[int] = None,
    ) -> pd.DataFrame:
        # Always use the unauthenticated production endpoint for OHLCV.
        # The testnet has very limited historical candles and fails on 1h/4h/1d.
        raw = await self._data_exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        # Drop the last (still-forming) candle — ccxt always includes the current
        # incomplete bar as the final row, which can generate false signals.
        if len(df) > 1:
            df = df.iloc[:-1]
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

        candidates: list[tuple[str, float]] = []
        for asset, info in items:
            free = float(info.get("free", 0))
            locked = float(info.get("locked", 0))
            total = free + locked
            # F-108: include locked balance — Binance spot locks the full quantity in
            # standing bracket SL/TP orders.  Without this, F-103 in close_position()
            # incorrectly sees "no position" and skips the cancel+sell flow, causing
            # the trade to stay open or close at the wrong price.
            if total > 0 and asset != "USDT":
                candidates.append((f"{asset}/USDT", total))

        if not candidates:
            return []

        # BUG-10 FIX: fetch all prices in parallel (asyncio.gather) with a per-call
        # timeout instead of N serial calls with no timeout. Previously, each call
        # blocked the next; with 10+ assets this could stall the event loop for
        # several seconds or hang indefinitely if a single ticker call timed out.
        async def _safe_price(sym: str) -> float | None:
            try:
                return await asyncio.wait_for(self.get_price(sym), timeout=5.0)
            except Exception:
                return None

        prices = await asyncio.gather(*[_safe_price(sym) for sym, _ in candidates])

        positions = []
        for (symbol, total), price in zip(candidates, prices):
            if price is None:
                continue
            positions.append(Position(
                symbol=symbol,
                side="long",
                quantity=total,
                entry_price=price,  # spot has no tracked entry; use current price → PnL = 0
                current_price=price,
                unrealized_pnl=0.0,
                asset_class="crypto",
            ))
        return positions

    async def _normalize_qty(self, symbol: str, qty: float) -> float:
        """Truncate qty to the symbol's LOT_SIZE step so Binance accepts the order.

        Works for both CCXT-parsed markets (precision.amount = step size in TICK_SIZE
        mode) and the raw exchangeInfo fallback (filters[LOT_SIZE].stepSize).
        """
        market = (self.exchange.markets or {}).get(symbol, {})
        step: float | None = None

        if "precision" in market:
            # CCXT TICK_SIZE mode (Binance default): precision.amount IS the step
            raw = market["precision"].get("amount")
            if raw is not None:
                try:
                    step = float(raw)
                except (TypeError, ValueError):
                    pass

        if step is None and "filters" in market:
            # Raw exchangeInfo fallback: LOT_SIZE filter carries stepSize
            for f in market.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    try:
                        step = float(f["stepSize"])
                    except (TypeError, ValueError):
                        pass
                    break

        if step and step > 0:
            normalized = math.floor(qty / step) * step
            # Avoid floating-point artifacts (e.g. 0.10000000001)
            step_str = f"{step:.10f}".rstrip("0")
            decimals = len(step_str.split(".")[-1]) if "." in step_str else 0
            return round(normalized, max(0, decimals))

        return qty

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
        await self._ensure_markets()
        quantity = await self._normalize_qty(symbol, quantity)
        if quantity <= 0:
            raise ValueError(
                f"[Binance] Quantity rounds to zero after LOT_SIZE normalization "
                f"for {symbol}. Check position sizing."
            )
        logger.info(f"[Binance] Placing {order_type.upper()} {side.upper()} {quantity} {symbol}")
        params = {}
        if stop_price and take_profit_price:
            # Bracket: market entry first, then SL + TP exit guards on the opposite side.
            # Binance OCO via ccxt "oco" type is rejected on testnet and unreliable on spot;
            # placing two separate exit orders achieves the same protection.
            result = await self.exchange.create_market_order(symbol, side, quantity)  # type: ignore[arg-type]
            exit_side = "sell" if side == "buy" else "buy"
            # BUG-2 FIX: place TP limit order FIRST, then SL STOP_LOSS order.
            # Binance spot locks the full base asset when a STOP_LOSS (sell) order is placed.
            # Placing SL first means free balance = 0 when TP tries to place, causing a
            # silent "insufficient balance" failure.  A limit sell order does NOT lock
            # the asset as reserved collateral, so the TP must come first.
            # TP guard
            for _attempt in range(2):
                try:
                    await self.exchange.create_limit_order(symbol, exit_side, quantity, take_profit_price)  # type: ignore[arg-type]
                    break
                except Exception as _tp_err:
                    if _attempt == 0:
                        await asyncio.sleep(0.5)
                    else:
                        logger.error(f"[Binance] TP guard (bracket) failed after retry: {_tp_err}")
                        raise RuntimeError(
                            f"[Binance] TP guard (bracket) permanently failed for {symbol}: {_tp_err}"
                        )
            # SL guard — use STOP_LOSS (market-on-trigger) not STOP_LOSS_LIMIT so the
            # full quantity is guaranteed to fill even when price gaps through the level.
            for _attempt in range(2):
                try:
                    await self.exchange.create_order(
                        symbol, "STOP_LOSS", exit_side, quantity,
                        params={"stopPrice": stop_price}
                    )
                    break
                except Exception as _sl_err:
                    if _attempt == 0:
                        await asyncio.sleep(0.5)
                    else:
                        # BUG-2 FIX: raise instead of silently continuing — a position
                        # without a stop-loss is unprotected. The caller (ForwardEngine)
                        # will catch this, revert the PENDING guard back to OPEN, and
                        # retry on the next tick rather than leaving capital at risk.
                        raise RuntimeError(
                            f"[Binance] SL guard (bracket) permanently failed for {symbol}: {_sl_err}"
                        )
            order_id = str(result["id"])
            fill_price = float(result.get("average") or result.get("price") or 0.0) or None
        elif stop_price and not take_profit_price:
            # SL only: place market entry, then stop-loss protection in opposite direction.
            result = await self.exchange.create_market_order(symbol, side, quantity)  # type: ignore[arg-type]
            exit_side = "sell" if side == "buy" else "buy"
            # F-082: retry once (0.5 s delay) so transient Binance errors don't silently
            # leave a position unprotected.  Log at ERROR on permanent failure.
            # STOP_LOSS (market-on-trigger) guarantees full fill on gap moves.
            for _attempt in range(2):
                try:
                    await self.exchange.create_order(
                        symbol, "STOP_LOSS", exit_side, quantity,
                        params={"stopPrice": stop_price}
                    )
                    break  # success
                except Exception as _sl_err:
                    if _attempt == 0:
                        await asyncio.sleep(0.5)
                    else:
                        # BUG-2 FIX: raise instead of silently continuing — a position
                        # without a stop-loss is unprotected. Caller will revert the
                        # order state and retry rather than leaving capital at risk.
                        raise RuntimeError(
                            f"[Binance] SL guard order permanently failed for {symbol}: {_sl_err}"
                        )
            order_id = str(result["id"])
            fill_price = float(result.get("average") or result.get("price") or 0.0) or None
        elif take_profit_price and not stop_price:
            # TP only: place market entry, then limit take-profit in opposite direction.
            result = await self.exchange.create_market_order(symbol, side, quantity)  # type: ignore[arg-type]
            exit_side = "sell" if side == "buy" else "buy"
            # F-082: retry once (0.5 s delay) so transient Binance errors don't silently
            # leave a position unprotected.  Log at ERROR on permanent failure.
            for _attempt in range(2):
                try:
                    await self.exchange.create_limit_order(symbol, exit_side, quantity, take_profit_price)  # type: ignore[arg-type]
                    break  # success
                except Exception as _tp_err:
                    if _attempt == 0:
                        await asyncio.sleep(0.5)
                    else:
                        logger.error(f"[Binance] TP guard order failed after retry: {_tp_err}")
                        raise RuntimeError(
                            f"[Binance] TP guard permanently failed for {symbol}: {_tp_err}"
                        )
            order_id = str(result["id"])
            fill_price = float(result.get("average") or result.get("price") or 0.0) or None
        elif order_type == "limit" and price:
            result = await self.exchange.create_limit_order(symbol, side, quantity, price)  # type: ignore[arg-type]
            order_id = str(result["id"])
            fill_price = float(result.get("average") or result.get("price") or 0.0) or None
        else:
            result = await self.exchange.create_market_order(symbol, side, quantity)  # type: ignore[arg-type]
            order_id = str(result["id"])
            fill_price = float(result.get("average") or result.get("price") or 0.0) or None

        # ── Poll for fill confirmation if not immediately filled ─────────────
        if not fill_price or str(result.get("status")) != "closed":
            _FILL_TIMEOUT = 10.0
            _POLL_INTERVAL = 0.5
            _elapsed = 0.0
            while _elapsed < _FILL_TIMEOUT:
                await asyncio.sleep(_POLL_INTERVAL)
                _elapsed += _POLL_INTERVAL
                try:
                    _status = await self.exchange.fetch_order(order_id, symbol)
                    _st = str(_status.get("status", ""))
                    if _st == "closed":
                        fill_price = float(_status.get("average") or _status.get("price") or 0.0) or None
                        logger.info(f"[Binance] Order {order_id} filled @ {fill_price}")
                        break
                    elif _st in ("canceled", "expired", "rejected"):
                        raise RuntimeError(
                            f"Binance order {order_id} ended with status '{_st}' — not filled"
                        )
                except RuntimeError:
                    raise
                except Exception as _pe:
                    logger.debug(f"[Binance] Poll {order_id}: {_pe}")
            else:
                logger.warning(f"[Binance] Order {order_id} not confirmed filled within {_FILL_TIMEOUT}s")

        return OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=float(result.get("price") or result.get("average") or 0.0),
            status="filled" if fill_price else str(result.get("status") or "open"),
            raw=dict(result),  # type: ignore[arg-type]
            fill_price=fill_price,
            effective_stop_price=stop_price,
            effective_take_profit=take_profit_price,
        )

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        try:
            await self.exchange.cancel_order(order_id, symbol)
            return True
        except Exception as e:
            logger.error(f"[Binance] Cancel order failed: {e}")
            return False

    async def cancel_open_orders(self, symbol: str) -> None:
        """
        Cancel all open orders for *symbol* (SL guard, TP limit, etc.) so the
        asset balance is freed before a software-generated market close order.
        Binance spot locks the full quantity in each standing exit order, which
        causes a subsequent market sell to fail with "insufficient balance".

        Uses the atomic DELETE /api/v3/openOrders endpoint (cancel_all_orders)
        rather than a fetch-then-cancel loop.  The atomic endpoint is more reliable
        on testnet where fetch_open_orders can return stale/empty data while orders
        still hold the balance locked.
        """
        try:
            result = await self.exchange.cancel_all_orders(symbol)
            cancelled = len(result) if isinstance(result, list) else 0
            if cancelled:
                logger.info(f"[Binance] Cancelled {cancelled} open order(s) for {symbol} before market close")
        except Exception as exc:
            # Fallback: fetch-then-cancel loop (older ccxt or endpoint unavailable)
            logger.debug(f"[Binance] cancel_all_orders unavailable for {symbol} ({exc}), falling back to fetch+cancel")
            try:
                open_orders = await self.exchange.fetch_open_orders(symbol)
                for order in open_orders:
                    try:
                        await self.exchange.cancel_order(str(order["id"]), symbol)
                    except Exception as _ce:
                        logger.debug(f"[Binance] cancel_open_orders: could not cancel {order.get('id')}: {_ce}")
                if open_orders:
                    logger.info(f"[Binance] Cancelled {len(open_orders)} open order(s) for {symbol} (fallback)")
            except Exception as _fb_exc:
                logger.warning(f"[Binance] cancel_open_orders failed for {symbol}: {_fb_exc}")

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

    async def update_stop_loss(
        self,
        symbol: str,
        side: str,
        quantity: float,
        new_sl_price: float,
    ) -> bool:
        """
        Cancel the existing STOP_LOSS_LIMIT for *symbol* and place a new one at
        *new_sl_price*.  Binance has no in-place order modify, so cancel + replace
        is used.  G1 guarantees at most one SL order per symbol at a time.
        """
        try:
            await self._ensure_markets()
            exit_side = "sell" if side.lower() == "buy" else "buy"
            open_orders = await self.exchange.fetch_open_orders(symbol)
            stop_orders = [
                o for o in open_orders
                if str(o.get("type", "")).upper() in ("STOP_LOSS_LIMIT", "STOP_LOSS")
                and str(o.get("side", "")).lower() == exit_side
            ]
            if not stop_orders:
                logger.debug(f"[Binance] update_stop_loss: no open stop order for {symbol}")
                return False

            # Cancel all matching SL orders (normally exactly one)
            for o in stop_orders:
                try:
                    await self.exchange.cancel_order(str(o["id"]), symbol)
                except Exception as _ce:
                    logger.debug(f"[Binance] cancel stop {o['id']}: {_ce}")

            # Replace with new STOP_LOSS_LIMIT at the trailed price
            _sl_limit = round(new_sl_price * (0.999 if exit_side == "sell" else 1.001), 8)
            for _attempt in range(2):
                try:
                    await self.exchange.create_order(
                        symbol, "STOP_LOSS_LIMIT", exit_side, quantity,
                        price=_sl_limit,
                        params={"stopPrice": new_sl_price},
                    )
                    break
                except Exception as _pe:
                    if _attempt == 0:
                        await asyncio.sleep(0.5)
                    else:
                        logger.error(f"[Binance] replace SL order failed after retry: {_pe}")
                        return False

            logger.info(f"[Binance] ⚡ Trailing stop synced: {symbol} SL → {new_sl_price}")
            return True
        except Exception as exc:
            logger.warning(f"[Binance] update_stop_loss failed for {symbol}: {exc}")
            return False

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
        # Testnet uses /ws/ path; live uses /stream?streams= combined feed.
        # Note: price_stream_manager always uses force_paper=False (live feed) for
        # SL/TP evaluation, so this testnet branch is only hit if explicitly testing
        # the WebSocket with a paper BinanceClient directly.
        if self._paper:
            # Testnet only supports single-stream /ws/<stream> — use first symbol.
            first_stream = f"{symbols[0].replace('/', '').lower()}@aggTrade"
            url = f"wss://testnet.binance.vision/ws/{first_stream}"
        else:
            url = f"wss://stream.binance.com:9443/stream?streams={streams}"

        logger.info(f"[Binance] Starting price stream for: {symbols}")
        async with websockets.connect(url) as ws:
            while True:
                try:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    stream_data = data.get("data", data)
                    symbol_raw = stream_data.get("s", "")
                    price = float(stream_data.get("p", 0))
                    # Convert e.g. BTCUSDT → BTC/USDT, ETHBTC → ETH/BTC, BTCBUSD → BTC/BUSD
                    # Strategy: try each known quote suffix (longest first) to avoid partial matches.
                    _QUOTE_SUFFIXES = ["USDT", "BUSD", "USDC", "BTC", "ETH", "BNB", "EUR", "GBP"]
                    symbol = symbol_raw  # fallback: raw stream symbol
                    for _quote in _QUOTE_SUFFIXES:
                        if symbol_raw.endswith(_quote) and len(symbol_raw) > len(_quote):
                            symbol = symbol_raw[:-len(_quote)] + "/" + _quote
                            break
                    if price > 0:
                        await callback(symbol, price) if asyncio.iscoroutinefunction(callback) else callback(symbol, price)
                except Exception as e:
                    logger.error(f"[Binance] Stream error: {e}")
                    await asyncio.sleep(1)
