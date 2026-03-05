"""
Iron Condor Strategy (IBKR Options)
=====================================
Sells a call spread + put spread above and below the current price.
Best in high-IV, range-bound conditions: sell expensive options, collect
premium, and profit if the underlying stays between the short strikes until
expiry (or until the trade is closed at 50% of max profit).

Signal conditions:
  IV Rank > 50   — elevated IV means expensive options, good to sell
  ADX < 25       — market is range-bound, not in a strong directional trend

Construction (per condor, premiums estimated via Black-Scholes):
  SELL 1 OTM call  @ price + 1×ATR   (short call strike)
  BUY  1 OTM call  @ price + 2×ATR   (long call wing — caps max loss)
  SELL 1 OTM put   @ price − 1×ATR   (short put strike)
  BUY  1 OTM put   @ price − 2×ATR   (long put wing — caps max loss)

  Signal fields:
    signal       = SELL  (sell the condor to open)
    entry_price  = net premium collected per condor
    stop_loss    = max loss per condor = spread_width − net_premium
    take_profit  = 50% of net_premium (standard close target)
    iv_rank      = current IV Rank (0–100)
    delta        ≈ 0.00  (delta‑neutral by construction)
    theta        > 0.00  (time decay HELPS the seller)
    vega         < 0.00  (short vega — profits when IV falls)
    options_meta = four-leg detail dict
"""
from __future__ import annotations

import pandas as pd
from typing import Optional
from loguru import logger

from core.strategies.base import BaseStrategy, Signal
from core.options.iv_rank import (
    iv_rank_from_ohlcv, historical_volatility,
    bs_call, bs_put,
    bs_theta_call, bs_theta_put, bs_vega,
    nearest_strike, next_monthly_expiry,
)
from tools.basic.volume_atr_adx import ATR, ADX


