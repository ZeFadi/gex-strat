"""Our own history: the daily archive, and whether the regime predicts the next day.

Archive and history
-------------------
Daily archive of raw chains and the one-row-per-day history the study reads.

Free option data has no history, so it is built here, one close at a time:

* the raw CBOE payload of each symbol, gzipped, under data/raw/{SYMBOL}/{YYYY-MM-DD}.json.gz
  (about 1-2 MB a day per symbol); any past day can be replayed with `cboe.load`;
* one summary row per (symbol, date) in data/history.csv: the levels of the "all expiries"
  view, the zero-gamma band, the front-expiry metrics, the day's OHLC and the vendor's
  30-day IV. CSV, so it opens in Excel.

A day is archived only once its close is in the file (as-of at or after 15:59 New York: the
ETFs' last print is stamped 15:59:59, the indices' 16:00 to 16:15);
a run during the session saves nothing unless asked for an intraday snapshot. An archived
close is never overwritten unless forced: re-running the same day reports it as archived.

Regime study
------------
Does the GEX regime say anything about the next day? Measured on the archived history.

The gamma regime is commonly used as a trading filter (sell vol in positive gamma, follow
trends in negative gamma), and published work reports lower realized vol when net GEX is
high. Before relying on it, this module measures it on the archived history.

Dataset, one row per (symbol, trading day t), from data/history.csv:

* features known at the close of t: regime (+1 positive, -1 negative, 0 indeterminate: the
  zero-gamma band contains spot), net GEX z-score (expanding, so no look-ahead), distance
  to zero gamma in one-day sigmas, front-expiry share of |GEX|, and two controls: the
  implied vol of the next session and today's realized vol;
* outcomes of day t+1: realized vol close-to-close and Garman-Klass (annualized with 252
  trading days), high-low range in % of the previous close, return, and whether the walls
  of t were touched and held.

Day t+1 is paired with day t only if the two are consecutive sessions: adjacent business
days, or (across a holiday) the previous close implied by t+1's `price_change` equals the
archived close of t to 0.01%. A missing archive day therefore never glues two
non-consecutive sessions. Returns and ranges of t+1 are measured from the ARCHIVED close of
t: the vendor's `prev_day_close` is not usable (in closing files it equals the day's own
close). An OHLC row is used for open-based measures only if low <= open, close <= high (the
vendor's open is sometimes inconsistent: NDX on 2026-10-12 has open > high).

Vol units: realized vols are annualized with 252 sessions. The implied vol is put on the
same basis: the ATM vol at one calendar day (ACT/365) gives the expected variance until the
next close, sigma^2 x calendar days / 365, annualized x 252 per session (`iv_next`). It is
comparable to the close-to-close outcome, which includes the weekend; Garman-Klass is
intraday only.

Statistics: conditional means by regime with Welch t-statistics, and OLS of each outcome
on the features with Newey-West (HAC, Bartlett kernel) standard errors. Implemented with
numpy, so the app needs no statsmodels; the tests check the HAC formula against a direct
computation and the estimator against a simulated effect.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, data, exposures, snapshot, surface
from .stats import PERIODS, newey_west_ols


# --------------------------------------------------------------------------- archive and history

HISTORY_FILE = "history.csv"


def summary_row(state: surface.MarketState) -> dict:
    """The day's levels in one flat row."""
    p = snapshot.build(state, "all")
    s, f, q = p["summary"], p["front"], p["quality"]
    band = exposures.zero_gamma_band(state.contracts, state.spot, state.rate)
    meta = state.meta
    return {
        "symbol": state.symbol, "date": state.asof.date().isoformat(), "asof": state.asof.isoformat(),
        "file_generated_utc": meta.get("generated_utc"),
        "spot": state.spot, "open": meta.get("open"), "high": meta.get("high"), "low": meta.get("low"),
        "close": meta.get("close"), "price_change": meta.get("price_change"),
        "iv30_vendor": meta.get("iv30_vendor"),
        "atm_vol_1d": surface.atm_vol(state, config.ONE_DAY), "one_day_move": s["one_day_move"],
        "net_gex": s["net_gex"], "call_gex": s["call_gex"], "put_gex": s["put_gex"],
        "net_vex": s["net_vex"], "net_chex": s["net_chex"],
        "zero_gamma": s["zero_gamma"], "zero_gamma_low": band["low"], "zero_gamma_high": band["high"],
        "regime_indeterminate": band["indeterminate"],
        "call_wall": s["call_wall"], "put_wall": s["put_wall"], "abs_gamma_strike": s["abs_gamma_strike"],
        "trapdoor": s["trapdoor"],
        "front_expiry": f["expiry"], "front_net_gex": f["net_gex"],
        "front_share_abs_gex": f["share_of_book_abs_gex"], "front_share_volume": f["share_of_volume"],
        "contracts_priced": q["contracts_priced"],
    }


