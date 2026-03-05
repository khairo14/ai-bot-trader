"""
ML Feedback Loop — Trade Outcome Resolver
==========================================
Nightly task that resolves pending TradeOutcome rows.

For each unresolved TradeOutcome (created when a BUY/SELL signal fired):
1.  Fetch OHLCV for the symbol from the signal date up to today (yfinance daily
    for crypto/stocks — good enough for 1d/4h resolution; actual intraday ticks
    are not needed to measure multi-candle outcomes).
2.  Walk candles forward from the entry candle:
    - If take_profit is set and high touches it → WIN
    - If stop_loss is set and low touches it → LOSS (or breakeven)
    - After HORIZON candles with no hit → EXPIRED (measure actual return)
3.  Update the row: outcome, pnl_pct, exit_price, candles_held, ml_label,
    resolved=True, resolved_at=now.
4.  Return summary for logging / API status.

ml_label: 1 = WIN, 0 = LOSS/BREAK_EVEN/EXPIRED-negative
"""

from __future__ import annotations
import asyncio
import datetime
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# outcome horizon in candles (24 = ~1 day of 1h candles, or 3 weeks of daily)
RESOLUTION_HORIZON = 24
# minimum age before we attempt resolution (allow market to move)
MIN_AGE_HOURS = 4


# ─────────────────────────────────────────────────────────────
# yfinance OHLCV helper (broker-agnostic historical resolver)
# ─────────────────────────────────────────────────────────────
def _symbol_to_yf(symbol: str) -> str:
    if "/" in symbol:
        base, quote = symbol.split("/", 1)
        quote_yf = "USD" if quote in ("USDT", "USDC", "BUSD") else quote
        return f"{base}-{quote_yf}"
    return symbol