class IronCondorStrategy(BaseStrategy):
    """Iron Condor — sell OTM call + put spreads in high-IV, range-bound markets."""

    name        = "iron_condor"
    description = (
        "Sells an iron condor (OTM call spread + OTM put spread) when IV Rank > 50 and "
        "the market is ranging (ADX < 25). Profits from time decay and IV contraction."
    )
    asset_class = "option"
    broker      = "ibkr"

    # ── Thresholds ─────────────────────────────────────────────────────────────
    MIN_IV_RANK:     float = 50.0   # require elevated IV to sell premium
    MAX_ADX:         float = 25.0   # require non-trending market
    SHORT_ATR_MULT:  float = 1.0    # short strikes: 1×ATR from spot
    LONG_ATR_MULT:   float = 2.0    # wing strikes: 2×ATR from spot (capped loss)
    DTE:             int   = 30     # target days to expiry

    def __init__(self) -> None:
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

        if len(data) < 60:
            return _hold(["Insufficient data (need ≥ 60 daily bars)"])

        current_price = float(data["close"].iloc[-1])

        # ── IV Rank ────────────────────────────────────────────────────────────
        iv_rank   = iv_rank_from_ohlcv(data)
        iv_decimal = historical_volatility(data) / 100.0   # decimal for B-S

        if iv_rank < self.MIN_IV_RANK:
            return _hold([
                f"IV Rank {iv_rank:.0f}% — need > {self.MIN_IV_RANK:.0f}% to sell premium; "
                "wait for volatility to rise"
            ])

        # ── ADX (trend strength) ───────────────────────────────────────────────
        adx_out = self.adx_tool.calculate(data)
        if adx_out.value > self.MAX_ADX:
            return _hold([
                f"ADX {adx_out.value:.1f} > {self.MAX_ADX} — market trending; "
                "iron condor needs a range-bound environment"
            ])

        # ── ATR for strike placement ───────────────────────────────────────────
        atr_out = self.atr_tool.calculate(data)
        atr = max(atr_out.value, current_price * 0.01)   # floor at 1% of price

        # ── Strike selection ───────────────────────────────────────────────────
        short_call = nearest_strike(current_price, current_price + self.SHORT_ATR_MULT * atr)
        long_call  = nearest_strike(current_price, current_price + self.LONG_ATR_MULT  * atr)
        short_put  = nearest_strike(current_price, current_price - self.SHORT_ATR_MULT * atr)
        long_put   = nearest_strike(current_price, current_price - self.LONG_ATR_MULT  * atr)

        # Ensure wing is always further OTM than short
        if long_call <= short_call:
            long_call = short_call + nearest_strike(current_price, atr)
        if long_put >= short_put:
            long_put = short_put - nearest_strike(current_price, atr)

        # ── Black-Scholes premium estimation ──────────────────────────────────
        expiry = next_monthly_expiry(self.DTE)
        T = self.DTE / 365.0

        p_short_call = bs_call(current_price, short_call, T, iv_decimal)
        p_long_call  = bs_call(current_price, long_call,  T, iv_decimal)
        p_short_put  = bs_put(current_price,  short_put,  T, iv_decimal)
        p_long_put   = bs_put(current_price,  long_put,   T, iv_decimal)

        call_spread_credit = p_short_call - p_long_call
        put_spread_credit  = p_short_put  - p_long_put
        net_premium        = call_spread_credit + put_spread_credit
        spread_width       = long_call - short_call      # same for both spreads
        max_loss           = spread_width - net_premium

        if net_premium < 0.05:
            return _hold(["Collected premium < $0.05 — not worth the commission risk"])

        # ── Greeks (net short-condor position) ────────────────────────────────
        # Delta ≈ 0 by symmetric construction
        net_delta = 0.0

        # Theta: selling = positive theta (you collect decay daily)
        # Buying wings = negative theta paid.  Net is positive for the seller.
        sc_th = bs_theta_call(current_price, short_call, T, iv_decimal)  # negative (long theta)
        lc_th = bs_theta_call(current_price, long_call,  T, iv_decimal)
        sp_th = bs_theta_put(current_price,  short_put,  T, iv_decimal)
        lp_th = bs_theta_put(current_price,  long_put,   T, iv_decimal)
        # SELL means we collect the theta the buyer loses: -sc_th (negate)
        net_theta = -sc_th + lc_th - sp_th + lp_th

        # Vega: negative (short vega — profits when IV contracts post-entry)
        sc_vg = bs_vega(current_price, short_call, T, iv_decimal)
        lc_vg = bs_vega(current_price, long_call,  T, iv_decimal)
        sp_vg = bs_vega(current_price, short_put,  T, iv_decimal)
        lp_vg = bs_vega(current_price, long_put,   T, iv_decimal)
        net_vega = -sc_vg + lc_vg - sp_vg + lp_vg

        # ── Confidence ─────────────────────────────────────────────────────────
        iv_factor  = min((iv_rank - self.MIN_IV_RANK) / 50.0, 1.0)
        adx_factor = max((self.MAX_ADX - adx_out.value) / self.MAX_ADX, 0.0)
        confidence = 0.50 + 0.25 * iv_factor + 0.25 * adx_factor

        reasons = [
            f"IV Rank {iv_rank:.0f}% — elevated volatility, premium-selling entry",
            f"ADX {adx_out.value:.1f} — range-bound market, condor-friendly",
            f"Strikes: {long_put:.1f}P / {short_put:.1f}P — {short_call:.1f}C / {long_call:.1f}C",
            f"Net premium: ${net_premium:.2f} | Spread width: ${spread_width:.2f} | Max loss: ${max_loss:.2f}",
            f"Expiry: {expiry[:4]}-{expiry[4:6]}-{expiry[6:]}",
        ]

        return Signal(
            symbol=symbol,
            signal="SELL",
            entry_price=round(net_premium, 4),
            stop_loss=round(max_loss, 4),
            take_profit=round(net_premium * 0.5, 4),   # close at 50% max profit
            confidence=round(confidence, 4),
            timeframe=timeframe,
            strategy_name=self.name,
            asset_class=self.asset_class,
            broker=self.broker,
            regime="ranging",
            reasons=reasons,
            iv_rank=round(iv_rank, 1),
            delta=round(net_delta, 4),
            theta=round(net_theta, 6),
            vega=round(net_vega, 4),
            options_meta={
                "strategy_type": "iron_condor",
                "expiry": expiry,
                "legs": [
                    {"action": "SELL", "right": "C", "strike": short_call, "premium": round(p_short_call, 4)},
                    {"action": "BUY",  "right": "C", "strike": long_call,  "premium": round(p_long_call,  4)},
                    {"action": "SELL", "right": "P", "strike": short_put,  "premium": round(p_short_put,  4)},
                    {"action": "BUY",  "right": "P", "strike": long_put,   "premium": round(p_long_put,   4)},
                ],
                "spread_width":      round(spread_width, 2),
                "max_loss":          round(max_loss,     4),
                "underlying_price":  round(current_price, 4),
            },
        )
