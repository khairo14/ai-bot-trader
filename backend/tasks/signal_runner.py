"""Tasks: automated signal runner via Celery."""
from celery_app import celery_app
import logging

logger = logging.getLogger(__name__)


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

                        # ── Pipe through ForwardEngine ───────────────────────────
                        trade = await forward_engine.process_signal(
                            signal=sig,
                            execution_mode=strat.execution_mode.value,
                            is_paper=strat.is_paper,
                            db_session=session,
                        )

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
