"""Shared constants and small utilities used across the backend."""

# Single source of truth for timeframe string → seconds mapping.
# Previously duplicated in: forward_test.py, outcome_resolver.py,
# signal_runner.py (twice), and kline_ws.py.
TIMEFRAME_SECONDS: dict[str, int] = {
    "1m":    60,
    "3m":    180,
    "5m":    300,
    "15m":   900,
    "30m":   1_800,
    "1h":    3_600,
    "1Hour": 3_600,   # IBKR alias used by outcome_resolver
    "2h":    7_200,
    "4h":    14_400,
    "6h":    21_600,
    "12h":   43_200,
    "1d":    86_400,
    "3d":    259_200,
    "1w":    604_800,
}


def timeframe_to_seconds(tf: str) -> int:
    """Convert a timeframe string to its duration in seconds.

    Examples: '4h' → 14400, '1d' → 86400, '1w' → 604800.
    Unknown timeframes default to 3600 (1 hour).
    """
    return TIMEFRAME_SECONDS.get(str(tf).lower(), 3_600)
