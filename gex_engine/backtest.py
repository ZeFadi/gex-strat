"""Strategy backtest: its market data, the pre-registered rules and the report.

Market data
-----------
Daily market data of the backtest: futures-like returns, roll-adjusted VIX futures, GEX series.

Inputs are the frozen files written by `cli.py backtest-data`:

* data/market/prices.csv: Bloomberg closes (SPX OHLC, SPXT, US 3M yield, VIX, UX1, UX2);
* data/external/squeezemetrics_dix.csv: SqueezeMetrics' public SPX GEX; or our own
  data/history.csv once it is long enough (`gex_series(source="own")`), or the history rebuilt
  from end-of-day vendor files (`source="eod"`, data/history_eod.csv): with the paid archive
  that is our GEX since 2002, and `dataset(source="eod")` runs every trial on it.

Return conventions, all EXCESS returns of a fully collateralized position, one per session,
from the previous close to this close:

* ES (S&P 500 future): SPXT (total return) minus the 3-month rate over the calendar days
  elapsed, act/360. A future earns the index's total return above cash, so this is its return
  without needing future prices (roll and basis ignored).
* UX1 (front VIX future): the generic UX1 jumps on roll days, when it switches from the
  expiring contract to the next one (+30 to +40% on 2016-04-21, 2016-07-21, 2024-12-19). On
  a roll day the return is UX1_t / UX2_{t-1} - 1: yesterday's second contract is today's
  first. Bloomberg's generic rolled ON the expiry session up to November 2014 and on the
  session AFTER it from December 2014 (identified on the data: on a roll day the whole curve
  UX1..UX3 matches yesterday's UX2..UX4; see UX_ROLL_NEXT_SESSION_FROM).

The calendar is the intersection of all series: no forward fill, a session missing anywhere
is dropped everywhere. The SPX OPEN is not used: before 2016 it equals the previous close on
up to 30% of days (the index's first print is computed from stale prices).

Rules and report
----------------
Does trading the gamma regime pay, net of costs, against the same rules without it?

PRE-REGISTERED (config.py "backtest" block, METHODOLOGY.md section 13, committed before the
first run). Three rules, each driven by a regime r_t in {+1 positive / -1 negative gamma}
known at the close of t:

* reversal  (ES):  position = -r_t * sign(ES return of the last session before entry).
                   Positive gamma: fade the latest move; negative gamma: follow it. The
                   usual dealer-hedging thesis. (Correction of 2026-10-15, logged as
                   new trials: the first run used the return of t, so at lag 2 it faded a
                   move two sessions old.)
* exposure  (ES):  position = r_t. Long in positive gamma, short in negative gamma.
* vol       (UX1): position = -r_t. Short the front VIX future in positive gamma, long it in
                   negative gamma.

Two regime definitions are the TRIALS: the sign of GEX, and "low gamma" (GEX below its
expanding 25th percentile, threshold known at t-1). Two benchmarks per rule are NOT trials:
the rule with r_t = +1 always (does the regime add anything?) and with a VIX filter
(r_t = -1 when VIX is above its expanding median: does GEX add anything beyond the level
of vol it is correlated with?).

Execution: the position decided at the close of t earns the return of session t + lag
(lag 2 primary, lag 1 optimistic; config.EXECUTION_LAGS). Positions are scaled to
config.VOL_TARGET with the asset's trailing vol known at t; costs are charged on every change
of the held position. All returns are daily excess returns, summed (not compounded).

Vol forecast trial
------------------
PRE-REGISTERED (config.py "vol forecast trial" block, METHODOLOGY.md section 14). The rules
above fail; the one robust result is that low gamma predicts higher next-session vol. Does
it improve a forecast, out of sample, beyond what HAR and VIX already know?

* target: the Parkinson variance of session t+1 (annualized, %^2);
* base model, HAR-X: today's variance and its 5- and 22-session means, VIX^2, today's ES
  return and a down-day dummy; GEX model: the same plus the GEX percentile rank among the last
  252 sessions;
* OLS refitted every 21 sessions on an expanding window, first fit after 1000 rows; a forecast
  made at the close of t uses only rows whose target is known at t;
* decision: Diebold-Mariano on the QLIKE loss over the whole walk-forward period, one-sided
  at 5%. Secondary, reported only: MSE, Clark-West, the sessions from config.OOS_START,
  Mincer-Zarnowitz regressions.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

from . import config, stats
from .stats import newey_west_ols
from .study import EOD_HISTORY_FILE, HISTORY_FILE


# --------------------------------------------------------------------------- market data

PRICES_FILE = Path("market") / "prices.csv"
# First VIX expiry after which Bloomberg's generic UX1 rolls the session AFTER the expiry
# (before it: on the expiry session itself). Measured on 2012-2026 data, see module docstring.
UX_ROLL_NEXT_SESSION_FROM = date(2014, 12, 1)
SQUEEZE_FILE = Path("external") / "squeezemetrics_dix.csv"


def load_prices(data_dir: Path = config.DATA_DIR) -> pd.DataFrame:
    """Bloomberg closes on the sessions where every series is present, from the first SPXT."""
    p = pd.read_csv(Path(data_dir) / PRICES_FILE, index_col="date", parse_dates=True)
    return p.dropna()


def es_returns(prices: pd.DataFrame) -> pd.Series:
    """Daily excess return of a fully collateralized S&P 500 future (SPXT minus 3M cash)."""
    days = prices.index.to_series().diff().dt.days
    cash = prices["us3m_pct"].shift(1) / 100.0 * days / 360.0
    return (prices["spxt"].pct_change() - cash).rename("es")


def vix_expiry(year: int, month: int) -> date:
    """Expiry of the VIX future of `month`: the Wednesday 30 days before the third Friday of
    the next month (exchange holidays not modelled; the data check in `vix_roll_days` covers it)."""
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    first = date(ny, nm, 1)
    third_friday = first + timedelta(days=(4 - first.weekday()) % 7 + 14)
    return third_friday - timedelta(days=30)


def vix_roll_days(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Sessions on which the generic UX1 changes contract: the expiry session up to November
    2014, the session after the expiry from December 2014 (UX_ROLL_NEXT_SESSION_FROM)."""
    expiries = [vix_expiry(y, m) for y in range(index.min().year - 1, index.max().year + 1) for m in range(1, 13)]
    sessions = index.sort_values()
    out = []
    for e in expiries:
        side = "right" if e >= UX_ROLL_NEXT_SESSION_FROM else "left"
        pos = sessions.searchsorted(pd.Timestamp(e), side=side)
        if 0 < pos < len(sessions):
            out.append(sessions[pos])
    return pd.DatetimeIndex(sorted(set(out)))


