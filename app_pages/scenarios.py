"""Scenarios: dealer hedge flows under shocks, and how robust the zero-gamma level is."""

import pandas as pd
import streamlit as st

import ui
from gex_engine import charts, exposures

ctx = ui.context()
p = ctx.view()

st.subheader(f"{ctx.symbol} scenarios")
st.caption("Each row re-prices the delta of every contract in the view (no Taylor expansion): sticky strike, "
           "carry held, positions per the dealer convention. Contracts that settle inside a time step leave "
           "the book (cash-settled indices) or turn into shares (ETFs). Magnitudes are upper bounds: the "
           "convention gives dealers all of the open interest, including positions between customers.")

st.markdown("**Dealer hedge flows**")
t = ctx.scenarios()
unit = f"Hedge flow, {exposures.hedge_unit_name(ctx.symbol)}"
ui.table(pd.DataFrame({
    "Shock": t["shock"], "Scenario": t["scenario"],
    "Dealers": t["hedge_flow_usd"].map(lambda x: "buy" if x > 0 else "sell" if x < 0 else ""),
    "Hedge flow, $bn": t["hedge_flow_usd"] / 1e9,
    unit: t["hedge_flow_hedge_units"],
}), {"Hedge flow, $bn": "%+.2f", unit: "%+,.0f"}, alt="Dealer hedge flows by scenario",
    height=36 * (len(t) + 1) + 2)
st.caption("What dealers must trade to stay delta-neutral if the shock happens now: + they buy, − they sell. "
           "A flow against the shock (buy after a fall) damps the move; with it, it adds to the move.")

st.markdown("**Zero gamma under alternative assumptions**")
band = ctx.band()
base = band["variants"][0]["zero_gamma"]
ui.table(pd.DataFrame({
    "Assumption": [v["variant"] for v in band["variants"]],
    "Zero gamma": [v["zero_gamma"] for v in band["variants"]],
    "vs base": [None if v["zero_gamma"] is None or base is None else v["zero_gamma"] - base
                for v in band["variants"]],
    "Net GEX at spot, $bn": [v["net_gex_at_spot"] / 1e9 for v in band["variants"]],
}), {"Zero gamma": "%,.2f", "vs base": "%+.2f", "Net GEX at spot, $bn": "%+.2f"},
    alt="Zero gamma under alternative assumptions")
if band["indeterminate"]:
    st.warning("The range contains spot: the sign of the regime depends on an assumption, not on the data.",
               icon=":material/warning:")
else:
    st.caption(f"Range {ui.px(band['low'])} to {ui.px(band['high'])}, spot {ui.px(ctx.market.spot)}: "
               "the regime does not depend on these assumptions.")

left, right = st.columns(2, gap="large")
with left:
    ui.chart(charts.signed_by_strike(p, "vex", "Vanna exposure by strike", "$bn of dealer delta per +1 vol point"),
             "Vanna exposure by strike")
with right:
    ui.chart(charts.signed_by_strike(p, "chex", "Charm exposure by strike", "$bn of dealer delta per calendar day"),
             "Charm exposure by strike")