def rebuild_history(data_dir: Path = config.DATA_DIR) -> tuple[pd.DataFrame, list[str]]:
    """Recompute data/history.csv from the archived closing chains (after a change to the engine).

    Intraday snapshots and set-aside files (names with "_") are skipped. A file that fails the
    archive's checks or does not price keeps its existing history row, and is reported; so
    does a row whose raw file is gone (SPY, 2026-10-19). Returns the table and the list of
    rows that could not be rebuilt.
    """
    old = load_history(data_dir)
    rows, failed = [], []
    for path in sorted((Path(data_dir) / "raw").glob("*/*.json.gz")):
        if "_" in path.name:
            continue
        symbol = path.parent.name
        try:
            chain = data.load(path, symbol)
            if quoted_share(chain) < config.ARCHIVE_MIN_QUOTED_SHARE:
                raise data.ChainError("not quoted enough")
            rows.append(summary_row(surface.build(chain)))
        except Exception as exc:
            failed.append(f"{path}: {exc}")
            if not old.empty:
                keep = old.loc[(old["symbol"] == symbol) & (old["date"] == path.name.split(".")[0])]
                rows.extend(keep.to_dict(orient="records"))
    if not old.empty:
        done = {(r["symbol"], r["date"]) for r in rows}
        orphans = old.loc[[(s, d) not in done for s, d in zip(old["symbol"], old["date"])]]
        failed.extend(f"{s} {d}: no raw file" for s, d in zip(orphans["symbol"], orphans["date"]))
        rows.extend(orphans.to_dict(orient="records"))
    hist = pd.DataFrame(rows)
    if not hist.empty:
        hist = hist.sort_values(["symbol", "date"])
        hist.to_csv(Path(data_dir) / HISTORY_FILE, index=False, lineterminator="\n")
    return hist, failed


def is_close(state_or_chain) -> bool:
    """True when the data's as-of is at or after the archive cutoff (15:59 New York)."""
    asof = state_or_chain.asof.astimezone(config.MARKET_TZ)
    return (asof.hour, asof.minute) >= config.ARCHIVE_CUTOFF


def save_raw(payload: dict, symbol: str, name: str, data_dir: Path = config.DATA_DIR) -> Path:
    """Write a raw payload, gzipped, to data/raw/{symbol}/{name}.json.gz."""
    path = Path(data_dir) / "raw" / symbol / f"{name}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return path


def append_history(row: dict, data_dir: Path = config.DATA_DIR) -> pd.DataFrame:
    """Add or replace the (symbol, date) row of the history and return the whole table."""
    path = Path(data_dir) / HISTORY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    hist = load_history(data_dir)
    hist = hist.loc[~((hist["symbol"] == row["symbol"]) & (hist["date"] == row["date"]))] if not hist.empty else hist
    hist = pd.concat([pd.DataFrame([row]), hist], ignore_index=True).sort_values(["symbol", "date"])
    hist.to_csv(path, index=False, lineterminator="\n")
    return hist


def load_history(data_dir: Path = config.DATA_DIR) -> pd.DataFrame:
    """The history table, empty if nothing has been archived yet.

    Floats are read back exactly (round trip), so re-writing the table changes only the rows
    that changed: the file is versioned and its diffs are read.
    """
    path = Path(data_dir) / HISTORY_FILE
    return pd.read_csv(path, float_precision="round_trip") if path.exists() else pd.DataFrame()


def quoted_share(chain: data.Chain) -> float:
    """Share of contracts with a two-sided quote. A closing file is above 0.9; before the
    open the delayed file can be nearly empty (156 of 13,028 SPY contracts on 2026-10-20)."""
    c = chain.contracts
    return float(((c["bid"] > 0) & (c["ask"] > 0)).mean()) if len(c) else 0.0


