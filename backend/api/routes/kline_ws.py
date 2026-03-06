"""
WebSocket endpoint for live candle (kline) streaming.

Proxies broker-specific real-time data streams to the frontend chart panel.
Every message sent to the client:
  {"time": <unix seconds>, "open": f, "high": f, "low": f, "close": f, "volume": f}

Routes:
  ws://host/ws/kline?symbol=BTC%2FUSDT&timeframe=1m&broker=binance
  ws://host/ws/kline?symbol=AAPL&timeframe=1m&broker=alpaca
  ws://host/ws/kline?symbol=SPY&timeframe=1m&broker=ibkr

Broker details
--------------
Binance : public kline stream (production Binance, not testnet — richer data)
Alpaca  : trades stream via IEX feed; builds candles per timeframe period
IBKR    : polls reqMktData ticker every 1s via singleton manager; builds candles in memory
"""
import asyncio
import json
import time as _time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from loguru import logger

router = APIRouter()

_TF_SECS: dict[str, int] = {
    "1m": 60, "5m": 300, "15m": 900,
    "1h": 3_600, "4h": 14_400,
    "1d": 86_400, "3d": 259_200, "1w": 604_800,
}


@router.websocket("/kline")
async def kline_ws(
    websocket: WebSocket,
    symbol: str = Query(...),
    timeframe: str = Query(default="1m"),
    broker: str = Query(default="binance"),
):
    await websocket.accept()
    broker = broker.lower()
    tf_secs = _TF_SECS.get(timeframe, 60)
    logger.info(f"[KlineWS] Connected: {broker}/{symbol}/{timeframe}")
    try:
        if broker == "binance":
            await _stream_binance(websocket, symbol, timeframe)
        elif broker == "alpaca":
            await _stream_alpaca(websocket, symbol, timeframe, tf_secs)
        elif broker == "ibkr":
            await _stream_ibkr(websocket, symbol, timeframe, tf_secs)
        else:
            await websocket.close(code=4000, reason=f"Unknown broker: {broker}")
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"[KlineWS] Session error {broker}/{symbol}: {e}")
        try:
            await websocket.close(code=4001, reason=str(e))
        except Exception:
            pass
    finally:
        logger.info(f"[KlineWS] Disconnected: {broker}/{symbol}/{timeframe}")


async def _send(ws: WebSocket, payload: dict) -> bool:
    """Send JSON to the client. Returns False when the client has disconnected."""
    try:
        await ws.send_json(payload)
        return True
    except Exception:
        return False


# ─── Binance ──────────────────────────────────────────────────────────────────

async def _stream_binance(ws: WebSocket, symbol: str, timeframe: str):
    """Proxy Binance public kline WebSocket (production stream, always real-time)."""
    import websockets as wsl

    sym = symbol.replace("/", "").lower()
    url = f"wss://stream.binance.com/ws/{sym}@kline_{timeframe}"
    logger.info(f"[KlineWS][Binance] Connecting {url}")

    async with wsl.connect(url, ping_interval=20, ping_timeout=10) as bws:
        async for raw in bws:
            try:
                msg = json.loads(raw)
                k = msg.get("k") or msg.get("data", {}).get("k")
                if not k:
                    continue
                if not await _send(ws, {
                    "time":   int(k["t"]) // 1000,
                    "open":   float(k["o"]),
                    "high":   float(k["h"]),
                    "low":    float(k["l"]),
                    "close":  float(k["c"]),
                    "volume": float(k["v"]),
                }):
                    break
            except Exception as e:
                logger.debug(f"[KlineWS][Binance] Parse error: {e}")


# ─── Alpaca ───────────────────────────────────────────────────────────────────

