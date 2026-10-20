"""Constants of the engine: market conventions, data rules and numerical settings.

Everything a reader would want to change, or question, lives here rather than as a literal
inside a formula. METHODOLOGY.md explains each choice.
"""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------- market

# Listed US options settle and trade on New York time. Every timestamp is converted to this
# zone before any arithmetic, so daylight saving is handled by the tz database rather than
# by a fixed UTC offset (which is wrong five months a year).
MARKET_TZ = ZoneInfo("America/New_York")

# Shares (or index units) per contract.
MULTIPLIER = 100.0

# Session clock, New York time, as (hour, minute). Index and ETF options trade until 16:15,
# fifteen minutes after the underlying's 16:00 close; ETFs stamp their last print 15:59:59,
# hence the archive cutoff at 15:59 (study.is_close).
SESSION_OPEN = (9, 30)
SESSION_CLOSE = (16, 0)
OPTIONS_CLOSE = (16, 15)
ARCHIVE_CUTOFF = (15, 59)

# Settlement time of each option root, local New York time.
# SPX (root "SPX") is the AM-settled monthly: its last trading day is the day BEFORE the
# expiration date and it settles on the opening print (SOQ), so time value runs out at
# 09:30, not 16:00. SPXW and the ETF options are PM-settled at the close.
# NDX and RUT follow the same pattern (AM monthly root, PM-settled NDXP / RUTW); XSP is
# PM-settled.
SETTLEMENT_TIME = {"SPX": SESSION_OPEN, "NDX": SESSION_OPEN, "RUT": SESSION_OPEN}
DEFAULT_SETTLEMENT_TIME = SESSION_CLOSE

# Index options settle in cash: at settlement the option, and the dealer's hedge of it,
# disappear. ETF options settle into shares: an in-the-money option becomes a position in
# the underlying, so its delta survives settlement. This decides the hedge flow of
# scenarios that run past an expiry (exposures.reprice).
CASH_SETTLED_ROOTS = {"SPX", "SPXW", "NDX", "NDXP", "RUT", "RUTW", "XSP"}

# Symbols the dashboard offers. VIX is deliberately absent: VIX options are priced off the
# VIX FUTURE of their own expiry, not off the spot index, so a Black-Scholes gamma taken
# against the VIX index level has no hedging meaning.
SYMBOLS = ("SPX", "NDX", "RUT", "XSP", "SPY", "QQQ", "IWM")

# Futures used to express hedge flows: (name, $ per index point, index level per unit of the
# underlying's price). Contracts = $ delta / (multiplier x ratio x S). XSP is SPX / 10, hence
# ratio 10. ETFs are expressed in shares: their ratio to the index drifts with fees and
# dividends.
FUTURES = {
    "SPX": ("ES", 50.0, 1.0),
    "XSP": ("ES", 50.0, 10.0),
    "NDX": ("NQ", 20.0, 1.0),
    "RUT": ("RTY", 50.0, 1.0),
}

# Continuously compounded risk-free rate used only to DISCOUNT (Black-76). The forward of
# each expiry is implied from put-call parity, so dividends and repo never need to be
# assumed; the rate's only role left is the discount factor, whose effect on gamma is
# second order (exp(-rT) on a one-year option at 4% is 0.96, and the implied vol absorbs
# most of it because it is implied with the same factor).
RISK_FREE_RATE = 0.04

# Calendar-time basis (ACT/365). This is the basis CBOE uses for the vendor IV and Greeks we
# validate against; measured on the frozen fixture, our gamma matches theirs with a median
# ratio of 0.99 under it.
DAYS_PER_YEAR = 365.0

# --------------------------------------------------------------------------- data rules

# Implied forward: number of strikes, nearest to at-the-money, whose parity forwards are
# combined by median. One strike (the CBOE VIX white-paper rule) is sensitive to a single
# stale quote; three keeps the rule local while removing that sensitivity.
FORWARD_STRIKES = 3
# An expiry needs at least this many strikes with a two-sided call AND put quote to get
# its own forward. Otherwise its forward is carried from the nearest priced expiry.
FORWARD_MIN_PAIRS = 3

# A quote is usable for implied vol when bid > 0 and ask >= bid. The relative spread cap
# rejects quotes too wide to locate the price (a 0.05 / 0.95 market says nothing).
MAX_REL_SPREAD = 1.0

# Smallest time value (currency units) a vol is inverted from; also at least half the spread.
MIN_TIME_VALUE = 0.01

# Implied vol accepted from the inversion. Outside this range the quote is treated as
# unusable and the vol is filled from the smile (surface._assign_vols).
IV_MIN, IV_MAX = 0.005, 5.0

# Contracts whose time to settlement is below this (in years, about one minute) are treated
# as settled: their gamma is a Dirac at the strike and carries no hedging information.
MIN_T_YEARS = 1.0 / (DAYS_PER_YEAR * 24 * 60)

# --------------------------------------------------------------------------- numerics

