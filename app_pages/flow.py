"""Flow: flow-signed GEX from a file of today's option trades."""

import io

import pandas as pd
import streamlit as st

import ui
from gex_engine import charts, flow

ctx = ui.context()
summary = ctx.view()["summary"]

st.subheader(f"{ctx.symbol} flow-signed GEX")
st.caption("Dealer positions inferred from today's signed trades (from 09:30 New York), against the structural "
           "view where dealers hold yesterday's open interest by convention. Needs a trade file with the quote "
           "in force at each trade (ThetaData, Databento or a broker export); the CBOE delayed chain cannot "
           "provide it.")

TEMPLATE = pd.DataFrame({
    "contract": ["SPXW261013C07800000", "SPXW261013C07800000", "SPXW261013P07750000"],
    "time": ["2026-10-13 09:31:02", "2026-10-13 09:31:40", "2026-10-13 09:35:11"],
    "price": [21.10, 21.40, 9.80], "size": [50, 20, 100], "bid": [20.90, 21.00, 9.70], "ask": [21.20, 21.40, 10.00],
})
left, right = st.columns([2, 1], gap="large")
with left:
    upload = st.file_uploader("Trade file (CSV or parquet)", type=["csv", "parquet"])
with right:
    method = st.segmented_control("Trade signing", flow.METHODS, default="lee_ready", key="flow_method",
                                  format_func={"lee_ready": "Lee-Ready", "emo": "EMO", "bvc": "BVC"}.get)
    st.download_button("Template CSV", TEMPLATE.to_csv(index=False), "trades_template.csv", "text/csv",
                       icon=":material/download:")

if upload is None:
    st.info(f"Columns: {', '.join(flow.TRADE_COLUMNS)}. `contract` is the OCC symbol as in the chain, "
            "`time` is New York time, `bid` / `ask` the quote in force at the trade.", icon=":material/upload_file:")
    st.stop()

try:
    trades = flow.load_trades(upload if upload.name.endswith(".parquet") else io.BytesIO(upload.getvalue()))
except ValueError as exc:
    st.error(str(exc), icon=":material/error:")
    st.stop()

inventory = flow.dealer_inventory(trades, method or "lee_ready")
res = flow.flow_by_strike(ctx.market, inventory, ctx.expiry)
volume = inventory[["customer_buy", "customer_sell", "unclassified"]].to_numpy().sum()

with st.container(horizontal=True):
    ui.kpi("Trades", f"{len(trades):,}", f"{len(inventory):,} contracts")
    ui.kpi("Unclassified volume", ui.share(inventory["unclassified"].sum() / volume if volume else None))
    ui.kpi("Flow net GEX, $bn per 1%", ui.tone(ui.bn(res["flow_net_gex"]), res["flow_net_gex"]), "session trades only")
    ui.kpi("Structural net GEX", ui.tone(ui.bn(res["structural_net_gex"]), res["structural_net_gex"]), "OI convention")

unmatched = res["unmatched_contracts"]
if unmatched:
    st.warning(f"{len(unmatched)} contracts of the file are not in the chain, ignored: "
               + ", ".join(unmatched[:5]) + ("..." if len(unmatched) > 5 else ""))

shown = res["strikes"].loc[(res["strikes"]["strike"] / ctx.market.spot - 1).abs() <= ctx.window]
ui.chart(charts.flow(shown, ctx.market.spot), "Structural, flow and combined GEX by strike")

rows = []
for name, side in (("Call wall", "call"), ("Put wall", "put")):
    w = flow.wall_pressure(res["strikes"], summary[f"{side}_wall"], summary["one_day_move"])
    if w:
        rows.append({"Wall": f"{name} {ui.strike(w['level'])}", "Structural GEX, $bn": w["structural_gex"] / 1e9,
                     "Flow GEX, $bn": w["flow_gex"] / 1e9, "Flow / |structural|": w["flow_to_structural"]})
st.markdown("**Pressure on the walls** (strikes within one 1-day σ of each wall)")
ui.table(pd.DataFrame(rows), dict.fromkeys(("Structural GEX, $bn", "Flow GEX, $bn", "Flow / |structural|"), "%+.2f"),
         alt="Structural and flow GEX around the walls")
st.caption("Flow / |structural| below −1: today's signed flow has more than offset yesterday's dealer gamma "
           "around the wall, which then no longer acts as the level the structural view shows.")
