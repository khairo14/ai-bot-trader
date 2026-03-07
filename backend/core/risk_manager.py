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
            logger.info(
                f"[RiskManager] State loaded — circuit_breaker={self._circuit_breaker_active} "
                f"consecutive_losses={self.consecutive_losses} "
                f"per_strategy_count={len(self._per_strategy)}"
            )
        except Exception as exc:
            logger.warning(f"[RiskManager] Could not load risk state: {exc}")

    def _save_state(self) -> None:
        """Persist circuit-breaker and consecutive-loss state to disk atomically.
        Uses write-to-temp-then-rename to avoid partial writes corrupting the state
        file when FastAPI and Celery workers both call _save_state concurrently.
        """
        try:
            import tempfile
            os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
            payload = json.dumps(
                {
                    "circuit_breaker_active": self._circuit_breaker_active,
                    "circuit_breaker_date": str(date.today()),
                    "consecutive_losses": self.consecutive_losses,
                    "per_strategy": self._per_strategy,  # F-032
                }
            )
            # Write to a sibling temp file, then atomically rename
            dir_ = os.path.dirname(_STATE_FILE)
            with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp") as tmp:
                tmp.write(payload)
                tmp_path = tmp.name
            os.replace(tmp_path, _STATE_FILE)
        except Exception as exc:
            logger.warning(f"[RiskManager] Could not save risk state: {exc}")

    def validate(
        self,
        signal: Signal,
        account_balance: float,
        open_positions_count: int,
        daily_pnl: float,
        asset_class_exposure: float = 0.0,
    ) -> RiskValidation:
        """
        Validate a signal before execution.
        Returns RiskValidation with approved=True/False and calculated position size.
        """
        # ── F-032: Per-strategy circuit breaker (checked before portfolio-wide) ──
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

        # ── Level 4: Portfolio-wide circuit breaker ───────
        if self._circuit_breaker_active:
            return RiskValidation(
                approved=False,
                position_size=0, position_value=0, risk_amount=0, stop_distance=0,
                reason="Daily circuit breaker is active. Reset required."
            )

        if account_balance > 0:
            daily_loss_pct = daily_pnl / account_balance
            if daily_loss_pct <= -self.daily_circuit_breaker_pct:
                self._circuit_breaker_active = True
                self._save_state()
                logger.warning(
                    f"[RiskManager] CIRCUIT BREAKER TRIGGERED — "
                    f"daily loss: {daily_loss_pct*100:.2f}%"
                )
                return RiskValidation(
                    approved=False,
                    position_size=0, position_value=0, risk_amount=0, stop_distance=0,
                    reason=f"Circuit breaker triggered: daily loss {daily_loss_pct*100:.2f}%"
                )

        # ── Level 2: Consecutive losses ──────────────────
        if self.consecutive_losses >= self.max_consecutive_losses:
            return RiskValidation(
                approved=False,
                position_size=0, position_value=0, risk_amount=0, stop_distance=0,
                reason=(
                    f"Consecutive loss limit reached ({self.consecutive_losses}/"
                    f"{self.max_consecutive_losses}). Manual reset required."
                ),
            )

        # ── Level 3: Open positions limit ────────────────
        if open_positions_count >= self.max_open_positions:
            return RiskValidation(
                approved=False,
                position_size=0, position_value=0, risk_amount=0, stop_distance=0,
                reason=f"Max open positions ({self.max_open_positions}) reached."
            )

        # ── Level 3: Asset class exposure ────────────────
        if asset_class_exposure / (account_balance + 1e-10) >= self.max_exposure_per_class_pct:
            return RiskValidation(
                approved=False,
                position_size=0, position_value=0, risk_amount=0, stop_distance=0,
                reason=f"Max exposure for {signal.asset_class} reached."
            )

        # ── Level 1: Check stop loss exists ──────────────
        if signal.stop_loss is None:
            return RiskValidation(
                approved=False,
                position_size=0, position_value=0, risk_amount=0, stop_distance=0,
                reason="Signal has no stop loss defined. Rejecting."
            )

        # ── Level 1: Position sizing ─────────────────────
        risk_amount = account_balance * self.risk_per_trade_pct
        stop_distance = abs(signal.entry_price - signal.stop_loss)

        if stop_distance <= 0:
            return RiskValidation(
                approved=False,
                position_size=0, position_value=0, risk_amount=0, stop_distance=0,
                reason="Invalid stop distance (zero or negative)."
            )

        position_size = risk_amount / stop_distance
        position_value = position_size * signal.entry_price

        # Cap at 15% of account in one asset
        max_position_value = account_balance * self.max_exposure_per_asset_pct
        if position_value > max_position_value:
            position_value = max_position_value
            position_size = position_value / signal.entry_price
            risk_amount = position_size * stop_distance
            logger.debug(f"[RiskManager] Position capped to max_exposure_per_asset.")

        # ── Level 1: Minimum R:R check ───────────────────
        if signal.take_profit:
            reward = abs(signal.take_profit - signal.entry_price)
            rr = reward / stop_distance
            if rr < 1.5:
                return RiskValidation(
                    approved=False,
                    position_size=0, position_value=0, risk_amount=0, stop_distance=0,
                    reason=f"R:R ratio {rr:.2f} below minimum 1.5."
                )

        # ── All checks passed ────────────────────────────
        logger.info(
            f"[RiskManager] ✓ Signal approved | "
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

    def is_circuit_breaker_active(self) -> bool:
        return self._circuit_breaker_active

    def record_outcome(self, won: bool, strategy_name: str | None = None) -> None:
        """
        Call after each trade resolves.
        Updates both portfolio-wide and per-strategy (F-032) consecutive-loss counters.
        Trips per-strategy circuit breaker when that strategy hits max_consecutive_losses.
        """
        # ── Portfolio-wide counter ────────────────────────
        if won:
            if self.consecutive_losses > 0:
                logger.info(
                    f"[RiskManager] Win — resetting portfolio consecutive_losses "
                    f"(was {self.consecutive_losses})"
                )
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
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
                if s["consecutive_losses"] >= self.max_consecutive_losses:
                    s["circuit_breaker_active"] = True
                    s["circuit_breaker_date"] = str(date.today())
                    logger.warning(
                        f"[RiskManager] 🔴 PER-STRATEGY CIRCUIT BREAKER: '{strategy_name}' "
                        f"halted after {s['consecutive_losses']} consecutive losses."
                    )

        self._save_state()

    def reset_consecutive_losses(self) -> None:
        """Manually reset the consecutive-loss counter (e.g., after manual review)."""
        self.consecutive_losses = 0
        self._save_state()
        logger.info("[RiskManager] Consecutive-loss counter reset manually.")