import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
from loguru import logger
from sqlalchemy import select, func

from core.strategies.base import Signal
from core.risk_manager import RiskManager, get_risk_manager  # BUG-2 FIX: import singleton factory
from brokers import get_broker, get_broker_modes
from db.models import Trade, LiveTrade, OrderStatus, ExecutionMode, AssetClass


def _trade_model(is_paper: bool):
    """Return the ORM class for the correct trades table."""
    return Trade if is_paper else LiveTrade
from config import settings as _cfg

# Convenience alias used by tests and external code
PAPER_INITIAL_CAPITAL: float = _cfg.paper_initial_balance

# IMP-3: prevent concurrent monitor_sl_tp calls within the same FastAPI process.
# M-7 FIX: This is a MODULE-LEVEL lock shared across all ForwardEngine instances
# in the same process — it serialises all concurrent monitor_sl_tp() calls that
# run on the same event loop (FastAPI heartbeat + manual trigger).
# Cross-process (Celery vs FastAPI) deduplication is handled by the DB advisory lock.
_MONITOR_SL_TP_LOCK: asyncio.Lock = asyncio.Lock()

# BUG-1 FIX: single module-level constant — previously defined as two separate
# local variables (_G1_DEFAULT_THRESHOLD and _G1_DEFAULT_THRESHOLD_MEM) in
# separate if/elif branches, which could silently diverge on edits.
_G1_DEFAULT_THRESHOLD: float = 0.75

