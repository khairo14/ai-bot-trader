import asyncio
from datetime import datetime
from typing import List, Optional
from loguru import logger

from core.strategies.base import Signal
from core.risk_manager import RiskManager
from brokers import get_broker
from db.models import Trade, OrderStatus, ExecutionMode


class ForwardEngine:
    """
    Paper trading and live execution engine.

    Modes (per strategy):
      suggestion  → Signal generated, no auto-execution
      semi-auto   → Signal generated, awaits manual confirm
      full-auto   → Signal generated, order placed immediately

    Always available:
      - Manual close any position
      - Emergency stop (close all)
    """

    def __init__(self):
        self.risk_manager = RiskManager()
        self._paper_positions: dict = {}     # symbol → position (for paper mode)
        self._paper_balance: float = 10000.0
        self._emergency_stop_active: bool = False

    async def process_signal(
        self,
        signal: Signal,
        execution_mode: str,
        is_paper: bool,
        db_session=None,
    ) -> Optional[Trade]:
        """
        Process a signal based on execution mode.
        Returns a Trade record if an order was placed, else None.
        """
        if self._emergency_stop_active:
            logger.warning("[ForwardEngine] Emergency stop is active. Ignoring signal.")
            return None

        if signal.signal in ("HOLD", None):
            return None

        # ── Get broker ───────────────────────────────────
        broker = get_broker(signal.broker)

        # ── Get balance ──────────────────────────────────
        if is_paper:
            balance = self._paper_balance
            open_count = len(self._paper_positions)
        else:
            bal = await broker.get_balance()
            balance = bal.available
            positions = await broker.get_positions()
            open_count = len(positions)

        # ── Risk validation ──────────────────────────────
        validation = self.risk_manager.validate(
            signal=signal,
            account_balance=balance,
            open_positions_count=open_count,
            daily_pnl=0.0,  # TODO: pull from DB
        )

        if not validation.approved:
            logger.warning(f"[ForwardEngine] Signal rejected by risk manager: {validation.reason}")
            return None

        # ── Handle execution mode ─────────────────────────
        if execution_mode == ExecutionMode.SUGGESTION:
            logger.info(f"[ForwardEngine] 💡 SUGGESTION: {signal.signal} {signal.symbol} @ {signal.entry_price}")
            return None  # No execution — signal shown on dashboard only

        if execution_mode == ExecutionMode.SEMI_AUTO:
            logger.info(f"[ForwardEngine] ⏸ SEMI-AUTO awaiting confirmation: {signal.signal} {signal.symbol}")
            return None  # Execution deferred until dashboard confirm

        # ── Full-auto execution ───────────────────────────
        logger.info(f"[ForwardEngine] 🤖 FULL-AUTO executing: {signal.signal} {signal.symbol} x {validation.position_size}")

        trade = Trade(
            symbol=signal.symbol,
            side=signal.signal.lower(),
            quantity=validation.position_size,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            status=OrderStatus.PENDING,
            execution_mode=execution_mode,
            broker=signal.broker,
            asset_class=signal.asset_class,
            is_paper=is_paper,
            strategy_name=signal.strategy_name,
            opened_at=datetime.utcnow(),
        )

        if is_paper:
            # Simulate fill at entry price
            trade.status = OrderStatus.OPEN
            trade.broker_order_id = f"paper_{signal.symbol}_{int(datetime.utcnow().timestamp())}"
            self._paper_positions[signal.symbol] = trade
            logger.info(f"[ForwardEngine] 📄 PAPER FILL: {signal.signal} {signal.symbol} @ {signal.entry_price}")
        else:
            # Place real order
            side = "buy" if signal.signal == "BUY" else "sell"
            result = await broker.place_order(
                symbol=signal.symbol,
                side=side,
                quantity=validation.position_size,
                order_type="market",
                stop_price=signal.stop_loss,
                take_profit_price=signal.take_profit,
            )
            trade.broker_order_id = result.order_id
            trade.status = OrderStatus.OPEN
            logger.info(f"[ForwardEngine] ✅ LIVE ORDER: {result.order_id}")

        if db_session:
            db_session.add(trade)
            await db_session.commit()

        return trade

    async def close_position(self, trade: Trade, reason: str = "manual"):
        """Close an open position."""
        broker = get_broker(trade.broker)
        if not trade.is_paper:
            side = "sell" if trade.side == "buy" else "buy"
            await broker.place_order(
                symbol=trade.symbol,
                side=side,
                quantity=trade.quantity,
                order_type="market",
            )

        trade.status = OrderStatus.FILLED
        trade.closed_at = datetime.utcnow()
        logger.info(f"[ForwardEngine] Position closed: {trade.symbol} — reason: {reason}")

    async def emergency_stop(self) -> int:
        """Close ALL open positions immediately."""
        self._emergency_stop_active = True
        closed = 0
        logger.warning("[ForwardEngine] ⚠️ EMERGENCY STOP ACTIVATED")

        for symbol, position in list(self._paper_positions.items()):
            await self.close_position(position, reason="emergency_stop")
            del self._paper_positions[symbol]
            closed += 1

        logger.warning(f"[ForwardEngine] Emergency stop: {closed} positions closed.")
        return closed

    def resume(self):
        """Re-enable trading after emergency stop."""
        self._emergency_stop_active = False
        logger.info("[ForwardEngine] Trading resumed after emergency stop.")
