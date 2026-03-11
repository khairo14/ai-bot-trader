"""
Portfolio Optimizer (ML-03)
===========================
Sharpe-weighted mean-variance optimization using resolved TradeOutcome data.

Algorithm:
  1.  Fetch all resolved TradeOutcome rows, group by strategy_name.
  2.  Require MIN_TRADES resolved outcomes per strategy to include it.
  3.  Compute per-strategy mean return + std dev → Sharpe ratio.
  4.  Apply a correlation penalty: strategies whose returns are highly
      correlated (r > CORR_THRESHOLD) are down-weighted together.
  5.  Normalize weights to sum to 1.0.
  6.  Write weights back to Strategy.parameters["weight"].

No scipy required — uses pure Python math.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import math
from collections import defaultdict
from typing import Optional

logger = logging.getLogger(__name__)

MIN_TRADES      = 5      # minimum resolved trades to include a strategy
CORR_THRESHOLD  = 0.75   # correlation above which to apply penalty
CORR_PENALTY    = 0.6    # multiply weight by this factor when highly correlated
RISK_FREE_RATE  = 0.0    # daily risk-free rate (0 for simplicity)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def _std(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    m = _mean(vals)
    variance = sum((v - m) ** 2 for v in vals) / (len(vals) - 1)
    return math.sqrt(variance) if variance > 0 else 0.0


def _pearson(a: list[float], b: list[float]) -> float:
    """Pearson correlation of two equal-length lists."""
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    a = a[:n]
    b = b[:n]
    std_a, std_b = _std(a), _std(b)
    # If either series is constant (zero variance), correlation is undefined.
    # Treat as uncorrelated (0.0) — don't apply a spurious correlation penalty.
    if std_a == 0.0 or std_b == 0.0:
        return 0.0
    ma, mb = _mean(a), _mean(b)
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    den = std_a * std_b * n
    return num / den if den > 1e-12 else 0.0


def _compute_weights(strategy_returns: dict[str, list[float]]) -> dict[str, float]:
    """
    Given {strategy_name: [pnl_pct, ...]} compute normalized Sharpe-based weights.
    """
    names = [n for n, r in strategy_returns.items() if len(r) >= MIN_TRADES]
    if not names:
        return {}

    # Sharpe per strategy (annualised with daily compounding proxy)
    sharpes: dict[str, float] = {}
    for name in names:
        rets = strategy_returns[name]
        mu  = _mean(rets)
        std = _std(rets)
        # std=0 means all returns are identical — perfectly consistent.
        # Assign a high Sharpe (capped at 10) so it's rewarded, not penalised.
        sharpes[name] = (mu - RISK_FREE_RATE) / std if std > 0 else 10.0 * mu

    # Clip negative Sharpe → 0 (don't want negative weights)
    raw: dict[str, float] = {n: max(0.0, s) for n, s in sharpes.items()}

    # If all strategies are negative Sharpe, fallback to equal weight
    total_raw = sum(raw.values())
    if total_raw < 1e-9:
        eq = 1.0 / len(names)
        return {n: round(eq, 4) for n in names}

    # Normalize
    weights: dict[str, float] = {n: v / total_raw for n, v in raw.items()}

    # Correlation penalty: find pairs with high |correlation|, down-weight both
    for i, na in enumerate(names):
        for j, nb in enumerate(names):
            if j <= i:
                continue
            ra = strategy_returns[na]
            rb = strategy_returns[nb]
            corr = abs(_pearson(ra, rb))
            if corr > CORR_THRESHOLD:
                weights[na] *= CORR_PENALTY
                weights[nb] *= CORR_PENALTY
                logger.info(f"[optimizer] {na} ↔ {nb} corr={corr:.2f} — applying penalty")

    # Re-normalize after penalty
    total = sum(weights.values())
    if total < 1e-9:
        eq = 1.0 / len(names)
        return {n: round(eq, 4) for n in names}

    return {n: round(v / total, 4) for n, v in weights.items()}


# ── Main async entry point ────────────────────────────────────────────────────

async def optimize_portfolio() -> dict:
    """
    Fetch TradeOutcome data, compute weights, write to Strategy.parameters.
    Returns summary dict.
    """
    from db.database import AsyncSessionLocal
    from db.models import TradeOutcome, Strategy
    from sqlalchemy import select

    now = datetime.datetime.now(datetime.timezone.utc)

    # 1. Load resolved outcomes
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(TradeOutcome).where(
                TradeOutcome.resolved == True,   # noqa: E712
                TradeOutcome.pnl_pct != None,    # noqa: E711
            ).order_by(TradeOutcome.resolved_at)
        )
        outcomes = list(result.scalars().all())

    if not outcomes:
        logger.info("[optimizer] No resolved outcomes — skipping optimization")
        return {"status": "skipped", "reason": "no_data", "weights": {}}

    # 2. Group returns by strategy name
    strategy_returns: dict[str, list[float]] = defaultdict(list)
    for o in outcomes:
        if o.pnl_pct is not None:
            strategy_returns[o.strategy_name].append(float(o.pnl_pct))

    # 3. Compute optimal weights
    weights = _compute_weights(dict(strategy_returns))

    if not weights:
        logger.info("[optimizer] Not enough data for any strategy (need MIN_TRADES=%d)", MIN_TRADES)
        return {
            "status": "skipped",
            "reason": f"all_strategies_have_fewer_than_{MIN_TRADES}_trades",
            "weights": {},
            "trade_counts": {k: len(v) for k, v in strategy_returns.items()},
        }

    # 4. Write weights back to Strategy rows (by strategy_type in parameters)
    updated: list[str] = []
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Strategy))
        db_strategies = list(result.scalars().all())

        for strat in db_strategies:
            params = dict(strat.parameters or {})
            # strategy_type key in parameters → strategy name in STRATEGY_REGISTRY
            stype = params.get("strategy_type") or strat.name
            # Try direct name match OR strategy_type match
            matching_weight = weights.get(strat.name) or weights.get(stype)
            if matching_weight is not None:
                params["weight"] = matching_weight
                strat.parameters = params
                updated.append(strat.name)

        await session.commit()

    summary = {
        "status": "ok",
        "optimized_at": now.isoformat(),
        "strategies_included": list(weights.keys()),
        "strategies_updated_in_db": updated,
        "weights": weights,
        "trade_counts": {k: len(v) for k, v in strategy_returns.items()},
        "sharpe_ratios": {
            n: round((_mean(r) - RISK_FREE_RATE) / _std(r), 3)
            for n, r in strategy_returns.items()
            if len(r) >= MIN_TRADES
        },
    }
    logger.info(f"[optimizer] Done: {summary}")
    return summary
