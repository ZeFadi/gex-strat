"""Exposures, levels, profile and zero gamma, front expiry, and scenario re-pricing."""

from datetime import datetime

import numpy as np
import pytest

from gex_engine import config, exposures, snapshot, surface


NY = config.MARKET_TZ


@pytest.mark.parametrize("view", ["all", "front", "2026-10-16"])
def test_profile_at_spot_equals_net_gex_equals_strike_bars(spx_state, view):
    p = snapshot.build(spx_state, view)
    net = p["summary"]["net_gex"]
    sub = exposures.select(spx_state, view)
    ex = exposures.contract_exposures(sub, spx_state.spot, spx_state.rate)
    bars = exposures.by_strike(ex)
    assert ex["gex"].sum() == pytest.approx(net, rel=1e-10)
    assert bars["net_gex"].sum() == pytest.approx(net, rel=1e-10)
    assert p["summary"]["call_gex"] + p["summary"]["put_gex"] == pytest.approx(net, rel=1e-10)
    book = exposures.Book.of(sub, spx_state.rate)
    assert float(np.interp(spx_state.spot, p["profile"]["spot"], p["profile"]["net_gex"])) == \
        pytest.approx(net, rel=2e-3)                       # grid interpolation only
    assert exposures.net_gex(book, spx_state.spot)[0] == pytest.approx(net, rel=1e-12)


@pytest.mark.parametrize("state", ["spx_state", "spy_state"])
def test_zero_gamma_is_a_root(state, request):
    st = request.getfixturevalue(state)
    book = exposures.Book.of(st.contracts, st.rate)
    prof = exposures.profile(book, st.spot)
    assert prof["roots"], "fixture has a sign change within +/-6%"
    scale = np.abs(prof["values"]).max()
    for root in prof["roots"]:
        assert abs(exposures.net_gex(book, root)[0]) < 1e-9 * scale
        left, right = exposures.net_gex(book, [root - 0.01 * st.spot / 100, root + 0.01 * st.spot / 100])
        assert np.sign(left) != np.sign(right)


def test_zero_gamma_matches_closed_form(make_synthetic):
    """One call at K1, one put at K2, same OI, flat vol, zero carry and rate.

    n(d1(S;K1)) = n(d1(S;K2)) with d1(K1) = -d1(K2) gives S* = sqrt(K1 K2) exp(-sigma^2 T / 2).
    """
    asof = datetime(2026, 3, 10, 11, 0, tzinfo=NY)
    sigma, K1, K2 = 0.2, 5100.0, 4900.0
    st = surface.build(make_synthetic(asof, q=0.0, r=0.0, days=(30,), smile=lambda k: sigma + 0 * k),
                       rate=0.0)
    c = st.contracts.copy()
    c["oi"] = 0.0
    c.loc[c["is_call"] & (c["strike"] == K1), "oi"] = 1000.0
    c.loc[~c["is_call"] & (c["strike"] == K2), "oi"] = 1000.0
    T = c["T"].iloc[0]
    prof = exposures.profile(exposures.Book.of(c, 0.0), st.spot)
    assert prof["zero_gamma"] == pytest.approx(np.sqrt(K1 * K2) * np.exp(-0.5 * sigma**2 * T), abs=1e-5)


def test_units_of_one_contract(make_synthetic):
    """GEX of one long call = gamma * 100 * S^2 / 100: the $-delta change for a 1% move."""
    asof = datetime(2026, 3, 10, 11, 0, tzinfo=NY)
    st = surface.build(make_synthetic(asof, days=(30,), oi=1.0))
    ex = exposures.contract_exposures(st.contracts, st.spot, st.rate)
    row = ex.loc[ex["is_call"]].iloc[40]
    S = st.spot
    bumped = exposures.contract_exposures(st.contracts.loc[[row.name]], S * 1.0001, st.rate)
    ddelta_dollars = (bumped["delta"].iloc[0] - row["delta"]) / 0.0001 * 0.01 * 100 * S
    assert row["gex"] == pytest.approx(ddelta_dollars, rel=1e-3)


