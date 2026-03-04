from fastapi import APIRouter, HTTPException, Query
from typing import Optional

router = APIRouter()


@router.get("/")
async def list_brokers():
    """List all available broker connectors and their status."""
    return {
        "brokers": [
            {
                "name": "binance",
                "asset_class": "crypto",
                "description": "Binance — Spot and Futures crypto trading",
                "paper_supported": True,
                "live_supported": True,
            },
            {
                "name": "alpaca",
                "asset_class": "stock",
                "description": "Alpaca — US Stocks and ETFs (commission-free)",
                "paper_supported": True,
                "live_supported": True,
            },
            {
                "name": "ibkr",
                "asset_class": "stock_options",
                "description": "Interactive Brokers — Stocks, Options, global markets",
                "paper_supported": True,
                "live_supported": True,
                "requirements": "IB Gateway must be running locally",
            },
        ]
    }


@router.get("/{broker_name}/price")
async def get_price(broker_name: str, symbol: str = Query(...)):
    """Get current price for a symbol on a specific broker."""
    from brokers import get_broker
    try:
        broker = get_broker(broker_name)
        price = await broker.get_price(symbol)
        return {"broker": broker_name, "symbol": symbol, "price": price}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{broker_name}/balance")
async def get_balance(broker_name: str):
    """Get account balance for a broker."""
    from brokers import get_broker
    try:
        broker = get_broker(broker_name)
        if broker_name == "ibkr":
            await broker.connect()
        balance = await broker.get_balance()
        return {"broker": broker_name, "balance": balance}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{broker_name}/positions")
async def get_positions(broker_name: str):
    """Get open positions for a broker."""
    from brokers import get_broker
    try:
        broker = get_broker(broker_name)
        if broker_name == "ibkr":
            await broker.connect()
        positions = await broker.get_positions()
        return {"broker": broker_name, "positions": positions}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{broker_name}/ohlcv")
async def get_ohlcv(
    broker_name: str,
    symbol: str = Query(...),
    timeframe: str = Query(default="1h"),
    limit: int = Query(default=100, le=1000),
):
    """Fetch OHLCV candle data from a broker."""
    from brokers import get_broker
    try:
        broker = get_broker(broker_name)
        if broker_name == "ibkr":
            await broker.connect()
        df = await broker.get_ohlcv(symbol, timeframe, limit)
        records = df.reset_index().to_dict(orient="records")
        return {"broker": broker_name, "symbol": symbol, "timeframe": timeframe, "candles": records}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
