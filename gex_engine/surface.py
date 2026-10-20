"""Market state: the clock, then from a raw chain to priced contracts.

Clock
-----
Time arithmetic, in one place: New York session times, settlement, year fractions.

Every duration in the engine is ACT/365 calendar time (config.DAYS_PER_YEAR) between two
timezone-aware datetimes; every clock time is New York (config.MARKET_TZ). Exchange holidays
are not modelled.

Market state
------------
From a raw chain to a priced market state: clock, forwards, reference spot, implied vols.

Order of operations, each step using only what the previous one established:

1. Clock. Time to settlement T of each contract is measured from the chain's `asof`, in
   ACT/365 years, to the settlement time of its ROOT (09:30 for AM-settled SPX, 16:00
   otherwise). Contracts with T below one minute are settled and dropped.
2. Forwards. Per series (root, expiry), F = K + (C_mid - P_mid) / DF at the strikes where
   |C - P| is smallest, median of `FORWARD_STRIKES` of them.
3. Reference spot. The front series' forward discounted at r over its (short) life. This is
   the spot the option prices are consistent with; the vendor's `current_price` can be an
   extended-hours print.
4. Carry. b = ln(F / S_ref) / T per series; series without enough two-sided pairs take b
   interpolated in T from the priced series.
5. Implied vol per strike from the OUT-of-the-money mid (the liquid side, and the one with
   no intrinsic value to subtract), provided its time value exceeds half the spread and one
   cent, applied to the call and the put of that strike, which
   parity says share one vol. Fallbacks, in order: the in-the-money mid, the vendor IV,
   linear interpolation of the smile in ln(K/F) within the series. Each contract records
   which one priced it (`iv_source`) and the diagnostics report the open interest per
   source.
6. Quote bounds. The same OTM rule applied to the bid and to the ask gives `sigma_bid` and
   `sigma_ask`: the vols the quotes cannot distinguish from the mid. They bound the
   measurement error of every level (exposures.zero_gamma_band). Where the vol did not come
   from a quote, both equal `sigma`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

import numpy as np
import pandas as pd

from . import config, pricing
from .data import Chain, ChainError


# --------------------------------------------------------------------------- clock

SECONDS_PER_YEAR = config.DAYS_PER_YEAR * 86400.0


def years_between(start: datetime, end: datetime) -> float:
    """Calendar time from `start` to `end`, in years (negative if `end` is earlier).

    Both are converted to UTC first: Python subtracts two datetimes sharing a tzinfo on the
    WALL clock, which is one hour off across a daylight-saving change (an expiry after the
    first Sunday of November measured from October).
    """
    return (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() / SECONDS_PER_YEAR


def ny(day: date, hm: tuple[int, int]) -> datetime:
    """New York datetime at clock time `hm` = (hour, minute) on `day`."""
    return datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=config.MARKET_TZ)


def settlement(root: str, expiry: date) -> datetime:
    """When time value of an option of `root` expiring on `expiry` runs out (AM or PM settled)."""
    return ny(expiry, config.SETTLEMENT_TIME.get(root, config.DEFAULT_SETTLEMENT_TIME))


def next_close(asof: datetime) -> datetime:
    """The next 16:00 New York on a weekday strictly after `asof`."""
    t = asof.astimezone(config.MARKET_TZ)
    close = ny(t.date(), config.SESSION_CLOSE)
    if close <= t:
        close += timedelta(days=1)
    while close.weekday() >= 5:
        close += timedelta(days=1)
    return close


def in_session(t: datetime) -> bool:
    """True on a weekday between the open and the options' close (09:30-16:15 New York)."""
    t = t.astimezone(config.MARKET_TZ)
    return t.weekday() < 5 and time(*config.SESSION_OPEN) <= t.time() <= time(*config.OPTIONS_CLOSE)


