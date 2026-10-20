"""Study: does the GEX regime of day t say anything about day t+1? On our own histories."""

import pandas as pd
import streamlit as st

import ui
from gex_engine import backtest, charts, config, study

# Our two histories, same engine and same row: the daily CBOE archive (closes since
# 2026-10-12, with the day's open, high and low) and the end-of-day vendor files (the free
# 2022 H2 sample today, the paid archive later; closes only).
SOURCES = {"CBOE archive": study.HISTORY_FILE, "End-of-day vendor": study.EOD_HISTORY_FILE}

OUTCOMES = {"next_rv_gk": "Vol of t+1, Garman-Klass (%)", "next_rv_cc": "Vol of t+1, close-to-close (%)",
            "next_range_pct": "High-low range of t+1 (% of close)", "next_ret_pct": "Return of t+1 (%)"}
MEANS_COLUMNS = {"outcome": "Outcome", "positive_gamma": "Positive γ", "n_positive": "n (positive)",
                 "negative_gamma": "Negative γ", "n_negative": "n (negative)", "indeterminate": "Indeterminate",
                 "n_indeterminate": "n (indet.)", "difference": "Positive − negative", "welch_t": "Welch t"}
TERMS = {"const": "Constant", "gex_z": "Net GEX (z-score of its past)",
         "zero_gamma_sigmas": "Zero gamma minus spot (1-day σ)", "iv_next": "Implied vol of t+1 (%)",
         "rv_today": "Realized vol of t (%)", "rv_today_cc": "Realized vol of t (%)",
         "front_share": "Front expiry share of |GEX|"}

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
    ui.kpi("Closes in negative gamma", f"{(own['net_gex'] < 0).mean():.0%}", "net GEX below zero")
ui.chart(charts.history(own), "Spot and zero gamma at each close")

if ctx.symbol == "SPX" and (config.DATA_DIR / backtest.SQUEEZE_FILE).exists():
    agree = backtest.compare_gex(own.set_index(pd.to_datetime(own["date"]))["net_gex"], backtest.gex_series())
    if agree["sessions"] >= 20:                 # fewer common sessions say nothing about agreement
        with st.expander(f"Our net GEX against SqueezeMetrics' (theirs drives the backtest): same sign on "
                         f"{agree['same_sign']:.0%} of sessions, rank correlation {agree['corr_ranks']:.2f}"):
            ui.kv({"Common sessions": f"{agree['sessions']} ({agree['first']:%Y-%m-%d} to {agree['last']:%Y-%m-%d})",
                   "Same sign": f"{agree['same_sign']:.0%}",
                   "Negative sessions": f"ours {agree['negative_ours']:.0%} · theirs {agree['negative_theirs']:.0%}",
                   "Correlation of levels": f"{agree['corr_levels']:.2f}",
                   "Rank correlation": f"{agree['corr_ranks']:.2f}",
                   "Correlation of daily changes": f"{agree['corr_daily_changes']:.2f}"}, alt="GEX agreement")
            st.caption("Different methods and units: only sign, ranks and co-movement compare. High rank and change "
                       "correlations with a lower sign agreement mean the same book with a shifted zero: conclusions "
                       "on GEX ranks carry over to our engine, conclusions on its sign do not.")

if not res["enough"]:
    st.info(f"{res['days']} consecutive day pairs so far; the statistics need {study.MIN_DAYS}. The archive job "
            "adds one per trading day.", icon=":material/hourglass_top:")
    st.stop()

# ---------------------------------------------------------------- next day by regime
gk = res["measure"] == "Garman-Klass"
vol_outcome = "next_rv_gk" if gk else "next_rv_cc"
means = res["means"].set_index("outcome")
v = means.loc[vol_outcome]
significant = abs(v["welch_t"]) >= 1.96
st.markdown("**Next day, after a positive or a negative gamma close**")
st.markdown(f"Realized vol of t+1 averaged **{v['negative_gamma']:.1f}%** after a negative-gamma close and "
            f"**{v['positive_gamma']:.1f}%** after a positive one ({v['negative_gamma'] - v['positive_gamma']:+.1f} pts, "
            f"Welch t {v['welch_t']:+.2f}: {'significant' if significant else 'not significant'} at 5%).")
shown = means.loc[(means["n_positive"] + means["n_negative"]) > 0].reset_index()
shown["outcome"] = shown["outcome"].map(OUTCOMES)
shown["indeterminate"] = shown["indeterminate"].map(lambda x: "-" if pd.isna(x) else f"{x:.2f}")   # often no such day
ui.table(shown.rename(columns=MEANS_COLUMNS)[list(MEANS_COLUMNS.values())],
         {**dict.fromkeys(("Positive γ", "Negative γ", "Positive − negative"), "%.2f"),
          "Welch t": "%+.2f"}, alt="Next-day outcomes by regime")
st.caption("Mean of each outcome on day t+1, grouped by the regime at the close of t. n = number of day pairs. "
           "Indeterminate: the zero-gamma band contained spot, kept out of the comparison. Welch t: difference "
           "over its standard error; beyond ±2 it is unlikely to be chance.")

# ---------------------------------------------------------------- regression
st.markdown(f"**What predicts next-day vol** ({res['measure']}), all features at once")
reg = res["regression"]
if reg is None:
    st.caption("Not enough complete rows for the regression.")
else:
    table = reg.assign(term=reg["term"].map(TERMS))[["term", "coef", "se_nw", "t"]]
    ui.table(table.rename(columns={"term": "Feature at the close of t", "coef": "Effect on vol of t+1 (pts)",
                                   "se_nw": "Std error", "t": "t"}),
             {"Effect on vol of t+1 (pts)": "%+.2f", "Std error": "%.2f", "t": "%+.2f"},
             alt="Regression of next-day realized vol")
    r = reg.iloc[0]
    st.caption(f"OLS on {int(r['n'])} day pairs, R² {r['r2']:.2f}, Newey-West errors ({int(r['lags'])} lags). "
               "Each effect holds the others fixed: a negative net GEX effect means lower gamma, higher vol the next "
               "day, beyond what implied and realized vol already say. |t| above 2: unlikely to be chance. Pairs "
               "before 20 closes of z-score history are left out. The distance to zero gamma is not added: it moves "
               "with net GEX (correlation about -0.9), and the two together give unstable coefficients."
               + (f" Dropped (constant): {r['dropped']}." if r["dropped"] else ""))

# ---------------------------------------------------------------- walls
if res["walls"]["days"].sum():
    st.markdown("**Walls of t on day t+1**")
    walls = res["walls"].rename(columns={"wall": "Wall", "days": "Day pairs", "touched": "Touched on t+1",
                                         "held_when_touched": "Held when touched"})
    ui.table(walls, {"Held when touched": "percent"}, alt="Wall touches and holds")
    st.caption("Touched: the high (call wall) or low (put wall) of t+1 reached the wall of t. Held: the close of t+1 "
               "stayed on the near side of it.")

ui.chart(charts.study_scatter(res["dataset"], vol_outcome, res["measure"]), "Next-day realized vol against net GEX z-score")