def ux1_returns(prices: pd.DataFrame) -> pd.Series:
    """Daily return of a long front VIX future, chained across rolls."""
    ux1, ux2 = prices["ux1"], prices["ux2"]
    r = ux1 / ux1.shift(1) - 1.0
    roll = r.index.isin(vix_roll_days(prices.index))
    r[roll] = (ux1 / ux2.shift(1) - 1.0)[roll]
    return r.rename("ux1")


def parkinson_vol(prices: pd.DataFrame) -> pd.Series:
    """Annualized Parkinson realized vol of each session, in % (high and low only, no open)."""
    hl = np.log(prices["spx_high"] / prices["spx_low"])
    return (100.0 * np.sqrt(hl**2 / (4.0 * np.log(2.0)) * 252.0)).rename("rv_parkinson")


def gex_series(source: str = "squeezemetrics", data_dir: Path = config.DATA_DIR, symbol: str = "SPX") -> pd.Series:
    """Daily net GEX dated by its session: SqueezeMetrics' (default), or ours from the CBOE
    archive ("own", data/history.csv) or from end-of-day vendor files ("eod", history_eod.csv).

    Theirs and ours are not on the same scale (different methodology and units); strategies
    only use the sign or the rank of a series in its own history.
    """
    if source == "squeezemetrics":
        d = pd.read_csv(Path(data_dir) / SQUEEZE_FILE, index_col="date", parse_dates=True)
        return d["gex"].rename("gex")
    files = {"own": HISTORY_FILE, "eod": EOD_HISTORY_FILE}
    if source in files:
        h = pd.read_csv(Path(data_dir) / files[source], parse_dates=["date"])
        return h.loc[h["symbol"] == symbol].set_index("date")["net_gex"].rename("gex")
    raise ValueError(f"unknown GEX source {source!r}")


