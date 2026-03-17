"""
Scalping Signal Runner (Celery task)
=====================================
Dedicated task for scalping strategies — runs independently of the main
``signal_runner.run_signals`` task.

Key differences from the swing/options signal runner:
  - Only processes strategies with ``strategy_type`` starting with ``"scalp_"``
  - NO regime router (session filter instead)
  - NO multi-timeframe confluence check
  - Uses ``_last_scalp_candle_fired`` — separate from swing ``_last_candle_fired``
  - Advisory lock namespace ``scalp:`` avoids collisions with swing dedup locks
  - Spread check supplied from live ticker before strategy execution
  - Execution mode is fully respected as configured per strategy row
    (suggestion / semi-auto / full-auto)
"""

import asyncio
import hashlib
import json
import math
import pathlib
import time as _time
from datetime import datetime, timezone as _tz
from typing import Optional

from celery_app import celery_app
from loguru import logger
from utils import timeframe_to_seconds as _tf_to_secs

# Per-strategy in-memory candle dedup — separate namespace from swing runner
_last_scalp_candle_fired: dict[int, float] = {}  # strategy_id → last_close epoch

# Trackable signals for ML outcome creation
_TRACKABLE = {"BUY", "SELL", "SHORT", "COVER"}

_SCALP_SETTINGS_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "runtime" / "scalping_settings.json"
)


def _load_scalp_settings() -> dict:
    defaults = {
        "enabled": True,
        "timeframe": "5m",
        "risk_per_trade_pct": 0.5,
        "sl_atr_mult": 0.8,
        "tp_atr_mult": 1.6,
        "min_volume_ratio": 1.2,
        "max_spread_pct": 0.05,
        "min_score": 4,
        "ml_veto_threshold": 0.40,
        "sr_tp_snap": False,
        # Risk gate overrides — applied by ForwardEngine for all scalp_ signals
        # so they don't compete with the global broker_risk_settings row.
        "max_consecutive_losses": 5,        # CB trips after N consecutive losses (swing default: 3)
        "daily_circuit_breaker_pct": 5.0,   # max daily loss % before halting scalping (swing default: 3%)
        "max_open_positions": 10,           # max concurrent scalp positions (swing typically 5)
        "max_exposure_per_asset_pct": 20.0, # max % of balance exposed to one symbol (swing default: 10%)
        "max_exposure_per_class_pct": 50.0, # max % of balance in one asset class (swing default: 30%)
        "session_filter": {"crypto": None, "stock": ["14:30-21:00"]},
    }
    try:
        if _SCALP_SETTINGS_PATH.exists():
            data = json.loads(_SCALP_SETTINGS_PATH.read_text())
            return {**defaults, **data}
    except (json.JSONDecodeError, OSError):
        pass
    return defaults


def _is_session_open(asset_class: str, broker: str, settings: dict) -> bool:
    """Return True if current wall-clock time is within an allowed trading session."""
    session_filter = settings.get("session_filter", {})

    # Crypto is 24/7
    if asset_class == "crypto" or broker == "binance":
        if session_filter.get("crypto") is None:
            return True

    # Stock / forex: check NYSE-aligned window list
    sessions = session_filter.get("stock", ["14:30-21:00"])
    if not sessions:
        return True

    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
        if now.weekday() >= 5:  # Saturday / Sunday
            return False
        for window in sessions:
            sh, sm = map(int, window.split("-")[0].split(":"))
            eh, em = map(int, window.split("-")[1].split(":"))
            start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
            end   = now.replace(hour=eh, minute=em, second=0, microsecond=0)
            if start <= now <= end:
                return True
        return False
    except Exception:
        return True  # fail-open so a zoneinfo import failure doesn't silence signals


@celery_app.task(name="tasks.scalping_runner.run_scalping_signals", bind=True, max_retries=3)
def run_scalping_signals(self):
    """
    Scalping signal runner — fires every 60 s via Celery Beat.

    Processes all active DB strategies whose ``strategy_type`` starts with
    ``"scalp_"``.  Skips strategies whose candle hasn't closed since the last
    run.  Fetches OHLCV, checks session, fetches spread, runs the strategy,
    deduplicates, persists signal, and pipes through ForwardEngine.
    """
    try:
        from brokers import reload_broker_modes
        reload_broker_modes()

        asyncio.run(_async_run())
    except Exception as exc:
        logger.error(f"[scalping_runner] Task error: {exc}", exc_info=True)
        raise self.retry(exc=exc, countdown=10)


