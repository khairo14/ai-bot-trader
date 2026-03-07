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

# Seconds per candle for each supported timeframe
_TF_SECONDS: dict[str, int] = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "1Hour": 3600, "4h": 14400, "1d": 86400,
}
# Sub-hour timeframes whose live history is often limited — fall back to 1h
_SUB_HOUR = {"1m", "5m", "15m", "30m"}


async def _fetch_ohlcv_broker(
    broker_name: str,
    symbol: str,
    timeframe: str,
    since: datetime.datetime,
):
    """
    Fetch OHLCV candles from the broker starting at `since`.
    Uses force_paper=False so we always get market data, not paper-trade data.
    Falls back to '1h' for sub-hour timeframes when the broker returns too few
    candles (many brokers cap intraday history to 30-90 days).
    Returns a pandas DataFrame or None.
    """
    import pandas as pd
    from brokers import get_broker

    since_ms = int(since.timestamp() * 1000)

    # How long since the oldest outcome — convert to candle count
    elapsed_secs = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - since).total_seconds()
    tf_secs = _TF_SECONDS.get(timeframe, 3600)
    limit = max(RESOLUTION_HORIZON + 10, int(elapsed_secs / tf_secs) + RESOLUTION_HORIZON + 10)

    for tf in ([timeframe, "1h"] if timeframe in _SUB_HOUR and timeframe != "1h" else [timeframe]):
        try:
            broker = get_broker(broker_name, force_paper=False)
            df = await broker.get_ohlcv(symbol, tf, limit=limit, since=since_ms)
            if df is not None and not df.empty and len(df) >= 2:
                # Normalise index to tz-naive UTC for consistent slicing
                if hasattr(df.index, "tz") and df.index.tz is not None:
                    df.index = df.index.tz_convert("UTC").tz_localize(None)
                return df[["open", "high", "low", "close", "volume"]].dropna()
        except Exception as exc:
            logger.warning(f"[resolver] broker={broker_name} {symbol}/{tf} OHLCV failed: {exc}")
        if tf != "1h" and timeframe in _SUB_HOUR:
            logger.info(f"[resolver] {symbol}/{timeframe} insufficient — retrying with 1h")
    return None


def _resolve_outcome(
    entry_price: float,
    stop_loss: Optional[float],
    take_profit: Optional[float],
    signal_type: str,
    df,           # OHLCV DataFrame sliced from entry candle onwards
    horizon: int = RESOLUTION_HORIZON,
    trailing_stop_pct: Optional[float] = None,
) -> dict:
    """
    Walk rows of `df` (starting at entry candle) to determine outcome.
    Returns dict with keys: outcome, pnl_pct, exit_price, candles_held, ml_label

    If trailing_stop_pct is set (e.g. 2.0 = 2%), the stop trails up/down
    with the price peak and overrides the fixed stop_loss once it would be
    more favourable to the trade.
    """
    is_long = signal_type in ("BUY", "COVER")   # COVER closes a short → long direction P&L
    is_short = signal_type in ("SELL", "SHORT")

    # Trailing state — initialised to entry price so the trail starts tight
    peak_high = entry_price   # for longs: running max high
    trough_low = entry_price  # for shorts: running min low

    for i, (ts, row) in enumerate(df.iterrows()):
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])

        # Update trailing peaks
        if trailing_stop_pct is not None:
            peak_high = max(peak_high, high)
            trough_low = min(trough_low, low)

            # Compute current trailing stop level
            if is_long:
                t_stop = peak_high * (1.0 - trailing_stop_pct / 100.0)
                # Use trailing stop if it's tighter (higher) than fixed stop_loss
                effective_stop = max(t_stop, stop_loss) if stop_loss is not None else t_stop
            else:
                t_stop = trough_low * (1.0 + trailing_stop_pct / 100.0)
                effective_stop = min(t_stop, stop_loss) if stop_loss is not None else t_stop
        else:
            effective_stop = stop_loss

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

        # Check stop loss (fixed or trailing)
        if effective_stop is not None:
            if is_long and low <= effective_stop:
                pnl_pct = round((effective_stop - entry_price) / entry_price * 100, 4)
                # trailing stop locked in profit → still a win
                if pnl_pct > 0:
                    return {
                        "outcome": "win",
                        "pnl_pct": pnl_pct,
                        "exit_price": effective_stop,
                        "candles_held": i + 1,
                        "ml_label": 1,
                    }
                outcome = "break_even" if abs(pnl_pct) < 0.1 else "loss"
                return {
                    "outcome": outcome,
                    "pnl_pct": pnl_pct,
                    "exit_price": effective_stop,
                    "candles_held": i + 1,
                    "ml_label": 0,
                }
            if is_short and high >= effective_stop:
                pnl_pct = round((entry_price - effective_stop) / entry_price * 100, 4)
                if pnl_pct > 0:
                    return {
                        "outcome": "win",
                        "pnl_pct": pnl_pct,
                        "exit_price": effective_stop,
                        "candles_held": i + 1,
                        "ml_label": 1,
                    }
                outcome = "break_even" if abs(pnl_pct) < 0.1 else "loss"
                return {
                    "outcome": outcome,
                    "pnl_pct": pnl_pct,
                    "exit_price": effective_stop,
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

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
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

    # Group by (symbol, timeframe, broker) to minimise broker OHLCV calls.
    # TradeOutcome has timeframe; broker comes from the linked Signal row.
    from collections import defaultdict
    from db.models import Signal
    from sqlalchemy import select as sa_select

    # Eagerly load broker for each outcome via its signal_id
    async with AsyncSessionLocal() as session:
        sig_ids = [o.signal_id for o in pending if o.signal_id is not None]
        sig_rows = {}
        if sig_ids:
            res = await session.execute(sa_select(Signal).where(Signal.id.in_(sig_ids)))
            sig_rows = {s.id: s for s in res.scalars().all()}

    # Build groups: key = (symbol, timeframe, broker_name)
    by_group: dict[tuple, list] = defaultdict(list)
    for o in pending:
        sig = sig_rows.get(o.signal_id) if o.signal_id else None
        broker_name = sig.broker.value if sig else "alpaca"  # fallback
        key = (o.symbol, o.timeframe, broker_name)
        by_group[key].append(o)

    for (symbol, timeframe, broker_name), outcomes in by_group.items():
        oldest = min(o.created_at for o in outcomes)
        df = await _fetch_ohlcv_broker(broker_name, symbol, timeframe, oldest)
        if df is None:
            logger.warning(
                f"[resolver] Could not fetch OHLCV for {symbol}/{timeframe} via {broker_name} "
                f"— skipping {len(outcomes)} outcomes"
            )
            skipped_count += len(outcomes)
            continue

        async with AsyncSessionLocal() as session:
            for o in outcomes:
                try:
                    # Slice df from entry candle onwards
                    if o.created_at is None:
                        skipped_count += 1
                        continue

                    # Find first candle on or after the signal timestamp
                    import pandas as pd
                    entry_ts = pd.Timestamp(o.created_at).tz_localize(None)
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
                        trailing_stop_pct=getattr(o, "trailing_stop_pct", None),
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