def archive(symbol: str, intraday: bool = False, force: bool = False, preopen: bool = False,
            data_dir: Path = config.DATA_DIR, download=data.download) -> tuple[str, str]:
    """Download one symbol (with retries) and archive it.

    `preopen`: before the next open, CBOE republishes the previous close with the open interest
    the OCC updated overnight (positions at the END of the session; the evening file carries
    those of its start). That file is kept raw only, as {date}_preopen.json.gz next to the
    close, to measure later which open interest predicts better; it never enters the history.
    Its quotes may be empty (ETFs): only the as-of is checked.

    Returns (status, message), status one of "archived", "exists" (close already on disk) or
    "skipped" (not a close, or not quoted enough): the caller decides whether a skip is an error.

    Nothing is written until the file has passed every check: it is a close, it is quoted
    (`quoted_share` >= config.ARCHIVE_MIN_QUOTED_SHARE) and it prices. An archived close is
    never overwritten unless `force`: the morning catch-up run only fills missing days, and a
    pre-open file can no longer replace a good close.
    """
    payload = download(symbol, attempts=config.ARCHIVE_ATTEMPTS)
    chain = data.parse(payload, symbol)
    asof = chain.asof.astimezone(config.MARKET_TZ)
    if preopen:
        if not is_close(chain):
            return "skipped", f"{symbol}: as-of {asof:%Y-%m-%d %H:%M} ET is not a close, no pre-open file archived"
        name = f"{asof:%Y-%m-%d}_preopen"
        if not force and (Path(data_dir) / "raw" / symbol / f"{name}.json.gz").exists():
            return "exists", f"{symbol}: pre-open file of {asof:%Y-%m-%d} already archived"
        path = save_raw(payload, symbol, name, data_dir)
        return "archived", f"{symbol}: pre-open file of the {asof:%Y-%m-%d} close -> {path}"
    share = quoted_share(chain)
    if share < config.ARCHIVE_MIN_QUOTED_SHARE:
        return "skipped", f"{symbol}: only {share:.0%} of contracts quoted (as-of {asof:%Y-%m-%d %H:%M} ET), nothing archived"
    if intraday:
        path = save_raw(payload, symbol, f"{asof:%Y-%m-%d_%H%M}", data_dir)
        return "archived", f"{symbol}: intraday snapshot {asof:%Y-%m-%d %H:%M} ET -> {path}"
    if not is_close(chain):
        return "skipped", f"{symbol}: as-of {asof:%Y-%m-%d %H:%M} ET is before the close, nothing archived"
    name = f"{asof:%Y-%m-%d}"
    if not force and (Path(data_dir) / "raw" / symbol / f"{name}.json.gz").exists():
        return "exists", f"{symbol}: close of {name} already archived"
    row = summary_row(surface.build(chain))
    path = save_raw(payload, symbol, name, data_dir)
    append_history(row, data_dir)
    return "archived", f"{symbol}: close of {row['date']} archived -> {path}"


# --------------------------------------------------------------------------- regime study

MIN_DAYS = 30          # below this, no statistic is reported
Z_MIN_PERIODS = 20     # expanding z-score starts after this many days


