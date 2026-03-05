"""Tasks: automated signal runner via Celery."""
from celery_app import celery_app
import logging

logger = logging.getLogger(__name__)

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
# Minimum fraction of timeframes (including primary) that must agree to allow execution
MIN_CONFLUENCE = 0.5


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
                    select(StrategyModel).where(StrategyModel.is_active == True)
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
                        # If confluence < MIN_CONFLUENCE, suppress execution but
                        # still save the signal (visible on Dashboard as low-conf).
                        allow_execution = True
                        conf = 1.0
                        if sig.signal in _TRACKABLE_SIGNALS:
                            conf = await _confluence_score(
                                signal_engine, strategy_type, symbol,
                                strat.broker.value, timeframe, sig.signal,
                            )
                            if conf < MIN_CONFLUENCE:
                                allow_execution = False
                                sig.reasons = (sig.reasons or []) + [
                                    f"execution suppressed: low multi-TF confluence ({conf:.0%})"
                                ]
                                logger.info(
                                    f"[signal_runner] ⚠ Low confluence {conf:.0%} for "
                                    f"{sig.signal} {symbol} on {timeframe} — not executing"
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
                            db_signal.acted_on = True
                            # ── Broadcast trade to WebSocket clients ──────────
                            await ws_manager.broadcast("trade", {
                                "symbol": trade.symbol,
                                "side": trade.side,
                                "quantity": trade.quantity,
                                "entry_price": trade.entry_price,
                                "is_paper": trade.is_paper,
                                "broker": trade.broker.value if hasattr(trade.broker, 'value') else trade.broker,
                                "strategy_name": trade.strategy_name,
                            })                            # ── Trade notification (with email) ──────────────
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
