"""Study: does the GEX regime of day t say anything about day t+1? On our own histories."""

import pandas as pd
import streamlit as st

import ui
from gex_engine import backtest, charts, config, study

# Our two histories, same engine and same row: the daily CBOE archive (closes since
# 2026-10-12, with the day's open, high and low) and the end-of-day vendor files (the free
# 2022 H2 sample today, the paid archive later; closes only).
SOURCES = {"CBOE archive": study.HISTORY_FILE, "End-of-day vendor": study.EOD_HISTORY_FILE}

ctx = ui.context()
st.subheader(f"{ctx.symbol} regime study")
source = st.segmented_control("History", list(SOURCES), default="CBOE archive", key="study_source") or "CBOE archive"
st.caption("Does the gamma regime at the close of t predict day t+1 (realized vol, range, wall behaviour), "
           "beyond what implied and realized vol already say? Only consecutive sessions are paired. "
           "Regression: OLS with Newey-West errors. Method: METHODOLOGY.md, section 10."
           + (" End-of-day vendor files carry closes only: vol is close-to-close, range and walls are not "
              "measured." if source != "CBOE archive" else ""))

hist = ui.history_table(SOURCES[source])
if hist.empty:
    if source == "CBOE archive":
        st.info("No history yet. Archive closes with `python cli.py archive`, or schedule it with "
                "`python cli.py install` (README, Command line).", icon=":material/info:")
    else:
        st.info("No end-of-day history yet: `python cli.py eod-sample`, then `python cli.py eod-import`. "
                "With the paid archive, put its daily files in data/vendor/historicaldata/day_by_date and run "
                "`python cli.py eod-import` again (README, Data).", icon=":material/info:")
    st.stop()

own = hist.loc[hist["symbol"] == ctx.symbol]
if own.empty:
    st.info(f"No {ctx.symbol} in this history ({', '.join(sorted(hist['symbol'].unique()))}).", icon=":material/info:")
    st.stop()
res = study.run(hist, ctx.symbol)
with st.container(horizontal=True):
    ui.kpi("Closes", f"{len(own)}", f"{own['date'].min()} to {own['date'].max()}")
    ui.kpi("Usable day pairs", f"{res['days']}", f"statistics from {study.MIN_DAYS}")
ui.chart(charts.history(own), "Spot and zero gamma at each close")

if ctx.symbol == "SPX" and (config.DATA_DIR / backtest.SQUEEZE_FILE).exists():
    agree = backtest.compare_gex(own.set_index(pd.to_datetime(own["date"]))["net_gex"], backtest.gex_series())
    if agree["sessions"]:
        st.markdown("**Our net GEX against SqueezeMetrics'** (theirs drives the backtest)")
        ui.kv({"Common sessions": f"{agree['sessions']} ({agree['first']:%Y-%m-%d} to {agree['last']:%Y-%m-%d})",
               "Same sign": f"{agree['same_sign']:.0%}",
               "Negative sessions": f"ours {agree['negative_ours']:.0%} · theirs {agree['negative_theirs']:.0%}",
               "Correlation of levels": f"{agree['corr_levels']:.2f}",
               "Rank correlation": f"{agree['corr_ranks']:.2f}",
               "Correlation of daily changes": f"{agree['corr_daily_changes']:.2f}"}, alt="GEX agreement")
        st.caption("Different methods and units: only sign, ranks and co-movement are comparable. A high agreement "
                   "lets the backtest's conclusions carry over to our engine; a low one means they test a different "
                   "quantity.")

if not res["enough"]:
    st.info(f"{res['days']} consecutive day pairs so far; the statistics need {study.MIN_DAYS}. The archive job "
            "adds one per trading day.", icon=":material/hourglass_top:")
    st.stop()

st.markdown("**Next day, positive vs negative gamma**")
ui.table(res["means"], dict.fromkeys(("positive_gamma", "negative_gamma", "indeterminate", "difference", "welch_t"), "%.2f"),
         alt="Next-day outcomes by regime")
st.markdown(f"**Next-day realized vol ({res['measure']}) on today's features, Newey-West t-stats**")
if res["regression"] is None:
    st.caption("Not enough complete rows for the regression.")
else:
    ui.table(res["regression"], dict.fromkeys(("coef", "se_nw", "t", "r2"), "%.3f"),
             alt="Regression of next-day realized vol")
if res["walls"]["days"].sum():
    st.markdown("**Walls of t on day t+1**")
    ui.table(res["walls"], {"held_when_touched": "percent"}, alt="Wall touches and holds")
outcome = "next_rv_gk" if res["measure"] == "Garman-Klass" else "next_rv_cc"
ui.chart(charts.study_scatter(res["dataset"], outcome, res["measure"]), "Next-day realized vol against net GEX z-score")