def build_dataset(hist: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Features at t and outcomes at t+1 for one symbol (rows without a valid t+1 dropped)."""
    h = hist.loc[hist["symbol"] == symbol].sort_values("date").reset_index(drop=True)
    if h.empty:
        return pd.DataFrame()
    h["ohlc_ok"] = ((h["low"] <= h[["open", "close"]].min(axis=1))
                    & (h["high"] >= h[["open", "close"]].max(axis=1)) & (h["low"] > 0))
    h["rv_gk"] = np.sqrt(np.maximum(
        0.5 * np.log(h["high"] / h["low"]) ** 2 - (2 * np.log(2) - 1) * np.log(h["close"] / h["open"]) ** 2,
        0.0) * PERIODS).where(h["ohlc_ok"])

    nxt = h.shift(-1)
    dates = pd.to_datetime(h["date"]).to_numpy(dtype="datetime64[D]")
    next_dates = pd.to_datetime(nxt["date"]).to_numpy(dtype="datetime64[D]")
    have_next = nxt["date"].notna().to_numpy()
    adjacent = np.zeros(len(h), dtype=bool)
    adjacent[have_next] = np.busday_count(dates[have_next], next_dates[have_next]) == 1
    implied_prev = nxt["close"] - nxt["price_change"] if "price_change" in h else np.nan
    across_holiday = np.abs(implied_prev / h["close"] - 1) < 1e-4
    consecutive = adjacent | across_holiday.fillna(False).to_numpy()

    base = h["close"]                                    # archived close of t
    calendar_days = (pd.to_datetime(nxt["date"]) - pd.to_datetime(h["date"])).dt.days
    indeterminate = (h["regime_indeterminate"].astype(str).str.lower() == "true"
                     if "regime_indeterminate" in h else pd.Series(False, index=h.index))
    gex = h["net_gex"]
    mean = gex.expanding(Z_MIN_PERIODS).mean().shift(1)
    std = gex.expanding(Z_MIN_PERIODS).std().shift(1)
    d = pd.DataFrame({
        "date": h["date"],
        "regime": np.where(indeterminate, 0.0, np.where(h["net_gex"] > 0, 1.0, -1.0)),
        "gex_z": (gex - mean) / std,
        "zero_gamma_sigmas": (h["zero_gamma"] - h["spot"]) / h["one_day_move"],
        "front_share": h["front_share_abs_gex"],
        "iv_next": 100.0 * h["atm_vol_1d"] * np.sqrt(calendar_days / config.DAYS_PER_YEAR * PERIODS),
        "rv_today": 100.0 * h["rv_gk"],
        "next_rv_cc": 100.0 * np.abs(np.log(nxt["close"] / base)) * np.sqrt(PERIODS),
        "next_rv_gk": 100.0 * nxt["rv_gk"],
        "next_range_pct": 100.0 * (nxt["high"] - nxt["low"]) / base,
        "next_ret_pct": 100.0 * (nxt["close"] / base - 1),
        "call_wall_touched": nxt["high"] >= h["call_wall"],
        "call_wall_held": (nxt["high"] >= h["call_wall"]) & (nxt["close"] < h["call_wall"]),
        "put_wall_touched": nxt["low"] <= h["put_wall"],
        "put_wall_held": (nxt["low"] <= h["put_wall"]) & (nxt["close"] > h["put_wall"]),
    })
    return d.loc[consecutive].reset_index(drop=True)


def conditional_means(d: pd.DataFrame, outcomes=("next_rv_gk", "next_rv_cc", "next_range_pct", "next_ret_pct")) -> pd.DataFrame:
    """Mean of each outcome in positive vs negative gamma, difference and Welch t.

    Indeterminate days (the zero-gamma band contains spot) are a third group, shown apart
    and kept out of the comparison.
    """
    rows = []
    pos, neg, mid = (d.loc[d["regime"] == r] for r in (1, -1, 0))
    for col in outcomes:
        a, b = pos[col].dropna(), neg[col].dropna()
        diff = a.mean() - b.mean() if len(a) and len(b) else np.nan
        se = np.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b)) if len(a) > 1 and len(b) > 1 else np.nan
        rows.append({"outcome": col, "positive_gamma": a.mean(), "n_positive": len(a),
                     "negative_gamma": b.mean(), "n_negative": len(b),
                     "indeterminate": mid[col].mean(), "n_indeterminate": int(mid[col].notna().sum()),
                     "difference": diff,
                     "welch_t": diff / se if se and np.isfinite(se) and se > 0 else np.nan})
    return pd.DataFrame(rows)


def regression(d: pd.DataFrame, outcome: str = "next_rv_gk",
               features=("gex_z", "zero_gamma_sigmas", "iv_next", "rv_today")) -> pd.DataFrame | None:
    """OLS of `outcome` on `features` with Newey-West errors; None if too few complete rows.

    A feature constant over the sample (a vendor field that never moved) is dropped and named
    in `dropped` rather than making X'X singular.
    """
    data = d[[outcome, *features]].dropna()
    if len(data) < MIN_DAYS:
        return None
    used = [f for f in features if data[f].std() > 1e-12]
    dropped = [f for f in features if f not in used]
    res = newey_west_ols(data[outcome], data[used])
    return pd.DataFrame({"term": ["const", *used], "coef": res["beta"], "se_nw": res["se"],
                         "t": res["t"]}).assign(n=res["n"], lags=res["lags"], r2=res["r2"],
                                                dropped=", ".join(dropped))


def wall_behaviour(d: pd.DataFrame) -> pd.DataFrame:
    """How often the walls of t were touched on t+1, and how often they held when touched."""
    rows = []
    for wall in ("call_wall", "put_wall"):
        touched = d[f"{wall}_touched"].astype(bool)
        rows.append({"wall": wall.replace("_", " "), "days": len(d), "touched": int(touched.sum()),
                     "held_when_touched": float(d.loc[touched, f"{wall}_held"].mean()) if touched.any() else np.nan})
    return pd.DataFrame(rows)


def run(hist: pd.DataFrame, symbol: str) -> dict:
    """Dataset and, from MIN_DAYS pairs on, conditional means, regression and wall statistics."""
    d = build_dataset(hist, symbol) if not hist.empty else pd.DataFrame()
    n = len(d)
    out = {"symbol": symbol, "days": n, "enough": n >= MIN_DAYS, "dataset": d}
    if n >= MIN_DAYS:
        out["means"] = conditional_means(d)
        out["regression"] = regression(d)
        out["walls"] = wall_behaviour(d)
    return out