def compare_gex(ours: pd.Series, theirs: pd.Series) -> dict:
    """How two daily GEX series agree on their common sessions: sign, levels, ranks, changes."""
    j = pd.concat([ours.rename("ours"), theirs.rename("theirs")], axis=1, join="inner").dropna()
    return {"sessions": len(j), "first": j.index.min(), "last": j.index.max(),
            "same_sign": float((np.sign(j["ours"]) == np.sign(j["theirs"])).mean()),
            "negative_ours": float((j["ours"] < 0).mean()), "negative_theirs": float((j["theirs"] < 0).mean()),
            "corr_levels": float(j["ours"].corr(j["theirs"])),
            "corr_ranks": float(j["ours"].corr(j["theirs"], method="spearman")),
            "corr_daily_changes": float(j["ours"].diff().corr(j["theirs"].diff()))}


def dataset(source: str = "squeezemetrics", data_dir: Path = config.DATA_DIR) -> pd.DataFrame:
    """Everything the backtest reads, on one calendar: returns, vol, GEX, VIX.

    The GEX source travels with the table (`attrs["gex_source"]`): it is part of every trial key.
    """
    p = load_prices(data_dir)
    d = pd.concat([es_returns(p), ux1_returns(p), parkinson_vol(p), p[["vix", "ux1", "spx_close"]]
                   .rename(columns={"ux1": "ux1_price"}), gex_series(source, data_dir)], axis=1, sort=True).dropna()
    d.attrs["gex_source"] = source
    return d


def _source_tag(d: pd.DataFrame) -> str:
    """Suffix of the trial keys for a non-default GEX source (the default keeps the keys logged so far)."""
    source = d.attrs.get("gex_source", "squeezemetrics")
    return "" if source == "squeezemetrics" else f" | gex={source}"


# --------------------------------------------------------------------------- rules and report

RULES = {"reversal": "es", "exposure": "es", "vol": "ux1"}
# Version of each rule's definition, part of the trial key: a corrected rule is a new trial.
RULE_VERSION = {"reversal": "v2-fade-entry-day", "exposure": "v1", "vol": "v1"}
TRIAL_REGIMES = ("gex_sign", "gex_low")
BENCHMARK_REGIMES = ("always", "vix_filter")
TRIALS_FILE = Path("backtest") / "trials.csv"


# --------------------------------------------------------------------------- regimes

def regimes(d: pd.DataFrame) -> pd.DataFrame:
    """+1 / -1 regime series known at each close; NaN until a threshold has enough history."""
    past = lambda s, q: s.expanding(config.REGIME_MIN_HISTORY).quantile(q).shift(1)
    low = past(d["gex"], config.LOW_GAMMA_QUANTILE)
    vix_mid = past(d["vix"], 0.5)
    return pd.DataFrame({
        "gex_sign": np.where(d["gex"] > 0, 1.0, -1.0),
        "gex_low": np.where(low.isna(), np.nan, np.where(d["gex"] < low, -1.0, 1.0)),
        "always": 1.0,
        "vix_filter": np.where(vix_mid.isna(), np.nan, np.where(d["vix"] > vix_mid, -1.0, 1.0)),
    }, index=d.index)


