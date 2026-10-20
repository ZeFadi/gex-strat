"""GEX monitor (Streamlit). Entry point: shared sidebar controls, then the selected page.

    .venv/bin/python -m streamlit run app.py

The sidebar sets the underlying, the expiry view and the strike window; every page reads
them through `ui.context()`.
"""

from __future__ import annotations

import streamlit as st

import ui
from gex_engine import config

st.set_page_config(page_title="GEX monitor", page_icon=":material/monitoring:", layout="wide")

with st.sidebar:
    symbol = st.segmented_control("Underlying", config.SYMBOLS, key="symbol", default="SPX") or "SPX"
    market, _ = ui.state(symbol)
    if market is not None:
        options = ui.expiry_options(market)
        if st.session_state.get("expiry") not in options:    # first run, or the date has settled
            st.session_state["expiry"] = "all"
        st.selectbox("Expiry", options, key="expiry", format_func=lambda v: ui.expiry_label(v, options))
        st.selectbox("Strike window", (0.03, 0.06, 0.10, 0.20), index=1, key="window",
                     format_func=lambda w: f"±{100 * w:.0f}% of spot")
        st.caption(f"CBOE delayed quotes · as of {market.asof:%a %d %b %H:%M:%S} ET  \n"
                   "Convention: dealers long calls, short puts.")

st.navigation([
    st.Page("app_pages/monitor.py", title="Monitor", icon=":material/monitoring:", default=True),
    st.Page("app_pages/scenarios.py", title="Scenarios", icon=":material/tune:"),
    st.Page("app_pages/study.py", title="Study", icon=":material/query_stats:"),
    st.Page("app_pages/flow.py", title="Flow", icon=":material/swap_vert:"),
    st.Page("app_pages/backtest.py", title="Backtest", icon=":material/science:"),
]).run()
