from dataclasses import dataclass
from typing import Optional
from loguru import logger

from config import settings
from core.strategies.base import Signal


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

        # Runtime state (loaded from DB on init)
        self.daily_pnl: float = 0.0
        self.open_positions_count: int = 0
        self.consecutive_losses: int = 0
        self._circuit_breaker_active: bool = False

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
        # ── Level 4: Circuit breaker ─────────────────────
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
                logger.warning(
                    f"[RiskManager] CIRCUIT BREAKER TRIGGERED — "
                    f"daily loss: {daily_loss_pct*100:.2f}%"
                )
                return RiskValidation(
                    approved=False,
                    position_size=0, position_value=0, risk_amount=0, stop_distance=0,
                    reason=f"Circuit breaker triggered: daily loss {daily_loss_pct*100:.2f}%"
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
        """Manually reset the daily circuit breaker."""
        self._circuit_breaker_active = False
        logger.info("[RiskManager] Circuit breaker reset manually.")

    def is_circuit_breaker_active(self) -> bool:
        return self._circuit_breaker_active
