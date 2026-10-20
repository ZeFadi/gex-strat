"""Plotly figures of the engine's outputs.

Visual rules: every surface painted (figures look the same in a light or dark host and
paste into a slide as they are), thin grey grid, left-aligned title with a muted subtitle,
one job per colour. Blue is positive gamma and the call leg, red negative gamma and the put
leg, ink the net curve and the reference lines; never green. The blue/red pair stays
distinguishable under colour-vision deficiency.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
POS = "#2a78d6"
NEG = "#e34948"
TITLE_TOP_PX = 12
FONT = "system-ui, -apple-system, 'Segoe UI', Arial, sans-serif"


def _layout(fig: go.Figure, title: str, subtitle: str, height: int) -> go.Figure:
    """Common layout: painted surfaces, grey grid, left title with a muted subtitle."""
    fig.update_layout(
        # Title pinned at a fixed pixel distance from the top,
        # so a short figure never clips it and a tall one never lets it drift onto the plot.
        title=dict(text=f"{title}<br><span style='font-size:12px;color:{MUTED}'>{subtitle}</span>",
                   x=0.0, xanchor="left", xref="container", y=1.0 - TITLE_TOP_PX / height, yref="container",
                   yanchor="top", font=dict(size=14, color=INK), pad=dict(l=4)),
        height=height, margin=dict(l=56, r=16, t=96, b=40),
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        font=dict(family=FONT, size=12, color=INK_SECONDARY),
        showlegend=False, bargap=0.15, hovermode="closest",
        hoverlabel=dict(bgcolor="#ffffff", bordercolor=AXIS, font=dict(family=FONT, color=INK, size=12)),
        legend=dict(orientation="h", x=0, y=-0.14, font=dict(color=INK_SECONDARY)),
    )
    fig.update_xaxes(gridcolor=GRID, linecolor=AXIS, zeroline=False, ticks="outside", tickcolor=AXIS,
                     tickformat=",~r")
    fig.update_yaxes(gridcolor=GRID, linecolor=AXIS, zerolinecolor=AXIS, zerolinewidth=1, tickformat=",.1f")
    return fig


def _vline(fig: go.Figure, x: float | None, dash: str, label: str, y: float = 1.0) -> None:
    """Full-height vertical reference line at `x`, with an optional label above the plot."""
    if x is None:
        return
    fig.add_shape(type="line", xref="x", yref="paper", x0=x, x1=x, y0=0, y1=1,
                  line=dict(color=INK, width=1, dash=dash))
    if label:
        fig.add_annotation(x=x, y=y, xref="x", yref="paper", yanchor="bottom", text=label,
                           showarrow=False, font=dict(size=11, color=INK), bgcolor=SURFACE)


def _references(fig: go.Figure, p: dict, xmin: float, xmax: float) -> None:
    """Spot (solid) and zero gamma (dashed, only if inside the x range)."""
    _vline(fig, p["spot"], "solid", f"Spot {p['spot']:,.2f}")
    zg = p["summary"]["zero_gamma"]
    if zg is not None and xmin <= zg <= xmax:
        _vline(fig, zg, "dash", f"Zero γ {zg:,.2f}", y=1.075)


def _by_sign(values) -> list[str]:
    """Bar colours: blue for positive values, red for negative ones."""
    return [POS if v >= 0 else NEG for v in values]


def _bn(values) -> list[float]:
    """Dollars to billions of dollars."""
    return [v / 1e9 for v in values]


def strikes(p: dict, view: str = "legs", height: int = 420) -> go.Figure:
    """GEX by strike: call and put legs stacked around zero, or the net bar coloured by sign."""
    k, s = p["strikes"], p["summary"]
    fig = go.Figure()
    hover = "Strike %{x}<br>%{fullData.name} %{y:,.3f} $bn<extra></extra>"
    if view == "legs":
        fig.add_bar(x=k["strike"], y=_bn(k["call_gex"]), name="Calls (dealers long: +)", marker_color=POS,
                    hovertemplate=hover)
        fig.add_bar(x=k["strike"], y=_bn(k["put_gex"]), name="Puts (dealers short: −)", marker_color=NEG,
                    hovertemplate=hover)
    else:
        fig.add_bar(x=k["strike"], y=_bn(k["net_gex"]), name="Net GEX", hovertemplate=hover,
                    marker_color=_by_sign(k["net_gex"]))
    _layout(fig, "GEX by strike", "$bn of dealer delta per 1% move · spot solid, zero gamma dashed"
            + ("" if view == "legs" else " · blue: net positive, red: net negative"), height)
    if view == "legs":                    # after _layout, which hides legends by default
        fig.update_layout(showlegend=True, barmode="relative")
    if not k["strike"]:
        return fig
    xmin, xmax = min(k["strike"]), max(k["strike"])
    pad = 0.01 * (xmax - xmin)
    fig.update_xaxes(range=[xmin - pad, xmax + pad])
    _references(fig, p, xmin, xmax)
    for level, label, leg in ((s["call_wall"], "Call wall", "call_gex"), (s["put_wall"], "Put wall", "put_gex")):
        if level is None or not xmin <= level <= xmax:
            continue
        col = leg if view == "legs" else "net_gex"
        y = k[col][k["strike"].index(level)] / 1e9 if level in k["strike"] else 0.0
        fig.add_annotation(x=level, y=y, text=label, showarrow=False, yanchor="bottom" if y >= 0 else "top",
                           font=dict(size=11, color=INK))
    return fig


def profile(p: dict, band: dict | None = None, height: int = 420) -> go.Figure:
    """Net GEX re-marked at each spot (sticky strike, time frozen), with every root marked.

    `band` (exposures.zero_gamma_band) is shaded: the range of zero gamma across assumptions.
    """
    pr, s = p["profile"], p["summary"]
    fig = go.Figure()
    if band and band.get("low") is not None:
        fig.add_vrect(x0=band["low"], x1=band["high"], fillcolor=GRID, opacity=0.6, line_width=0, layer="below")
    fig.add_scatter(x=pr["spot"], y=_bn(pr["net_gex"]), mode="lines", line=dict(color=INK, width=2),
                    hovertemplate="Spot %{x:,.2f}<br>Net GEX %{y:,.2f} $bn<extra></extra>")
    if s["zero_gamma_roots"]:
        fig.add_scatter(x=s["zero_gamma_roots"], y=[0.0] * len(s["zero_gamma_roots"]), mode="markers",
                        marker=dict(size=9, color=SURFACE, line=dict(color=INK, width=2)),
                        hovertemplate="Zero gamma %{x:,.2f}<extra></extra>")
    _layout(fig, "GEX profile", "$bn per 1% · re-marked at each spot, sticky strike, time frozen", height)
    xmin, xmax = pr["spot"][0], pr["spot"][-1]
    fig.update_xaxes(range=[xmin, xmax])
    _references(fig, p, xmin, xmax)
    return fig


def signed_by_strike(p: dict, col: str, title: str, subtitle: str, height: int = 300) -> go.Figure:
    """One measure by strike, bar coloured by sign (VEX, CHEX)."""
    k = p["strikes"]
    fig = go.Figure()
    fig.add_bar(x=k["strike"], y=_bn(k[col]), marker_color=_by_sign(k[col]),
                hovertemplate="Strike %{x}<br>%{y:,.3f} $bn<extra></extra>")
    _layout(fig, title, f"{subtitle} · blue positive, red negative · line: spot", height)
    if k["strike"]:
        fig.update_xaxes(range=[min(k["strike"]), max(k["strike"])])
        _vline(fig, p["spot"], "solid", "")
    return fig


# Sequential ramp, one hue light -> dark, for ordered series (clock times, dates).
RAMP = ["#c3c2b7", "#a3a29a", "#898781", "#6b6a66", "#52514e", "#2e2d2b", "#0b0b0b"]


def projection(proj: dict, spot: float, height: int = 360) -> go.Figure:
    """Front-expiry GEX by strike now and at later clock times of its expiry day."""
    fig = go.Figure()
    series = proj["series"]
    ramp = RAMP[-len(series):] if len(series) <= len(RAMP) else RAMP
    for i, sr in enumerate(series):
        fig.add_scatter(x=proj["strikes"], y=_bn(sr["net_gex"]), mode="lines", name=sr["label"],
                        line=dict(color=ramp[min(i, len(ramp) - 1)], width=2 if i == len(series) - 1 else 1.5),
                        hovertemplate=f"{sr['label']}<br>Strike %{{x}}<br>%{{y:,.2f}} $bn<extra></extra>")
    _layout(fig, f"Front expiry {proj['expiry']}: GEX by strike through its last day",
            "$bn per 1% · spot and vols held, time running to settlement · darker = later", height)
    fig.update_layout(showlegend=True)
    _vline(fig, spot, "solid", "")
    return fig


def flow(table, spot: float, height: int = 400) -> go.Figure:
    """Structural (OI convention), flow (session trades) and combined GEX by strike."""
    fig = go.Figure()
    x = table["strike"].tolist()
    fig.add_bar(x=x, y=_bn(table["structural_gex"]), name="Structural (OI)", marker_color=AXIS,
                hovertemplate="Strike %{x}<br>Structural %{y:,.3f} $bn<extra></extra>")
    fig.add_bar(x=x, y=_bn(table["flow_gex"]), name="Flow (session)",
                marker_color=_by_sign(table["flow_gex"]),
                hovertemplate="Strike %{x}<br>Flow %{y:,.3f} $bn<extra></extra>")
    fig.add_scatter(x=x, y=_bn(table["combined_gex"]), mode="lines", name="Combined", line=dict(color=INK, width=2),
                    hovertemplate="Strike %{x}<br>Combined %{y:,.3f} $bn<extra></extra>")
    _layout(fig, "Structural vs flow-signed GEX by strike",
            "$bn per 1% · grey: OI with the dealer convention · bars: dealer positions from today's signed trades",
            height)
    fig.update_layout(showlegend=True, barmode="overlay")
    _vline(fig, spot, "solid", "")
    return fig


def history(hist, height: int = 400) -> go.Figure:
    """Spot at each close, coloured by its gamma regime, against zero gamma and its band.

    Time axis in dates (a long history stays legible). The band is drawn as a bar on each
    zero-gamma point: a shaded area would bridge the days without a zero gamma.
    """
    h = hist.assign(date=pd.to_datetime(hist["date"]))
    zg = h["zero_gamma"]
    fig = go.Figure()
    fig.add_scatter(x=h["date"], y=zg, mode="lines+markers", name="Zero gamma (bar: range across assumptions)",
                    line=dict(color=MUTED, width=1, dash="dash"), marker=dict(size=4, color=MUTED),
                    error_y=dict(type="data", symmetric=False, array=h["zero_gamma_high"] - zg,
                                 arrayminus=zg - h["zero_gamma_low"], color=AXIS, thickness=1.5, width=0),
                    hovertemplate="%{x|%d %b %Y}<br>zero gamma %{y:,.0f}<extra></extra>")
    fig.add_scatter(x=h["date"], y=h["spot"], mode="lines", line=dict(color=INK, width=1.5), name="Spot",
                    hoverinfo="skip")
    for sign, name, color in ((1, "Close in positive gamma", POS), (-1, "Close in negative gamma", NEG)):
        sub = h.loc[np.sign(h["net_gex"]) == sign]
        fig.add_scatter(x=sub["date"], y=sub["spot"], mode="markers", name=name,
                        marker=dict(size=6, color=color, line=dict(color=SURFACE, width=1)),
                        customdata=sub["net_gex"] / 1e9,
                        hovertemplate="%{x|%d %b %Y}<br>spot %{y:,.0f}<br>net GEX %{customdata:+.1f} $bn<extra></extra>")
    _layout(fig, "Spot and zero gamma at each close",
            "a break in the dashed line: no zero gamma within ±6% of spot that day", height)
    fig.update_layout(showlegend=True, margin=dict(b=72), legend=dict(y=-0.2))
    fig.update_xaxes(type="date", tickformat="%d %b %y", nticks=10)
    fig.update_yaxes(tickformat=",.0f")
    return fig


def study_scatter(d, y: str = "next_rv_gk", measure: str = "Garman-Klass", height: int = 400) -> go.Figure:
    """Next-day realized vol against today's net GEX z-score, coloured by regime."""
    fig = go.Figure()
    for flag, name, color in ((1.0, "Positive gamma at t", POS), (-1.0, "Negative gamma at t", NEG),
                              (0.0, "Indeterminate at t", MUTED)):
        sub = d.loc[d["regime"] == flag]
        if sub.empty:
            continue
        fig.add_scatter(x=sub["gex_z"], y=sub[y], mode="markers", name=name,
                        marker=dict(size=8, color=color, line=dict(color=SURFACE, width=1)),
                        customdata=sub["date"],
                        hovertemplate="%{customdata}<br>GEX z %{x:.2f}<br>next-day vol %{y:.1f}%<extra></extra>")
    _layout(fig, "Next-day realized vol against today's net GEX",
            f"{measure} vol of t+1, annualized % · net GEX of t as a z-score of its own past (no look-ahead)", height)
    fig.update_layout(showlegend=True, margin=dict(b=88), legend=dict(y=-0.24))
    fig.update_xaxes(tickformat=".1f", title_text="net GEX z-score at the close of t (left: lower gamma)")
    fig.update_yaxes(title_text="realized vol of t+1, %")
    return fig