def test_max_pain_brute_force(spx_state):
    sub = exposures.select(spx_state, "front")
    mp = exposures.max_pain(sub)
    K, oi, call = sub["strike"].to_numpy(), sub["oi"].to_numpy(), sub["is_call"].to_numpy()
    pay = {s: sum(o * (max(s - k, 0) if c else max(k - s, 0)) for k, o, c in zip(K, oi, call))
           for s in np.unique(K)}
    assert mp == min(pay, key=pay.get)
    assert exposures.max_pain(spx_state.contracts) is None      # undefined across expiries


def test_walls_sit_on_their_side_of_spot(spx_state):
    for view in ("all", "front"):
        s = snapshot.build(spx_state, view)["summary"]
        assert s["call_wall"] >= spx_state.spot >= s["put_wall"]


def test_front_metrics(spx_state):
    f = snapshot.build(spx_state, "all")["front"]
    assert f["expiry"] == "2026-10-13" and f["is_0dte"] is False     # as-of is the 10-12 close
    assert f["hours_to_settlement"] == pytest.approx(23.75, abs=0.01)
    assert f["expected_move_pts"] == pytest.approx(spx_state.spot * f["atm_iv"] *
                                                    np.sqrt(f["hours_to_settlement"] / 24 / 365))
    assert 0 < f["share_of_book_abs_gex"] < 1


def test_sticky_moneyness_equals_sticky_strike_on_a_flat_smile(make_synthetic):
    asof = datetime(2026, 3, 10, 11, 0, tzinfo=NY)
    st = surface.build(make_synthetic(asof, days=(7, 30), smile=lambda k: 0.2 + 0 * k))
    a = exposures.Book.of(st.contracts, st.rate)
    b = exposures.Book.of(st.contracts, st.rate, sticky="moneyness", spot0=st.spot)
    grid = st.spot * np.array([0.95, 1.0, 1.04])
    assert np.allclose(exposures.net_gex(a, grid), exposures.net_gex(b, grid), rtol=1e-6)


def test_sticky_moneyness_moves_vols_along_the_skew(spx_state):
    b = exposures.Book.of(spx_state.contracts, spx_state.rate, sticky="moneyness", spot0=spx_state.spot)
    assert np.allclose(b.vols(spx_state.spot), b.sigma)
    atm = np.argmin(np.abs(b.K - spx_state.spot) + 1e3 * (b.T < 20 / 365) + 1e3 * (b.T > 40 / 365))
    # The smile moves with spot: a fixed strike is read further to the right of a downward
    # equity skew when spot falls, so its vol FALLS (ATM vol, at K = S, stays put).
    assert b.vols(spx_state.spot * 0.97)[atm] < b.sigma[atm]


def test_band(spx_state):
    band = exposures.zero_gamma_band(spx_state.contracts, spx_state.spot, spx_state.rate)
    names = [v["variant"] for v in band["variants"]]
    assert names[0].startswith("Base") and "Sticky moneyness" in names and "Without the front expiry" in names
    base = band["variants"][0]["zero_gamma"]
    assert band["low"] <= base <= band["high"]
    assert band["indeterminate"] is False and band["high"] < spx_state.spot


def test_explicit_positions(spx_state):
    sub = exposures.select(spx_state, "front")
    default = exposures.contract_exposures(sub, spx_state.spot, spx_state.rate)
    flipped = exposures.contract_exposures(sub, spx_state.spot, spx_state.rate,
                                           position=-exposures.dealer_position(sub))
    assert np.allclose(flipped["gex"], -default["gex"])


def test_projection_concentrates_gamma(spx_state):
    from gex_engine import exposures
    p = exposures.front_projection(spx_state)
    labels = [s["label"] for s in p["series"]]
    assert labels[0] == "now" and labels[-1] == "15:50"
    now = exposures.select(spx_state, "front")
    now = now.loc[(now["strike"] - spx_state.spot).abs() <= 2 * 31]   # same window, ~2 EM
    near = int(np.argmin(np.abs(np.array(p["strikes"]) - spx_state.spot)))
    gex_near = [abs(s["net_gex"][near]) for s in p["series"]]
    assert gex_near[-1] > gex_near[0]                                 # gamma grows at the money into the close




