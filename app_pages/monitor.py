"""Monitor: levels, profile, front expiry and data quality of the selected view.

Read at the close, once or twice a day: the page does not poll the source; the sidebar's
Refresh button downloads the current file.
"""

import pandas as pd
import streamlit as st

import ui
from gex_engine import charts, config


def kpis(p: dict, band: dict) -> None:
    """The headline row: spot, regime, zero gamma, walls, one-day move, VEX, CHEX."""
    s, q, spot = p["summary"], p["quality"], p["spot"]
    if band["indeterminate"]:
        regime = "Indeterminate: the zero-gamma band contains spot"
    elif s["regime"] == "positive":
        regime = "Positive gamma: lower next-day vol (SPX history)"
    else:
        regime = "Negative gamma: higher next-day vol, no trend (SPX history)"
    gap = "" if q["spot_gap_bp"] is None else f"vendor {ui.px(q['spot_quoted'])} ({ui.sgn(q['spot_gap_bp'], 1)} bp)"
    zg = s["zero_gamma"]
    zg_note = (f"{ui.pct(s['zero_gamma_distance_pct'])} · {ui.sigmas(s['zero_gamma_sigmas'])}"
               + ("" if band["low"] is None else f" · band {ui.px(band['low'], 0)} to {ui.px(band['high'], 0)}")
               if zg else f"no sign change within ±{100 * p['profile']['half_range']:.0f}%")

    def wall(name: str, side: str) -> None:
        level = s[f"{side}_wall"]
        note = ("none on this side" if level is None else
                f"{ui.pct(100 * (level / spot - 1))} · {ui.sigmas(s[f'{side}_wall_sigmas'])} · {ui.bn(s[f'{side}_wall_gex'])} $bn")
        ui.kpi(name, ui.strike(level), note)

    with st.container(horizontal=True):
        ui.kpi("Spot (implied)", ui.px(spot), gap, help="Spot consistent with the option prices (put-call parity).")
        ui.kpi("Net GEX, $bn per 1%", ui.tone(ui.bn(s["net_gex"]), s["net_gex"]), regime)
        ui.kpi("Zero gamma", ui.px(zg) if zg else "none", zg_note,
               help="Band: zero gamma under sticky moneyness, bid / ask vols, and without long-dated or front expiries.")
        wall("Call wall", "call")
        wall("Put wall", "put")
        ui.kpi("1-day move (1σ)", f"±{ui.px(s['one_day_move'])}",
               f"ATM 1-day IV {100 * s['one_day_move'] / spot * config.DAYS_PER_YEAR ** 0.5:.1f}%",
               help="S × ATM implied vol at one day × √(1/365). Distances above are in these units.")
        ui.kpi("Net VEX, $bn per vol pt", ui.tone(ui.bn(s["net_vex"]), s["net_vex"]), "dealer delta if IV +1 pt")
        ui.kpi("Net CHEX, $bn per day", ui.tone(ui.bn(s["net_chex"]), s["net_chex"]), "dealer delta after one day")

    if s["trapdoor"]:
        st.warning(f"Trapdoor: spot is below zero gamma and the put wall ({ui.strike(s['put_wall'])}) is "
                   f"{ui.sigmas(s['put_wall_sigmas'])} away. Below it, dealer hedging would add to a fall (mechanics, not a measured forecast).",
                   icon=":material/warning:")


def front_table(f: dict) -> None:
    """Front-expiry (0DTE) metrics."""
    t = f["top_strike"]
    ui.kv({
        "Expiry": f["expiry"] + (" (0DTE)" if f["is_0dte"] else ""),
        "Time to settlement": f"{f['hours_to_settlement']:.1f} h",
        "ATM implied vol": f"{100 * f['atm_iv']:.2f}%",
        "1σ move to settlement": f"±{ui.px(f['expected_move_pts'])}",
        "Net GEX, $bn per 1%": ui.bn(f["net_gex"]),
        "Call / put GEX, $bn": f"{ui.bn(f['call_gex'])} / {ui.bn(f['put_gex'])}",
        "Max pain": ui.strike(f["max_pain"]),
        "Largest |GEX| strike within 2σ": "n/a" if t is None else
            f"{ui.strike(t['strike'])} ({ui.sgn(t['distance_pts'])} pts, {ui.sgn(t['distance_em'])}σ)",
        "Its share of front |GEX|": ui.share(t["share_of_front_abs_gex"]) if t else "n/a",
        "Front share of book |GEX|": ui.share(f["share_of_book_abs_gex"]),
        "Front share of volume": ui.share(f["share_of_volume"]),
    }, alt="Front expiry metrics")


