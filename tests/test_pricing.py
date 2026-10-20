"""Greeks against finite differences of the price, parity, and implied-vol round trips."""

import numpy as np
import pytest

from gex_engine import pricing

# Grid spanning OTM / ATM / ITM, a 0DTE-like and a one-year tenor, positive and negative carry.
S = 100.0
K = np.array([80.0, 95.0, 100.0, 103.0, 120.0])
CASES = [(T, sigma, b, r) for T in (0.002, 0.08, 1.0) for sigma in (0.12, 0.45)
         for b, r in ((0.03, 0.04), (-0.01, 0.02))]


def _bump(f, x, h):
    return (f(x + h) - f(x - h)) / (2 * h)


@pytest.mark.parametrize("T,sigma,b,r", CASES)
@pytest.mark.parametrize("is_call", [True, False])
def test_greeks_match_finite_differences(T, sigma, b, r, is_call):
    g = pricing.greeks(S, K, T, sigma, b, r, is_call)
    # Spot bump scaled to the move the option can see (S sigma sqrt T), or 0DTE truncation error dominates.
    h_s, h_v, h_t = 1e-3 * S * sigma * np.sqrt(T), 1e-5, 1e-7

    delta = lambda s: pricing.greeks(s, K, T, sigma, b, r, is_call)["delta"]
    fd_delta = _bump(lambda s: pricing.price(s, K, T, sigma, b, r, is_call), S, h_s)
    fd_gamma = _bump(delta, S, h_s)
    fd_vanna = _bump(lambda v: pricing.greeks(S, K, T, v, b, r, is_call)["delta"], sigma, h_v)
    # charm is dDelta/dt = -dDelta/dT (calendar time passing, carry held fixed)
    fd_charm = -_bump(lambda t: pricing.greeks(S, K, t, sigma, b, r, is_call)["delta"], T, min(h_t, T / 10))

    scale = lambda x: np.maximum(np.abs(x), 1e-6)
    assert np.all(np.abs(g["delta"] - fd_delta) / scale(fd_delta) < 1e-4)
    assert np.all(np.abs(g["gamma"] - fd_gamma) <= 1e-4 * scale(fd_gamma) + 1e-9)
    assert np.all(np.abs(g["vanna"] - fd_vanna) <= 1e-4 * scale(fd_vanna) + 1e-7)
    assert np.all(np.abs(g["charm"] - fd_charm) <= 1e-3 * scale(fd_charm) + 1e-5)


def test_gamma_shortcut_equals_full_greeks():
    for T, sigma, b, r in CASES:
        assert np.allclose(pricing.gamma(S, K, T, sigma, b, r),
                           pricing.greeks(S, K, T, sigma, b, r, True)["gamma"], rtol=1e-14)


@pytest.mark.parametrize("T,sigma,b,r", CASES)
def test_put_call_parity(T, sigma, b, r):
    c = pricing.price(S, K, T, sigma, b, r, True)
    p = pricing.price(S, K, T, sigma, b, r, False)
    assert np.allclose(c - p, S * np.exp((b - r) * T) - K * np.exp(-r * T), atol=1e-10)


def test_known_value():
    # Hull, Options Futures and Other Derivatives: S=42, K=40, r=10%, sigma=20%, T=0.5 -> 4.76 / 0.81
    c = pricing.price(42.0, 40.0, 0.5, 0.2, 0.1, 0.1, True)
    p = pricing.price(42.0, 40.0, 0.5, 0.2, 0.1, 0.1, False)
    assert c == pytest.approx(4.7594, abs=1e-4) and p == pytest.approx(0.8086, abs=1e-4)


@pytest.mark.parametrize("T", [0.001, 0.05, 2.0])
def test_implied_vol_round_trip(T):
    r, b = 0.04, 0.02
    sig = np.array([0.08, 0.15, 0.30, 0.90])
    F, df = S * np.exp(b * T), np.exp(-r * T)
    for is_call in (True, False):
        prem = pricing.price(S, K[:, None], T, sig[None, :], b, r, is_call)
        iv = pricing.implied_vol(prem, F, K[:, None], T, df, is_call)
        # Where the price is distinguishable from intrinsic, the vol comes back exactly.
        meaningful = prem - np.where(is_call, np.maximum(S * np.exp((b - r) * T) - K[:, None] * df, 0),
                                     np.maximum(K[:, None] * df - S * np.exp((b - r) * T), 0)) > 1e-8
        assert np.allclose(iv[meaningful], np.broadcast_to(sig, prem.shape)[meaningful], atol=1e-7)


def test_implied_vol_rejects_arbitrage():
    F, df, T = 100.0, 1.0, 0.1
    assert np.isnan(pricing.implied_vol(4.0, F, 95.0, T, df, True))     # below intrinsic 5
    assert np.isnan(pricing.implied_vol(101.0, F, 95.0, T, df, True))   # above F
    assert np.isnan(pricing.implied_vol(np.nan, F, 95.0, T, df, True))
