"""Trade signing, dealer inventory and flow-signed GEX."""

import io

import numpy as np
import pandas as pd
import pytest

from gex_engine import exposures, flow


def test_lee_ready_quote_rule():
    assert flow.lee_ready([1.10, 1.00, 1.04], [1.00] * 3, [1.10] * 3).tolist() == [1, -1, -1]


def test_lee_ready_tick_test_carries_last_nonzero_tick():
    prices = [1.05, 1.06, 1.06, 1.05, 1.05]
    bids, asks = [1.00, 1.02, 1.02, 1.00, 1.00], [1.10, 1.10, 1.10, 1.10, 1.10]
    assert flow.lee_ready(prices, bids, asks).tolist() == [0, 1, 1, -1, -1]


def test_emo_uses_quotes_only_at_the_quotes():
    # at ask: buy; at bid: sell; inside: tick test (1.07 after 1.00 is an uptick)
    assert flow.emo([1.10, 1.00, 1.07, 1.03], [1.00] * 4, [1.10] * 4).tolist() == [1, -1, 1, -1]


def test_bvc_buy_share():
    assert flow.bvc_buy_share([0.0], 0.1)[0] == pytest.approx(0.5)
    assert flow.bvc_buy_share([0.1], 0.1)[0] == pytest.approx(0.8413, abs=1e-4)


def _trades(rows):
    df = pd.DataFrame(rows, columns=list(flow.TRADE_COLUMNS))
    return flow.load_trades(io.StringIO(df.to_csv(index=False)))


def test_inventory_signs_and_session_start():
    t = _trades([
        ("C1", "2026-10-13 09:29:00", 1.10, 99, 1.00, 1.10),       # before the open: ignored
        ("C1", "2026-10-13 09:31:00", 1.10, 10, 1.00, 1.10),       # customer buys 10
        ("C1", "2026-10-13 09:32:00", 1.00, 4, 1.00, 1.10),        # customer sells 4
    ])
    inv = flow.dealer_inventory(t, "lee_ready").set_index("contract")
    assert inv.loc["C1", "dealer_position"] == -6                  # dealer net short 6


def test_missing_columns_are_reported():
    with pytest.raises(ValueError, match="missing columns"):
        flow.load_trades(io.StringIO("contract,time,price\nX,2026-10-13 10:00,1\n"))


def test_flow_gex_equals_structural_for_the_same_positions(spx_state):
    """If the session's inventory equals the convention's positions, flow GEX = structural GEX."""
    sub = exposures.select(spx_state, "front")
    inv = pd.DataFrame({"contract": sub["contract"], "dealer_position": exposures.dealer_position(sub)})
    res = flow.flow_by_strike(spx_state, inv, "front")
    assert np.allclose(res["strikes"]["flow_gex"], res["strikes"]["structural_gex"])
    assert res["unmatched_contracts"] == []


def test_wall_pressure():
    table = pd.DataFrame({"strike": [100.0, 105.0, 110.0], "structural_gex": [0.0, 2.0, 1.0],
                          "flow_gex": [0.0, -5.0, -1.0], "combined_gex": [0.0, -3.0, 0.0]})
    w = flow.wall_pressure(table, 105.0, half_width=5.0)
    assert w["structural_gex"] == 3.0 and w["flow_gex"] == -6.0 and w["flow_to_structural"] == -2.0
