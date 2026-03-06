import asyncio
from datetime import datetime, timezone
from typing import Optional
from loguru import logger
from sqlalchemy import select, func

from core.strategies.base import Signal
from core.risk_manager import RiskManager
from brokers import get_broker
from db.models import Trade, OrderStatus, ExecutionMode

PAPER_INITIAL_CAPITAL = 10_000.0  # kept for legacy DB migration reference only


class ForwardEngine:
    """
    Paper trading and live execution engine.

    Paper mode: routes orders through each broker's paper/testnet API using
    the paper credentials configured in .env.  This means:
      - Alpaca  → paper-api.alpaca.markets   (paper account)
      - Binance → testnet.binance.vision     (testnet account)
      - IBKR    → TWS paper gateway          (paper account)
    Trades appear in the broker's own dashboard just like live trades.

    Modes (per strategy):
      suggestion  → Signal generated, no auto-execution
      semi-auto   → Signal generated, awaits manual confirm
      full-auto   → Signal generated, order placed immediately

    Always available:
      - Manual close any position
      - Emergency stop (close all)

    Call `await engine.initialize(db_session)` once per run cycle so that
    in-memory state is always consistent with the database.
    """

    def __init__(self):
        self.risk_manager = RiskManager()
        self._paper_positions: dict = {}     # symbol → Trade (open positions, any mode)
        self._paper_balance: float = PAPER_INITIAL_CAPITAL  # fallback if broker unreachable
        self._emergency_stop_active: bool = False
        self._initialized: bool = False

    # ──────────────────────────────────────────────────────────────────────────
    # State hydration from DB
    # ──────────────────────────────────────────────────────────────────────────

    async def initialize(self, db_session) -> None:
        """
        Reload in-memory state from the database.  Must be awaited before
        processing any signal so that positions and balance reflect reality
        across server restarts.
        """
        # Load open paper trades into _paper_positions
        open_q = await db_session.execute(
            select(Trade).where(
                Trade.is_paper == True,
                Trade.status == OrderStatus.OPEN,
            )
        )
        open_trades = open_q.scalars().all()
        self._paper_positions = {t.symbol: t for t in open_trades}

        # Re-compute paper balance from realised P&L
        pnl_q = await db_session.execute(
            select(func.coalesce(func.sum(Trade.pnl), 0.0)).where(
                Trade.is_paper == True,
                Trade.status == OrderStatus.FILLED,
            )
        )
        realised_pnl: float = pnl_q.scalar_one()
        self._paper_balance = PAPER_INITIAL_CAPITAL + realised_pnl

        self._initialized = True
        logger.info(
            f"[ForwardEngine] Hydrated: {len(self._paper_positions)} open paper positions, "
            f"paper balance=${self._paper_balance:,.2f}"
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    async def _daily_pnl(db_session) -> float:
        """Return the sum of realised P&L for trades closed today (UTC)."""
        today_start = datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        q = await db_session.execute(
            select(func.coalesce(func.sum(Trade.pnl), 0.0)).where(
                Trade.status == OrderStatus.FILLED,
                Trade.closed_at >= today_start,
            )
        )
        return q.scalar_one()

    # ──────────────────────────────────────────────────────────────────────────
    # Core signal processing
    # ──────────────────────────────────────────────────────────────────────────

    async def process_signal(
        self,
        signal: Signal,
        execution_mode: str,
        is_paper: bool,
        db_session=None,
        position_size_multiplier: float = 1.0,
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
        await broker.connect()   # no-op for Binance/Alpaca; ensures IBKR singleton is live

        # ── Get balance + open count ──────────────────────
        # Both paper and live use the real broker API — paper broker instances
        # are already configured with paper/testnet credentials by get_broker().
        try:
            bal = await broker.get_balance()
            balance = bal.available
        except Exception as _bal_err:
            logger.warning(f"[ForwardEngine] Could not fetch balance from {signal.broker}: {_bal_err} — using fallback")
            balance = self._paper_balance  # fallback: last known in-memory value
        try:
            positions = await broker.get_positions()
            open_count = len(positions)
        except Exception:
            open_count = len(self._paper_positions)  # fallback

        # ── Daily P&L from DB (for circuit breaker) ──────
        daily_pnl = 0.0
        if db_session is not None:
            try:
                daily_pnl = await self._daily_pnl(db_session)
            except Exception as exc:
                logger.warning(f"[ForwardEngine] Could not compute daily_pnl: {exc}")

        # ── Risk validation ──────────────────────────────
        validation = self.risk_manager.validate(
            signal=signal,
            account_balance=balance,
            open_positions_count=open_count,
            daily_pnl=daily_pnl,
        )

        if not validation.approved:
            logger.warning(f"[ForwardEngine] Signal rejected by risk manager: {validation.reason}")
            return None
        # ── Apply portfolio weight multiplier ─────────────────────────────
        # ML-03: strategies with higher Sharpe weight get proportionally larger size
        effective_size = round(validation.position_size * position_size_multiplier, 6)
        effective_size = max(effective_size, 1e-8)  # never zero
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
            quantity=effective_size,
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

        # ── Broker API order (paper OR live) ────────────────────────────────────
        # Paper strategies use paper/testnet credentials via get_broker(), so
        # the order appears in the broker's own paper trading dashboard (Alpaca
        # paper portal, Binance testnet, IBKR paper TWS).  Live strategies use
        # live credentials.  The routing is already handled by get_broker().
        #
        # Brokers reject orders during:
        #   • Market-wide circuit breakers (NYSE L1/L2/L3 halts — 7%/13%/20% drops)
        #   • Individual stock LULD (Limit-Up Limit-Down) halts
        #   • Exchange emergency halts / forced closures
        #   • Crypto exchange maintenance windows
        # We catch all broker errors here so the scheduler never crashes.
        # A FAILED trade record is written to the DB so you have a full audit trail.
        # BUY  → buy (open long)
        # SELL → sell (close long or open short for equity/crypto)
        # SHORT → sell (open short position)
        # COVER → buy (buy-to-cover; close an open short position)
        side = "buy" if signal.signal in ("BUY", "COVER") else "sell"
        # ── Options extra kwargs (single-leg only; multi-leg is paper-only) ──
        option_kwargs: dict = {}
        meta = getattr(signal, "options_meta", None)
        if meta and isinstance(meta, dict):
            legs = meta.get("legs", [])
            if len(legs) == 1:
                option_kwargs = {
                    "option_expiry": meta.get("expiry"),
                    "option_strike": legs[0].get("strike"),
                    "option_right":  legs[0].get("right"),
                }
            elif len(legs) > 1:
                logger.warning(
                    f"[ForwardEngine] Multi-leg option ({meta.get('strategy_type', '?')}) for "
                    f"{signal.symbol} — live execution not supported; switch to paper mode."
                )
        mode_tag = "PAPER" if is_paper else "LIVE"
        try:
            result = await broker.place_order(
                symbol=signal.symbol,
                side=side,
                quantity=effective_size,
                order_type="market",
                stop_price=signal.stop_loss,
                take_profit_price=signal.take_profit,
                **option_kwargs,
            )
            trade.broker_order_id = result.order_id
            trade.status = OrderStatus.OPEN
            self._paper_positions[signal.symbol] = trade  # track in-memory too
            logger.info(f"[ForwardEngine] ✅ {mode_tag} ORDER PLACED: {result.order_id} | {signal.signal} {signal.symbol} @ {signal.entry_price} qty={effective_size}")
        except Exception as order_err:
            # Broker rejected or is unreachable — record FAILED trade for audit
            trade.status = OrderStatus.REJECTED
            trade.broker_order_id = f"rejected_{signal.symbol}_{int(datetime.utcnow().timestamp())}"
            trade.notes = f"Order rejected: {order_err}"
            logger.warning(
                f"[ForwardEngine] ⚠️  {mode_tag} ORDER REJECTED for {signal.symbol}: {order_err}. "
                f"Likely causes: market halt, circuit breaker, exchange maintenance, "
                f"insufficient funds, or invalid symbol. Trade saved as FAILED."
            )
            # Return the rejected Trade so callers can set signal_id and acted_on=True.
            # This prevents the same signal from being re-attempted on the next run.
            if db_session:
                db_session.add(trade)
                await db_session.commit()
                await db_session.refresh(trade)
            return trade

        if db_session:
            db_session.add(trade)
            await db_session.commit()
            await db_session.refresh(trade)

        # ── Broadcast trade event ─────────────────────────────
        try:
            from api.websocket import manager as ws_manager
            await ws_manager.broadcast("trade", {
                "symbol": trade.symbol,
                "side": trade.side,
                "quantity": trade.quantity,
                "entry_price": trade.entry_price,
                "is_paper": trade.is_paper,
                "broker": trade.broker.value if hasattr(trade.broker, "value") else trade.broker,
                "strategy_name": trade.strategy_name,
            })
        except Exception as _ws_err:
            logger.warning(f"[ForwardEngine] WS broadcast failed: {_ws_err}")

        return trade

    # ──────────────────────────────────────────────────────────────────────────
    # Position management
    # ──────────────────────────────────────────────────────────────────────────

    async def close_position(self, trade: Trade, reason: str = "manual"):
        """Close an open position and compute exit price + realised PnL."""
        broker = get_broker(trade.broker)
        try:
            await broker.connect()   # no-op for Binance/Alpaca; ensures IBKR singleton is live
        except Exception as _conn_err:
            logger.warning(f"[ForwardEngine] Broker connect failed before close: {_conn_err}")

        # ── Fetch current market price (live and paper) ───────────────────
        exit_price: float = 0.0
        try:
            exit_price = await broker.get_price(trade.symbol)
        except Exception as _price_err:
            logger.warning(
                f"[ForwardEngine] Could not fetch exit price for {trade.symbol}: {_price_err}"
            )

        # ── Send closing market order (paper and live both call broker API) ─────
        side = "sell" if trade.side == "buy" else "buy"
        try:
            await broker.place_order(
                symbol=trade.symbol,
                side=side,
                quantity=trade.quantity,
                order_type="market",
            )
        except Exception as _close_err:
            logger.warning(f"[ForwardEngine] Could not place closing order for {trade.symbol}: {_close_err}")

        # ── Compute realised PnL ──────────────────────────────────────────
        if exit_price and trade.entry_price:
            side_mult = 1.0 if trade.side == "buy" else -1.0
            raw_pnl = (exit_price - trade.entry_price) * trade.quantity * side_mult
            trade.exit_price = round(exit_price, 8)
            trade.pnl = round(raw_pnl, 4)
            cost_basis = trade.entry_price * trade.quantity
            trade.pnl_pct = round(raw_pnl / cost_basis * 100, 4) if cost_basis else 0.0

        trade.status = OrderStatus.FILLED
        trade.closed_at = datetime.utcnow()
        # Remove from in-memory cache
        self._paper_positions.pop(trade.symbol, None)

        # ── Update consecutive-loss counter ───────────────────────────────
        if trade.pnl is not None:
            self.risk_manager.record_outcome(won=trade.pnl > 0)

        logger.info(
            f"[ForwardEngine] Position closed: {trade.symbol} — reason: {reason} "
            f"| exit={exit_price} pnl={getattr(trade, 'pnl', None)}"
        )

    async def emergency_stop(self, db_session=None) -> int:
        """
        Close ALL open positions immediately.
        Queries the DB so it catches positions that survived a restart,
        not just the ones currently in _paper_positions.
        """
        self._emergency_stop_active = True
        closed = 0
        logger.warning("[ForwardEngine] ⚠️ EMERGENCY STOP ACTIVATED")

        if db_session is not None:
            # DB-authoritative: close every open trade regardless of in-memory state
            open_q = await db_session.execute(
                select(Trade).where(Trade.status == OrderStatus.OPEN)
            )
            all_open = open_q.scalars().all()
            for trade in all_open:
                await self.close_position(trade, reason="emergency_stop")
                closed += 1
            await db_session.commit()
        else:
            # Fallback: in-memory only (should not happen in normal usage)
            for symbol, trade in list(self._paper_positions.items()):
                await self.close_position(trade, reason="emergency_stop")
                closed += 1

        self._paper_positions.clear()
        logger.warning(f"[ForwardEngine] Emergency stop: {closed} positions closed.")
        return closed

    def resume(self):
        """Re-enable trading after emergency stop."""
        self._emergency_stop_active = False
        logger.info("[ForwardEngine] Trading resumed after emergency stop.")
