"""Snapshot
--------
One call from a market state to the full, JSON-ready payload of a view.

Everything the page shows for one (symbol, expiry view) comes out of `build` in one pass at
one spot, so no two numbers on screen can come from different chains or different models.

Data quality
------------
Data-quality report attached to every snapshot.

A GEX number is only as good as the chain under it. These checks travel with the numbers
so a stale file, a missing expiry or a vendor disagreement is visible on the page rather
than discovered later:

* freshness: as-of time, last index print and option trade, file generation time;
* coverage: contracts dropped as expired or unpriceable, open interest by IV source;
* forwards: series priced by parity vs interpolated, vendor spot vs implied spot;
* model check: our gamma and IV against the vendor's on liquid near-the-money contracts.
  The vendor rounds gamma to 1e-4, so the ratio is only compared where gamma >= 5e-4.
"""

from __future__ import annotations


import numpy as np
import pandas as pd

from . import config, exposures, surface
from .surface import MarketState


# --------------------------------------------------------------------------- snapshot

def build(state: MarketState, expiry: str = "all", window: float = 0.06,
          half_range: float = config.PROFILE_RANGE) -> dict:
    """Payload of one view: summary, profile, strikes within `window` of spot, front, quality."""
    spot, rate = state.spot, state.rate
    ex_all = exposures.contract_exposures(state.contracts, spot, rate)
    sub = exposures.select(state, expiry)
    ex = ex_all.loc[sub.index]

    book = exposures.Book.of(sub, rate)
    prof = exposures.profile(book, spot, half_range=half_range)
    net = float(exposures.net_gex(book, spot)[0])

    strikes = exposures.by_strike(ex)
    levels = exposures.walls(strikes, spot)
    single_date = ex["expiry"].nunique() == 1
    zg = prof["zero_gamma"]

    shown = strikes.loc[(strikes["strike"] - spot).abs() <= window * spot]
    one_day = surface.one_day_move(state)
    sig = lambda level: None if level is None or one_day <= 0 else (level - spot) / one_day
    trapdoor = bool(zg is not None and spot < zg and levels["put_wall"] is not None
                    and sig(levels["put_wall"]) >= -config.TRAPDOOR_SIGMAS)
    return {
        "symbol": state.symbol,
        "asof": state.asof.isoformat(),
        "spot": spot,
        "summary": {
            "net_gex": net,
            "call_gex": float(ex.loc[ex["is_call"], "gex"].sum()),
            "put_gex": float(ex.loc[~ex["is_call"], "gex"].sum()),
            "net_vex": float(ex["vex"].sum()),
            "net_chex": float(ex["chex"].sum()),
            "regime": "positive" if net > 0 else "negative",
            "zero_gamma": zg,
            "zero_gamma_distance_pct": 100.0 * (zg / spot - 1) if zg else None,
            "zero_gamma_roots": prof["roots"],
            "max_pain": exposures.max_pain(ex) if single_date else None,
            **levels,
            "one_day_move": one_day,
            "zero_gamma_sigmas": sig(zg),
            "call_wall_sigmas": sig(levels["call_wall"]),
            "put_wall_sigmas": sig(levels["put_wall"]),
            "trapdoor": trapdoor,
        },
        "profile": {"spot": prof["grid"].round(4).tolist(), "net_gex": prof["values"].tolist(),
                    "half_range": half_range},
        "strikes": shown.round(6).to_dict(orient="list"),
        "front": exposures.front_metrics(state, ex_all),
        "quality": quality_report(state, ex_all),
    }


# --------------------------------------------------------------------------- data quality

def quality_report(state: MarketState, ex_all: pd.DataFrame) -> dict:
    """Freshness, coverage, forward and model checks of a state; `ex_all` = its contract exposures."""
    c = state.contracts
    oi = c["oi"].sum()
    oi_by_source = (c.groupby("iv_source")["oi"].sum() / oi).to_dict() if oi > 0 else {}

    generated = state.meta.get("generated_utc")

    near = (ex_all["strike"] / state.spot - 1).abs() < 0.05
    liquid = near & (ex_all["iv_source"] == "mid") & (ex_all["oi"] > 0)
    g_ok = liquid & (ex_all["gamma_vendor"] >= 5e-4)
    gamma_ratio = (ex_all.loc[g_ok, "gamma"] / ex_all.loc[g_ok, "gamma_vendor"])
    iv_ok = liquid & ex_all["iv_vendor"].between(0.01, 3.0)
    iv_diff = 100.0 * (ex_all.loc[iv_ok, "sigma"] - ex_all.loc[iv_ok, "iv_vendor"])

    total_oi = oi + state.dropped.get("expired_oi", 0.0) + state.dropped.get("unpriced_oi", 0.0)
    return {
        "asof": state.asof.isoformat(),
        "underlying_time": state.meta.get("underlying_time"),
        "options_last_trade": state.meta.get("options_last_trade"),
        "file_generated_utc": generated,
        "contracts_priced": int(len(c)),
        "expired_contracts": state.dropped.get("expired_contracts", 0),
        "expired_oi_share": _share(state.dropped.get("expired_oi", 0.0), total_oi),
        "unpriced_contracts": state.dropped.get("unpriced_contracts", 0),
        "unpriced_oi_share": _share(state.dropped.get("unpriced_oi", 0.0), total_oi),
        "oi_share_by_iv_source": {k: float(v) for k, v in oi_by_source.items()},
        "series": int(len(state.series)),
        "series_forward_interpolated": int((state.series["forward_source"] == "interpolated").sum()),
        "spot_quoted": state.spot_quoted,
        "spot_gap_bp": 1e4 * (state.spot_quoted / state.spot - 1) if np.isfinite(state.spot_quoted) else None,
        "gamma_vs_vendor": _dist(gamma_ratio),
        "iv_minus_vendor_volpts": _dist(iv_diff),
    }


def _share(x, total):
    """x / total, 0 when the total is 0."""
    return float(x / total) if total > 0 else 0.0


def _dist(s: pd.Series) -> dict | None:
    """Count, median, 5th and 95th percentiles of the finite values; None if there are none."""
    s = s.replace([np.inf, -np.inf], np.nan).dropna()
    if s.empty:
        return None
    return {"n": int(s.size), "median": float(s.median()),
            "p05": float(s.quantile(0.05)), "p95": float(s.quantile(0.95))}