def session_lag_minutes(asof: datetime, now: datetime | None = None) -> float | None:
    """Minutes from `asof` to now while the market is open; None outside the session.

    Outside the session an old as-of is normal (the data is the last close). Inside, a lag
    above config.MAX_SESSION_LAG_MINUTES means the source stopped updating, which a failed
    download would not reveal.
    """
    now = now or datetime.now(config.MARKET_TZ)
    return years_between(asof, now) * SECONDS_PER_YEAR / 60.0 if in_session(now) else None


def stale_message(asof: datetime, now: datetime | None = None) -> str | None:
    """Warning text when, during the session, the data lags by more than MAX_SESSION_LAG_MINUTES."""
    now = (now or datetime.now(config.MARKET_TZ)).astimezone(config.MARKET_TZ)
    lag = session_lag_minutes(asof, now)
    if lag is None or lag <= config.MAX_SESSION_LAG_MINUTES:
        return None
    if now < ny(now.date(), config.SESSION_OPEN) + timedelta(minutes=config.MAX_SESSION_LAG_MINUTES):
        return None                      # the delayed feed still shows the previous close
    return (f"The data is {lag:.0f} minutes old during the session (expected about 15): "
            "the source may have stopped updating.")


# --------------------------------------------------------------------------- market state

@dataclass(frozen=True)
class MarketState:
    """A chain ready for exposure calculations. Immutable once built."""

    symbol: str
    asof: datetime
    spot: float                 # reference spot implied by the front forward
    spot_quoted: float          # vendor spot, for the diagnostics
    rate: float
    contracts: pd.DataFrame     # alive contracts with T, b, sigma, iv_source
    series: pd.DataFrame        # one row per (root, expiry): T, F, b, pairs, forward_source
    dropped: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)


