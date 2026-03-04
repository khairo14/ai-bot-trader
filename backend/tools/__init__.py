from tools.basic import RSI, MACD, BollingerBands, MovingAverages, VolumeAnalysis, ATR, ADX
from tools.advanced.vwap import VWAP

__all__ = [
    # Basic
    "RSI", "MACD", "BollingerBands", "MovingAverages",
    "VolumeAnalysis", "ATR", "ADX",
    # Advanced
    "VWAP",
]

# Tool registry — maps tool name string → class
TOOL_REGISTRY = {
    "RSI": RSI,
    "MACD": MACD,
    "BollingerBands": BollingerBands,
    "MovingAverages": MovingAverages,
    "VolumeAnalysis": VolumeAnalysis,
    "ATR": ATR,
    "ADX": ADX,
    "VWAP": VWAP,
}


def get_tool(name: str):
    """Factory: returns instantiated tool by name."""
    if name not in TOOL_REGISTRY:
        raise ValueError(f"Unknown tool: '{name}'. Available: {list(TOOL_REGISTRY.keys())}")
    return TOOL_REGISTRY[name]()
