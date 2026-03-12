import asyncio
import math
import os
import threading
import time
import pandas as pd
from typing import List, Optional, Callable
from loguru import logger

# ib_insync's eventkit calls asyncio.get_event_loop() at module import time.
# In Celery forked workers (Python 3.10+) there is no default event loop in the
# forked child process, which raises RuntimeError before any code runs.
# Ensure one exists so the import succeeds; tasks use asyncio.run() which
# creates its own isolated loop and is unaffected by this.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ib_insync import IB, Stock, Forex as IBForex, Option, Contract, Order, MarketOrder, LimitOrder, StopLimitOrder, StopOrder, Trade as IBTrade

from config import settings
from brokers.base import AbstractBroker, OrderResult, Position, Balance


# ── Contract factory ─────────────────────────────────────────────────────────

_FX_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "HKD", "SGD"}

# G12: European equity routing loaded from config/ibkr_eu_stocks.json
# Edit that file to add new EU tickers — no code change required.
import json as _json
import pathlib as _pathlib

_EU_STOCKS_PATH = _pathlib.Path(__file__).resolve().parent.parent / "config" / "ibkr_eu_stocks.json"

def _load_eu_stocks() -> dict[str, tuple[str, str]]:
    """Load EU stock routing map from config file. Falls back to empty dict on error."""
    try:
        raw = _json.loads(_EU_STOCKS_PATH.read_text())
        return {
            k: (v[0], v[1])
            for k, v in raw.items()
            if not k.startswith("_comment") and isinstance(v, list) and len(v) == 2
        }
    except Exception as _e:
        logger.warning(f"[IBKR] Could not load EU stocks config ({_EU_STOCKS_PATH}): {_e}")
        return {}

_EU_STOCKS: dict[str, tuple[str, str]] = _load_eu_stocks()

def _ibkr_contract(symbol: str):
    """
    Return the correct ib_insync contract for a symbol string.
    - 'EUR/USD' or 'EURUSD' with known 3-letter currencies → IBForex via IDEALPRO
    - Known EU tickers → Stock on their home exchange with local currency
    - Everything else → US stock via SMART routing
    """
    clean = symbol.replace("/", "").upper()
    if (len(clean) == 6 and clean.isalpha()
            and clean[:3] in _FX_CURRENCIES and clean[3:] in _FX_CURRENCIES):
        return IBForex(clean)
    ticker = symbol.upper()
    if ticker in _EU_STOCKS:
        exch, curr = _EU_STOCKS[ticker]
        return Stock(ticker, exch, curr)
    return Stock(clean, "SMART", "USD")


