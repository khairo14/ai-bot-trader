"""
Bull Call Spread Strategy (IBKR Options)
==========================================
Generates a BUY signal (net-debit vertical spread) in bullish, low-IV conditions.
Buying an ATM call and selling a further OTM call caps the upside but drastically
reduces the cost versus owning the naked call.

Signal conditions:
  IV Rank < 40%   — cheap options: buying is favourable
  MACD bullish    — histogram > 0  OR  MACD line just crossed above signal
  RSI 40–55       — bullish momentum without being overbought

Construction (same expiry, 45 DTE):
  BUY  1 ATM call  @ strike ≈ current_price         (delta ≈ 0.50)
  SELL 1 OTM call  @ strike ≈ current_price + 1×ATR (delta ≈ 0.25)

Risk / Reward:
  entry_price  = net debit (cost of the spread)
  stop_loss    = full net debit  (max loss if both legs expire worthless)
  take_profit  = net_debit + (spread_width − net_debit) × 0.75  (75% of max profit)
  delta        = positive (net long delta — directional bullish bet)
  theta        = negative (net option buyer — time decay works against you)
  vega         = positive (benefits from IV expansion / long vega)
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
from tools.basic.macd import MACD
from tools.basic.volume_atr_adx import ATR


class BullCallSpreadStrategy(BaseStrategy):
    """Bull Call Spread — buy ATM call / sell OTM call in bullish, low-IV markets."""

    name        = "bull_call_spread"
    description = (
        "Buys an ATM call and sells a higher-strike OTM call with the same expiry "
        "to create a defined-risk, defined-reward bullish position. "
        "Best when IV is low and the underlying shows bullish momentum."
    )
    asset_class = "option"
    broker      = "ibkr"

    MAX_IV_RANK: float = 40.0   # cheap options environment
    RSI_LOW:     float = 40.0
    RSI_HIGH:    float = 55.0
    OTM_MULT:    float = 1.0    # sell_strike = price + OTM_MULT × ATR
    DTE:         int   = 45
    TP_RATIO:    float = 0.75   # close at 75% of max spread profit

    def __init__(self) -> None:
        self.rsi_tool  = RSI()
        self.macd_tool = MACD()
        self.atr_tool  = ATR()

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

        if len(data) < 50:
            return _hold(["Insufficient data (need ≥ 50 daily bars)"])

        current_price = float(data["close"].iloc[-1])
        iv_rank    = iv_rank_from_ohlcv(data)
        iv_decimal = historical_volatility(data) / 100.0

        if iv_rank > self.MAX_IV_RANK:
            return _hold([
                f"IV Rank {iv_rank:.0f}% > {self.MAX_IV_RANK}% — options too expensive for a debit spread; "
                "consider Iron Condor instead"
            ])

        # MACD bullish filter
        macd_out = self.macd_tool.calculate(data)
        histogram = getattr(macd_out, "histogram", None)
        macd_line = getattr(macd_out, "macd", None)
        signal_line = getattr(macd_out, "signal", None)

        macd_bullish = False
        macd_reason  = "MACD neutral"
        if histogram is not None and histogram > 0:
            macd_bullish = True
            macd_reason  = f"MACD histogram positive ({histogram:.4f})"
        elif (macd_line is not None and signal_line is not None
              and macd_line > signal_line):
            macd_bullish = True
            macd_reason  = f"MACD ({macd_line:.4f}) above signal ({signal_line:.4f})"

        if not macd_bullish:
            return _hold([f"MACD not bullish — {macd_reason}"])

        # RSI filter
        rsi_out = self.rsi_tool.calculate(data)
        if rsi_out.value < self.RSI_LOW:
            return _hold([f"RSI {rsi_out.value:.1f} < {self.RSI_LOW} — momentum not yet bullish"])
        if rsi_out.value > self.RSI_HIGH:
            return _hold([f"RSI {rsi_out.value:.1f} > {self.RSI_HIGH} — overbought; breakout risk of buying top"])

        # Strike computation
        atr_out     = self.atr_tool.calculate(data)
        atr         = max(atr_out.value, current_price * 0.01)
        buy_strike  = nearest_strike(current_price, current_price)             # ATM
        sell_strike = nearest_strike(current_price, current_price + self.OTM_MULT * atr)  # OTM
        expiry      = next_monthly_expiry(self.DTE)
        T           = self.DTE / 365.0
        spread_width = sell_strike - buy_strike

        if spread_width <= 0:
            return _hold(["ATM and OTM strikes are identical after rounding — ATR too small for a spread"])

        long_premium  = bs_call(current_price, buy_strike,  T, iv_decimal)
        short_premium = bs_call(current_price, sell_strike, T, iv_decimal)
        net_debit     = long_premium - short_premium

        if net_debit <= 0.05:
            return _hold(["Net debit ≤ $0.05 — strikes too close; spread not viable"])

        max_profit = spread_width - net_debit

        # Greeks (net position = long ATM − short OTM)
        d_buy   = bs_delta_call(current_price, buy_strike,  T, iv_decimal)
        d_sell  = bs_delta_call(current_price, sell_strike, T, iv_decimal)
        t_buy   = bs_theta_call(current_price, buy_strike,  T, iv_decimal)
        t_sell  = bs_theta_call(current_price, sell_strike, T, iv_decimal)
        v_buy   = bs_vega(current_price, buy_strike,  T, iv_decimal)
        v_sell  = bs_vega(current_price, sell_strike, T, iv_decimal)

        net_delta = d_buy - d_sell       # positive
        net_theta = t_buy - t_sell       # negative (ATM decays faster than OTM; net buyer hurt)
        net_vega  = v_buy  - v_sell      # positive (long vega)

        # Confidence
        iv_factor   = (self.MAX_IV_RANK - iv_rank) / self.MAX_IV_RANK   # higher when IV cheaper
        macd_factor = 1.0  # already confirmed bullish
        rsi_factor  = 1.0 - abs(rsi_out.value - 47.5) / 47.5           # peaks at mid-range
        confidence  = 0.4 + 0.2 * iv_factor + 0.2 * macd_factor + 0.2 * rsi_factor

        reasons = [
            f"IV Rank {iv_rank:.0f}% < {self.MAX_IV_RANK}% — cheap options, debit spread favourable",
            macd_reason,
            f"RSI {rsi_out.value:.1f} in bullish momentum zone [{self.RSI_LOW}–{self.RSI_HIGH}]",
            f"Buy {symbol} {buy_strike:.1f}C / Sell {sell_strike:.1f}C @ net debit ${net_debit:.2f}",
            f"Max profit: ${max_profit:.2f} | Max loss: ${net_debit:.2f} | DTE: {self.DTE}",
            f"Delta: +{net_delta:.2f} | Theta: ${net_theta:.4f}/day | Vega: +{net_vega:.4f}",
            f"Expiry: {expiry[:4]}-{expiry[4:6]}-{expiry[6:]}",
        ]

        return self._enhance_signal(Signal(
            symbol=symbol,
            signal="BUY",
            entry_price=round(net_debit, 4),
            stop_loss=round(net_debit, 4),                                         # full debit at risk
            take_profit=round(net_debit + max_profit * self.TP_RATIO, 4),          # 75% of max profit
            confidence=round(confidence, 4),
            timeframe=timeframe,
            strategy_name=self.name,
            asset_class=self.asset_class,
            broker=self.broker,
            reasons=reasons,
            iv_rank=round(iv_rank, 1),
            delta=round(net_delta, 4),
            theta=round(net_theta, 6),
            vega=round(net_vega, 4),
            options_meta={
                "strategy_type": "bull_call_spread",
                "expiry": expiry,
                "spread_width": round(spread_width, 2),
                "max_profit": round(max_profit, 4),
                "underlying_price": round(current_price, 4),
                "legs": [
                    {"action": "BUY",  "right": "C", "strike": buy_strike,  "premium": round(long_premium, 4)},
                    {"action": "SELL", "right": "C", "strike": sell_strike, "premium": round(short_premium, 4)},
                ],
            },
        ), data)  # _enhance_signal: confirmation candle only (options — no trailing stop / S/R snap)
