"""
Real-time scalping stream engine.

Subscribes to Binance kline WebSocket streams (one per active symbol/timeframe
combination) and triggers scalping signal generation on every candle close.
This delivers sub-10 s latency from candle close to order entry — much faster
than the 60 s Celery poll in scalping_runner.py.

Architecture
------------
* One asyncio Task per unique (symbol, timeframe) — shared across all strategies
  on that pair so we only hold one WS connection per pair.
* On candle close (``k.x == True``):
    1. Fetch OHLCV via broker REST (limit = max over matching strategies).
    2. Optionally fetch live bid/ask for spread gate.
    3. Run ``ScalpingEmaVwap.generate_signal()`` per strategy.
    4. If BUY/SELL → ``ForwardEngine.process_signal()``.
    5. Broadcast ``scalp_signal`` over the global WS manager.
* ``_last_candle_ts[strat_id]`` deduplicates against the Celery runner —
  the same candle open-time is never processed twice for the same strategy id.
* Exponential backoff reconnect (2 → 4 → 8 … → 60 s cap).

Lifecycle
---------
    async with AsyncSessionLocal() as _ASL:
        scalping_stream_task = asyncio.create_task(
            scalping_stream_manager.start(AsyncSessionLocal)
        )

Fallback
--------
If the WS stream is down the 60 s Celery ``scalping_runner`` continues to
operate normally — this stream adds speed, not correctness.
"""

import asyncio
import json
from typing import Any

from loguru import logger


