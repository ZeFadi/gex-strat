"""Clock, forwards, implied spot and vols: exact on synthetic data, consistent with CBOE at the close and in session."""

from datetime import datetime

import numpy as np
import pytest

from gex_engine import config, data, exposures, snapshot, surface
from gex_engine.data import ChainError

from .conftest import fixture


NY = config.MARKET_TZ


def test_synthetic_truth_is_recovered(make_synthetic):
    asof = datetime(2026, 3, 10, 11, 0, tzinfo=NY)
    r, q, spot = 0.04, 0.015, 5000.0
    st = surface.build(make_synthetic(asof, spot=spot, r=r, q=q), rate=r)
    s = st.series
    # Forwards and vols depend only on the quotes: recovered to numerical precision.
    assert np.allclose(s["F"], spot * np.exp((r - q) * s["T"]), rtol=1e-9)
    c = st.contracts
    # A strike is inverted from its mid exactly when its out-of-the-money option is worth at
    # least a cent; the others (far wings of short expiries) fall back to the vendor vol,
    # which on this synthetic chain is the true one.
    otm = np.where(c["is_call"], c["strike"] >= c["F"], c["strike"] < c["F"])
    otm_mid = c.assign(m=np.where(otm, c["mid"], np.nan)).groupby(["expiry", "strike"])["m"].transform("max")
    assert ((c["iv_source"] == "mid") == (otm_mid > config.MIN_TIME_VALUE)).all()
    k = np.log(c["strike"] / c["F"])
    assert np.allclose(c["sigma"], 0.18 - 0.10 * k + 0.30 * k * k, atol=1e-6)
    # The implied spot discounts the front forward at r only (documented): it misses the
    # dividend yield over the front's life, S e^{-q T_front}, here 0.04 point on 5000.
    T_front = s["T"].min()
    assert st.spot == pytest.approx(spot * np.exp(-q * T_front), rel=1e-9)
    assert np.allclose(s["b"], (r - q) + q * T_front / s["T"], atol=1e-7)


def test_time_is_measured_from_asof_not_the_wall_clock(make_synthetic):
    asof = datetime(2026, 3, 10, 15, 0, tzinfo=NY)        # one hour before the close
    st = surface.build(make_synthetic(asof, days=(0, 1)))
    T0 = st.contracts.loc[st.contracts["expiry"] == asof.date(), "T"].iloc[0]
    assert T0 * config.DAYS_PER_YEAR * 24 == pytest.approx(1.0, abs=1e-9)


def test_expired_contracts_are_dropped(spx_state, spx_chain):
    assert spx_state.dropped["expired_contracts"] > 0     # the 2026-10-12 0DTE is in the file
    assert (spx_state.contracts["T"] > 0).all()
    assert min(spx_state.contracts["expiry"]) > spx_chain.asof.date()


def test_am_settled_spx_settles_at_the_open(spx_state):
    c = spx_state.contracts
    am = c.loc[(c["root"] == "SPX") & (c["expiry"].astype(str) == "2026-10-16"), "T"].iloc[0]
    pm = c.loc[(c["root"] == "SPXW") & (c["expiry"].astype(str) == "2026-10-16"), "T"].iloc[0]
    assert (pm - am) * config.DAYS_PER_YEAR * 24 == pytest.approx(6.5, abs=1e-9)


@pytest.mark.parametrize("asof", [datetime(2026, 1, 15, 10, 0, tzinfo=NY),    # EST, UTC-5
                                  datetime(2026, 7, 15, 10, 0, tzinfo=NY)])   # EDT, UTC-4
def test_0dte_hours_are_right_in_winter_and_summer(make_synthetic, asof):
    st = surface.build(make_synthetic(asof, days=(0, 7)))
    T0 = st.contracts.loc[st.contracts["expiry"] == asof.date(), "T"].iloc[0]
    assert T0 * config.DAYS_PER_YEAR * 24 == pytest.approx(6.0, abs=1e-9)


def test_all_expired_raises(make_synthetic):
    asof = datetime(2026, 3, 10, 16, 30, tzinfo=NY)
    with pytest.raises(ChainError):
        surface.build(make_synthetic(asof, days=(0,)))


@pytest.mark.parametrize("state", ["spx_state", "spy_state"])
def test_cboe_forwards_are_consistent(state, request):
    st = request.getfixturevalue(state)
    s = st.series.sort_values("T")
    assert (s["forward_source"] == "parity").mean() > 0.9
    # Implied spot within 10 bp of the vendor's (the gap is the 16:00 vs 16:15 / post-close print)
    assert abs(st.spot / st.spot_quoted - 1) < 10e-4
    # Carry of every parity-priced series inside a plausible band (rates minus dividends)
    assert s.loc[s["T"] > 7 / 365, "b"].between(-0.05, 0.10).all()


