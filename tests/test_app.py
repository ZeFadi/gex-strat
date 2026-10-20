"""The Streamlit app end to end, offline: the provider is replaced by the frozen fixtures."""

from datetime import datetime

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from gex_engine import backtest, charts, config, data, snapshot, study, surface

from .conftest import FIXTURES, NO_FIXTURES

APP = str(FIXTURES.parent.parent / "app.py")
pytestmark = pytest.mark.skipif(not (FIXTURES / "SPX_20261012.json.gz").exists(), reason=NO_FIXTURES)
PAGES = ("app_pages/scenarios.py", "app_pages/study.py", "app_pages/flow.py", "app_pages/backtest.py")


@pytest.fixture
def offline(monkeypatch):
    calls = []

    def fetch(symbol):
        calls.append(symbol)
        path = FIXTURES / f"{symbol}_20261012.json.gz"
        if not path.exists():
            raise ConnectionError("offline")
        return data.load(path, symbol)

    monkeypatch.setattr(data, "fetch", fetch)
    monkeypatch.setattr(study, "load_history", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(backtest, "register_trials", lambda *a, **k: 0)   # never touch the real trials log
    return calls


def _run(at):
    at.run(timeout=60)
    assert not at.exception, at.exception
    return at


def test_monitor_shows_the_engine_numbers(offline):
    at = _run(AppTest.from_file(APP))
    values = {m.label: m.value for m in at.metric}
    assert values["Zero gamma"].startswith("7,752.")
    assert "+83.2" in values["Net GEX, $bn per 1%"]
    assert len(at.get("plotly_chart")) == 3


@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders(offline, page):
    at = _run(AppTest.from_file(APP))
    at.switch_page(page)
    _run(at)


def test_views_reuse_one_download(offline):
    at = _run(AppTest.from_file(APP))
    n = offline.count("SPX")
    for view in ("front", "2026-10-16", "all"):
        at.selectbox(key="expiry").set_value(view)
        _run(at)
    assert offline.count("SPX") == n


def test_symbol_without_data_shows_an_error(offline, monkeypatch):
    at = _run(AppTest.from_file(APP))
    monkeypatch.setattr(data, "fetch", lambda s: (_ for _ in ()).throw(ConnectionError("offline")))
    at.session_state["symbol"] = "QQQ"
    at.run(timeout=60)
    assert at.error and "no data available" in at.error[0].value


def test_charts_build(spx_state):
    p = snapshot.build(spx_state, "front")
    for fig in (charts.strikes(p, "legs"), charts.strikes(p, "net"), charts.profile(p),
                charts.signed_by_strike(p, "vex", "VEX", "")):
        assert fig.layout.paper_bgcolor == charts.SURFACE and len(fig.data) >= 1


def test_session_lag_flag():
    NY = config.MARKET_TZ
    asof = datetime(2026, 10, 13, 10, 0, tzinfo=NY)
    assert surface.session_lag_minutes(asof, datetime(2026, 10, 13, 11, 0, tzinfo=NY)) == 60.0
    assert surface.session_lag_minutes(asof, datetime(2026, 10, 13, 18, 0, tzinfo=NY)) is None
    assert surface.session_lag_minutes(asof, datetime(2026, 10, 17, 11, 0, tzinfo=NY)) is None
    assert "60 minutes" in surface.stale_message(asof, datetime(2026, 10, 13, 11, 0, tzinfo=NY))
    assert surface.stale_message(asof, datetime(2026, 10, 13, 10, 20, tzinfo=NY)) is None    # normal 15-20 min delay
    close = datetime(2026, 10, 12, 16, 14, tzinfo=NY)                                            # previous close
    assert surface.stale_message(close, datetime(2026, 10, 13, 9, 35, tzinfo=NY)) is None       # feed not caught up yet
    assert surface.stale_message(close, datetime(2026, 10, 13, 11, 0, tzinfo=NY)) is not None
