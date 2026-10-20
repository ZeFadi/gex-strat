"""Dealer gamma, vanna and charm exposure engine.

    config      every convention and threshold, commented
    data        normalized option-chain schema and the CBOE delayed-quotes provider
    pricing     generalized Black-Scholes Greeks and Black-76 implied vol, vectorized
    surface     clock (DST-safe year fractions, settlement) and chain -> priced MarketState
    exposures   positions, GEX / VEX / CHEX, walls, zero gamma and its band, 0DTE, scenarios
    snapshot    one view's payload and its data-quality report
    flow        flow-signed GEX from a trade file: Lee-Ready, EMO, BVC
    stats       Newey-West regression, Sharpe and its uncertainty, deflated Sharpe
    study       daily archive of our closes and the next-day regime study
    backtest    pre-registered strategy backtest on external GEX and market data
    charts      Plotly figures

See METHODOLOGY.md for the formulas and conventions.
"""

from . import (backtest, charts, config, data, exposures, flow, pricing, snapshot, stats, study,
               surface)
from .data import Chain, ChainError

__all__ = ["backtest", "charts", "config", "data", "exposures", "flow", "pricing", "snapshot", "stats",
           "study", "surface", "Chain", "ChainError"]
