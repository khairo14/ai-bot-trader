from datetime import datetime, timezone
import asyncio
import time
from collections import defaultdict
from typing import Optional, Any
from fastapi import APIRouter, Query, HTTPException, Request
from sqlalchemy import select, desc
from loguru import logger

from db.database import AsyncSessionLocal
from db.models import Signal, Trade, LiveTrade, OrderStatus

router = APIRouter()

# ── Simple per-user rate limiter for the charts candles endpoint ─────────────
# 60 requests per minute per user — enough for a 4-panel grid with live polls.
_RATE_LIMIT_WINDOW = 60   # seconds
_RATE_LIMIT_MAX    = 60   # requests per window (was 10 — too low for multi-panel)
_rate_hits: dict[str, list[float]] = defaultdict(list)

def _check_chart_rate_limit(request: Request) -> None:
    """Raise HTTP 429 if the calling user exceeds the chart rate limit."""
    user_id = getattr(getattr(request.state, "user", None), "id", None)
    key = f"chart:{user_id or request.client.host if request.client else 'anon'}"
    now = time.monotonic()
    hits = _rate_hits[key]
    # Expire old hits outside the window
    fresh = [t for t in hits if now - t < _RATE_LIMIT_WINDOW]
    if fresh:
        _rate_hits[key] = fresh
    else:
        # No recent hits — remove the key entirely to prevent unbounded growth
        _rate_hits.pop(key, None)
        fresh = []
    if len(fresh) >= _RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail="Chart rate limit exceeded — please wait a moment")
    _rate_hits[key] = fresh + [now]

# ── In-memory candle response cache ──────────────────────────────────────────
# Prevents the same symbol/timeframe/range from hitting the broker API multiple
# times when several panels mount simultaneously or the live-poll fires rapidly.
_candle_cache: dict[str, tuple[float, Any]] = {}   # key → (expires_mono, payload)