def test_small_spot_shock_matches_gex(spx_state):
    """Dollar delta change for a tiny move, scaled to 1%, equals net GEX (first order)."""
    sub, S = spx_state.contracts, spx_state.spot
    h = 1e-5
    d_up = exposures.dealer_delta(sub, S * (1 + h), spx_state.rate)
    d_dn = exposures.dealer_delta(sub, S * (1 - h), spx_state.rate)
    gex = exposures.contract_exposures(sub, S, spx_state.rate)["gex"].sum()
    assert (d_up - d_dn) / (2 * h) * 0.01 * S == pytest.approx(gex, rel=1e-4)


def test_small_vol_shock_matches_vex(spx_state):
    sub, S = spx_state.contracts, spx_state.spot
    h = 1e-5
    dv = (exposures.dealer_delta(sub, S, spx_state.rate, vol_shift=h)
          - exposures.dealer_delta(sub, S, spx_state.rate, vol_shift=-h)) / (2 * h)
    vex = exposures.contract_exposures(sub, S, spx_state.rate)["vex"].sum()
    assert dv * S / 100 == pytest.approx(vex, rel=1e-4)


def _one_contract(make_synthetic, root, is_call, strike_offset):
    asof = datetime(2026, 3, 10, 11, 0, tzinfo=NY)
    st = surface.build(make_synthetic(asof, root=root, days=(0, 7)), rate=0.04)
    c = st.contracts.copy()
    c["oi"] = 0.0
    K = c.loc[c["expiry"] == asof.date(), "strike"].sub(st.spot + strike_offset).abs().idxmin()
    k_val = c.at[K, "strike"]
    c.loc[(c["expiry"] == asof.date()) & (c["strike"] == k_val) & (c["is_call"] == is_call), "oi"] = 10.0
    return st, c.loc[c["oi"] > 0]


def test_expiry_cash_settled_unwinds_the_hedge(make_synthetic):
    st, c = _one_contract(make_synthetic, "SPXW", True, -200)       # deep ITM call
    after = exposures.dealer_delta(c, st.spot, st.rate, dt=1 / 365)
    assert after == 0.0


def test_expiry_physical_settles_into_shares(make_synthetic):
    st, c = _one_contract(make_synthetic, "SPY", True, -200)        # deep ITM call
    after = exposures.dealer_delta(c, st.spot, st.rate, dt=1 / 365)
    assert after == pytest.approx(10 * config.MULTIPLIER)          # dealer long 10 calls -> long 1000 shares


def test_next_close():
    assert surface.next_close(datetime(2026, 10, 12, 16, 14, tzinfo=NY)) == datetime(2026, 10, 13, 16, 0, tzinfo=NY)
    assert surface.next_close(datetime(2026, 10, 13, 10, 0, tzinfo=NY)) == datetime(2026, 10, 13, 16, 0, tzinfo=NY)
    assert surface.next_close(datetime(2026, 10, 16, 17, 0, tzinfo=NY)) == datetime(2026, 10, 19, 16, 0, tzinfo=NY)


def test_futures_conversion():
    assert exposures.hedge_units("SPX", 50_000) == pytest.approx(1000)      # 50k index units = 1000 ES
    assert exposures.hedge_units("XSP", 500_000) == pytest.approx(1000)
    assert exposures.hedge_units("SPY", 1234) == 1234


def test_table_signs(spx_state):
    t = exposures.scenario_table(spx_state, spx_state.contracts).set_index("scenario")
    # Positive vanna book under the convention: an IV drop leaves dealers short delta -> they buy.
    assert t.loc["-5 vol pts", "hedge_flow_usd"] > 0 > t.loc["+5 vol pts", "hedge_flow_usd"]
    assert np.isfinite(t["hedge_flow_hedge_units"]).all()


def test_year_fraction_is_right_across_daylight_saving():
    """Python subtracts same-tz datetimes on the wall clock; the engine must not (1 h off in Nov)."""
    before = datetime(2026, 10, 30, 16, 0, tzinfo=NY)               # EDT, UTC-4
    after = datetime(2026, 11, 6, 16, 0, tzinfo=NY)                 # EST, UTC-5, 7 days + 1 hour later
    assert surface.years_between(before, after) * config.DAYS_PER_YEAR * 24 == pytest.approx(7 * 24 + 1)