def quality_table(q: dict) -> None:
    """Freshness, coverage and the check against the vendor's Greeks."""
    dist = lambda d, k=3: "n/a" if d is None else f"{d['median']:.{k}f} [{d['p05']:.{k}f}, {d['p95']:.{k}f}]"
    ui.kv({
        "As of (earlier of the two)": q["asof"][:19].replace("T", " ") + " ET",
        "Last index print / option trade":
            f"index {(q['underlying_time'] or 'n/a')[11:19]} · options {(q['options_last_trade'] or 'n/a')[11:19]}",
        "File generated (UTC)": q["file_generated_utc"] or "n/a",
        "Contracts priced": f"{q['contracts_priced']:,}",
        "Dropped as settled": f"{q['expired_contracts']:,} ({ui.share(q['expired_oi_share'])} of OI)",
        "Dropped as unpriceable": f"{q['unpriced_contracts']:,} ({ui.share(q['unpriced_oi_share'])} of OI)",
        "OI by vol source": ", ".join(f"{k} {ui.share(v)}" for k, v in q["oi_share_by_iv_source"].items()),
        "Expiries, forward interpolated": f"{q['series_forward_interpolated']} of {q['series']}",
        "Gamma / vendor, median [p5, p95]": dist(q["gamma_vs_vendor"]),
        "IV − vendor, vol pts": dist(q["iv_minus_vendor_volpts"], 2),
    }, alt="Data quality checks")


def strike_table(p: dict, symbol: str) -> None:
    """Exposures by strike, highest strike first, levels tagged."""
    k, s, spot = pd.DataFrame(p["strikes"]), p["summary"], p["spot"]
    tags = {s["call_wall"]: "call wall", s["put_wall"]: "put wall", s["abs_gamma_strike"]: "max |γ|"}
    table = pd.DataFrame({
        "Strike": k["strike"], "Level": k["strike"].map(lambda x: tags.get(x, "")),
        "vs spot %": 100 * (k["strike"] / spot - 1),
        "Call GEX": k["call_gex"] / 1e6, "Put GEX": k["put_gex"] / 1e6, "Net GEX": k["net_gex"] / 1e6,
        "Call OI": k["call_oi"], "Put OI": k["put_oi"], "Volume": k["call_volume"] + k["put_volume"],
        "VEX": k["vex"] / 1e6, "CHEX": k["chex"] / 1e6,
    }).sort_values("Strike", ascending=False)
    ui.table(table, {"Strike": "%.1f", "vs spot %": "%+.2f",
                     **dict.fromkeys(("Call GEX", "Put GEX", "Net GEX", "VEX", "CHEX"), "%+.1f"),
                     **dict.fromkeys(("Call OI", "Put OI", "Volume"), "%,.0f")},
             alt=f"{symbol} exposures by strike", height=420)


def body() -> None:
    ctx = ui.context()
    p, band = ctx.view(), ctx.band()

    st.subheader(f"{ctx.symbol} dealer gamma exposure")
    kpis(p, band)

    left, right = st.columns([3, 2], gap="large")
    with left:
        bars = st.segmented_control("Bars", ("Call / put", "Net"), key="bars", default="Call / put",
                                    label_visibility="collapsed") or "Call / put"
        ui.chart(charts.strikes(p, "legs" if bars == "Call / put" else "net"), "GEX by strike")
    with right:
        ui.chart(charts.profile(p, band), "Net GEX as a function of spot, with zero gamma and its band")

    left, right = st.columns([1, 2], gap="large")
    with left:
        st.markdown("**Front expiry**")
        front_table(p["front"])
    with right:
        ui.chart(charts.projection(ctx.projection(), p["spot"]),
                 "Front-expiry GEX by strike at later times of its expiry day")

    left, right = st.columns([1, 2], gap="large")
    with left:
        st.markdown("**Data quality**")
        quality_table(p["quality"])
    with right:
        st.markdown("**Strikes** · GEX, VEX, CHEX in $mn")
        strike_table(p, ctx.symbol)


body()
