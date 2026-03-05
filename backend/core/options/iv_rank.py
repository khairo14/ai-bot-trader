"""
IV Rank & Black-Scholes helpers
================================
Since live IV data from IBKR requires an active connection (which may not be
available during signal generation), this module derives a proxy for Implied
Volatility from Historical Volatility (HV) of the underlying's OHLCV data.

  HV (30-day, annualised) = std(log-returns over 30 candles) × √252 × 100

  IV Rank = (current_HV − 52-week min_HV) / (52-week max_HV − 52-week min_HV) × 100

  > 50  → IV relatively high  → prefer selling premium  (Iron Condor, Covered Call)
  < 30  → IV relatively low   → prefer buying  options  (Bull Call Spread)

Black-Scholes pricing and Greeks are used at signal-generation time to produce
premium estimates and display values.  When a live IBKR order is placed, the
actual mid-market price from the options chain supersedes these estimates.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Optional, Tuple

import pandas as pd
from loguru import logger

# ─── Constants ────────────────────────────────────────────────────────────────

RISK_FREE_RATE: float = 0.05   # approximate US 3-month T-bill yield (annualised)
HV_WINDOW:      int   = 30     # rolling-window size for HV calculation (trading days)
HV_52W:         int   = 252    # 52-week lookback in trading days


# ─── Normal distribution helpers (no scipy dependency) ───────────────────────

def _ncdf(x: float) -> float:
    """Cumulative standard normal distribution N(x) via math.erf."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def _npdf(x: float) -> float:
    """Standard normal probability-density function n(x)."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


# ─── Historical Volatility ────────────────────────────────────────────────────

def historical_volatility(data: pd.DataFrame, window: int = HV_WINDOW) -> float:
    """
    Annualised Historical Volatility (%) from OHLCV close prices.
    Returns a value such as 35.2 (meaning ~35% annualised vol).
    Floors at 5% to prevent near-zero issues in Black-Scholes.
    """
    if len(data) < window + 2:
        return 30.0  # fallback when data is insufficient

    log_ret = data["close"].astype(float).apply(math.log).diff().dropna()
    hv = float(log_ret.iloc[-window:].std()) * math.sqrt(252) * 100.0
    return max(5.0, hv)


def iv_rank_from_ohlcv(data: pd.DataFrame) -> float:
    """
    Compute IV Rank (0–100) using trailing Historical Volatility as an IV proxy.

    Returns 50.0 as a neutral fallback when there is insufficient history.
    """
    if len(data) < HV_52W + HV_WINDOW + 5:
        # Insufficient history — return the raw HV as a rough signal-quality indicator
        return min(100.0, max(0.0, historical_volatility(data)))

    log_ret = data["close"].astype(float).apply(math.log).diff().dropna()
    hv_series = log_ret.rolling(HV_WINDOW).std() * math.sqrt(252) * 100.0
    hv_series = hv_series.dropna()

    if len(hv_series) < 10:
        return 50.0

    recent = hv_series.iloc[-HV_52W:]
    curr = float(hv_series.iloc[-1])
    lo   = float(recent.min())
    hi   = float(recent.max())

    if (hi - lo) < 0.5:
        return 50.0  # flat-vol environment — treat as neutral

    rank = (curr - lo) / (hi - lo) * 100.0
    return min(100.0, max(0.0, rank))


# ─── Black-Scholes Pricing ────────────────────────────────────────────────────

def _d1d2(
    S: float, K: float, T: float, sigma: float, r: float = RISK_FREE_RATE
) -> Tuple[float, float]:
    """Shared d1/d2 terms for Black-Scholes."""
    sq_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sq_T)
    d2 = d1 - sigma * sq_T
    return d1, d2


def bs_call(S: float, K: float, T: float, sigma: float, r: float = RISK_FREE_RATE) -> float:
    """Black-Scholes European call price.  T in years, sigma as decimal (0.30 = 30%)."""
    if T <= 1e-6 or sigma <= 1e-6:
        return max(0.0, S - K)
    d1, d2 = _d1d2(S, K, T, sigma, r)
    return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)


def bs_put(S: float, K: float, T: float, sigma: float, r: float = RISK_FREE_RATE) -> float:
    """Black-Scholes European put price (via put-call parity)."""
    call = bs_call(S, K, T, sigma, r)
    return call - S + K * math.exp(-r * T)


# ─── Greeks ───────────────────────────────────────────────────────────────────

def bs_delta_call(S: float, K: float, T: float, sigma: float, r: float = RISK_FREE_RATE) -> float:
    """Delta of a European call (0–1)."""
    if T <= 1e-6 or sigma <= 1e-6:
        return 1.0 if S > K else 0.0
    d1, _ = _d1d2(S, K, T, sigma, r)
    return _ncdf(d1)


def bs_delta_put(S: float, K: float, T: float, sigma: float, r: float = RISK_FREE_RATE) -> float:
    """Delta of a European put (−1 to 0)."""
    return bs_delta_call(S, K, T, sigma, r) - 1.0


def bs_theta_call(S: float, K: float, T: float, sigma: float, r: float = RISK_FREE_RATE) -> float:
    """Per-day theta for a long call (negative = option value decays each day)."""
    if T <= 1e-6 or sigma <= 1e-6:
        return 0.0
    d1, d2 = _d1d2(S, K, T, sigma, r)
    term1 = -(S * _npdf(d1) * sigma) / (2.0 * math.sqrt(T))
    term2 = -r * K * math.exp(-r * T) * _ncdf(d2)
    return (term1 + term2) / 365.0


def bs_theta_put(S: float, K: float, T: float, sigma: float, r: float = RISK_FREE_RATE) -> float:
    """Per-day theta for a long put (negative = value decays each day)."""
    if T <= 1e-6 or sigma <= 1e-6:
        return 0.0
    d1, d2 = _d1d2(S, K, T, sigma, r)
    term1 = -(S * _npdf(d1) * sigma) / (2.0 * math.sqrt(T))
    term2 = r * K * math.exp(-r * T) * _ncdf(-d2)
    return (term1 + term2) / 365.0


def bs_vega(S: float, K: float, T: float, sigma: float, r: float = RISK_FREE_RATE) -> float:
    """Vega: $ change per 1% move in IV (standard convention; divide raw vega by 100)."""
    if T <= 1e-6 or sigma <= 1e-6:
        return 0.0
    d1, _ = _d1d2(S, K, T, sigma, r)
    return S * _npdf(d1) * math.sqrt(T) / 100.0


# ─── Strike & Expiry Helpers ──────────────────────────────────────────────────

def nearest_strike(price: float, around: float) -> float:
    """Round `around` to the nearest standard option strike increment for `price`."""
    if price < 25:
        step = 0.5
    elif price < 50:
        step = 1.0
    elif price < 200:
        step = 5.0
    else:
        step = 10.0
    return round(around / step) * step


def next_monthly_expiry(dte_target: int = 30) -> str:
    """
    Return the YYYYMMDD string of the next standard monthly options expiry
    (third Friday of a month) that is at least `dte_target` calendar days away.
    """
    today = date.today()
    for mo_offset in range(5):          # scan up to 5 months ahead
        raw_month = today.month + mo_offset
        year  = today.year + (raw_month - 1) // 12
        month = (raw_month - 1) % 12 + 1
        first_day  = date(year, month, 1)
        # Advance to first Friday (weekday 4)
        days_to_fri = (4 - first_day.weekday()) % 7
        third_friday = first_day + timedelta(days=days_to_fri + 14)
        if (third_friday - today).days >= dte_target:
            return third_friday.strftime("%Y%m%d")
    # Fallback: dte_target days from today
    return (today + timedelta(days=dte_target)).strftime("%Y%m%d")