# Bug-9 FIX: per-trade in-progress guard for close_position when no DB session
# is available (paper in-memory mode).  asyncio is cooperative — adding an id
# before any await and removing it after is race-free within one process.
_closing_ids: set[int] = set()


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
        self.risk_manager = get_risk_manager()  # BUG-2 FIX: use process-wide singleton
        self._paper_positions: dict = {}     # symbol:broker_key → Trade (open positions)
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
        # Key by symbol:broker so two brokers on the same symbol do not
        # clobber each other's in-memory position.
        self._paper_positions = {
            f"{t.symbol}:{t.broker.value if hasattr(t.broker, 'value') else str(t.broker)}": t
            for t in open_trades
        }

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
            # Include unrealized P&L from OPEN positions so the paper balance
            # reflects current mark-to-market, not just closed trades.
            unrealised_q = await db_session.execute(
                select(func.coalesce(func.sum(Trade.pnl), 0.0)).where(
                    Trade.is_paper == True,
                    Trade.status == OrderStatus.OPEN,
                    Trade.pnl.isnot(None),
                    Trade.broker == broker_val,
                )
            )
            unrealised_pnl: float = unrealised_q.scalar_one()
            key = broker_val.value if hasattr(broker_val, 'value') else str(broker_val)
            self._paper_balance[key] = _cfg.paper_initial_balance + realised_pnl + unrealised_pnl

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
        # BUG-1 FIX: build today_start as a tz-naive UTC datetime so it matches
        # the TIMESTAMP WITHOUT TIME ZONE columns in the DB.  Previously the code
        # called datetime.now(utc).replace(..., tzinfo=None) which first creates a
        # timezone-aware object and then strips the tzinfo, giving the correct UTC
        # value but was fragile (would break if the host clock is not UTC).
        # Using datetime.utcnow() directly is the canonical tz-naive UTC approach.
        from datetime import timezone as _tz
        _now_utc = datetime.now(_tz.utc)
        today_start = datetime(
            _now_utc.year, _now_utc.month, _now_utc.day,
            _cfg.daily_reset_hour_utc, 0, 0,  # configurable reset hour, always tz-naive
        )
        # If reset hour > current UTC hour the reset boundary is still yesterday — go back one day
        if _now_utc.hour < _cfg.daily_reset_hour_utc:
            today_start -= timedelta(days=1)
        # Build optional broker filter
        broker_filter: list = []
        if broker:
            try:
                from db.models import BrokerName as _BN
                broker_filter = [Trade.broker == _BN(broker)]
            except ValueError:
                pass

        # Build equivalent broker filter for LiveTrade
        live_broker_filter: list = []
        if broker:
            try:
                from db.models import BrokerName as _BN2
                live_broker_filter = [LiveTrade.broker == _BN2(broker)]
            except ValueError:
                pass

        # Bug-15 FIX: separate paper and live P&L so a large live loss does
        # not trip the paper circuit-breaker and vice versa.  Only paper rows
        # carry is_paper == True; LiveTrade rows are always live.
        q_realised_paper = await db_session.execute(
            select(func.coalesce(func.sum(Trade.pnl), 0.0)).where(
                Trade.is_paper == True,
                Trade.status == OrderStatus.FILLED,
                Trade.closed_at >= today_start,
                *broker_filter,
            )
        )
        q_realised_live = await db_session.execute(
            select(func.coalesce(func.sum(LiveTrade.pnl), 0.0)).where(
                LiveTrade.status == OrderStatus.FILLED,
                LiveTrade.closed_at >= today_start,
                *live_broker_filter,
            )
        )
        realised: float = q_realised_paper.scalar_one() + q_realised_live.scalar_one()
        q_open_paper = await db_session.execute(
            select(func.coalesce(func.sum(Trade.pnl), 0.0)).where(
                Trade.is_paper == True,
                Trade.status == OrderStatus.OPEN,
                Trade.pnl.isnot(None),
                Trade.opened_at >= today_start,
                *broker_filter,
            )
        )
        q_open_live = await db_session.execute(
            select(func.coalesce(func.sum(LiveTrade.pnl), 0.0)).where(
                LiveTrade.status == OrderStatus.OPEN,
                LiveTrade.pnl.isnot(None),
                LiveTrade.opened_at >= today_start,
                *live_broker_filter,
            )
        )
        unrealized: float = q_open_paper.scalar_one() + q_open_live.scalar_one()
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
    ) -> Trade | LiveTrade | None:
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
        # force_paper ensures paper strategies use paper/testnet credentials and
        # live strategies use live credentials, regardless of the global _BROKER_MODES
        # setting derived from .env.  Without this, flipping a strategy to is_paper=False
        # would still trade against the paper endpoint if that's what .env points to.
        broker = get_broker(signal.broker, force_paper=is_paper)
        await broker.connect()   # no-op for Binance/Alpaca; ensures IBKR singleton is live

        # ── Get balance + open count ──────────────────────
        # Both paper and live use the real broker API — paper broker instances
        # are already configured with paper/testnet credentials by get_broker().
        try:
            bal = await broker.get_balance()
            balance = bal.available
        except Exception as _bal_err:
            broker_key = getattr(signal.broker, 'value', str(signal.broker))
            if not is_paper:
                # Never guess balance for a live strategy — incorrect sizing can mean
                # a significantly oversized position with real money.  Refuse the trade.
                logger.error(
                    f"[ForwardEngine] LIVE balance unavailable for {broker_key}: {_bal_err}. "
                    f"Refusing signal — cannot size safely without confirmed account balance."
                )
                return None
            # Paper fallback: reconstruct from realised P&L stored in memory
            logger.warning(f"[ForwardEngine] Could not fetch balance from {broker_key}: {_bal_err} — using paper fallback")
            balance = self._paper_balance.get(broker_key, _cfg.paper_initial_balance)
        # Count open positions for this broker from the DB (authoritative source).
        # broker.get_positions() returns ALL non-zero exchange balances which
        # includes pre-funded testnet assets unrelated to bot trades, causing false
        # "Max open positions" rejections across brokers.  When no DB session is
        # available, fall back to in-memory paper positions filtered by broker.
        broker_key = getattr(signal.broker, 'value', str(signal.broker))
        if db_session is not None:
            try:
                _oq_paper = await db_session.execute(
                    select(func.count()).where(
                        Trade.status == OrderStatus.OPEN,
                        Trade.broker == signal.broker,
                    )
                )
                _oq_live = await db_session.execute(
                    select(func.count()).where(
                        LiveTrade.status == OrderStatus.OPEN,
                        LiveTrade.broker == signal.broker,
                    )
                )
                # GAP-3 FIX: count only same-mode (paper or live) open positions
                # so that live positions don’t block paper strategies and vice-versa.
                # Use is_paper param from process_signal() — Signal has no is_paper attr.
                if is_paper:
                    open_count = int(_oq_paper.scalar_one() or 0)
                else:
                    open_count = int(_oq_live.scalar_one() or 0)
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
        # Include BOTH paper (Trade) and live (LiveTrade) open positions so that
        # live real-money positions count against the exposure cap — critical for
        # accurate risk enforcement when real capital is at stake.
        asset_class_exposure = 0.0
        if db_session is not None:
            try:
                # Filter by is_paper so paper positions don't count against live
                # exposure caps and vice-versa.
                if is_paper:
                    _exp_q = await db_session.execute(
                        select(func.coalesce(func.sum(Trade.quantity * Trade.entry_price), 0.0)).where(
                            Trade.status == OrderStatus.OPEN,
                            Trade.asset_class == signal.asset_class,
                            Trade.broker == signal.broker,
                            Trade.is_paper == True,
                        )
                    )
                else:
                    _exp_q = await db_session.execute(
                        select(func.coalesce(func.sum(LiveTrade.quantity * LiveTrade.entry_price), 0.0)).where(
                            LiveTrade.status == OrderStatus.OPEN,
                            LiveTrade.asset_class == signal.asset_class,
                            LiveTrade.broker == signal.broker,
                        )
                    )
                asset_class_exposure = float(_exp_q.scalar_one() or 0.0)
            except Exception as _exp_err:
                logger.debug(f"[ForwardEngine] Could not compute asset_class_exposure: {_exp_err}")

        # BUG-5 FIX: compute per-symbol asset exposure so validate() can enforce
        # max_exposure_per_asset_pct as a hard gate, not just a position-size cap.
        asset_exposure = 0.0
        if db_session is not None:
            try:
                # Filter by is_paper to avoid mixing paper/live asset exposure.
                if is_paper:
                    _sym_q = await db_session.execute(
                        select(func.coalesce(func.sum(Trade.quantity * Trade.entry_price), 0.0)).where(
                            Trade.status == OrderStatus.OPEN,
                            Trade.symbol == signal.symbol,
                            Trade.broker == signal.broker,
                            Trade.is_paper == True,
                        )
                    )
                else:
                    _sym_q = await db_session.execute(
                        select(func.coalesce(func.sum(LiveTrade.quantity * LiveTrade.entry_price), 0.0)).where(
                            LiveTrade.status == OrderStatus.OPEN,
                            LiveTrade.symbol == signal.symbol,
                            LiveTrade.broker == signal.broker,
                        )
                    )
                asset_exposure = float(_sym_q.scalar_one() or 0.0)
            except Exception as _ae_err:
                logger.debug(f"[ForwardEngine] Could not compute asset_exposure: {_ae_err}")

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
            asset_exposure=asset_exposure,
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
        # BUG-LOW-02 FIX: reject zero/negative multipliers — clamp to 0.05 so
        # position sizing is never silently zeroed out by a bad caller value.
        if position_size_multiplier is not None and position_size_multiplier <= 0:
            logger.warning(
                f"[ForwardEngine] position_size_multiplier={position_size_multiplier} "
                f"is zero or negative for {signal.symbol} — clamping to 0.05"
            )
            position_size_multiplier = 0.05
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
        _pp_key = f"{signal.symbol}:{broker_key}"  # Bug-14: composite key for in-memory lookup
        if db_session is not None:
            _TradeModel = _trade_model(is_paper)
            _existing_q = await db_session.execute(
                select(_TradeModel).where(
                    _TradeModel.symbol == signal.symbol,
                    _TradeModel.broker == signal.broker,
                    _TradeModel.status == OrderStatus.OPEN,
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
                    # g1_override_min_confidence in strategy params overrides the global
                    # default of 0.75 (75%). Any broker, any pair, any timeframe.
                    _g1_threshold = _G1_DEFAULT_THRESHOLD  # BUG-1 FIX: uses module-level constant
                    if strategy_params:
                        try:
                            _g1_threshold = float(strategy_params["g1_override_min_confidence"])
                        except (KeyError, TypeError, ValueError):
                            pass
                    if (
                        signal.confidence is not None
                        and signal.confidence >= _g1_threshold
                    ):
                        logger.info(
                            f"[ForwardEngine] G1 overridden by ML confidence "
                            f"({signal.confidence:.2f} >= {_g1_threshold}): allowing re-entry "
                            f"{signal.signal} {signal.symbol} alongside id={_existing.id}"
                        )
                        # Re-query position to confirm it's still OPEN.
                        # The FastAPI scheduler (monitor_sl_tp) runs in a separate
                        # process and may have closed this position between the
                        # initial DB read and now.
                        if db_session is not None:
                            _recheck = await db_session.get(type(_existing), _existing.id)
                            if _recheck is None or _recheck.status != OrderStatus.OPEN:
                                logger.info(
                                    f"[ForwardEngine] G1 re-entry aborted: "
                                    f"position {_existing.id} was already closed"
                                )
                                return None
                        # Fall through — new entry proceeds with signal's own SL/TP
                    else:
                        logger.info(
                            f"[ForwardEngine] G1: Same-direction duplicate blocked: "
                            f"{signal.signal} {signal.symbol} — already {_existing.side.upper()} id={_existing.id}"
                        )
                        return None
        elif _pp_key in self._paper_positions:
            _existing = self._paper_positions[_pp_key]
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
                # G1 in-memory path — same override logic, same global default
                _g1_threshold_mem = _G1_DEFAULT_THRESHOLD  # BUG-1 FIX: reuses module-level constant
                if strategy_params:
                    try:
                        _g1_threshold_mem = float(strategy_params["g1_override_min_confidence"])
                    except (KeyError, TypeError, ValueError):
                        pass
                if (
                    signal.confidence is not None
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

        # G5: IBKR forex minimum lot size pre-check.
        # IMP-27 FIX: IBKR IDEALPRO actual minimum is 20,000 base currency units,
        # not 25,000 as was previously hardcoded.  Orders below this threshold are
        # rejected by IBKR with error 200 "No security definition has been found".
        _IBKR_FOREX_MIN_LOT = 20_000.0
        _broker_key_g5 = getattr(signal.broker, 'value', str(signal.broker))
        _asset_cls_g5 = getattr(signal.asset_class, 'value', str(signal.asset_class or '')).upper()
        if _broker_key_g5 == 'ibkr' and _asset_cls_g5 == 'FOREX' and effective_size < _IBKR_FOREX_MIN_LOT:
            logger.warning(
                f"[ForwardEngine] G5: IBKR forex lot {effective_size:.0f} < minimum {_IBKR_FOREX_MIN_LOT:.0f} "
                f"for {signal.symbol} — order rejected to avoid IBKR rejection"
            )
            return None

        TradeModel = _trade_model(is_paper)
        trade = TradeModel(
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
                if not is_paper:
                    # GAP-01: IBKR live multi-leg execution via BAG combo contract.
                    if broker_key == "ibkr":
                        expiry = meta.get("expiry", "")
                        quantity = max(1, int(getattr(signal, "quantity", 1) or 1))
                        try:
                            result = await broker.place_multi_leg_order(
                                symbol=signal.symbol,
                                legs=legs,
                                quantity=quantity,
                                expiry=expiry,
                            )
                        except Exception as _mleg_err:
                            logger.error(
                                f"[ForwardEngine] LIVE multi-leg IBKR order failed for "
                                f"{signal.symbol}: {_mleg_err}"
                            )
                            return None
                        trade.entry_price = result.fill_price or signal.entry_price
                        trade.status = OrderStatus.OPEN
                        trade.broker_order_id = result.order_id
                        if db_session:
                            db_session.add(trade)
                            await db_session.commit()
                            await db_session.refresh(trade)
                        logger.info(
                            f"[ForwardEngine] ✅ LIVE multi-leg IBKR order placed: "
                            f"{signal.signal} {signal.symbol} @ {trade.entry_price} "
                            f"(order_id={trade.broker_order_id})"
                        )
                        return trade
                    else:
                        logger.error(
                            f"[ForwardEngine] LIVE multi-leg options not supported for "
                            f"broker '{broker_key}' — use IBKR or switch to paper mode."
                        )
                        return None
                # BUG-CRIT-02 FIX: Paper mode multi-leg options — simulate execution without
                # calling broker.place_order() (no broker supports multi-leg paper orders).
                # Mark OPEN directly so the position is tracked and monitor_sl_tp can apply
                # software-side SL/TP on the net premium.
                logger.warning(
                    f"[ForwardEngine] Multi-leg option ({meta.get('strategy_type', '?')}) for "
                    f"{signal.symbol} — paper simulation (live requires IBKR)."
                )
                trade.entry_price = signal.entry_price
                trade.status = OrderStatus.OPEN
                trade.broker_order_id = (
                    f"paper_multileg_{signal.symbol}_{int(datetime.now(timezone.utc).timestamp())}"
                )
                if db_session:
                    db_session.add(trade)
                    await db_session.commit()
                    await db_session.refresh(trade)
                self._paper_positions[f"{signal.symbol}:{broker_key}"] = trade
                logger.info(
                    f"[ForwardEngine] ✅ PAPER multi-leg option simulated: "
                    f"{signal.signal} {signal.symbol} @ {signal.entry_price} "
                    f"(broker_order_id={trade.broker_order_id})"
                )
                try:
                    from notifications.notifier import notifier as _notifier
                    await _notifier.trade(
                        db_session,
                        title=f"[PAPER] Multi-leg Option Opened — {signal.symbol}",
                        message=(
                            f"{signal.signal} {signal.symbol} @ {signal.entry_price} "
                            f"({meta.get('strategy_type', 'options')}) — paper simulated"
                        ),
                        metadata={
                            "symbol": signal.symbol, "side": signal.signal,
                            "broker": broker_key, "is_paper": True,
                            "strategy": signal.strategy_name,
                        },
                    )
                except Exception:
                    pass
                return trade
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
            # If broker reanchored SL/TP (e.g. Alpaca code 42210000), update the trade
            # record so monitor_sl_tp enforces the same levels the broker bracket uses.
            if result.effective_stop_price is not None:
                trade.stop_loss = result.effective_stop_price
            if result.effective_take_profit is not None:
                trade.take_profit = result.effective_take_profit
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
                self._paper_positions[f"{signal.symbol}:{broker_key}"] = trade  # only track confirmed fills
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

    async def close_position(self, trade: Trade | LiveTrade, reason: str = "manual", db_session=None):
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
            _TradeModel = type(trade)
            _guard = await db_session.execute(
                _upd(_TradeModel)
                .where(_TradeModel.id == trade.id, _TradeModel.status == OrderStatus.OPEN)
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
        elif trade.id is not None:
            # Bug-9 FIX: no DB session (paper in-memory) — use a process-level
            # set of in-progress trade IDs.  asyncio cooperative scheduling
            # means no interleaving can occur between non-await statements.
            if trade.id in _closing_ids:
                logger.warning(
                    f"[ForwardEngine] close_position SKIPPED (in-progress) "
                    f"for {trade.symbol} id={trade.id}"
                )
                return
            _closing_ids.add(trade.id)

        # BUG-CRIT-02 FIX: ensure _closing_ids is always cleaned up even when
        # an exception propagates out of close_position (e.g. broker unreachable,
        # RuntimeError from place_order).  Without this, the trade is permanently
        # stuck in _closing_ids and can never be closed again in this process.
        try:
            _close_result_cleanup_id = trade.id
        except Exception:
            _close_result_cleanup_id = None
        _close_position_entered = True

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
        # F-108: bracket tracking — how much the exchange's own SL/TP order already sold
        # and at what average price, so we can compute a weighted-average exit price.
        _bracket_qty: float = 0.0
        _bracket_price: float = 0.0
        _close_qty: float = trade.quantity
        _do_market_close: bool = True
        if not _skip_market_order:
            # Cancel any standing bracket exit orders (SL guard, TP limit) so that
            # the asset balance is fully freed before we issue the market close order.
            # On Binance spot, open exit orders lock the full sell quantity — without
            # cancellation, the market sell fails with "insufficient balance".
            try:
                await broker.cancel_open_orders(trade.symbol)
            except Exception as _coo_err:
                logger.debug(f"[ForwardEngine] pre-close cancel_open_orders failed: {_coo_err}")
            # F-108: After cancelling bracket orders, verify the actual free balance.
            # Binance spot deducts fees from the received base quantity, so trade.quantity
            # (what was ordered) may exceed what's actually free.  Also handles the case
            # where the exchange's own bracket SL/TP already executed the position (free=0).
            _exch = getattr(broker, "exchange", None)
            if _exch is not None:
                try:
                    _fb = await _exch.fetch_balance()
                    _asset = trade.symbol.split("/")[0]
                    _free_now = float((_fb.get(_asset) or {}).get("free", 0) or 0)
                    if _free_now == 0.0:
                        logger.info(
                            f"[ForwardEngine] F-108: {trade.symbol} free balance=0 after cancel "
                            f"— exchange bracket already closed this position. Marking FILLED at snapshot price."
                        )
                        _do_market_close = False
                        # Try to recover bracket fill price from recent closed orders
                        try:
                            _closed = await _exch.fetch_closed_orders(trade.symbol, limit=10)
                            _sl_fills = [
                                o for o in _closed
                                if str(o.get("type", "")).upper() in ("STOP_LOSS", "STOP_LOSS_LIMIT", "STOP_MARKET")
                                and str(o.get("status", "")).upper() == "CLOSED"
                                and float(o.get("filled", 0) or 0) > 0
                            ]
                            if _sl_fills:
                                _latest = sorted(_sl_fills, key=lambda o: o.get("timestamp", 0))[-1]
                                _bracket_qty = float(_latest.get("filled", 0) or 0)
                                _bracket_price = float(_latest.get("average") or _latest.get("price") or 0)
                        except Exception as _bfp_err:
                            logger.debug(f"[ForwardEngine] F-108 bracket fill-price lookup failed: {_bfp_err}")
                        # Final fallback: use SL level as bracket price estimate
                        if _bracket_price == 0.0 and trade.stop_loss:
                            _bracket_qty = trade.quantity
                            _bracket_price = trade.stop_loss
                    elif 0 < _free_now < trade.quantity:
                        _bracket_qty = round(trade.quantity - _free_now, 8)
                        # Try to get actual bracket fill price
                        try:
                            _closed = await _exch.fetch_closed_orders(trade.symbol, limit=10)
                            _sl_fills = [
                                o for o in _closed
                                if str(o.get("type", "")).upper() in ("STOP_LOSS", "STOP_LOSS_LIMIT", "STOP_MARKET")
                                and str(o.get("status", "")).upper() == "CLOSED"
                                and float(o.get("filled", 0) or 0) > 0
                            ]
                            if _sl_fills:
                                _latest = sorted(_sl_fills, key=lambda o: o.get("timestamp", 0))[-1]
                                _bracket_price = float(_latest.get("average") or _latest.get("price") or 0)
                        except Exception as _bfp_err:
                            logger.debug(f"[ForwardEngine] F-108 bracket fill-price lookup failed: {_bfp_err}")
                        # Fallback: use stop_loss level as bracket price estimate
                        if _bracket_price == 0.0 and trade.stop_loss:
                            _bracket_price = trade.stop_loss
                        _close_qty = _free_now
                        logger.info(
                            f"[ForwardEngine] F-108: {trade.symbol} bracket filled {_bracket_qty:.4f} "
                            f"@ ~{_bracket_price}, remnant {_close_qty:.4f} free — selling remnant"
                        )
                except Exception as _f108_err:
                    logger.debug(f"[ForwardEngine] F-108 balance check failed: {_f108_err}")
            if _do_market_close:
                try:
                    close_result = await broker.place_order(
                        symbol=trade.symbol,
                        side=side,
                        quantity=_close_qty,
                        order_type="market",
                    )
                    # Use broker-confirmed fill price for PnL accuracy
                    if close_result.fill_price:
                        _remnant_fill = close_result.fill_price
                        # F-108: compute weighted average exit across bracket fill + remnant fill
                        # so the recorded PnL reflects the full original position cost.
                        if _bracket_qty > 0 and _bracket_price > 0:
                            exit_price = round(
                                (_bracket_qty * _bracket_price + _close_qty * _remnant_fill) / trade.quantity, 8
                            )
                            logger.info(
                                f"[ForwardEngine] Close order filled @ {_remnant_fill} (confirmed) — "
                                f"weighted avg exit {exit_price} "
                                f"(bracket {_bracket_qty:.4f}@{_bracket_price} + remnant {_close_qty:.4f}@{_remnant_fill})"
                            )
                        else:
                            exit_price = _remnant_fill
                            logger.info(f"[ForwardEngine] Close order filled @ {exit_price} (confirmed)")
                    elif exit_price:
                        logger.warning(
                            f"[ForwardEngine] Close order {close_result.order_id} not confirmed filled — "
                            f"using price snapshot ({exit_price}) for PnL"
                        )
                    else:
                        raise RuntimeError("Close order unconfirmed and no price snapshot available")
                except Exception as _close_err:
                    # F-103b / BUG-6 FIX: revert the atomic guard (PENDING→OPEN) for ANY
                    # exception — including RuntimeError — so the next scheduler tick can
                    # retry instead of leaving the trade permanently stuck as PENDING.
                    # Previously `except RuntimeError: raise` skipped this block, meaning
                    # a RuntimeError (e.g. "no price snapshot") left the trade stuck forever.
                    if db_session is not None and trade.id is not None:
                        from sqlalchemy import update as _upd_revert
                        _TradeModelRevert = type(trade)
                        try:
                            await db_session.execute(
                                _upd_revert(_TradeModelRevert)
                                .where(_TradeModelRevert.id == trade.id, _TradeModelRevert.status == OrderStatus.PENDING)
                                .values(status=OrderStatus.OPEN)
                            )
                            await db_session.flush()
                        except Exception:
                            pass
                    logger.error(
                        f"[ForwardEngine] Could not place closing order for {trade.symbol}: {_close_err} "
                        "— guard reverted to OPEN for retry next tick"
                    )
                    # BUG-CRIT-02 FIX: release in-memory guard so the next tick can retry.
                    # Without this, the trade is permanently stuck in _closing_ids when
                    # an exception propagates before the discard at the end of the function.
                    _closing_ids.discard(trade.id)
                    raise

        # F-108: when bracket filled the entire position (free=0 after cancel),
        # no market sell was sent — override exit_price with the bracket's fill price.
        # Short-circuit AND: if _skip_market_order=True, _do_market_close is never evaluated.
        if not _skip_market_order and not _do_market_close and _bracket_price > 0:
            exit_price = _bracket_price

        # ── Compute realised PnL ──────────────────────────────────────────
        # Always record exit_price when we have it — even if entry_price is 0/None
        # (corrupted earlier), so the closed trade at least shows when it exited.
        if exit_price:
            trade.exit_price = round(exit_price, 8)
            if trade.entry_price:  # can only compute P&L when entry is known and non-zero
                side_mult = 1.0 if trade.side in _long_sides else -1.0
                raw_pnl = (exit_price - trade.entry_price) * trade.quantity * side_mult
                trade.pnl = round(raw_pnl, 4)
                cost_basis = trade.entry_price * trade.quantity
                trade.pnl_pct = round(raw_pnl / cost_basis * 100, 4) if cost_basis else 0.0
            trade.status = OrderStatus.FILLED
        else:
            # Couldn't obtain exit price — mark CANCELLED so the dashboard
            # never shows a FILLED trade with blank exit/P&L.
            trade.status = OrderStatus.CANCELLED
            trade.notes = (
                (trade.notes or "") +
                " Closed but exit price unavailable — marked CANCELLED."
            ).strip()
            logger.warning(
                f"[ForwardEngine] close_position: {trade.symbol} id={trade.id} "
                f"exit_price=0 → CANCELLED (price fetch failed)"
            )
        trade.closed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        # Remove from in-memory cache
        _trade_bk = trade.broker.value if hasattr(trade.broker, 'value') else str(trade.broker)
        self._paper_positions.pop(f"{trade.symbol}:{_trade_bk}", None)
        _closing_ids.discard(trade.id)  # Bug-9: release in-progress guard

        # F-082: cancel IBKR persistent bracket market-data subscription for this symbol.
        # For Alpaca/Binance this is a no-op (hasattr guard).
        try:
            _cbs = getattr(broker, "cancel_bracket_subscription", None)
            if _cbs:
                _cbs(trade.symbol)
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

        # Auto-convert residual FX to USD immediately after closing an IBKR forex position.
        # This prevents T+2 balance fluctuation from unsettled foreign-currency proceeds.
        try:
            _close_broker_str2 = getattr(trade.broker, "value", str(trade.broker))
            if _close_broker_str2 == "ibkr" and "/" in trade.symbol:
                _atf = getattr(broker, "auto_convert_fx", None)
                _fx_result = await _atf() if _atf else None
                if _fx_result:
                    logger.info(f"[ForwardEngine] FX auto-converted after {trade.symbol} close: {_fx_result}")
        except Exception as _fx_err:
            logger.warning(f"[ForwardEngine] FX auto-convert failed after {trade.symbol} close: {_fx_err}")

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
            paper_q = await db_session.execute(
                select(Trade).where(Trade.status == OrderStatus.OPEN)
            )
            live_q = await db_session.execute(
                select(LiveTrade).where(LiveTrade.status == OrderStatus.OPEN)
            )
            all_open = paper_q.scalars().all() + live_q.scalars().all()
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

    async def cleanup_stale_pending_trades(self, db_session, timeout_minutes: int = 30) -> int:
        """
        IMP-31: Resolve PENDING trades that have been stuck too long.

        A PENDING trade is one where the broker accepted the order but the
        fill confirmation never arrived within the broker's poll timeout.
        These trades block position slots indefinitely if never resolved.

        Strategy:
          1. Find all PENDING trades older than `timeout_minutes`.
          2. Mark them as REJECTED (safe: the broker order may or may not have
             filled, but we cannot confirm — the trade audit trail is preserved).
          3. Dispatch an in-app warning notification so the operator is aware.

        Returns the number of trades resolved.
        """
        from db.models import BrokerName as _BN
        # BUG-LOW-01 FIX: datetime.utcnow() is deprecated in Python 3.12+; use
        # datetime.now(timezone.utc).replace(tzinfo=None) for tz-naive UTC instead.
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=timeout_minutes)
        resolved = 0

        for _TradeModel in (Trade, LiveTrade):
            _pending_q = await db_session.execute(
                select(_TradeModel).where(
                    _TradeModel.status == OrderStatus.PENDING,
                    _TradeModel.opened_at <= cutoff,
                )
            )
            stale = _pending_q.scalars().all()
            for t in stale:
                # BUG-HIGH-03 FIX: before marking REJECTED, check the broker for the
                # actual order status.  A slow network or exchange delay can leave a
                # legitimate fill in PENDING state — marking it REJECTED would
                # incorrectly orphan a real open position.
                _t_bk = t.broker.value if hasattr(t.broker, 'value') else str(t.broker)
                _broker_confirmed_filled = False
                if t.broker_order_id and not t.broker_order_id.startswith("rejected_"):
                    try:
                        _chk_broker = get_broker(_t_bk, force_paper=t.is_paper)
                        await _chk_broker.connect()
                        _order_result = await _chk_broker.get_order_status(
                            t.broker_order_id, t.symbol
                        )
                        if _order_result and str(_order_result.status).lower() in ("filled", "closed"):
                            _broker_confirmed_filled = True
                            t.status = OrderStatus.OPEN
                            if _order_result.fill_price:
                                t.entry_price = _order_result.fill_price
                            self._paper_positions[f"{t.symbol}:{_t_bk}"] = t
                            logger.info(
                                f"[ForwardEngine] IMP-31: PENDING trade id={t.id} {t.symbol} "
                                f"confirmed FILLED at broker — upgraded to OPEN."
                            )
                    except Exception as _chk_err:
                        logger.debug(
                            f"[ForwardEngine] IMP-31: broker status check failed for "
                            f"id={t.id} {t.symbol}: {_chk_err} — will try position poll"
                        )

                # ── Position-level fallback ──────────────────────────────────────
                # get_order_status() can't confirm a fill for synthetic IDs like
                # "orphan_sync", and IBKR paper routinely delays/drops fill events.
                # Before rejecting, verify whether the broker still has an OPEN
                # position for this symbol — if yes, the fill happened and the DB
                # record just missed the confirmation.
                if not _broker_confirmed_filled:
                    try:
                        _chk_broker2 = get_broker(_t_bk, force_paper=t.is_paper)
                        await _chk_broker2.connect()
                        _live_positions = await _chk_broker2.get_positions()
                        _t_norm = t.symbol.replace("/", "").split(":")[0].upper()
                        for _lp in _live_positions:
                            _lp_norm = _lp.symbol.replace("/", "").split(":")[0].upper()
                            if _lp_norm == _t_norm:
                                _broker_confirmed_filled = True
                                t.status = OrderStatus.OPEN
                                if _lp.entry_price and t.entry_price is None:
                                    t.entry_price = round(_lp.entry_price, 8)
                                if _lp.quantity and t.quantity is None:
                                    t.quantity = _lp.quantity
                                self._paper_positions[f"{t.symbol}:{_t_bk}"] = t
                                logger.info(
                                    f"[ForwardEngine] IMP-31: PENDING trade id={t.id} "
                                    f"{t.symbol} confirmed via position poll — upgraded to OPEN."
                                )
                                break
                    except Exception as _pos_err:
                        logger.debug(
                            f"[ForwardEngine] IMP-31: position poll fallback failed for "
                            f"id={t.id} {t.symbol}: {_pos_err}"
                        )

                if not _broker_confirmed_filled:
                    t.status = OrderStatus.REJECTED
                    t.notes = (
                        (t.notes or "") +
                        f" | IMP-31: auto-resolved as REJECTED after {timeout_minutes}m pending timeout"
                    )
                    # Remove from in-memory cache so it doesn't block the next strategy run
                    self._paper_positions.pop(f"{t.symbol}:{_t_bk}", None)
                    logger.warning(
                        f"[ForwardEngine] IMP-31: PENDING trade id={t.id} {t.symbol} "
                        f"exceeded {timeout_minutes}m timeout — marked REJECTED. "
                        f"broker_order_id={t.broker_order_id}"
                    )
                    try:
                        from notifications.notifier import dispatch as _notif_dispatch
                        await _notif_dispatch(
                            db_session,
                            title=f"⚠ PENDING Trade Timed Out — {t.symbol}",
                            message=(
                                f"Trade id={t.id} ({t.side.upper()} {t.symbol}) has been PENDING "
                                f"for over {timeout_minutes} minutes without fill confirmation. "
                                f"It has been auto-resolved as REJECTED. "
                                f"broker_order_id={t.broker_order_id}"
                            ),
                            level="warning",
                            category="trade",
                            metadata={"trade_id": t.id, "symbol": t.symbol, "broker_order_id": t.broker_order_id},
                        )
                    except Exception as _n_err:
                        logger.debug(f"[ForwardEngine] PENDING timeout notification failed: {_n_err}")
                resolved += 1

        if resolved:
            await db_session.flush()
            logger.info(f"[ForwardEngine] IMP-31: resolved {resolved} stale PENDING trade(s)")
        return resolved

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

        # Fetch ALL open trades (paper + live) — we need trades without SL/TP too for time-based exits.
        _paper_q = await db_session.execute(
            _sel(Trade).where(Trade.status == OrderStatus.OPEN)
        )
        _live_q = await db_session.execute(
            _sel(LiveTrade).where(LiveTrade.status == OrderStatus.OPEN)
        )
        open_trades: list = _paper_q.scalars().all() + _live_q.scalars().all()
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
        _price_cache: dict[tuple[str, bool, str], tuple[float, float | None]] = {}

        # Use streamed prices (sub-second) where available; fall back to REST.
        from core.engine.price_stream import price_stream_manager as _psm

        # GAP-7 FIX: batch-fetch all uncached prices in parallel BEFORE the trade loop
        # to avoid sequential REST calls (each ~300ms for IBKR) when streaming is down.
        _prefetch: list[tuple[tuple, str, str, bool]] = []
        _seen_prefetch: set[tuple] = set()
        for _t in open_trades:
            if _t.stop_loss is None and _t.take_profit is None and _t.trailing_stop_pct is None:
                continue
            _t_broker = _t.broker.value if hasattr(_t.broker, "value") else str(_t.broker)
            _t_key = (_t_broker, _t.is_paper, _t.symbol)
            if _psm.get_price(_t.symbol) is None and _t_key not in _price_cache and _t_key not in _seen_prefetch:
                _prefetch.append((_t_key, _t_broker, _t.symbol, _t.is_paper))
                _seen_prefetch.add(_t_key)
        if _prefetch:
            async def _fetch_bid_ask_one(ck, bn, sym, ip):
                try:
                    _b = get_broker(bn, force_paper=ip)
                    await _b.connect()
                    bid, ask = await _b.get_bid_ask(sym)
                    return ck, bid, ask
                except Exception as _fe:
                    logger.debug(f"[ForwardEngine] batch price prefetch failed {sym}: {_fe}")
                    return ck, None, None
            _pf_results = await asyncio.gather(
                *[_fetch_bid_ask_one(ck, bn, sym, ip) for ck, bn, sym, ip in _prefetch],
                return_exceptions=False,
            )
            for _ck, _bid, _ask in _pf_results:
                # BUG-HIGH-02 FIX: validate price before caching — a zero or negative
                # bid would cause SL checks to incorrectly trigger on all long positions.
                if _bid is not None and _bid > 0 and (_ask is None or _ask >= _bid):
                    _price_cache[_ck] = (_bid, _ask)

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
                _cache_key = (broker_name, trade.is_paper, trade.symbol)
                # Always resolve the broker object with the correct paper/live credentials.
                # force_paper=trade.is_paper ensures live trades sync SL/TP to the live
                # broker endpoint (not the paper one from _BROKER_MODES default).
                broker = get_broker(broker_name, force_paper=trade.is_paper)

                streamed = _psm.get_price(trade.symbol)
                if streamed is not None and streamed > 0:
                    # Streaming mid-price available — no REST call needed.
                    # bid ≈ ask ≈ mid for liquid assets; acceptable for SL/TP trigger.
                    bid_price, ask_price = streamed, streamed
                    _price_cache[_cache_key] = (bid_price, ask_price)
                elif _cache_key in _price_cache:
                    bid_price, ask_price = _price_cache[_cache_key]
                else:
                    await broker.connect()
                    bid_price, ask_price = await broker.get_bid_ask(trade.symbol)
                    # Only cache valid prices
                    if bid_price and bid_price > 0:
                        _price_cache[_cache_key] = (bid_price, ask_price)
            except Exception as _pe:
                logger.debug(f"[ForwardEngine] monitor_sl_tp: price fetch failed for {trade.symbol}: {_pe}")
                continue

            # Skip this trade if we still have no valid price
            if not bid_price or bid_price <= 0:
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
                        # GAP-2 FIX: explicit parentheses — prevents silent precedence bug
                        # if the condition is ever extended with more terms.
                        (is_long     and trade.stop_loss < trade.entry_price) or
                        (not is_long and trade.stop_loss > trade.entry_price)
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
                _sltp_bk = trade.broker.value if hasattr(trade.broker, 'value') else str(trade.broker)
                self._paper_positions.pop(f"{trade.symbol}:{_sltp_bk}", None)
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

        paper_q = await db_session.execute(
            # F-105: also reconcile PENDING entry trades — fill may have arrived after
            # the 30 s poll window, or the entry fill + broker SL/TP chain completed
            # before we could check.  Ignoring PENDING left ghost positions permanently.
            _sel(Trade).where(Trade.status.in_([OrderStatus.OPEN, OrderStatus.PENDING]))
        )
        live_q = await db_session.execute(
            _sel(LiveTrade).where(LiveTrade.status.in_([OrderStatus.OPEN, OrderStatus.PENDING]))
        )
        open_trades: list = paper_q.scalars().all() + live_q.scalars().all()
        # NOTE: do NOT early-return here even when open_trades is empty.
        # The orphan-sync block at the bottom of this function must always run
        # so that IBKR live positions that were never saved to the DB are detected
        # and created as LiveTrade records.

        # Group by (broker_name, is_paper) so paper trades are reconciled against
        # the paper endpoint and live trades against the live endpoint.  Without
        # this split, a live trade could be compared to paper account positions
        # (which it would never appear in) and be incorrectly ghost-closed.
        by_broker: dict = defaultdict(list)
        for t in open_trades:
            bk = t.broker.value if hasattr(t.broker, "value") else str(t.broker)
            by_broker[(bk, t.is_paper)].append(t)

        ghost_count = 0
        # NOTE: "long" is an alias for "buy" used by the DB rebuild script.
        _long_sides = {"buy", "cover", "long"}

        for (broker_name, _is_paper), trades in by_broker.items():
            try:
                broker = get_broker(broker_name, force_paper=_is_paper)
                await broker.connect()
                broker_positions = await broker.get_positions()
                broker_symbols = {p.symbol for p in broker_positions}
            except Exception as _e:
                logger.debug(f"[ForwardEngine] reconcile: {broker_name} positions unavailable: {_e}")
                continue

            # For IBKR: pre-fetch open bracket SL/TP so we can patch PENDING→OPEN
            # upgrades that are missing SL/TP (bracket created but DB record predated it).
            _open_brackets: dict = {}
            if broker_name == "ibkr":
                try:
                    _gob_r = getattr(broker, "get_open_brackets", None)
                    if _gob_r:
                        _open_brackets = await asyncio.wait_for(_gob_r(), timeout=12.0)
                        if _open_brackets:
                            logger.info(f"[ForwardEngine] reconcile: brackets fetched for {list(_open_brackets.keys())}")
                        else:
                            logger.debug("[ForwardEngine] reconcile: no open bracket orders found at IBKR")
                except asyncio.TimeoutError:
                    logger.warning("[ForwardEngine] reconcile: bracket fetch timed out (>12s) — SL/TP patch skipped")
                except Exception as _br_err:
                    logger.warning(f"[ForwardEngine] reconcile: bracket fetch failed: {_br_err}")

            for trade in trades:
                _norm_key = _normalize(trade.symbol, broker_name)
                if _norm_key in broker_symbols:
                    # F-105: PENDING entry trade confirmed filled at broker → upgrade to OPEN
                    if trade.status == OrderStatus.PENDING:
                        pos = next(
                            (p for p in broker_positions if p.symbol == _norm_key), None
                        )
                        # Only update entry_price from broker avgCost when it is > 0;
                        # IBKR reports avgCost=0 while a fill is still settling, which
                        # would corrupt the entry_price with a zero value.
                        if pos and pos.entry_price:
                            trade.entry_price = round(pos.entry_price, 8)
                        # Patch null SL/TP from open bracket orders (IBKR only).
                        # This covers the case where the trade was created before the
                        # bracket child orders were placed (race condition on fast fills).
                        if trade.stop_loss is None and _open_brackets:
                            _br = _open_brackets.get(_norm_key, {})
                            if _br.get("sl"):
                                trade.stop_loss = round(float(_br["sl"]), 8)
                                logger.info(
                                    f"[ForwardEngine] F-105: restored SL={trade.stop_loss} "
                                    f"from bracket for {trade.symbol} id={trade.id}"
                                )
                            if _br.get("tp"):
                                trade.take_profit = round(float(_br["tp"]), 8)
                                logger.info(
                                    f"[ForwardEngine] F-105: restored TP={trade.take_profit} "
                                    f"from bracket for {trade.symbol} id={trade.id}"
                                )
                        trade.status = OrderStatus.OPEN
                        _rec_bk = trade.broker.value if hasattr(trade.broker, 'value') else str(trade.broker)
                        self._paper_positions[f"{trade.symbol}:{_rec_bk}"] = trade
                        ghost_count += 1
                        logger.info(
                            f"[ForwardEngine] F-105: PENDING {trade.symbol} id={trade.id} "
                            f"confirmed at broker → OPEN @ {trade.entry_price}"
                        )
                    elif trade.status == OrderStatus.OPEN:
                        # Patch null SL/TP for already-OPEN trades (e.g. created before
                        # bracket child orders were placed, or orphan-synced trades).
                        if _open_brackets and (trade.stop_loss is None or trade.take_profit is None):
                            _br_o = _open_brackets.get(_norm_key, {})
                            if trade.stop_loss is None and _br_o.get("sl"):
                                trade.stop_loss = round(float(_br_o["sl"]), 8)
                                logger.info(
                                    f"[ForwardEngine] reconcile: restored SL={trade.stop_loss} "
                                    f"from bracket for OPEN {trade.symbol} id={trade.id}"
                                )
                            if trade.take_profit is None and _br_o.get("tp"):
                                trade.take_profit = round(float(_br_o["tp"]), 8)
                                logger.info(
                                    f"[ForwardEngine] reconcile: restored TP={trade.take_profit} "
                                    f"from bracket for OPEN {trade.symbol} id={trade.id}"
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
                # Guard: zero-qty entries were never actually executed at the broker.
                # Marking them FILLED would produce phantom trades with no exit/P&L.
                if (trade.quantity or 0) <= 0:
                    trade.status = OrderStatus.CANCELLED
                    trade.notes = (
                        (trade.notes or "") +
                        " Zero-quantity order — never executed at broker."
                    ).strip()
                    ghost_count += 1
                    logger.info(
                        f"[ForwardEngine] Reconcile: {trade.symbol} id={trade.id} "
                        f"qty=0 → CANCELLED (never executed)"
                    )
                    continue
                try:
                    exit_price: float = 0.0
                    is_long = trade.side in _long_sides
                    reason = "broker_sl_tp"

                    # IMP-3 FIX: attempt actual bracket fill-price lookup from the broker
                    # (same F-108 logic used in close_position) before falling back to a
                    # current market snapshot that may be stale by minutes or hours.
                    _imp3_resolved = False
                    _exch2 = getattr(broker, "exchange", None)
                    if _exch2 is not None:
                        try:
                            _closed = await _exch2.fetch_closed_orders(trade.symbol, limit=10)
                            _tp_types = {"TAKE_PROFIT", "TAKE_PROFIT_LIMIT", "TAKE_PROFIT_MARKET"}
                            _sl_types = {"STOP_LOSS", "STOP_LOSS_LIMIT", "STOP_MARKET"}
                            _tp_fills = [
                                o for o in _closed
                                if str(o.get("type", "")).upper() in _tp_types
                                and str(o.get("status", "")).upper() == "CLOSED"
                                and float(o.get("filled", 0) or 0) > 0
                            ]
                            _sl_fills = [
                                o for o in _closed
                                if str(o.get("type", "")).upper() in _sl_types
                                and str(o.get("status", "")).upper() == "CLOSED"
                                and float(o.get("filled", 0) or 0) > 0
                            ]
                            if _tp_fills:
                                _latest = sorted(_tp_fills, key=lambda o: o.get("timestamp", 0))[-1]
                                _fp = float(_latest.get("average") or _latest.get("price") or 0)
                                if _fp:
                                    exit_price = _fp
                                    reason = "take_profit"
                                    _imp3_resolved = True
                                    logger.info(
                                        f"[ForwardEngine] IMP-3: {trade.symbol} id={trade.id} "
                                        f"bracket TP fill @ {exit_price} from closed orders"
                                    )
                            elif _sl_fills:
                                _latest = sorted(_sl_fills, key=lambda o: o.get("timestamp", 0))[-1]
                                _fp = float(_latest.get("average") or _latest.get("price") or 0)
                                if _fp:
                                    exit_price = _fp
                                    reason = "stop_loss"
                                    _imp3_resolved = True
                                    logger.info(
                                        f"[ForwardEngine] IMP-3: {trade.symbol} id={trade.id} "
                                        f"bracket SL fill @ {exit_price} from closed orders"
                                    )
                        except Exception as _imp3_err:
                            logger.debug(f"[ForwardEngine] IMP-3 bracket fill lookup failed: {_imp3_err}")

                    if not _imp3_resolved:
                        # Fallback: use current market snapshot to estimate reason
                        try:
                            exit_price = await broker.get_price(trade.symbol)
                        except Exception:
                            pass

                        # Best-guess reason: compare snapshot price to SL/TP levels
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

                        # BUG-4 FIX: use the SL/TP bracket level instead of the stale
                        # market snapshot (only when IMP-3 broker lookup was unavailable).
                        if reason == "take_profit" and trade.take_profit:
                            exit_price = trade.take_profit
                        elif reason == "stop_loss" and trade.stop_loss:
                            exit_price = trade.stop_loss
                        # reason == "broker_close" → no known bracket fired, keep snapshot

                    # Compute PnL from exit price — always record exit_price even
                    # when entry_price is 0/corrupt so the dashboard shows the exit.
                    if exit_price:
                        trade.exit_price = round(exit_price, 8)
                        if trade.entry_price:
                            side_mult = 1.0 if is_long else -1.0
                            raw_pnl = (exit_price - trade.entry_price) * trade.quantity * side_mult
                            trade.pnl = round(raw_pnl, 4)
                            cost_basis = trade.entry_price * trade.quantity
                            trade.pnl_pct = round(raw_pnl / cost_basis * 100, 4) if cost_basis else 0.0
                        trade.status = OrderStatus.FILLED
                    else:
                        # Could not retrieve exit price → mark CANCELLED so the trade
                        # doesn't appear as a FILLED record with no exit/P&L data.
                        trade.status = OrderStatus.CANCELLED
                        trade.notes = (
                            (trade.notes or "") +
                            " Closed at broker but exit price unavailable — marked CANCELLED."
                        ).strip()
                        logger.warning(
                            f"[ForwardEngine] Reconcile: {trade.symbol} id={trade.id} "
                            f"exit_price=0 → CANCELLED (price unavailable)"
                        )
                    trade.closed_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    _mon_bk = trade.broker.value if hasattr(trade.broker, 'value') else str(trade.broker)
                    self._paper_positions.pop(f"{trade.symbol}:{_mon_bk}", None)

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

                    # Auto-convert residual FX to USD when reconcile-closing an IBKR forex ghost.
                    try:
                        _rec_broker_str = getattr(trade.broker, "value", str(trade.broker))
                        if _rec_broker_str == "ibkr" and "/" in trade.symbol:
                            _atf3 = getattr(broker, "auto_convert_fx", None)
                            _rec_fx = await _atf3() if _atf3 else None
                            if _rec_fx:
                                logger.info(f"[ForwardEngine] FX auto-converted after reconcile of {trade.symbol}: {_rec_fx}")
                    except Exception as _fx_rec_err:
                        logger.warning(f"[ForwardEngine] FX auto-convert failed during reconcile of {trade.symbol}: {_fx_rec_err}")

                    ghost_count += 1
                except Exception as _re:
                    logger.error(f"[ForwardEngine] Reconcile failed for {trade.symbol} id={trade.id}: {_re}")

        if ghost_count:
            await db_session.commit()
            logger.info(f"[ForwardEngine] Reconciled {ghost_count} ghost position(s).")

        # ── Broker→DB sync: create records for IBKR positions with no DB trade ──
        # This recovers from the scenario where place_order() succeeded at IBKR but the
        # DB commit failed (session crash, timeout, etc.), leaving the position orphaned.
        # We pull all the data directly from TWS: entry price (avgCost), side, quantity,
        # and — crucially — the live SL/TP prices from the open bracket child orders.
        orphan_count = 0
        try:
            from db.models import Signal as SignalModel, BrokerName
            from config import settings as _settings
            # Respect configured IBKR mode (paper or live) — the singleton always
            # connects to the configured port so force_paper must match.
            _ibkr_is_paper: bool = _settings.ibkr_paper
            ibkr_key = ("ibkr", _ibkr_is_paper)
            if ibkr_key in by_broker:
                _ibkr_trades = by_broker[ibkr_key]
            else:
                # No existing DB trades for this IBKR mode — still need to check broker
                _ibkr_trades = []

            ibkr_broker = get_broker("ibkr", force_paper=_ibkr_is_paper)
            await ibkr_broker.connect()
            ibkr_positions = await ibkr_broker.get_positions()
            _gob = getattr(ibkr_broker, "get_open_brackets", None)
            try:
                brackets = await asyncio.wait_for(_gob(), timeout=12.0) if _gob else {}
                if brackets:
                    logger.info(f"[ForwardEngine] orphan-sync: brackets fetched for {list(brackets.keys())}")
                else:
                    logger.debug("[ForwardEngine] orphan-sync: no open bracket orders found at IBKR")
            except (asyncio.TimeoutError, Exception) as _be:
                logger.warning(f"[ForwardEngine] orphan-sync: bracket fetch failed: {_be}")
                brackets = {}

            # Build the set of short symbols already tracked in DB (normalised)
            tracked_symbols = {
                _normalize(t.symbol, "ibkr") for t in _ibkr_trades
            }

            # Use the right ORM model: Trade for paper, LiveTrade for live
            _TradeModel = Trade if _ibkr_is_paper else LiveTrade

            for pos in ibkr_positions:
                if pos.symbol in tracked_symbols:
                    continue  # already in DB — handled by ghost-close loop above

                # Orphan position: at broker, not in DB
                bracket = brackets.get(pos.symbol, {})
                sl_price: Optional[float] = bracket.get("sl")
                tp_price: Optional[float] = bracket.get("tp")
                # Priority: bracket full_symbol → pos.full_symbol (CASH→"GBP/USD") → pos.symbol fallback
                full_sym: str = bracket.get("full_symbol") or pos.full_symbol or pos.symbol

                # Find the most recent signal for this symbol/broker to link strategy info.
                # We check both acted_on=False (DB save failed) and acted_on=True (saved but
                # trade record missing) in the last 48 hours.
                _cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=48)
                _sig_q = await db_session.execute(
                    _sel(SignalModel)
                    .where(
                        SignalModel.symbol == full_sym,
                        SignalModel.broker == BrokerName.IBKR,
                        SignalModel.created_at >= _cutoff,
                    )
                    .order_by(SignalModel.created_at.desc())
                    .limit(1)
                )
                linked_signal = _sig_q.scalar_one_or_none()

                # If no signal found by full_sym, retry with the short contract symbol
                # (handles edge cases where signals were stored without the /currency suffix)
                if linked_signal is None and full_sym != pos.symbol:
                    _sig_q2 = await db_session.execute(
                        _sel(SignalModel)
                        .where(
                            SignalModel.symbol == pos.symbol,
                            SignalModel.broker == BrokerName.IBKR,
                            SignalModel.created_at >= _cutoff,
                        )
                        .order_by(SignalModel.created_at.desc())
                        .limit(1)
                    )
                    linked_signal = _sig_q2.scalar_one_or_none()

                # Guard: if this signal already has a trade record (any status),
                # do NOT insert another row — that would violate uq_trades_signal_id
                # and poison the DB session, blocking all subsequent signal runs.
                # This happens when a position was manually closed but a new IBKR
                # position appears for the same symbol within the 48-hour window.
                if linked_signal is not None:
                    _existing_for_sig = await db_session.execute(
                        _sel(_TradeModel).where(_TradeModel.signal_id == linked_signal.id)
                    )
                    if _existing_for_sig.scalar_one_or_none() is not None:
                        logger.debug(
                            f"[ForwardEngine] Orphan sync: {full_sym} — "
                            f"signal_id={linked_signal.id} already has a trade record; "
                            f"skipping duplicate INSERT"
                        )
                        continue  # skip this position — trade record already exists

                new_trade = _TradeModel(
                    symbol=full_sym,
                    side=pos.side,  # "long" | "short"
                    quantity=pos.quantity,
                    # Guard against IBKR avgCost=0 (settling state) — store None so it
                    # can be patched on the next reconcile tick when avgCost is available.
                    entry_price=round(pos.entry_price, 8) if pos.entry_price else None,
                    stop_loss=round(sl_price, 8) if sl_price else (linked_signal.stop_loss if linked_signal else None),
                    take_profit=round(tp_price, 8) if tp_price else (linked_signal.take_profit if linked_signal else None),
                    status=OrderStatus.OPEN,
                    execution_mode=linked_signal.execution_mode if linked_signal else ExecutionMode.FULL_AUTO,
                    asset_class=AssetClass(pos.asset_class) if pos.asset_class in AssetClass._value2member_map_ else AssetClass.FOREX,
                    broker=BrokerName.IBKR,
                    is_paper=_ibkr_is_paper,
                    strategy_name=linked_signal.strategy_name if linked_signal else "unknown",
                    signal_id=linked_signal.id if linked_signal else None,
                    broker_order_id="orphan_sync",
                    opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
                db_session.add(new_trade)

                if linked_signal and not linked_signal.acted_on:
                    linked_signal.acted_on = True

                orphan_count += 1
                logger.info(
                    f"[ForwardEngine] ORPHAN SYNC: created LiveTrade for {full_sym} "
                    f"entry={pos.entry_price} sl={sl_price} tp={tp_price} "
                    f"(signal_id={linked_signal.id if linked_signal else None})"
                )

            if orphan_count:
                await db_session.commit()
                logger.info(f"[ForwardEngine] Synced {orphan_count} orphan broker position(s) to DB.")
        except Exception as _oe:
            logger.error(f"[ForwardEngine] Orphan broker sync failed: {_oe}", exc_info=False)
            try:
                await db_session.rollback()
            except Exception:
                pass

        # BUG-LOW-03 FIX: extend orphan sync to Alpaca and Binance.
        # The original block only covered IBKR, so Alpaca/Binance positions created
        # after a DB-commit failure would never be recovered and stayed permanently
        # orphaned (real capital at risk with no SL/TP tracking in the DB).
        for _orph_broker, _orph_broker_name, _orph_bn_enum in (
            ("alpaca", False, "alpaca"),
            ("binance", False, "binance"),
        ):
            try:
                from db.models import BrokerName as _BN2, AssetClass as _AC2, ExecutionMode as _EM2
                # Respect runtime paper/live toggle — never force live when paper is enabled.
                _orph_is_paper: bool = get_broker_modes().get(_orph_broker, "paper") == "paper"
                _orph_key = (_orph_broker, _orph_is_paper)
                _orph_tracked = {
                    _normalize(t.symbol, _orph_broker)
                    for t in by_broker.get(_orph_key, [])
                }
                _o_broker_obj = get_broker(_orph_broker)  # no force_paper — honours runtime mode
                await _o_broker_obj.connect()
                _o_positions = await _o_broker_obj.get_positions()

                # Only orphan-sync symbols that have an active strategy on this broker.
                # Without this guard the Binance testnet account (which holds hundreds of
                # pre-seeded dust positions) would flood the DB with "unknown" trade records
                # every time the engine restarts.
                from db.models import Strategy as _StratModel2
                _active_strats_q = await db_session.execute(
                    _sel(_StratModel2).where(
                        _StratModel2.broker == _BN2(_orph_broker),
                        _StratModel2.is_active == True,
                    )
                )
                _active_orphan_syms = {
                    _normalize(s.symbol, _orph_broker)
                    for s in _active_strats_q.scalars().all()
                }

                for _op in _o_positions:
                    _op_norm = _normalize(_op.symbol, _orph_broker)
                    if _op_norm in _orph_tracked:
                        continue  # already in DB — handled by ghost-close loop

                    # Skip positions with no active strategy — avoids testnet garbage
                    if _op_norm not in _active_orphan_syms:
                        continue

                    # Look up a recent signal for strategy info
                    _cutoff2 = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=48)
                    try:
                        _osig_q = await db_session.execute(
                            _sel(SignalModel)
                            .where(
                                SignalModel.symbol == _op.symbol,
                                SignalModel.broker == _BN2(_orph_broker),
                                SignalModel.created_at >= _cutoff2,
                            )
                            .order_by(SignalModel.created_at.desc())
                            .limit(1)
                        )
                        _osig = _osig_q.scalar_one_or_none()
                    except Exception:
                        _osig = None

                    _o_model = Trade if _orph_is_paper else LiveTrade
                    _onew = _o_model(
                        symbol=_op.symbol,
                        side=_op.side,
                        quantity=_op.quantity,
                        entry_price=round(_op.entry_price, 8) if _op.entry_price else None,
                        stop_loss=_osig.stop_loss if _osig else None,
                        take_profit=_osig.take_profit if _osig else None,
                        status=OrderStatus.OPEN,
                        execution_mode=_osig.execution_mode if _osig else ExecutionMode.FULL_AUTO,
                        asset_class=AssetClass(_op.asset_class) if _op.asset_class in AssetClass._value2member_map_ else AssetClass.CRYPTO,
                        broker=_BN2(_orph_broker),
                        is_paper=_orph_is_paper,
                        strategy_name=_osig.strategy_name if _osig else "unknown",
                        signal_id=_osig.id if _osig else None,
                        broker_order_id="orphan_sync",
                        opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    )
                    db_session.add(_onew)
                    orphan_count += 1
                    logger.info(
                        f"[ForwardEngine] ORPHAN SYNC ({_orph_broker}): created LiveTrade for "
                        f"{_op.symbol} entry={_op.entry_price} (signal_id={_osig.id if _osig else None})"
                    )

                if orphan_count:
                    await db_session.commit()
            except Exception as _o2e:
                logger.debug(f"[ForwardEngine] Orphan sync skipped for {_orph_broker}: {_o2e}")

        return ghost_count + orphan_count

    def resume(self):
        """Re-enable trading after emergency stop."""
        self._emergency_stop_active = False
        logger.info("[ForwardEngine] Trading resumed after emergency stop.")


# ── Module-level singleton (GAP-6 FIX) ──────────────────────────────────────────────────────────
# Shared instance for the SL/TP heartbeat and manual-execute paths.
# NOT used by _run_one_strategy (concurrent, needs per-call isolation).
_forward_engine_instance: "ForwardEngine | None" = None


def get_forward_engine() -> ForwardEngine:
    """Return the process-wide ForwardEngine singleton (mirrors get_risk_manager)."""
    global _forward_engine_instance
    if _forward_engine_instance is None:
        _forward_engine_instance = ForwardEngine()
    return _forward_engine_instance
