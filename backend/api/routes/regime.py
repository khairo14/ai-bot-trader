"""
GET /api/regime  — Market Regime Endpoint (ML-02)
===================================================
Returns the current market regime for a given symbol/timeframe,
along with the raw indicator features used to classify it.
"""

import pandas as pd
from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from brokers import get_broker
from core.regime_classifier import regime_classifier, ALL_REGIMES

router = APIRouter()

_VALID_BROKERS = {"binance", "alpaca", "ibkr"}


async def _fetch_ohlcv(symbol: str, timeframe: str, broker_name: str) -> pd.DataFrame:
    """Fetch recent OHLCV data using the same broker clients as signal generation."""
    broker = get_broker(broker_name)
    try:
        data = await broker.get_ohlcv(symbol, timeframe, limit=200)
        return data
    except Exception as exc:
        logger.warning(f"[Regime] fetch_ohlcv({symbol}, {timeframe}, {broker_name}): {exc}")
        raise HTTPException(status_code=502, detail=f"Could not fetch market data: {exc}")
    finally:
        await broker.close()


@router.get("")
async def get_regime(
    symbol: str = Query("BTC/USDT", description="Trading pair, e.g. BTC/USDT"),
    timeframe: str = Query("1h", description="Candle timeframe, e.g. 1h, 4h, 1d"),
    broker: str = Query("binance", description="Broker / data source"),
):
    """
    Classify the current market regime for the given symbol and timeframe.
    """
    if broker not in _VALID_BROKERS:
        raise HTTPException(status_code=422, detail=f"Unknown broker '{broker}'. Valid values: {sorted(_VALID_BROKERS)}")
    data = await _fetch_ohlcv(symbol, timeframe, broker)

    result = regime_classifier.classify(data)

    return {
        "regime":           result.regime,
        "symbol":           symbol,
        "timeframe":        timeframe,
        "broker":           broker,
        "features":         result.features,
        "score_adjustment": regime_classifier.score_adjustment(result.regime),
        "atr_multipliers":  regime_classifier.atr_multipliers(result.regime),
        "all_regimes":      ALL_REGIMES,
    }
