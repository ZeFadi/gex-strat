"""Study: does the GEX regime of day t say anything about day t+1? On the archived history."""

import streamlit as st

import ui
from gex_engine import charts, study

ctx = ui.context()
st.subheader(f"{ctx.symbol} regime study")
st.caption("Does the gamma regime at the close of t predict day t+1 (realized vol, range, wall behaviour), "
           "beyond what implied and realized vol already say? Only consecutive sessions are paired. "
           "Regression: OLS with Newey-West errors. Method: METHODOLOGY.md, section 10.")

hist = ui.history_table()
if hist.empty:
    st.info("No history yet. Archive closes with `python cli.py archive`, or schedule it with "
            "`python cli.py install` (README, Command line).", icon=":material/info:")
    st.stop()

own = hist.loc[hist["symbol"] == ctx.symbol]
res = study.run(hist, ctx.symbol)
with st.container(horizontal=True):
    ui.kpi("Archived closes", f"{len(own)}", f"{own['date'].min()} to {own['date'].max()}" if len(own) else "")
    ui.kpi("Usable day pairs", f"{res['days']}", f"statistics from {study.MIN_DAYS}")

if len(own):
    ui.chart(charts.history(own), "Spot and zero gamma at each archived close")

if not res["enough"]:
    st.info(f"{res['days']} consecutive day pairs so far; the statistics need {study.MIN_DAYS}. The archive job "
            "adds one per trading day.", icon=":material/hourglass_top:")
    st.stop()

st.markdown("**Next day, positive vs negative gamma**")
ui.table(res["means"], dict.fromkeys(("positive_gamma", "negative_gamma", "difference", "welch_t"), "%.2f"),
         alt="Next-day outcomes by regime")
st.markdown("**Next-day realized vol (Garman-Klass) on today's features, Newey-West t-stats**")
if res["regression"] is None:
    st.caption("Not enough complete rows for the regression.")
else:
    ui.table(res["regression"], dict.fromkeys(("coef", "se_nw", "t", "r2"), "%.3f"),
             alt="Regression of next-day realized vol")
st.markdown("**Walls of t on day t+1**")
ui.table(res["walls"], {"held_when_touched": "percent"}, alt="Wall touches and holds")
ui.chart(charts.study_scatter(res["dataset"]), "Next-day realized vol against net GEX z-score")
