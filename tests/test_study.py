"""Archive and history rules; study statistics: HAC errors, a simulated effect recovered, no look-ahead, no glued days."""

import gzip
import json

import numpy as np
import pandas as pd
import pytest

from gex_engine import backtest, config, stats, study

from .conftest import fixture


def test_newey_west_matches_direct_formula():
    rng = np.random.default_rng(0)
    n = 200
    x = rng.normal(size=n)
    e = np.convolve(rng.normal(size=n + 1), [1, 0.6], "valid")       # MA(1) errors
    y = 1.0 + 0.5 * x + e
    res = stats.newey_west_ols(y, x[:, None], lags=3)
    X = np.column_stack([np.ones(n), x])
    u = y - X @ res["beta"]
    S = sum(np.outer(X[t], X[t]) * u[t] ** 2 for t in range(n))
    for lag in range(1, 4):
        w = 1 - lag / 4
        S += w * sum(u[t] * u[t - lag] * (np.outer(X[t], X[t - lag]) + np.outer(X[t - lag], X[t]))
                     for t in range(lag, n))
    B = np.linalg.inv(X.T @ X)
    assert np.allclose(res["se"], np.sqrt(np.diag(B @ S @ B)))
    lag0 = stats.newey_west_ols(y, x[:, None], lags=0)                # = White HC0
    assert np.allclose(lag0["se"] ** 2, np.diag(B @ (X.T * u**2) @ X @ B))


def _history(n=250, effect=-3.0, seed=1):
    """Synthetic closes where next-day realized vol is lower by `effect` vol points in positive gamma."""
    rng = np.random.default_rng(seed)
    rows, close = [], 100.0
    gex = rng.normal(size=n)
    dates = pd.bdate_range("2025-01-02", periods=n)
    for i in range(n):
        vol = (16 + (effect if i and gex[i - 1] > 0 else 0)) / 100 / np.sqrt(252)
        prev = close
        ret = rng.normal(scale=vol)
        close = prev * np.exp(ret)
        hi, lo = max(prev, close) * (1 + abs(rng.normal(scale=vol / 2))), min(prev, close) * (1 - abs(rng.normal(scale=vol / 2)))
        rows.append({"symbol": "SYN", "date": dates[i].date().isoformat(), "spot": close,
                     "open": prev, "high": hi, "low": lo, "close": close, "price_change": close - prev,
                     "net_gex": gex[i] * 1e9, "zero_gamma": close * (1 + rng.normal(scale=0.01)),
                     "one_day_move": close * vol, "atm_vol_1d": 0.16 + rng.normal(scale=0.01), "front_share_abs_gex": 0.1, "call_wall": close * 1.02,
                     "put_wall": close * 0.98})
    return pd.DataFrame(rows)


def test_simulated_regime_effect_is_found():
    res = study.run(_history(), "SYN")
    m = res["means"].set_index("outcome")
    assert m.loc["next_rv_gk", "difference"] < -1.5 and m.loc["next_rv_gk", "welch_t"] < -2
    assert res["regression"] is not None and res["regression"]["n"].iloc[0] > 150


def test_indeterminate_days_are_a_third_group():
    h = _history()
    h["regime_indeterminate"] = [i % 10 == 0 for i in range(len(h))]
    d = study.build_dataset(h, "SYN")
    m = study.conditional_means(d).set_index("outcome")
    assert (d["regime"] == 0).sum() > 15
    assert m.loc["next_rv_gk", "n_positive"] + m.loc["next_rv_gk", "n_negative"] + \
        m.loc["next_rv_gk", "n_indeterminate"] == d["next_rv_gk"].notna().sum()


def test_implied_vol_is_on_the_realized_basis():
    """Friday to Monday: three calendar days of implied variance, annualized on 252 sessions."""
    d = study.build_dataset(_history(n=10), "SYN").set_index("date")
    h = _history(n=10).set_index("date")
    friday = "2025-01-03"
    assert d.loc[friday, "iv_next"] == pytest.approx(100 * h.loc[friday, "atm_vol_1d"] * np.sqrt(3 * 252 / 365))


def test_no_effect_no_signal():
    m = study.run(_history(effect=0.0, seed=7), "SYN")["means"].set_index("outcome")
    assert abs(m.loc["next_rv_gk", "welch_t"]) < 3


def test_missing_day_is_not_glued():
    h = _history(n=40)
    h = h.drop(index=20).reset_index(drop=True)          # day 20 missing from the archive
    d = study.build_dataset(h, "SYN")
    assert len(d) == 40 - 1 - 2                         # last day and the broken pair drop


