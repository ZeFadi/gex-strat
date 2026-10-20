"""Backtest machinery on synthetic data: alignment, no look-ahead, costs, rolls, statistics."""

from datetime import date

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from gex_engine import backtest, config, stats


def _data(n=1500, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2015-01-01", periods=n)
    es = pd.Series(rng.normal(0.0003, 0.01, n), index=idx)
    return pd.DataFrame({"es": es, "ux1": rng.normal(-0.002, 0.05, n), "ux1_price": 18.0,
                         "gex": rng.normal(3e9, 3e9, n), "vix": rng.normal(18, 5, n).clip(9),
                         "rv_parkinson": rng.normal(10, 3, n).clip(1)}, index=idx)


@pytest.mark.parametrize("lag", [1, 2])
def test_perfect_foresight_only_pays_at_the_matching_lag(lag):
    """A regime equal to the sign of the return `lag` sessions ahead is perfect at that lag only."""
    d = _data()
    oracle = np.sign(d["es"].shift(-lag)).fillna(1.0)
    right = stats.sharpe(backtest.run("exposure", oracle, d, lag)["gross"])
    wrong = stats.sharpe(backtest.run("exposure", oracle, d, 3 - lag)["gross"])
    assert right > 10 and abs(wrong) < 1.5


def test_regimes_have_no_look_ahead():
    d = _data()
    a = backtest.regimes(d)
    d2 = d.copy()
    d2.iloc[-1, d2.columns.get_loc("gex")] = -1e15
    d2.iloc[-1, d2.columns.get_loc("vix")] = 1e3
    b = backtest.regimes(d2)
    pd.testing.assert_frame_equal(a.iloc[:-1], b.iloc[:-1])


def test_vol_weight_uses_only_the_past():
    d = _data()
    w = backtest.vol_weight(d["es"])
    d2 = d.copy()
    d2.iloc[800, d2.columns.get_loc("es")] = 0.5
    w2 = backtest.vol_weight(d2["es"])
    assert np.allclose(w.iloc[:800].dropna(), w2.iloc[:800].dropna())
    assert w.max() <= config.MAX_WEIGHT


def test_costs_follow_changes_of_the_held_position():
    d = _data(300)
    const = backtest.run("exposure", pd.Series(1.0, index=d.index), d.assign(es=0.01), 2)
    # constant regime and constant vol -> weight constant after warm-up: one entry trade only
    assert (const["cost"] > 0).sum() == 1
    flip = pd.Series(np.where(np.arange(300) % 2 == 0, 1.0, -1.0), index=d.index)
    res = backtest.run("exposure", flip, d.assign(es=np.tile([0.01, -0.01], 150)), 2)
    traded = res["position"].diff().abs()
    assert np.allclose(res["cost"].iloc[1:], traded.iloc[1:] * config.COST_ES)


def test_ux1_roll_adjustment():
    idx = pd.bdate_range("2016-04-18", periods=5)                    # roll session 2016-04-21
    p = pd.DataFrame({"ux1": [20.0, 20.0, 20.0, 25.0, 25.0], "ux2": [25.0, 25.0, 25.0, 26.0, 26.0]}, index=idx)
    r = backtest.ux1_returns(p)
    assert r[pd.Timestamp("2016-04-21")] == pytest.approx(0.0)       # 25 (new UX1) vs 25 (old UX2)
    assert r[pd.Timestamp("2016-04-22")] == pytest.approx(0.0)


@pytest.mark.parametrize("year,month,expiry", [(2016, 4, date(2016, 4, 20)), (2024, 12, date(2024, 12, 18)),
                                               (2014, 12, date(2014, 12, 17))])
def test_vix_expiry_rule(year, month, expiry):
    assert backtest.vix_expiry(year, month) == expiry


def test_roll_convention_change():
    idx = pd.bdate_range("2014-11-17", "2014-12-24")
    rolls = backtest.vix_roll_days(idx)
    assert pd.Timestamp("2014-11-19") in rolls                       # expiry session (old convention)
    assert pd.Timestamp("2014-12-18") in rolls                       # session after (new convention)


def test_es_excess_return():
    idx = pd.to_datetime(["2020-01-02", "2020-01-03", "2020-01-06"])
    p = pd.DataFrame({"spxt": [100.0, 101.0, 101.0], "us3m_pct": [3.6, 3.6, 3.6]}, index=idx)
    r = backtest.es_returns(p)
    assert r.iloc[1] == pytest.approx(0.01 - 0.036 / 360)
    assert r.iloc[2] == pytest.approx(0.0 - 0.036 * 3 / 360)         # weekend: three days of cash


def test_episodes():
    s = pd.Series([1, -1, -1, 1, -1, 1, 1, -1, -1, -1])
    assert backtest.episodes(s) == {"negative_days": 6, "episodes": 3, "longest": 3}


def test_deflated_sharpe():
    sr, T = 1.0, 2520
    one = stats.deflated_sharpe(sr, 1, T, 0.0, 3.0)
    assert one == pytest.approx(norm.cdf(sr / np.sqrt(252) * np.sqrt(T - 1)
                                         / np.sqrt(1 + 0.5 * (sr / np.sqrt(252)) ** 2)))
    many = [stats.deflated_sharpe(sr, n, T, 0.0, 3.0) for n in (1, 10, 100)]
    assert many[0] > many[1] > many[2]


def test_summary_and_bootstrap():
    rng = np.random.default_rng(3)
    r = pd.Series(rng.normal(0.0004, 0.01, 5000))
    s = stats.summary(r)
    assert s["sharpe"] == pytest.approx(r.mean() / r.std() * np.sqrt(252))
    assert s["sharpe_se"] == pytest.approx(np.sqrt(252 / 5000), rel=0.02)   # Lo (2002), iid normal
    lo, hi = stats.bootstrap_sharpe(r, 20, 500, 1)
    assert lo < s["sharpe"] < hi and (lo, hi) == stats.bootstrap_sharpe(r, 20, 500, 1)
    assert stats.max_drawdown([0.1, -0.3, 0.1, -0.1]) == pytest.approx(0.3)


def test_placebo_is_deterministic_and_null_for_noise():
    d = _data(1200)
    reg = backtest.regimes(d)["gex_sign"]
    a = backtest.placebo_pvalue("exposure", reg, d, 2, draws=50)
    assert a == backtest.placebo_pvalue("exposure", reg, d, 2, draws=50)
    assert 0.0 <= a["p_better"] <= 1.0 and 0.0 <= a["p_worse"] <= 1.0


def test_trials_log_is_append_only(tmp_path):
    t = pd.DataFrame({"strategy": ["exposure | gex_sign", "exposure | always"], "lag": [2, 2],
                      "trial": [True, False], "sample": ["full", "full"]})
    assert backtest.register_trials(t, tmp_path) == 1
    assert backtest.register_trials(t, tmp_path) == 1                # same configuration: no new row
    t2 = t.assign(lag=1)
    assert backtest.register_trials(t2, tmp_path) == 2


# --------------------------------------------------------------------------- vol forecast trial

def _vol_world(n=2600, effect=150.0, seed=3):
    """Sessions where the next variance rises by `effect` (%^2) times (1 - GEX percentile rank).

    Log-variance is AR(1) around a 16% vol, VIX^2 sees it with noise, the realized Parkinson
    variance is the true one times a mean-one lognormal error.
    """
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2012-01-02", periods=n)
    gex = pd.Series(np.cumsum(rng.normal(size=n)) * 0.2 + rng.normal(size=n), index=idx)
    pct = gex.rolling(config.FORECAST_RANK_WINDOW).rank(pct=True).fillna(0.5).to_numpy()
    lv = np.empty(n)
    lv[0] = np.log(256.0)
    for t in range(1, n):
        lv[t] = np.log(256.0) + 0.97 * (lv[t - 1] - np.log(256.0)) + 0.2 * rng.normal()
    true = np.exp(lv) + effect * np.r_[0.0, 1.0 - pct[:-1]]          # variance of t set by GEX at t-1
    realized = true * np.exp(rng.normal(scale=0.5, size=n) - 0.125)
    return pd.DataFrame({"rv_parkinson": np.sqrt(realized), "vix": np.sqrt(1.2 * true * np.exp(rng.normal(scale=0.1, size=n))),
                         "es": rng.normal(scale=np.sqrt(true) / 100 / np.sqrt(252)), "gex": gex}, index=idx)


def test_hac_mean_test_matches_the_regression_estimator():
    x = np.random.default_rng(1).normal(0.1, 1.0, 500)
    t = stats.hac_mean_test(x, 7)
    assert t["t"] == pytest.approx(stats.newey_west_ols(x, np.empty((500, 0)), lags=7)["t"][0], rel=1e-12)
    assert t["p"] == pytest.approx(norm.sf(t["t"]))
    assert stats.qlike([2.0], [2.0])[0] == 0.0 and stats.qlike([2.0], [1.0])[0] > 0


def test_forecast_has_no_look_ahead():
    """Everything after the close of t, the target of t included, can change: the forecast for t+1 cannot."""
    d = _vol_world()
    rows = backtest.forecasts(d).dropna(subset=["base"]).index
    first = rows[0]
    for t in (first, rows[config.FORECAST_REFIT_EVERY], rows[config.FORECAST_REFIT_EVERY + 7]):
        later = d.index > t
        d2 = d.copy()
        d2.loc[later, "rv_parkinson"] *= 3.0
        d2.loc[later, "vix"] += 10.0
        d2.loc[later, "es"] *= -1.0
        d2.loc[later, "gex"] *= 5.0
        a, b = backtest.forecasts(d).loc[:t], backtest.forecasts(d2).loc[:t]
        assert (a[["base", "gex"]].dropna() == b[["base", "gex"]].dropna()).all().all()
    assert rows[0] == backtest.forecast_frame(d).dropna(subset=list(backtest.FORECAST_GEX)).index[config.FORECAST_MIN_TRAIN]


def test_forecast_finds_a_simulated_gex_effect(tmp_path):
    res = backtest.forecast_test(_vol_world(), data_dir=tmp_path)
    assert res["passed"] and res["table"]["dm_t"].iloc[0] > 3 and res["gex_coef"] < 0
    assert list(res["table"]["sample"]) == ["walk-forward"]         # this world ends before OOS_START
    assert res["n_trials"] == 1 and backtest.forecast_test(_vol_world(), data_dir=tmp_path)["n_trials"] == 1


def test_forecast_finds_nothing_without_an_effect(tmp_path):
    res = backtest.forecast_test(_vol_world(effect=0.0, seed=4), data_dir=tmp_path)
    assert not res["passed"]