async def _dispatch_ibkr_notif(title: str, message: str, level: str = "error") -> None:
    """Persist an in-app notification from within the IBKR background event loop."""
    try:
        from db.database import AsyncSessionLocal
        from notifications.notifier import dispatch as _notif_dispatch
        async with AsyncSessionLocal() as _db:
            await _notif_dispatch(_db, title=title, message=message, level=level, category="system")
            await _db.commit()
    except Exception as _e:
        logger.debug(f"[IBKR] In-app notification dispatch failed: {_e}")


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
    _CACHE_TTL        = 60.0   # return cached balance for up to 60 s
    _SETTLE_SECS      = 5.0    # initial wait after connect for Gateway to push account data
    _ACCT_POLL_SECS   = 0.5    # poll interval when waiting for accountValues to populate
    _ACCT_TIMEOUT     = 20.0   # max seconds to wait for account data after connect
    _CONNECT_TIMEOUT  = 15     # seconds for connectAsync
    _CONNECT_RETRIES  = 4      # attempts before giving up in a single _ensure_connected call
    _CONNECT_RETRY_DELAY = 8.0  # seconds between connect retries

    def __init__(self, client_id: int | None = None) -> None:
        if client_id is not None:
            self._client_id: int = client_id
        else:
            self._client_id = settings.ibkr_client_id
        self._ib: Optional[IB] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # Asyncio lock: prevents concurrent connectAsync() on the same IB instance
        # (e.g. _reconnect_loop racing with _fetch_async on startup)
        self._connect_lock: Optional[asyncio.Lock] = None
        # F-102: serializes concurrent reqPositionsAsync() calls — TWS only sends
        # positionEnd once; a second concurrent caller would hang until timeout.
        self._positions_lock: Optional[asyncio.Lock] = None
        self._cached: Optional[Balance] = None
        self._cache_ts: float = 0.0
        self._started = False
        # F-082: contracts for open bracket orders — keep market data subscription
        # alive after fill so IBKR paper SL/TP child orders have a price feed.
        # Entries are added in place_order (bracket path) and removed in
        # cancel_bracket_subscription() which is called from close_position().
        self._bracket_subscriptions: dict = {}  # symbol_key → contract
        # Reconnect resilience: exponential backoff + hard-reject guard (Error 326)
        self._reconnect_backoff: float = 15.0   # current sleep interval (grows on failure)
        self._reconnect_consec_fails: int = 0   # consecutive failed reconnect cycles
        self._hard_rejected_until: float = 0.0  # epoch — skip reconnect until this time (Error 326)

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
        # Create asyncio locks on the background loop — they must be created on
        # the loop that will use them (asyncio.Lock is not thread-safe across loops).
        self._connect_lock = asyncio.Lock()
        self._positions_lock = asyncio.Lock()  # F-102: one reqPositionsAsync at a time
        self._loop.create_task(self._reconnect_loop())
        self._loop.run_forever()

    async def _reconnect_loop(self) -> None:
        """Background task: silently reconnect whenever the Gateway drops us.

        Behaviour:
        - Error 326 (clientId conflict): back off 120s so the stale TWS socket
          can time out, then retry — does NOT count as a consecutive failure.
        - Genuine network failure: exponential backoff 15s → 30s → 60s → … → 300s.
        - After 20 consecutive genuine failures: stop the loop and log CRITICAL.
          The FastAPI/Celery process must be restarted to recover (container
          health-check will handle this in production).
        """
        _BACKOFF_MIN = 15.0
        _BACKOFF_MAX = 300.0
        _MAX_CONSEC_FAILS = 20
        # Brief initial delay so the loop doesn't race with the first _fetch_async call.
        await asyncio.sleep(5)
        while True:
            try:
                if self._ib is None or not self._ib.isConnected():
                    # ── Error 326 backoff guard ──────────────────────────────
                    remaining = self._hard_rejected_until - time.time()
                    if remaining > 0:
                        logger.warning(
                            f"[IBKR] clientId conflict (Error 326) backoff — IBKR trading suspended, "
                            f"waiting {remaining:.0f}s for stale TWS socket to release"
                        )
                        await asyncio.sleep(min(remaining + 1.0, _BACKOFF_MAX))
                        continue
                    logger.info("[IBKR] Connection lost — attempting auto-reconnect …")
                    reconnected = await self._ensure_connected()

                    if reconnected and self._ib:
                        # Success — notify if we had prior failures so the operator
                        # knows trading has resumed.
                        if self._reconnect_consec_fails > 0:
                            await _dispatch_ibkr_notif(
                                "IBKR Connection Restored",
                                f"IBKR reconnected successfully after {self._reconnect_consec_fails} "
                                "failed attempt(s). Trading has resumed.",
                                level="success",
                            )
                        # Reset counters and re-subscribe
                        self._reconnect_consec_fails = 0
                        self._reconnect_backoff = _BACKOFF_MIN
                        try:
                            self._ib.reqAccountUpdates(True)  # type: ignore[arg-type]
                        except Exception:
                            pass
                        try:
                            await self._ib.reqPositionsAsync()
                        except Exception:
                            pass
                        for _sym, _contract in list(self._bracket_subscriptions.items()):
                            try:
                                self._ib.reqMktData(_contract, "", False, False)
                                logger.debug(f"[IBKR] Re-subscribed mkt data for bracket: {_sym}")
                            except Exception as _sub_err:
                                logger.debug(f"[IBKR] Re-subscribe failed for {_sym}: {_sub_err}")
                    else:
                        # Failure — exponential backoff, cap consecutive count
                        self._reconnect_consec_fails += 1
                        self._reconnect_backoff = min(self._reconnect_backoff * 2, _BACKOFF_MAX)
                        logger.warning(
                            f"[IBKR] Reconnect failed ({self._reconnect_consec_fails}/"
                            f"{_MAX_CONSEC_FAILS}) — next retry in {self._reconnect_backoff:.0f}s"
                        )
                        if self._reconnect_consec_fails >= _MAX_CONSEC_FAILS:
                            logger.critical(
                                f"[IBKR] {_MAX_CONSEC_FAILS} consecutive reconnect failures — "
                                "background reconnect loop stopped. Restart the container to recover."
                            )
                            await _dispatch_ibkr_notif(
                                "IBKR Trading Stopped — Reconnect Failed",
                                f"IBKR failed to reconnect after {_MAX_CONSEC_FAILS} consecutive "
                                "attempts. All IBKR trading is stopped. Restart the container to recover.",
                            )
                            return  # stop spinning; container restart will revive this
                else:
                    # Already connected — reset backoff so a future drop recovers quickly
                    self._reconnect_consec_fails = 0
                    self._reconnect_backoff = _BACKOFF_MIN
            except Exception as exc:
                logger.debug(f"[IBKR] Auto-reconnect attempt failed: {exc}")
            await asyncio.sleep(self._reconnect_backoff)

    def _submit(self, coro, timeout: float = 30.0):
        """Run a coroutine on the background loop and block until done."""
        assert self._loop is not None
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, self._loop)  # type: ignore[arg-type]
        except RuntimeError as exc:
            # Bug-20 FIX: loop may have been closed between the is_closed() check
            # and this call during worker shutdown — treat as a connection error.
            raise ConnectionError(f"[IBKR] Background event loop is closed: {exc}") from exc
        return fut.result(timeout=timeout)

    # ── connection helpers ────────────────────────────────────────────────────

    async def _ensure_connected(self) -> bool:
        # Fast-path: already connected.
        if self._ib is not None and self._ib.isConnected():
            return True

        # Serialize concurrent connect attempts (e.g. _reconnect_loop racing
        # with the first _fetch_async call on startup).
        lock = self._connect_lock
        if lock is not None:
            await lock.acquire()
        try:
            # Double-check inside the lock.
            if self._ib is not None and self._ib.isConnected():
                return True

            # Always tear down and recreate the IB instance.
            # A disconnected or previously-failed IB object can be in a broken
            # state where connectAsync() keeps failing — a fresh object fixes it.
            if self._ib is not None:
                try:
                    self._ib.disconnect()
                except Exception:
                    pass
            self._ib = IB()
            self._ib.disconnectedEvent += lambda: logger.warning(
                "[IBKR] Gateway disconnected — will auto-reconnect"
            )
            # Detect Error 326 (clientId already in use) — a permanent rejection
            # until the stale TWS socket times out (~60-90s).  Set a backoff guard
            # so the reconnect loop doesn't hammer TWS pointlessly.
            _self_ref = self
            def _on_ib_error(reqId: int, errorCode: int, errorString: str, contract) -> None:  # type: ignore[type-arg]
                if errorCode == 326:
                    logger.warning(
                        f"[IBKR] clientId {_self_ref._client_id} already in use (Error 326) "
                        "— backing off 180s for stale TWS socket to release"
                    )
                    _self_ref._hard_rejected_until = time.time() + 180.0
                    # Push an in-app notification so the operator knows trading is suspended.
                    # Scheduled on the IBKR background loop (this callback is synchronous).
                    if _self_ref._loop and not _self_ref._loop.is_closed():
                        try:
                            asyncio.run_coroutine_threadsafe(
                                _dispatch_ibkr_notif(
                                    "IBKR Trading Suspended — clientId Conflict",
                                    f"Error 326: clientId {_self_ref._client_id} is already in use by "
                                    "another TWS/Gateway session. IBKR trading is suspended for ~180s "
                                    "while the stale socket times out.",
                                ),
                                _self_ref._loop,
                            )
                        except RuntimeError:
                            pass  # loop closed between the is_closed() check and use — notification lost
            self._ib.errorEvent += _on_ib_error

            # Retry loop: Gateway may still be initialising when Docker starts.
            for attempt in range(self._CONNECT_RETRIES):
                try:
                    await self._ib.connectAsync(
                        host=settings.ibkr_host,
                        port=settings.ibkr_port,
                        clientId=self._client_id,
                        timeout=self._CONNECT_TIMEOUT,
                    )
                    # Explicitly subscribe to account updates so accountValues() is
                    # populated.  Paper accounts are slow to push NetLiquidation.
                    try:
                        self._ib.reqAccountUpdates(True)  # type: ignore[arg-type]
                    except Exception:
                        pass
                    # Wait for Gateway to push the initial account snapshot.
                    await asyncio.sleep(self._SETTLE_SECS)
                    logger.info(
                        f"[IBKR] Connected (clientId={self._client_id}, "
                        f"port={settings.ibkr_port})"
                    )
                    return True
                except Exception as exc:
                    remaining = self._CONNECT_RETRIES - attempt - 1
                    if remaining > 0:
                        logger.warning(
                            f"[IBKR] Connect attempt {attempt + 1}/{self._CONNECT_RETRIES} "
                            f"failed: {exc} — retrying in {self._CONNECT_RETRY_DELAY:.0f}s "
                            f"({remaining} left)"
                        )
                        await asyncio.sleep(self._CONNECT_RETRY_DELAY)
                    else:
                        logger.warning(
                            f"[IBKR] All {self._CONNECT_RETRIES} connect attempts failed: {exc}"
                        )
            return False
        finally:
            if lock is not None:
                lock.release()

    # ── balance fetch ─────────────────────────────────────────────────────────

    async def _fetch_async(self) -> Balance:
        if not await self._ensure_connected():
            raise ConnectionError("IBKR Gateway is not reachable")

        assert self._ib is not None
        # Poll until account data is populated or timeout expires.
        # Paper accounts push NetLiquidation/AvailableFunds asynchronously;
        # accountValues() returns an empty list until the subscription fires.
        _deadline = asyncio.get_running_loop().time() + self._ACCT_TIMEOUT  # F-099: get_running_loop() not deprecated
        total = available = 0.0
        while asyncio.get_running_loop().time() < _deadline:
            total = available = 0.0
            for v in self._ib.accountValues():
                if v.tag == "NetLiquidation" and v.currency in ("USD", "BASE"):
                    try:
                        total = float(v.value)
                    except (ValueError, TypeError):
                        pass
                if v.tag == "AvailableFunds" and v.currency in ("USD", "BASE"):
                    try:
                        available = float(v.value)
                    except (ValueError, TypeError):
                        pass
            if total > 0:
                break  # got real data
            await asyncio.sleep(self._ACCT_POLL_SECS)

        if total == 0:
            logger.warning("[IBKR] accountValues() still empty after timeout — returning $0")

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

    async def _place_bracket_async(
        self,
        contract,
        action: str,
        quantity: float,
        take_profit_price: Optional[float],
        stop_loss_price: Optional[float],
    ) -> "IBTrade":
        """Place a market entry bracketed by TP limit and/or SL stop orders."""
        if not await self._ensure_connected():
            raise ConnectionError("IBKR Gateway is not reachable")
        assert self._ib is not None
        ib = self._ib
        reverse = "SELL" if action == "BUY" else "BUY"
        has_tp = take_profit_price is not None
        has_sl = stop_loss_price is not None

        parent = MarketOrder(
            action, quantity,
            orderId=ib.client.getReqId(),
            transmit=not (has_tp or has_sl),
        )
        orders: list[Order] = [parent]

        if has_tp:
            tp = LimitOrder(
                reverse, quantity, take_profit_price,
                orderId=ib.client.getReqId(),
                parentId=parent.orderId,
                tif="GTC",
                transmit=not has_sl,
            )
            orders.append(tp)

        if has_sl:
            sl = StopOrder(
                reverse, quantity, stop_loss_price,
                orderId=ib.client.getReqId(),
                parentId=parent.orderId,
                tif="GTC",
                transmit=True,
            )
            orders.append(sl)

        parent_trade: Optional[IBTrade] = None
        for o in orders:
            t = ib.placeOrder(contract, o)
            if o.orderId == parent.orderId:
                parent_trade = t
        # F-073: 1.0 s settle ensures the Gateway assigns a server-side orderId to
        # the parent before child orders reference it via parentId — 0.5 s was
        # occasionally too short under Gateway load or paper-account processing delays.
        await asyncio.sleep(1.0)
        assert parent_trade is not None
        return parent_trade

    async def _do_subscribe_mkt_data_for_fill(self, contract) -> None:
        """Subscribe to market data on the background loop for paper fill simulation."""
        if not self._ib:
            return
        self._ib.reqMktData(contract, "", False, False)
        await asyncio.sleep(1.0)  # allow at least one tick to arrive

    def place_bracket_sync(
        self,
        contract,
        action: str,
        quantity: float,
        take_profit_price: Optional[float],
        stop_loss_price: Optional[float],
    ) -> "IBTrade":
        """Synchronous wrapper for _place_bracket_async for use with run_in_executor."""
        return self._submit(
            self._place_bracket_async(contract, action, quantity, take_profit_price, stop_loss_price)
        )

    def ensure_connected_sync(self) -> bool:
        """
        Synchronously ensure the singleton IB connection is live.
        Runs _ensure_connected() on the background loop (where ib_insync lives).
        Returns True if connected, False if Gateway is unreachable.
        Unlike get_balance(), exceptions are NOT swallowed here.
        Timeout is 90 s — long enough to survive all 4 connect retries (4 × 15 s)
        plus delays, so trading callers (ForwardEngine) are never cut short.
        """
        self._start()
        return self._submit(self._ensure_connected(), timeout=90.0)

    def get_ib(self) -> "IB":
        """
        Return the live, connected IB instance.
        Attempts a reconnect if disconnected (e.g. Gateway was restarted).
        Raises ConnectionError if Gateway is still unreachable.
        """
        self._start()
        if not self.is_connected():
            try:
                self._submit(self._ensure_connected())
            except Exception:
                pass
        if not self.is_connected():
            raise ConnectionError(
                "IBKRClient is not connected. Call connect() first or ensure IB Gateway is running."
            )
        assert self._ib is not None
        return self._ib

    # ── OHLCV / price helpers (run on the background loop) ───────────────────

    async def _do_ohlcv(
        self, symbol: str, bar_size: str, duration: str
    ) -> list:
        """Fetch historical bars on the manager's dedicated event loop."""
        if not await self._ensure_connected():
            raise ConnectionError("IBKR Gateway is not reachable")
        assert self._ib is not None
        contract = _ibkr_contract(symbol)
        await self._ib.qualifyContractsAsync(contract)
        # Forex trades 24/5 — RTH filter must be off; stocks use RTH only
        use_rth = contract.secType != "CASH"
        # MIDPOINT is only valid for Forex (CASH secType).
        # Stocks, ETFs, and options require "TRADES".
        what_to_show = "MIDPOINT" if contract.secType == "CASH" else "TRADES"
        bars = await self._ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=use_rth,
        )
        return bars

    def fetch_ohlcv(self, symbol: str, bar_size: str, duration: str) -> list:
        """Thread-safe OHLCV fetch via the singleton IB connection.
        Uses a 90-second timeout to accommodate IBKR pacing delays on large watchlists.
        """
        self._start()
        return self._submit(self._do_ohlcv(symbol, bar_size, duration), timeout=90.0)

    async def _do_price(self, symbol: str) -> float:
        """Fetch current price on the manager's dedicated event loop."""
        if not await self._ensure_connected():
            raise ConnectionError("IBKR Gateway is not reachable")
        assert self._ib is not None
        contract = _ibkr_contract(symbol)
        await self._ib.qualifyContractsAsync(contract)
        ticker = self._ib.reqMktData(contract)
        # Poll until we get a valid price or hit the timeout (5 s).
        # ib_insync initialises all ticker fields to math.nan, NOT None/0.
        _loop = asyncio.get_running_loop()
        _deadline = _loop.time() + 5.0
        while _loop.time() < _deadline:
            price = 0.0
            for _attr in ("last", "bid", "close"):
                _v = getattr(ticker, _attr, None)
                if _v is not None:
                    try:
                        _fv = float(_v)
                        if not math.isnan(_fv) and _fv > 0:
                            price = _fv
                            break
                    except (TypeError, ValueError):
                        pass
            if price > 0:
                break
            await asyncio.sleep(0.25)
        self._ib.cancelMktData(contract)
        if price == 0.0:
            raise ValueError(f"[IBKR] No valid price received for '{symbol}' within 5 s")
        return price

    def fetch_price(self, symbol: str) -> float:
        """Thread-safe price fetch via the singleton IB connection."""
        self._start()
        return self._submit(self._do_price(symbol))

    async def _do_bid_ask(self, symbol: str) -> tuple[float, float]:
        """Fetch current bid/ask prices on the manager's dedicated event loop."""
        if not await self._ensure_connected():
            raise ConnectionError("IBKR Gateway is not reachable")
        assert self._ib is not None
        contract = _ibkr_contract(symbol)
        try:
            await self._ib.qualifyContractsAsync(contract)
        except Exception:
            pass
        ticker = self._ib.reqMktData(contract, "", False, False)
        _loop = asyncio.get_running_loop()
        _deadline = _loop.time() + 5.0
        bid = ask = 0.0
        while _loop.time() < _deadline:
            try:
                _bid = getattr(ticker, "bid", None)
                _ask = getattr(ticker, "ask", None)
                if _bid is not None and _ask is not None:
                    b, a = float(_bid), float(_ask)
                    if not math.isnan(b) and not math.isnan(a) and b > 0 and a > 0:
                        bid, ask = b, a
                        break
            except (TypeError, ValueError):
                pass
            await asyncio.sleep(0.25)
        self._ib.cancelMktData(contract)
        if bid <= 0 or ask <= 0:
            # Fallback: use mid-price from last/bid for both sides
            price = await self._do_price(symbol)
            return price, price
        return bid, ask

    def fetch_bid_ask(self, symbol: str) -> tuple[float, float]:
        """Thread-safe bid/ask fetch via the singleton IB connection."""
        self._start()
        return self._submit(self._do_bid_ask(symbol))

    async def _do_positions(self) -> list:
        """
        Force a fresh position request via reqPositionsAsync() and return the result.
        Uses the async API to avoid calling loop.run_until_complete() inside a
        running event loop (which the blocking reqPositions() does internally).
        Serialized by _positions_lock (F-102) because TWS sends positionEnd only
        once — a second concurrent caller would never receive it and hang until timeout.
        """
        lock = self._positions_lock
        if lock is None:
            # Fallback safety net (should always be set by _run_loop before first call)
            lock = asyncio.Lock()
            self._positions_lock = lock
        async with lock:
            if not await self._ensure_connected():
                raise ConnectionError("IBKR Gateway is not reachable")
            assert self._ib is not None
            # reqPositionsAsync() triggers a fresh REQ_POSITIONS from TWS.
            # However, when reqAccountUpdates(True) was already called on connect,
            # TWS delivers position data via the portfolio update pathway instead,
            # meaning reqPositionsAsync() may return [] even though positions exist.
            # Fix: if reqPositionsAsync() returns empty, fall back to portfolio()
            # which is populated by the account-update subscription.
            positions = await self._ib.reqPositionsAsync()
            if not positions:
                # portfolio() returns PortfolioItem objects; convert to Position namedtuples
                # so callers get a consistent type. PortfolioItem has the same fields we need.
                portfolio_items = self._ib.portfolio()
                if portfolio_items:
                    from ib_insync import Position as IbPosition
                    positions = [
                        IbPosition(
                            account=item.account,
                            contract=item.contract,
                            position=item.position,
                            avgCost=item.averageCost,
                        )
                        for item in portfolio_items
                        if item.position != 0
                    ]
            return positions

    def fetch_positions(self) -> list:
        """Thread-safe fresh position fetch via the singleton IB connection."""
        self._start()
        return self._submit(self._do_positions(), timeout=15.0)

    async def _do_orderbook(self, symbol: str) -> dict:
        """Fetch market depth on the background loop."""
        if not await self._ensure_connected():
            raise ConnectionError("IBKR Gateway is not reachable")
        assert self._ib is not None
        contract = _ibkr_contract(symbol)
        await self._ib.qualifyContractsAsync(contract)
        ticker = self._ib.reqMktDepth(contract)
        await asyncio.sleep(1)
        bids = [[b.price, b.size] for b in ticker.domBids[:10]]
        asks = [[a.price, a.size] for a in ticker.domAsks[:10]]
        self._ib.cancelMktDepth(contract)
        return {"bids": bids, "asks": asks}

    def fetch_orderbook(self, symbol: str) -> dict:
        """Thread-safe orderbook fetch via the singleton IB connection."""
        self._start()
        return self._submit(self._do_orderbook(symbol))

    async def _do_options_chain(self, symbol: str) -> dict:
        """Fetch options chain parameters on the background loop."""
        if not await self._ensure_connected():
            raise ConnectionError("IBKR Gateway is not reachable")
        assert self._ib is not None
        contract = _ibkr_contract(symbol)
        await self._ib.qualifyContractsAsync(contract)
        chains = await self._ib.reqSecDefOptParamsAsync(
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

    def fetch_options_chain(self, symbol: str) -> dict:
        """Thread-safe options chain fetch via the singleton IB connection."""
        self._start()
        return self._submit(self._do_options_chain(symbol))

    async def _do_qualify(self, contract) -> None:
        """Qualify a contract on the background loop (fills in conId etc.)."""
        if not await self._ensure_connected():
            raise ConnectionError("IBKR Gateway is not reachable")
        assert self._ib is not None
        await self._ib.qualifyContractsAsync(contract)

    def qualify_contract_sync(self, contract) -> None:
        """Thread-safe contract qualification via the singleton IB connection."""
        self._start()
        self._submit(self._do_qualify(contract))

    # ── Live market data subscription (used by /ws/kline endpoint) ───────────

    async def _do_subscribe_mkt_data(self, contract):
        """Subscribe to live price ticks on the manager's background loop."""
        if not await self._ensure_connected():
            raise ConnectionError("IBKR Gateway is not reachable")
        assert self._ib is not None
        try:
            await self._ib.qualifyContractsAsync(contract)
        except Exception:
            pass
        ticker = self._ib.reqMktData(contract, "", False, False)
        await asyncio.sleep(1.5)  # let Gateway push the initial snapshot
        return ticker

    def subscribe_mkt_data(self, contract):
        """Thread-safe: open a live price tick subscription. Returns the Ticker object."""
        self._start()
        return self._submit(self._do_subscribe_mkt_data(contract), timeout=15.0)

    def subscribe_mkt_data_for_fill(self, contract) -> None:
        """Fire-and-forget: schedule reqMktData on the background loop so IBKR paper
        account has a live price reference to simulate fills.  Does not block.
        Cancel via unsubscribe_mkt_data() once the fill is confirmed.
        """
        if self._loop and self._ib:
            try:
                self._loop.call_soon_threadsafe(
                    self._ib.reqMktData, contract, "", False, False
                )
            except Exception:
                pass

    def unsubscribe_mkt_data(self, contract) -> None:
        """Cancel a live market data subscription (fire-and-forget)."""
        if self._loop and self._ib:
            try:
                self._loop.call_soon_threadsafe(self._ib.cancelMktData, contract)
            except Exception:
                pass

    def cancel_bracket_subscription(self, symbol: str) -> None:
        """F-082: Cancel the persistent market data subscription kept alive for an open
        bracket order.  Called from ForwardEngine.close_position() after a trade closes."""
        contract = self._bracket_subscriptions.pop(symbol, None)
        if contract:
            self.unsubscribe_mkt_data(contract)
            logger.debug(f"[IBKR] Cancelled bracket mkt-data subscription for {symbol}")

    async def _do_modify_stop_order(
        self, contract_symbol: str, exit_action: str, new_price: float
    ) -> bool:
        """
        Background-loop coroutine: find the open STP child order for *contract_symbol*
        whose action matches *exit_action* (SELL for longs, BUY for shorts) and modify
        its auxPrice (stop trigger) in-place.  Runs on the _IBKRManager event loop via
        _submit() so ib_insync handles the thread correctly.
        """
        if not self._ib:
            return False
        open_trades = self._ib.openTrades()
        for t in open_trades:
            if (t.order.orderType in ("STP", "STOP")
                    and t.order.action == exit_action
                    and hasattr(t, "contract")
                    and t.contract.symbol == contract_symbol):
                t.order.auxPrice = new_price
                self._ib.placeOrder(t.contract, t.order)
                await asyncio.sleep(0.1)   # let the Gateway process the amendment
                return True
        return False

    async def _fetch_open_brackets(self) -> dict:
        """
        Query TWS for all open bracket child orders and return a map:
            { contract_symbol (str) → {
                "sl": float | None,
                "tp": float | None,
                "full_symbol": str,   # e.g. "GBP/USD" for forex, "AAPL" for stock
                "side": str,          # "long" | "short" (from parent entry action)
                "quantity": float,
              }
            }

        Called by get_open_brackets() on IBKRClient (run via run_in_executor).
        Uses ib.reqOpenOrdersAsync() to force TWS to push the current open-order
        list before reading ib.trades(), so stale in-memory state is never returned.
        """
        if not self._ib:
            return {}
        if not await self._ensure_connected():
            return {}
        ib = self._ib
        # Force TWS to push the full open-order list for our clientId.
        await ib.reqOpenOrdersAsync()

        # Build parent orderId → (contract_symbol, full_symbol, entry_action)
        # from ALL trades in the current session (includes filled parent orders).
        parent_map: dict[int, tuple[str, str, str]] = {}
        for t in ib.trades():
            if t.order.parentId != 0:
                continue  # skip child orders in this pass
            contract = getattr(t, "contract", None)
            if contract is None:
                continue
            sym = contract.symbol
            sec_type = getattr(contract, "secType", "")
            currency = getattr(contract, "currency", "USD")
            full_sym = f"{sym}/{currency}" if sec_type == "CASH" else sym
            parent_map[t.order.orderId] = (sym, full_sym, t.order.action)

        result: dict[str, dict] = {}
        for t in ib.openTrades():
            order = t.order
            if order.parentId == 0:
                continue  # only interested in bracket children
            info = parent_map.get(order.parentId)
            if info is None:
                continue
            sym, full_sym, parent_action = info
            if sym not in result:
                result[sym] = {
                    "sl": None,
                    "tp": None,
                    "full_symbol": full_sym,
                    # parent_action is BUY → long, SELL → short
                    "side": "long" if parent_action == "BUY" else "short",
                    "quantity": float(order.totalQuantity or 0),
                }
            ot = order.orderType.upper()
            if ot in ("STP", "STOP"):
                result[sym]["sl"] = float(order.auxPrice) if order.auxPrice else None
            elif ot in ("LMT", "LIMIT"):
                result[sym]["tp"] = float(order.lmtPrice) if order.lmtPrice else None

        return result

    def fetch_open_brackets(self) -> dict:
        """Thread-safe sync wrapper for _fetch_open_brackets."""
        self._start()
        return self._submit(self._fetch_open_brackets(), timeout=15.0)

    async def _do_convert_fx_to_usd(self) -> list[dict]:
        """
        Convert all non-USD cash balances back to USD immediately via spot FX orders.

        After a forex trade closes, IBKR holds the foreign currency until T+2.
        This method finds every non-USD cash balance in the account and places a
        spot SELL market order (e.g. SELL AUD → buy USD on AUD.USD CASH contract)
        to collapse the FX exposure instantly.

        Returns a list of dicts describing each conversion placed, e.g.
            [{"currency": "AUD", "amount": 150000.0, "order_id": "..."}]
        """
        if not await self._ensure_connected():
            return []
        assert self._ib is not None
        ib = self._ib

        # Pull latest account values — filter for CashBalance per currency
        # (IBKR reports one entry per currency with tag="CashBalance")
        fx_balances: dict[str, float] = {}
        for v in ib.accountValues():
            if v.tag == "CashBalance" and v.currency not in ("USD", "BASE", "", "EUR"):
                # Only convert if balance is meaningful (> $1 equivalent)
                try:
                    amount = float(v.value)
                except (ValueError, TypeError):
                    continue
                if abs(amount) > 1.0:
                    fx_balances[v.currency] = amount

        conversions = []
        for currency, amount in fx_balances.items():
            try:
                from ib_insync import Forex as _IbForex, MarketOrder as _MktOrder
                # Construct the CASH contract: e.g. AUD/USD → Forex("AUDUSD")
                pair = f"{currency}USD"
                contract = _IbForex(pair)
                await ib.qualifyContractsAsync(contract)
                # Positive balance = we're LONG the foreign currency → SELL to get USD
                # Negative balance = we're SHORT the foreign currency → BUY to cover
                action = "SELL" if amount > 0 else "BUY"
                qty = abs(int(amount))  # IBKR CASH contracts use integer lots
                if qty < 1:
                    continue
                order = _MktOrder(action, qty)
                trade = ib.placeOrder(contract, order)
                await asyncio.sleep(0.2)  # let TWS acknowledge
                order_id = str(trade.order.orderId)
                logger.info(
                    f"[IBKR] FX conversion: {action} {qty:,} {currency}/USD "
                    f"(order {order_id}) — collapsing T+2 exposure"
                )
                conversions.append({"currency": currency, "amount": amount, "order_id": order_id})
            except Exception as _conv_err:
                logger.warning(f"[IBKR] FX conversion failed for {currency}: {_conv_err}")

        return conversions

    def convert_fx_to_usd(self) -> list[dict]:
        """Thread-safe sync wrapper for _do_convert_fx_to_usd."""
        self._start()
        return self._submit(self._do_convert_fx_to_usd(), timeout=20.0)


_manager = _IBKRManager(client_id=settings.ibkr_client_id)
# Celery workers derive a PID-based clientId (range 10-97) so each worker
# process uses a unique ID and avoids Error 326 (clientId already in use).
# PID is stable for the lifetime of the process, so the singleton is consistent.
_celery_client_id: int = 10 + (os.getpid() % 88)
_celery_manager = _IBKRManager(client_id=_celery_client_id)


def _get_manager() -> "_IBKRManager":
    """Return the correct IBKR singleton — PID-based clientId in Celery workers, clientId 1 in FastAPI."""
    if os.environ.get("CELERY_WORKER_PROCESS") == "1":
        return _celery_manager
    return _manager


def ibkr_balance_sync() -> Balance:
    """
    Thread-safe entry-point used by portfolio route.
    Returns a cached or freshly fetched IBKR balance WITHOUT spawning a new
    IB connection on every call.
    """
    return _get_manager().get_balance()


def get_ibkr_manager() -> "_IBKRManager":
    """Return the singleton IBKR manager (used by /ws/kline for live tick streaming)."""
    return _get_manager()


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
        mode = "PAPER" if self._paper else "LIVE"
        logger.info(f"IBKRClient initialized in {mode} mode (delegating to singleton manager).")

    @property
    def ib(self) -> "IB":
        """
        Return the singleton IB instance shared by the persistent manager.
        Raises ConnectionError if the Gateway is not reachable.
        """
        return _get_manager().get_ib()

    async def connect(self) -> None:
        """
        Ensure the singleton IBKR manager is connected (idempotent).
        Submits _ensure_connected directly to the background loop so the real
        socket state is checked — no exception swallowing.
        """
        _get_manager()._start()
        loop = asyncio.get_running_loop()
        try:
            connected = await loop.run_in_executor(None, _get_manager().ensure_connected_sync)
        except Exception as exc:
            raise ConnectionError(
                f"IBKRClient: could not connect to IB Gateway: {exc}"
            ) from exc
        if not connected:
            raise ConnectionError(
                "IBKRClient: IB Gateway is not reachable. Ensure IB Gateway is running."
            )
        mode = "PAPER" if self._paper else "LIVE"
        logger.info(f"[IBKR] IBKRClient ready — singleton connected ({mode}, port={settings.ibkr_port})")

    async def disconnect(self) -> None:
        """No-op: the singleton manager owns the connection lifecycle."""
        logger.debug("[IBKR] IBKRClient.disconnect() called — singleton connection preserved.")

    def cancel_bracket_subscription(self, symbol: str) -> None:
        """F-082: Cancel the persistent mkt data feed kept for an open bracket order.
        Called by ForwardEngine.close_position() after a trade is closed."""
        _get_manager().cancel_bracket_subscription(symbol)

    def _ensure_connected(self) -> None:
        """Raise if the singleton IB manager is not currently connected."""
        if not _get_manager().is_connected():
            raise ConnectionError(
                "IBKRClient is not connected. Call connect() first or ensure IB Gateway is running."
            )

    # ── Market Data ──────────────────────────────────────

    async def get_price(self, symbol: str) -> float:
        """
        Fetch current market price via the singleton IB connection.
        Runs the ib_insync async call on the manager's dedicated event loop.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _get_manager().fetch_price, symbol)

    async def get_bid_ask(self, symbol: str) -> tuple[float, float]:
        """
        Fetch current bid and ask prices via the singleton IB connection.
        Returns (bid, ask). Forex pairs always have bid/ask; stocks during
        market hours also have both. Falls back to (price, price) on failure.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _get_manager().fetch_bid_ask, symbol)

    async def get_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1 hour",
        limit: int = 500,
        since: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV bars via the singleton IB connection.
        reqHistoricalDataAsync must run on the manager's background loop;
        we dispatch it there via run_in_executor so FastAPI's loop stays free.
        The manager's _do_ohlcv() handles reconnection internally.
        """

        bar_size_map = {
            "1m": "1 min",
            "5m": "5 mins",
            "15m": "15 mins",
            "30m": "30 mins",
            "1h": "1 hour",
            "1 hour": "1 hour",
            "4h": "4 hours",
            "1d": "1 day",
            "3d": "3 days",
            "1w": "1 week",
        }
        bar_size = bar_size_map.get(timeframe, "1 hour")
        # Convert bar_size to minutes per bar so we can compute the correct duration.
        # The old formula was broken:
        #   'hour' branch: max(1, 200//24)= 8 D → only ~13 4h bars, ~52 1h bars
        #   else branch:   max(1, 200//390)= 1 D → only 1 daily bar or ~78 5m bars
        # Fix: compute calendar days from trading minutes needed (390 min/trading day).
        _bar_minutes = {
            "1 min": 1, "5 mins": 5, "15 mins": 15, "30 mins": 30,
            "1 hour": 60, "4 hours": 240, "1 day": 390,
            "3 days": 1170, "1 week": 1950,
        }
        mins_per_bar = _bar_minutes.get(bar_size, 60)
        trading_days_needed = (limit * mins_per_bar + 390) / 390
        calendar_days = int(trading_days_needed * 7 / 5) + 5  # +5 day buffer
        # Cap to IBKR's hard historical-data limits per bar size.
        # Requesting more triggers pacing delays (up to 10 min wait) or flat rejection.
        # Source: TWS API documentation — reqHistoricalData max durations.
        _max_calendar_days = {
            "1 min":   7,    # 1 W max
            "5 mins":  30,   # 1 M max
            "15 mins": 60,   # 2 M max
            "30 mins": 180,  # 6 M max
            "1 hour":  365,  # 1 Y max
            "4 hours": 365,  # 1 Y max
            "1 day":   3650, # 10 Y — effectively unlimited
            "3 days":  3650,
            "1 week":  3650,
        }
        calendar_days = min(calendar_days, _max_calendar_days.get(bar_size, 365))
        duration = f"{max(1, calendar_days)} D"

        loop = asyncio.get_running_loop()
        bars = await loop.run_in_executor(
            None, _get_manager().fetch_ohlcv, symbol, bar_size, duration
        )
        _EMPTY_OHLCV = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        if not bars:
            logger.warning(
                f"[IBKR] No bars returned for {symbol} ({bar_size}, {duration}). "
                f"Check market data subscriptions for this exchange."
            )
            return _EMPTY_OHLCV
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
        if df.empty:
            return _EMPTY_OHLCV
        df.set_index("timestamp", inplace=True)
        # Drop the last (still-forming) candle so strategies only see confirmed closes.
        # IBKR includes the current incomplete bar as the final row; signals based on
        # a partial candle can reverse before the bar closes, causing false entries.
        if len(df) > 1:
            df = df.iloc[:-1]
        return df

    async def get_orderbook(self, symbol: str) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _get_manager().fetch_orderbook, symbol)

    # ── Options Chain ────────────────────────────────────

    async def get_options_chain(self, symbol: str) -> dict:
        """
        Fetch the full options chain for an underlying symbol.
        Returns strikes, expirations, IVs, and Greeks via IBKR.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _get_manager().fetch_options_chain, symbol)

    # ── Account ──────────────────────────────────────────

    async def get_balance(self) -> Balance:
        # Delegate to the singleton manager which uses a polled/cached balance.
        # Reading self.ib.accountValues() directly returns stale or empty data
        # immediately after a connect — the manager's _fetch_async waits up to
        # 20 s for the Gateway to push account values, preventing balance=0
        # from propagating through the risk manager and zeroing position sizes.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _get_manager().get_balance)

    async def get_positions(self) -> List[Position]:
        loop = asyncio.get_running_loop()
        # Use fetch_positions() which forces reqPositions() before reading the cache.
        # This ensures positions closed by broker brackets or external clients are
        # reflected immediately rather than showing stale in-memory data.
        raw = await loop.run_in_executor(None, _get_manager().fetch_positions)
        positions = []
        for p in raw:
            contract = p.contract
            sec_type = getattr(contract, "secType", "")
            currency = getattr(contract, "currency", "USD")
            sym = contract.symbol
            # For CASH (forex) contracts IBKR only stores the base currency as symbol
            # (e.g. "GBP" for GBP/USD).  Reconstruct the full pair so DB records and
            # signal lookups use the correct symbol form.
            full_sym = f"{sym}/{currency}" if sec_type == "CASH" else sym
            asset_class = "stock" if sec_type == "STK" else ("forex" if sec_type == "CASH" else "option")
            qty = abs(p.position)
            if qty == 0:
                continue  # IBKR emits zero-qty entries for recently closed positions; skip them
            entry_price = float(p.avgCost)
            # Derive current price from market value provided by IB Gateway
            current_price = (
                float(p.marketValue) / qty if qty > 0 and hasattr(p, "marketValue") and p.marketValue
                else 0.0
            )
            unrealized_pnl = float(p.unrealizedPNL) if hasattr(p, "unrealizedPNL") and p.unrealizedPNL is not None else 0.0
            positions.append(Position(
                symbol=sym,          # short symbol for broker_symbols matching (e.g. "GBP")
                full_symbol=full_sym, # full symbol for DB records (e.g. "GBP/USD")
                side="long" if p.position > 0 else "short",
                quantity=qty,
                entry_price=entry_price,
                current_price=current_price,
                unrealized_pnl=unrealized_pnl,
                asset_class=asset_class,
            ))
        return positions

    async def get_open_brackets(self) -> dict:
        """
        Return a map of open IBKR bracket SL/TP orders keyed by contract symbol.
        See _IBKRManager._fetch_open_brackets() for the full dict schema.
        Used by reconcile_positions to recover SL/TP for orphan broker positions.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _get_manager().fetch_open_brackets)

    async def auto_convert_fx(self) -> list[dict]:
        """
        Convert all non-USD cash balances to USD immediately via spot CASH orders.
        Called automatically after IBKR forex trades close to prevent T+2 FX exposure.
        Returns list of conversions placed (empty if already all USD).
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _get_manager().convert_fx_to_usd)

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
        # IBKR (IDEALPRO for forex, SMART for stocks) rejects fractional quantities with
        # error 10318.  Floor to a whole number here — the risk manager works in float
        # but the exchange requires integer lot sizes.
        quantity = math.floor(quantity)
        if quantity < 1:
            raise ValueError(f"[IBKR] Computed quantity < 1 after flooring — insufficient balance or position sizing error.")
        logger.info(f"[IBKR] Placing {order_type.upper()} {side.upper()} {quantity} {symbol}")

        # Build and qualify contract on the background loop (avoids 'event loop already running')
        # F-082: track whether this is a bracket order so we know whether to keep
        # the market data subscription alive after fill (bracket) or cancel it (plain market).
        _is_bracket = bool(stop_price or take_profit_price)

        if option_expiry and option_strike and option_right:
            contract = Option(symbol, option_expiry, option_strike, option_right, "SMART")
        else:
            contract = _ibkr_contract(symbol)
        _place_loop = asyncio.get_running_loop()
        await _place_loop.run_in_executor(None, _get_manager().qualify_contract_sync, contract)

        # Build order
        action = "BUY" if side.lower() == "buy" else "SELL"
        if order_type == "limit" and price:
            order = LimitOrder(action, quantity, price)
            trade: IBTrade = self.ib.placeOrder(contract, order)
        elif order_type in ("stop_limit", "stop") and stop_price and price:
            order = StopLimitOrder(action, quantity, price, stop_price)
            trade = self.ib.placeOrder(contract, order)
        elif (stop_price or take_profit_price):
            # Market order with SL/TP — send as a bracket order so IBKR
            # actually enforces the stop and take-profit on their side.
            # F-077: subscribe to live ticks FIRST so IBKR paper account has a
            # price reference to simulate fills (reqMktData is called inside
            # _place_bracket_async which runs on the background loop).
            await _place_loop.run_in_executor(
                None,
                lambda: _get_manager()._submit(_get_manager()._do_subscribe_mkt_data_for_fill(contract)),
            )
            trade = await _place_loop.run_in_executor(
                None,
                _get_manager().place_bracket_sync,
                contract, action, quantity, take_profit_price, stop_price,
            )
        else:
            order = MarketOrder(action, quantity)
            # F-077: subscribe before placing so IBKR paper can simulate the fill
            _get_manager().subscribe_mkt_data_for_fill(contract)
            await asyncio.sleep(0.5)  # let at least one tick arrive
            trade = self.ib.placeOrder(contract, order)
        await asyncio.sleep(0.5)

        # ── Poll for fill confirmation ─────────────────────────────────────────
        # F-077: timeout raised 15 → 30 s; reqMktData is now active so paper
        # fills arrive reliably.  try/finally ensures the subscription is
        # always cancelled regardless of fill, timeout, or rejection.
        order_id = str(trade.order.orderId)
        fill_price: Optional[float] = None
        _FILL_TIMEOUT = 30.0
        _POLL_INTERVAL = 0.5
        _elapsed = 0.0
        try:
            while _elapsed < _FILL_TIMEOUT:
                await asyncio.sleep(_POLL_INTERVAL)
                _elapsed += _POLL_INTERVAL
                try:
                    _st = trade.orderStatus.status
                    _filled_price = trade.orderStatus.avgFillPrice
                    if _st in ("Filled", "PreSubmitted") and _filled_price:
                        fill_price = float(_filled_price)
                        logger.info(f"[IBKR] Order {order_id} filled @ {fill_price}")
                        break
                    elif _st == "Filled" and not _filled_price:
                        # Filled but price not yet pushed — use last trade price
                        fill_price = float(trade.orderStatus.lastFillPrice or 0) or None
                        logger.info(f"[IBKR] Order {order_id} filled (lastFillPrice={fill_price})")
                        break
                    elif _st == "Cancelled":
                        raise RuntimeError(f"IBKR order {order_id} cancelled by broker")
                    elif _st == "Inactive":
                        # F-104: IBKR paper sets Inactive while waiting for a price feed
                        # before submitting to the exchange.  The order is live and will
                        # fill once data arrives — do NOT raise; continue polling.
                        logger.debug(f"[IBKR] Order {order_id} Inactive — awaiting price feed, continuing poll")
                except RuntimeError:
                    raise
                except Exception as _pe:
                    logger.debug(f"[IBKR] Poll {order_id}: {_pe}")
            else:
                logger.warning(f"[IBKR] Order {order_id} not confirmed filled within {_FILL_TIMEOUT}s — treating as pending")
        finally:
            if _is_bracket:
                # F-082: keep max data subscription alive — bracket child orders (SL stop,
                # TP limit) need a live price feed to trigger in IBKR paper simulation.
                # The subscription is cancelled later by cancel_bracket_subscription()
                # which ForwardEngine.close_position() calls after the trade closes.
                _get_manager()._bracket_subscriptions[symbol] = contract
                logger.debug(f"[IBKR] Keeping mkt-data subscription alive for bracket: {symbol}")
            else:
                # Plain market order — subscription only needed for fill simulation; cancel now.
                _get_manager().unsubscribe_mkt_data(contract)

        return OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=float(fill_price or 0),
            status="filled" if fill_price is not None else trade.orderStatus.status,
            raw={"order_id": trade.order.orderId, "status": trade.orderStatus.status},
            fill_price=fill_price,
            effective_stop_price=stop_price,
            effective_take_profit=take_profit_price,
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

    async def update_stop_loss(
        self,
        symbol: str,
        side: str,
        quantity: float,
        new_sl_price: float,
    ) -> bool:
        """
        Modify the IBKR STP bracket child order in-place by sending the same
        orderId with an updated auxPrice.  IBKR treats re-submitting an existing
        orderId as a modify request, not a new order.
        Uses the _IBKRManager background loop so the ib_insync event loop is
        not violated from the asyncio task running in Celery/FastAPI.
        """
        try:
            self._ensure_connected()
            contract = _ibkr_contract(symbol)
            exit_action = "SELL" if side.lower() == "buy" else "BUY"
            _place_loop = asyncio.get_running_loop()
            found: bool = await _place_loop.run_in_executor(
                None,
                lambda: _get_manager()._submit(
                    _get_manager()._do_modify_stop_order(
                        contract.symbol, exit_action, new_sl_price
                    )
                ),
            )
            if found:
                logger.info(f"[IBKR] \u26a1 Trailing stop synced: {symbol} SL \u2192 {new_sl_price}")
            else:
                logger.debug(f"[IBKR] update_stop_loss: no open STP order found for {symbol}")
            return found
        except Exception as exc:
            logger.warning(f"[IBKR] update_stop_loss failed for {symbol}: {exc}")
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
        _stream_loop = asyncio.get_running_loop()
        for _c in contracts:
            await _stream_loop.run_in_executor(None, _get_manager().qualify_contract_sync, _c)

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