class ScalpingStreamManager:
    """Manages one Binance kline WS connection per active scalp (symbol, tf)."""

    # How often to refresh the active strategy list from the DB
    RESUB_INTERVAL: int = 60       # seconds
    # Exponential back-off bounds (seconds)
    RECONNECT_DELAY_INIT: int = 2
    RECONNECT_DELAY_MAX: int = 60

    def __init__(self) -> None:
        # key "symbol:tf" → running asyncio.Task
        self._tasks: dict[str, asyncio.Task] = {}
        # strat_id → candle open-time (ms) of last processed candle (dedup)
        self._last_candle_ts: dict[int, int] = {}

    # ──────────────────────────────────────────────────────────────────────────
    # Public entry-point
    # ──────────────────────────────────────────────────────────────────────────

    async def start(self, db_session_factory) -> None:
        """Long-running coroutine.  Run as an asyncio.Task in the FastAPI lifespan."""
        logger.info("[ScalpingStream] Manager started.")
        while True:
            try:
                await self._refresh(db_session_factory)
                # Sleep is inside the try so CancelledError during sleep also
                # triggers the cleanup block — prevents orphaned child tasks.
                await asyncio.sleep(self.RESUB_INTERVAL)
            except asyncio.CancelledError:
                logger.info("[ScalpingStream] Manager cancelled — stopping all streams.")
                for task in self._tasks.values():
                    task.cancel()
                await asyncio.gather(*self._tasks.values(), return_exceptions=True)
                return
            except Exception as exc:
                logger.debug(f"[ScalpingStream] Subscription refresh error: {exc}")

    # ──────────────────────────────────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────────────────────────────────

    async def _refresh(self, db_session_factory) -> None:
        """Query active scalp strategies and reconcile per-(symbol, tf) stream tasks."""
        from db.models import Strategy as StrategyModel
        from sqlalchemy import select

        async with db_session_factory() as session:
            q = await session.execute(
                select(StrategyModel).where(StrategyModel.is_active == True)  # noqa: E712
            )
            all_strats = q.scalars().all()

        scalp_strats = [
            s for s in all_strats
            if (s.parameters or {}).get("strategy_type", "").startswith("scalp_")
            and (s.parameters or {}).get("symbol")
        ]

        # Collect unique (symbol, timeframe) stream keys needed
        needed: set[str] = set()
        for s in scalp_strats:
            params = s.parameters or {}
            sym = params.get("symbol", "")
            tf  = params.get("timeframe", "5m")
            needed.add(f"{sym}:{tf}")

        # Cancel tasks for keys that are no longer needed
        for key in list(self._tasks):
            if key not in needed:
                logger.info(f"[ScalpingStream] Stopping stream for {key}")
                self._tasks[key].cancel()
                del self._tasks[key]

        # Start new tasks for new (symbol, tf) keys
        for key in needed:
            if key not in self._tasks or self._tasks[key].done():
                sym, tf = key.split(":", 1)
                logger.info(f"[ScalpingStream] Starting stream: {sym} @ {tf}")
                self._tasks[key] = asyncio.create_task(
                    self._stream_symbol(sym, tf, db_session_factory)
                )

    # ──────────────────────────────────────────────────────────────────────────

    async def _stream_symbol(
        self,
        symbol: str,
        timeframe: str,
        db_session_factory,
    ) -> None:
        """Per-(symbol, tf) streaming loop with exponential-backoff reconnect."""
        delay = self.RECONNECT_DELAY_INIT
        while True:
            try:
                await self._connect_and_listen(symbol, timeframe, db_session_factory)
                # Clean exit (e.g. remote close) — reset back-off
                delay = self.RECONNECT_DELAY_INIT
            except asyncio.CancelledError:
                logger.info(f"[ScalpingStream] Stream {symbol}@{timeframe} cancelled.")
                return
            except Exception as exc:
                logger.warning(
                    f"[ScalpingStream] Stream {symbol}@{timeframe} error: {exc} "
                    f"— retry in {delay}s"
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.RECONNECT_DELAY_MAX)

    # ──────────────────────────────────────────────────────────────────────────

    async def _connect_and_listen(
        self,
        symbol: str,
        timeframe: str,
        db_session_factory,
    ) -> None:
        """Open one Binance WS kline connection and process candle-close events."""
        import websockets as wsl

        sym = symbol.replace("/", "").lower()
        url = f"wss://stream.binance.com/ws/{sym}@kline_{timeframe}"
        logger.info(f"[ScalpingStream] Connecting {url}")

        async with wsl.connect(url, ping_interval=20, ping_timeout=10) as bws:
            async for raw in bws:
                try:
                    msg = json.loads(raw)
                    k = msg.get("k") or (msg.get("data") or {}).get("k")
                    if not k or not k.get("x"):
                        # x == True means the candle is closed
                        continue
                    candle_open_ts = int(k["t"])  # ms, used for dedup
                    await self._on_candle_close(
                        symbol, timeframe, candle_open_ts, db_session_factory
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug(f"[ScalpingStream] Parse error for {symbol}: {exc}")

    # ──────────────────────────────────────────────────────────────────────────

    async def _on_candle_close(
        self,
        symbol: str,
        timeframe: str,
        candle_open_ts: int,
        db_session_factory,
    ) -> None:
        """
        Process a candle-close event for all strategies on (symbol, timeframe).

        Queries active strategies from DB on every call so strategy additions,
        removals, and parameter changes are reflected without a stream reconnect.

        For each matched strategy:
          1. In-memory dedup — skip same candle on WS reconnect.
          2. Advisory lock + DB Signal query — prevents race with Celery runner.
          3. Persist Signal + TradeOutcome row (ML feedback loop).
          4. Call ForwardEngine.process_signal().
          5. Broadcast scalp_signal over WS.
        """
        import hashlib
        from datetime import datetime, timezone as _tz
        from core.strategies.scalping_ema_vwap import ScalpingEmaVwap
        from core.engine.forward_engine import get_forward_engine
        from brokers import get_broker
        from api.websocket import manager as _ws
        from db.models import (
            Strategy as StrategyModel,
            Signal as SignalModel,
            TradeOutcome as TradeOutcomeModel,
            AssetClass,
            SignalType,
        )
        from sqlalchemy import select, and_, text as _text
        from utils import timeframe_to_seconds as _tf_to_secs

        # ── Query current active strategies for this (symbol, tf) pair ───────────
        # Querying fresh on every candle ensures strategy param changes and
        # additions/removals are reflected without requiring a stream reconnect.
        try:
            async with db_session_factory() as _q_session:
                _q = await _q_session.execute(
                    select(StrategyModel).where(StrategyModel.is_active == True)  # noqa: E712
                )
                _all = _q.scalars().all()
        except Exception as _qe:
            logger.warning(f"[ScalpingStream] Strategy query failed for {symbol}: {_qe}")
            return

        strats = [
            s for s in _all
            if (s.parameters or {}).get("symbol") == symbol
            and (s.parameters or {}).get("timeframe", "5m") == timeframe
            and (s.parameters or {}).get("strategy_type", "").startswith("scalp_")
        ]
        if not strats:
            return  # all strategies deactivated; task stops on next _refresh

        # ── Global enabled gate ───────────────────────────────────────────────
        # The Celery runner checks this at task start; the stream must also
        # respect it so POST /scalping/enable?enabled=false stops BOTH paths.
        import json as _json
        import pathlib as _pl
        _settings_path = (
            _pl.Path(__file__).resolve().parent.parent.parent
            / "runtime" / "scalping_settings.json"
        )
        try:
            _cfg = _json.loads(_settings_path.read_text()) if _settings_path.exists() else {}
        except Exception:
            _cfg = {}
        if not _cfg.get("enabled", True):
            logger.debug(f"[ScalpingStream] Scalping disabled via settings — skipping candle {symbol}@{timeframe}")
            return

        # ── Fetch OHLCV once (shared across all strategies on this pair) ──────
        ohlcv = None
        spread_pct = None
        try:
            broker_name = (
                strats[0].broker.value
                if hasattr(strats[0].broker, "value")
                else str(strats[0].broker)
            )
            broker = get_broker(broker_name)
            await asyncio.wait_for(broker.connect(), timeout=8.0)

            limit = max(
                int((s.parameters or {}).get("limit", 100)) for s in strats
            )
            ohlcv = await asyncio.wait_for(
                broker.get_ohlcv(symbol, timeframe, limit=limit),
                timeout=12.0,
            )

            # Optional: live spread for spread gate in generate_signal
            try:
                bid, ask = await asyncio.wait_for(broker.get_bid_ask(symbol), timeout=5.0)
                mid = (bid + ask) / 2.0
                if mid > 0:
                    spread_pct = (ask - bid) / mid * 100.0
            except Exception:
                pass  # spread gate is best-effort

        except Exception as exc:
            logger.warning(f"[ScalpingStream] OHLCV fetch error for {symbol}: {exc}")
            return

        if ohlcv is None or ohlcv.empty:
            return

        _TRACKABLE = {"BUY", "SELL", "SHORT", "COVER"}
        _SCALP_STRATEGY_REGISTRY = {
            "scalp_ema_vwap": ScalpingEmaVwap,
        }
        forward_engine = get_forward_engine()  # use singleton — same instance as heartbeat
        interval = _tf_to_secs(timeframe)

        for strat in strats:
            # ── In-memory dedup: prevents re-firing the same candle on WS reconnect ──
            # NOTE: this does NOT guard against the Celery runner in another process;
            # that race is handled below via the PostgreSQL advisory lock + DB query.
            if self._last_candle_ts.get(strat.id, 0) >= candle_open_ts:
                logger.debug(
                    f"[ScalpingStream] {strat.name} candle ts={candle_open_ts} "
                    "already fired on this stream — skipping"
                )
                continue
            self._last_candle_ts[strat.id] = candle_open_ts

            params = strat.parameters or {}
            strategy_type = params.get("strategy_type", "scalp_ema_vwap")
            strategy_cls = _SCALP_STRATEGY_REGISTRY.get(strategy_type)
            if strategy_cls is None:
                logger.warning(
                    f"[ScalpingStream] Unknown strategy type '{strategy_type}' "
                    f"for {strat.name} — skipping"
                )
                continue
            spread_kw: dict = {"spread_pct": spread_pct} if spread_pct is not None else {}

            sig = strategy_cls().generate_signal(ohlcv, symbol, timeframe=timeframe, **spread_kw)
            sig.strategy_name = strat.name  # use user-defined name, not class attribute

            if sig.signal not in _TRACKABLE:
                continue

            try:
                sig_type = SignalType(sig.signal)
            except ValueError:
                continue

            logger.info(
                f"[ScalpingStream] {sig.signal} {symbol} @ {sig.entry_price:.4f} | "
                f"conf={sig.confidence:.2f} | strat={strat.name} | tf={timeframe} [WS]"
            )

            try:
                _signal_id: int | None = None

                async with db_session_factory() as exec_session:
                    # ── Advisory lock (same namespace as scalping_runner) ─────
                    # Serialises with the Celery runner for the exact same
                    # (strategy_type, symbol, timeframe) so only one side persists.
                    _lock_str = f"scalp:{strategy_type}:{symbol}:{timeframe}"
                    _lock_int = int.from_bytes(
                        hashlib.sha256(_lock_str.encode()).digest()[:4], "big"
                    ) % (2 ** 31)
                    await exec_session.execute(
                        _text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_int}
                    )

                    # ── DB dedup — skip if Celery already persisted this candle ──
                    _candle_start = datetime.fromtimestamp(
                        int(datetime.now(_tz.utc).timestamp() // interval) * interval,
                        tz=_tz.utc,
                    ).replace(tzinfo=None)
                    dup_q = await exec_session.execute(
                        select(SignalModel.id).where(
                            and_(
                                SignalModel.strategy_name == strat.name,
                                SignalModel.symbol        == sig.symbol,
                                SignalModel.signal        == sig_type,
                                SignalModel.timeframe     == timeframe,
                                SignalModel.created_at    >= _candle_start,
                            )
                        ).limit(1)
                    )
                    if dup_q.scalar_one_or_none() is not None:
                        logger.debug(
                            f"[ScalpingStream] Duplicate {sig.signal} {symbol} "
                            f"({strat.name}) — Celery already handled this candle"
                        )
                        continue

                    # ── Persist Signal ────────────────────────────────────────
                    try:
                        asset_cls_enum = AssetClass(sig.asset_class)
                    except ValueError:
                        asset_cls_enum = strat.asset_class

                    db_signal = SignalModel(
                        symbol         = sig.symbol,
                        signal         = sig_type,
                        entry_price    = sig.entry_price,
                        stop_loss      = sig.stop_loss,
                        take_profit    = sig.take_profit,
                        confidence     = sig.confidence,
                        timeframe      = sig.timeframe,
                        strategy_name  = sig.strategy_name,
                        regime         = sig.regime,
                        asset_class    = asset_cls_enum,
                        broker         = strat.broker,
                        execution_mode = strat.execution_mode.value if strat.execution_mode is not None else "SUGGESTION",
                        reasons        = sig.reasons,
                        acted_on       = False,
                    )
                    exec_session.add(db_signal)
                    await exec_session.flush()  # populate db_signal.id before use

                    # ── ML feedback loop ──────────────────────────────────────
                    exec_session.add(TradeOutcomeModel(
                        signal_id     = db_signal.id,
                        symbol        = sig.symbol,
                        timeframe     = sig.timeframe,
                        strategy_name = sig.strategy_name,
                        signal_type   = sig.signal,
                        entry_price   = sig.entry_price,
                        stop_loss     = sig.stop_loss,
                        take_profit   = sig.take_profit,
                        resolved      = False,
                    ))

                    # ── Forward execution ─────────────────────────────────────
                    await forward_engine.initialize(exec_session)
                    result = await forward_engine.process_signal(
                        signal          = sig,
                        execution_mode  = strat.execution_mode.value if strat.execution_mode is not None else "SUGGESTION",
                        is_paper        = strat.is_paper,
                        db_session      = exec_session,
                        strategy_params = params,
                    )
                    if result is not None:
                        result.signal_id = db_signal.id
                    _signal_id = db_signal.id  # read before session closes
                    await exec_session.commit()

                await _ws.broadcast("scalp_signal", {
                    "id":            _signal_id,
                    "symbol":        sig.symbol,
                    "signal":        sig.signal,
                    "entry_price":   sig.entry_price,
                    "stop_loss":     sig.stop_loss,
                    "take_profit":   sig.take_profit,
                    "confidence":    sig.confidence,
                    "timeframe":     sig.timeframe,
                    "strategy_name": sig.strategy_name,
                    "reasons":       sig.reasons,
                    "spread_pct":    spread_pct,
                    "source":        "ws_stream",
                })

            except Exception as exc:
                logger.error(
                    f"[ScalpingStream] process_signal error for {strat.name}: {exc}",
                    exc_info=True,
                )


# Module-level singleton — imported by main.py lifespan
scalping_stream_manager = ScalpingStreamManager()
