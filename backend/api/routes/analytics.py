"""
GET /api/analytics/summary — Performance Analytics (UI-01)
===========================================================
Aggregates trade outcomes and signals into time-series and breakdowns
used by the Analytics Dashboard page.

All calculations run in-process from the DB; no heavy ML needed.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import get_db
from db.models import TradeOutcome, Trade, OrderStatus

router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_pnl(v) -> float:
    return float(v) if v is not None else 0.0


def _sharpe(pnl_list: list[float], window: int = 30) -> list[dict]:
    """Compute rolling Sharpe ratio over a window of trades."""
    ANNUALISATION = (252 ** 0.5)  # daily approximation
    results = []
    for i in range(len(pnl_list)):
        chunk = pnl_list[max(0, i - window + 1): i + 1]
        if len(chunk) < 2:
            results.append(None)
            continue
        import statistics
        mean = statistics.mean(chunk)
        std = statistics.stdev(chunk)
        results.append(round((mean / std) * ANNUALISATION, 3) if std > 0 else None)
    return results


# ── Main endpoint ─────────────────────────────────────────────────────────────

@router.get("/summary")
async def get_analytics_summary(db: AsyncSession = Depends(get_db)):
    """
    Returns all analytics data needed for the Analytics Dashboard in one call.

    Structure
    ---------
    {
      equity_curve:   [{date, cumulative_pnl, trade_index}],
      monthly_returns: [{year, month, pnl_pct, label}],
      by_strategy:    [{strategy_name, total, wins, losses, win_rate, avg_pnl}],
      by_symbol:      [{symbol, total, wins, losses, win_rate, avg_pnl}],
      by_hour:        [{hour, total, wins, win_rate}],
      rolling_sharpe: [{date, sharpe}],
      summary:        {total_trades, win_rate, avg_pnl, best_trade, worst_trade, total_pnl}
    }
    """

    # Fetch all resolved outcomes ordered by creation time
    q = await db.execute(
        select(TradeOutcome)
        .where(TradeOutcome.resolved == True)
        .order_by(TradeOutcome.resolved_at)
    )
    outcomes: list[TradeOutcome] = list(q.scalars().all())

    # ── Equity curve ─────────────────────────────────────────────────────────
    cumulative = 0.0
    equity_curve = []
    pnl_series: list[float] = []

    for idx, o in enumerate(outcomes):
        pnl = _safe_pnl(o.pnl_pct)
        cumulative += pnl
        pnl_series.append(pnl)
        dt = o.resolved_at or o.created_at
        equity_curve.append({
            "date":           dt.isoformat() if dt else None,
            "cumulative_pnl": round(cumulative, 4),
            "pnl_pct":        round(pnl, 4),
            "trade_index":    idx + 1,
        })

    # ── Monthly returns heatmap ───────────────────────────────────────────────
    monthly: dict[tuple, float] = defaultdict(float)
    for o in outcomes:
        dt = o.resolved_at or o.created_at
        if dt:
            key = (dt.year, dt.month)
            monthly[key] += _safe_pnl(o.pnl_pct)

    monthly_returns = sorted(
        [
            {
                "year":    y,
                "month":   m,
                "pnl_pct": round(v, 4),
                "label":   datetime(y, m, 1).strftime("%b %Y"),
            }
            for (y, m), v in monthly.items()
        ],
        key=lambda r: (r["year"], r["month"]),
    )

    # ── Win rate by strategy ──────────────────────────────────────────────────
    strat_stats: dict[str, dict] = defaultdict(lambda: {"total": 0, "wins": 0, "pnl_sum": 0.0})
    for o in outcomes:
        s = strat_stats[o.strategy_name]
        s["total"] += 1
        pnl = _safe_pnl(o.pnl_pct)
        s["pnl_sum"] += pnl
        if o.ml_label == 1:
            s["wins"] += 1

    by_strategy = [
        {
            "strategy_name": name,
            "total":         d["total"],
            "wins":          d["wins"],
            "losses":        d["total"] - d["wins"],
            "win_rate":      round(d["wins"] / d["total"] * 100, 1) if d["total"] else 0,
            "avg_pnl":       round(d["pnl_sum"] / d["total"], 4) if d["total"] else 0,
        }
        for name, d in sorted(strat_stats.items())
    ]

    # ── Win rate by symbol ────────────────────────────────────────────────────
    sym_stats: dict[str, dict] = defaultdict(lambda: {"total": 0, "wins": 0, "pnl_sum": 0.0})
    for o in outcomes:
        s = sym_stats[o.symbol]
        s["total"] += 1
        pnl = _safe_pnl(o.pnl_pct)
        s["pnl_sum"] += pnl
        if o.ml_label == 1:
            s["wins"] += 1

    by_symbol = [
        {
            "symbol":   sym,
            "total":    d["total"],
            "wins":     d["wins"],
            "losses":   d["total"] - d["wins"],
            "win_rate": round(d["wins"] / d["total"] * 100, 1) if d["total"] else 0,
            "avg_pnl":  round(d["pnl_sum"] / d["total"], 4) if d["total"] else 0,
        }
        for sym, d in sorted(sym_stats.items())
    ]

    # ── Win rate by hour-of-day ───────────────────────────────────────────────
    hour_stats: dict[int, dict] = defaultdict(lambda: {"total": 0, "wins": 0})
    for o in outcomes:
        dt = o.created_at
        if dt:
            h = dt.hour
            hour_stats[h]["total"] += 1
            if o.ml_label == 1:
                hour_stats[h]["wins"] += 1

    by_hour = [
        {
            "hour":     h,
            "total":    d["total"],
            "wins":     d["wins"],
            "win_rate": round(d["wins"] / d["total"] * 100, 1) if d["total"] else 0,
        }
        for h, d in sorted(hour_stats.items())
    ]

    # ── Rolling Sharpe ────────────────────────────────────────────────────────
    sharpe_values = _sharpe(pnl_series, window=30)
    rolling_sharpe = [
        {"date": equity_curve[i]["date"], "sharpe": sharpe_values[i]}
        for i in range(len(equity_curve))
        if sharpe_values[i] is not None
    ]

    # ── Summary stats ─────────────────────────────────────────────────────────
    total = len(outcomes)
    wins  = sum(1 for o in outcomes if o.ml_label == 1)
    all_pnl = [_safe_pnl(o.pnl_pct) for o in outcomes]

    summary = {
        "total_trades":  total,
        "win_rate":      round(wins / total * 100, 1) if total else 0,
        "avg_pnl":       round(sum(all_pnl) / total, 4) if total else 0,
        "total_pnl":     round(sum(all_pnl), 4),
        "best_trade":    round(max(all_pnl), 4) if all_pnl else 0,
        "worst_trade":   round(min(all_pnl), 4) if all_pnl else 0,
    }

    return {
        "equity_curve":    equity_curve,
        "monthly_returns": monthly_returns,
        "by_strategy":     by_strategy,
        "by_symbol":       by_symbol,
        "by_hour":         by_hour,
        "rolling_sharpe":  rolling_sharpe,
        "summary":         summary,
    }