async def _async_run() -> None:
    from brokers import get_broker
    from core.engine.forward_engine import get_forward_engine
    from core.strategies.scalping_ema_vwap import ScalpingEmaVwap
    from db.database import AsyncSessionLocal
    from db.models import (
        Strategy as StrategyModel,
        Signal as SignalModel,
        TradeOutcome as TradeOutcomeModel,
        AssetClass,
        SignalType,
    )
    from sqlalchemy import select, and_, text

    # Load settings once per task run
    cfg = _load_scalp_settings()
    if not cfg.get("enabled", True):
        logger.debug("[scalping_runner] Scalping disabled via settings — skipping")
        return

    # Strategy registry — add other scalp strategies here when built
    _SCALP_STRATEGY_REGISTRY = {
        "scalp_ema_vwap": ScalpingEmaVwap,
    }

    async with AsyncSessionLocal() as session:
        # Fetch active strategies whose type starts with "scalp_"
        q = await session.execute(
            select(StrategyModel).where(
                StrategyModel.is_active == True,  # noqa: E712
            )
        )
        all_strats = q.scalars().all()
        scalp_strats = [
            s for s in all_strats
            if (s.parameters or {}).get("strategy_type", "").startswith("scalp_")
        ]

        if not scalp_strats:
            logger.debug("[scalping_runner] No active scalping strategies configured")
            return

        forward_engine = get_forward_engine()
        await forward_engine.initialize(session)

        now_ts = _time.time()

        for strat in scalp_strats:
            params        = strat.parameters or {}
            strategy_type = params.get("strategy_type", "scalp_ema_vwap")
            symbol        = params.get("symbol")
            timeframe     = params.get("timeframe", cfg.get("timeframe", "5m"))
            limit         = int(params.get("limit", 100))
            broker_name   = strat.broker.value if hasattr(strat.broker, "value") else str(strat.broker)
            asset_cls_val = getattr(strat.asset_class, "value", "crypto")

            if not symbol:
                logger.warning(f"[scalping_runner] Strategy id={strat.id} missing 'symbol' — skipping")
                continue

            # ── Candle-close dedup (F-010 equivalent) ────────────────────────
            interval      = _tf_to_secs(timeframe)
            last_close_ts = math.floor(now_ts / interval) * interval
            if _last_scalp_candle_fired.get(strat.id, 0) >= last_close_ts:
                logger.debug(f"[scalping_runner] {strat.name} ({timeframe}) candle unchanged — skipping")
                continue

            # ── Session filter ─────────────────────────────────────────────────
            if not _is_session_open(asset_cls_val, broker_name, cfg):
                logger.debug(f"[scalping_runner] {strat.name} — outside session window, skipping")
                continue

            try:
                # ── Fetch OHLCV ─────────────────────────────────────────────────
                broker = get_broker(broker_name)
                await asyncio.wait_for(broker.connect(), timeout=8.0)
                ohlcv = await asyncio.wait_for(
                    broker.get_ohlcv(symbol, timeframe, limit=limit),
                    timeout=12.0,
                )
                if ohlcv is None or ohlcv.empty:
                    logger.warning(f"[scalping_runner] No OHLCV data for {symbol} ({timeframe})")
                    continue

                # ── Live spread check ────────────────────────────────────────────
                spread_pct_live: Optional[float] = None
                try:
                    bid, ask = await asyncio.wait_for(broker.get_bid_ask(symbol), timeout=5.0)
                    if bid and ask and bid > 0:
                        spread_pct_live = (ask - bid) / ((ask + bid) / 2) * 100
                except Exception as _sp_err:
                    logger.debug(f"[scalping_runner] Spread fetch skipped: {_sp_err}")

                # ── Run strategy ─────────────────────────────────────────────────
                strategy_cls = _SCALP_STRATEGY_REGISTRY.get(strategy_type)
                if strategy_cls is None:
                    logger.warning(f"[scalping_runner] Unknown scalp strategy '{strategy_type}'")
                    continue

                strategy = strategy_cls()
                # Propagate broker/asset_class from DB row into the strategy instance
                strategy.broker      = broker_name
                strategy.asset_class = asset_cls_val

                sig = strategy.generate_signal(
                    data=ohlcv,
                    symbol=symbol,
                    timeframe=timeframe,
                    spread_pct=spread_pct_live,
                )
                # Preserve the canonical scalp_ type on the signal so ForwardEngine's
                # risk override check (startswith("scalp_")) always matches correctly.
                # The display name is stored separately in the DB signal row below.
                sig.strategy_name = strategy_type   # e.g. "scalp_ema_vwap"
                _display_name = strat.name           # e.g. "5min UNI/USDT"

                # Skip non-actionable signals — mark candle processed but don't persist
                if sig.signal not in _TRACKABLE:
                    _last_scalp_candle_fired[strat.id] = last_close_ts
                    logger.debug(
                        f"[scalping_runner] HOLD {symbol} ({strat.name}) — no signal this candle"
                    )
                    continue

                # Mark candle as processed (only trackable signals reach here)
                _last_scalp_candle_fired[strat.id] = last_close_ts

                # ── Coerce enums ───────────────────────────────────────────────
                try:
                    sig_type = SignalType(sig.signal)
                except ValueError:
                    logger.warning(
                        f"[scalping_runner] Unrecognized signal '{sig.signal}' for {symbol} — skipping"
                    )
                    continue

                try:
                    asset_cls_enum = AssetClass(sig.asset_class)
                except ValueError:
                    asset_cls_enum = strat.asset_class

                # ── Advisory lock + dedup ────────────────────────────────────────
                # Namespace "scalp:" avoids collision with swing advisory locks.
                _lock_str = f"scalp:{strategy_type}:{symbol}:{timeframe}"
                _lock_int = int.from_bytes(
                    hashlib.sha256(_lock_str.encode()).digest()[:4], "big"
                ) % (2 ** 31)
                await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_int})

                _candle_start = datetime.fromtimestamp(
                    int(datetime.now(_tz.utc).timestamp() // interval) * interval,
                    tz=_tz.utc,
                ).replace(tzinfo=None)

                dup_q = await session.execute(
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
                    logger.debug(f"[scalping_runner] Duplicate {sig.signal} {symbol} — skipped")
                    continue

                # ── Persist signal ───────────────────────────────────────────────
                db_signal = SignalModel(
                    symbol        = sig.symbol,
                    signal        = sig_type,
                    entry_price   = sig.entry_price,
                    stop_loss     = sig.stop_loss,
                    take_profit   = sig.take_profit,
                    confidence    = sig.confidence,
                    timeframe     = sig.timeframe,
                    strategy_name = _display_name,    # human-readable name for UI
                    regime        = sig.regime,
                    asset_class   = asset_cls_enum,
                    broker        = strat.broker,
                    execution_mode= strat.execution_mode.value if strat.execution_mode is not None else "SUGGESTION",
                    reasons       = sig.reasons,
                    acted_on      = False,
                )
                session.add(db_signal)
                await session.flush()

                # ── ML feedback loop (TradeOutcome) ──────────────────────────────
                if sig.signal in _TRACKABLE:
                    session.add(TradeOutcomeModel(
                        signal_id     = db_signal.id,
                        symbol        = sig.symbol,
                        timeframe     = sig.timeframe,
                        strategy_name = _display_name,
                        signal_type   = sig.signal,
                        entry_price   = sig.entry_price,
                        stop_loss     = sig.stop_loss,
                        take_profit   = sig.take_profit,
                        resolved      = False,
                    ))

                await session.commit()
                logger.info(
                    f"[scalping_runner] {sig.signal} {symbol} @ {sig.entry_price:.4f} | "
                    f"conf={sig.confidence:.2f} | strat={strat.name} | tf={timeframe}"
                )

                # ── In-app signal notification ───────────────────────────────────
                try:
                    from notifications.notifier import notifier as _notify
                    await _notify.signal(
                        session,
                        title=f"⚡ [SCALP] {sig.signal} • {sig.symbol}",
                        message=(
                            f"Strategy: {_display_name} | "
                            f"Entry: ${sig.entry_price:,.4f} | "
                            f"Conf: {sig.confidence * 100:.0f}% | "
                            f"TF: {timeframe}"
                            + (f" | Spread: {spread_pct_live:.3f}%" if spread_pct_live else "")
                        ),
                        metadata={
                            "symbol": sig.symbol,
                            "signal": sig.signal,
                            "entry_price": sig.entry_price,
                            "stop_loss": sig.stop_loss,
                            "take_profit": sig.take_profit,
                            "strategy": _display_name,
                            "broker": broker_name,
                            "timeframe": timeframe,
                            "spread_pct": spread_pct_live,
                        },
                    )
                    await session.commit()
                except Exception as _n_err:
                    logger.debug(f"[scalping_runner] Signal notification failed: {_n_err}")

                # ── Forward execution ────────────────────────────────────────────
                if sig.signal in _TRACKABLE:
                    try:
                        # Inject strategy_type into params so ForwardEngine risk override
                        # can detect scalp_ strategies even after strategy_name is display name.
                        exec_params = {**params, "strategy_type": strategy_type}

                        async with AsyncSessionLocal() as exec_session:
                            result = await forward_engine.process_signal(
                                signal=sig,
                                execution_mode=strat.execution_mode.value if strat.execution_mode is not None else "SUGGESTION",
                                is_paper=strat.is_paper,
                                db_session=exec_session,
                                strategy_params=exec_params,
                            )
                            if result is not None:
                                # BUG-FIX: set signal_id and commit so the FK is persisted
                                result.signal_id = db_signal.id
                                # Ensure trade shows the human-readable display name, not the algo type.
                                result.strategy_name = _display_name
                                db_signal.acted_on = True
                                exec_session.add(result)
                                await exec_session.commit()
                                # SAFETY-NET: scalp trade must have SL and TP set.
                                # If bracket order failed silently, close immediately
                                # rather than leave an unprotected position open.
                                from db.models import OrderStatus as _OS
                                if (
                                    result.status == _OS.OPEN
                                    and result.stop_loss is None
                                    and result.take_profit is None
                                ):
                                    logger.critical(
                                        f"[scalping_runner] SAFETY-NET: trade id={result.id} "
                                        f"{result.symbol} opened with no SL/TP — closing immediately"
                                    )
                                    try:
                                        async with AsyncSessionLocal() as _close_sess:
                                            await forward_engine.close_position(
                                                result, reason="null_sltp_safety_close",
                                                db_session=_close_sess,
                                            )
                                            await _close_sess.commit()
                                    except Exception as _safe_err:
                                        logger.error(f"[scalping_runner] Safety close failed: {_safe_err}")
                                # Sync acted_on back to the outer session
                                await session.merge(db_signal)
                                await session.commit()
                            else:
                                await exec_session.commit()

                        # BUG-FIX: re-hydrate ForwardEngine so subsequent scalp strategies
                        # in the same tick see updated balance / open-position counts.
                        try:
                            await forward_engine.initialize(session)
                        except Exception as _rh_err:
                            logger.debug(f"[scalping_runner] ForwardEngine re-hydrate failed: {_rh_err}")

                    except Exception as _fwd_err:
                        logger.error(f"[scalping_runner] ForwardEngine error: {_fwd_err}", exc_info=True)

                    # WebSocket broadcast always fires for actionable signals,
                    # even when ForwardEngine execution fails (e.g. broker error).
                    try:
                        from api.websocket import manager as _ws
                        await _ws.broadcast("scalp_signal", {
                            "id":             db_signal.id,
                            "symbol":         sig.symbol,
                            "signal":         sig.signal,
                            "entry_price":    sig.entry_price,
                            "stop_loss":      sig.stop_loss,
                            "take_profit":    sig.take_profit,
                            "confidence":     sig.confidence,
                            "timeframe":      sig.timeframe,
                            "strategy_name":  _display_name,
                            "reasons":        sig.reasons,
                            "spread_pct":     spread_pct_live,
                            "source":         "celery",
                        })
                    except Exception as _ws_err:
                        logger.debug(f"[scalping_runner] WS broadcast failed: {_ws_err}")

            except asyncio.TimeoutError:
                logger.warning(f"[scalping_runner] Timeout fetching data for {symbol} — skipping")
            except Exception as exc:
                logger.error(f"[scalping_runner] Error for {strat.name} ({symbol}): {exc}", exc_info=True)
                try:
                    await session.rollback()
                except Exception:
                    pass
