from typing import Dict, List, Optional, Any
from loguru import logger
import pandas as pd

from core.strategies.base import Signal, BaseStrategy
from core.strategies.hybrid import HybridStrategy
from core.strategies.momentum import MomentumBreakoutStrategy
from core.strategies.mean_reversion import MeanReversionBBStrategy
from core.strategies.iron_condor import IronCondorStrategy
from core.strategies.covered_call import CoveredCallStrategy
from core.strategies.bull_call_spread import BullCallSpreadStrategy
from brokers import get_broker


STRATEGY_REGISTRY = {
    "hybrid_macd_rsi":   HybridStrategy,
    "momentum_breakout": MomentumBreakoutStrategy,
    "mean_reversion_bb": MeanReversionBBStrategy,
    "iron_condor":       IronCondorStrategy,
    "covered_call":      CoveredCallStrategy,
    "bull_call_spread":  BullCallSpreadStrategy,
}


class SignalEngine:
    """
    Orchestrates strategy execution on live or fetched market data.
    Runs tools → strategy → returns Signal.
    Supports multiple strategies per symbol per call.
    """

    def __init__(self):
        self._strategies: Dict[str, BaseStrategy] = {}

    def get_strategy(self, strategy_name: str) -> BaseStrategy:
        if strategy_name not in self._strategies:
            if strategy_name not in STRATEGY_REGISTRY:
                raise ValueError(f"Unknown strategy: '{strategy_name}'")
            self._strategies[strategy_name] = STRATEGY_REGISTRY[strategy_name]()
        return self._strategies[strategy_name]

    async def run(
        self,
        strategy_name: str,
        symbol: str,
        broker_name: str,
        timeframe: str = "1h",
        limit: int = 200,
        data: Optional[pd.DataFrame] = None,
    ) -> Signal:
        """
        Run a strategy and return a Signal.
        If data is not provided, fetches OHLCV from broker.
        """
        if data is None:
            broker = get_broker(broker_name)
            try:
                await broker.connect()          # no-op for Binance/Alpaca; ensures IBKR is live
                data = await broker.get_ohlcv(symbol, timeframe, limit)
            finally:
                await broker.close()

        strategy = self.get_strategy(strategy_name)
        signal = strategy.generate_signal(data, symbol=symbol, timeframe=timeframe)

        logger.info(
            f"[SignalEngine] {strategy_name} | {symbol} | {timeframe} → "
            f"{signal.signal} @ {signal.entry_price} (conf: {signal.confidence})"
        )
        return signal

    async def run_all_active(
        self,
        strategy_configs: List[dict],
    ) -> List[Signal]:
        """
        Run all active strategies from DB config.
        Each config: {strategy_name, symbol, broker, timeframe, ...}
        """
        signals = []
        for config in strategy_configs:
            try:
                signal = await self.run(
                    strategy_name=config["strategy_name"],
                    symbol=config["symbol"],
                    broker_name=config["broker"],
                    timeframe=config.get("timeframe", "1h"),
                )
                signals.append(signal)
            except Exception as e:
                logger.error(f"[SignalEngine] Error running {config.get('strategy_name')}: {e}")
        return signals