# Spot grid of the GEX(S) profile, as a +/- fraction of the reference spot, and its size.
# The zero-gamma level is then refined by Brent's method on the exact function, so the grid
# only has to bracket the roots, not locate them.
PROFILE_RANGE = 0.06
PROFILE_POINTS = 241

# Bisection iterations of the implied-vol inversion: the bracket [IV_MIN, IV_MAX] shrinks
# by 2^-60, far below any quote's precision.
IV_BISECTION_STEPS = 60

# --------------------------------------------------------------------------- levels & scenarios

# Distances to levels are also expressed in one-day standard deviations, S sigma_1d / sqrt(365),
# with sigma_1d the ATM implied vol interpolated in total variance at one calendar day.
ONE_DAY = 1.0 / DAYS_PER_YEAR

# "Trapdoor": spot below zero gamma AND the put wall less than this many one-day sigmas below.
TRAPDOOR_SIGMAS = 1.0

# Shocks of the scenario table. Vol shocks are parallel, in vol points; spot shocks are
# relative and sticky-strike.
VOL_SHOCKS = (-5.0, -2.0, 2.0, 5.0)
SPOT_SHOCKS = (-0.02, -0.01, 0.01, 0.02)

# Clock times (New York) at which the front expiry's GEX is projected, spot and vols held.
PROJECTION_TIMES = ((12, 0), (14, 0), (15, 0), (15, 30), (15, 50))

# --------------------------------------------------------------------------- service

# Local data folder: daily archive of raw chains and the history the study reads.
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# During the session, a file whose as-of lags the clock by more than this is frozen
# (the quotes themselves are 15 minutes old).
MAX_SESSION_LAG_MINUTES = 30

# CBOE refreshes its delayed chains about every 15 seconds but the quotes themselves are
# 15 minutes old; polling faster than once a minute buys nothing.
REFRESH_SECONDS = 60
CBOE_URL = "https://cdn-api.cboe.com/api/global/delayed_quotes/options/{file}.json"
CBOE_FILE = {"SPX": "_SPX", "NDX": "_NDX", "RUT": "_RUT", "XSP": "_XSP",
             "SPY": "SPY", "QQQ": "QQQ", "IWM": "IWM"}
HTTP_TIMEOUT = 20
# Network attempts of the daily archive, and the pause before the n-th retry (n x this).
ARCHIVE_ATTEMPTS = 3
# A file is archived only if at least this share of its contracts has a two-sided quote.
ARCHIVE_MIN_QUOTED_SHARE = 0.5
RETRY_PAUSE_SECONDS = 10

# --------------------------------------------------------------------------- backtest
# Fixed BEFORE the first run (see METHODOLOGY.md, section 13, and the git history). Any later
# change is a new trial and goes into data/backtest/trials.csv.

# Out-of-sample period: never used to choose anything; looked at once.
OOS_START = "2024-01-01"

# Execution lag in sessions between the signal (close t) and the return earned. 2 = enter at
# the close of t+1 (primary: robust to an unknown publication time of the GEX); 1 = enter
# right after the close of t (ES trades until 17:00 New York; optimistic bound).
EXECUTION_LAGS = (2, 1)

# Positions are scaled to this annualized volatility with the asset's trailing 60-session
# volatility, known at the decision: comparable risk across ES and VIX futures, and no ruin
# on 2018-02-05 (UX1 +113% in a day). Gross weight capped.
VOL_TARGET = 0.10
VOL_WINDOW = 60
MAX_WEIGHT = 2.0

# Trading costs per unit of notional traded, per side: ES ~ half a tick plus commission;
# VIX futures one tick (0.05 point) over the futures price. Results are also shown at x2.
COST_ES = 1.0e-4
COST_VIX_POINTS = 0.05

# Regime definitions: the sign of GEX, and "low gamma" = GEX below this expanding quantile of
# its own past (at least REGIME_MIN_HISTORY sessions, threshold known at t-1).
LOW_GAMMA_QUANTILE = 0.25
REGIME_MIN_HISTORY = 250

# Statistics: circular block bootstrap of daily returns, placebo by circular shifts of the
# regime series (shift at least the longest regime episode).
BOOTSTRAP_BLOCK = 20
BOOTSTRAP_DRAWS = 2000
PLACEBO_DRAWS = 500
SEED = 20261015

# --------------------------------------------------------------------------- vol forecast trial

# PRE-REGISTERED on 2026-10-20 (METHODOLOGY.md section 14), committed before any run on
# market data. Does the GEX regime improve a HAR-X forecast of next-session realized
# variance out of sample? Changing any of these is a new trial (data/backtest/forecast_trials.csv).
FORECAST_RANK_WINDOW = 252     # GEX percentile rank among the trailing sessions, t included
FORECAST_MIN_TRAIN = 1000      # complete rows before the first fit
FORECAST_REFIT_EVERY = 21      # sessions between two refits; the window expands
FORECAST_FLOOR = 1.0           # floor of a variance forecast, annualized %^2 (1 vol point)
FORECAST_HAC_LAGS = 20         # Newey-West lags of the loss differentials
FORECAST_ALPHA = 0.05          # one-sided: the GEX model must lower the QLIKE loss
