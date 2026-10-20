"""Shared fixtures: the frozen CBOE chains of 2026-10-12 and a synthetic chain with known truth."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from gex_engine import pricing, surface
from gex_engine import data
from gex_engine.data import Chain, validate

FIXTURES = Path(__file__).parent / "fixtures"
NO_FIXTURES = "CBOE fixtures not present (tests/fixtures is not distributed with the code)"


def fixture(name: str) -> Path:
    """Path of a frozen CBOE file; skips the test when the fixtures are not installed."""
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(NO_FIXTURES)
    return path


@pytest.fixture(scope="session")
def spx_chain():
    return data.load(fixture("SPX_20261012.json.gz"), "SPX")


@pytest.fixture(scope="session")
def spy_chain():
    return data.load(fixture("SPY_20261012.json.gz"), "SPY")


@pytest.fixture(scope="session")
def spx_state(spx_chain):
    return surface.build(spx_chain)


@pytest.fixture(scope="session")
def spy_state(spy_chain):
    return surface.build(spy_chain)


def synthetic_chain(asof: datetime, spot=5000.0, r=0.04, q=0.015, root="SPXW",
                    days=(0, 1, 7, 30, 90), strikes=None, oi=1000.0, half_spread=0.0,
                    smile=lambda k: 0.18 - 0.10 * k + 0.30 * k * k):
    """Chain priced exactly by generalized Black-Scholes with a known smile in ln(K/F).

    `days` are calendar-day offsets of the expiry DATES from `asof`'s date; settlement time
    follows the root, as in production.
    """
    strikes = np.arange(0.80 * spot, 1.20 * spot + 1, spot / 200) if strikes is None else np.asarray(strikes)
    rows = []
    for d in days:
        exp = (asof + timedelta(days=d)).date()
        settle = surface.settlement(root, exp)
        T = surface.years_between(asof, settle)
        if T <= 0:
            continue
        F = spot * np.exp((r - q) * T)
        for K in strikes:
            vol = smile(np.log(K / F))
            for is_call in (True, False):
                p = float(pricing.price(spot, K, T, vol, r - q, r, is_call))
                tag = f"{root}{exp:%y%m%d}{'C' if is_call else 'P'}{int(round(K * 1000)):08d}"
                rows.append((tag, root, exp, is_call, float(K), p - half_spread, p + half_spread, p,
                             10.0, oi, vol, np.nan, np.nan))
    df = pd.DataFrame(rows, columns=list(data._columns()))
    return validate(Chain(symbol="SYN", asof=asof, spot_quoted=spot, contracts=df))


@pytest.fixture
def make_synthetic():
    return synthetic_chain