def build(chain: Chain, rate: float = config.RISK_FREE_RATE) -> MarketState:
    """Price a raw chain: steps 1 to 6 of the module docstring. Raises ChainError if unusable."""
    df = chain.contracts.copy()

    # 1. Clock ---------------------------------------------------------------------------
    settle = {key: settlement(*key) for key in set(zip(df["root"], df["expiry"]))}
    df["T"] = [years_between(chain.asof, settle[key]) for key in zip(df["root"], df["expiry"])]
    expired = df["T"] < config.MIN_T_YEARS
    dropped = {"expired_contracts": int(expired.sum()), "expired_oi": float(df.loc[expired, "oi"].sum())}
    df = df.loc[~expired].reset_index(drop=True)
    if df.empty:
        raise ChainError(f"{chain.symbol}: every contract is past settlement at {chain.asof}")

    df["mid"] = _mid(df)
    if df["mid"].notna().sum() == 0:
        raise ChainError(f"{chain.symbol}: the file carries no two-sided quote yet. The delayed feed "
                         f"shows the open about 15 minutes after it; try again shortly.")
    df["df"] = np.exp(-rate * df["T"])

    # 2-4. Forwards, reference spot, carry --------------------------------------------------
    series = _forwards(df)
    priced = series.dropna(subset=["F"]).sort_values("T")
    if priced.empty:
        raise ChainError(f"{chain.symbol}: no expiry has {config.FORWARD_MIN_PAIRS} two-sided call/put pairs")
    front = priced.iloc[0]
    spot = float(front["F"] * np.exp(-rate * front["T"]))
    series["b"] = np.log(series["F"] / spot) / series["T"]
    series["forward_source"] = np.where(series["F"].notna(), "parity", "interpolated")
    missing = series["F"].isna()
    if missing.any():
        p = series.loc[~missing].sort_values("T")
        series.loc[missing, "b"] = np.interp(series.loc[missing, "T"], p["T"], p["b"])
        series.loc[missing, "F"] = spot * np.exp(series.loc[missing, "b"] * series.loc[missing, "T"])
    df = df.merge(series[["root", "expiry", "F", "b"]], on=["root", "expiry"], how="left")

    # 5. Implied vols ------------------------------------------------------------------------
    # A vol is only read from a price whose time value stands out of the quote: more than
    # half the bid-ask spread, and at least one cent.
    min_tv = np.maximum(0.5 * (df["ask"] - df["bid"]).fillna(0.0), config.MIN_TIME_VALUE)
    df["iv_mid"] = pricing.implied_vol(df["mid"].to_numpy(), df["F"].to_numpy(), df["strike"].to_numpy(),
                                       df["T"].to_numpy(), df["df"].to_numpy(), df["is_call"].to_numpy(),
                                       min_tv.to_numpy())
    df["sigma"], df["iv_source"] = _assign_vols(df)

    # 6. Quote bounds -------------------------------------------------------------------------
    floor = np.full(len(df), config.MIN_TIME_VALUE)
    args = (df["F"].to_numpy(), df["strike"].to_numpy(), df["T"].to_numpy(), df["df"].to_numpy(),
            df["is_call"].to_numpy(), floor)
    for side in ("bid", "ask"):
        quote = df[side].where(df["mid"].notna()).to_numpy()
        iv = pd.Series(pricing.implied_vol(quote, *args), index=df.index)
        bound = _otm_per_strike(df, iv).where(df["iv_source"] == "mid")
        df[f"sigma_{side}"] = bound.fillna(df["sigma"])
    df["sigma_bid"] = np.minimum(df["sigma_bid"], df["sigma"])
    df["sigma_ask"] = np.maximum(df["sigma_ask"], df["sigma"])

    unpriced = df["sigma"].isna()
    dropped.update(unpriced_contracts=int(unpriced.sum()), unpriced_oi=float(df.loc[unpriced, "oi"].sum()))
    df = df.loc[~unpriced].reset_index(drop=True)

    keep = ["contract", "root", "expiry", "is_call", "strike", "T", "F", "b", "sigma", "iv_source",
            "sigma_bid", "sigma_ask",
            "oi", "volume", "bid", "ask", "mid", "iv_vendor", "delta_vendor", "gamma_vendor"]
    return MarketState(symbol=chain.symbol, asof=chain.asof, spot=spot, spot_quoted=chain.spot_quoted,
                       rate=rate, contracts=df[keep], series=series, dropped=dropped,
                       meta=dict(chain.meta))


# --------------------------------------------------------------------------- shared helpers

def otm_mask(df: pd.DataFrame) -> np.ndarray:
    """True for out-of-the-money options: calls struck at or above the forward, puts below."""
    return np.where(df["is_call"], df["strike"] >= df["F"], df["strike"] < df["F"])


def atm_row(df: pd.DataFrame) -> pd.Series:
    """The contract whose strike is nearest its forward (the ATM of one series)."""
    return df.iloc[(df["strike"] - df["F"]).abs().to_numpy().argmin()]


def atm_vol(state: MarketState, T: float) -> float:
    """ATM implied vol at maturity T, interpolated linearly in total variance sigma^2 T.

    Per series, the ATM vol is the vol of the strike nearest the forward. Flat in vol
    outside the listed maturities.
    """
    rows = [(float(g["T"].iloc[0]), float(atm_row(g)["sigma"]))
            for _, g in state.contracts.groupby(["root", "expiry"])]
    ts, vols = np.array(sorted(rows)).T
    if T <= ts[0]:
        return float(vols[0])
    if T >= ts[-1]:
        return float(vols[-1])
    w = np.interp(T, ts, vols**2 * ts)
    return float(np.sqrt(w / T))


def one_day_move(state: MarketState) -> float:
    """One-day 1-sigma move of spot, in points: S sigma_ATM(1d) sqrt(1/365)."""
    return state.spot * atm_vol(state, config.ONE_DAY) * np.sqrt(config.ONE_DAY)


# --------------------------------------------------------------------------- build steps

