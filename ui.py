"""What the pages share: the page context, cached data access, formatting and display helpers.

Caching: one priced market state per symbol per minute (`state`), and every derived view
keyed on (symbol, as-of, view) with the state itself left unhashed, so a click never
re-downloads and a new file always recomputes. If a refresh fails, the last good state is
served and the error travels with it to every page.

Pages only format. Every number they show comes from gex_engine through `Context`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd
import streamlit as st

from gex_engine import backtest, config, exposures, snapshot, study, surface
from gex_engine import data


# --------------------------------------------------------------------------- data

@st.cache_resource
def _last_good() -> dict:
    """Last successfully built state per symbol, shared across sessions."""
    return {}


@st.cache_data(ttl=config.REFRESH_SECONDS, show_spinner=False)
def _fetch_state(symbol: str) -> surface.MarketState:
    """Download and price one symbol (cached for REFRESH_SECONDS)."""
    return surface.build(data.fetch(symbol))


def state(symbol: str) -> tuple[surface.MarketState | None, str | None]:
    """(market state, error): the fresh state, or the last good one and why the refresh failed."""
    try:
        s = _fetch_state(symbol)
        _last_good()[symbol] = s
        return s, None
    except Exception as exc:                     # provider or data failure
        return _last_good().get(symbol), f"{type(exc).__name__}: {exc}"


def expiry_options(market: surface.MarketState) -> list[str]:
    """Expiry views of a state: 'all', 'front', then each live date."""
    return ["all", "front"] + [str(e) for e in exposures.expiries(market)]


def expiry_label(value: str, options: list[str]) -> str:
    """Display name of an expiry view."""
    if value == "all":
        return "All expiries"
    if value == "front":
        return f"Front ({options[2]})"
    return f"{value} {date.fromisoformat(value):%a}"


# The cached views below take the state as `_market` (leading underscore: Streamlit does not
# hash it) and are keyed on `symbol` and `asof` instead. Those two arguments look unused but
# ARE the cache key: a new file (new as-of) recomputes, a click on the same file does not.

@st.cache_data(max_entries=64, show_spinner=False)
def _view(_market, symbol: str, asof: str, expiry: str, window: float) -> dict:
    return snapshot.build(_market, expiry, window)


@st.cache_data(max_entries=32, show_spinner="Testing zero gamma under alternative assumptions...")
def _band(_market, symbol: str, asof: str, expiry: str) -> dict:
    return exposures.zero_gamma_band(exposures.select(_market, expiry), _market.spot, _market.rate)


@st.cache_data(max_entries=32, show_spinner=False)
def _scenarios(_market, symbol: str, asof: str, expiry: str) -> pd.DataFrame:
    return exposures.scenario_table(_market, exposures.select(_market, expiry))


@st.cache_data(max_entries=32, show_spinner=False)
def _projection(_market, symbol: str, asof: str) -> dict:
    return exposures.front_projection(_market)


@st.cache_data(max_entries=2, show_spinner=False)
def backtest_dataset(data_stamp: float) -> pd.DataFrame:
    """backtest.dataset on the frozen files; `data_stamp` (files' mtime) invalidates the cache."""
    return backtest.dataset()


@st.cache_data(max_entries=4, show_spinner="Running the pre-registered backtest...")
def backtest_report(cost_mult: float, data_stamp: float) -> dict:
    """backtest.report on the frozen data; `data_stamp` (files' mtime) invalidates the cache."""
    return backtest.report(backtest_dataset(data_stamp), cost_mult=cost_mult)


BACKTEST_INPUTS = (backtest.PRICES_FILE, backtest.SQUEEZE_FILE)


def backtest_missing_inputs() -> list[str]:
    """Backtest input files absent from data/ (third-party data the code does not create)."""
    return [str(f) for f in BACKTEST_INPUTS if not (config.DATA_DIR / f).exists()]


def backtest_data_stamp() -> float:
    """Last modification time of the backtest's input files (all present)."""
    return max((config.DATA_DIR / f).stat().st_mtime for f in BACKTEST_INPUTS)


@st.cache_data(ttl=300, show_spinner=False)
def history_table() -> pd.DataFrame:
    """The archived history (re-read every 5 minutes)."""
    return study.load_history()


@dataclass(frozen=True)
class Context:
    """The sidebar selection and the market state it points to. Derived views are cached."""

    symbol: str
    expiry: str
    window: float
    market: surface.MarketState
    error: str | None

    @property
    def asof(self) -> str:
        return self.market.asof.isoformat()

    def view(self) -> dict:
        """snapshot.build payload of the selected view."""
        return _view(self.market, self.symbol, self.asof, self.expiry, self.window)

    def band(self) -> dict:
        """Zero gamma under alternative assumptions (exposures.zero_gamma_band)."""
        return _band(self.market, self.symbol, self.asof, self.expiry)

    def scenarios(self) -> pd.DataFrame:
        """Dealer hedge flows by scenario (exposures.scenario_table)."""
        return _scenarios(self.market, self.symbol, self.asof, self.expiry)

    def projection(self) -> dict:
        """Front-expiry GEX projected to the close (exposures.front_projection)."""
        return _projection(self.market, self.symbol, self.asof)


def context() -> Context:
    """Read the sidebar selection, load its state, warn if stale; stop the page if no data."""
    symbol = st.session_state.get("symbol") or "SPX"
    market, error = state(symbol)
    if market is None:
        st.error(f"{symbol}: no data available. {error or ''}", icon=":material/error:")
        st.stop()
    if error:
        st.warning(f"Last refresh failed, showing the data of {market.asof:%d %b %H:%M} ET. {error}",
                   icon=":material/cloud_off:")
    stale = surface.stale_message(market.asof)
    if stale:
        st.warning(stale, icon=":material/schedule:")
    expiry = st.session_state.get("expiry")
    expiry = expiry if expiry in expiry_options(market) else "all"
    return Context(symbol, expiry, st.session_state.get("window", 0.06), market, error)


# --------------------------------------------------------------------------- formatting

def sgn(x, d: int = 2) -> str:
    """Signed number with a true minus sign, 'n/a' for None."""
    return "n/a" if x is None else ("+" if x > 0 else "−" if x < 0 else "") + f"{abs(x):,.{d}f}"


def bn(x, d: int = 2) -> str:
    """Dollars as signed billions."""
    return "n/a" if x is None else sgn(x / 1e9, d)


def px(x, d: int = 2) -> str:
    """A price level."""
    return "n/a" if x is None else f"{x:,.{d}f}"


def strike(x) -> str:
    """A strike, without decimals when it is whole."""
    return "n/a" if x is None else f"{x:,.0f}" if float(x).is_integer() else f"{x:,.1f}"


def pct(x, d: int = 2) -> str:
    """A signed percentage (x already in %)."""
    return "n/a" if x is None else sgn(x, d) + "%"


def sigmas(x) -> str:
    """A signed distance in one-day sigmas."""
    return "n/a" if x is None else sgn(x, 2) + "σ"


def share(x) -> str:
    """A fraction as a percentage."""
    return "n/a" if x is None else f"{100 * x:.1f}%"


def tone(text: str, x) -> str:
    """Blue for positive, red for negative (theme colours, see .streamlit/config.toml)."""
    return f":blue[{text}]" if x and x > 0 else f":red[{text}]" if x and x < 0 else text


# --------------------------------------------------------------------------- display

PLOT = {"displayModeBar": False}


def kpi(label: str, value: str, note: str = "", help: str | None = None) -> None:
    """A metric whose second line is a plain grey note (no arrow, no green/red delta)."""
    st.metric(label, value, delta=note or None, delta_color="off", delta_arrow="off", help=help)


def chart(fig, alt: str) -> None:
    """A Plotly figure with its own styling kept (Streamlit's theme off)."""
    st.plotly_chart(fig, theme=None, config=PLOT, alt=alt)


def kv(rows: dict, alt: str) -> None:
    """A two-column label / value table without header."""
    st.table(pd.DataFrame({"": list(rows.values())}, index=list(rows)),
             hide_header=True, border="horizontal", alt=alt)


def table(df: pd.DataFrame, formats: dict, alt: str, height: int | str = "auto") -> None:
    """A data table with printf-style number formats per column ({column: '%+.2f'})."""
    st.dataframe(df, hide_index=True, alt=alt, height=height,
                 column_config={c: st.column_config.NumberColumn(format=f) for c, f in formats.items()})
