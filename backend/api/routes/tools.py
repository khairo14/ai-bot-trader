from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

router = APIRouter()


@router.get("/")
async def list_tools():
    """List all available tools in the registry."""
    from tools import TOOL_REGISTRY
    return {
        "tools": [
            {"name": name, "tier": _get_tier(name)}
            for name in TOOL_REGISTRY.keys()
        ]
    }


def _get_tier(name: str) -> str:
    basic = {"RSI", "MACD", "BollingerBands", "MovingAverages", "VolumeAnalysis", "ATR", "ADX"}
    advanced = {"VWAP", "OrderFlow", "IVTools", "Greeks"}
    return "basic" if name in basic else "advanced" if name in advanced else "custom"


class ToolRunRequest(BaseModel):
    tool_name: str
    broker: str
    symbol: str
    timeframe: str = "1h"
    limit: int = 200
    parameters: Optional[dict] = None


@router.post("/run")
async def run_tool(request: ToolRunRequest):
    """
    Run a specific tool on live or cached market data and return its output.
    Useful for testing tools before adding them to a strategy.
    """
    from tools import get_tool
    from brokers import get_broker

    broker = get_broker(request.broker)
    await broker.connect()
    df = await broker.get_ohlcv(request.symbol, request.timeframe, request.limit)
    tool = get_tool(request.tool_name)
    params = request.parameters or {}
    try:
        output = tool.calculate(df, **params)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "tool": output.tool,
        "symbol": request.symbol,
        "timeframe": request.timeframe,
        "value": output.value,
        "signal": output.signal,
        "strength": output.strength,
        "metadata": output.metadata,
    }
