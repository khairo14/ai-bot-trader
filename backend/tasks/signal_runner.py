"""Tasks: automated signal runner via Celery."""
from celery_app import celery_app
import asyncio
import hashlib
import json
import logging
import math
import pathlib
import time as _time
from utils import timeframe_to_seconds as _timeframe_to_seconds

logger = logging.getLogger(__name__)

# F-010: track the last candle-close epoch we successfully processed per strategy.
# Prevents running the full engine (data fetch + ML) when the candle hasn't
# changed since the last Celery tick.  In-memory is fine for a single worker;
# the DB deduplication (F-039) is the safety net for multi-worker deployments.
_last_candle_fired: dict[int, float] = {}  # strategy_id → last_close epoch

# ── Regime Router ────────────────────────────────────────────────────────────
# Maps each market regime to the strategy types that are appropriate for it.
# When a strategy has regime_mode="auto_switch", only regime-matched strategies
# run. When regime_mode="fixed" (default), the configured strategy always runs
# and emits a HOLD signal with a reason if the regime doesn't match.
REGIME_STRATEGY_MAP: dict[str, list[str]] = {
    "trending_up":     ["momentum_breakout", "hybrid_macd_rsi", "bull_call_spread"],
    "trending_down":   ["momentum_breakout", "hybrid_macd_rsi"],
    "ranging":         ["mean_reversion_bb", "iron_condor", "covered_call"],
    "high_volatility": ["volatility_squeeze", "iron_condor"],
    "low_volatility":  ["volatility_squeeze", "mean_reversion_bb"],
}

# Per-symbol regime history for hysteresis: symbol → deque of last N regime labels
# Regime switch only happens when the same regime appears N candles in a row.
_regime_history: dict[str, list[str]] = {}   # symbol → [regime, regime, ...]

# Per-symbol current stable regime (only updated after hysteresis confirmed)
_stable_regime: dict[str, str] = {}  # symbol → confirmed stable regime

_REGIME_SETTINGS_PATH = pathlib.Path(__file__).resolve().parent.parent / "runtime" / "regime_settings.json"

# BUG-19 FIX: Redis keys for persisting regime hysteresis across worker restarts.
# On cold start we load both dicts from Redis so hysteresis is never reset to
# zero just because the Celery worker was restarted or replaced.
_REDIS_REGIME_HISTORY_KEY = "regime_hysteresis:history"
_REDIS_STABLE_REGIME_KEY  = "regime_hysteresis:stable"

# M-2 FIX: module-level Redis connection pool so _load/_save each re-use a
# persistent connection instead of opening+closing a socket on every call.
_redis_pool = None


def _get_redis():
    """Return a Redis client backed by the module-level connection pool."""
    global _redis_pool
    if _redis_pool is None:
        try:
            import redis as _sync_redis
            from config import settings as _cfg_s
            _redis_pool = _sync_redis.ConnectionPool.from_url(
                _cfg_s.redis_url,
                decode_responses=True,
                max_connections=4,
                socket_connect_timeout=2,
            )
        except Exception:
            return None
    try:
        import redis as _sync_redis
        return _sync_redis.Redis(connection_pool=_redis_pool)
    except Exception:
        return None


def _load_regime_state_from_redis() -> None:
    """Restore _regime_history and _stable_regime from Redis on worker startup."""
    try:
        _r = _get_redis()
        if _r is None:
            return
        raw_history = _r.get(_REDIS_REGIME_HISTORY_KEY)
        raw_stable  = _r.get(_REDIS_STABLE_REGIME_KEY)
        if raw_history:
            _regime_history.update(json.loads(raw_history))
        if raw_stable:
            _stable_regime.update(json.loads(raw_stable))
    except Exception as _e:
        logger.debug(f"[signal_runner] Could not load regime state from Redis: {_e}")


def _save_regime_state_to_redis() -> None:
    """Persist _regime_history and _stable_regime to Redis after each update."""
    try:
        _r = _get_redis()
        if _r is None:
            return
        _r.set(_REDIS_REGIME_HISTORY_KEY, json.dumps(_regime_history))
        _r.set(_REDIS_STABLE_REGIME_KEY,  json.dumps(_stable_regime))
    except Exception as _e:
        logger.debug(f"[signal_runner] Could not save regime state to Redis: {_e}")


# Load persisted regime state at module import time (worker startup)
_load_regime_state_from_redis()


def _load_regime_settings() -> dict:
    """Load global regime router settings from disk."""
    defaults = {"enabled": True, "hysteresis_candles": 3}
    try:
        if _REGIME_SETTINGS_PATH.exists():
            data = json.loads(_REGIME_SETTINGS_PATH.read_text())
            return {**defaults, **data}
    except (json.JSONDecodeError, OSError):
        pass
    return defaults


