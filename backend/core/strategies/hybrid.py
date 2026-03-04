import pandas as pd
from typing import Optional
from loguru import logger

from core.strategies.base import BaseStrategy, Signal
from tools.basic.rsi import RSI
from tools.basic.macd import MACD
from tools.basic.bollinger_bands import BollingerBands
from tools.basic.moving_averages import MovingAverages
from tools.basic.volume_atr_adx import VolumeAnalysis, ATR, ADX


class HybridStrategy(BaseStrategy):
    """
    Hybrid Rule-Based + ML Signal Strategy.

    Step 1: Rule-based filter — all conditions must pass threshold
    Step 2: ML model scores the probability (Phase 6 — not yet integrated)
    Step 3: Regime-adjusted signal output

    Entry (LONG):
      - RSI < 45 (not overbought)
      - MACD bullish or histogram_positive
      - EMA fast > EMA slow (trend aligned)
      - Volume above average
      - ADX > 15 (some trending)

    Entry (SHORT):
      - RSI > 55 (not oversold)
      - MACD bearish or histogram_negative
      - EMA fast < EMA slow
      - Volume above average
      - ADX > 15

    Exit signal generated when opposing conditions meet.
    """

    name = "hybrid_macd_rsi"
    description = "Hybrid rule-based strategy combining RSI, MACD, EMA crossover, Volume, and ADX."
    asset_class = "crypto"
    broker = "binance"

    def __init__(self):
        self.rsi = RSI()
        self.macd = MACD()
        self.bb = BollingerBands()
        self.ma = MovingAverages()
        self.vol = VolumeAnalysis()
        self.atr = ATR()
        self.adx = ADX()

    def generate_signal(
        self,
        data: pd.DataFrame,
        symbol: str,
        timeframe: str = "1h",
        tool_outputs: Optional[dict] = None,
        **kwargs,
    ) -> Signal:

        if len(data) < 50:
            return Signal(
                symbol=symbol, signal="HOLD", entry_price=float(data["close"].iloc[-1]),
                stop_loss=None, take_profit=None, confidence=0.0,
                timeframe=timeframe, strategy_name=self.name,
                asset_class=self.asset_class, broker=self.broker,
                reasons=["Insufficient data"]
            )

        # ── Run tools ────────────────────────────────────
        rsi_out = self.rsi.calculate(data, period=14)
        macd_out = self.macd.calculate(data)
        ma_out = self.ma.calculate(data, fast_period=9, slow_period=21)
        vol_out = self.vol.calculate(data)
        atr_out = self.atr.calculate(data)
        adx_out = self.adx.calculate(data)
        bb_out = self.bb.calculate(data)

        current_price = float(data["close"].iloc[-1])
        atr_value = atr_out.value

        # ── Score signals (0–5 points each direction) ────
        long_score = 0
        short_score = 0
        reasons_long = []
        reasons_short = []

        # RSI
        if rsi_out.value < 45:
            long_score += 1
            reasons_long.append(f"RSI {rsi_out.value:.1f} (not overbought)")
        if rsi_out.value > 55:
            short_score += 1
            reasons_short.append(f"RSI {rsi_out.value:.1f} (not oversold)")
        if rsi_out.signal == "oversold":
            long_score += 1
            reasons_long.append(f"RSI oversold ({rsi_out.value:.1f})")
        if rsi_out.signal == "overbought":
            short_score += 1
            reasons_short.append(f"RSI overbought ({rsi_out.value:.1f})")

        # MACD
        if macd_out.signal in ("bullish_crossover", "histogram_positive"):
            long_score += 2 if "crossover" in macd_out.signal else 1
            reasons_long.append(f"MACD {macd_out.signal}")
        if macd_out.signal in ("bearish_crossover", "histogram_negative"):
            short_score += 2 if "crossover" in macd_out.signal else 1
            reasons_short.append(f"MACD {macd_out.signal}")

        # EMA crossover
        if ma_out.signal in ("golden_cross", "bullish"):
            long_score += 2 if ma_out.signal == "golden_cross" else 1
            reasons_long.append(f"EMA {ma_out.signal}")
        if ma_out.signal in ("death_cross", "bearish"):
            short_score += 2 if ma_out.signal == "death_cross" else 1
            reasons_short.append(f"EMA {ma_out.signal}")

        # Volume confirmation
        if vol_out.signal in ("spike", "above"):
            long_score += 1
            short_score += 1  # volume confirms either direction
            reasons_long.append(f"Volume {vol_out.signal} ({vol_out.value:.2f}x avg)")
            reasons_short.append(f"Volume {vol_out.signal} ({vol_out.value:.2f}x avg)")

        # ADX trend filter
        if adx_out.value > 20:
            long_score += 1
            short_score += 1
            reasons_long.append(f"ADX {adx_out.value:.1f} ({adx_out.signal})")
            reasons_short.append(f"ADX {adx_out.value:.1f} ({adx_out.signal})")

        # ── Determine signal ─────────────────────────────
        min_score = kwargs.get("min_score", 4)
        total_possible = 8

        if long_score >= min_score and long_score > short_score:
            signal_type = "BUY"
            confidence = round(long_score / total_possible, 3)
            stop_loss = round(current_price - (atr_value * 2.0), 4)
            take_profit = round(current_price + (atr_value * 4.0), 4)  # 2:1 R:R minimum
            reasons = reasons_long

        elif short_score >= min_score and short_score > long_score:
            signal_type = "SHORT"
            confidence = round(short_score / total_possible, 3)
            stop_loss = round(current_price + (atr_value * 2.0), 4)
            take_profit = round(current_price - (atr_value * 4.0), 4)
            reasons = reasons_short

        else:
            signal_type = "HOLD"
            confidence = 0.0
            stop_loss = None
            take_profit = None
            reasons = ["Conditions not met for entry"]

        logger.debug(
            f"[{self.name}] {symbol} {timeframe} → {signal_type} "
            f"(long: {long_score}, short: {short_score}, conf: {confidence})"
        )

        return Signal(
            symbol=symbol,
            signal=signal_type,
            entry_price=current_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            confidence=confidence,
            timeframe=timeframe,
            strategy_name=self.name,
            asset_class=self.asset_class,
            broker=self.broker,
            reasons=reasons,
        )