def episodes(regime: pd.Series) -> dict:
    """Number and longest length (sessions) of the negative-regime runs."""
    neg = (regime == -1).astype(int)
    starts = (neg.diff().fillna(neg.iloc[0]) == 1).sum()
    runs = neg.groupby((neg != neg.shift()).cumsum()).sum()
    return {"negative_days": int(neg.sum()), "episodes": int(starts),
            "longest": int(runs.max()) if len(runs) else 0}


# --------------------------------------------------------------------------- strategies

def raw_position(rule: str, regime: pd.Series, d: pd.DataFrame) -> pd.Series:
    """Unscaled position (-1, 0, +1) held over each session, from the regime already lagged
    to that session. Reversal fades or follows the last session before entry."""
    if rule == "reversal":
        return -regime * np.sign(d["es"].shift(1))
    if rule == "exposure":
        return regime.astype(float)
    if rule == "vol":
        return -regime.astype(float)
    raise ValueError(f"unknown rule {rule!r}")


def vol_weight(returns: pd.Series) -> pd.Series:
    """Notional per unit of position: VOL_TARGET / trailing annualized vol known at t, capped."""
    vol = returns.rolling(config.VOL_WINDOW).std() * np.sqrt(stats.PERIODS)
    return (config.VOL_TARGET / vol).clip(upper=config.MAX_WEIGHT)


def run(rule: str, regime: pd.Series, d: pd.DataFrame, lag: int, cost_mult: float = 1.0) -> pd.DataFrame:
    """Daily gross, cost and net return of one rule under one regime series.

    The position decided at the close of t is held over session t + lag; trades happen at
    the close of t + lag - 1, and cost |change in held position| x unit cost.
    """
    asset = RULES[rule]
    ret = d[asset]
    held = raw_position(rule, regime.shift(lag), d) * vol_weight(ret).shift(lag)
    if asset == "es":
        unit_cost = pd.Series(config.COST_ES, index=d.index)
    else:
        unit_cost = config.COST_VIX_POINTS / d["ux1_price"].shift(1)
    traded = held.fillna(0.0).diff().abs().fillna(0.0)
    cost = traded * unit_cost * cost_mult
    gross = held * ret
    out = pd.DataFrame({"position": held, "gross": gross, "cost": cost, "net": gross - cost})
    return out.loc[held.notna()]


def name(rule: str, regime: str) -> str:
    """Display name of a rule / regime pair."""
    return f"{rule} | {regime}"


def run_all(d: pd.DataFrame, lag: int, cost_mult: float = 1.0) -> dict[str, pd.DataFrame]:
    """Every pre-registered trial and benchmark at one execution lag."""
    reg = regimes(d)
    return {name(rule, r): run(rule, reg[r], d, lag, cost_mult)
            for rule in RULES for r in TRIAL_REGIMES + BENCHMARK_REGIMES}


# --------------------------------------------------------------------------- inference

def placebo_pvalue(rule: str, regime: pd.Series, d: pd.DataFrame, lag: int, draws: int = config.PLACEBO_DRAWS,
                   seed: int = config.SEED) -> dict:
    """Two-sided placebo: where the actual Sharpe ranks among circularly shifted regimes.

    Shifting keeps the regime's persistence and frequency but breaks its timing. Shifts are
    at least the longest regime episode (and 63 sessions), so a shift never lands in the
    same episode. `p_better` small: the timing adds value. `p_worse` small: the timing is
    informative in the OPPOSITE direction (the rule is on the wrong side of it).
    """
    actual = stats.sharpe(run(rule, regime, d, lag)["net"])
    reg = regime.dropna()
    n = len(reg)
    min_shift = max(episodes(reg)["longest"], 63)
    rng = np.random.default_rng(seed)
    shifts = rng.integers(min_shift, n - min_shift, size=draws)
    sims = np.array([stats.sharpe(run(rule, pd.Series(np.roll(reg.to_numpy(), k), index=reg.index)
                                            .reindex(d.index), d, lag)["net"]) for k in shifts])
    return {"actual": actual, "p_better": float((sims >= actual).mean()), "p_worse": float((sims <= actual).mean()),
            "placebo_median": float(np.median(sims))}