def _cache_key(symbol: str, timeframe: str, broker: str, since: int, until: int, tf_ms: int) -> str:
    """Round since/until to tf_ms granularity so near-identical requests share a cache slot."""
    s = (since // tf_ms) * tf_ms
    u = (until // tf_ms) * tf_ms
    return f"{broker}:{symbol}:{timeframe}:{s}:{u}"

def _cache_ttl(tf_ms: int) -> float:
    """Cache lifetime = quarter of the timeframe, clamped to [10 s, 120 s]."""
    return max(10.0, min(120.0, tf_ms / 1000 / 4))

def _cache_get(key: str) -> Optional[Any]:
    entry = _candle_cache.get(key)
    if entry is None:
        return None
    expires, payload = entry
    if time.monotonic() > expires:
        _candle_cache.pop(key, None)
        return None
    return payload

def _cache_set(key: str, payload: Any, ttl: float) -> None:
    # Prune stale entries before inserting to prevent unbounded growth
    now = time.monotonic()
    stale = [k for k, (exp, _) in _candle_cache.items() if now > exp]
    for k in stale:
        _candle_cache.pop(k, None)
    _candle_cache[key] = (now + ttl, payload)

# Popular Alpaca/IBKR stocks — fallback when broker can't enumerate assets
_ALPACA_DEFAULTS = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","AMD","INTC","ORCL",
    "CRM","NFLX","ADBE","PYPL","UBER","LYFT","ABNB","SHOP","SQ","SNAP",
    "TWTR","ROKU","ZM","PLTR","RBLX","DKNG","HOOD","COIN","MSTR","GME",
    "AMC","BB","NOK","BBBY","SNDL","WKHS","CLOV","MMAT","WISH","SPCE",
    "SPY","QQQ","IWM","DIA","GLD","SLV","USO","TLT","HYG","XLF",
    "XLE","XLK","XLV","ARKK","ARKG","ARKW","ARKF","ARKX","VTI","VOO",
    "BABA","JD","NIO","XPEV","LI","BIDU","PDD","TCEHY","TME","FUTU",
    "JPM","BAC","GS","MS","WFC","C","BRK.B","V","MA","AXP",
    "PFE","MRNA","JNJ","ABBV","LLY","BMY","GILD","REGN","BIIB","VRTX",
    "TSMC","SOFI","LCID","RIVN","F","GM","STLA","TM","HMC","RACE",
]
_IBKR_DEFAULTS = [
    # US stocks
    "SPY","QQQ","IWM","DIA","AAPL","MSFT","NVDA","AMZN","GOOGL","META",
    "TSLA","AMD","JPM","BAC","GS","GLD","SLV","USO","TLT","XLF",
    "BRK.B","V","MA","JNJ","PFE","MRNA","XOM","CVX","BABA","NIO",
    # Forex (IDEALPRO FX)
    "EUR/USD","GBP/USD","USD/JPY","AUD/USD","USD/CAD","USD/CHF","NZD/USD","EUR/GBP",
    # European equities (Alternative European Equities subscription)
    "SAP","SIE","ALV","BMW","BAYN",           # Xetra
    "ASML","INGA","PHIA",                      # Amsterdam
    "AZN","SHEL","HSBA","BP","GSK",           # LSE
    "NESN","NOVN","ROG",                        # SWX
    "OR","TTE","BNP",                           # Paris
]


@router.get("/symbols")
async def get_symbols(
    broker: str = Query(default="binance", description="binance | alpaca | ibkr"),
):
    """Return tradeable symbols for a broker. Used to populate the chart search combobox."""
    b = broker.lower()
    try:
        if b == "binance":
            from brokers.binance_client import BinanceClient
            client = BinanceClient()
            try:
                await client._ensure_markets()
                # Filter to active USDT spot pairs only
                symbols = sorted([
                    s for s in (client.exchange.markets or {})  # type: ignore[union-attr]
                    if s.endswith("/USDT") and (client.exchange.markets or {}).get(s, {}).get("active", True)  # type: ignore[union-attr]
                ])
            finally:
                await client.close()
            return {"symbols": symbols, "broker": b}

        elif b == "alpaca":
            try:
                from alpaca.trading.client import TradingClient
                from alpaca.trading.requests import GetAssetsRequest
                from alpaca.trading.enums import AssetClass as AlpacaAssetClass, AssetStatus
                from config import settings
                import asyncio
                tc = TradingClient(api_key=settings.alpaca_api_key, secret_key=settings.alpaca_api_secret, paper=True)
                req = GetAssetsRequest(asset_class=AlpacaAssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
                assets = await asyncio.to_thread(tc.get_all_assets, req)
                symbols = sorted([a.symbol for a in assets if a.tradable])  # type: ignore[union-attr]
                return {"symbols": symbols, "broker": b}
            except Exception:
                return {"symbols": _ALPACA_DEFAULTS, "broker": b}

        elif b == "ibkr":
            return {"symbols": _IBKR_DEFAULTS, "broker": b}

        else:
            raise HTTPException(status_code=400, detail=f"Unknown broker: {broker}")

    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"[Charts] get_symbols fallback for {broker}: {e}")
        fallback = _ALPACA_DEFAULTS if b == "alpaca" else _IBKR_DEFAULTS if b == "ibkr" else []
        return {"symbols": fallback, "broker": b}


def _get_broker_client(broker: str):
    b = broker.lower()
    if b == "binance":
        from brokers.binance_client import BinanceClient
        return BinanceClient()
    elif b == "alpaca":
        from brokers.alpaca_client import AlpacaClient
        return AlpacaClient()
    elif b == "ibkr":
        from brokers.ibkr_client import IBKRClient
        return IBKRClient()
    else:
        raise ValueError(f"Unknown broker: {broker}")


# ms per candle for each supported timeframe
_TF_MS: dict[str, int] = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000,
    "1h": 3_600_000, "4h": 14_400_000,
    "1d": 86_400_000, "3d": 259_200_000, "1w": 604_800_000,
}


