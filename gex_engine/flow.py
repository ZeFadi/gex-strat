"""Flow-signed GEX: dealer positions inferred from signed trades instead of the OI convention.

The structural GEX assumes dealers are long every call and short every put of the open
interest, and that open interest is yesterday's. Flow-signed GEX (also called
`polarity=flow`) instead signs each trade of the session by its aggressor and gives the
dealer the other side, starting from zero at 09:30:

    dealer position of a contract = - sum over its trades of sign x size   (contracts)

Trade signing (each function returns +1 buyer-initiated, -1 seller-initiated, 0 unknown):

* Lee-Ready (1991): above the prevailing mid buy, below sell, at the mid the tick test
  (uptick buy, downtick sell, zero tick: last non-zero tick).
* EMO, Ellis-Michaely-O'Hara (2000): at the ask buy, at the bid sell, anything else by the
  tick test. Better than Lee-Ready when many trades print inside the spread.
* BVC, bulk volume classification (Easley, Lopez de Prado, O'Hara 2012): no trade-level sign;
  in each time bar the buy share of volume is Phi(dP / sigma_dP), from the bar's price
  change standardized by the contract's own bar-to-bar volatility.

INPUT. A trade file with one row per trade: `contract` (OCC symbol, as in the chain, e.g.
SPXW261013C07800000), `time` (New York, or with an offset), `price`, `size`, and `bid`,
`ask` = the quote in force just before the trade. Any OPRA source can produce it
(ThetaData, Databento, a broker export). The CBOE delayed chain cannot: it has one last
price and a cumulative volume per contract, so it is never used here.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.special import ndtr

from . import config, exposures
from .surface import MarketState

TRADE_COLUMNS = ("contract", "time", "price", "size", "bid", "ask")
METHODS = ("lee_ready", "emo", "bvc")


# --------------------------------------------------------------------------- classifiers

def lee_ready(price, bid, ask, prev_price=None):
    """Sign of each trade, arrays in time order for one contract."""
    price, bid, ask = (np.asarray(x, float) for x in (price, bid, ask))
    mid = 0.5 * (bid + ask)
    quoted = (bid > 0) & (ask >= bid)
    sign = np.where(quoted & (price > mid), 1, np.where(quoted & (price < mid), -1, 0))
    tick = _tick_signs(price) if prev_price is None else np.sign(price - np.asarray(prev_price, float))
    return np.where(sign != 0, sign, tick).astype(int)


def emo(price, bid, ask):
    """Ellis-Michaely-O'Hara: quote rule only at the quotes, tick test everywhere else."""
    price, bid, ask = (np.asarray(x, float) for x in (price, bid, ask))
    quoted = (bid > 0) & (ask >= bid)
    sign = np.where(quoted & np.isclose(price, ask), 1, np.where(quoted & np.isclose(price, bid), -1, 0))
    return np.where(sign != 0, sign, _tick_signs(price)).astype(int)


def bvc_buy_share(bar_price_change, sigma):
    """Share of a bar's volume that is buyer-initiated."""
    return ndtr(np.asarray(bar_price_change, float) / sigma) if sigma > 0 else np.full(np.shape(bar_price_change), 0.5)


def _tick_signs(price: np.ndarray) -> np.ndarray:
    """Tick test with zero ticks carried from the last non-zero tick."""
    out = np.zeros(price.size, dtype=int)
    last = 0
    for i in range(1, price.size):
        d = np.sign(price[i] - price[i - 1])
        last = int(d) if d != 0 else last
        out[i] = last
    return out


# --------------------------------------------------------------------------- trades -> inventory

def load_trades(source) -> pd.DataFrame:
    """Read a trade file (CSV or parquet, path or file-like) and check the columns."""
    name = getattr(source, "name", str(source))
    df = pd.read_parquet(source) if name.endswith(".parquet") else pd.read_csv(source)
    missing = [c for c in TRADE_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"trade file is missing columns {missing}; expected {list(TRADE_COLUMNS)}")
    df = df[list(TRADE_COLUMNS)].copy()
    t = pd.to_datetime(df["time"])
    df["time"] = (t.dt.tz_localize(config.MARKET_TZ) if t.dt.tz is None else t.dt.tz_convert(config.MARKET_TZ))
    for c in ("price", "size", "bid", "ask"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["price", "size"]).sort_values(["contract", "time"]).reset_index(drop=True)