MECHANISM_LAGS = 20   # Newey-West lags: realized vol is persistent, the default bandwidth is too short


def mechanism(d: pd.DataFrame, regime: pd.Series, controls: str = "har") -> pd.DataFrame:
    """OLS of next-session Parkinson vol on the negative-gamma dummy and vol controls (HAC, 20 lags).

    controls="base": VIX and today's vol. controls="har" (the headline): adds the 5- and
    22-session mean vol (HAR-RV: realized vol has long memory) and today's ES return and a
    down-day dummy (leverage effect: negative gamma sessions fall in sell-offs).
    """
    rv = d["rv_parkinson"]
    x = pd.DataFrame({"negative_gamma": (regime == -1).astype(float), "vix": d["vix"], "rv_today": rv})
    if controls == "har":
        x = x.assign(rv_5=rv.rolling(5).mean(), rv_22=rv.rolling(22).mean(), es_return=100.0 * d["es"],
                     down_day=(d["es"] < 0).astype(float))
    data = pd.concat([rv.shift(-1).rename("y"), x], axis=1).dropna()
    res = newey_west_ols(data["y"], data[x.columns], lags=MECHANISM_LAGS)
    return pd.DataFrame({"term": ["const", *x.columns], "coef": res["beta"], "t_nw": res["t"]}).assign(n=res["n"])


# --------------------------------------------------------------------------- report

def report(d: pd.DataFrame, cost_mult: float = 1.0, data_dir: Path = config.DATA_DIR,
           with_placebo: bool = True) -> dict:
    """All tables: performance per strategy, sample and lag; inference; mechanism; episodes."""
    reg = regimes(d)
    oos = pd.Timestamp(config.OOS_START)
    rows, trial_sr = [], []
    for lag in config.EXECUTION_LAGS:
        for key, res in run_all(d, lag, cost_mult).items():
            rule, r = key.split(" | ")
            for sample, part in (("full", res), ("in-sample", res.loc[res.index < oos]),
                                 ("out-of-sample", res.loc[res.index >= oos])):
                s = stats.summary(part["net"])
                s.update(strategy=key, rule=rule, regime=r, trial=r in TRIAL_REGIMES, lag=lag, sample=sample,
                         turnover=float(part["position"].diff().abs().mean() * stats.PERIODS),
                         cost_drag=float(part["cost"].mean() * stats.PERIODS))
                if sample == "full":
                    s["ci05"], s["ci95"] = stats.bootstrap_sharpe(
                        part["net"], config.BOOTSTRAP_BLOCK, config.BOOTSTRAP_DRAWS, config.SEED)
                    if r in TRIAL_REGIMES:
                        trial_sr.append(s["sharpe"])
                rows.append(s)
    table = pd.DataFrame(rows)
    n_trials = register_trials(table, data_dir, _source_tag(d))
    full = table["sample"] == "full"
    table.loc[full, "deflated_sharpe"] = [
        stats.deflated_sharpe(r.sharpe, n_trials, r.days, r.skew, r.kurtosis, trial_sr) if r.trial else np.nan
        for r in table.loc[full].itertuples()]

    placebo = []
    if with_placebo:
        primary = config.EXECUTION_LAGS[0]
        for rule in RULES:
            for r in TRIAL_REGIMES:
                placebo.append({"strategy": name(rule, r), **placebo_pvalue(rule, reg[r], d, primary)})

    eps = []
    for r in ("gex_sign", "gex_low", "vix_filter"):
        for sample, part in (("in-sample", reg.loc[reg.index < oos, r]), ("out-of-sample", reg.loc[reg.index >= oos, r])):
            eps.append({"regime": r, "sample": sample, **episodes(part.dropna())})

    return {"table": table, "n_trials": n_trials, "placebo": pd.DataFrame(placebo), "episodes": pd.DataFrame(eps),
            "mechanism": {(r, c): mechanism(d, reg[r], c) for r in ("gex_sign", "gex_low", "vix_filter")
                          for c in ("har", "base")},
            "start": d.index.min(), "end": d.index.max()}