@router.get("/candles")
async def get_candles(
    request: Request,
    symbol: str = Query(..., description="e.g. BTC/USDT or AAPL"),
    timeframe: str = Query(default="1h", description="1m 5m 15m 1h 4h 1d 3d 1w"),
    broker: str = Query(default="binance", description="binance | alpaca | ibkr"),
    since: Optional[int] = Query(default=None, description="Start time unix ms"),
    until: Optional[int] = Query(default=None, description="End time unix ms (default: now)"),
):
    """
    Return OHLCV candles for lightweight-charts.
    Paginates Binance's 1000-candle-per-call limit automatically so any date
    range works correctly regardless of timeframe.
    """
    _check_chart_rate_limit(request)
    CHUNK = 1000          # Binance max per request
    MAX_CANDLES = 5000    # safety cap (~5 paginated calls)

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    tf_ms = _TF_MS.get(timeframe, 3_600_000)

    if until is None:
        until = now_ms
    if since is None:
        since = until - 200 * tf_ms  # default: last 200 candles

    # Cache check — return immediately for duplicate/recent requests.
    # IBKR is included: we only ever store non-empty results (see _cache_set
    # guard below), so a cache hit is always valid data — no risk of serving
    # an empty/warmup response.
    ck = _cache_key(symbol.upper(), timeframe, broker.lower(), since, until, tf_ms)
    cached = _cache_get(ck)
    if cached is not None:
        logger.debug(f"[Charts] cache hit {symbol}/{timeframe}")
        return cached
    try:
        # Always use production Binance for OHLCV — testnet has <30 days history
        if broker.lower() == "binance":
            from brokers.binance_client import BinanceClient
            client = BinanceClient(paper=False)
        elif broker.lower() == "ibkr":
            # For IBKR charts: only start the singleton background thread — do NOT call
            # connect() / ensure_connected_sync() here.  connect() holds _connect_lock
            # for up to ~60 s during a Gateway reconnect, which can starve a concurrent
            # ForwardEngine trade call that also needs that lock.  _do_ohlcv() calls
            # _ensure_connected() internally, and _reconnect_loop fires every 15 s, so
            # the first OHLCV retry (3 attempts × 3 s) will catch up naturally.
            from brokers.ibkr_client import get_ibkr_manager as _ibkr_mgr
            _ibkr_mgr()._start()
            client = _get_broker_client(broker)
        else:
            client = _get_broker_client(broker)
            await client.connect()   # no-op for Alpaca

        all_rows: list[tuple[int, object]] = []
        current_since = since

        # IBKR does not support a `since` parameter — it always returns the most
        # recent N bars computed from a duration string.  Call it *once* with a
        # limit derived from the requested date range, then filter by since/until.
        is_ibkr = broker.lower() == "ibkr"

        try:
            if is_ibkr:
                ibkr_limit = max(CHUNK, int((until - since) / tf_ms) + 10)
                # Retry up to 3 times — the first call often returns empty while the
                # IBKR Gateway singleton is warming up or recovering from a pacing delay.
                df = None
                for _attempt in range(3):
                    try:
                        df = await client.get_ohlcv(
                            symbol.upper(), timeframe=timeframe,
                            limit=ibkr_limit, since=since,
                        )
                    except Exception as _ohlcv_exc:
                        logger.warning(
                            f"[Charts] IBKR get_ohlcv error for {symbol}/{timeframe} "
                            f"(attempt {_attempt + 1}/3): {_ohlcv_exc}"
                        )
                        df = None
                    if df is not None and not df.empty:
                        break
                    if _attempt < 2:
                        logger.info(
                            f"[Charts] IBKR returned empty for {symbol}/{timeframe} "
                            f"— retrying in 1 s (attempt {_attempt + 1}/3, Gateway warmup)"
                        )
                        await asyncio.sleep(1)
                if df is not None and not df.empty:
                    for ts, row in df.iterrows():
                        try:
                            t_ms = int(ts.timestamp() * 1000)  # type: ignore[union-attr]
                        except Exception:
                            continue
                        if t_ms < since or t_ms > until:
                            continue
                        all_rows.append((t_ms, row))
            else:
                while current_since < until and len(all_rows) < MAX_CANDLES:
                    df = await client.get_ohlcv(
                        symbol.upper(), timeframe=timeframe,
                        limit=CHUNK, since=current_since,
                    )
                    if df.empty:
                        break

                    for ts, row in df.iterrows():
                        try:
                            t_ms = int(ts.timestamp() * 1000)  # type: ignore[union-attr]
                        except Exception:
                            break
                        if t_ms > until:
                            break
                        all_rows.append((t_ms, row))

                    # Advance past last fetched candle
                    try:
                        last_ms = int(df.index[-1].timestamp() * 1000)  # type: ignore[union-attr]
                    except Exception:
                        break
                    if last_ms <= current_since:
                        break  # no progress
                    current_since = last_ms + tf_ms

                    if len(df) < CHUNK:
                        break  # Binance returned less than a full chunk → no more data
        finally:
            if hasattr(client, "close"):
                try:
                    await client.close()
                except Exception:
                    pass

        # Sort by timestamp and deduplicate at SECOND resolution — IBKR can
        # return out-of-order or sub-second duplicate bars (DST transitions,
        # reconnect overlaps).  lightweight-charts requires strictly-increasing
        # integer `time` values (in seconds); a single collision makes setData()
        # throw 'Value of property time must be greater of the previous one',
        # which the frontend catch reports as 'Failed to load chart data.'.
        # We dedup by the final `time` (t_ms // 1000) rather than by raw ms so
        # two bars with the same second but different sub-second offsets don't
        # both appear to be unique and then collide in the output array.
        all_rows.sort(key=lambda x: x[0])
        seen_secs: set[int] = set()
        deduped: list[tuple[int, object]] = []
        for t_ms, row in all_rows:
            t_sec = t_ms // 1000
            if t_sec not in seen_secs:
                seen_secs.add(t_sec)
                deduped.append((t_ms, row))
        all_rows = deduped

        import math as _math
        candles = []
        for t_ms, row in all_rows[:MAX_CANDLES]:
            try:
                o = round(float(row["open"]),  8)  # type: ignore[index]
                h = round(float(row["high"]),  8)  # type: ignore[index]
                lo = round(float(row["low"]),   8)  # type: ignore[index]
                c = round(float(row["close"]), 8)  # type: ignore[index]
                # IBKR MIDPOINT bars: volume=-1 when unavailable; clamp to 0.
                # Also skip any bar where OHLC contains NaN/Inf (ib_insync
                # sentinel values) or where all prices are 0 (empty bar) —
                # these cause NaN to propagate through EMA/RSI on the frontend
                # and make lightweight-charts throw 'Value is null'.
                vol = max(0.0, round(float(row.get("volume", 0)), 4))  # type: ignore[union-attr]
                if any(_math.isnan(v) or _math.isinf(v) for v in (o, h, lo, c)):
                    continue
                # Skip bars where any price field is zero or negative — IBKR
                # returns 0.0 for unavailable prices (e.g. closed-session bars)
                # and -1.0 as a sentinel.  A zero close fed through EMA/RSI
                # produces NaN on the frontend, and a zero price rendered by
                # lightweight-charts creates a visually broken candle.
                if o <= 0 or h <= 0 or lo <= 0 or c <= 0:
                    continue
            except Exception:
                continue
            candles.append({"time": t_ms // 1000, "open": o, "high": h, "low": lo, "close": c, "volume": vol})

        # F-092: Forward-fill gaps in IBKR OHLCV for chart display only.
        # IBKR returns sparse bars during thin-liquidity or off-hours periods,
        # leaving blank spaces on the chart.  We fill every missing tf-slot
        # with a flat doji (open=high=low=close=prev_close, volume=0) so the
        # chart series is continuous.  This is purely cosmetic — the strategy
        # engine fetches its own unmodified bars directly from get_ohlcv().
        # We only forward-fill for IBKR; Binance/Alpaca are already continuous.
        if broker.lower() == "ibkr" and len(candles) >= 2:
            tf_secs = tf_ms // 1000
            filled: list[dict] = []
            for i, c in enumerate(candles):
                filled.append(c)
                if i < len(candles) - 1:
                    next_t = candles[i + 1]["time"]
                    cur_t  = c["time"] + tf_secs
                    # Fill every missing slot between this candle and the next
                    while cur_t < next_t and len(filled) < MAX_CANDLES:
                        filled.append({
                            "time":   cur_t,
                            "open":   c["close"],
                            "high":   c["close"],
                            "low":    c["close"],
                            "close":  c["close"],
                            "volume": 0.0,
                        })
                        cur_t += tf_secs
            candles = filled

        logger.info(f"[Charts] {broker.lower()}/{symbol.upper()}/{timeframe}: {len(candles)} candles (since={since}, until={until})")
        result = {
            "candles": candles,
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "broker": broker.lower(),
            "candle_count": len(candles),
        }

        # Only cache non-empty results — empty IBKR warmup responses are never
        # stored, so a cache hit is always genuine data that is safe to serve.
        if candles:
            _cache_set(ck, result, _cache_ttl(tf_ms))
        return result

    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logger.error(f"[Charts] get_candles error for {symbol}/{timeframe}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/signals")
async def get_chart_signals(
    symbol: str = Query(...),
    timeframe: str = Query(default="1h"),
    broker: str = Query(default="binance"),
    limit: int = Query(default=500, le=1000),
    since: Optional[int] = Query(default=None, description="Start unix ms"),
    until: Optional[int] = Query(default=None, description="End unix ms"),
):
    """Return non-HOLD signals for a symbol/timeframe as chart markers."""
    filters = [
        Signal.symbol == symbol.upper(),
        Signal.timeframe == timeframe,
        Signal.broker == broker.lower(),
        Signal.signal != "HOLD",
    ]
    if since is not None:
        since_dt = datetime.fromtimestamp(since / 1000, tz=timezone.utc).replace(tzinfo=None)
        filters.append(Signal.created_at >= since_dt)
    if until is not None:
        until_dt = datetime.fromtimestamp(until / 1000, tz=timezone.utc).replace(tzinfo=None)
        filters.append(Signal.created_at <= until_dt)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Signal)
            .where(*filters)
            .order_by(desc(Signal.created_at))
            .limit(limit)
        )
        rows = result.scalars().all()

    markers = []
    for s in rows:
        if not s.created_at:
            continue
        markers.append({
            "time": int(s.created_at.timestamp()),
            "signal": s.signal.value if hasattr(s.signal, "value") else s.signal,
            "price": s.entry_price,
            "confidence": s.confidence,
            "strategy_name": s.strategy_name,
            "stop_loss": s.stop_loss,
            "take_profit": s.take_profit,
        })

    return {
        "markers": markers,
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "broker": broker.lower(),
    }


