"""Backtest: does trading the gamma regime pay, net of costs, against the same rules without it?

Pre-registered rules and inference: METHODOLOGY.md section 13. This page only formats
backtest.report.
"""

import pandas as pd
import streamlit as st

import ui
from gex_engine import backtest, charts, config

st.subheader("Strategy backtest")
missing = ui.backtest_missing_inputs()
if missing:
    st.info("The backtest needs two third-party input files that are missing here (the price file is "
            f"licensed data and is not in the repository): {', '.join('data/' + m for m in missing)}. Create them with "
            "`python cli.py backtest-data --squeeze` and `python cli.py backtest-data --prices FILE`, "
            "FILE being a daily CSV with columns date, spx_open, spx_high, spx_low, spx_close, spxt, "
            "us3m_pct, vix, ux1, ux2 (tickers in data/market/prices.csv.provenance.json).", icon=":material/info:")
    st.stop()
st.caption("Regime: SqueezeMetrics' public SPX GEX (the market's GEX, not gex_engine's; the two methodologies "
           "can disagree in sign). Rules, regimes, benchmarks, execution and costs were fixed and committed "
           "before the first run. Daily excess returns, vol-scaled to 10%, summed.")

c1, c2, c3 = st.columns(3)
lag = c1.segmented_control("Execution", config.EXECUTION_LAGS, default=config.EXECUTION_LAGS[0], key="bt_lag",
                           format_func=lambda x: "close t+1 (primary)" if x == 2 else "close t (optimistic)")
cost = c2.segmented_control("Costs", (1.0, 2.0), default=1.0, key="bt_cost", format_func=lambda x: f"×{x:.0f}")
sample = c3.segmented_control("Sample", ("full", "in-sample", "out-of-sample"), default="full", key="bt_sample")
lag, cost, sample = lag or 2, cost or 1.0, sample or "full"

rep = ui.backtest_report(cost, ui.backtest_data_stamp())
t = rep["table"]
view = t.loc[(t["lag"] == lag) & (t["sample"] == sample)]
full = t.loc[(t["lag"] == lag) & (t["sample"] == "full")].set_index("strategy")

# --------------------------------------------------------------------------- verdict
winners = []
for rule in backtest.RULES:
    bench = max(full.loc[backtest.name(rule, b), "sharpe"] for b in backtest.BENCHMARK_REGIMES)
    for r in backtest.TRIAL_REGIMES:
        row = full.loc[backtest.name(rule, r)]
        if row["sharpe"] > bench and row["deflated_sharpe"] > 0.95:
            winners.append(backtest.name(rule, r))
if winners:
    st.success(f"Beats its benchmarks with a deflated Sharpe above 95%: {', '.join(winners)}.", icon=":material/check:")
else:
    st.warning(f"No rule beats both its benchmarks (always-on, VIX filter) with a deflated Sharpe above 95% "
               f"({rep['n_trials']} trials, full sample, this execution and cost). As a trading rule, the GEX "
               "regime is not profitable here.", icon=":material/block:")

st.markdown("**Mechanism: does negative gamma predict next-day realized vol beyond VIX and today's vol?**")
mech = pd.DataFrame([{"regime": r, "controls": "VIX, RV, HAR 5/22, leverage" if c == "har" else "VIX, RV",
                      "Extra vol pts next day": m.set_index("term").loc["negative_gamma", "coef"],
                      "t (Newey-West, 20 lags)": m.set_index("term").loc["negative_gamma", "t_nw"]}
                     for (r, c), m in rep["mechanism"].items()])
ui.table(mech, {"Extra vol pts next day": "%+.2f", "t (Newey-West, 20 lags)": "%+.2f"}, alt="Mechanism regression")
st.caption("OLS of next-session Parkinson vol (annualized %) on the negative-regime dummy and vol controls: VIX, "
           "today's vol, the 5- and 22-session mean vol (HAR) and today's return and down-day dummy (leverage). "
           "A significant GEX coefficient with an insignificant VIX-filter one means GEX carries vol information "
           "that the level and the memory of volatility do not.")

# --------------------------------------------------------------------------- performance
st.markdown(f"**Performance** · {sample}, {'close t+1' if lag == 2 else 'close t'} execution, costs ×{cost:.0f}")
cols = {"strategy": "Strategy", "trial": "Trial", "sharpe": "Sharpe", "sharpe_se": "SE", "ci05": "CI 5%",
        "ci95": "CI 95%", "deflated_sharpe": "Deflated SR", "t_mean_nw": "t (NW)", "ann_return": "Return %",
        "max_drawdown": "Max DD %", "worst_day": "Worst day %", "turnover": "Turnover", "cost_drag": "Costs %/yr"}
shown = view[list(cols)].rename(columns=cols).copy()
for c in ("Return %", "Max DD %", "Worst day %", "Costs %/yr"):
    shown[c] = 100 * shown[c]
ui.table(shown, {"Sharpe": "%.2f", "SE": "%.2f", "CI 5%": "%.2f", "CI 95%": "%.2f", "Deflated SR": "percent",
                 "t (NW)": "%+.2f", "Return %": "%+.1f", "Max DD %": "%.1f", "Worst day %": "%.1f",
                 "Turnover": "%.0f", "Costs %/yr": "%.2f"}, alt="Backtest performance", height=36 * (len(shown) + 1) + 2)
st.caption("Trials: the two GEX regimes; benchmarks: always-on and VIX filter. CI = circular block bootstrap; "
           "Deflated SR = probability the true Sharpe beats the best of N lucky trials (full sample only). "
           "Turnover = notional traded per year.")

rule = st.segmented_control("Rule", list(backtest.RULES), default="exposure", key="bt_rule") or "exposure"
data = ui.backtest_dataset(ui.backtest_data_stamp())
reg = backtest.regimes(data)
curves = {r: (r, backtest.run(rule, reg[r], data, lag, cost)["net"])
          for r in backtest.TRIAL_REGIMES + backtest.BENCHMARK_REGIMES}
ui.chart(charts.equity(curves, f"{rule}: cumulative net return", "% of capital, summed daily excess returns",
                       oos_start=config.OOS_START), f"Cumulative return of the {rule} rule")

left, right = st.columns(2, gap="large")
with left:
    st.markdown("**Placebo** (regime shifted in time, primary execution)")
    ui.table(rep["placebo"], {"actual": "%.2f", "p_better": "%.2f", "p_worse": "%.2f", "placebo_median": "%.2f"},
             alt="Placebo test")
    st.caption("Share of time-shifted regimes doing at least as well (p_better) or at most as well (p_worse). "
               "Small p_better: the timing adds value. Small p_worse: the timing is informative but the rule is on "
               "the wrong side of it. Neither small: the timing adds nothing.")
with right:
    st.markdown("**Regime episodes** (negative-regime runs)")
    ui.table(rep["episodes"], {}, alt="Regime episodes")
    st.caption(f"{rep['n_trials']} trials logged in data/backtest/trials.csv. Sample "
               f"{rep['start']:%Y-%m-%d} to {rep['end']:%Y-%m-%d}; out-of-sample from {config.OOS_START}.")