def _mid(df: pd.DataFrame) -> pd.Series:
    """Mid of a usable two-sided quote, NaN otherwise."""
    bid, ask = df["bid"], df["ask"]
    mid = 0.5 * (bid + ask)
    usable = (bid > 0) & (ask >= bid) & ((ask - bid) <= config.MAX_REL_SPREAD * mid)
    return mid.where(usable)


def _forwards(df: pd.DataFrame) -> pd.DataFrame:
    """Parity forward of each (root, expiry) series, NaN where too few two-sided pairs."""
    rows = []
    for (root, expiry), g in df.groupby(["root", "expiry"], sort=False):
        T, disc = float(g["T"].iloc[0]), float(g["df"].iloc[0])
        calls = g.loc[g["is_call"]].set_index("strike")["mid"]
        puts = g.loc[~g["is_call"]].set_index("strike")["mid"]
        diff = (calls - puts).dropna()
        F, n = np.nan, len(diff)
        if n >= config.FORWARD_MIN_PAIRS:
            near = diff.abs().nsmallest(config.FORWARD_STRIKES).index
            F = float(np.median(near.to_numpy() + diff.loc[near].to_numpy() / disc))
        rows.append({"root": root, "expiry": expiry, "T": T, "F": F, "pairs": n})
    return pd.DataFrame(rows)


def _otm_per_strike(df: pd.DataFrame, iv: pd.Series) -> pd.Series:
    """For each contract, the value of `iv` on the out-of-the-money option of its strike."""
    otm = otm_mask(df)
    key = ["root", "expiry", "strike"]
    side = df[key].assign(v=np.where(otm, iv, np.nan))
    per_strike = side.groupby(key, sort=False)["v"].max()
    return pd.Series(per_strike.reindex(pd.MultiIndex.from_frame(df[key])).to_numpy(), index=df.index)


def _assign_vols(df: pd.DataFrame):
    """One vol per (series, strike): OTM mid > ITM mid > vendor > smile interpolation.

    Returns the vol of every contract and the name of the rule that gave it.
    """
    otm = otm_mask(df)
    key = ["root", "expiry", "strike"]
    side = df[key].copy()
    side["iv_mid_otm"] = np.where(otm, df["iv_mid"], np.nan)
    side["iv_mid_itm"] = np.where(~otm, df["iv_mid"], np.nan)
    vendor_ok = df["iv_vendor"].between(config.IV_MIN, config.IV_MAX)
    side["iv_vendor_otm"] = np.where(otm & vendor_ok, df["iv_vendor"], np.nan)
    side["iv_vendor_any"] = np.where(vendor_ok, df["iv_vendor"], np.nan)
    per_strike = side.groupby(key, sort=False).max()   # max over two rows of which one is NaN
    per_strike = per_strike.reindex(pd.MultiIndex.from_frame(df[key])).reset_index(drop=True)

    sigma = np.full(len(df), np.nan)
    source = np.full(len(df), None, dtype=object)
    for col, label in (("iv_mid_otm", "mid"), ("iv_mid_itm", "mid"),
                       ("iv_vendor_otm", "vendor"), ("iv_vendor_any", "vendor")):
        fill = np.isnan(sigma) & per_strike[col].notna().to_numpy()
        sigma[fill] = per_strike[col].to_numpy()[fill]
        source[fill] = label

    # Smile interpolation within each series, in log-moneyness, flat beyond the last quote.
    if np.isnan(sigma).any():
        x = np.log(df["strike"] / df["F"]).to_numpy()
        for rows in df.groupby(["root", "expiry"], sort=False).indices.values():
            gap, known = rows[np.isnan(sigma[rows])], rows[~np.isnan(sigma[rows])]
            if gap.size == 0 or known.size == 0:
                continue
            order = np.argsort(x[known])
            sigma[gap] = np.interp(x[gap], x[known][order], sigma[known][order])
            source[gap] = "smile"
    return pd.Series(sigma, index=df.index), pd.Series(source, index=df.index)