def _update_regime_hysteresis(symbol: str, new_regime: str, n: int) -> str:
    """Track regime history and return the stable regime after hysteresis.

    Returns the confirmed stable regime if the last N observations are all
    the same, otherwise returns the previous stable regime (no switch yet).
    If no stable regime has been confirmed yet, uses the new_regime directly
    (cold start — first N candles use whatever regime is observed).
    """
    history = _regime_history.setdefault(symbol, [])
    history.append(new_regime)
    # Keep only last N entries
    if len(history) > n:
        history[:] = history[-n:]

    if len(history) >= n and len(set(history[-n:])) == 1:
        # Regime has been stable for N candles — confirm the switch
        _stable_regime[symbol] = new_regime

    # BUG-19 FIX: persist state after every update so Celery worker restarts
    # don't reset hysteresis to zero (which would cause premature regime switches).
    _save_regime_state_to_redis()

    # Return confirmed stable regime, or fall back to new_regime on cold start
    return _stable_regime.get(symbol, new_regime)

# Signal types that are worth tracking for ML feedback
_TRACKABLE_SIGNALS = {"BUY", "SELL", "SHORT", "COVER"}

# Multi-timeframe confluence: map each TF to the two higher ones to check
_HIGHER_TF: dict[str, list[str]] = {
    "1m":  ["5m",  "15m"],  # fixed: 1h was 60× too far for a 1m scalp
    "3m":  ["15m", "1h"],
    "5m":  ["15m", "1h"],   # fixed: 4h was 48× too far for a 5m strategy
    "15m": ["1h",  "4h"],
    "30m": ["4h",  "1d"],
    "1h":  ["4h",  "1d"],   # industry standard
    "2h":  ["4h",  "1d"],   # fixed: 1w was 84× too far for a 2h strategy
    "4h":  ["1d",  "1w"],
    "6h":  ["1d",  "1w"],
    "12h": ["1d",  "1w"],
    "1d":  [],  # already highest common TF — no suppression
    "1w":  [],
}
# Minimum fraction of timeframes (including primary) that must agree to allow execution.
# Can be overridden per strategy via parameters["min_confluence"].
# Sensible per-strategy defaults (used when not set in parameters):
#   hybrid_macd_rsi    → 0.5  (trend-following: needs alignment)
#   momentum_breakout  → 0.5  (breakouts confirm across TFs)
#   mean_reversion_bb  → 0.0  (counter-trend by design — bypass)
MIN_CONFLUENCE = 0.5
_STRATEGY_CONFLUENCE_DEFAULTS: dict[str, float] = {
    "mean_reversion_bb":  0.0,   # counter-trend — higher TFs will always disagree
    "hybrid_macd_rsi":    0.5,
    "momentum_breakout":  0.5,
    "volatility_squeeze": 0.6,  # breakout after compression — multi-TF helps
    # Options income strategies emit SELL (non-directional) — higher TFs will
    # always return HOLD, so confluence suppression must be disabled for them.
    "covered_call":       0.0,
    "iron_condor":        0.0,
    "bull_call_spread":   0.0,
}


async def _confluence_score(
    engine,
    strategy_type: str,
    symbol: str,
    broker: str,
    primary_tf: str,
    primary_signal: str,
) -> float:
    """
    Runs the same strategy on the higher timeframes and returns the fraction
    that agree with the primary signal (1.0 = full agreement, 0.33 = only primary).
    """
    higher_tfs = _HIGHER_TF.get(primary_tf, [])
    if not higher_tfs:
        return 1.0  # daily/weekly — no suppression

    votes = [primary_signal]  # primary TF already voted
    for tf in higher_tfs:
        try:
            # Bug-12 FIX: wrap each broker call in a timeout so a slow IBKR
            # connection does not block the entire Celery task.
            sig = await asyncio.wait_for(
                engine.run(
                    strategy_name=strategy_type,
                    symbol=symbol,
                    broker_name=broker,
                    timeframe=tf,
                    limit=200,
                ),
                timeout=15.0,
            )
            votes.append(sig.signal)
        except asyncio.TimeoutError:
            # Slow broker — skip this TF from denominator (same as network error).
            logger.warning(f"[confluence] {strategy_type} {symbol} {tf} timed out (>15s) — skipping")
        except ValueError as exc:
            # No data / bad symbol on this TF — counts as a genuine HOLD vote
            logger.debug(f"[confluence] {strategy_type} {symbol} {tf} no-data: {exc}")
            votes.append("HOLD")
        except Exception as exc:
            # Transient broker/network error — skip this TF from the denominator
            # so a broker outage doesn't systematically suppress all strategies.
            logger.debug(f"[confluence] {strategy_type} {symbol} {tf} skipped (error): {exc}")

    agreeing = sum(1 for v in votes if v == primary_signal)
    return agreeing / len(votes)


