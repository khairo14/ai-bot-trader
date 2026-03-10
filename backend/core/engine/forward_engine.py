import asyncio
from datetime import datetime, timezone
from typing import Optional
from loguru import logger
from sqlalchemy import select, func

from core.strategies.base import Signal
from core.risk_manager import RiskManager
from brokers import get_broker
from db.models import Trade, OrderStatus, ExecutionMode
from config import settings as _cfg

# Convenience alias used by tests and external code
PAPER_INITIAL_CAPITAL: float = _cfg.paper_initial_balance

# IMP-3: prevent concurrent monitor_sl_tp calls within the same FastAPI process.
# Each ForwardEngine instance (heartbeat, signal_runner) gets its own lock; since
# they run on the same event loop (FastAPI process) this stops double-ratcheting.
# Cross-process (Celery vs FastAPI) deduplication is handled by the DB advisory lock.
_MONITOR_SL_TP_LOCK: asyncio.Lock = asyncio.Lock()


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
        self._paper_balance: dict[str, float] = {}   # broker_name → paper balance (F-028)
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
        # Load open and pending paper trades into _paper_positions.
        # PENDING = order submitted but fill not yet confirmed (e.g. after a restart).
        # Including PENDING here prevents the scheduler from opening a second order
        # on the same symbol before the first one is confirmed or rejected (F-069).
        open_q = await db_session.execute(
            select(Trade).where(
                Trade.is_paper == True,
                Trade.status.in_([OrderStatus.OPEN, OrderStatus.PENDING]),
            )
        )
        open_trades = open_q.scalars().all()
        self._paper_positions = {t.symbol: t for t in open_trades}

        # Re-compute paper balance per broker from realised P&L (F-028)
        from sqlalchemy import distinct
        broker_q = await db_session.execute(
            select(distinct(Trade.broker)).where(Trade.is_paper == True)
        )
        brokers = [row[0] for row in broker_q.all()]
        self._paper_balance = {}
        for broker_val in brokers:
            pnl_q = await db_session.execute(
                select(func.coalesce(func.sum(Trade.pnl), 0.0)).where(
                    Trade.is_paper == True,
                    Trade.status == OrderStatus.FILLED,
                    Trade.broker == broker_val,
                )
            )
            realised_pnl: float = pnl_q.scalar_one()
            key = broker_val.value if hasattr(broker_val, 'value') else str(broker_val)
            self._paper_balance[key] = _cfg.paper_initial_balance + realised_pnl

        self._initialized = True
        logger.info(
            f"[ForwardEngine] Hydrated: {len(self._paper_positions)} open paper positions, "
            f"per-broker paper balances: { {k: f'${v:,.2f}' for k, v in self._paper_balance.items()} }"
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    async def _daily_pnl(db_session, broker: str | None = None) -> float:
        """Return today's realised + unrealized P&L, optionally filtered to one broker."""
        from datetime import timezone as _tz
        today_start = datetime.now(_tz.utc).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None
        )
        # Build optional broker filter
        broker_filter: list = []
        if broker:
            try:
                from db.models import BrokerName as _BN
                broker_filter = [Trade.broker == _BN(broker)]
            except ValueError:
                pass

        q_realised = await db_session.execute(
            select(func.coalesce(func.sum(Trade.pnl), 0.0)).where(
                Trade.status == OrderStatus.FILLED,
                Trade.closed_at >= today_start,
                *broker_filter,
            )
        )
        realised: float = q_realised.scalar_one()
        q_open = await db_session.execute(
            select(func.coalesce(func.sum(Trade.pnl), 0.0)).where(
                Trade.status == OrderStatus.OPEN,
                Trade.pnl.isnot(None),
                *broker_filter,
            )
        )
        unrealized: float = q_open.scalar_one()
        return realised + unrealized

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
        strategy_params: dict | None = None,
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
            broker_key = signal.broker.value if hasattr(signal.broker, 'value') else str(signal.broker)
            balance = self._paper_balance.get(broker_key, _cfg.paper_initial_balance)
        # Count open positions for this broker from the DB (authoritative source).
        # broker.get_positions() returns ALL non-zero exchange balances which
        # includes pre-funded testnet assets unrelated to bot trades, causing false
        # "Max open positions" rejections across brokers.  When no DB session is
        # available, fall back to in-memory paper positions filtered by broker.
        broker_key = signal.broker.value if hasattr(signal.broker, 'value') else str(signal.broker)
        if db_session is not None:
            try:
                _open_q = await db_session.execute(
                    select(func.count()).where(
                        Trade.status == OrderStatus.OPEN,
                        Trade.broker == signal.broker,
                    )
                )
                open_count = int(_open_q.scalar_one() or 0)
            except Exception as _cnt_err:
                logger.debug(f"[ForwardEngine] Could not count open positions from DB: {_cnt_err}")
                open_count = sum(
                    1 for t in self._paper_positions.values()
                    if (t.broker.value if hasattr(t.broker, 'value') else str(t.broker)) == broker_key
                )
        else:
            open_count = sum(
                1 for t in self._paper_positions.values()
                if (t.broker.value if hasattr(t.broker, 'value') else str(t.broker)) == broker_key
            )

        # F-083: Compute asset-class exposure so RiskManager Level 3
        # (max_exposure_per_class_pct) actually fires.  Previously always 0.0.
        asset_class_exposure = 0.0
        if db_session is not None:
            try:
                _exp_q = await db_session.execute(
                    select(func.coalesce(func.sum(Trade.quantity * Trade.entry_price), 0.0)).where(
                        Trade.status == OrderStatus.OPEN,
                        Trade.asset_class == signal.asset_class,
                        Trade.broker == signal.broker,
                    )
                )
                asset_class_exposure = float(_exp_q.scalar_one() or 0.0)
            except Exception as _exp_err:
                logger.debug(f"[ForwardEngine] Could not compute asset_class_exposure: {_exp_err}")

        # ── Load per-broker risk settings from DB ────────────────
        broker_settings: dict | None = None
        if db_session is not None:
            try:
                from sqlalchemy import select as _sel
                from db.models import BrokerRiskSettings, BrokerName as _BN
                _bs_q = await db_session.execute(
                    _sel(BrokerRiskSettings).where(BrokerRiskSettings.broker == _BN(broker_key))
                )
                _bs_row = _bs_q.scalar_one_or_none()
                if _bs_row is not None:
                    broker_settings = {
                        "risk_per_trade_pct":       _bs_row.risk_per_trade_pct,
                        "max_open_positions":        _bs_row.max_open_positions,
                        "daily_circuit_breaker_pct": _bs_row.daily_circuit_breaker_pct,
                        "max_consecutive_losses":    _bs_row.max_consecutive_losses,
                        "max_exposure_per_asset_pct":_bs_row.max_exposure_per_asset_pct,
                        "max_exposure_per_class_pct":_bs_row.max_exposure_per_class_pct,
                    }
            except Exception as _bs_err:
                logger.debug(f"[ForwardEngine] Could not load broker settings for {broker_key}: {_bs_err}")

        # ── Daily P&L from DB filtered to this broker (for circuit breaker) ──
        daily_pnl = 0.0
        if db_session is not None:
            try:
                daily_pnl = await self._daily_pnl(db_session, broker=broker_key)
            except Exception as exc:
                logger.warning(f"[ForwardEngine] Could not compute daily_pnl: {exc}")

        # ── Risk validation ──────────────────
        validation = self.risk_manager.validate(
            signal=signal,
            account_balance=balance,
            open_positions_count=open_count,
            daily_pnl=daily_pnl,
            asset_class_exposure=asset_class_exposure,
            broker=broker_key,
            broker_settings=broker_settings,
        )

        if not validation.approved:
            logger.warning(f"[ForwardEngine] Signal rejected by risk manager: {validation.reason}")
            if db_session:
                try:
                    from notifications.notifier import notifier as _notifier
                    _mode_tag_risk = "PAPER" if is_paper else "LIVE"
                    await _notifier.warning(
                        db_session,
                        title=f"🛡 [{_mode_tag_risk}] Signal Blocked — {signal.symbol}",
                        message=(
                            f"{signal.signal} {signal.symbol} blocked by risk manager.\n"
                            f"Reason: {validation.reason}"
                        ),
                        metadata={
                            "symbol": signal.symbol,
                            "side": signal.signal,
                            "broker": broker_key,
                            "is_paper": is_paper,
                            "strategy": signal.strategy_name,
                            "reason": validation.reason,
                        },
                    )
                    await db_session.commit()
                except Exception as _n_err:
                    logger.debug(f"[ForwardEngine] Risk-reject notification failed: {_n_err}")
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

        # ── Close any existing opposing position before opening a new one ────────
        # Prevents holding simultaneous long + short on the same symbol.
        # e.g. open BUY + new SHORT signal → close the BUY first, then open SHORT.
        # NOTE: "long" is an alias for "buy" used by the DB rebuild script.
        _long_sides = {"buy", "cover", "long"}
        _new_is_long = signal.signal.upper() in ("BUY", "COVER")
        if db_session is not None:
            _existing_q = await db_session.execute(
                select(Trade).where(
                    Trade.symbol == signal.symbol,
                    Trade.broker == signal.broker,
                    Trade.is_paper == is_paper,
                    Trade.status == OrderStatus.OPEN,
                )
            )
            _existing_trades = _existing_q.scalars().all()
            for _existing in _existing_trades:
                _existing_is_long = _existing.side in _long_sides
                if _new_is_long != _existing_is_long:
                    logger.info(
                        f"[ForwardEngine] Reversing position: closing {_existing.side.upper()} "
                        f"{_existing.symbol} (id={_existing.id}) before opening {signal.signal}"
                    )
                    try:
                        await self.close_position(_existing, reason="signal_reversal", db_session=db_session)
                        await db_session.commit()
                    except Exception as _rev_err:
                        logger.error(
                            f"[ForwardEngine] Failed to close opposing position for "
                            f"{_existing.symbol} id={_existing.id}: {_rev_err}"
                        )
                else:
                    # G1: same-direction position already OPEN — block pyramiding unless
                    # ML confidence is high enough to justify a fresh re-entry.
                    # g1_override_min_confidence in strategy params opts-in to this;
                    # when confidence >= threshold, allow the new entry trusting the model's
                    # own SL/TP fully (no artificial tightening — we trust the model).
                    _g1_threshold = None
                    if strategy_params:
                        try:
                            _g1_threshold = float(strategy_params["g1_override_min_confidence"])
                        except (KeyError, TypeError, ValueError):
                            pass
                    if (
                        _g1_threshold is not None
                        and signal.confidence is not None
                        and signal.confidence >= _g1_threshold
                    ):
                        logger.info(
                            f"[ForwardEngine] G1 overridden by ML confidence "
                            f"({signal.confidence:.2f} >= {_g1_threshold}): allowing re-entry "
                            f"{signal.signal} {signal.symbol} alongside id={_existing.id}"
                        )
                        # Fall through — new entry proceeds with signal's own SL/TP
                    else:
                        logger.info(
                            f"[ForwardEngine] G1: Same-direction duplicate blocked: "
                            f"{signal.signal} {signal.symbol} — already {_existing.side.upper()} id={_existing.id}"
                        )
                        return None
        elif signal.symbol in self._paper_positions:
            _existing = self._paper_positions[signal.symbol]
            _existing_is_long = _existing.side in _long_sides
            if _new_is_long != _existing_is_long:
                logger.info(
                    f"[ForwardEngine] Reversing position (in-memory): closing {_existing.side.upper()} "
                    f"{_existing.symbol} before opening {signal.signal}"
                )
                try:
                    await self.close_position(_existing, reason="signal_reversal")
                except Exception as _rev_err:
                    logger.error(
                        f"[ForwardEngine] Failed to close opposing position for {_existing.symbol}: {_rev_err}"
                    )
            else:
                # G1 in-memory path — same override logic
                _g1_threshold_mem = None
                if strategy_params:
                    try:
                        _g1_threshold_mem = float(strategy_params["g1_override_min_confidence"])
                    except (KeyError, TypeError, ValueError):
                        pass
                if (
                    _g1_threshold_mem is not None
                    and signal.confidence is not None
                    and signal.confidence >= _g1_threshold_mem
                ):
                    logger.info(
                        f"[ForwardEngine] G1 overridden by ML confidence "
                        f"({signal.confidence:.2f} >= {_g1_threshold_mem}): allowing re-entry "
                        f"{signal.signal} {signal.symbol} (in-memory)"
                    )
                else:
                    logger.info(
                        f"[ForwardEngine] G1: Same-direction duplicate blocked (in-memory): "
                        f"{signal.signal} {signal.symbol} — already {_existing.side.upper()}"
                    )
                    return None

        # G5: IBKR forex minimum lot size pre-check (25,000 base currency units)
        _IBKR_FOREX_MIN_LOT = 25_000.0
        _broker_key_g5 = signal.broker.value if hasattr(signal.broker, 'value') else str(signal.broker)
        _asset_cls_g5 = getattr(signal.asset_class, 'value', str(signal.asset_class or '')).upper()
        if _broker_key_g5 == 'ibkr' and _asset_cls_g5 == 'FOREX' and effective_size < _IBKR_FOREX_MIN_LOT:
            logger.warning(
                f"[ForwardEngine] G5: IBKR forex lot {effective_size:.0f} < minimum {_IBKR_FOREX_MIN_LOT:.0f} "
                f"for {signal.symbol} — order rejected to avoid IBKR rejection"
            )
            return None

        trade = Trade(
            symbol=signal.symbol,
            side=signal.signal.lower(),
            quantity=effective_size,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            trailing_stop_pct=getattr(signal, "trailing_stop_pct", None),
            status=OrderStatus.PENDING,
            execution_mode=execution_mode,
            broker=signal.broker,
            asset_class=signal.asset_class,
            is_paper=is_paper,
            strategy_name=signal.strategy_name,
            opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
            user_id=getattr(signal, "user_id", None),  # I9: propagate user for audit trail
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
                entry_price=signal.entry_price,  # used by Alpaca to reanchor stale SL/TP
                **option_kwargs,
            )
            # Use broker-confirmed fill price if available, fall back to signal price
            confirmed_entry = result.fill_price or result.price or signal.entry_price
            if result.fill_price:
                logger.info(
                    f"[ForwardEngine] ✅ {mode_tag} ORDER FILLED: {result.order_id} | "
                    f"{signal.signal} {signal.symbol} @ {confirmed_entry} (confirmed) qty={effective_size}"
                )
            else:
                # Broker accepted but fill not confirmed within poll timeout
                # — mark as PENDING so it doesn't appear as a ghost open position
                logger.warning(
                    f"[ForwardEngine] ⏳ {mode_tag} ORDER PENDING (no fill confirmation): "
                    f"{result.order_id} | {signal.symbol} — marking PENDING, will stay out of positions"
                )
            trade.broker_order_id = result.order_id
            trade.entry_price = round(confirmed_entry, 8)
            trade.status = OrderStatus.OPEN if result.fill_price else OrderStatus.PENDING
            if result.fill_price:
                self._paper_positions[signal.symbol] = trade  # only track confirmed fills
        except Exception as order_err:
            # Broker rejected or is unreachable — record FAILED trade for audit
            trade.status = OrderStatus.REJECTED
            trade.broker_order_id = f"rejected_{signal.symbol}_{int(datetime.now(timezone.utc).timestamp())}"
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
                try:
                    from notifications.notifier import notifier as _notifier
                    await _notifier.warning(
                        db_session,
                        title=f"❌ [{mode_tag}] Order Rejected — {signal.symbol}",
                        message=(
                            f"{signal.signal} {signal.symbol} @ {signal.entry_price} rejected.\n"
                            f"Reason: {order_err}"
                        ),
                        metadata={
                            "symbol": signal.symbol,
                            "side": signal.signal,
                            "broker": broker_key,
                            "is_paper": is_paper,
                            "strategy": signal.strategy_name,
                            "reason": str(order_err),
                        },
                    )
                    await db_session.commit()
                except Exception as _n_err:
                    logger.debug(f"[ForwardEngine] Rejection notification failed: {_n_err}")
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

        # ── In-app notification ───────────────────────────────
        if db_session:
            try:
                from notifications.notifier import notifier as _notifier
                _broker_str = trade.broker.value if hasattr(trade.broker, "value") else str(trade.broker)
                if trade.status == OrderStatus.OPEN:
                    await _notifier.trade(
                        db_session,
                        title=f"✅ [{mode_tag}] Trade Filled — {trade.symbol}",
                        message=(
                            f"{trade.side.upper()} {trade.quantity:.4f} {trade.symbol} "
                            f"@ {trade.entry_price} on {_broker_str}."
                            + (f"  SL: {trade.stop_loss}  TP: {trade.take_profit}" if trade.stop_loss else "")
                        ),
                        metadata={
                            "symbol": trade.symbol,
                            "side": trade.side,
                            "quantity": trade.quantity,
                            "entry_price": trade.entry_price,
                            "stop_loss": trade.stop_loss,
                            "take_profit": trade.take_profit,
                            "broker": _broker_str,
                            "is_paper": trade.is_paper,
                            "strategy": trade.strategy_name,
                            "trade_id": trade.id,
                        },
                    )
                else:  # PENDING
                    await _notifier.warning(
                        db_session,
                        title=f"⏳ [{mode_tag}] Order Pending — {trade.symbol}",
                        message=(
                            f"{trade.side.upper()} {trade.quantity:.4f} {trade.symbol} "
                            f"submitted to {_broker_str} but fill not confirmed yet."
                        ),
                        metadata={
                            "symbol": trade.symbol,
                            "side": trade.side,
                            "broker": _broker_str,
                            "is_paper": trade.is_paper,
                            "strategy": trade.strategy_name,
                            "order_id": trade.broker_order_id,
                        },
                    )
                await db_session.commit()
            except Exception as _n_err:
                logger.debug(f"[ForwardEngine] Trade notification failed: {_n_err}")

        return trade

    # ──────────────────────────────────────────────────────────────────────────
    # Position management
    # ──────────────────────────────────────────────────────────────────────────

    async def close_position(self, trade: Trade, reason: str = "manual", db_session=None):
        """Close an open position and compute exit price + realised PnL."""
        # ── Atomic DB guard: prevent double-close from concurrent callers ─────
        # Each strategy spawns its own ForwardEngine() instance, so in-memory
        # sets can't protect across instances.  We need a DB-level lock instead.
        #
        # Pattern: UPDATE trade SET status='PENDING' WHERE id=X AND status='OPEN'
        # PostgreSQL row locking means only ONE concurrent session will get
        # rowcount=1; all others get rowcount=0 and bail out immediately.
        # This prevents the observed 4-5 duplicate MKT close orders per second.
        if db_session is not None and trade.id is not None:
            from sqlalchemy import update as _upd
            _guard = await db_session.execute(
                _upd(Trade)
                .where(Trade.id == trade.id, Trade.status == OrderStatus.OPEN)
                .values(status=OrderStatus.PENDING)
                .execution_options(synchronize_session="fetch")
            )
            await db_session.flush()
            if _guard.rowcount == 0:
                logger.warning(
                    f"[ForwardEngine] close_position SKIPPED for {trade.symbol} id={trade.id} "
                    f"— already closing/closed by another caller (broker bracket or concurrent monitor)"
                )
                return

        broker = get_broker(trade.broker)
        try:
            await broker.connect()   # no-op for Binance/Alpaca; ensures IBKR singleton is live
        except Exception as _conn_err:
            logger.warning(f"[ForwardEngine] Broker connect failed before close: {_conn_err}")

        # ── F-103: Verify broker holds this position before sending a market close ──
        # If the broker is already flat (qty=0 or symbol absent), a directional market
        # order creates an unintended reverse position.  Skip the order and fall through
        # to the PnL / FILLED marking below using the price snapshot.
        _FX_BASE_CLOSE = {"USD","EUR","GBP","JPY","CHF","CAD","AUD","NZD","SEK","NOK",
                          "DKK","HKD","SGD","MXN","ZAR","HUF","PLN","TRY","CZK","ILS"}
        _broker_key = trade.broker.value if hasattr(trade.broker, "value") else str(trade.broker)

        def _norm_sym_close(sym: str) -> str:
            clean = sym.replace("/", "").upper()
            if _broker_key == "ibkr" and len(clean) == 6 and clean[:3] in _FX_BASE_CLOSE and clean[3:] in _FX_BASE_CLOSE:
                return clean[:3]
            return clean if _broker_key == "alpaca" else sym

        _skip_market_order = False
        try:
            _bpos = await broker.get_positions()
            _norm_sym = _norm_sym_close(trade.symbol)
            _has_pos = any(abs(p.quantity) > 0 and p.symbol == _norm_sym for p in _bpos)
            if not _has_pos:
                logger.warning(
                    f"[ForwardEngine] F-103: {trade.symbol} id={trade.id} qty=0 at {_broker_key} "
                    f"— skipping market close order, marking FILLED at snapshot price"
                )
                _skip_market_order = True
        except Exception as _f103_err:
            logger.debug(f"[ForwardEngine] F-103 position check failed: {_f103_err} — proceeding with close order")

        # ── Fetch current market price (live and paper) ───────────────────
        exit_price: float = 0.0
        try:
            exit_price = await broker.get_price(trade.symbol)
        except Exception as _price_err:
            logger.warning(
                f"[ForwardEngine] Could not fetch exit price for {trade.symbol}: {_price_err}"
            )

        # ── Send closing market order (paper and live both call broker API) ─────
        # F-071: COVER positions are buy-to-cover (long), so they close the same
        # way as BUY positions — by selling.  Using a simple "not buy" check was
        # wrong for "cover" (resolved to "buy" instead of "sell").
        # NOTE: "long" is an alias for "buy" used by the DB rebuild script.
        _long_sides = {"buy", "cover", "long"}
        side = "sell" if trade.side in _long_sides else "buy"
        if not _skip_market_order:
            try:
                close_result = await broker.place_order(
                    symbol=trade.symbol,
                    side=side,
                    quantity=trade.quantity,
                    order_type="market",
                )
                # Use broker-confirmed fill price for PnL accuracy
                if close_result.fill_price:
                    exit_price = close_result.fill_price
                    logger.info(f"[ForwardEngine] Close order filled @ {exit_price} (confirmed)")
                elif exit_price:
                    logger.warning(
                        f"[ForwardEngine] Close order {close_result.order_id} not confirmed filled — "
                        f"using price snapshot ({exit_price}) for PnL"
                    )
                else:
                    raise RuntimeError("Close order unconfirmed and no price snapshot available")
            except RuntimeError:
                raise
            except Exception as _close_err:
                # F-103b: revert the atomic guard (PENDING→OPEN) so the next scheduler tick
                # can retry instead of leaving the trade permanently stuck as PENDING.
                if db_session is not None and trade.id is not None:
                    from sqlalchemy import update as _upd_revert
                    try:
                        await db_session.execute(
                            _upd_revert(Trade)
                            .where(Trade.id == trade.id, Trade.status == OrderStatus.PENDING)
                            .values(status=OrderStatus.OPEN)
                        )
                        await db_session.flush()
                    except Exception:
                        pass
                logger.error(
                    f"[ForwardEngine] Could not place closing order for {trade.symbol}: {_close_err} "
                    "— guard reverted to OPEN for retry next tick"
                )
                raise

        # ── Compute realised PnL ──────────────────────────────────────────
        if exit_price and trade.entry_price:
            # F-071: COVER trades are long (bought to cover a short), so they
            # profit when price rises — same sign as BUY.
            side_mult = 1.0 if trade.side in _long_sides else -1.0
            raw_pnl = (exit_price - trade.entry_price) * trade.quantity * side_mult
            trade.exit_price = round(exit_price, 8)
            trade.pnl = round(raw_pnl, 4)
            cost_basis = trade.entry_price * trade.quantity
            trade.pnl_pct = round(raw_pnl / cost_basis * 100, 4) if cost_basis else 0.0

        trade.status = OrderStatus.FILLED
        trade.closed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        # Remove from in-memory cache
        self._paper_positions.pop(trade.symbol, None)

        # F-082: cancel IBKR persistent bracket market-data subscription for this symbol.
        # For Alpaca/Binance this is a no-op (hasattr guard).
        try:
            if hasattr(broker, "cancel_bracket_subscription"):
                broker.cancel_bracket_subscription(trade.symbol)
        except Exception as _sub_err:
            logger.debug(f"[ForwardEngine] cancel_bracket_subscription failed for {trade.symbol}: {_sub_err}")

        # ── Update consecutive-loss counter (portfolio, strategy, broker) ────────
        if trade.pnl is not None:
            _broker_val = trade.broker.value if hasattr(trade.broker, 'value') else None
            self.risk_manager.record_outcome(
                won=trade.pnl > 0,
                strategy_name=getattr(trade, "strategy_name", None),
                broker=_broker_val,
            )

        logger.info(
            f"[ForwardEngine] Position closed: {trade.symbol} — reason: {reason} "
            f"| exit={exit_price} pnl={getattr(trade, 'pnl', None)}"
        )

        # ── In-app notification for closed trade ─────────────────────────
        try:
            if db_session is not None:
                from notifications.notifier import dispatch as _dispatch
                _mode_tag_close = "PAPER" if trade.is_paper else "LIVE"
                _broker_str_close = trade.broker.value if hasattr(trade.broker, "value") else str(trade.broker)
                _pnl_str = (
                    f"  P&L: {'+'if (trade.pnl or 0) >= 0 else ''}{trade.pnl:.4f} ({'+' if (trade.pnl_pct or 0) >= 0 else ''}{trade.pnl_pct:.2f}%)"
                    if trade.pnl is not None else ""
                )
                _level = "success" if (trade.pnl or 0) > 0 else "warning" if (trade.pnl or 0) == 0 else "error"
                await _dispatch(
                    db_session,
                    title=f"🏁 [{_mode_tag_close}] Trade Closed — {trade.symbol}",
                    message=(
                        f"{trade.side.upper()} {trade.quantity:.4f} {trade.symbol} "
                        f"closed @ {exit_price} on {_broker_str_close}.  Reason: {reason}.{_pnl_str}"
                    ),
                    level=_level,
                    category="trade",
                    metadata={
                        "symbol": trade.symbol,
                        "side": trade.side,
                        "entry_price": trade.entry_price,
                        "exit_price": exit_price,
                        "pnl": trade.pnl,
                        "pnl_pct": trade.pnl_pct,
                        "broker": _broker_str_close,
                        "is_paper": trade.is_paper,
                        "strategy": trade.strategy_name,
                        "reason": reason,
                        "trade_id": trade.id,
                    },
                    send_email=True,
                )
        except Exception as _n_err:
            logger.debug(f"[ForwardEngine] Close notification failed: {_n_err}")

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
            # DB-authoritative: close ALL open trades (paper and live).
            # G2: live trades are included so a single emergency stop covers everything.
            open_q = await db_session.execute(
                select(Trade).where(
                    Trade.status == OrderStatus.OPEN,
                )
            )
            all_open = open_q.scalars().all()
            for trade in all_open:
                try:
                    await self.close_position(trade, reason="emergency_stop", db_session=db_session)
                    closed += 1
                except Exception as _e:
                    logger.error(f"[ForwardEngine] Emergency stop: failed to close {trade.symbol} id={trade.id}: {_e}")
            await db_session.commit()
        else:
            # Fallback: in-memory only (should not happen in normal usage)
            for symbol, trade in list(self._paper_positions.items()):
                try:
                    await self.close_position(trade, reason="emergency_stop")
                    closed += 1
                except Exception as _e:
                    logger.error(f"[ForwardEngine] Emergency stop: failed to close {symbol}: {_e}")

        self._paper_positions.clear()
        logger.warning(f"[ForwardEngine] Emergency stop: {closed} positions closed.")
        return closed

    async def monitor_sl_tp(self, db_session) -> int:
        """
        Software-side SL/TP enforcement — runs every scheduler tick as a
        safety net independent of broker bracket orders.

        Broker bracket orders (IBKR paper/live, Alpaca OTO/bracket, Binance OCO)
        are *also* placed at entry, but they can silently fail to fire due to:
          - IBKR paper account missing active market data subscription
          - Network interruption between bracket placement and trigger
          - Broker outage or maintenance

        This method fetches the current price for every OPEN trade that has
        SL or TP set and immediately closes positions that have breached their
        level.  Uses the same `close_position()` path as manual close, so
        PnL, notifications, and risk-manager counters all fire correctly.

        Returns the number of positions closed.
        """
        # IMP-3: skip if another coroutine in the same event loop is already running
        # the monitor. The DB advisory lock inside close_position prevents double-close
        # across processes; this lock prevents redundant update_stop_loss broker calls
        # when the FastAPI heartbeat and a signal_runner coroutine overlap.
        if _MONITOR_SL_TP_LOCK.locked():
            logger.debug("[ForwardEngine] monitor_sl_tp: already running in this process, skipping.")
            return 0
        async with _MONITOR_SL_TP_LOCK:
            return await self._monitor_sl_tp_inner(db_session)

    async def _monitor_sl_tp_inner(self, db_session) -> int:
        from sqlalchemy import select as _sel
        from db.models import Strategy as _StratModel

        # Fetch ALL open trades — we need trades without SL/TP too for time-based exits.
        open_q = await db_session.execute(
            _sel(Trade).where(Trade.status == OrderStatus.OPEN)
        )
        open_trades: list = open_q.scalars().all()
        if not open_trades:
            return 0

        # Cache strategy params by strategy_name to avoid repeated DB hits.
        _strat_params_cache: dict[str, dict] = {}
        _strategy_names = {t.strategy_name for t in open_trades if t.strategy_name}
        if _strategy_names:
            _sq = await db_session.execute(
                _sel(_StratModel).where(_StratModel.name.in_(_strategy_names))
            )
            for _sr in _sq.scalars().all():
                _strat_params_cache[_sr.name] = _sr.parameters or {}

        # Helper: safe float from strategy params
        def _param_float(strategy_name: str | None, key: str) -> float | None:
            if not strategy_name:
                return None
            params = _strat_params_cache.get(strategy_name, {})
            try:
                v = params.get(key)
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        # NOTE: "long" is an alias for "buy" used by the DB rebuild script.
        _long_sides = {"buy", "cover", "long"}
        closed_count = 0
        # F-082: cache prices per (broker, symbol) to avoid redundant API calls when
        # multiple trades share the same symbol on the same broker (e.g. scaled entries).
        # F-090: cache bid/ask tuples so SL/TP checks use the directionally-correct
        # price: SHORT exits buy at the ask, LONG exits sell at the bid.
        _price_cache: dict[tuple[str, str], tuple[float, float]] = {}

        # Use streamed prices (sub-second) where available; fall back to REST.
        from core.engine.price_stream import price_stream_manager as _psm

        for trade in open_trades:
            # Skip price fetch entirely if this trade has nothing to monitor:
            # no SL, no TP, no trailing stop, and no time-based exit params.
            _has_sl_tp  = trade.stop_loss is not None or trade.take_profit is not None or trade.trailing_stop_pct is not None
            _has_time   = (
                _param_float(trade.strategy_name, "breakeven_after_hours") is not None
                or _param_float(trade.strategy_name, "max_hold_hours") is not None
            )
            if not _has_sl_tp and not _has_time:
                continue

            try:
                broker_name = trade.broker.value if hasattr(trade.broker, "value") else str(trade.broker)
                _cache_key = (broker_name, trade.symbol)
                # Always resolve the broker object (factory is cheap; it returns a singleton/cached
                # client). This ensures update_stop_loss is called on the correct broker even when
                # the bid/ask price comes from the cache (cache-hit path skips the else branch).
                broker = get_broker(broker_name)

                streamed = _psm.get_price(trade.symbol)
                if streamed is not None:
                    # Streaming mid-price available — no REST call needed.
                    # bid ≈ ask ≈ mid for liquid assets; acceptable for SL/TP trigger.
                    bid_price, ask_price = streamed, streamed
                    _price_cache[_cache_key] = (bid_price, ask_price)
                elif _cache_key in _price_cache:
                    bid_price, ask_price = _price_cache[_cache_key]
                else:
                    await broker.connect()
                    bid_price, ask_price = await broker.get_bid_ask(trade.symbol)
                    _price_cache[_cache_key] = (bid_price, ask_price)
            except Exception as _pe:
                logger.debug(f"[ForwardEngine] monitor_sl_tp: price fetch failed for {trade.symbol}: {_pe}")
                continue

            is_long = trade.side in _long_sides
            # F-090: use directionally-correct price for SL/TP comparison.
            # LONG exits sell at the bid → check bid against SL/TP.
            # SHORT exits buy at the ask → check ask against SL/TP.
            exit_price = bid_price if is_long else ask_price
            reason = None  # reset per iteration — prevents UnboundLocalError when SL xor TP is set

            # ── Trailing stop: ratchet stop_loss with price movement ─────────
            # Only moves the stop in the favourable direction (never widens it).
            # Uses mid-high (bid for long, ask for short) as the reference price
            # so the trail tracks the best price the position has seen.
            if trade.trailing_stop_pct is not None and trade.entry_price is not None:
                _trail_pct = trade.trailing_stop_pct / 100.0
                if is_long:
                    # Trail: SL = best_bid × (1 − pct) — only move UP
                    _new_trail = bid_price * (1.0 - _trail_pct)
                    if trade.stop_loss is None or _new_trail > trade.stop_loss:
                        _old_sl = trade.stop_loss
                        logger.info(
                            f"[ForwardEngine] Trailing stop UP: {trade.symbol} id={trade.id} "
                            f"sl {_old_sl} → {_new_trail:.6f}  (bid={bid_price}  trail={trade.trailing_stop_pct}%)"
                        )
                        trade.stop_loss = _new_trail
                        db_session.add(trade)
                        await db_session.flush()
                        # Sync the new SL to the broker's standing stop order
                        try:
                            await broker.update_stop_loss(
                                trade.symbol, trade.side, trade.quantity, _new_trail
                            )
                        except Exception as _bsl_err:
                            logger.debug(
                                f"[ForwardEngine] broker SL sync failed for "
                                f"{trade.symbol} id={trade.id}: {_bsl_err}"
                            )
                        # G11: broadcast and notify trailing stop movement
                        try:
                            from api.websocket import manager as _ws_mgr
                            await _ws_mgr.broadcast("trailing_stop_moved", {
                                "trade_id": trade.id, "symbol": trade.symbol,
                                "old_stop": round(_old_sl, 8) if _old_sl else None,
                                "new_stop": round(_new_trail, 8),
                                "direction": "up", "broker": trade.broker.value if hasattr(trade.broker, 'value') else str(trade.broker),
                            })
                        except Exception:
                            pass
                else:
                    # Trail: SL = best_ask × (1 + pct) — only move DOWN
                    _new_trail = ask_price * (1.0 + _trail_pct)
                    if trade.stop_loss is None or _new_trail < trade.stop_loss:
                        _old_sl = trade.stop_loss
                        logger.info(
                            f"[ForwardEngine] Trailing stop DOWN: {trade.symbol} id={trade.id} "
                            f"sl {_old_sl} → {_new_trail:.6f}  (ask={ask_price}  trail={trade.trailing_stop_pct}%)"
                        )
                        trade.stop_loss = _new_trail
                        db_session.add(trade)
                        await db_session.flush()
                        # Sync the new SL to the broker's standing stop order
                        try:
                            await broker.update_stop_loss(
                                trade.symbol, trade.side, trade.quantity, _new_trail
                            )
                        except Exception as _bsl_err:
                            logger.debug(
                                f"[ForwardEngine] broker SL sync failed for "
                                f"{trade.symbol} id={trade.id}: {_bsl_err}"
                            )
                        # G11: broadcast and notify trailing stop movement
                        try:
                            from api.websocket import manager as _ws_mgr
                            await _ws_mgr.broadcast("trailing_stop_moved", {
                                "trade_id": trade.id, "symbol": trade.symbol,
                                "old_stop": round(_old_sl, 8) if _old_sl else None,
                                "new_stop": round(_new_trail, 8),
                                "direction": "down", "broker": trade.broker.value if hasattr(trade.broker, 'value') else str(trade.broker),
                            })
                        except Exception:
                            pass
                if is_long and exit_price <= trade.stop_loss:
                    reason = "stop_loss"
                elif not is_long and exit_price >= trade.stop_loss:
                    reason = "stop_loss"

            # ── Static SL check (non-trailing trades) ─────────────────────────
            # Must run even when trailing_stop_pct is None so that trades with a
            # fixed stop_loss (including legacy open positions with no trailing pct)
            # are closed when they breach their level. The trailing block above
            # handles this for trailing-stop trades; this covers the fixed-SL case.
            if (reason is None
                    and trade.trailing_stop_pct is None
                    and trade.stop_loss is not None):
                if is_long and exit_price <= trade.stop_loss:
                    reason = "stop_loss"
                elif not is_long and exit_price >= trade.stop_loss:
                    reason = "stop_loss"

            if reason is None and trade.take_profit is not None:
                # For long: TP fires when bid rises at or above TP
                # For short: TP fires when ask falls at or below TP
                if is_long and exit_price >= trade.take_profit:
                    reason = "take_profit"
                elif not is_long and exit_price <= trade.take_profit:
                    reason = "take_profit"

            # ── Time-based exits (breakeven + max-hold) ──────────────────────
            # These run regardless of whether SL/TP fired above.
            # opened_at is tz-naive UTC stored in DB.
            if reason is None and trade.opened_at is not None and trade.entry_price is not None:
                _now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
                _age_hours = (_now_utc - trade.opened_at).total_seconds() / 3600.0

                _breakeven_h = _param_float(trade.strategy_name, "breakeven_after_hours")
                _maxhold_h   = _param_float(trade.strategy_name, "max_hold_hours")

                # max_hold: force-close the position — capital has been tied up too long.
                if _maxhold_h is not None and _age_hours >= _maxhold_h:
                    reason = "max_hold_timeout"
                    logger.info(
                        f"[ForwardEngine] Max-hold timeout: {trade.symbol} id={trade.id} "
                        f"open {_age_hours:.1f}h >= {_maxhold_h}h — force closing"
                    )

                # breakeven: slide SL to entry price so the worst outcome is a scratch.
                # Only acts when the position is not already at or past breakeven.
                elif (
                    _breakeven_h is not None
                    and _age_hours >= _breakeven_h
                    and (trade.stop_loss is None or (
                        is_long  and trade.stop_loss < trade.entry_price or
                        not is_long and trade.stop_loss > trade.entry_price
                    ))
                ):
                    _old_sl = trade.stop_loss
                    trade.stop_loss = trade.entry_price
                    db_session.add(trade)
                    await db_session.flush()
                    logger.info(
                        f"[ForwardEngine] Breakeven: {trade.symbol} id={trade.id} "
                        f"open {_age_hours:.1f}h >= {_breakeven_h}h — SL moved "
                        f"{_old_sl} → {trade.entry_price} (entry)"
                    )
                    # Sync to broker's standing stop order
                    try:
                        await broker.update_stop_loss(
                            trade.symbol, trade.side, trade.quantity, trade.entry_price
                        )
                    except Exception as _be_err:
                        logger.debug(f"[ForwardEngine] Breakeven broker SL sync failed: {_be_err}")
                    try:
                        from api.websocket import manager as _ws_mgr
                        await _ws_mgr.broadcast("trailing_stop_moved", {
                            "trade_id": trade.id, "symbol": trade.symbol,
                            "old_stop": round(_old_sl, 8) if _old_sl else None,
                            "new_stop": round(trade.entry_price, 8),
                            "direction": "breakeven",
                            "broker": trade.broker.value if hasattr(trade.broker, 'value') else str(trade.broker),
                        })
                    except Exception:
                        pass
                    # Don't close yet — let price reach the new breakeven SL naturally

            if reason is None:
                continue

            logger.info(
                f"[ForwardEngine] SL/TP monitor triggered {reason} for "
                f"{trade.symbol} id={trade.id} | "
                f"exit={'ask' if not is_long else 'bid'}={exit_price}  sl={trade.stop_loss}  tp={trade.take_profit}"
            )
            try:
                await self.close_position(trade, reason=reason, db_session=db_session)
                self._paper_positions.pop(trade.symbol, None)
                closed_count += 1
            except Exception as _ce:
                logger.error(
                    f"[ForwardEngine] monitor_sl_tp: failed to close {trade.symbol} id={trade.id}: {_ce}"
                )

        if closed_count:
            logger.info(f"[ForwardEngine] SL/TP monitor closed {closed_count} position(s).")

        return closed_count

    async def reconcile_positions(self, db_session) -> int:
        """
        Compare DB OPEN trades against each broker's actual live positions.

        When a SL or TP bracket order fires at the broker, the broker closes
        the position but our DB still has status=OPEN (ghost).  This method
        detects those ghosts and closes them in the DB with an estimated PnL.

        Should be called once per scheduler tick BEFORE processing new signals
        so that ghost entries don't block new trades on the same symbol.

        Returns the number of positions reconciled.
        """
        from sqlalchemy import select as _sel
        from collections import defaultdict

        # ── Symbol normalisation per broker ───────────────────────────────
        # IBKR  forex: "GBP/USD" → "GBP"  (IBForex.symbol = base currency only)
        # Alpaca forex: "GBP/USD" → "GBPUSD"
        # Binance:      "BTC/USDT" → "BTC/USDT"  (direct match)
        _FX = {"USD","EUR","GBP","JPY","CHF","CAD","AUD","NZD",
               "SEK","NOK","DKK","HKD","SGD","MXN","ZAR","HUF","PLN","TRY","CZK","ILS"}

        def _normalize(symbol: str, broker: str) -> str:
            clean = symbol.replace("/", "").upper()
            if broker == "ibkr":
                if len(clean) == 6 and clean[:3] in _FX and clean[3:] in _FX:
                    return clean[:3]
                return symbol.split("/")[0].upper() if "/" in symbol else clean
            if broker == "alpaca":
                return clean
            return symbol  # binance and others already match

        open_q = await db_session.execute(
            # F-105: also reconcile PENDING entry trades — fill may have arrived after
            # the 30 s poll window, or the entry fill + broker SL/TP chain completed
            # before we could check.  Ignoring PENDING left ghost positions permanently.
            _sel(Trade).where(Trade.status.in_([OrderStatus.OPEN, OrderStatus.PENDING]))
        )
        open_trades: list = open_q.scalars().all()
        if not open_trades:
            return 0

        by_broker: dict = defaultdict(list)
        for t in open_trades:
            bk = t.broker.value if hasattr(t.broker, "value") else str(t.broker)
            by_broker[bk].append(t)

        ghost_count = 0
        # NOTE: "long" is an alias for "buy" used by the DB rebuild script.
        _long_sides = {"buy", "cover", "long"}

        for broker_name, trades in by_broker.items():
            try:
                broker = get_broker(broker_name)
                await broker.connect()
                broker_positions = await broker.get_positions()
                broker_symbols = {p.symbol for p in broker_positions}
            except Exception as _e:
                logger.debug(f"[ForwardEngine] reconcile: {broker_name} positions unavailable: {_e}")
                continue

            for trade in trades:
                _norm_key = _normalize(trade.symbol, broker_name)
                if _norm_key in broker_symbols:
                    # F-105: PENDING entry trade confirmed filled at broker → upgrade to OPEN
                    if trade.status == OrderStatus.PENDING:
                        pos = next(
                            (p for p in broker_positions if p.symbol == _norm_key), None
                        )
                        if pos and getattr(pos, "entry_price", None):
                            trade.entry_price = round(pos.entry_price, 8)
                        trade.status = OrderStatus.OPEN
                        self._paper_positions[trade.symbol] = trade
                        ghost_count += 1
                        logger.info(
                            f"[ForwardEngine] F-105: PENDING {trade.symbol} id={trade.id} "
                            f"confirmed at broker → OPEN @ {trade.entry_price}"
                        )
                    continue  # position exists at broker — no further action needed

                # Position is gone from broker.
                # F-105: PENDING with no real orderId → order never reached broker → REJECTED
                if trade.status == OrderStatus.PENDING:
                    _oid = trade.broker_order_id or ""
                    if _oid.startswith("rejected_") or not _oid:
                        trade.status = OrderStatus.REJECTED
                        ghost_count += 1
                        logger.info(
                            f"[ForwardEngine] F-105: PENDING {trade.symbol} id={trade.id} "
                            f"no real orderId → REJECTED"
                        )
                        continue
                    # Real orderId: order submitted, filled+closed at broker before reconcile
                    # (e.g. entry filled + SL/TP bracket fired before next scheduler tick).
                    # Fall through to the ghost-close logic to record FILLED in DB.
                    logger.info(
                        f"[ForwardEngine] F-105: PENDING {trade.symbol} id={trade.id} "
                        f"orderId={_oid} filled+closed at broker — reconciling as FILLED"
                    )

                # Position is gone from broker → SL/TP fired (or manually closed via broker UI)
                logger.info(
                    f"[ForwardEngine] RECONCILE ghost: {trade.symbol} id={trade.id} "
                    f"not in {broker_name} positions — closing in DB"
                )
                try:
                    exit_price: float = 0.0
                    try:
                        exit_price = await broker.get_price(trade.symbol)
                    except Exception:
                        pass

                    # Best-guess reason: compare exit price to SL/TP levels
                    is_long = trade.side in _long_sides
                    reason = "broker_sl_tp"
                    if exit_price and trade.stop_loss and trade.take_profit:
                        if is_long:
                            reason = "take_profit" if exit_price >= trade.take_profit else "stop_loss"
                        else:
                            reason = "take_profit" if exit_price <= trade.take_profit else "stop_loss"
                    elif exit_price and trade.stop_loss:
                        if is_long:
                            reason = "stop_loss" if exit_price <= trade.stop_loss else "broker_close"
                        else:
                            reason = "stop_loss" if exit_price >= trade.stop_loss else "broker_close"

                    # Compute PnL from exit price
                    if exit_price and trade.entry_price:
                        side_mult = 1.0 if is_long else -1.0
                        raw_pnl = (exit_price - trade.entry_price) * trade.quantity * side_mult
                        trade.exit_price = round(exit_price, 8)
                        trade.pnl = round(raw_pnl, 4)
                        cost_basis = trade.entry_price * trade.quantity
                        trade.pnl_pct = round(raw_pnl / cost_basis * 100, 4) if cost_basis else 0.0

                    trade.status = OrderStatus.FILLED
                    trade.closed_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    self._paper_positions.pop(trade.symbol, None)

                    # Record outcome for risk manager circuit breaker counters
                    if trade.pnl is not None:
                        _broker_val = trade.broker.value if hasattr(trade.broker, "value") else None
                        self.risk_manager.record_outcome(
                            won=trade.pnl > 0,
                            strategy_name=getattr(trade, "strategy_name", None),
                            broker=_broker_val,
                        )

                    # In-app notification
                    try:
                        from notifications.notifier import dispatch as _dispatch
                        _mode_tag = "PAPER" if trade.is_paper else "LIVE"
                        _broker_str = trade.broker.value if hasattr(trade.broker, "value") else str(trade.broker)
                        _pnl_str = (
                            f" P&L: {'+'if (trade.pnl or 0) >= 0 else ''}{trade.pnl:.4f}"
                            if trade.pnl is not None else ""
                        )
                        _level = "success" if (trade.pnl or 0) > 0 else "warning" if (trade.pnl or 0) == 0 else "error"
                        await _dispatch(
                            db_session,
                            title=f"🎯 [{_mode_tag}] {reason.replace('_', ' ').title()} — {trade.symbol}",
                            message=(
                                f"{trade.side.upper()} {trade.symbol} closed by broker "
                                f"({reason.replace('_', ' ')}) @ {exit_price} on {_broker_str}.{_pnl_str}"
                            ),
                            level=_level,
                            category="trade",
                            metadata={
                                "symbol": trade.symbol,
                                "side": trade.side,
                                "entry_price": trade.entry_price,
                                "exit_price": exit_price,
                                "pnl": trade.pnl,
                                "broker": _broker_str,
                                "is_paper": trade.is_paper,
                                "reason": reason,
                                "trade_id": trade.id,
                            },
                            send_email=True,
                        )
                    except Exception as _ne:
                        logger.debug(f"[ForwardEngine] Reconcile notification failed: {_ne}")

                    ghost_count += 1
                except Exception as _re:
                    logger.error(f"[ForwardEngine] Reconcile failed for {trade.symbol} id={trade.id}: {_re}")

        if ghost_count:
            await db_session.commit()
            logger.info(f"[ForwardEngine] Reconciled {ghost_count} ghost position(s).")

        return ghost_count

    def resume(self):
        """Re-enable trading after emergency stop."""
        self._emergency_stop_active = False
        logger.info("[ForwardEngine] Trading resumed after emergency stop.")