@router.get("/trades")
async def get_chart_trades(
    symbol: str = Query(...),
    broker: str = Query(default="binance"),
    since: Optional[int] = Query(default=None, description="Start unix ms"),
    until: Optional[int] = Query(default=None, description="End unix ms"),
    limit: int = Query(default=500, le=2000),
):
    """
    Return executed trades for a symbol/broker as chart overlays.
    Each trade includes entry marker, exit marker, stop_loss, take_profit,
    and realised PnL — allowing the frontend to draw entry/exit arrows and
    TP/SL horizontal price levels on the chart.
    """
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    if until is None:
        until = now_ms
    if since is None:
        since = until - 365 * 24 * 3600 * 1000  # default: last 1 year

    since_dt = datetime.fromtimestamp(since / 1000, tz=timezone.utc).replace(tzinfo=None)
    until_dt = datetime.fromtimestamp(until / 1000, tz=timezone.utc).replace(tzinfo=None)

    async with AsyncSessionLocal() as session:
        base_filters_paper = [
            Trade.symbol == symbol.upper(),
            Trade.broker == broker.lower(),
            Trade.status.in_([OrderStatus.OPEN, OrderStatus.FILLED]),
            Trade.opened_at >= since_dt,
            Trade.opened_at <= until_dt,
        ]
        base_filters_live = [
            LiveTrade.symbol == symbol.upper(),
            LiveTrade.broker == broker.lower(),
            LiveTrade.status.in_([OrderStatus.OPEN, OrderStatus.FILLED]),
            LiveTrade.opened_at >= since_dt,
            LiveTrade.opened_at <= until_dt,
        ]
        paper_result = await session.execute(
            select(Trade).where(*base_filters_paper).order_by(desc(Trade.opened_at)).limit(limit)
        )
        live_result = await session.execute(
            select(LiveTrade).where(*base_filters_live).order_by(desc(LiveTrade.opened_at)).limit(limit)
        )
        trades = sorted(
            paper_result.scalars().all() + live_result.scalars().all(),
            key=lambda t: t.opened_at or datetime.min,
            reverse=True,
        )[:limit]

    out = []
    for t in trades:
        out.append({
            "id": t.id,
            "symbol": t.symbol,
            "side": t.side,
            "status": t.status.value if hasattr(t.status, "value") else t.status,
            "is_paper": t.is_paper,
            "strategy_name": t.strategy_name,
            # Entry
            "entry_price": t.entry_price,
            "entry_time": int(t.opened_at.timestamp()) if t.opened_at else None,
            # Exit (None if still open)
            "exit_price": t.exit_price,
            "exit_time": int(t.closed_at.timestamp()) if t.closed_at else None,
            # Risk levels
            "stop_loss": t.stop_loss,
            "take_profit": t.take_profit,
            # Performance
            "pnl": t.pnl,
            "pnl_pct": t.pnl_pct,
            "quantity": t.quantity,
        })

    return {
        "trades": out,
        "symbol": symbol.upper(),
        "broker": broker.lower(),
        "count": len(out),
    }
