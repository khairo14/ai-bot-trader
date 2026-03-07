"""Tasks: automated signal runner via Celery."""
from celery_app import celery_app
import logging
import math
import time as _time

logger = logging.getLogger(__name__)

# F-010: track the last candle-close epoch we successfully processed per strategy.
# Prevents running the full engine (data fetch + ML) when the candle hasn't
# changed since the last Celery tick.  In-memory is fine for a single worker;
# the DB deduplication (F-039) is the safety net for multi-worker deployments.
_last_candle_fired: dict[int, float] = {}  # strategy_id → last_close epoch

# Signal types that are worth tracking for ML feedback
_TRACKABLE_SIGNALS = {"BUY", "SELL", "SHORT", "COVER"}

# Multi-timeframe confluence: map each TF to the two higher ones to check
_HIGHER_TF: dict[str, list[str]] = {
    "1m":  ["5m",  "1h"],
    "5m":  ["1h",  "4h"],
    "15m": ["1h",  "4h"],
    "1h":  ["4h",  "1d"],
    "4h":  ["1d",  "1w"],
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
    "mean_reversion_bb": 0.0,   # counter-trend — higher TFs will always disagree
    "hybrid_macd_rsi":   0.5,
    "momentum_breakout": 0.5,
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
            sig = await engine.run(
                strategy_name=strategy_type,
                symbol=symbol,
                broker_name=broker,
                timeframe=tf,
                limit=200,
            )
            votes.append(sig.signal)
        except Exception as exc:
            logger.debug(f"[confluence] {strategy_type} {symbol} {tf}: {exc}")
            votes.append("HOLD")  # treat error as neutral

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

        async def _run():
            signal_engine = SignalEngine()
            forward_engine = ForwardEngine()
            total_run = 0
            total_errors = 0

            from api.websocket import manager as ws_manager
            from notifications.notifier import notifier as _notify

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

                # Hydrate in-memory state from DB before processing any signal
                await forward_engine.initialize(session)

                for strat in active_strategies:
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
                    _tf_secs_map = {
                        "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
                        "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
                        "1d": 86400, "1w": 604800,
                    }
                    _interval = _tf_secs_map.get(timeframe, 3600)
                    _now_ts = _time.time()
                    _last_close_ts = math.floor(_now_ts / _interval) * _interval
                    if _last_candle_fired.get(strat.id, 0) >= _last_close_ts:
                        logger.debug(
                            f"[signal_runner] {strat.name} ({timeframe}) — "
                            f"candle unchanged since last run, skipping"
                        )
                        continue

                    try:
                        # ── Generate signal ──────────────────────────────────────
                        sig = await signal_engine.run(
                            strategy_name=strategy_type,
                            symbol=symbol,
                            broker_name=strat.broker.value,
                            timeframe=timeframe,
                            limit=limit,
                        )

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

                        # ── Deduplication: skip if identical signal already exists
                        # within the current candle window (prevents duplicate orders
                        # when Celery fires the same task multiple times per candle).
                        from datetime import datetime, timezone as _tz
                        from sqlalchemy import and_
                        _tf_seconds = {
                            "1m": 60, "5m": 300, "15m": 900, "1h": 3600,
                            "4h": 14400, "1d": 86400, "1w": 604800,
                        }
                        _candle_secs = _tf_seconds.get(timeframe, 3600)
                        _candle_start = datetime.fromtimestamp(
                            int(datetime.now(_tz.utc).timestamp() // _candle_secs) * _candle_secs,
                            tz=_tz.utc,
                        )
                        _dup_q = await session.execute(
                            select(SignalModel.id).where(
                                and_(
                                    SignalModel.strategy_name == (sig.strategy_name or strategy_type),
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
                            session.add(TradeOutcomeModel(
                                signal_id=db_signal.id,
                                symbol=sig.symbol,
                                timeframe=sig.timeframe,
                                strategy_name=sig.strategy_name,
                                signal_type=sig.signal,
                                entry_price=sig.entry_price,
                                stop_loss=sig.stop_loss,
                                take_profit=sig.take_profit,
                                trailing_stop_pct=_trailing,
                                resolved=False,
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
                                signal_engine, strategy_type, symbol,
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

                        # ── Market-hours gate (execution only) ──────────────────
                        # Signals are always saved — useful visibility even overnight.
                        # Trade execution (paper or live) is suppressed when the
                        # broker's session is closed. Crypto (Binance) is always open.
                        if allow_execution:
                            from api.routes.forward_test import is_market_open as _is_mkt_open
                            if not _is_mkt_open(strat.broker.value):
                                allow_execution = False
                                _market_note = f"execution suppressed: {strat.broker.value} session closed"
                                sig.reasons = (sig.reasons or []) + [_market_note]
                                db_signal.reasons = sig.reasons
                                logger.info(
                                    f"[signal_runner] ⏸ Market closed for {strat.broker.value} — "
                                    f"signal saved but trade suppressed"
                                )

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
                        trade = await forward_engine.process_signal(
                            signal=sig,
                            execution_mode=strat.execution_mode.value,
                            is_paper=strat.is_paper,
                            db_session=session,
                            position_size_multiplier=port_weight,
                        ) if allow_execution else None

                        if trade is not None:
                            trade.signal_id = db_signal.id
                            db_signal.acted_on = True  # mark regardless of OPEN/REJECTED
                            # Only broadcast / notify for successfully placed orders
                            if trade.status == OrderStatus.OPEN:
                                # ── Broadcast trade to WebSocket clients ────────
                                await ws_manager.broadcast("trade", {
                                    "symbol": trade.symbol,
                                    "side": trade.side,
                                    "quantity": trade.quantity,
                                    "entry_price": trade.entry_price,
                                    "is_paper": trade.is_paper,
                                    "broker": trade.broker.value if hasattr(trade.broker, 'value') else trade.broker,
                                    "strategy_name": trade.strategy_name,
                                })
                                # ── Trade notification (with email) ─────────────
                                await _notify.trade(
                                    session,
                                    title=f"Trade Executed • {trade.side.upper()} {trade.symbol}",
                                    message=(
                                        f"{'PAPER' if trade.is_paper else 'LIVE'} order filled | "
                                        f"Qty: {trade.quantity} | Entry: ${trade.entry_price:,.4f}"
                                    ),
                                    metadata={
                                        "symbol": trade.symbol,
                                        "side": trade.side,
                                        "quantity": trade.quantity,
                                        "entry_price": trade.entry_price,
                                        "broker": trade.broker.value if hasattr(trade.broker, 'value') else trade.broker,
                                        "mode": "paper" if trade.is_paper else "live",
                                        "strategy": trade.strategy_name,
                                    },
                                )
                        await session.commit()

                        logger.info(
                            f"[signal_runner] ✓ {strat.name} | {symbol} | {sig.signal} "
                            f"@ {sig.entry_price} (conf={sig.confidence:.2f}) "
                            f"acted_on={db_signal.acted_on}"
                        )
                        # F-010: record that we processed this candle so the next
                        # Celery tick skips it (candle hasn't changed).
                        _last_candle_fired[strat.id] = _last_close_ts
                        total_run += 1

                    except Exception as e:
                        await session.rollback()
                        logger.error(
                            f"[signal_runner] ✗ Strategy id={strat.id} name='{strat.name}': {e}",
                            exc_info=True,
                        )
                        total_errors += 1

            logger.info(
                f"[signal_runner] Completed: {total_run} strategies run, {total_errors} errors."
            )

        asyncio.run(_run())

    except Exception as exc:
        logger.error(f"[signal_runner] Task-level failure: {exc}", exc_info=True)
        raise self.retry(exc=exc, countdown=60)
