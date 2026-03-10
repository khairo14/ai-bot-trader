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

    # Fallback: yfinance — works for all asset classes without a broker connection.
    # Covers the common case where IBKR clientId is already held by the API process.
    return await _fetch_ohlcv_yfinance(symbol, timeframe, since)


_YF_INTERVAL_MAP: dict[str, str] = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "60m", "1Hour": "60m", "4h": "60m", "1d": "1d", "1w": "1wk",
}
_REAL_FX = {
    "USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD",
    "HKD", "SGD", "MXN", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF",
}


def _to_yf_ticker(symbol: str) -> str:
    """Convert trading symbol to yfinance ticker (mirrors models/trainer.py logic)."""
    if "/" in symbol:
        base, quote = symbol.split("/", 1)
        base, quote = base.upper(), quote.upper()
        if base in _REAL_FX and quote in _REAL_FX:
            return f"{base}{quote}=X"
        quote_yf = "USD" if quote in ("USDT", "USDC", "BUSD") else quote
        return f"{base}-{quote_yf}"
    # 6-char all-alpha with no slash → IBKR-style forex (e.g. GBPUSD)
    if len(symbol) == 6 and symbol.isalpha():
        return f"{symbol}=X"
    return symbol


async def _fetch_ohlcv_yfinance(
    symbol: str,
    timeframe: str,
    since: datetime.datetime,
):
    """yfinance fallback when broker OHLCV is unavailable."""
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        return None

    ticker = _to_yf_ticker(symbol)
    yf_interval = _YF_INTERVAL_MAP.get(timeframe, "1d")
    days = max(14, int((datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - since).total_seconds() / 86400) + 5)
    # Cap at yfinance intraday limits (60m max 730d, 1m max 7d)
    if yf_interval in ("1m",):
        days = min(days, 7)
    elif yf_interval in ("60m", "5m", "15m", "30m"):
        days = min(days, 729)

    try:
        df = yf.download(ticker, period=f"{days}d", interval=yf_interval,
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            logger.warning(f"[resolver:yf] No data for {ticker} ({yf_interval})")
            return None
        df = df.rename(columns=str.lower)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        df = df[["open", "high", "low", "close", "volume"]].dropna()
        df.index = pd.to_datetime(df.index)
        if hasattr(df.index, "tz") and df.index.tz is not None:
            df.index = df.index.tz_convert("UTC").tz_localize(None)
        # Resample 4h since yfinance only offers 1h natively
        if timeframe == "4h" and yf_interval == "60m":
            df = df.resample("4h").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
            ).dropna()
        since_naive = since.replace(tzinfo=None)
        df = df[df.index >= since_naive]
        logger.info(f"[resolver:yf] {ticker} ({yf_interval}): {len(df)} rows")
        return df if len(df) >= 2 else None
    except Exception as exc:
        logger.warning(f"[resolver:yf] fetch failed for {ticker}: {exc}")
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


# G4: Per-timeframe resolution horizon (candles)
_HORIZON_BY_TF: dict[str, int] = {
    "1m": 60, "5m": 36, "15m": 30, "30m": 24,
    "1h": 24, "4h": 20, "1d": 10, "1w": 5,
}