async def _stream_alpaca(ws: WebSocket, symbol: str, timeframe: str, tf_secs: int):
    """
    Proxy Alpaca data stream (trades feed via IEX).

    Subscribes to individual trades and builds OHLCV candles locally, one per
    timeframe period.  Sends are throttled to at most once per second to avoid
    flooding the browser with hundreds of per-trade messages (AAPL can produce
    200+ ticks/s which would freeze the JS thread).
    """
    import websockets as wsl
    from config import settings

    sym = symbol.upper()
    feed = getattr(settings, "alpaca_data_feed", "iex")
    url = f"wss://stream.data.alpaca.markets/v2/{feed}"
    logger.info(f"[KlineWS][Alpaca] Connecting {url} feed={feed} sym={sym}")

    now_sec = int(_time.time())
    period_start = (now_sec // tf_secs) * tf_secs
    o = h = l = c_price = vol = 0.0
    last_sent = 0.0          # monotonic timestamp of last _send call
    THROTTLE_SECS = 1.0      # max one update per second to the browser

    async with wsl.connect(url, ping_interval=20, ping_timeout=10) as aws:
        # Auth
        await aws.send(json.dumps({
            "action": "auth",
            "key": settings.alpaca_api_key,
            "secret": settings.alpaca_api_secret,
        }))
        auth_raw = await asyncio.wait_for(aws.recv(), timeout=5)
        logger.debug(f"[KlineWS][Alpaca] Auth: {auth_raw[:120]}")

        # Subscribe to trades
        await aws.send(json.dumps({"action": "subscribe", "trades": [sym]}))
        sub_raw = await asyncio.wait_for(aws.recv(), timeout=5)
        logger.debug(f"[KlineWS][Alpaca] Subscribe: {sub_raw[:120]}")

        # No-data timeout: if no valid trade arrives within 60 s the market is
        # likely closed.  Close the WS so the frontend shows the overlay.
        NO_DATA_TIMEOUT = 60.0
        while True:
            try:
                raw = await asyncio.wait_for(aws.recv(), timeout=NO_DATA_TIMEOUT)
            except asyncio.TimeoutError:
                logger.info(
                    f"[KlineWS][Alpaca] No trades for {NO_DATA_TIMEOUT:.0f}s "
                    f"— market likely closed for {sym}"
                )
                return  # causes ws.onclose on the frontend
            try:
                msgs = json.loads(raw)
                if not isinstance(msgs, list):
                    msgs = [msgs]
                for msg in msgs:
                    if msg.get("T") != "t" or msg.get("S", "").upper() != sym:
                        continue
                    price = float(msg.get("p", 0))
                    size  = float(msg.get("s", 0))
                    if price <= 0:
                        continue

                    now_sec = int(_time.time())
                    current_period = (now_sec // tf_secs) * tf_secs

                    if current_period != period_start:
                        # Finalize previous period — always send this immediately
                        if o > 0:
                            if not await _send(ws, {
                                "time": period_start,
                                "open": o, "high": h, "low": l, "close": c_price, "volume": vol,
                            }):
                                return
                            last_sent = _time.monotonic()
                        period_start = current_period
                        o = h = l = price
                        vol = 0.0

                    if o == 0:
                        o = h = l = price
                    else:
                        h = max(h, price)
                        l = min(l, price)
                    c_price = price
                    vol += size

                    # Throttle: only push to browser at most once per second
                    now_mono = _time.monotonic()
                    if now_mono - last_sent < THROTTLE_SECS:
                        continue
                    last_sent = now_mono

                    if not await _send(ws, {
                        "time": period_start,
                        "open": o, "high": h, "low": l, "close": c_price, "volume": vol,
                    }):
                        return
            except Exception as e:
                logger.debug(f"[KlineWS][Alpaca] Parse error: {e}")


# ─── IBKR ─────────────────────────────────────────────────────────────────────

async def _stream_ibkr(ws: WebSocket, symbol: str, timeframe: str, tf_secs: int):
    """
    Stream live IBKR price ticks as candle updates via the singleton manager.

    Uses reqMktData (subscribed on the ib_insync background thread) and polls
    the Ticker object every 1 s from the FastAPI async loop.  Works with both
    the live gateway (port 7496) and the paper gateway (port 7497) — real-time
    data is available on either when the live account has market data subscriptions.

    Stops streaming and closes the WebSocket (so the frontend falls back to
    REST polling) if no valid price is received for 60 consecutive seconds.
    """
    from brokers.ibkr_client import get_ibkr_manager
    from ib_insync import Stock

    loop = asyncio.get_event_loop()
    mgr = get_ibkr_manager()
    sym = symbol.upper()
    contract = Stock(sym, "SMART", "USD")

    logger.info(f"[KlineWS][IBKR] Subscribing to live ticks for {sym}")
    try:
        ticker = await loop.run_in_executor(
            None, lambda: mgr.subscribe_mkt_data(contract)
        )
    except Exception as e:
        logger.warning(f"[KlineWS][IBKR] Could not subscribe to {sym}: {e}")
        try:
            await ws.close(code=4002, reason=f"IBKR: {e}")
        except Exception:
            pass
        return

    now_sec = int(_time.time())
    period_start = (now_sec // tf_secs) * tf_secs
    o = h = l = c_price = 0.0
    last_valid_ts = _time.monotonic()

    try:
        while True:
            await asyncio.sleep(1)
            # Reading ticker attributes from the FastAPI loop is safe under CPython's GIL
            price = float(ticker.last or ticker.close or ticker.bid or 0)

            if price <= 0:
                if _time.monotonic() - last_valid_ts > 60:
                    logger.warning(f"[KlineWS][IBKR] No valid price for {sym} in 60 s — closing")
                    break
                continue

            last_valid_ts = _time.monotonic()
            now_sec = int(_time.time())
            current_period = (now_sec // tf_secs) * tf_secs

            if current_period != period_start:
                # Flush finalized candle
                if o > 0 and not await _send(ws, {
                    "time": period_start,
                    "open": o, "high": h, "low": l, "close": c_price, "volume": 0.0,
                }):
                    break
                period_start = current_period
                o = h = l = price

            if o == 0:
                o = h = l = price
            else:
                h = max(h, price)
                l = min(l, price)
            c_price = price

            if not await _send(ws, {
                "time": period_start,
                "open": o, "high": h, "low": l, "close": c_price, "volume": 0.0,
            }):
                break
    finally:
        await loop.run_in_executor(None, lambda: mgr.unsubscribe_mkt_data(contract))
        logger.info(f"[KlineWS][IBKR] Unsubscribed market data for {sym}")
