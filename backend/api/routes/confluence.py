"""
Multi-Timeframe Confluence
==========================
Runs the same strategy across multiple timeframes for a symbol and returns
a confluence score + per-timeframe breakdown.

GET /api/confluence?symbol=BTC/USDT&broker=binance&strategy_type=hybrid_macd_rsi
                   &timeframes=1h,4h,1d
"""

from __future__ import annotations

import asyncio
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from loguru import logger

from core.engine.signal_engine import SignalEngine, STRATEGY_REGISTRY

router = APIRouter()
_engine = SignalEngine()

# Timeframes available for confluence analysis
VALID_TIMEFRAMES = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "3d", "1w"}
DEFAULT_TIMEFRAMES = ["1h", "4h", "1d"]


def _consensus(signals: list[dict]) -> dict:
    """
    Given per-TF signal results, compute:
        - consensus direction (BUY / SELL / HOLD / MIXED)
        - confluence_score  0.0 – 1.0  (fraction of TFs that agree with consensus)
        - agreement list (True/False per TF)
    """
    types = [s["signal"] for s in signals]
    counts: dict[str, int] = {}
    for t in types:
        counts[t] = counts.get(t, 0) + 1

    best = max(counts, key=lambda k: counts[k])
    score = counts[best] / len(types) if types else 0.0

    # MIXED if no single direction holds majority
    if score < 0.5 and len(types) > 1:
        best = "MIXED"
        score = 0.0

    agreement = [t == best for t in types]
    return {"consensus": best, "confluence_score": round(score, 2), "agreement": agreement}


@router.get("")
async def get_confluence(
    symbol: str = Query(..., description="Trading symbol e.g. BTC/USDT"),
    broker: str = Query(..., description="Broker e.g. binance"),
    strategy_type: str = Query("hybrid_macd_rsi", description="Strategy key from registry"),
    timeframes: str = Query("1h,4h,1d", description="Comma-separated timeframes"),
):
    """
    Run the same strategy on multiple timeframes and return a confluence analysis.
    """
    if strategy_type not in STRATEGY_REGISTRY:
        raise HTTPException(400, f"Unknown strategy_type '{strategy_type}'. Available: {list(STRATEGY_REGISTRY)}")

    tf_list = [t.strip() for t in timeframes.split(",") if t.strip() in VALID_TIMEFRAMES]
    if not tf_list:
        raise HTTPException(400, f"No valid timeframes. Use subset of: {sorted(VALID_TIMEFRAMES)}")

    # Run all timeframes concurrently
    async def _run_one(tf: str) -> dict:
        try:
            sig = await _engine.run(
                strategy_name=strategy_type,
                symbol=symbol.upper(),
                broker_name=broker.lower(),
                timeframe=tf,
                limit=200,
            )
            return {
                "timeframe": tf,
                "signal": sig.signal,
                "confidence": round(sig.confidence, 3),
                "entry_price": sig.entry_price,
                "stop_loss": sig.stop_loss,
                "take_profit": sig.take_profit,
                "regime": sig.regime,
                "reasons": sig.reasons or [],
                "error": None,
            }
        except Exception as exc:
            logger.warning(f"[confluence] {strategy_type} {symbol} {tf} failed: {exc}")
            return {
                "timeframe": tf,
                "signal": "HOLD",
                "confidence": 0.0,
                "entry_price": None,
                "stop_loss": None,
                "take_profit": None,
                "regime": None,
                "reasons": [],
                "error": str(exc),
            }

    results = await asyncio.gather(*[_run_one(tf) for tf in tf_list])
    results = list(results)

    cdata = _consensus(results)

    # Attach per-TF agreement flag
    for i, r in enumerate(results):
        r["agrees_with_consensus"] = cdata["agreement"][i]

    return {
        "symbol": symbol.upper(),
        "broker": broker.lower(),
        "strategy_type": strategy_type,
        "consensus": cdata["consensus"],
        "confluence_score": cdata["confluence_score"],
        "timeframes": results,
    }


@router.get("/batch")
async def get_confluence_batch(
    broker: str = Query(...),
    strategy_type: str = Query("hybrid_macd_rsi"),
    symbols: str = Query(..., description="Comma-separated symbols"),
    timeframes: str = Query("1h,4h,1d"),
):
    """
    Run confluence for multiple symbols at once.
    Returns list of per-symbol confluence results.
    """
    if strategy_type not in STRATEGY_REGISTRY:
        raise HTTPException(400, f"Unknown strategy_type '{strategy_type}'")

    sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not sym_list:
        raise HTTPException(400, "Provide at least one symbol")
    if len(sym_list) > 10:
        raise HTTPException(400, "Max 10 symbols per batch request")

    async def _run_sym(sym: str) -> dict:
        try:
            tf_list = [t.strip() for t in timeframes.split(",") if t.strip() in VALID_TIMEFRAMES]

            async def _one(tf: str) -> dict:
                try:
                    sig = await _engine.run(
                        strategy_name=strategy_type,
                        symbol=sym,
                        broker_name=broker.lower(),
                        timeframe=tf,
                        limit=200,
                    )
                    return {"timeframe": tf, "signal": sig.signal, "confidence": round(sig.confidence, 3), "error": None}
                except Exception as exc:
                    return {"timeframe": tf, "signal": "HOLD", "confidence": 0.0, "error": str(exc)}

            tfs = await asyncio.gather(*[_one(tf) for tf in tf_list])
            cdata = _consensus(list(tfs))
            return {"symbol": sym, "consensus": cdata["consensus"], "confluence_score": cdata["confluence_score"], "timeframes": list(tfs)}
        except Exception as exc:
            return {"symbol": sym, "consensus": "HOLD", "confluence_score": 0.0, "timeframes": [], "error": str(exc)}

    results = await asyncio.gather(*[_run_sym(s) for s in sym_list])
    return {"results": list(results), "strategy_type": strategy_type, "broker": broker}