def test_holiday_is_bridged_by_the_price_change():
    """Two sessions around a holiday are not adjacent business days but are consecutive sessions."""
    h = _history(n=10)
    h.loc[5:, "date"] = (pd.to_datetime(h.loc[5:, "date"]) + pd.offsets.BDay(1)).dt.date.astype(str)
    assert len(study.build_dataset(h, "SYN")) == 9


def test_real_vendor_format(spx_state):
    """CBOE closing files carry prev_day_close == close; the study must not depend on it."""
    row = study.summary_row(spx_state)
    assert row["price_change"] == pytest.approx(-4.2998)
    assert "prev_close" not in row
    nxt = dict(row, date="2026-10-13", close=row["close"] * 1.01, price_change=row["close"] * 0.01,
               high=row["close"] * 1.012, low=row["close"] * 0.999, open=row["close"])
    d = study.build_dataset(pd.DataFrame([row, nxt]), "SPX")
    assert len(d) == 1 and d.loc[0, "next_ret_pct"] == pytest.approx(1.0)


def test_z_score_has_no_look_ahead():
    h = _history(n=60)
    d1 = study.build_dataset(h, "SYN")
    h2 = h.copy()
    h2.loc[59, "net_gex"] = 1e15                        # a huge last value
    d2 = study.build_dataset(h2, "SYN")
    common = d1["date"].isin(d2["date"]) & (d1["date"] < h.loc[59, "date"])
    assert np.allclose(d1.loc[common, "gex_z"].dropna(), d2.loc[common, "gex_z"].dropna())


def test_inconsistent_ohlc_is_not_used():
    h = _history(n=40)
    h.loc[10, "open"] = h.loc[10, "high"] * 1.01        # open above high, as NDX on 2026-10-12
    d = study.build_dataset(h, "SYN")
    assert np.isnan(d.loc[d["date"] == h.loc[9, "date"], "next_rv_gk"]).all()


def test_constant_feature_is_dropped_not_fatal():
    h = _history()
    h["zero_gamma"] = h["spot"]                         # distance to zero gamma always 0
    reg = study.run(h, "SYN")["regression"]
    assert "zero_gamma_sigmas" not in reg["term"].tolist() and reg["dropped"].iloc[0] == "zero_gamma_sigmas"


def _payload(symbol, **_):
    with gzip.open(fixture(f"{symbol}_20261012.json.gz"), "rt", encoding="utf-8") as fh:
        return json.load(fh)


def test_archive_close_twice_is_idempotent(tmp_path):
    status, msg = study.archive("SPX", data_dir=tmp_path, download=_payload)
    assert status == "archived" and "close of 2026-10-12" in msg
    assert study.archive("SPX", data_dir=tmp_path, download=_payload)[0] == "exists"
    assert study.archive("SPX", force=True, data_dir=tmp_path, download=_payload)[0] == "archived"
    h = study.load_history(tmp_path)
    assert len(h) == 1 and h.loc[0, "zero_gamma"] == pytest.approx(7752.39, abs=0.5)
    assert (tmp_path / "raw" / "SPX" / "2026-10-12.json.gz").exists()


def test_unquoted_file_never_replaces_a_close(tmp_path):
    """A pre-open file (no quotes yet) must not be written, even with force."""
    study.archive("SPY", data_dir=tmp_path, download=_payload)
    raw = tmp_path / "raw" / "SPY" / "2026-10-12.json.gz"
    before = raw.read_bytes()
    empty = _payload("SPY")
    for o in empty["data"]["options"]:
        o["bid"] = o["ask"] = 0.0
    status, _ = study.archive("SPY", force=True, data_dir=tmp_path, download=lambda s, **_: empty)
    assert status == "skipped" and raw.read_bytes() == before


def test_rebuild_keeps_rows_it_cannot_rebuild(tmp_path):
    study.archive("SPX", data_dir=tmp_path, download=_payload)
    row = study.load_history(tmp_path).iloc[0]
    assert row["file_generated_utc"] == "2026-10-13 11:54:18"
    raw = tmp_path / "raw" / "SPX" / "2026-10-12.json.gz"
    raw.write_bytes(gzip.compress(b'{"data": {"options": []}}'))       # unreadable archive
    hist, failed = study.rebuild_history(tmp_path)
    assert len(failed) == 1 and len(hist) == 1 and hist.iloc[0]["net_gex"] == row["net_gex"]
    raw.unlink()                                                       # raw file gone
    hist, failed = study.rebuild_history(tmp_path)
    assert failed == ["SPX 2026-10-12: no raw file"] and hist.iloc[0]["net_gex"] == row["net_gex"]