def _fetch_ohlcv_since(symbol: str, since: datetime.datetime, days: int = 60):
    """Fetch daily OHLCV from `since` up to today. Returns DataFrame or None."""
    try:
        import yfinance as yf
        import pandas as pd

        ticker = _symbol_to_yf(symbol)
        start = (since - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        end = (datetime.datetime.utcnow() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        df = yf.download(ticker, start=start, end=end, interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        df = df.rename(columns=str.lower)
        if isinstance(df.columns, __import__("pandas").MultiIndex):
            df.columns = [c[0] for c in df.columns]
        df.index = __import__("pandas").to_datetime(df.index).tz_localize(None)
        return df[["open", "high", "low", "close", "volume"]].dropna()
    except Exception as exc:
        logger.warning(f"[resolver] yfinance fetch failed for {symbol}: {exc}")
        return None


def _resolve_outcome(
    entry_price: float,
    stop_loss: Optional[float],
    take_profit: Optional[float],
    signal_type: str,
    df,           # OHLCV DataFrame sliced from entry candle onwards
    horizon: int = RESOLUTION_HORIZON,
) -> dict:
    """
    Walk rows of `df` (starting at entry candle) to determine outcome.
    Returns dict with keys: outcome, pnl_pct, exit_price, candles_held, ml_label
    """
    is_long = signal_type in ("BUY",)
    is_short = signal_type in ("SELL", "SHORT")

    for i, (ts, row) in enumerate(df.iterrows()):
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])

        # Check take profit first (optimistic — assume best intra-candle fill)
        if take_profit is not None:
            if is_long and high >= take_profit:
                pnl_pct = round((take_profit - entry_price) / entry_price * 100, 4)
                return {
                    "outcome": "win",
                    "pnl_pct": pnl_pct,
                    "exit_price": take_profit,
                    "candles_held": i + 1,
                    "ml_label": 1,
                }
            if is_short and low <= take_profit:
                pnl_pct = round((entry_price - take_profit) / entry_price * 100, 4)
                return {
                    "outcome": "win",
                    "pnl_pct": pnl_pct,
                    "exit_price": take_profit,
                    "candles_held": i + 1,
                    "ml_label": 1,
                }

        # Check stop loss
        if stop_loss is not None:
            if is_long and low <= stop_loss:
                pnl_pct = round((stop_loss - entry_price) / entry_price * 100, 4)
                outcome = "break_even" if abs(pnl_pct) < 0.1 else "loss"
                return {
                    "outcome": outcome,
                    "pnl_pct": pnl_pct,
                    "exit_price": stop_loss,
                    "candles_held": i + 1,
                    "ml_label": 0,
                }
            if is_short and high >= stop_loss:
                pnl_pct = round((entry_price - stop_loss) / entry_price * 100, 4)
                outcome = "break_even" if abs(pnl_pct) < 0.1 else "loss"
                return {
                    "outcome": outcome,
                    "pnl_pct": pnl_pct,
                    "exit_price": stop_loss,
                    "candles_held": i + 1,
                    "ml_label": 0,
                }

        # Horizon reached — measure actual return
        if i + 1 >= horizon:
            if is_long:
                pnl_pct = round((close - entry_price) / entry_price * 100, 4)
            else:
                pnl_pct = round((entry_price - close) / entry_price * 100, 4)
            return {
                "outcome": "expired",
                "pnl_pct": pnl_pct,
                "exit_price": close,
                "candles_held": horizon,
                "ml_label": 1 if pnl_pct > 0 else 0,
            }

    # Not enough candles yet — cannot resolve
    return {}


# ─────────────────────────────────────────────────────────────
# Async resolver
# ─────────────────────────────────────────────────────────────
async def resolve_pending_outcomes() -> dict:
    """Main async entry point. Returns summary dict."""
    from db.database import AsyncSessionLocal
    from db.models import TradeOutcome, OutcomeResult
    from sqlalchemy import select

    now = datetime.datetime.utcnow()
    cutoff = now - datetime.timedelta(hours=MIN_AGE_HOURS)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(TradeOutcome).where(
                TradeOutcome.resolved == False,  # noqa: E712
                TradeOutcome.created_at <= cutoff,
            )
        )
        pending = result.scalars().all()

    if not pending:
        logger.info("[resolver] No pending outcomes to resolve.")
        return {"resolved": 0, "skipped": 0, "errors": 0}

    logger.info(f"[resolver] Resolving {len(pending)} pending outcomes…")

    resolved_count = 0
    skipped_count = 0
    error_count = 0

    # Group by symbol to minimise yfinance calls
    from collections import defaultdict
    by_symbol: dict[str, list] = defaultdict(list)
    for o in pending:
        by_symbol[o.symbol].append(o)

    for symbol, outcomes in by_symbol.items():
        # Oldest entry date for this symbol
        oldest = min(o.created_at for o in outcomes)
        df = _fetch_ohlcv_since(symbol, oldest)
        if df is None:
            logger.warning(f"[resolver] Could not fetch OHLCV for {symbol} — skipping {len(outcomes)} outcomes")
            skipped_count += len(outcomes)
            continue

        async with AsyncSessionLocal() as session:
            for o in outcomes:
                try:
                    # Slice df from entry candle onwards
                    entry_date = o.created_at.date() if o.created_at else None
                    if entry_date is None:
                        skipped_count += 1
                        continue

                    # Find first candle on or after the signal date
                    import pandas as pd
                    entry_ts = pd.Timestamp(entry_date)
                    future_df = df[df.index >= entry_ts]

                    if len(future_df) < 2:
                        skipped_count += 1
                        continue

                    result_dict = _resolve_outcome(
                        entry_price=o.entry_price,
                        stop_loss=o.stop_loss,
                        take_profit=o.take_profit,
                        signal_type=o.signal_type,
                        df=future_df,
                        horizon=RESOLUTION_HORIZON,
                    )

                    if not result_dict:
                        skipped_count += 1
                        continue

                    # Refresh the row within this session
                    db_outcome = await session.get(TradeOutcome, o.id)
                    if db_outcome is None:
                        skipped_count += 1
                        continue

                    db_outcome.outcome = OutcomeResult(result_dict["outcome"])
                    db_outcome.pnl_pct = result_dict["pnl_pct"]
                    db_outcome.exit_price = result_dict["exit_price"]
                    db_outcome.candles_held = result_dict["candles_held"]
                    db_outcome.ml_label = result_dict["ml_label"]
                    db_outcome.resolved = True
                    db_outcome.resolved_at = now

                    resolved_count += 1
                    logger.info(
                        f"[resolver] {symbol} signal_id={o.signal_id} → "
                        f"{result_dict['outcome'].upper()} {result_dict['pnl_pct']:+.2f}% "
                        f"in {result_dict['candles_held']} candles"
                    )

                except Exception as exc:
                    logger.error(f"[resolver] Error resolving outcome id={o.id}: {exc}")
                    error_count += 1

            await session.commit()

    summary = {
        "resolved": resolved_count,
        "skipped": skipped_count,
        "errors": error_count,
        "timestamp": now.isoformat(),
    }
    logger.info(f"[resolver] Done: {summary}")
    return summary


# ─────────────────────────────────────────────────────────────
# Celery task wrapper
# ─────────────────────────────────────────────────────────────
from celery_app import celery_app


@celery_app.task(name="tasks.outcome_resolver.resolve_outcomes", bind=True, max_retries=1)
def resolve_outcomes(self):
    """Nightly Celery task — resolves pending trade outcomes for ML retraining."""
    try:
        result = asyncio.run(resolve_pending_outcomes())
        logger.info(f"[resolver] Task complete: {result}")
        return result
    except Exception as exc:
        logger.error(f"[resolver] Task failed: {exc}")
        raise self.retry(exc=exc, countdown=3600)