def register_trials(table: pd.DataFrame, data_dir: Path = config.DATA_DIR, tag: str = "") -> int:
    """Append-only log of every trial ever evaluated; its row count is N of the deflated Sharpe.

    A trial is (rule, regime, lag, cost multiplier, configuration, GEX source: `tag`). Benchmarks
    are not trials.
    Re-running the same configuration does not add rows.
    """
    cfg = (f"vt{config.VOL_TARGET}-w{config.VOL_WINDOW}-q{config.LOW_GAMMA_QUANTILE}"
           f"-h{config.REGIME_MIN_HISTORY}-oos{config.OOS_START}")
    trials = table.loc[table["trial"] & (table["sample"] == "full")]
    keys = {f"{t.strategy} | lag{t.lag} | {RULE_VERSION.get(t.strategy.split(' | ')[0], 'v1')} | {cfg}"
            .replace(" | v1 | ", " | ") + tag for t in trials.itertuples()}
    return _append_log(Path(data_dir) / TRIALS_FILE, keys)


def _append_log(path: Path, keys) -> int:
    """Add the keys not yet in the append-only log at `path`, stamped now; return its row count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    log = pd.read_csv(path) if path.exists() else pd.DataFrame(columns=["key", "first_run_utc"])
    new = sorted(set(keys) - set(log["key"]))
    if new:
        now = pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds")
        log = pd.concat([log, pd.DataFrame({"key": new, "first_run_utc": now})], ignore_index=True)
        log.to_csv(path, index=False, lineterminator="\n")
    return len(log)


# --------------------------------------------------------------------------- vol forecast trial

FORECAST_TRIALS_FILE = Path("backtest") / "forecast_trials.csv"
FORECAST_BASE = ("rv_d", "rv_w", "rv_m", "vix2", "es_return", "down_day")
FORECAST_GEX = (*FORECAST_BASE, "gex_pct")


def forecast_frame(d: pd.DataFrame) -> pd.DataFrame:
    """Target and regressors of the forecast trial, one row per session t, known at its close.

    y is the Parkinson variance of the next session of the calendar (annualized, %^2).
    """
    rv2 = d["rv_parkinson"] ** 2
    return pd.DataFrame({
        "y": rv2.shift(-1),
        "rv_d": rv2, "rv_w": rv2.rolling(5).mean(), "rv_m": rv2.rolling(22).mean(),
        "vix2": d["vix"] ** 2, "es_return": 100.0 * d["es"], "down_day": (d["es"] < 0).astype(float),
        "gex_pct": d["gex"].rolling(config.FORECAST_RANK_WINDOW).rank(pct=True),
    }, index=d.index)


def walk_forward(f: pd.DataFrame, features) -> pd.Series:
    """Raw (unfloored) out-of-sample OLS forecasts of f["y"] from `features`, expanding window.

    The forecast at row i uses rows 0..i-1 only: row j's target is the session after j, known
    at the close of row j + 1 <= i. Refit at rows FORECAST_MIN_TRAIN, + FORECAST_REFIT_EVERY, ...
    """
    X = np.column_stack([np.ones(len(f)), f[list(features)].to_numpy(float)])
    y = f["y"].to_numpy(float)
    out = np.full(len(f), np.nan)
    for start in range(config.FORECAST_MIN_TRAIN, len(f), config.FORECAST_REFIT_EVERY):
        known = np.isfinite(y[:start])
        beta = np.linalg.lstsq(X[:start][known], y[:start][known], rcond=None)[0]
        stop = min(start + config.FORECAST_REFIT_EVERY, len(f))
        out[start:stop] = X[start:stop] @ beta
    return pd.Series(out, index=f.index)


def forecasts(d: pd.DataFrame) -> pd.DataFrame:
    """Realized y and the raw forecasts of both models, on the same rows (all regressors known)."""
    f = forecast_frame(d).dropna(subset=list(FORECAST_GEX))
    return pd.DataFrame({"y": f["y"], "base": walk_forward(f, FORECAST_BASE), "gex": walk_forward(f, FORECAST_GEX)})


def forecast_test(d: pd.DataFrame, data_dir: Path = config.DATA_DIR) -> dict:
    """The pre-registered forecast trial: losses, Diebold-Mariano, Clark-West, Mincer-Zarnowitz, verdict."""
    raw = forecasts(d).dropna()
    floor = config.FORECAST_FLOOR
    floored = {m: int((raw[m] < floor).sum()) for m in ("base", "gex")}
    zero_range = int((raw["y"] <= 0).sum())
    ev = raw.loc[raw["y"] > 0].assign(base=lambda x: x["base"].clip(lower=floor),
                                      gex=lambda x: x["gex"].clip(lower=floor))
    lags = config.FORECAST_HAC_LAGS
    rows = []
    for sample, part in (("walk-forward", ev), (f"from {config.OOS_START}", ev.loc[ev.index >= pd.Timestamp(config.OOS_START)])):
        if len(part) <= 2 * lags:                       # e.g. no session after OOS_START yet
            continue
        y, b, a = part["y"], part["base"], part["gex"]
        lb, la = stats.qlike(y, b), stats.qlike(y, a)
        dm = stats.hac_mean_test(lb - la, lags)
        mse = stats.hac_mean_test((y - b) ** 2 - (y - a) ** 2, lags)
        cw = stats.hac_mean_test((y - b) ** 2 - ((y - a) ** 2 - (b - a) ** 2), lags)
        rows.append({"sample": sample, "start": part.index.min(), "end": part.index.max(), "n": len(part),
                     "qlike_base": lb.mean(), "qlike_gex": la.mean(), "qlike_gain_pct": 100 * (1 - la.mean() / lb.mean()),
                     "dm_t": dm["t"], "dm_p": dm["p"], "mse_dm_t": mse["t"], "clark_west_t": cw["t"], "clark_west_p": cw["p"]})
    mz = []
    for model in ("base", "gex"):
        r = newey_west_ols(ev["y"], ev[[model]], lags=lags)
        mz.append({"model": model, "alpha": r["beta"][0], "beta": r["beta"][1], "t_beta_eq_1": (r["beta"][1] - 1) / r["se"][1],
                   "r2": r["r2"]})
    f = forecast_frame(d).dropna()
    full = newey_west_ols(f["y"], f[list(FORECAST_GEX)], lags=lags)
    table = pd.DataFrame(rows)
    cfg = (f"w{config.FORECAST_RANK_WINDOW}-min{config.FORECAST_MIN_TRAIN}-refit{config.FORECAST_REFIT_EVERY}"
           f"-floor{config.FORECAST_FLOOR}-hac{lags}")
    return {"table": table, "mincer_zarnowitz": pd.DataFrame(mz),
            "gex_coef": float(full["beta"][-1]), "gex_t": float(full["t"][-1]),
            "passed": bool(table["dm_t"].iloc[0] > norm.isf(config.FORECAST_ALPHA)),
            "floored": floored, "zero_range_sessions": zero_range,
            "n_trials": _append_log(Path(data_dir) / FORECAST_TRIALS_FILE,
                                    [f"har-x + gex_pct | parkinson variance t+1 | qlike dm | {cfg}{_source_tag(d)}"])}
