"""Dealer exposures: positions, GEX / VEX / CHEX, levels, the front expiry, scenarios.

Exposures and levels
--------------------
Dealer exposures (GEX, VEX, CHEX), structural levels and the GEX(S) profile.

Positioning convention ("standard"): customers are net sellers of calls and net buyers of
puts, so dealers are LONG calls (+1) and SHORT puts (-1). It is an assumption about who holds
the open interest, not an observation; the call and put legs are therefore always reported
separately so a reader can apply another view.

Per contract i, with OI_i contracts of multiplier m, all at spot S:

    GEX_i  = sign_i OI_i m  gamma_i S^2 / 100   dealer $-delta change for a +1% move in S
    VEX_i  = sign_i OI_i m  vanna_i S   / 100   dealer $-delta change for +1 vol point
    CHEX_i = sign_i OI_i m  charm_i S   / 365   dealer $-delta change over one calendar day

The hedge flow that offsets each is its NEGATIVE: with GEX > 0 a rally leaves dealers longer
delta, which they sell.

Every total, every strike bar and every point of the profile goes through `net_gex`, so the
profile evaluated at the reference spot equals the reported net GEX exactly
(tests/test_exposures.py holds that invariant).

Front expiry (0DTE)
-------------------
Front-expiry (0DTE when the front settles on the as-of date) analytics.

The former "Pin Score" blended distance, concentration, time and regime with hand-picked
weights (50/30/20, x0.4 in negative gamma) into a 0-100 number that read like a probability
and was not one. It is replaced by its components, each with a unit, so the reader does the
weighting:

* time to settlement, in hours;
* the 1-sigma move left to settlement, S sigma_ATM sqrt(T), from the front ATM implied vol;
* the strike of largest |net GEX| within two expected moves of spot, its distance in points
  and in expected moves (a strike 0.2 EM away is "in play", one 1.8 EM away is not);
* the share of the front expiry's |GEX| sitting on that strike (concentration);
* the front expiry's share of the whole book's |GEX| and of the session volume.

`projection` shows the front expiry's GEX by strike at later clock times of its expiry day,
spot and vols held: as T shrinks, gamma concentrates on the strikes nearest spot (it tends
to a Dirac at settlement). This is the part of the end-of-day hedging a single snapshot
does not show.

Scenarios
---------
Dealer hedge flows under explicit shocks of spot, implied vol and time.

GEX, VEX and CHEX are local slopes. A desk wants the finite answer: "if implied vol drops 3
points, how much do dealers have to buy?". Each scenario here re-prices the whole book's
delta (no Taylor expansion), so large shocks and expiries in between are handled exactly:

    change in dealer delta  dD = sum_i pos_i m [Delta_i(S', sigma_i + dsigma, T_i - dt) - Delta_i(S, sigma_i, T_i)]
    hedge flow              = -dD units of the underlying (bought if positive)

Conventions: sticky strike (each contract keeps its vol, plus the parallel shock), carry
held, positions as in exposures.dealer_position. A contract whose settlement falls inside
the scenario's time step settles: a cash-settled index option leaves no position (its delta
becomes 0, the dealer unwinds the hedge), an ETF option settles into shares (its delta
becomes 1 or -1 if in the money, 0 otherwise).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import brentq

from . import config, pricing, surface
from .surface import MarketState


# --------------------------------------------------------------------------- exposures and levels

def expiries(state: MarketState) -> list:
    """Live expiry dates of the chain, ascending."""
    return sorted(state.contracts["expiry"].unique())


def select(state: MarketState, expiry: str = "all") -> pd.DataFrame:
    """Contracts of one view: 'all', 'front' (first live expiry date) or 'YYYY-MM-DD'.

    A date selects every root settling that day (the AM SPX monthly and the PM SPXW of a
    third Friday together), because both hedge into the same underlying that day.
    """
    c = state.contracts
    if expiry in (None, "", "all"):
        return c
    target = expiries(state)[0] if expiry == "front" else pd.Timestamp(expiry).date()
    sub = c.loc[c["expiry"] == target]
    if sub.empty:
        raise ValueError(f"no live contracts expiring {expiry}")
    return sub


# --------------------------------------------------------------------------- positions

def dealer_position(sub: pd.DataFrame) -> np.ndarray:
    """Dealer contracts per row under the standard convention: +OI on calls, -OI on puts."""
    return np.where(sub["is_call"], 1.0, -1.0) * sub["oi"].to_numpy(float)


def dollar_gex(weight, gamma, spot):
    """Dollar GEX, $ of dealer delta per +1% move: weight (contracts x multiplier) x gamma x S^2 / 100."""
    return weight * gamma * spot * spot / 100.0


def reprice(sub: pd.DataFrame, spot: float, rate: float, dt: float = 0.0, vol_shift: float = 0.0) -> dict:
    """Delta and gamma of every contract at `spot`, `dt` years later, vols shifted by `vol_shift`.

    Sticky strike, carry held. A contract whose settlement falls inside `dt` has settled: its
    gamma is 0; its delta is 0 if cash-settled (index), or the share position it turned into
    if physically settled (ETF): +1 / -1 when in the money, 0 otherwise.
    """
    K, call = sub["strike"].to_numpy(), sub["is_call"].to_numpy()
    T = sub["T"].to_numpy() - dt
    live = T > config.MIN_T_YEARS
    delta, gamma = np.zeros(len(sub)), np.zeros(len(sub))
    if live.any():
        sig = np.maximum(sub["sigma"].to_numpy()[live] + vol_shift, config.IV_MIN)
        g = pricing.greeks(spot, K[live], T[live], sig, sub["b"].to_numpy()[live], rate, call[live])
        delta[live], gamma[live] = g["delta"], g["gamma"]
    settled = ~live
    if settled.any():
        physical = ~sub["root"].isin(config.CASH_SETTLED_ROOTS).to_numpy()
        itm = np.where(call, spot > K, spot < K)
        delta[settled] = np.where(physical[settled] & itm[settled], np.where(call[settled], 1.0, -1.0), 0.0)
    return {"delta": delta, "gamma": gamma}


# --------------------------------------------------------------------------- core function

@dataclass(frozen=True)
class Book:
    """The arrays `net_gex` needs, extracted once per view (only contracts with a position).

    `sticky="strike"` (default): each contract keeps its implied vol while S moves; on an
    equity skew, the at-the-money vol therefore rises as spot falls.
    `sticky="moneyness"`: the smile of each expiry moves with the forward, so a contract's vol
    at spot S is the smile read at ln(K / F(S)). ATM vol stays put, and a fixed strike's vol
    falls as spot falls (it is read further to the right of the skew).
    """

    K: np.ndarray
    T: np.ndarray
    sigma: np.ndarray
    b: np.ndarray
    weight: np.ndarray      # dealer contracts * multiplier
    rate: float
    spot0: float = 0.0
    sticky: str = "strike"
    smiles: tuple = ()      # per expiry series: (row indices, x0 = ln(K/F0), sorted x, sorted vol)

    @classmethod
    def of(cls, sub: pd.DataFrame, rate: float, position=None, sigma_col: str = "sigma",
           sticky: str = "strike", spot0: float = 0.0) -> "Book":
        """Extract the arrays of `sub`, for a dealer `position` (default: the convention).

        `sigma_col` picks the vols (sigma, sigma_bid, sigma_ask); `spot0` is the spot the
        smiles were read at, needed for sticky moneyness.
        """
        pos = dealer_position(sub) if position is None else np.asarray(position, float)
        live = sub.loc[pos != 0]
        smiles = ()
        if sticky == "moneyness":
            x_live = np.log(live["strike"] / live["F"]).to_numpy()
            rows_of = live.groupby(["root", "expiry"], sort=False).indices
            groups = []
            for key, g in sub.groupby(["root", "expiry"], sort=False):
                if key not in rows_of:
                    continue
                gx = np.log(g["strike"] / g["F"]).to_numpy()
                order = np.argsort(gx)
                rows = rows_of[key]
                groups.append((rows, x_live[rows], gx[order], g[sigma_col].to_numpy()[order]))
            smiles = tuple(groups)
        return cls(K=live["strike"].to_numpy(float), T=live["T"].to_numpy(float),
                   sigma=live[sigma_col].to_numpy(float), b=live["b"].to_numpy(float),
                   weight=pos[pos != 0] * config.MULTIPLIER, rate=rate, spot0=spot0, sticky=sticky,
                   smiles=smiles)

    def vols(self, S: float) -> np.ndarray:
        """Vol of every contract when spot is at `S` (per `sticky`)."""
        if self.sticky == "strike":
            return self.sigma
        out = np.empty_like(self.sigma)
        shift = np.log(S / self.spot0)
        for rows, x0, xs, vs in self.smiles:
            out[rows] = np.interp(x0 - shift, xs, vs)
        return out


def net_gex(book: Book, S) -> np.ndarray:
    """Net dealer GEX ($ per 1% move) at one spot or an array of spots.

    Time is frozen and carry is held: the profile is "the book as it is now, re-marked at
    another spot", with vols per `book.sticky`.
    """
    S = np.atleast_1d(np.asarray(S, float))
    out = np.empty(S.shape)
    for start in range(0, S.size, 16):           # 16 spots x N contracts per block
        s = S[start:start + 16, None]
        sig = book.sigma if book.sticky == "strike" else np.vstack([book.vols(x) for x in s[:, 0]])
        g = pricing.gamma(s, book.K, book.T, sig, book.b, book.rate)
        out[start:start + 16] = dollar_gex((g * book.weight).sum(axis=1), 1.0, s[:, 0])
    return out


# --------------------------------------------------------------------------- per contract / strike

def contract_exposures(sub: pd.DataFrame, spot: float, rate: float, position=None) -> pd.DataFrame:
    """GEX, VEX and CHEX of every contract at `spot`, for a dealer `position` in contracts
    (default: the standard convention, `dealer_position`)."""
    g = pricing.greeks(spot, sub["strike"].to_numpy(), sub["T"].to_numpy(), sub["sigma"].to_numpy(),
                       sub["b"].to_numpy(), rate, sub["is_call"].to_numpy())
    pos = dealer_position(sub) if position is None else np.asarray(position, float)
    w = pos * config.MULTIPLIER
    out = sub.copy()
    out["position"] = pos
    out["delta"], out["gamma"] = g["delta"], g["gamma"]
    out["gex"] = dollar_gex(w, g["gamma"], spot)
    out["vex"] = w * g["vanna"] * spot / 100.0
    out["chex"] = w * g["charm"] * spot / config.DAYS_PER_YEAR
    return out


def by_strike(ex: pd.DataFrame) -> pd.DataFrame:
    """One row per strike: call and put legs, net, OI and volume."""
    calls, puts = ex.loc[ex["is_call"]], ex.loc[~ex["is_call"]]
    out = pd.DataFrame({
        "call_gex": calls.groupby("strike")["gex"].sum(),
        "put_gex": puts.groupby("strike")["gex"].sum(),
        "call_oi": calls.groupby("strike")["oi"].sum(),
        "put_oi": puts.groupby("strike")["oi"].sum(),
        "call_volume": calls.groupby("strike")["volume"].sum(),
        "put_volume": puts.groupby("strike")["volume"].sum(),
        "vex": ex.groupby("strike")["vex"].sum(),
        "chex": ex.groupby("strike")["chex"].sum(),
    }).fillna(0.0).sort_index()
    out["net_gex"] = out["call_gex"] + out["put_gex"]
    return out.reset_index()


# --------------------------------------------------------------------------- levels

def walls(strikes: pd.DataFrame, spot: float) -> dict:
    """Call wall at or above spot, put wall at or below spot, largest |gamma| strike anywhere.

    Each wall is searched on its own side of spot, symmetrically. Unrestricted, the largest
    put GEX is often an IN-the-money strike above spot (on the fixture: SPX 8000, carrying a
    large December put OI), which cannot play the "support" role the level is read for. The
    unrestricted extreme is still reported as `abs_gamma_strike`.
    """
    out = {"call_wall": None, "call_wall_gex": None, "put_wall": None, "put_wall_gex": None,
           "abs_gamma_strike": None}
    if strikes.empty:
        return out
    above, below = strikes.loc[strikes["strike"] >= spot], strikes.loc[strikes["strike"] <= spot]
    if not above.empty and above["call_gex"].max() > 0:
        c = above.loc[above["call_gex"].idxmax()]
        out.update(call_wall=float(c["strike"]), call_wall_gex=float(c["call_gex"]))
    if not below.empty and below["put_gex"].min() < 0:
        p = below.loc[below["put_gex"].idxmin()]
        out.update(put_wall=float(p["strike"]), put_wall_gex=float(p["put_gex"]))
    a = strikes.loc[(strikes["call_gex"].abs() + strikes["put_gex"].abs()).idxmax()]
    out["abs_gamma_strike"] = float(a["strike"])
    return out


def max_pain(sub: pd.DataFrame) -> float | None:
    """Settlement price, among the listed strikes, minimizing the option holders' payoff.

    Defined for ONE expiry date only; a single settlement price across expiries means
    nothing, so the caller passes one date's contracts.
    """
    if sub.empty or sub["expiry"].nunique() != 1:
        return None
    K, oi, call = sub["strike"].to_numpy(), sub["oi"].to_numpy(), sub["is_call"].to_numpy()
    grid = np.unique(K)[:, None]
    payoff = np.where(call, np.maximum(grid - K, 0.0), np.maximum(K - grid, 0.0)) * oi
    return float(grid[payoff.sum(axis=1).argmin(), 0])


# --------------------------------------------------------------------------- profile & zero gamma

def profile(book: Book, spot: float, half_range: float = config.PROFILE_RANGE,
            points: int = config.PROFILE_POINTS) -> dict:
    """GEX(S) on a grid around spot and every zero crossing, refined by Brent's method.

    The grid only brackets sign changes; each root is then solved on the exact function to
    1e-6 of a point. All roots are returned; `zero_gamma` is the one nearest to spot.
    """
    grid = np.linspace(spot * (1 - half_range), spot * (1 + half_range), points)
    values = net_gex(book, grid)
    f = lambda s: float(net_gex(book, s)[0])
    roots = []
    for i in np.flatnonzero(np.sign(values[:-1]) * np.sign(values[1:]) < 0):
        roots.append(brentq(f, grid[i], grid[i + 1], xtol=1e-6, rtol=1e-12))
    roots += [float(grid[i]) for i in np.flatnonzero(values == 0.0)]
    roots = sorted(roots)
    nearest = min(roots, key=lambda x: abs(x - spot)) if roots else None
    return {"grid": grid, "values": values, "roots": roots, "zero_gamma": nearest,
            "half_range": half_range}


def zero_gamma_band(sub: pd.DataFrame, spot: float, rate: float,
                    half_range: float = config.PROFILE_RANGE) -> dict:
    """Zero gamma under alternative, equally defensible assumptions, and their range.

    The book itself (open interest) is exact; what is uncertain is how vols behave when
    spot moves, how precisely the quotes fix them, and which expiries are meant. One
    deterministic variant per question, rather than a bootstrap whose noise model would be
    invented:

    * base: sticky strike, mid vols;
    * sticky moneyness: the smile moves with spot;
    * bid vols / ask vols: every vol at the bid or at the ask of its quote;
    * without expiries beyond one year;
    * without the front expiry date (only when the view holds several dates).

    `indeterminate` is True when the range contains spot: the sign of the regime then
    depends on an assumption, not on the data.
    """
    variants = [("Base (sticky strike, mid vols)", sub, {}),
                ("Sticky moneyness", sub, {"sticky": "moneyness", "spot0": spot}),
                ("Vols at bid", sub, {"sigma_col": "sigma_bid"}),
                ("Vols at ask", sub, {"sigma_col": "sigma_ask"}),
                ("Without expiries > 1 year", sub.loc[sub["T"] <= 1.0], {})]
    if sub["expiry"].nunique() > 1:
        front = min(sub["expiry"])
        variants.append(("Without the front expiry", sub.loc[sub["expiry"] != front], {}))

    rows = []
    for name, data, kw in variants:
        if data.empty:
            continue
        prof = profile(Book.of(data, rate, **kw), spot, half_range=half_range)
        rows.append({"variant": name, "zero_gamma": prof["zero_gamma"], "roots": len(prof["roots"]),
                     "net_gex_at_spot": float(net_gex(Book.of(data, rate, **kw), spot)[0])})
    found = [r["zero_gamma"] for r in rows if r["zero_gamma"] is not None]
    lo, hi = (min(found), max(found)) if found else (None, None)
    signs = {np.sign(r["net_gex_at_spot"]) for r in rows}
    return {"variants": rows, "low": lo, "high": hi,
            "indeterminate": bool(len(signs) > 1 or (lo is not None and lo <= spot <= hi))}


# --------------------------------------------------------------------------- front expiry

def _front(contracts: pd.DataFrame, state: MarketState) -> pd.DataFrame:
    """Contracts of the front (first live) expiry date."""
    return contracts.loc[contracts["expiry"] == expiries(state)[0]]


def _expected_move(state: MarketState, front_contracts: pd.DataFrame) -> tuple[float, float, float]:
    """(ATM vol, years to settlement, 1-sigma move in points) of the front expiry.

    ATM = strike nearest the forward of the root carrying the most open interest that day
    (on a third Friday the PM-settled weekly, not the AM monthly).
    """
    main_root = front_contracts.groupby("root")["oi"].sum().idxmax()
    sigma_atm = float(surface.atm_row(front_contracts.loc[front_contracts["root"] == main_root])["sigma"])
    T = float(front_contracts["T"].max())
    return sigma_atm, T, state.spot * sigma_atm * np.sqrt(T)


def front_metrics(state: MarketState, ex_all: pd.DataFrame) -> dict:
    """`ex_all`: contract exposures of the whole book at the reference spot."""
    spot = state.spot
    ex = _front(ex_all, state)
    front_date = ex["expiry"].iloc[0]
    sigma_atm, T, em = _expected_move(state, ex)

    strikes = by_strike(ex)
    near = strikes.loc[(strikes["strike"] - spot).abs() <= 2 * em]
    top = None
    if not near.empty:
        r = near.loc[near["net_gex"].abs().idxmax()]
        total_abs = strikes["net_gex"].abs().sum()
        top = {
            "strike": float(r["strike"]),
            "net_gex": float(r["net_gex"]),
            "distance_pts": float(r["strike"] - spot),
            "distance_em": float((r["strike"] - spot) / em) if em > 0 else None,
            "share_of_front_abs_gex": float(abs(r["net_gex"]) / total_abs) if total_abs > 0 else None,
        }

    abs_front, abs_all = ex["gex"].abs().sum(), ex_all["gex"].abs().sum()
    vol_front, vol_all = ex["volume"].sum(), ex_all["volume"].sum()
    return {
        "expiry": str(front_date),
        "is_0dte": front_date == state.asof.date(),
        "hours_to_settlement": T * config.DAYS_PER_YEAR * 24.0,
        "atm_iv": sigma_atm,
        "expected_move_pts": em,
        "net_gex": float(ex["gex"].sum()),
        "call_gex": float(ex.loc[ex["is_call"], "gex"].sum()),
        "put_gex": float(ex.loc[~ex["is_call"], "gex"].sum()),
        "max_pain": max_pain(ex),
        "top_strike": top,
        "share_of_book_abs_gex": float(abs_front / abs_all) if abs_all > 0 else None,
        "share_of_volume": float(vol_front / vol_all) if vol_all > 0 else None,
    }


def front_projection(state: MarketState, window_em: float = 2.0) -> dict:
    """Front-expiry GEX by strike now and at `config.PROJECTION_TIMES` of its expiry day.

    Strikes within `window_em` expected moves of spot. Only times after `asof` and before
    each contract's settlement are evaluated; a contract that has settled by then (the
    AM-settled monthly on a third Friday) drops out.
    """
    spot, rate = state.spot, state.rate
    ex = _front(state.contracts, state)
    front_date = ex["expiry"].iloc[0]
    _, _, em = _expected_move(state, ex)
    ex = ex.loc[(ex["oi"] > 0) & ((ex["strike"] - spot).abs() <= window_em * max(em, 1e-9))]
    weight = dealer_position(ex) * config.MULTIPLIER
    strikes = np.unique(ex["strike"].to_numpy())

    times = [("now", state.asof)] + [(f"{h:02d}:{m:02d}", surface.ny(front_date, (h, m)))
                                     for h, m in config.PROJECTION_TIMES]
    out = {"expiry": str(front_date), "strikes": strikes.tolist(), "series": []}
    for label, t in times:
        if label != "now" and t <= state.asof:
            continue
        gamma = reprice(ex, spot, rate, dt=surface.years_between(state.asof, t))["gamma"]
        gex = dollar_gex(weight, gamma, spot)
        per_strike = pd.Series(gex).groupby(ex["strike"].to_numpy()).sum().reindex(strikes, fill_value=0.0)
        out["series"].append({"label": label, "time": t.isoformat(), "net_gex": per_strike.tolist()})
    return out


# --------------------------------------------------------------------------- scenarios

def dealer_delta(sub: pd.DataFrame, spot: float, rate: float, vol_shift: float = 0.0,
                 dt: float = 0.0, position=None) -> float:
    """Dealer delta in units of the underlying at `spot`, vols + `vol_shift`, time + `dt` years."""
    pos = dealer_position(sub) if position is None else np.asarray(position, float)
    delta = reprice(sub, spot, rate, dt=dt, vol_shift=vol_shift)["delta"]
    return float((pos * config.MULTIPLIER * delta).sum())


def scenario_table(state: MarketState, sub: pd.DataFrame) -> pd.DataFrame:
    """One row per scenario: change in dealer delta, hedge flow in $ and in futures or shares."""
    S, r = state.spot, state.rate
    base = dealer_delta(sub, S, r)
    close = surface.next_close(state.asof)
    to_close = surface.years_between(state.asof, close)
    rows = []
    for dv in config.VOL_SHOCKS:
        rows.append(("Implied vol", f"{dv:+.0f} vol pts", S, dv / 100.0, 0.0))
    for ds in config.SPOT_SHOCKS:
        rows.append(("Spot", f"{100 * ds:+.0f}%", S * (1 + ds), 0.0, 0.0))
    rows.append(("Time", f"to next close ({close:%a %H:%M} ET)", S, 0.0, to_close))
    rows.append(("Time", "+1 calendar day", S, 0.0, 1.0 / config.DAYS_PER_YEAR))
    rows.append(("Time", "+3 calendar days", S, 0.0, 3.0 / config.DAYS_PER_YEAR))

    out = []
    for kind, label, s1, dv, dt in rows:
        d = dealer_delta(sub, s1, r, vol_shift=dv, dt=dt) - base
        flow_units = -d
        out.append({"shock": kind, "scenario": label, "dealer_delta_change": d,
                    "hedge_flow_usd": flow_units * s1,
                    "hedge_flow_hedge_units": hedge_units(state.symbol, flow_units)})
    return pd.DataFrame(out)


def hedge_units(symbol: str, units: float) -> float:
    """Flow in futures contracts (indices) or shares (ETFs).

    A future's notional is multiplier x index level, and the index level is ratio x S, so
    `units` of the underlying (worth units x S) are units / (multiplier x ratio) contracts.
    The future is taken at spot; the basis (well under 1% at front-month tenors) is ignored.
    """
    if symbol in config.FUTURES:
        _, mult, ratio = config.FUTURES[symbol]
        return units / (mult * ratio)
    return units


def hedge_unit_name(symbol: str) -> str:
    """Label of the hedge unit of `symbol`: futures contracts for indices, shares for ETFs."""
    return f"{config.FUTURES[symbol][0]} contracts" if symbol in config.FUTURES else f"{symbol} shares"