def test_etf_close_at_15_59_is_accepted(tmp_path):
    assert study.archive("SPY", data_dir=tmp_path, download=_payload)[0] == "archived"


def test_intraday_snapshot_is_never_a_close(tmp_path):
    p = _payload("SPX")
    p["data"]["last_trade_time"] = "2026-10-13T10:05:00"
    for o in p["data"]["options"]:
        o["last_trade_time"] = "2026-10-13T10:04:00"
    status, msg = study.archive("SPX", data_dir=tmp_path, download=lambda s, **_: p)
    assert status == "skipped" and "before the close" in msg and study.load_history(tmp_path).empty
    study.archive("SPX", intraday=True, data_dir=tmp_path, download=lambda s, **_: p)
    assert (tmp_path / "raw" / "SPX" / "2026-10-13_1004.json.gz").exists()


def test_rewriting_the_history_changes_no_byte(tmp_path):
    """Re-archiving a day rewrites the whole file; the rows that did not change must stay identical."""
    path = config.DATA_DIR / "history.csv"
    if not path.exists():
        pytest.skip("no archived history in this checkout")
    (tmp_path / "history.csv").write_bytes(path.read_bytes())
    row = study.load_history(tmp_path).iloc[-1].to_dict()
    study.append_history(row, tmp_path)
    assert (tmp_path / "history.csv").read_bytes() == path.read_bytes()


def test_preopen_file_is_kept_raw_beside_the_close(tmp_path):
    """An unquoted pre-open ETF file still carries the overnight open interest: kept raw, never in the history."""
    study.archive("SPY", data_dir=tmp_path, download=_payload)
    history = (tmp_path / "history.csv").read_bytes()
    empty = _payload("SPY")
    for o in empty["data"]["options"]:
        o["bid"] = o["ask"] = 0.0
    status, msg = study.archive("SPY", preopen=True, data_dir=tmp_path, download=lambda s, **_: empty)
    assert status == "archived" and (tmp_path / "raw" / "SPY" / "2026-10-12_preopen.json.gz").exists()
    assert study.archive("SPY", preopen=True, data_dir=tmp_path, download=lambda s, **_: empty)[0] == "exists"
    assert (tmp_path / "history.csv").read_bytes() == history
    hist, failed = study.rebuild_history(tmp_path)
    assert len(hist) == 1 and not failed                # rebuild reads the close only


def test_the_versioned_history_is_the_one_read():
    """The app, the study and the backtest all read data/history.csv (a path regression hid here once)."""
    path = config.DATA_DIR / "history.csv"
    if not path.exists():
        pytest.skip("no archived history in this checkout")
    assert study.HISTORY_FILE == "history.csv"
    hist = study.load_history()
    assert not hist.empty and {"symbol", "date", "net_gex", "close"} <= set(hist.columns)
    own = backtest.gex_series(source="own")
    assert len(own) == (hist["symbol"] == "SPX").sum()


def test_eod_import_chains_price_changes_and_never_bridges_a_failed_day(tmp_path, spx_chain):
    from .test_surface import vendor_day
    folder = tmp_path / "day_by_date"
    folder.mkdir()
    close = spx_chain.meta["close"]
    vendor_day(spx_chain, "2026-10-12", close).to_csv(folder / "2026-10-12_options.csv", index=False)
    vendor_day(spx_chain, "2026-10-12", close).iloc[:0].to_csv(folder / "2026-10-13_options.csv", index=False)
    vendor_day(spx_chain, "2026-10-12", close + 10).to_csv(folder / "2026-10-14_options.csv", index=False)
    vendor_day(spx_chain, "2026-10-12", close + 25).to_csv(folder / "2026-10-15_options.csv", index=False)
    vendor_day(spx_chain, "2026-10-12", close).to_csv(folder / "2026-10-15_options_provisional.csv", index=False)
    hist, failed = study.import_eod(["SPX"], folder, tmp_path)
    assert len(failed) == 1 and "2026-10-13" in failed[0]
    assert len(hist) == 3                                               # provisional file ignored
    assert np.isnan(hist["price_change"].iloc[0]) and np.isnan(hist["price_change"].iloc[1])   # after the gap
    assert hist["price_change"].iloc[2] == pytest.approx(15.0)
    assert study.load_history(tmp_path, study.EOD_HISTORY_FILE).shape == hist.shape


def test_close_only_history_uses_close_to_close_and_leaves_walls_empty():
    h = _history().assign(open=np.nan, high=np.nan, low=np.nan)
    res = study.run(h, "SYN")
    assert res["measure"] == "close-to-close" and res["regression"] is not None
    assert "rv_today_cc" in res["regression"]["term"].tolist()
    assert (res["walls"]["days"] == 0).all()
