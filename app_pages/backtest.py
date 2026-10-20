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
mech = pd.DataFrame([{"Regime": r, "Controls": "VIX, RV, HAR 5/22, leverage" if c == "har" else "VIX, RV",
                      "Extra vol pts next day": m.set_index("term").loc["negative_gamma", "coef"],
                      "t (Newey-West, 20 lags)": m.set_index("term").loc["negative_gamma", "t_nw"]}
                     for (r, c), m in rep["mechanism"].items()])
ui.table(mech, {"Extra vol pts next day": "%+.2f", "t (Newey-West, 20 lags)": "%+.2f"}, alt="Mechanism regression")
st.caption("OLS of next-session Parkinson vol (annualized %) on the negative-regime dummy and vol controls: VIX, "
           "today's vol, the 5- and 22-session mean vol (HAR) and today's return and down-day dummy (leverage). "
           "A significant GEX coefficient with an insignificant VIX-filter one means GEX carries vol information "
           "that the level and the memory of volatility do not.")

st.markdown("**Vol forecast: does GEX improve a HAR-X forecast of next-session variance, out of sample?**")
fc = ui.forecast_report(ui.backtest_data_stamp())
if fc["passed"]:
    st.success("The GEX model beats HAR-X on QLIKE (Diebold-Mariano, one-sided 5%). Next trial: a vol-targeting "
               "overlay, to be specified and committed before it is run.", icon=":material/check:")
else:
    st.warning("The GEX model does not beat HAR-X out of sample (pre-registered test: Diebold-Mariano on QLIKE, "
               "one-sided 5%). As registered, the overlay and variance-risk-premium line stops here.",
               icon=":material/block:")
FORECAST_COLUMNS = {"sample": "Sample", "n": "Sessions", "qlike_base": "Loss HAR-X", "qlike_gex": "Loss + GEX",
                    "qlike_gain_pct": "Gain %", "dm_t": "DM t (decision)", "dm_p": "p", "mse_dm_t": "DM t (MSE)",
                    "clark_west_t": "Clark-West t", "clark_west_p": "Clark-West p"}
ui.table(fc["table"].rename(columns=FORECAST_COLUMNS)[list(FORECAST_COLUMNS.values())],
         {"Loss HAR-X": "%.3f", "Loss + GEX": "%.3f", "Gain %": "%+.1f", "DM t (decision)": "%+.2f", "p": "%.3f",
          "DM t (MSE)": "%+.2f", "Clark-West t": "%+.2f", "Clark-West p": "%.3f"}, alt="Vol forecast trial")
st.caption(f"Walk-forward from {fc['table']['start'].iloc[0]:%Y-%m-%d}: refit every 21 sessions, expanding window. "
           "Loss: QLIKE, lower is better; Gain %: loss saved by adding GEX. Positive t = the GEX model is better. "
           "Decision on the first row's DM t only; MSE, Clark-West and the "
           f"recent sessions are reported, not decisive. Forecasts floored at 1 %²: {fc['floored']['base']} (HAR-X), "
           f"{fc['floored']['gex']} (GEX). {fc['n_trials']} trial(s) in data/backtest/forecast_trials.csv. "
           "METHODOLOGY section 14.")

# --------------------------------------------------------------------------- performance
st.markdown(f"**Performance** · {sample}, {'close t+1' if lag == 2 else 'close t'} execution, costs ×{cost:.0f}")
cols = {"strategy": "Strategy", "trial": "Trial", "sharpe": "Sharpe", "sharpe_se": "SE", "ci05": "CI 5%",
        "ci95": "CI 95%", "deflated_sharpe": "Deflated SR", "t_mean_nw": "t (NW)", "ann_return": "Return %",
        "max_drawdown": "Max DD %", "worst_day": "Worst day %", "turnover": "Turnover", "cost_drag": "Costs %/yr"}
shown = view[list(cols)].rename(columns=cols).copy()
for c in ("Return %", "Max DD %", "Worst day %", "Costs %/yr"):
    shown[c] = 100 * shown[c]
shown["Deflated SR"] = shown["Deflated SR"].map(lambda x: "—" if pd.isna(x) else f"{100 * x:.1f}%")   # benchmarks: none
ui.table(shown, {"Sharpe": "%.2f", "SE": "%.2f", "CI 5%": "%.2f", "CI 95%": "%.2f",
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
    ui.table(rep["placebo"].rename(columns={"strategy": "Strategy", "actual": "Sharpe", "p_better": "p better",
                                            "p_worse": "p worse", "placebo_median": "Placebo median"}),
             {"Sharpe": "%.2f", "p better": "%.2f", "p worse": "%.2f", "Placebo median": "%.2f"}, alt="Placebo test")
    st.caption("Share of time-shifted regimes doing at least as well (p better) or at most as well (p worse). "
               "Small p better: the timing adds value. Small p worse: the timing is informative but the rule is on "
               "the wrong side of it. Neither small: the timing adds nothing.")
with right:
    st.markdown("**Regime episodes** (negative-regime runs)")
    ui.table(rep["episodes"].rename(columns={"regime": "Regime", "sample": "Sample", "negative_days": "Negative days",
                                             "episodes": "Episodes", "longest": "Longest (sessions)"}),
             {}, alt="Regime episodes")
    st.caption(f"{rep['n_trials']} trials logged in data/backtest/trials.csv. Sample "
               f"{rep['start']:%Y-%m-%d} to {rep['end']:%Y-%m-%d}; out-of-sample from {config.OOS_START}.")
