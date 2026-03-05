import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional
from loguru import logger

from brokers import get_broker
from core.engine.signal_engine import SignalEngine
from core.strategies.base import Signal


class BacktestEngine:
    """
    Replays historical OHLCV data candle-by-candle through a strategy.
    Simulates realistic order fills with configurable slippage and fees.
    Outputs full performance metrics.
    """

    def __init__(self):
        self.signal_engine = SignalEngine()

    async def run(
        self,
        strategy_name: str,
        symbol: str,
        timeframe: str,
        start_date: datetime,
        end_date: datetime,
        initial_capital: float = 10000.0,
        commission_pct: float = 0.1,
        slippage_pct: float = 0.05,
        risk_per_trade_pct: float = 2.0,
        broker: str = "binance",
        parameters: Optional[dict] = None,
    ) -> dict:
        """Run a full backtest and return performance metrics."""

        logger.info(f"[Backtest] Starting: {strategy_name} | {symbol} | {timeframe} | {start_date.date()} → {end_date.date()}")

        # Fetch full historical data across the requested date range (paginated).
        # Always use the live (non-testnet) client for data — testnet has no 
        # OHLCV history. OHLCV is a public endpoint; no API key needed.
        broker_client = get_broker(broker, force_paper=False)
        try:
            df = await broker_client.get_ohlcv_range(symbol, timeframe, start_date, end_date)
        finally:
            # Always close the async session (ccxt uses aiohttp — must be released
            # or subsequent requests will hit a closed connector error).
            await broker_client.close()

        if len(df) < 50:
            return {"error": f"Insufficient data: {len(df)} candles"}

        # Warm-up period (first 50 candles for indicator calculation)
        warmup = 50
        trades = []
        capital = initial_capital
        position = None
        equity_curve = [capital]
        commission = commission_pct / 100
        slippage = slippage_pct / 100

        def _ts(val) -> str:
            """Convert a pandas Timestamp or datetime to ISO string."""
            try:
                return val.isoformat()
            except Exception:
                return str(val)

        for i in range(warmup, len(df)):
            window = df.iloc[:i]
            current_candle = df.iloc[i]
            current_price = float(current_candle["close"])

            # Check for exit if in position
            if position:
                exit_signal = False
                exit_price = current_price

                # Stop loss hit
                if position["side"] == "BUY" and current_price <= position["stop_loss"]:
                    exit_price = position["stop_loss"]
                    exit_signal = True
                    exit_reason = "stop_loss"
                elif position["side"] == "SHORT" and current_price >= position["stop_loss"]:
                    exit_price = position["stop_loss"]
                    exit_signal = True
                    exit_reason = "stop_loss"
                # Take profit hit
                elif position["side"] == "BUY" and current_price >= position["take_profit"]:
                    exit_price = position["take_profit"]
                    exit_signal = True
                    exit_reason = "take_profit"
                elif position["side"] == "SHORT" and current_price <= position["take_profit"]:
                    exit_price = position["take_profit"]
                    exit_signal = True
                    exit_reason = "take_profit"

                if exit_signal:
                    # Apply slippage on exit
                    if position["side"] == "BUY":
                        fill_exit = exit_price * (1 - slippage)
                        pnl = (fill_exit - position["fill_price"]) * position["quantity"]
                    else:
                        fill_exit = exit_price * (1 + slippage)
                        pnl = (position["fill_price"] - fill_exit) * position["quantity"]

                    # Apply commission on exit
                    pnl -= (fill_exit * position["quantity"] * commission)
                    capital += pnl

                    trades.append({
                        "entry_time": position["entry_time"],   # already ISO string from _ts()
                        "exit_time": _ts(current_candle.name),
                        "symbol": symbol,
                        "side": position["side"],
                        "entry_price": position["fill_price"],
                        "exit_price": fill_exit,
                        "quantity": position["quantity"],
                        "pnl": round(pnl, 4),
                        "pnl_pct": round(pnl / initial_capital * 100, 4),
                        "exit_reason": exit_reason,
                    })
                    position = None

            # Generate new signal if not in position
            if position is None and capital > 0:
                signal = self.signal_engine.get_strategy(strategy_name).generate_signal(
                    window, symbol=symbol, timeframe=timeframe, **(parameters or {})
                )

                if signal.signal in ("BUY", "SHORT") and signal.stop_loss and signal.take_profit:
                    risk_amount = capital * (risk_per_trade_pct / 100)
                    stop_dist = abs(current_price - signal.stop_loss)
                    if stop_dist > 0:
                        qty = risk_amount / stop_dist
                        fill_price = current_price * (1 + slippage if signal.signal == "BUY" else 1 - slippage)
                        cost = fill_price * qty * commission
                        capital -= cost

                        position = {
                            "side": signal.signal,
                            "fill_price": fill_price,
                            "stop_loss": signal.stop_loss,
                            "take_profit": signal.take_profit,
                            "quantity": qty,
                            "entry_time": _ts(current_candle.name),
                        }

            equity_curve.append(capital)

        # ── Performance Metrics ──────────────────────────
        def _safe(v, default: float = 0.0) -> float:
            """Return float v, replacing NaN / inf / complex with default."""
            try:
                f = float(v.real if hasattr(v, "real") else v)
                return default if (f != f or abs(f) == float("inf")) else f
            except Exception:
                return default

        # Floor capital at 0 — negative capital causes complex-number errors in
        # power expressions like (capital/initial_capital) ** 0.5 when cap < 0.
        capital = max(capital, 0.0)

        total_return_pct = (capital - initial_capital) / initial_capital * 100
        n_trades = len(trades)
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        win_rate = len(wins) / n_trades * 100 if n_trades > 0 else 0
        avg_win = _safe(np.mean([t["pnl"] for t in wins])) if wins else 0.0
        avg_loss = _safe(abs(np.mean([t["pnl"] for t in losses]))) if losses else 0.0
        profit_factor = _safe(
            sum(t["pnl"] for t in wins) / (abs(sum(t["pnl"] for t in losses)) + 1e-10)
        )

        # Max drawdown — replace 0/negative equity with tiny epsilon to avoid /0
        safe_eq = pd.Series([max(e, 1e-6) for e in equity_curve])
        rolling_max = safe_eq.cummax()
        drawdown = (safe_eq - rolling_max) / rolling_max * 100
        max_drawdown = _safe(drawdown.min(), default=-100.0)

        # Sharpe ratio — replace inf from pct_change on near-zero equity
        if len(equity_curve) > 1:
            returns = safe_eq.pct_change().dropna().replace([np.inf, -np.inf], 0.0)
            sharpe = _safe(returns.mean() / (returns.std() + 1e-10) * np.sqrt(252))
            # Sortino ratio — only penalise downside volatility
            downside = returns[returns < 0]
            downside_std = _safe(downside.std(), default=0.0)
            sortino = _safe(returns.mean() / (downside_std + 1e-10) * np.sqrt(252))
        else:
            sharpe = 0.0
            sortino = 0.0

        days = max((end_date - start_date).days, 1)
        if capital <= 0:
            annualized_return = -100.0
        else:
            annualized_return = _safe(
                ((capital / initial_capital) ** (365 / days) - 1) * 100,
                default=-100.0,
            )

        result = {
            "strategy_name": strategy_name,
            "symbol": symbol,
            "timeframe": timeframe,
            "start_date": start_date,
            "end_date": end_date,
            "initial_capital": initial_capital,
            "final_capital": round(capital, 2),
            "total_return_pct": round(total_return_pct, 4),
            "annualized_return_pct": round(annualized_return, 4),
            "max_drawdown_pct": round(max_drawdown, 4),
            "sharpe_ratio": round(sharpe, 4),
            "sortino_ratio": round(sortino, 4),
            "profit_factor": round(profit_factor, 4),
            "win_rate_pct": round(win_rate, 4),
            "total_trades": n_trades,
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "rr_ratio": round(_safe(avg_win / (avg_loss + 1e-10)), 4),
            "trades_detail": trades,
            "parameters": parameters or {},
        }

        logger.info(
            f"[Backtest] Done: {n_trades} trades | "
            f"Win: {win_rate:.1f}% | Return: {total_return_pct:.2f}% | "
            f"Max DD: {max_drawdown:.2f}% | Sharpe: {sharpe:.2f} | Sortino: {sortino:.2f}"
        )
        return result
