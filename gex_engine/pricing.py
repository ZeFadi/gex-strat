"""European option pricing, Greeks and implied volatility, vectorized.

Generalized Black-Scholes with a cost of carry b per expiry (Haug's notation):

    F = S e^{bT},   DF = e^{-rT},   d1 = [ln(S/K) + (b + sigma^2/2) T] / (sigma sqrt T),   d2 = d1 - sigma sqrt T

b is not assumed: it is backed out of the parity forward of each expiry, b = ln(F/S)/T, so
dividends, borrow and the AM/PM settlement basis are all carried by F. With b = r - q this
is Merton's model; written on F it is Black-76, which is how the prices are inverted.

All Greeks are with respect to SPOT, per unit of the underlying, for one option:

    delta  dV/dS          call  e^{(b-r)T} N(d1)        put  e^{(b-r)T} (N(d1) - 1)
    gamma  d2V/dS2        e^{(b-r)T} n(d1) / (S sigma sqrt T)
    vanna  d2V/dS dsigma  -e^{(b-r)T} n(d1) d2 / sigma               (per 1.00 of vol)
    charm  d2V/dS dt      -dDelta/dT, per YEAR of calendar time passing

with  dDelta/dT = e^{(b-r)T} [ (b-r) N(+-d1)_adj + n(d1) (b/(sigma sqrt T) - d2/(2T)) ],
N(+-d1)_adj = N(d1) for a call and N(d1) - 1 for a put. tests/test_pricing.py checks every
one of these against central finite differences of the price.
"""

from __future__ import annotations

import numpy as np
from scipy.special import ndtr

from . import config

_INV_SQRT_2PI = 1.0 / np.sqrt(2.0 * np.pi)


def npdf(x):
    """Standard normal density."""
    return _INV_SQRT_2PI * np.exp(-0.5 * x * x)


def _d1_d2(S, K, T, sigma, b):
    """d1, d2 and sigma sqrt(T) of the generalized Black-Scholes formula."""
    sst = sigma * np.sqrt(T)
    d1 = (np.log(S / K) + (b + 0.5 * sigma * sigma) * T) / sst
    return d1, d1 - sst, sst


def price(S, K, T, sigma, b, r, is_call):
    """Option value (generalized Black-Scholes)."""
    d1, d2, _ = _d1_d2(S, K, T, sigma, b)
    carry, df = np.exp((b - r) * T), np.exp(-r * T)
    call = S * carry * ndtr(d1) - K * df * ndtr(d2)
    put = K * df * ndtr(-d2) - S * carry * ndtr(-d1)
    return np.where(is_call, call, put)


def greeks(S, K, T, sigma, b, r, is_call):
    """Delta, gamma, vanna and charm (see module docstring for units)."""
    d1, d2, sst = _d1_d2(S, K, T, sigma, b)
    carry = np.exp((b - r) * T)
    pdf = npdf(d1)
    n_adj = np.where(is_call, ndtr(d1), ndtr(d1) - 1.0)
    delta = carry * n_adj
    gamma = carry * pdf / (S * sst)
    vanna = -carry * pdf * d2 / sigma
    ddelta_dT = carry * ((b - r) * n_adj + pdf * (b / sst - d2 / (2.0 * T)))
    return {"delta": delta, "gamma": gamma, "vanna": vanna, "charm": -ddelta_dT}


def gamma(S, K, T, sigma, b, r):
    """Gamma only: the inner loop of the GEX(S) profile, so kept to the bare formula."""
    sst = sigma * np.sqrt(T)
    d1 = (np.log(S / K) + (b + 0.5 * sigma * sigma) * T) / sst
    return np.exp((b - r) * T) * npdf(d1) / (S * sst)


def black76_undiscounted(F, K, T, sigma, is_call):
    """Forward (undiscounted) Black-76 value: E[(F_T - K)+] or E[(K - F_T)+]."""
    sst = sigma * np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sst * sst) / sst
    d2 = d1 - sst
    return np.where(is_call, F * ndtr(d1) - K * ndtr(d2), K * ndtr(-d2) - F * ndtr(-d1))


def implied_vol(premium, F, K, T, df, is_call, min_time_value=0.0):
    """Black-76 implied vol of discounted `premium`, vectorized; NaN where none exists.

    Bisection on [IV_MIN, IV_MAX]: the price is monotone in sigma, so bisection always
    converges, which Newton does not far from the money where vega vanishes. Sixty halvings
    reach machine precision; on 30,000 contracts that costs tens of milliseconds.

    A premium outside the no-arbitrage band (below intrinsic on the forward, or above F for
    a call / K for a put) has no implied vol and returns NaN. So does one whose time value
    (premium above discounted intrinsic) is below `min_time_value`: a deep in-the-money
    price is almost all intrinsic, and inverting the rounding left over returns any vol at
    all (0.95 instead of 0.17 on a synthetic 0DTE before this rule).
    """
    premium, F, K, T, is_call, min_tv = np.broadcast_arrays(
        np.asarray(premium, float), np.asarray(F, float), np.asarray(K, float),
        np.asarray(T, float), np.asarray(is_call, bool), np.asarray(min_time_value, float))
    undiscounted = premium / df
    intrinsic = np.where(is_call, np.maximum(F - K, 0.0), np.maximum(K - F, 0.0))
    upper = np.where(is_call, F, K)
    lo_v = black76_undiscounted(F, K, T, np.full_like(F, config.IV_MIN), is_call)
    hi_v = black76_undiscounted(F, K, T, np.full_like(F, config.IV_MAX), is_call)
    ok = (np.isfinite(undiscounted) & (undiscounted - intrinsic > np.maximum(min_tv / df, 0.0))
          & (undiscounted < upper)
          & (undiscounted >= lo_v) & (undiscounted <= hi_v) & (T > 0))

    lo = np.full(F.shape, config.IV_MIN)
    hi = np.full(F.shape, config.IV_MAX)
    for _ in range(config.IV_BISECTION_STEPS):
        mid = 0.5 * (lo + hi)
        above = black76_undiscounted(F, K, T, mid, is_call) > undiscounted
        hi = np.where(above, mid, hi)
        lo = np.where(above, lo, mid)
    return np.where(ok, 0.5 * (lo + hi), np.nan)
