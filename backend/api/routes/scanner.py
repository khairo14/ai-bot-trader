"""
Market Scanner API
POST /api/scanner/scan  — run a strategy across a list of symbols in parallel,
                          return non-HOLD signals ranked by confidence.
GET  /api/scanner/watchlists — return preset symbol lists per asset class.
"""
from __future__ import annotations

import asyncio
from typing import List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from loguru import logger

router = APIRouter()

# ─── Preset watchlists ────────────────────────────────────────────────────────

WATCHLISTS: dict[str, list[str]] = {
    "crypto_major": [
        "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT",
        "ADA/USDT", "AVAX/USDT", "DOGE/USDT", "DOT/USDT", "POL/USDT",
    ],
    "crypto_mid": [
        "LINK/USDT", "UNI/USDT", "ATOM/USDT", "LTC/USDT", "FIL/USDT",
        "NEAR/USDT", "APT/USDT", "ARB/USDT", "OP/USDT", "WLD/USDT",
    ],
    "us_stocks": [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL",
        "META", "TSLA", "SPY", "QQQ", "AMD",
    ],
    "us_stocks_mid": [
        "COIN", "HOOD", "PLTR", "SQ", "SHOP",
        "NET", "DKNG", "RIVN", "LCID", "SOFI",
    ],
}

# Watchlists available per broker (keeps crypto off stock brokers and vice-versa)
BROKER_WATCHLISTS: dict[str, list[str]] = {
    "binance": ["crypto_major", "crypto_mid"],
    "alpaca":  ["us_stocks", "us_stocks_mid"],
    "ibkr":    ["us_stocks", "us_stocks_mid"],
}

# Strategies available per broker — options strategies require IBKR only
BROKER_STRATEGIES: dict[str, list[str]] = {
    "binance": ["hybrid_macd_rsi", "momentum_breakout", "mean_reversion_bb"],
    "alpaca":  ["hybrid_macd_rsi", "momentum_breakout", "mean_reversion_bb"],
    "ibkr":    ["hybrid_macd_rsi", "momentum_breakout", "mean_reversion_bb",
                 "iron_condor", "covered_call", "bull_call_spread"],
}

# ─── Request / response models ────────────────────────────────────────────────

class ScanRequest(BaseModel):
    strategy: str               # e.g. "momentum_breakout"
    broker: str                 # e.g. "binance" | "alpaca"
    timeframe: str = "1h"
    symbols: Optional[List[str]] = None   # override; uses watchlist when null
    watchlist: Optional[str] = "crypto_major"
    limit: int = 200
    include_hold: bool = False  # show HOLDs too if requested


class ScanResult(BaseModel):
    symbol: str
    signal: str
    confidence: float
    entry_price: Optional[float]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    regime: Optional[str]
    reasons: List[str]
    error: Optional[str] = None


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def _scan_one(
    engine,
    strategy: str,
    broker: str,
    symbol: str,
    timeframe: str,
    limit: int,
) -> ScanResult:
    """Run signal engine for a single symbol; catches all exceptions."""
    try:
        sig = await engine.run(
            strategy_name=strategy,
            symbol=symbol,
            broker_name=broker,
            timeframe=timeframe,
            limit=limit,
        )
        return ScanResult(
            symbol=symbol,
            signal=sig.signal,
            confidence=round(sig.confidence or 0.0, 4),
            entry_price=sig.entry_price,
            stop_loss=sig.stop_loss,
            take_profit=sig.take_profit,
            regime=sig.regime,
            reasons=sig.reasons or [],
        )
    except Exception as exc:
        logger.warning(f"[Scanner] {symbol} → error: {exc}")
        return ScanResult(
            symbol=symbol,
            signal="ERROR",
            confidence=0.0,
            entry_price=None,
            stop_loss=None,
            take_profit=None,
            regime=None,
            reasons=[],
            error=str(exc),
        )


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/watchlists")
async def get_watchlists(broker: Optional[str] = None):
    """Return preset watchlists, optionally filtered to those valid for `broker`."""
    if broker and broker in BROKER_WATCHLISTS:
        allowed = BROKER_WATCHLISTS[broker]
        return {"watchlists": {k: WATCHLISTS[k] for k in allowed if k in WATCHLISTS}}
    return {"watchlists": WATCHLISTS}


@router.get("/strategies")
async def get_strategies(broker: Optional[str] = None):
    """Return strategy names valid for `broker`, or all strategies if broker omitted."""
    from core.engine.signal_engine import STRATEGY_REGISTRY
    all_strats = list(STRATEGY_REGISTRY.keys())
    if broker and broker in BROKER_STRATEGIES:
        return {"strategies": [s for s in all_strats if s in BROKER_STRATEGIES[broker]]}
    return {"strategies": all_strats}


@router.post("/scan")
async def scan_market(body: ScanRequest):
    """
    Run `strategy` on every symbol in `symbols` (or the preset `watchlist`)
    in parallel, then return results sorted by confidence descending.

    Errors on individual symbols are included in the response with
    signal='ERROR' so the user can see which symbols failed.
    """
    from core.engine.signal_engine import SignalEngine

    # Validate strategy is compatible with broker
    valid_for_broker = BROKER_STRATEGIES.get(body.broker, list(BROKER_STRATEGIES.get("binance", [])))
    if body.strategy not in valid_for_broker:
        raise HTTPException(
            status_code=400,
            detail=f"Strategy '{body.strategy}' is not compatible with broker '{body.broker}'. "
                   f"Valid strategies: {', '.join(valid_for_broker)}",
        )

    # Resolve symbol list
    symbols: list[str] = body.symbols or []
    if not symbols and body.watchlist:
        symbols = WATCHLISTS.get(body.watchlist, [])
    if not symbols:
        return {"results": [], "scanned": 0, "strategy": body.strategy}

    engine = SignalEngine()

    # Fan out — one coroutine per symbol, all run in parallel
    tasks = [
        _scan_one(engine, body.strategy, body.broker, sym, body.timeframe, body.limit)
        for sym in symbols
    ]
    raw: list[ScanResult] = await asyncio.gather(*tasks)

    # Filter + sort
    results = [r for r in raw if body.include_hold or r.signal not in ("HOLD", "ERROR")]
    errors  = [r for r in raw if r.signal == "ERROR"]
    results.sort(key=lambda r: r.confidence, reverse=True)

    logger.info(
        f"[Scanner] {body.strategy} | {body.broker} | {body.timeframe} — "
        f"scanned {len(symbols)}, hits={len(results)}, errors={len(errors)}"
    )

    return {
        "results": [r.model_dump() for r in results],
        "errors":  [r.model_dump() for r in errors],
        "scanned": len(symbols),
        "strategy": body.strategy,
        "broker": body.broker,
        "timeframe": body.timeframe,
    }