# Trials in the two categorical colours, benchmarks in ink and grey, dashed: identity is
# carried by both colour and line style.
SERIES_STYLE = {
    "gex_sign": dict(color=POS, width=2),
    "gex_low": dict(color="#eb6834", width=2),
    "always": dict(color=INK, width=1.5, dash="dash"),
    "vix_filter": dict(color=MUTED, width=1.5, dash="dot"),
}


def equity(curves: dict, title: str, subtitle: str, oos_start: str | None = None, height: int = 380) -> go.Figure:
    """Cumulative (summed) net return of several strategies, in % of capital."""
    fig = go.Figure()
    for label, (regime, series) in curves.items():
        cum = 100.0 * series.cumsum()
        fig.add_scatter(x=cum.index, y=cum, mode="lines", name=label, line=SERIES_STYLE.get(regime, dict(color=INK)),
                        hovertemplate=f"{label}<br>%{{x|%Y-%m-%d}}: %{{y:.1f}}%<extra></extra>")
    _layout(fig, title, subtitle, height)
    fig.update_layout(showlegend=True, hovermode="x unified")
    fig.update_xaxes(tickformat="%Y")
    fig.update_yaxes(tickformat=",.0f", ticksuffix="%")
    if oos_start:
        fig.add_vline(x=oos_start, line=dict(color=MUTED, width=1, dash="dot"))
        fig.add_annotation(x=oos_start, y=1.0, yref="paper", text="out-of-sample", showarrow=False,
                           xanchor="left", yanchor="bottom", font=dict(size=11, color=INK_SECONDARY))
    return fig
