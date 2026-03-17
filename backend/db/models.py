from __future__ import annotations
from datetime import datetime, timezone
from typing import List, Optional

# All DateTime columns are TIMESTAMP WITHOUT TIME ZONE — use tz-naive UTC datetimes
def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
from sqlalchemy import (
    String, Float, Integer, Boolean,
    DateTime, Text, Enum as SAEnum, ForeignKey, JSON
)
from sqlalchemy.orm import relationship, Mapped, mapped_column
import enum

from db.database import Base


class SignalType(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"
    SHORT = "SHORT"
    COVER = "COVER"
    HOLD = "HOLD"


class ExecutionMode(str, enum.Enum):
    SUGGESTION = "suggestion"
    SEMI_AUTO = "semi-auto"
    FULL_AUTO = "full-auto"


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class AssetClass(str, enum.Enum):
    CRYPTO = "crypto"
    STOCK = "stock"
    OPTION = "option"
    FOREX = "forex"


class BrokerName(str, enum.Enum):
    BINANCE = "binance"
    ALPACA = "alpaca"
    IBKR = "ibkr"


class OutcomeResult(str, enum.Enum):
    WIN = "win"
    LOSS = "loss"
    BREAK_EVEN = "break_even"
    EXPIRED = "expired"    # horizon passed, no SL/TP hit — measured return


class NotificationLevel(str, enum.Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


class NotificationCategory(str, enum.Enum):
    SIGNAL = "signal"
    TRADE = "trade"
    EMERGENCY = "emergency"
    SYSTEM = "system"
    ML = "ml"


# ─────────────────────────────────────────────────────────
# Signals
# ─────────────────────────────────────────────────────────
class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    signal: Mapped[SignalType] = mapped_column(SAEnum(SignalType), nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    take_profit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # 0.0 – 1.0
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    strategy_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)  # STRATEGY_REGISTRY key
    regime: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    asset_class: Mapped[AssetClass] = mapped_column(SAEnum(AssetClass), nullable=False)
    broker: Mapped[BrokerName] = mapped_column(SAEnum(BrokerName), nullable=False)
    execution_mode: Mapped[Optional[ExecutionMode]] = mapped_column(SAEnum(ExecutionMode), nullable=True)  # suggestion | semi-auto | full-auto
    reasons: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # list of reason strings
    acted_on: Mapped[bool] = mapped_column(Boolean, default=False)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False)  # hidden from Recent Signals UI
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False, index=True)
    # ── Options-specific fields (NULL for equity / crypto signals) ───────────
    iv_rank: Mapped[Optional[float]] = mapped_column(Float, nullable=True)   # 0–100
    delta:   Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    theta:   Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vega:    Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    options_meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True) # strikes, expiry, legs
    user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)  # owner (F-059)

    trade: Mapped[Optional[Trade]] = relationship("Trade", back_populates="signal", uselist=False)
    live_trade: Mapped[Optional["LiveTrade"]] = relationship("LiveTrade", back_populates="signal", uselist=False)
    outcome: Mapped[Optional[TradeOutcome]] = relationship("TradeOutcome", back_populates="signal", uselist=False)


# ─────────────────────────────────────────────────────────
# Trade Outcomes (ML feedback loop)
# ─────────────────────────────────────────────────────────
class TradeOutcome(Base):
    """Records the resolved outcome of a BUY/SELL signal for ML retraining.

    Created immediately when a non-HOLD signal fires (resolved=False).
    The nightly outcome_resolver task walks OHLCV, detects SL/TP hits or
    measures the 24-candle forward return, and marks resolved=True.
    """
    __tablename__ = "trade_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    signal_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("signals.id"), nullable=True, unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(10), nullable=False)   # BUY / SELL / SHORT
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    take_profit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trailing_stop_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)   # e.g. 2.0 → trail by 2%
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    outcome: Mapped[Optional[OutcomeResult]] = mapped_column(
        SAEnum(OutcomeResult, values_callable=lambda x: [e.value for e in x]),
        nullable=True,
    )
    pnl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)   # % gain/loss
    candles_held: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ml_label: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)   # 1=win, 0=loss (for retraining)
    # GAP-06: options trade metadata (strikes, expiry, legs) for options ML labels
    options_meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # IMP-04: category for ML label disambiguation — "directional" or "premium_collection"
    signal_type_category: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True)   # False = live trade outcome
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=_utcnow, index=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    signal: Mapped[Optional[Signal]] = relationship("Signal", back_populates="outcome")