@celery_app.task(name="tasks.signal_runner.run_signals", bind=True, max_retries=3)
def run_signals(self):
    """
    Fetch latest OHLCV data for all active strategies, generate signals,
    persist them to DB, and pipe actionable signals through ForwardEngine.

    Each Strategy row must have a `parameters` JSON like:
        {
            "strategy_type": "hybrid_macd_rsi",
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "limit": 200          # optional, default 200
        }
    """
    try:
        # Reload broker modes from runtime/broker_modes.json at the start of every
        # task run.  This is what makes UI paper/live toggles propagate to the Celery
        # worker without a process restart (FastAPI saves the file; Celery re-reads it).
        from brokers import reload_broker_modes
        reload_broker_modes()

        import asyncio
        from core.engine.signal_engine import SignalEngine
        from core.engine.forward_engine import ForwardEngine
        from db.database import AsyncSessionLocal
        from db.models import (
            Strategy as StrategyModel,
            Signal as SignalModel,
            AssetClass,
            BrokerName,
            SignalType,
            OrderStatus,
        )
        from sqlalchemy import select

        async def _run_broker_group(broker_name: str, strategies: list) -> tuple[int, int]:
            """
            Process all strategies for ONE broker sequentially, using a dedicated
            DB session.  Runs concurrently with other broker groups via asyncio.gather.

            Isolation guarantees:
            - Each broker group has its own AsyncSession → no cross-broker session
              sharing, no lock contention.
            - ForwardEngine is per-group → balance/position state is broker-scoped.
            - A crash or long IBKR connect timeout in one group never delays another.
            """
            from api.websocket import manager as ws_manager
            from notifications.notifier import notifier as _notify

            local_signal_engine = SignalEngine()
            local_forward_engine = ForwardEngine()
            group_run = 0
            group_errors = 0

            async with AsyncSessionLocal() as session:
                # Hydrate this group's ForwardEngine from the DB.
                await local_forward_engine.initialize(session)

                for strat in strategies:
                    params = strat.parameters or {}
                    strategy_type = params.get("strategy_type") or params.get("strategy_name")
                    symbol = params.get("symbol")
                    timeframe = params.get("timeframe", "1h")
                    limit = int(params.get("limit", 200))

                    if not strategy_type or not symbol:
                        logger.warning(
                            f"[signal_runner] Strategy id={strat.id} name='{strat.name}' "
                            "missing 'strategy_type' or 'symbol' in parameters — skipping."
                        )
                        continue

                    # F-010: skip if the candle for this timeframe hasn't closed
                    # since we last ran this strategy (avoids 288 runs/day for 1d strategies).
                    _interval = _timeframe_to_seconds(timeframe)
                    _now_ts = _time.time()
                    _last_close_ts = math.floor(_now_ts / _interval) * _interval
                    if _last_candle_fired.get(strat.id, 0) >= _last_close_ts:
                        logger.debug(
                            f"[signal_runner] {strat.name} ({timeframe}) — "
                            f"candle unchanged since last run, skipping"
                        )
                        continue

                    try:
                        # ── Regime Router (Phase 1 + 2) ──────────────────────────
                        # Step 1: load global settings
                        _regime_cfg = _load_regime_settings()
                        _regime_enabled = _regime_cfg.get("enabled", True)
                        _hysteresis_n   = int(_regime_cfg.get("hysteresis_candles", 3))

                        # Step 2: per-strategy regime mode — default "fixed"
                        _regime_mode = str(params.get("regime_mode", "fixed")).lower()
                        # Per-strategy override: list of allowed regimes
                        _regime_filter: list[str] | None = params.get("regime_filter")

                        # Step 3: classify market regime (only when routing is enabled)
                        # _ohlcv is pre-fetched here for the regime classifier and reused
                        # by signal_engine.run() to avoid a second broker round-trip.
                        # Initialise to None so the data= kwarg below is always defined
                        # even when the broker connect or OHLCV fetch fails (BUG-MED-01).
                        _ohlcv = None
                        _active_strategy_type = strategy_type  # may be swapped for auto_switch
                        if _regime_enabled:
                            try:
                                from brokers import get_broker as _get_broker
                                from core.regime_classifier import regime_classifier as _rc
                                _broker_obj = _get_broker(strat.broker.value)
                                # Default 12 s fast-fail: a down broker won't stall
                                # this group's candle tick.
                                await asyncio.wait_for(_broker_obj.connect(), timeout=12.0)
                                _ohlcv = await asyncio.wait_for(
                                    _broker_obj.get_ohlcv(symbol, timeframe, limit=max(limit, 100)),
                                    timeout=20.0,
                                )
                                # IMP-30: pass asset_class so classify() selects the
                                # appropriate ADX threshold for this instrument type
                                _asset_class_str = params.get("asset_class", "")
                                _regime_result = _rc.classify(_ohlcv, asset_class=_asset_class_str)
                                _raw_regime = _regime_result.regime
                                # Apply hysteresis — only switch after N stable candles
                                _symbol_key = f"{strat.id}:{symbol}:{timeframe}"  # Bug-17 FIX: include strat.id
                                _confirmed_regime = _update_regime_hysteresis(
                                    _symbol_key, _raw_regime, _hysteresis_n
                                )

                                # IMP-05: auto-reset per-strategy CB when regime normalises
                                # away from high_volatility — losses were regime-driven.
                                from core.risk_manager import get_risk_manager as _get_rm
                                _rm = _get_rm()
                                if _rm.maybe_reset_on_regime_change(
                                    strategy_type, _confirmed_regime
                                ):
                                    logger.info(
                                        f"[RegimeRouter] {strat.name} | {symbol} | "
                                        f"regime={_confirmed_regime} → CB auto-reset"
                                    )

                                # If classifier had no data / errored, confidence=0.0
                                # means we can't trust the classification — skip filtering
                                if _regime_result.confidence == 0.0:
                                    logger.debug(
                                        f"[RegimeRouter] {strat.name} | {symbol} | "
                                        "regime confidence=0.0 — skipping regime filter"
                                    )
                                    raise ValueError("low-confidence regime — skip")

                                # Determine which strategies are allowed for this regime
                                _allowed = (
                                    _regime_filter
                                    if _regime_filter
                                    else REGIME_STRATEGY_MAP.get(_confirmed_regime, [])
                                )

                                if _regime_mode == "auto_switch":
                                    # Pick best strategy for this regime
                                    if strategy_type not in _allowed:
                                        # Find first allowed strategy available in the engine
                                        _candidate = next(
                                            (s for s in _allowed
                                             if s in local_signal_engine._strategies),
                                            None,
                                        )
                                        # If none are loaded yet, fall back to any name in the map
                                        # (signal_engine will lazy-load it on first run)
                                        if _candidate is None:
                                            _candidate = next(iter(_allowed), None)
                                        if _candidate and _candidate != strategy_type:
                                            logger.info(
                                                f"[RegimeRouter] {strat.name} | {symbol} | "
                                                f"regime={_confirmed_regime} → "
                                                f"auto-switch {strategy_type} → {_candidate}"
                                            )
                                            _active_strategy_type = _candidate
                                        else:
                                            logger.info(
                                                f"[RegimeRouter] {strat.name} | {symbol} | "
                                                f"regime={_confirmed_regime} → "
                                                f"no suitable strategy found, holding"
                                            )
                                            _last_candle_fired[strat.id] = _last_close_ts  # Bug-18 FIX
                                            continue  # no viable replacement — skip this candle
                                else:
                                    # fixed mode: if mismatch, save HOLD and skip execution
                                    if strategy_type not in _allowed:
                                        logger.info(
                                            f"[RegimeRouter] {strat.name} | {symbol} | "
                                            f"regime={_confirmed_regime} → "
                                            f"{strategy_type} not suitable (fixed mode), HOLD"
                                        )
                                        # Record a HOLD signal so dashboard shows the reason
                                        _price = float(_ohlcv["close"].iloc[-1])
                                        from core.strategies.base import Signal as _Sig
                                        _hold_sig = _Sig(
                                            symbol=symbol, signal="HOLD",
                                            entry_price=float(_price),
                                            stop_loss=None, take_profit=None,
                                            confidence=0.0, timeframe=timeframe,
                                            strategy_name=strat.name,
                                            asset_class=getattr(strat.asset_class, "value", "crypto"),
                                            broker=strat.broker.value,
                                            regime=_confirmed_regime,
                                            reasons=[
                                                f"regime-filtered: {strategy_type} not suitable "
                                                f"for {_confirmed_regime} market (fixed mode)"
                                            ],
                                        )
                                        _last_candle_fired[strat.id] = _last_close_ts
                                        # Persist HOLD to DB for dashboard visibility
                                        try:
                                            _hold_sig_type = SignalType.HOLD
                                            try:
                                                _hold_asset = AssetClass(getattr(strat.asset_class, "value", "crypto"))
                                            except ValueError:
                                                _hold_asset = strat.asset_class
                                            from db.models import Signal as _SignalModel
                                            _hold_db = _SignalModel(
                                                symbol=symbol, signal=_hold_sig_type,
                                                entry_price=float(_price),
                                                stop_loss=None, take_profit=None,
                                                confidence=0.0, timeframe=timeframe,
                                                strategy_name=strat.name,
                                                strategy_type=strategy_type,
                                                regime=_confirmed_regime,
                                                asset_class=_hold_asset,
                                                broker=strat.broker,
                                                execution_mode=strat.execution_mode.value,
                                                reasons=_hold_sig.reasons,
                                                acted_on=False,
                                            )
                                            session.add(_hold_db)
                                            await session.commit()
                                        except Exception as _hdb_err:
                                            logger.debug(f"[RegimeRouter] HOLD persist error: {_hdb_err}")
                                            await session.rollback()
                                        continue

                            except Exception as _re_err:
                                logger.warning(
                                    f"[RegimeRouter] Regime classification failed for "
                                    f"{strat.name} ({symbol}): {_re_err} — running strategy as-is"
                                )
                                _confirmed_regime = None

                        # ── Generate signal ──────────────────────────────────────
                        # BUG-MED-01 FIX: pass the already-fetched OHLCV DataFrame so
                        # signal_engine.run() skips its own broker.get_ohlcv() call.
                        # Without data=, every strategy fetches OHLCV a second time —
                        # unnecessary broker round-trips and rate-limit consumption.
                        sig = await local_signal_engine.run(
                            strategy_name=_active_strategy_type,
                            symbol=symbol,
                            broker_name=strat.broker.value,
                            timeframe=timeframe,
                            limit=limit,
                            asset_class=getattr(strat.asset_class, "value", None),
                            data=_ohlcv if _ohlcv is not None else None,
                        )
                        # Always store the user-defined strategy name (strat.name) so
                        # the UI displays the configured strategy name, not the algorithm type.
                        sig.strategy_name = strat.name
                        if _active_strategy_type != strategy_type:
                            sig.reasons = (sig.reasons or []) + [
                                f"configured as {strategy_type}, executed as "
                                f"{_active_strategy_type} via regime auto-switch "
                                f"(regime: {_confirmed_regime if _regime_enabled else 'n/a'})"
                            ]

                        # ── Persist signal to DB ─────────────────────────────────
                        # Coerce enums safely
                        try:
                            sig_type = SignalType(sig.signal)
                        except ValueError:
                            sig_type = SignalType.HOLD

                        try:
                            asset_cls = AssetClass(sig.asset_class)
                        except ValueError:
                            asset_cls = strat.asset_class

                        # ── B3: Advisory lock prevents concurrent Celery workers from
                        # both passing the SELECT and double-inserting the same signal.
                        # pg_advisory_xact_lock is transaction-scoped and auto-released.
                        # H-3 FIX: use SHA-256 (not Python's PYTHONHASHSEED-randomised
                        # hash()) so the lock key is consistent across all worker processes.
                        _lock_key_str = f"{strategy_type}:{sig.symbol}:{timeframe}"
                        _lock_key_int = int.from_bytes(
                            hashlib.sha256(_lock_key_str.encode()).digest()[:4], "big"
                        ) % (2 ** 31)
                        await session.execute(
                            __import__("sqlalchemy").text("SELECT pg_advisory_xact_lock(:k)"),
                            {"k": _lock_key_int},
                        )

                        # ── Deduplication: skip if identical signal already exists
                        # within the current candle window (prevents duplicate orders
                        # when Celery fires the same task multiple times per candle).
                        from datetime import datetime, timezone as _tz
                        from sqlalchemy import and_
                        # GAP-4 FIX: added 3m, 30m, 2h, 6h, 12h so these timeframes
                        # get the correct candle-window instead of falling back to 1h.
                        _candle_secs = _timeframe_to_seconds(timeframe)
                        _candle_start = datetime.fromtimestamp(
                            int(datetime.now(_tz.utc).timestamp() // _candle_secs) * _candle_secs,
                            tz=_tz.utc,
                        ).replace(tzinfo=None)  # tz-naive to match TIMESTAMP WITHOUT TIME ZONE
                        _dup_q = await session.execute(
                            select(SignalModel.id).where(
                                and_(
                                    # Key on the user-defined strategy name (strat.name) so
                                    # each configured strategy row has its own dedup slot.
                                    SignalModel.strategy_name == strat.name,
                                    SignalModel.symbol == sig.symbol,
                                    SignalModel.signal == sig_type,
                                    SignalModel.timeframe == timeframe,
                                    SignalModel.created_at >= _candle_start,
                                )
                            ).limit(1)
                        )
                        if _dup_q.scalar_one_or_none() is not None:
                            logger.debug(
                                f"[signal_runner] Duplicate {sig.signal} {sig.symbol} "
                                f"({timeframe}) for '{strat.name}' — skipped"
                            )
                            continue

                        db_signal = SignalModel(
                            symbol=sig.symbol,
                            signal=sig_type,
                            entry_price=sig.entry_price,
                            stop_loss=sig.stop_loss,
                            take_profit=sig.take_profit,
                            confidence=sig.confidence,
                            timeframe=sig.timeframe,
                            strategy_name=sig.strategy_name,
                            strategy_type=_active_strategy_type,
                            regime=sig.regime,
                            asset_class=asset_cls,
                            broker=strat.broker,
                            execution_mode=strat.execution_mode.value,
                            reasons=sig.reasons,
                            acted_on=False,
                            # Options fields (None for non-options signals)
                            iv_rank=getattr(sig, "iv_rank", None),
                            delta=getattr(sig, "delta", None),
                            theta=getattr(sig, "theta", None),
                            vega=getattr(sig, "vega", None),
                            options_meta=getattr(sig, "options_meta", None),
                        )
                        session.add(db_signal)
                        await session.flush()   # get db_signal.id

                        # ── Create TradeOutcome for ML feedback loop ──────────
                        if sig.signal in _TRACKABLE_SIGNALS:
                            from db.models import TradeOutcome as TradeOutcomeModel
                            _trailing = None
                            if strat.parameters:
                                _t = strat.parameters.get("trailing_stop_pct")
                                if _t is not None:
                                    try:
                                        _trailing = float(_t)
                                    except (TypeError, ValueError):
                                        pass
                            # Attach trailing_stop_pct to the signal so execute_signal
                            # stores it on the Trade record for live monitoring.
                            # Priority: static strategy param > dynamic ATR value from _enhance_signal.
                            if _trailing is not None:
                                sig.trailing_stop_pct = _trailing
                            # Use sig.trailing_stop_pct (which may be ATR-dynamic) so the
                            # TradeOutcome resolver applies the same trailing logic as the live trade.
                            session.add(TradeOutcomeModel(
                                signal_id=db_signal.id,
                                symbol=sig.symbol,
                                timeframe=sig.timeframe,
                                strategy_name=sig.strategy_name,
                                signal_type=sig.signal,
                                entry_price=sig.entry_price,
                                stop_loss=sig.stop_loss,
                                take_profit=sig.take_profit,
                                trailing_stop_pct=sig.trailing_stop_pct,
                                resolved=False,
                                is_paper=strat.is_paper,
                                # IMP-04 / GAP-06: store category + options data at creation time
                                signal_type_category=(
                                    "premium_collection"
                                    if _active_strategy_type in {"iron_condor", "covered_call", "bull_call_spread"}
                                    else "directional"
                                ),
                                options_meta=getattr(sig, "options_meta", None),
                            ))

                        # ── Broadcast signal to WebSocket clients ─────────────
                        await ws_manager.broadcast("signal", {
                            "id": db_signal.id,
                            "symbol": sig.symbol,
                            "signal": sig.signal,
                            "entry_price": sig.entry_price,
                            "confidence": sig.confidence,
                            "strategy_name": sig.strategy_name,
                            "broker": strat.broker.value,
                        })

                        # ── In-app notification for actionable signals ──────────
                        if sig.signal not in ("HOLD", None):
                            await _notify.signal(
                                session,
                                title=f"{sig.signal} • {sig.symbol}",
                                message=(
                                    f"Strategy: {sig.strategy_name} | "
                                    f"Entry: ${sig.entry_price:,.4f} | "
                                    f"Confidence: {sig.confidence*100:.0f}%"
                                ),
                                metadata={
                                    "symbol": sig.symbol,
                                    "signal": sig.signal,
                                    "entry_price": sig.entry_price,
                                    "strategy": sig.strategy_name,
                                    "broker": strat.broker.value,
                                    "timeframe": sig.timeframe,
                                },
                            )

                        # ── UI-02: Multi-timeframe confluence check ──────────────
                        # For actionable signals, verify higher timeframes agree.
                        # If confluence < threshold, suppress execution but
                        # still save the signal (visible on Dashboard as low-conf).
                        # Threshold is read from strategy params first, then
                        # per-strategy default, then global MIN_CONFLUENCE.
                        _strat_default = _STRATEGY_CONFLUENCE_DEFAULTS.get(
                            strategy_type, MIN_CONFLUENCE
                        )
                        _min_conf = float(
                            params.get("min_confluence", _strat_default)
                        )
                        allow_execution = True
                        conf = 1.0
                        if sig.signal in _TRACKABLE_SIGNALS and _min_conf > 0.0:
                            conf = await _confluence_score(
                                local_signal_engine, strategy_type, symbol,
                                strat.broker.value, timeframe, sig.signal,
                            )
                            if conf < _min_conf:
                                allow_execution = False
                                _note = f"execution suppressed: low multi-TF confluence ({conf:.0%})"
                                sig.reasons = (sig.reasons or []) + [_note]
                                db_signal.reasons = sig.reasons  # sync to already-flushed DB record
                                logger.info(
                                    f"[signal_runner] ⚠ Low confluence {conf:.0%} for "
                                    f"{sig.signal} {symbol} on {timeframe} — not executing"
                                )
                                try:
                                    await _notify.warning(
                                        session,
                                        title=f"⚠️ Execution Suppressed — {sig.signal} {symbol}",
                                        message=(
                                            f"Strategy: {sig.strategy_name} | "
                                            f"Multi-TF confluence {conf:.0%} below minimum {_min_conf:.0%}. "
                                            f"Signal saved but trade not placed."
                                        ),
                                        metadata={
                                            "symbol": symbol,
                                            "signal": sig.signal,
                                            "strategy": sig.strategy_name,
                                            "confluence": conf,
                                            "min_confluence": _min_conf,
                                            "reason": "low_confluence",
                                        },
                                    )
                                except Exception as _nw_err:
                                    logger.debug(f"[signal_runner] Suppression notification failed: {_nw_err}")

                        # ── Market-hours gate (execution only) ──────────────────
                        # Signals are always saved — useful visibility even overnight.
                        # Trade execution (paper or live) is suppressed when the
                        # broker's session is closed. Crypto (Binance) is always open.
                        if allow_execution:
                            from api.routes.forward_test import is_market_open as _is_mkt_open
                            if not _is_mkt_open(strat.broker.value, getattr(strat.asset_class, "value", None)):  # F-098: pass asset_class for FX hours check
                                allow_execution = False
                                _market_note = f"execution suppressed: {strat.broker.value} session closed"
                                sig.reasons = (sig.reasons or []) + [_market_note]
                                db_signal.reasons = sig.reasons
                                logger.info(
                                    f"[signal_runner] ⏸ Market closed for {strat.broker.value} — "
                                    f"signal saved but trade suppressed"
                                )
                                try:
                                    await _notify.warning(
                                        session,
                                        title=f"⏸ Execution Suppressed — {sig.signal} {symbol}",
                                        message=(
                                            f"Strategy: {sig.strategy_name} | "
                                            f"{strat.broker.value} session is closed. "
                                            f"Signal saved but trade not placed."
                                        ),
                                        metadata={
                                            "symbol": symbol,
                                            "signal": sig.signal,
                                            "strategy": sig.strategy_name,
                                            "broker": strat.broker.value,
                                            "reason": "market_closed",
                                        },
                                    )
                                except Exception as _nw_err:
                                    logger.debug(f"[signal_runner] Market-closed notification failed: {_nw_err}")

                        # ── ML-03: Portfolio weight multiplier ───────────────────
                        # Strategies with a higher Sharpe-based weight (set by the
                        # portfolio optimizer) get proportionally larger position sizes.
                        port_weight = 1.0
                        try:
                            w = params.get("weight")
                            if w is not None:
                                port_weight = max(0.05, float(w))
                        except (TypeError, ValueError):
                            pass

                        # ── Pipe through ForwardEngine ───────────────────────────
                        trade = await local_forward_engine.process_signal(
                            signal=sig,
                            execution_mode=strat.execution_mode.value,
                            is_paper=strat.is_paper,
                            db_session=session,
                            position_size_multiplier=port_weight,
                            strategy_params=params,
                        ) if allow_execution else None

                        if trade is not None:
                            trade.signal_id = db_signal.id
                            db_signal.acted_on = True  # mark regardless of OPEN/REJECTED
                            # F-083: process_signal() already broadcasts the "trade" WS event
                            # and sends the trade notification (+ email) internally.
                            # Removing the redundant block here prevents 2× WS events,
                            # 2× DB notification rows, and 2× emails per live trade fill.
                        await session.commit()

                        # BUG-18 FIX: re-hydrate ForwardEngine in-memory state after
                        # each per-strategy commit.  Without this, _paper_balance and
                        # _paper_positions can drift from DB reality across strategies
                        # in the same Celery tick (e.g. a trade opened by Strategy A
                        # reduces available balance, but Strategy B's fallback balance
                        # still reads the pre-A value stored at initialization time).
                        try:
                            await local_forward_engine.initialize(session)
                        except Exception as _rh_err:
                            logger.debug(f"[signal_runner] ForwardEngine re-hydrate failed (non-fatal): {_rh_err}")

                        logger.info(
                            f"[signal_runner] ✓ [{broker_name}] {strat.name} | {symbol} | {sig.signal} "
                            f"@ {sig.entry_price} (conf={sig.confidence:.2f}) "
                            f"acted_on={db_signal.acted_on}"
                        )
                        # F-010: record that we processed this candle so the next
                        # Celery tick skips it (candle hasn't changed).
                        _last_candle_fired[strat.id] = _last_close_ts
                        group_run += 1

                    except Exception as e:
                        await session.rollback()
                        logger.error(
                            f"[signal_runner] ✗ [{broker_name}] Strategy id={strat.id} name='{strat.name}': {e}",
                            exc_info=True,
                        )
                        group_errors += 1
                        # BUG-MED-01 FIX: persist regime hysteresis state to Redis so it
                        # survives Celery worker restarts even when a strategy errors out.
                        try:
                            _save_regime_state_to_redis()
                        except Exception as _redis_err:
                            logger.debug(f"[signal_runner] Failed to save regime state after error: {_redis_err}")

            return group_run, group_errors

        async def _run():
            total_run = 0
            total_errors = 0

            # ── Phase 1: reconcile / monitor / PENDING cleanup ────────────────
            # Use a shared short-lived session for housekeeping only.
            # A single shared ForwardEngine is fine here because reconcile and
            # monitor iterate per-broker internally without storing trade state.
            housekeeping_engine = ForwardEngine()
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    # Exclude paper strategies — they are handled by the
                    # wall-clock-aligned in-process forward_test scheduler
                    # (main.py lifespan → _forward_test_scheduler).  Running
                    # them here too would create duplicate signals + trades.
                    select(StrategyModel).where(
                        StrategyModel.is_active == True,
                        StrategyModel.is_paper == False,
                    )
                )
                active_strategies = result.scalars().all()

                await housekeeping_engine.initialize(session)

                # Reconcile ghost positions FIRST: mark DB OPEN trades as FILLED
                # that the broker already closed via SL/TP bracket orders.  Running
                # this before monitor_sl_tp prevents the monitor from placing a
                # duplicate close order for positions the broker already exited.
                try:
                    _ghosts = await housekeeping_engine.reconcile_positions(session)
                    if _ghosts:
                        logger.info(f"[signal_runner] Reconciled {_ghosts} ghost position(s)")
                except Exception as _rec_err:
                    logger.warning(f"[signal_runner] Reconcile error (non-fatal): {_rec_err}", exc_info=True)

                # Software SL/TP enforcement — runs AFTER reconcile so it only
                # fires on positions that are genuinely still open at the broker.
                try:
                    _sl_closed = await housekeeping_engine.monitor_sl_tp(session)
                    if _sl_closed:
                        logger.info(f"[signal_runner] SL/TP monitor closed {_sl_closed} position(s)")
                except Exception as _mon_err:
                    logger.debug(f"[signal_runner] SL/TP monitor error (non-fatal): {_mon_err}")

                # IMP-31: resolve stale PENDING trades before running strategies
                # so they don't hold position slots indefinitely.
                try:
                    _pending_resolved = await housekeeping_engine.cleanup_stale_pending_trades(session)
                    if _pending_resolved:
                        logger.info(f"[signal_runner] IMP-31: resolved {_pending_resolved} stale PENDING trade(s)")
                except Exception as _pend_err:
                    logger.debug(f"[signal_runner] PENDING cleanup error (non-fatal): {_pend_err}")

                # F-083: Commit monitor/reconcile changes before the strategy loop.
                # If the first strategy below raises and triggers session.rollback(),
                # WITHOUT this commit the SL/TP closing orders already sent to the
                # broker would be undone in the DB — leaving ghost OPEN trades.
                try:
                    await session.commit()
                except Exception as _pre_commit_err:
                    # This is a critical failure: SL/TP monitor changes won't be
                    # persisted, positions may be unprotected. Raise so the Celery
                    # task retries rather than continuing with a dirty session.
                    logger.error(
                        f"[signal_runner] Monitor/reconcile pre-commit failed — "
                        f"aborting this run to avoid ghost positions: {_pre_commit_err}"
                    )
                    raise

            # ── Phase 2: per-broker parallel signal generation + execution ────
            # Group active strategies by broker name so each group gets its own
            # isolated asyncio.Task, DB session, and ForwardEngine instance.
            # Binance, Alpaca, and IBKR groups race concurrently — a 12 s IBKR
            # connect timeout never stalls Binance/Alpaca signals.
            from collections import defaultdict as _defaultdict
            broker_groups: dict[str, list] = _defaultdict(list)
            for strat in active_strategies:
                broker_groups[strat.broker.value].append(strat)

            if broker_groups:
                results = await asyncio.gather(
                    *[
                        _run_broker_group(broker_name, strategies)
                        for broker_name, strategies in broker_groups.items()
                    ],
                    return_exceptions=True,
                )
                for broker_name, result in zip(broker_groups.keys(), results):
                    if isinstance(result, Exception):
                        logger.error(
                            f"[signal_runner] Broker group '{broker_name}' raised an "
                            f"unhandled exception: {result}",
                            exc_info=result,
                        )
                        total_errors += 1
                    else:
                        r, e = result
                        total_run += r
                        total_errors += e

            logger.info(
                f"[signal_runner] Completed: {total_run} strategies run, {total_errors} errors."
            )

        asyncio.run(_run())

    except Exception as exc:
        logger.error(f"[signal_runner] Task-level failure: {exc}", exc_info=True)
        raise self.retry(exc=exc, countdown=60)