# ─────────────────────────────────────────────────────────────
# Async resolver
# ─────────────────────────────────────────────────────────────
async def resolve_pending_outcomes() -> dict:
    """Main async entry point. Returns summary dict."""
    from db.database import AsyncSessionLocal
    from db.models import TradeOutcome, OutcomeResult, Signal, Strategy
    from sqlalchemy import select
    from collections import defaultdict
    import pandas as pd

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    cutoff = now - datetime.timedelta(hours=MIN_AGE_HOURS)

    # B5: Single session with FOR UPDATE SKIP LOCKED — concurrent resolvers
    # claim disjoint row sets so each outcome is resolved exactly once.
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(TradeOutcome).where(
                TradeOutcome.resolved == False,  # noqa: E712
                TradeOutcome.created_at <= cutoff,
            ).with_for_update(skip_locked=True)
        )
        pending = result.scalars().all()

        if not pending:
            logger.info("[resolver] No pending outcomes to resolve.")
            return {"resolved": 0, "skipped": 0, "errors": 0}

        logger.info(f"[resolver] Resolving {len(pending)} pending outcomes…")

        resolved_count = 0
        skipped_count = 0
        error_count = 0

        # Eagerly load signal broker for each outcome via its signal_id
        sig_ids = [o.signal_id for o in pending if o.signal_id is not None]
        sig_rows: dict = {}
        if sig_ids:
            res = await session.execute(select(Signal).where(Signal.id.in_(sig_ids)))
            sig_rows = {s.id: s for s in res.scalars().all()}

        # G3: Pre-load strategy broker map for NULL-signal_id outcomes
        strategy_names = {o.strategy_name for o in pending if o.signal_id is None and o.strategy_name}
        strategy_broker_map: dict[str, str] = {}
        if strategy_names:
            strat_res = await session.execute(
                select(Strategy).where(Strategy.name.in_(strategy_names))
            )
            for strat in strat_res.scalars().all():
                strategy_broker_map[strat.name] = strat.broker.value

        def _infer_broker(o: TradeOutcome) -> str:
            # G3: lookup chain — (1) strategy table, (2) symbol heuristic
            if o.strategy_name and o.strategy_name in strategy_broker_map:
                return strategy_broker_map[o.strategy_name]
            # Crypto pairs use "/" (BTC/USDT), IBKR forex uses 6-char alpha (GBPUSD)
            if "/" in o.symbol:
                return "binance"
            if len(o.symbol) == 6 and o.symbol.isalpha():
                return "ibkr"
            return "alpaca"

        # Build groups: key = (symbol, timeframe, broker_name)
        by_group: dict[tuple, list] = defaultdict(list)
        for o in pending:
            sig = sig_rows.get(o.signal_id) if o.signal_id else None
            broker_name = sig.broker.value if sig else _infer_broker(o)
            by_group[(o.symbol, o.timeframe, broker_name)].append(o)

        # Fetch OHLCV once per group (I6: already grouped, no per-outcome re-fetch)
        # and resolve all outcomes in the same locked session.
        for (symbol, timeframe, broker_name), outcomes in by_group.items():
            # G4: resolution horizon depends on timeframe
            horizon = _HORIZON_BY_TF.get(timeframe, RESOLUTION_HORIZON)

            oldest = min(o.created_at for o in outcomes)
            df = await _fetch_ohlcv_broker(broker_name, symbol, timeframe, oldest)
            if df is None:
                logger.warning(
                    f"[resolver] Could not fetch OHLCV for {symbol}/{timeframe} via {broker_name} "
                    f"— skipping {len(outcomes)} outcomes"
                )
                skipped_count += len(outcomes)
                continue

            for o in outcomes:
                try:
                    if o.created_at is None:
                        skipped_count += 1
                        continue

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
                        horizon=horizon,
                        trailing_stop_pct=getattr(o, "trailing_stop_pct", None),
                    )

                    if not result_dict:
                        skipped_count += 1
                        continue

                    # The row is already locked in this session — update directly
                    o.outcome = OutcomeResult(result_dict["outcome"])
                    o.pnl_pct = result_dict["pnl_pct"]
                    o.exit_price = result_dict["exit_price"]
                    o.candles_held = result_dict["candles_held"]
                    o.ml_label = result_dict["ml_label"]
                    o.resolved = True
                    o.resolved_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

                    resolved_count += 1
                    logger.info(
                        f"[resolver] {symbol} signal_id={o.signal_id} → "
                        f"{result_dict['outcome'].upper()} {result_dict['pnl_pct']:+.2f}% "
                        f"in {result_dict['candles_held']} candles"
                    )

                except Exception as exc:
                    logger.error(f"[resolver] Error resolving outcome id={o.id}: {exc}")
                    error_count += 1

        # Commit all resolutions atomically (also releases FOR UPDATE locks)
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
