"""
Covered Call Strategy (IBKR Options)
=======================================
Generates a SELL signal to write an OTM call against an assumed (or existing)
long equity position.  Best in moderate-IV, flat-to-mildly-bullish environments.

Signal conditions:
  IV Rank 30–70%   — moderate IV: not too cheap, not too expensive
  RSI 40–60        — underlying in slight uptrend or neutral (not overbought)
  ADX < 30         — not in a strong trend that blows past the call strike

Construction:
  SELL 1 OTM call  @ strike ≈ price + 1×ATR  (delta ≈ 0.25–0.35, ~30 DTE)

Risk / Reward (options P&L only — shares held separately):
  entry_price  = call premium collected per share
  stop_loss    = 3× premium  (buy-back if option triples against you)
  take_profit  = 10% of premium  (close when 90% of premium is captured)
  delta        = negative  (short call has negative delta — capped upside)
  theta        = positive  (time decay works in the seller's favour)
  vega         = negative  (short vega — benefits from IV contraction)
"""
from __future__ import annotations

import pandas as pd
from typing import Optional
from loguru import logger

from core.strategies.base import BaseStrategy, Signal
from core.options.iv_rank import (
    iv_rank_from_ohlcv, historical_volatility,
    bs_call, bs_delta_call, bs_theta_call, bs_vega,
    nearest_strike, next_monthly_expiry,
)
from tools.basic.rsi import RSI
from tools.basic.volume_atr_adx import ATR, ADX


class CoveredCallStrategy(BaseStrategy):
    """Covered Call — sell OTM call on a long equity position for income."""

    name        = "covered_call"
    description = (
        "Sells an OTM call ~30 DTE to generate income on a long stock position. "
        "Best in moderate-IV (30–70%), neutral-to-mildly-bullish conditions."
    )
    asset_class = "option"
    broker      = "ibkr"

    MIN_IV_RANK: float = 30.0
    MAX_IV_RANK: float = 70.0
    RSI_LOW:     float = 40.0
    RSI_HIGH:    float = 60.0
    MAX_ADX:     float = 30.0
    OTM_MULT:    float = 1.0    # call strike = price + OTM_MULT × ATR
    DTE:         int   = 30

    def __init__(self) -> None:
        self.rsi_tool = RSI()
        self.atr_tool = ATR()
        self.adx_tool = ADX()

    def generate_signal(
        self,
        data: pd.DataFrame,
        symbol: str,
        timeframe: str = "1d",
        tool_outputs: Optional[dict] = None,
        **kwargs,
    ) -> Signal:
        def _hold(reasons):
            return Signal(
                symbol=symbol, signal="HOLD",
                entry_price=float(data["close"].iloc[-1]),
                stop_loss=None, take_profit=None, confidence=0.0,
                timeframe=timeframe, strategy_name=self.name,
                asset_class=self.asset_class, broker=self.broker,
                reasons=reasons,
            )

        if len(data) < 40:
            return _hold(["Insufficient data (need ≥ 40 daily bars)"])

        current_price = float(data["close"].iloc[-1])
        iv_rank    = iv_rank_from_ohlcv(data)
        iv_decimal = historical_volatility(data) / 100.0

        if iv_rank < self.MIN_IV_RANK:
            return _hold([
                f"IV Rank {iv_rank:.0f}% too low (need {self.MIN_IV_RANK}–{self.MAX_IV_RANK}%) "
                "— premium too cheap for covered call"
            ])
        if iv_rank > self.MAX_IV_RANK:
            return _hold([
                f"IV Rank {iv_rank:.0f}% too high — Iron Condor is more appropriate here"
            ])

        rsi_out = self.rsi_tool.calculate(data)
        if rsi_out.value < self.RSI_LOW:
            return _hold([f"RSI {rsi_out.value:.1f} < {self.RSI_LOW} — underlying too bearish for a covered call"])
        if rsi_out.value > self.RSI_HIGH:
            return _hold([f"RSI {rsi_out.value:.1f} > {self.RSI_HIGH} — underlying overbought, call strike risk elevated"])

        adx_out = self.adx_tool.calculate(data)
        if adx_out.value > self.MAX_ADX:
            return _hold([f"ADX {adx_out.value:.1f} > {self.MAX_ADX} — trending too strongly, strike may be breached"])

        atr_out = self.atr_tool.calculate(data)
        atr    = max(atr_out.value, current_price * 0.01)
        strike = nearest_strike(current_price, current_price + self.OTM_MULT * atr)
        expiry = next_monthly_expiry(self.DTE)
        T      = self.DTE / 365.0

        premium = bs_call(current_price, strike, T, iv_decimal)
        if premium < 0.10:
            return _hold(["Premium < $0.10 — too small relative to commission"])

        # Greeks for a SHORT call position
        long_delta = bs_delta_call(current_price, strike, T, iv_decimal)
        delta   = -long_delta                        # short call → negative delta
        theta   = -bs_theta_call(current_price, strike, T, iv_decimal)  # positive for seller
        vega    = -bs_vega(current_price, strike, T, iv_decimal)         # negative for seller

        # Confidence: peaks when IV is in the middle of [30, 70] and RSI is neutral
        iv_factor  = 1.0 - abs(iv_rank - 50.0) / 50.0
        rsi_factor = 1.0 - abs(rsi_out.value - 50.0) / 50.0
        confidence = 0.5 + 0.25 * iv_factor + 0.25 * rsi_factor

        reasons = [
            f"IV Rank {iv_rank:.0f}% — moderate IV, good covered-call environment",
            f"RSI {rsi_out.value:.1f} — neutral-to-bullish underlying",
            f"Sell {symbol} {strike:.1f} C @ ${premium:.2f} premium",
            f"Delta: {delta:.2f} | Theta: +${theta:.4f}/day | DTE: {self.DTE}",
            f"Expiry: {expiry[:4]}-{expiry[4:6]}-{expiry[6:]}",
        ]

        return Signal(
            symbol=symbol,
            signal="SELL",
            entry_price=round(premium, 4),
            stop_loss=round(premium * 3.0, 4),     # roll/close if premium triples
            take_profit=round(premium * 0.10, 4),  # close when 90% of premium is captured
            confidence=round(confidence, 4),
            timeframe=timeframe,
            strategy_name=self.name,
            asset_class=self.asset_class,
            broker=self.broker,
            reasons=reasons,
            iv_rank=round(iv_rank, 1),
            delta=round(delta, 4),
            theta=round(theta, 6),
            vega=round(vega, 4),
            options_meta={
                "strategy_type": "covered_call",
                "expiry": expiry,
                "strike": strike,
                "right": "C",
                "legs": [{"action": "SELL", "right": "C", "strike": strike, "premium": round(premium, 4)}],
                "underlying_price": round(current_price, 4),
            },
        )
