import asyncio
import json
import os
from dataclasses import dataclass
from datetime import date
from typing import Optional
from loguru import logger

from config import settings
from core.strategies.base import Signal

# Persisted risk-state file (circuit breaker + consecutive losses survive restarts)
_STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "runtime", "risk_state.json")

# BUG-2 FIX: process-wide singleton — all callers (ForwardEngine, risk.py, portfolio.py)
# share one in-memory instance so reset_circuit_breaker() from the API propagates
# immediately to the engine without requiring a full process restart.
_risk_manager_instance: Optional["RiskManager"] = None


def get_risk_manager() -> "RiskManager":
    """Return the process-wide RiskManager singleton, creating it on first call."""
    global _risk_manager_instance
    if _risk_manager_instance is None:
        _risk_manager_instance = RiskManager()
    return _risk_manager_instance


@dataclass
class RiskValidation:
    approved: bool
    position_size: float       # in units of the asset
    position_value: float      # in USD
    risk_amount: float         # max loss in USD
    stop_distance: float       # price distance to stop
    reason: Optional[str] = None


class RiskManager:
    """
    Validates every signal before execution.
    Enforces all 5 levels of risk hierarchy.

    Level 1: Per-trade risk (position sizing)
    Level 2: Per-strategy daily loss limit
    Level 3: Portfolio exposure limits
    Level 4: Daily circuit breaker
    Level 5: Emergency stop (handled in ForwardEngine)
    """

    def __init__(self):
        self.risk_per_trade_pct = settings.risk_per_trade_pct / 100
        self.max_open_positions = settings.max_open_positions
        self.daily_circuit_breaker_pct = settings.daily_circuit_breaker_pct / 100
        self.max_consecutive_losses = settings.max_consecutive_losses
        self.max_exposure_per_asset_pct = settings.max_exposure_per_asset_pct / 100
        self.max_exposure_per_class_pct = settings.max_exposure_per_class_pct / 100
        self.default_rr_ratio = settings.default_rr_ratio
        self.atr_stop_multiplier = settings.atr_stop_multiplier

        # Runtime state — loaded from persistent JSON so restarts don't clear them
        self.daily_pnl: float = 0.0
        self.open_positions_count: int = 0
        self.consecutive_losses: int = 0           # portfolio-wide
        self._circuit_breaker_active: bool = False  # portfolio-wide
        # F-032: per-strategy state — keyed by strategy_name
        # {"my_strat": {"consecutive_losses": 2, "circuit_breaker_active": False, "circuit_breaker_date": "2026-03-07"}}
        self._per_strategy: dict[str, dict] = {}
        # Per-broker state — keyed by broker name (binance/alpaca/ibkr)
        # {"binance": {"consecutive_losses": 1, "circuit_breaker_active": False, "circuit_breaker_date": "2026-03-07"}}
        self._per_broker: dict[str, dict] = {}
        # Cross-process reset propagation: track mtime of the state file so that
        # when the FastAPI container resets and saves, the Celery worker detects
        # the change on the next validate() call and reloads automatically.
        self._state_file_mtime: float = 0.0
        self._load_state()

    # ──────────────────────────────────────────────────────────────────────────
    # State persistence helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _load_state(self) -> None:
        """Restore circuit-breaker and consecutive-loss state from disk."""
        try:
            if not os.path.exists(_STATE_FILE):
                return
            with open(_STATE_FILE, "r") as fh:
                state = json.load(fh)
            today = str(date.today())
            # Portfolio-wide state
            breaker_date = state.get("circuit_breaker_date")
            if breaker_date == today:
                self._circuit_breaker_active = state.get("circuit_breaker_active", False)
            else:
                self._circuit_breaker_active = False  # new day → reset
            self.consecutive_losses = state.get("consecutive_losses", 0)
            # F-032: Per-strategy state — auto-reset CB if it was set on a previous day
            raw_per = state.get("per_strategy", {})
            for name, s in raw_per.items():
                cb_active = s.get("circuit_breaker_active", False)
                if cb_active and s.get("circuit_breaker_date") != today:
                    cb_active = False  # new day → reset
                self._per_strategy[name] = {
                    "consecutive_losses": s.get("consecutive_losses", 0),
                    "circuit_breaker_active": cb_active,
                    "circuit_breaker_date": s.get("circuit_breaker_date"),
                }
            # Per-broker state — auto-reset CB if set on a previous day
            raw_per_broker = state.get("per_broker", {})
            for broker_name, bs in raw_per_broker.items():
                cb_active = bs.get("circuit_breaker_active", False)
                if cb_active and bs.get("circuit_breaker_date") != today:
                    cb_active = False  # new day → reset
                self._per_broker[broker_name] = {
                    "consecutive_losses": bs.get("consecutive_losses", 0),
                    "circuit_breaker_active": cb_active,
                    "circuit_breaker_date": bs.get("circuit_breaker_date"),
                }
            logger.info(
                f"[RiskManager] State loaded — circuit_breaker={self._circuit_breaker_active} "
                f"consecutive_losses={self.consecutive_losses} "
                f"per_strategy_count={len(self._per_strategy)} "
                f"per_broker_count={len(self._per_broker)}"
            )
            # Record mtime so _maybe_reload_state() knows this load is current.
            try:
                self._state_file_mtime = os.path.getmtime(_STATE_FILE)
            except OSError:
                pass
        except Exception as exc:
            logger.warning(f"[RiskManager] Could not load risk state: {exc}")

    def _save_state(self) -> None:
        """Persist circuit-breaker and consecutive-loss state to disk atomically.
        Uses write-to-temp-then-rename to avoid partial writes corrupting the state
        file when FastAPI and Celery workers both call _save_state concurrently.

        BUG-5 FIX: always call _do_save_state() synchronously.
        The JSON payload is tiny (< 1 KB) so the blocking I/O completes in < 1 ms
        — not worth the risk of losing state on SIGTERM if the executor future
        is still queued when the event loop shuts down.
        """
        self._do_save_state()

    def _do_save_state(self) -> None:
        """Blocking file write — always safe to call from a thread."""
        try:
            import tempfile
            os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
            payload = json.dumps(
                {
                    "circuit_breaker_active": self._circuit_breaker_active,
                    "circuit_breaker_date": str(date.today()),
                    "consecutive_losses": self.consecutive_losses,
                    "per_strategy": self._per_strategy,  # F-032
                    "per_broker": self._per_broker,
                }
            )
            # Write to a sibling temp file, then atomically rename.
            # M-3 FIX: on Windows, os.replace can raise PermissionError if the
            # destination file is open; clean up the tmp file if rename fails.
            dir_ = os.path.dirname(_STATE_FILE)
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp") as tmp:
                    tmp.write(payload)
                    tmp_path = tmp.name
                os.replace(tmp_path, _STATE_FILE)
                # Update mtime so our own write doesn't trigger a redundant reload.
                try:
                    self._state_file_mtime = os.path.getmtime(_STATE_FILE)
                except OSError:
                    pass
            except Exception as _replace_err:
                logger.warning(f"[RiskManager] Could not atomically save risk state: {_replace_err}")
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
        except Exception as exc:
            logger.warning(f"[RiskManager] Could not save risk state: {exc}")

    def _maybe_reload_state(self) -> None:
        """Re-load persisted state when risk_state.json has been modified externally.

        This propagates manual resets performed via the FastAPI process to the
        Celery worker process (and vice-versa), since each runs its own in-memory
        RiskManager singleton.  The check is a single stat() syscall — cheap
        enough to call on every validate().
        """
        try:
            if not os.path.exists(_STATE_FILE):
                return
            mtime = os.path.getmtime(_STATE_FILE)
            if mtime != self._state_file_mtime:
                logger.info(
                    "[RiskManager] State file changed externally — reloading "
                    f"(stored mtime={self._state_file_mtime}, file mtime={mtime})"
                )
                self._load_state()
        except OSError:
            pass

    def validate(
        self,
        signal: Signal,
        account_balance: float,
        open_positions_count: int,
        daily_pnl: float,
        asset_class_exposure: float = 0.0,
        asset_exposure: float = 0.0,          # BUG-5 FIX: existing open value for THIS symbol
        broker: str | None = None,
        broker_settings: dict | None = None,
    ) -> RiskValidation:
        """
        Validate a signal before execution.
        Returns RiskValidation with approved=True/False and calculated position size.

        broker_settings: dict fetched from the broker_risk_settings DB table.
        Any None value in broker_settings falls back to the global config default.
        asset_exposure: total notional of already-open positions in signal.symbol,
        used to enforce max_exposure_per_asset_pct as a hard gate (not just a cap).
        """
        # Propagate external resets (e.g., from the FastAPI container) to this
        # process's in-memory singleton before checking any limits.
        self._maybe_reload_state()
        # ── Resolve effective risk parameters ─────────────────────────────────
        # Per-broker DB overrides take precedence; NULL fields use global config.
        bs = broker_settings or {}
        _risk_per_trade    = (bs["risk_per_trade_pct"] / 100.0)    if bs.get("risk_per_trade_pct")          is not None else self.risk_per_trade_pct
        _max_positions     = int(bs["max_open_positions"])          if bs.get("max_open_positions")          is not None else self.max_open_positions
        _daily_cb_pct      = (bs["daily_circuit_breaker_pct"] / 100.0) if bs.get("daily_circuit_breaker_pct") is not None else self.daily_circuit_breaker_pct
        _max_consec        = int(bs["max_consecutive_losses"])      if bs.get("max_consecutive_losses")     is not None else self.max_consecutive_losses
        _max_asset_exp     = (bs["max_exposure_per_asset_pct"] / 100.0) if bs.get("max_exposure_per_asset_pct") is not None else self.max_exposure_per_asset_pct
        _max_class_exp     = (bs["max_exposure_per_class_pct"] / 100.0) if bs.get("max_exposure_per_class_pct") is not None else self.max_exposure_per_class_pct
        # Use an isolated CB key when the caller requests it (e.g. scalp signals
        # inject "_broker_cb_key"="scalp" so their losses don't share the "binance"
        # CB counter with swing strategies on the same broker).
        _cb_broker = bs.get("_broker_cb_key") or broker
        # BUG-4 FIX: cache the effective per-broker consecutive-loss threshold so
        # record_outcome() uses the same value rather than always the global default.
        if _cb_broker:
            _b = self._per_broker.setdefault(
                _cb_broker,
                {"consecutive_losses": 0, "circuit_breaker_active": False, "circuit_breaker_date": None},
            )
            _b["max_consecutive_losses_effective"] = _max_consec
        # Cache the effective threshold on the per-strategy dict too so record_outcome()
        # trips the strategy CB at the correct level (e.g. scalp=5, not global=3).
        if strategy_name := getattr(signal, "strategy_name", None):
            _ps = self._per_strategy.setdefault(
                strategy_name,
                {"consecutive_losses": 0, "circuit_breaker_active": False, "circuit_breaker_date": None},
            )
            _ps["max_consecutive_losses_effective"] = _max_consec
        # ── Per-broker circuit breaker and consecutive-loss check ──────────────
        if _cb_broker:
            b_state = self._per_broker.get(_cb_broker, {})
            if b_state.get("circuit_breaker_active", False):
                return RiskValidation(
                    approved=False,
                    position_size=0, position_value=0, risk_amount=0, stop_distance=0,
                    reason=(
                        f"Per-broker circuit breaker active for '{_cb_broker}'. "
                        f"Consecutive losses: {b_state.get('consecutive_losses', 0)}. "
                        f"Reset required."
                    ),
                )
            # Also enforce the count directly so broker-specific _max_consec
            # overrides are respected even before record_outcome() trips the CB.
            if b_state.get("consecutive_losses", 0) >= _max_consec:
                return RiskValidation(
                    approved=False,
                    position_size=0, position_value=0, risk_amount=0, stop_distance=0,
                    reason=(
                        f"Consecutive loss limit reached for broker '{_cb_broker}' "
                        f"({b_state.get('consecutive_losses', 0)}/{_max_consec}). "
                        f"Manual reset required."
                    ),
                )

        # ── F-032: Per-strategy circuit breaker ───────────────────────────────
        strategy_name = getattr(signal, "strategy_name", None)
        if strategy_name:
            s_state = self._per_strategy.get(strategy_name, {})
            if s_state.get("circuit_breaker_active", False):
                return RiskValidation(
                    approved=False,
                    position_size=0, position_value=0, risk_amount=0, stop_distance=0,
                    reason=(
                        f"Per-strategy circuit breaker active for '{strategy_name}'. "
                        f"Consecutive losses: {s_state.get('consecutive_losses', 0)}. "
                        f"Reset required."
                    ),
                )
            # Also enforce consecutive-loss limit at the per-strategy level so that
            # broker-specific _max_consec overrides are respected (record_outcome uses
            # the global threshold which may differ from the broker-specific one).
            if s_state.get("consecutive_losses", 0) >= _max_consec:
                return RiskValidation(
                    approved=False,
                    position_size=0, position_value=0, risk_amount=0, stop_distance=0,
                    reason=(
                        f"Per-strategy consecutive loss limit reached for '{strategy_name}' "
                        f"({s_state.get('consecutive_losses', 0)}/{_max_consec}). "
                        f"Manual reset required."
                    ),
                )

        # ── Level 4: Per-broker daily circuit breaker ────────────────────────────
        # Only check daily P&L CB when using the real broker key.
        # When _cb_broker is an isolated virtual key (e.g. "scalp"), the daily_pnl
        # passed in covers the entire real broker — using it against the isolated
        # key would trip the scalp CB purely from swing trade losses.
        # Isolated keys rely on per-strategy + consecutive-loss CBs instead.
        if _cb_broker and _cb_broker == broker and account_balance > 0:
            daily_loss_pct = daily_pnl / account_balance
            if daily_loss_pct <= -_daily_cb_pct:
                b = self._per_broker.setdefault(
                    _cb_broker,
                    {"consecutive_losses": 0, "circuit_breaker_active": False, "circuit_breaker_date": None},
                )
                b["circuit_breaker_active"] = True
                b["circuit_breaker_date"] = str(date.today())
                self._save_state()
                logger.warning(
                    f"[RiskManager] CIRCUIT BREAKER TRIGGERED — "
                    f"broker={_cb_broker} daily loss: {daily_loss_pct*100:.2f}%"
                )
                return RiskValidation(
                    approved=False,
                    position_size=0, position_value=0, risk_amount=0, stop_distance=0,
                    reason=f"Circuit breaker triggered: daily loss {daily_loss_pct*100:.2f}% for broker '{_cb_broker}'"
                )

        # ── Level 3: Open positions limit (effective threshold) ───────────────
        if open_positions_count >= _max_positions:
            return RiskValidation(
                approved=False,
                position_size=0, position_value=0, risk_amount=0, stop_distance=0,
                reason=f"Max open positions ({_max_positions}) reached."
            )

        # ── Level 3: Asset class exposure (effective threshold) ───────────────
        if asset_class_exposure / (account_balance + 1e-10) >= _max_class_exp:
            return RiskValidation(
                approved=False,
                position_size=0, position_value=0, risk_amount=0, stop_distance=0,
                reason=f"Max exposure for {signal.asset_class} reached."
            )

        # ── Level 3: Per-asset exposure gate (BUG-5 FIX) ─────────────────────
        # Reject outright if existing open value in THIS symbol already meets or
        # exceeds the per-asset cap. The position-sizing cap below handles NEW
        # orders within budget, but this gate prevents pyramiding past the limit.
        if asset_exposure / (account_balance + 1e-10) >= _max_asset_exp:
            return RiskValidation(
                approved=False,
                position_size=0, position_value=0, risk_amount=0, stop_distance=0,
                reason=(
                    f"Per-asset exposure limit reached for {signal.symbol} "
                    f"({asset_exposure / (account_balance + 1e-10) * 100:.1f}% "
                    f">= {_max_asset_exp * 100:.1f}%)."
                ),
            )

        # ── Level 1: Check stop loss exists ──────────────────────────────────
        if signal.stop_loss is None:
            return RiskValidation(
                approved=False,
                position_size=0, position_value=0, risk_amount=0, stop_distance=0,
                reason="Signal has no stop loss defined. Rejecting."
            )

        # ── Level 1: Position sizing (broker-specific risk %) ─────────────────
        risk_amount = account_balance * _risk_per_trade

        # ML-confidence scaling: signals with higher blended confidence (rule + ML)
        # receive proportionally more capital.  Maps confidence ∈ [0, 1] → scale ∈ [0.5, 1.0]
        # so even a minimum-confidence signal still risks 50% of the normal amount.
        # confidence=1.0 → 100 % risk; confidence=0.5 → 75 %; confidence=0.0 → 50 %.
        _sig_conf = float(getattr(signal, "confidence", 1.0) or 1.0)
        _conf_scale = 0.5 + 0.5 * max(0.0, min(1.0, _sig_conf))
        risk_amount *= _conf_scale

        stop_distance = abs(signal.entry_price - signal.stop_loss)

        if stop_distance <= 0:
            return RiskValidation(
                approved=False,
                position_size=0, position_value=0, risk_amount=0, stop_distance=0,
                reason="Invalid stop distance (zero or negative)."
            )

        # BUG-CRIT-03 FIX: options position sizing.
        # entry_price for options is the per-share premium (e.g. $2.50).
        # Using risk_amount / stop_distance gives 40 contracts for a 1% risk on $10k,
        # which equals $10,000 premium cost — 100% of account.
        # Correct sizing: use max_loss per CONTRACT (premium × lot_size = $250/contract),
        # so position_size comes out in contracts and position_value in dollars.
        options_meta = getattr(signal, "options_meta", None)
        _is_options = bool(options_meta) or getattr(signal, "asset_class", None) == "option"

        if _is_options and isinstance(options_meta, dict):
            _lot_size = options_meta.get("lot_size", 100)
            _max_loss_per_share = (
                options_meta.get("max_loss")       # iron condor / bull call spread
                or options_meta.get("net_debit")    # debit spread fallback
                or stop_distance                    # generic fallback
            )
            _max_loss_per_contract = float(_max_loss_per_share) * float(_lot_size)
            if _max_loss_per_contract <= 0:
                return RiskValidation(
                    approved=False,
                    position_size=0, position_value=0, risk_amount=0, stop_distance=0,
                    reason="Options max_loss per contract is zero or negative."
                )
            position_size = risk_amount / _max_loss_per_contract  # number of contracts
            position_value = position_size * _max_loss_per_contract  # total dollars at risk
        else:
            position_size = risk_amount / stop_distance
            position_value = position_size * signal.entry_price

        # Cap at effective max-per-asset of account in one asset
        max_position_value = account_balance * _max_asset_exp
        if position_value > max_position_value:
            position_value = max_position_value
            if _is_options and isinstance(options_meta, dict) and _max_loss_per_contract > 0:
                position_size = position_value / _max_loss_per_contract
            else:
                position_size = position_value / signal.entry_price
            risk_amount = position_size * stop_distance
            logger.debug(f"[RiskManager] Position capped to max_exposure_per_asset.")

        # ── Level 1: Minimum R:R check ────────────────────────────────────────
        # Skip for options strategies: premium-selling has inverted R:R by design
        # (iron condor, covered call) and debit spreads use max_profit/net_debit
        # rather than price distance, so the standard check gives misleading results.
        if signal.take_profit is not None and not _is_options:
            reward = abs(signal.take_profit - signal.entry_price)
            rr = round(reward / stop_distance, 2)  # round to 2dp to avoid floating-point edge cases
            if rr < self.default_rr_ratio:  # BUG-2 FIX: use config value, not hardcoded 1.5
                return RiskValidation(
                    approved=False,
                    position_size=0, position_value=0, risk_amount=0, stop_distance=0,
                    reason=f"R:R ratio {rr:.2f} below minimum {self.default_rr_ratio}."
                )

        # ── All checks passed ─────────────────────────────────────────────────
        logger.info(
            f"[RiskManager] ✓ Signal approved | broker={broker or 'any'} | "
            f"size: {position_size:.4f} | value: ${position_value:.2f} | "
            f"max loss: ${risk_amount:.2f}"
        )
        return RiskValidation(
            approved=True,
            position_size=round(position_size, 6),
            position_value=round(position_value, 2),
            risk_amount=round(risk_amount, 2),
            stop_distance=round(stop_distance, 6),
        )

    def reset_circuit_breaker(self):
        """Manually reset the portfolio-wide daily circuit breaker."""
        self._circuit_breaker_active = False
        self._save_state()
        logger.info("[RiskManager] Portfolio circuit breaker reset manually.")

    def reset_strategy_circuit_breaker(self, strategy_name: str) -> None:
        """Manually reset the per-strategy circuit breaker (F-032)."""
        s = self._per_strategy.get(strategy_name)
        if s:
            s["circuit_breaker_active"] = False
            s["consecutive_losses"] = 0
            self._save_state()
        logger.info(f"[RiskManager] Circuit breaker reset for strategy '{strategy_name}'.")

    def reset_broker_circuit_breaker(self, broker: str) -> None:
        """Manually reset the per-broker circuit breaker."""
        b = self._per_broker.get(broker)
        if b:
            b["circuit_breaker_active"] = False
            b["consecutive_losses"] = 0
            self._save_state()
        logger.info(f"[RiskManager] Circuit breaker reset for broker '{broker}'.")

    def reset_broker_consecutive_losses(self, broker: str) -> None:
        """Manually reset the per-broker consecutive-loss counter."""
        b = self._per_broker.get(broker)
        if b:
            b["consecutive_losses"] = 0
            self._save_state()
        logger.info(f"[RiskManager] Consecutive-loss counter reset for broker '{broker}'.")

    def is_circuit_breaker_active(self) -> bool:
        return self._circuit_breaker_active

    def get_broker_state(self, broker: str) -> dict:
        """Return the current per-broker risk state dict."""
        self._load_state()
        return dict(self._per_broker.get(broker, {
            "consecutive_losses": 0,
            "circuit_breaker_active": False,
            "circuit_breaker_date": None,
        }))

    def record_outcome(self, won: bool, strategy_name: str | None = None, broker: str | None = None) -> None:
        """
        Call after each trade resolves.
        Updates portfolio-wide, per-strategy (F-032), and per-broker consecutive-loss counters.
        Trips per-strategy and per-broker circuit breakers when thresholds are hit.
        """
        # ── Portfolio-wide counter ────────────────────────
        if won:
            # GAP-3 FIX: only reset global counter when the win is from the same broker
            # that caused the streak — prevents a win on Alpaca zeroing a Binance losing
            # streak (cross-broker counter contamination).
            _last_loss_broker = getattr(self, "_last_loss_broker", None)
            if self.consecutive_losses > 0 and (
                # Only reset portfolio streak when the win comes from the same broker
                # that caused it, or when the loss was never attributed to a specific
                # broker. A broker=None win must NOT reset a Binance/Alpaca streak.
                _last_loss_broker is None or broker == _last_loss_broker
            ):
                logger.info(
                    f"[RiskManager] Win — resetting portfolio consecutive_losses "
                    f"(was {self.consecutive_losses})"
                )
                self.consecutive_losses = 0
                self._last_loss_broker = None
        else:
            self.consecutive_losses += 1
            self._last_loss_broker = broker
            logger.warning(
                f"[RiskManager] Loss — portfolio consecutive_losses={self.consecutive_losses}"
            )

        # F-032: Per-strategy counter + circuit breaker ───
        if strategy_name:
            s = self._per_strategy.setdefault(
                strategy_name,
                {"consecutive_losses": 0, "circuit_breaker_active": False, "circuit_breaker_date": None},
            )
            if won:
                if s["consecutive_losses"] > 0:
                    logger.info(
                        f"[RiskManager] Win on '{strategy_name}' — resetting its "
                        f"consecutive_losses (was {s['consecutive_losses']})"
                    )
                s["consecutive_losses"] = 0
                s["circuit_breaker_active"] = False
            else:
                s["consecutive_losses"] += 1
                logger.warning(
                    f"[RiskManager] Loss on '{strategy_name}' — "
                    f"consecutive_losses={s['consecutive_losses']}"
                )
                # Use the effective threshold cached by validate() so scalp strategies
                # (max_consec=5) don't trip at the global default (3).
                _strat_max_consec = s.get("max_consecutive_losses_effective", self.max_consecutive_losses)
                if s["consecutive_losses"] >= _strat_max_consec:
                    s["circuit_breaker_active"] = True
                    s["circuit_breaker_date"] = str(date.today())
                    logger.warning(
                        f"[RiskManager] 🔴 PER-STRATEGY CIRCUIT BREAKER: '{strategy_name}' "
                        f"halted after {s['consecutive_losses']} consecutive losses."
                    )

        # Per-broker counter + circuit breaker
        if broker:
            b = self._per_broker.setdefault(
                broker,
                {"consecutive_losses": 0, "circuit_breaker_active": False, "circuit_breaker_date": None},
            )
            if won:
                if b["consecutive_losses"] > 0:
                    logger.info(
                        f"[RiskManager] Win on broker '{broker}' — resetting its "
                        f"consecutive_losses (was {b['consecutive_losses']})"
                    )
                b["consecutive_losses"] = 0
                b["circuit_breaker_active"] = False
            else:
                b["consecutive_losses"] += 1
                logger.warning(
                    f"[RiskManager] Loss on broker '{broker}' — "
                    f"consecutive_losses={b['consecutive_losses']}"
                )
                # BUG-4 FIX: use broker-specific threshold if cached by validate(), else global
                _broker_max_consec = b.get("max_consecutive_losses_effective", self.max_consecutive_losses)
                if b["consecutive_losses"] >= _broker_max_consec:
                    b["circuit_breaker_active"] = True
                    b["circuit_breaker_date"] = str(date.today())
                    logger.warning(
                        f"[RiskManager] 🔴 PER-BROKER CIRCUIT BREAKER: '{broker}' "
                        f"halted after {b['consecutive_losses']} consecutive losses."
                    )

        self._save_state()

    def reset_consecutive_losses(self) -> None:
        """Manually reset the consecutive-loss counter (e.g., after manual review)."""
        self.consecutive_losses = 0
        self._save_state()
        logger.info("[RiskManager] Consecutive-loss counter reset manually.")

    def maybe_reset_on_regime_change(self, strategy_name: str, current_regime: str) -> bool:
        """
        IMP-05: Regime-aware circuit-breaker reset.

        If a per-strategy CB was tripped while the regime was `high_volatility`
        and the current regime has since normalised (is no longer high_volatility),
        reset the CB automatically — the losses were regime-driven, not
        strategy-driven, and blocking the strategy indefinitely hurts edge.

        Returns True if a reset was performed, False otherwise.
        """
        _HIGH_VOL = "high_volatility"
        s = self._per_strategy.get(strategy_name)
        if not s:
            return False
        if not s.get("circuit_breaker_active", False):
            return False
        if current_regime == _HIGH_VOL:
            return False  # regime still hostile — keep CB active
        # CB is active AND regime has normalised → auto-reset
        s["circuit_breaker_active"] = False
        s["consecutive_losses"] = 0
        self._save_state()
        logger.info(
            f"[RiskManager] IMP-05 — auto-reset CB for '{strategy_name}' "
            f"because regime normalised to '{current_regime}' (was high_volatility)."
        )
        return True