"""GEX monitor (Streamlit). Entry point: shared sidebar controls, then the selected page.

    .venv/bin/python -m streamlit run app.py

The sidebar sets the underlying, the expiry view and the strike window; every page reads
them through `ui.context()`.
"""

from __future__ import annotations

from datetime import date, datetime, time

import streamlit as st

import ui
from gex_engine import config


def _close_local() -> datetime:
    """Today's options close (16:15 New York) on this machine's clock."""
    return datetime.combine(date.today(), time(*config.OPTIONS_CLOSE), tzinfo=config.MARKET_TZ).astimezone()


st.set_page_config(page_title="GEX monitor", page_icon=":material/monitoring:", layout="wide")

# A link can open the app on a given underlying and archived close, so a reading can be
# reproduced: http://localhost:8501/study?symbol=SPX&close=2026-10-16
params = st.query_params
if "symbol" not in st.session_state:
    st.session_state["symbol"] = params.get("symbol") if params.get("symbol") in config.SYMBOLS else "SPX"

with st.sidebar:
    symbol = st.segmented_control("Underlying", config.SYMBOLS, key="symbol") or "SPX"
    closes = ui.archived_closes(symbol)
    if "close" not in st.session_state:
        st.session_state["close"] = params.get("close") if params.get("close") in closes else ui.LIVE
    if st.session_state["close"] not in [ui.LIVE, *closes]:
        st.session_state["close"] = ui.LIVE
    close = st.selectbox("Data", [ui.LIVE, *closes], key="close",
                         format_func=lambda c: "Live (CBOE delayed)" if c == ui.LIVE
                         else f"Archived close {date.fromisoformat(c):%a %d %b %Y}")
    market, _ = ui.state(symbol, close)
    if market is not None:
        options = ui.expiry_options(market)
        if st.session_state.get("expiry") not in options:    # first run, or the date has settled
            st.session_state["expiry"] = "all"
        st.selectbox("Expiry", options, key="expiry", format_func=lambda v: ui.expiry_label(v, options))
        st.selectbox("Strike window", (0.03, 0.06, 0.10, 0.20), index=1, key="window",
                     format_func=lambda w: f"±{100 * w:.0f}% of spot")
        st.caption(("CBOE delayed quotes" if close == ui.LIVE else "Archived CBOE closing file")
                   + f" · as of {market.asof:%a %d %b %H:%M:%S} ET  \n"
                   f"The close is final from 16:15 ET ({_close_local():%H:%M} on this machine's clock).  \n"
                   "Convention: dealers long calls, short puts.")
    if close == ui.LIVE and st.button("Refresh data", icon=":material/refresh:",
                                      help="Download the current CBOE file now"):
        ui.refresh()
        st.rerun()

st.navigation([
    st.Page("app_pages/monitor.py", title="Monitor", icon=":material/monitoring:", default=True),
    st.Page("app_pages/scenarios.py", title="Scenarios", icon=":material/tune:"),
    st.Page("app_pages/study.py", title="Study", icon=":material/query_stats:"),
    st.Page("app_pages/flow.py", title="Flow", icon=":material/swap_vert:"),
    st.Page("app_pages/backtest.py", title="Backtest", icon=":material/science:"),
]).run()
