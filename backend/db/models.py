from __future__ import annotations
from datetime import datetime
from typing import List, Optional
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
    regime: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    asset_class: Mapped[AssetClass] = mapped_column(SAEnum(AssetClass), nullable=False)
    broker: Mapped[BrokerName] = mapped_column(SAEnum(BrokerName), nullable=False)
    execution_mode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # suggestion | semi-auto | full-auto
    reasons: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)  # list of reason strings
    acted_on: Mapped[bool] = mapped_column(Boolean, default=False)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False)  # hidden from Recent Signals UI
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)

    trade: Mapped[Optional[Trade]] = relationship("Trade", back_populates="signal", uselist=False)
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
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    outcome: Mapped[Optional[OutcomeResult]] = mapped_column(SAEnum(OutcomeResult), nullable=True)
    pnl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)   # % gain/loss
    candles_held: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ml_label: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)   # 1=win, 0=loss (for retraining)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    signal: Mapped[Optional[Signal]] = relationship("Signal", back_populates="outcome")


# ─────────────────────────────────────────────────────────
# Trades
# ─────────────────────────────────────────────────────────
class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    signal_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("signals.id"), nullable=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(10), nullable=False)      # buy | sell | short | cover
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    take_profit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[OrderStatus] = mapped_column(SAEnum(OrderStatus), default=OrderStatus.PENDING)
    execution_mode: Mapped[ExecutionMode] = mapped_column(SAEnum(ExecutionMode), nullable=False)
    broker: Mapped[BrokerName] = mapped_column(SAEnum(BrokerName), nullable=False)
    asset_class: Mapped[AssetClass] = mapped_column(SAEnum(AssetClass), nullable=False)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True)
    broker_order_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    strategy_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)  # rejection reason, halt info, etc.
    opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)

    signal: Mapped[Optional[Signal]] = relationship("Signal", back_populates="trade")


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
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────────────────
# In-App Notifications
# ─────────────────────────────────────────────────────────
class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    level: Mapped[NotificationLevel] = mapped_column(SAEnum(NotificationLevel), nullable=False, default=NotificationLevel.INFO)
    category: Mapped[NotificationCategory] = mapped_column(SAEnum(NotificationCategory), nullable=False, default=NotificationCategory.SYSTEM)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    email_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    extra: Mapped[Optional[dict]] = mapped_column("metadata", JSON, nullable=True)   # symbol, broker, trade_id, etc.
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