def session_trades(trades: pd.DataFrame) -> pd.DataFrame:
    """Trades from 09:30 New York of their own day: the inventory starts at zero at the open."""
    t = trades["time"]
    open_h, open_m = config.SESSION_OPEN
    return trades.loc[(t.dt.hour * 60 + t.dt.minute) >= open_h * 60 + open_m]


def dealer_inventory(trades: pd.DataFrame, method: str = "lee_ready", bar_seconds: int = 60) -> pd.DataFrame:
    """Per contract: customer volume bought and sold, unclassified volume, dealer position."""
    if method not in METHODS:
        raise ValueError(f"method must be one of {METHODS}")
    rows = []
    for contract, g in session_trades(trades).groupby("contract", sort=False):
        size = g["size"].to_numpy(float)
        if method == "bvc":
            bars = g.set_index("time").resample(f"{bar_seconds}s").agg({"price": "last", "size": "sum"}).dropna()
            dp = bars["price"].diff().dropna()
            vol = bars["size"].iloc[1:].to_numpy()
            share = bvc_buy_share(dp.to_numpy(), float(dp.std(ddof=1)) if len(dp) > 1 else 0.0)
            buy, sell, unknown = float((vol * share).sum()), float((vol * (1 - share)).sum()), float(bars["size"].iloc[0])
        else:
            sign = (lee_ready(g["price"], g["bid"], g["ask"]) if method == "lee_ready"
                    else emo(g["price"], g["bid"], g["ask"]))
            buy, sell, unknown = size[sign > 0].sum(), size[sign < 0].sum(), size[sign == 0].sum()
        rows.append({"contract": contract, "customer_buy": buy, "customer_sell": sell,
                     "unclassified": unknown, "dealer_position": sell - buy})
    return pd.DataFrame(rows, columns=["contract", "customer_buy", "customer_sell", "unclassified",
                                       "dealer_position"])


# --------------------------------------------------------------------------- exposures

def flow_by_strike(state: MarketState, inventory: pd.DataFrame, expiry: str = "all") -> dict:
    """Structural GEX (OI convention), flow GEX (session inventory) and their sum, by strike.

    The sum is the dealer book if yesterday's OI is held as the convention says and today's
    trades are added to it. Contracts of the trade file absent from the chain are reported.
    """
    sub = exposures.select(state, expiry)
    inv = inventory.set_index("contract")["dealer_position"]
    flow_pos = sub["contract"].map(inv).fillna(0.0).to_numpy()
    structural = exposures.by_strike(exposures.contract_exposures(sub, state.spot, state.rate))
    flow = exposures.by_strike(exposures.contract_exposures(sub, state.spot, state.rate, position=flow_pos))
    table = pd.DataFrame({"strike": structural["strike"], "structural_gex": structural["net_gex"],
                          "flow_gex": flow["net_gex"]})
    table["combined_gex"] = table["structural_gex"] + table["flow_gex"]
    unmatched = sorted(set(inv.index) - set(state.contracts["contract"]))
    return {"strikes": table, "unmatched_contracts": unmatched,
            "flow_net_gex": float(table["flow_gex"].sum()),
            "structural_net_gex": float(table["structural_gex"].sum())}


def wall_pressure(table: pd.DataFrame, level: float | None, half_width: float) -> dict | None:
    """Structural and flow GEX of the strikes within `half_width` points of a wall.

    The report's divergence: a call wall is a resistance only while dealers are long its
    gamma. If aggressive call buying (sweeps) has made the session's flow GEX there strongly
    negative, the wall is being absorbed. `flow_to_structural` < -1 means the session's flow
    has more than offset yesterday's structural position around the wall.
    """
    if level is None:
        return None
    zone = table.loc[(table["strike"] - level).abs() <= half_width]
    s, f = float(zone["structural_gex"].sum()), float(zone["flow_gex"].sum())
    return {"level": level, "structural_gex": s, "flow_gex": f,
            "flow_to_structural": f / abs(s) if s else None}