# ─────────────────────────────────────────────────────────
# Trades
# ─────────────────────────────────────────────────────────
class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    signal_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("signals.id"), nullable=True, unique=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(10), nullable=False)      # buy | sell | short | cover
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    take_profit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trailing_stop_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # e.g. 1.5 → trail by 1.5%
    pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[OrderStatus] = mapped_column(SAEnum(OrderStatus), default=OrderStatus.PENDING)
    execution_mode: Mapped[ExecutionMode] = mapped_column(SAEnum(ExecutionMode), nullable=False)
    broker: Mapped[BrokerName] = mapped_column(SAEnum(BrokerName), nullable=False, index=True)
    asset_class: Mapped[AssetClass] = mapped_column(SAEnum(AssetClass), nullable=False)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True)
    broker_order_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    strategy_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    notes: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)  # rejection reason, halt info, etc.
    opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=_utcnow)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)  # owner (F-059)

    signal: Mapped[Optional[Signal]] = relationship("Signal", back_populates="trade")


# ─────────────────────────────────────────────────────────
# Live Trades  (separate table — real-money execution)
# ─────────────────────────────────────────────────────────
class LiveTrade(Base):
    """Real-money trades.  Mirrors Trade columns exactly; stored in a separate
    table so paper and live records are never mixed, and each can have different
    retention/archival policies in the future."""
    __tablename__ = "live_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    signal_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("signals.id"), nullable=True, unique=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(10), nullable=False)      # buy | sell | short | cover
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    take_profit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trailing_stop_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[OrderStatus] = mapped_column(SAEnum(OrderStatus), default=OrderStatus.PENDING)
    execution_mode: Mapped[ExecutionMode] = mapped_column(SAEnum(ExecutionMode), nullable=False)
    broker: Mapped[BrokerName] = mapped_column(SAEnum(BrokerName), nullable=False, index=True)
    asset_class: Mapped[AssetClass] = mapped_column(SAEnum(AssetClass), nullable=False)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=False)
    broker_order_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    strategy_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    notes: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=_utcnow)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    signal: Mapped[Optional[Signal]] = relationship("Signal", back_populates="live_trade")


# ─────────────────────────────────────────────────────────
# Strategies
# ─────────────────────────────────────────────────────────
class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    asset_class: Mapped[AssetClass] = mapped_column(SAEnum(AssetClass), nullable=False)
    broker: Mapped[BrokerName] = mapped_column(SAEnum(BrokerName), nullable=False)
    execution_mode: Mapped[ExecutionMode] = mapped_column(SAEnum(ExecutionMode), default=ExecutionMode.SUGGESTION)
    parameters: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)   # strategy-specific config
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


# ─────────────────────────────────────────────────────────
# Backtest Results
# ─────────────────────────────────────────────────────────
class BacktestResult(Base):
    __tablename__ = "backtest_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    strategy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False)
    start_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    initial_capital: Mapped[float] = mapped_column(Float, nullable=False)
    final_capital: Mapped[float] = mapped_column(Float, nullable=False)
    total_return_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    annualized_return_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_drawdown_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sharpe_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sortino_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    profit_factor: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    win_rate_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_trades: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    avg_win: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    avg_loss: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rr_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    trades_detail: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)   # full list of trades
    parameters: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=_utcnow)


# ─────────────────────────────────────────────────────────
# In-App Notifications
# ─────────────────────────────────────────────────────────
class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    level: Mapped[NotificationLevel] = mapped_column(
        SAEnum(NotificationLevel, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False, default=NotificationLevel.INFO,
    )
    category: Mapped[NotificationCategory] = mapped_column(
        SAEnum(NotificationCategory, values_callable=lambda obj: [e.value for e in obj]),
        nullable=False, default=NotificationCategory.SYSTEM,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    email_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    extra_data: Mapped[Optional[dict]] = mapped_column("metadata", JSON, nullable=True)   # symbol, broker, trade_id, etc.
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)


# ─────────────────────────────────────────────────────────
# Users (Auth)
# ─────────────────────────────────────────────────────────
class User(Base):
    """Dashboard user account. Passwords are bcrypt-hashed."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=_utcnow)


# ─────────────────────────────────────────────────────────
# Per-broker Risk Settings
# ─────────────────────────────────────────────────────────
class BrokerRiskSettings(Base):
    """
    Per-broker overrides for risk parameters.
    One row per broker (binance / alpaca / ibkr).
    Any NULL field means "use the global config default".
    Editable from the Settings page without restarting Docker.
    """
    __tablename__ = "broker_risk_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    broker: Mapped[BrokerName] = mapped_column(SAEnum(BrokerName), nullable=False, unique=True, index=True)
    risk_per_trade_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)         # e.g. 2.0 → 2%
    max_open_positions: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    daily_circuit_breaker_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # e.g. 5.0 → -5%
    max_consecutive_losses: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_exposure_per_asset_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True) # e.g. 15.0 → 15%
    max_exposure_per_class_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True) # e.g. 40.0 → 40%
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