def test_cboe_vols_agree_with_vendor_near_the_money(spx_state):
    c = spx_state.contracts
    m = ((c["strike"] / spx_state.spot - 1).abs() < 0.03) & (c["iv_source"] == "mid") & (c["T"] > 7 / 365)
    diff = (c.loc[m, "sigma"] - c.loc[m, "iv_vendor"]).abs()
    assert diff.median() < 0.005                           # half a vol point
    assert spx_state.dropped["unpriced_oi"] == 0


def test_asof_is_the_earlier_of_index_and_options(spx_chain):
    """If the index print runs ahead of the delayed option data, time is measured from the options."""
    import gzip
    import json

    from gex_engine import data
    with gzip.open(fixture("SPX_20261012.json.gz"), "rt", encoding="utf-8") as fh:
        p = json.load(fh)
    p["data"]["last_trade_time"] = "2026-10-13T10:15:00"
    for o in p["data"]["options"]:
        o["last_trade_time"] = "2026-10-13T10:00:00"
    chain = data.parse(p, "SPX")
    assert chain.asof == datetime(2026, 10, 13, 10, 0, tzinfo=NY)
    assert chain.meta["underlying_time"].startswith("2026-10-13T10:15")


@pytest.mark.parametrize("symbol,am_root,pm_root", [("NDX", "NDX", "NDXP"), ("RUT", "RUT", "RUTW"), ("XSP", None, "XSP")])
def test_new_indices(symbol, am_root, pm_root):
    from gex_engine import data
    st = surface.build(data.load(fixture(f"{symbol}_20261012.json.gz"), symbol))
    assert abs(st.spot / st.spot_quoted - 1) < 20e-4
    assert (st.series["forward_source"] == "parity").mean() > 0.8
    assert st.dropped["unpriced_oi"] == 0
    c = st.contracts
    if am_root:                                           # AM monthly settles 6.5 h before the PM weekly
        am = c.loc[(c["root"] == am_root) & (c["expiry"].astype(str) == "2026-10-16"), "T"].iloc[0]
        pm = c.loc[(c["root"] == pm_root) & (c["expiry"].astype(str) == "2026-10-16"), "T"].iloc[0]
        assert (pm - am) * config.DAYS_PER_YEAR * 24 == pytest.approx(6.5, abs=1e-9)


def test_bid_ask_vols_bracket_the_mid(spx_state):
    c = spx_state.contracts
    assert (c["sigma_bid"] <= c["sigma"] + 1e-12).all() and (c["sigma"] <= c["sigma_ask"] + 1e-12).all()
    q = c["iv_source"] == "mid"
    assert (c.loc[q, "sigma_ask"] - c.loc[q, "sigma_bid"]).median() < 0.01     # under one vol point
    assert (c.loc[~q, "sigma_bid"] == c.loc[~q, "sigma"]).all()


def test_atm_vol_interpolates_total_variance(make_synthetic):
    asof = datetime(2026, 3, 10, 11, 0, tzinfo=NY)
    st = surface.build(make_synthetic(asof, days=(1, 7, 30), smile=lambda k: 0.2 + 0 * k))
    assert surface.atm_vol(st, 3 / 365) == pytest.approx(0.2, abs=1e-6)
    assert surface.one_day_move(st) == pytest.approx(st.spot * 0.2 * np.sqrt(1 / 365), rel=1e-6)




@pytest.fixture(scope="module", params=["SPX", "NDX"])
def session(request):
    return surface.build(data.load(fixture(f"session_{request.param}_2026-10-13_0933.json.gz"), request.param))


def test_index_and_option_times_agree_in_session(session):
    """Both are 15 minutes behind the file: the as-of rule takes either, they are equal."""
    assert session.meta["underlying_time"] == session.meta["options_last_trade"]
    assert session.asof == datetime(2026, 10, 13, 9, 32, 58 + (session.symbol == "NDX"), tzinfo=NY)


def test_front_is_0dte_with_the_right_clock(session):
    f = snapshot.build(session, "front")["front"]
    assert f["is_0dte"] is True and f["expiry"] == "2026-10-13"
    assert f["hours_to_settlement"] == pytest.approx(6.45, abs=0.01)       # 09:33 -> 16:00


def test_0dte_gamma_matches_vendor_near_the_money(session):
    """A wrong clock would show here first: 15 minutes off moves 0DTE ATM gamma by ~2% at 6 h."""
    c = session.contracts
    z = c.loc[(c["expiry"] == min(c["expiry"])) & (c["iv_source"] == "mid") & (c["gamma_vendor"] >= 5e-4)
              & ((c["strike"] / session.spot - 1).abs() < 0.01)]
    ex = exposures.contract_exposures(z, session.spot, session.rate)
    assert len(ex) > 30
    assert np.median(ex["gamma"] / ex["gamma_vendor"]) == pytest.approx(1.0, abs=0.02)


def test_spot_is_consistent_in_session(session):
    assert abs(session.spot / session.spot_quoted - 1) < 5e-4


def test_file_before_the_delayed_open_is_refused_clearly():
    chain = data.load(fixture("open_SPY_2026-10-13_noquotes.json.gz"), "SPY")
    with pytest.raises(ChainError, match="no two-sided quote yet"):
        surface.build(chain)
