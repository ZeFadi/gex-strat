# GEX Strat

A local research tool for dealer gamma, vanna and charm exposure on listed US index and ETF options (SPX, NDX, RUT, XSP, SPY, QQQ, IWM). It reads CBOE's free delayed option chains, recomputes forwards, implied vols and Greeks from prices, checks them against CBOE's own Greeks, and covers everything with offline tests. A strategy backtest asks whether trading the gamma regime pays.

Formulas, conventions, validation and limits: [METHODOLOGY.md](METHODOLOGY.md).

## Start in five minutes

Python 3.11 or later. The repository carries the data (CBOE archive, history, SqueezeMetrics GEX), so a fresh clone starts where the archive stands. The one exception is the backtest's licensed price file (see Data).

macOS / Linux:

```bash
python3.11 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
.venv/bin/python cli.py check                  # interpreter, packages, network to CBOE, data, schedule
.venv/bin/python -m pytest                     # offline tests, ~15 s
.venv/bin/python -m streamlit run app.py       # http://localhost:8501
.venv/bin/python cli.py install                # archiving machine only: the closing chains every weekday
```

Windows (PowerShell or cmd):

```bat
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python cli.py check
.venv\Scripts\python -m pytest
.venv\Scripts\python -m streamlit run app.py
.venv\Scripts\python cli.py install          :: archiving machine only
```

With [uv](https://docs.astral.sh/uv/), `uv venv --python 3.11 .venv` then `uv pip install --python .venv/bin/python -r requirements.txt` (`.venv\Scripts\python` on Windows) does the same, faster.

The source is CBOE's delayed chain: quotes are 15 minutes old in session; outside it, the data is the previous close. The sidebar states the as-of time, and the Monitor page refreshes itself every minute.

## Pages

The sidebar sets the underlying, the expiry view (all, front, or one date) and the strike window for every page.

| Page | Question it answers |
|---|---|
| **Monitor** | Where are the levels? Net GEX and regime, zero gamma with its assumption band, walls (in % and one-day σ), 1-day move, VEX, CHEX, trapdoor warning, GEX by strike, GEX(S) profile, front expiry and its GEX projected to the close, data quality, strike table. |
| **Scenarios** | What must dealers trade if IV, spot or time moves? Full re-pricing, in $ and in ES / NQ / RTY contracts (shares for ETFs). Zero gamma under each alternative assumption. VEX and CHEX by strike. |
| **Study** | Has the regime predicted next-day realized vol, range and wall behaviour on the archived history? Statistics appear once 30 consecutive day pairs are archived. |
| **Flow** | Given a file of today's option trades: flow-signed GEX (Lee-Ready, EMO or BVC) against the structural view, and the pressure on each wall. |
| **Backtest** | Does trading the gamma regime pay, net of costs, against the same rules without it and against a VIX filter? Pre-registered rules on ES and VIX futures, with deflated Sharpe, bootstrap intervals, placebo and the vol-prediction mechanism. Needs the licensed price file `data/market/prices.csv`, not in the repository (see Data). |

## How it works

```
CBOE JSON ──> data.py ──> Chain (normalized contracts, as-of time)
                              │
                   surface.build: clock, parity forwards, implied spot, implied vols
                              │
                          MarketState (priced contracts, immutable)
                              │
          ┌───────────────────┼─────────────────────┐
     exposures              flow                  study
     GEX / VEX / CHEX,      trade-signed          daily archive (data/history.csv),
     levels, 0DTE,          dealer positions      next-day regime study
     scenarios                                         │
          │                                       backtest (+ external GEX and prices)
     snapshot.build: one payload per view, with its data-quality report
          │
     app pages (format only)
```

Two rules hold everywhere:

1. **Pages only format.** Every number on screen comes from `gex_engine` through `ui.context()`. A page never computes a quantity.
2. **One function per quantity.** The net GEX, the strike bars and the profile all go through `exposures.net_gex` / `dollar_gex`. Delta and gamma after a time step or a vol shock go through `exposures.reprice`. Durations go through `surface.years_between`. The tests hold the invariants between them.

## Conventions

* **Dealer positions**: long calls, short puts (+OI on calls, −OI on puts). This is an assumption, so magnitudes are upper bounds.
* **GEX**: $ of dealer delta per +1% move in spot. **VEX**: per +1 vol point. **CHEX**: per calendar day. The hedge flow has the opposite sign.
* **Time**: ACT/365 calendar time on the New York clock, computed in UTC so daylight saving is exact. AM-settled monthlies (SPX, NDX, RUT) settle at 09:30, everything else at 16:00.
* **Colours**: blue for positive gamma and calls, red for negative gamma and puts, never green.

## Where to change what

| To… | Change | Then |
|---|---|---|
| add an underlying | `config.SYMBOLS`, `CBOE_FILE`; `SETTLEMENT_TIME` if it has AM roots; `FUTURES` if it has a future; `CASH_SETTLED_ROOTS` if cash-settled | freeze a closing file in `tests/fixtures/` and add it to `test_surface.test_new_indices` |
| change a threshold (trapdoor, shocks, profile range, projection times) | `gex_engine/config.py`, each constant is commented | `pytest` |
| plug in another data source | a function returning `data.Chain` (see `data.COLUMNS`) with a timezone-aware `asof` | live: use it in `ui._fetch_state` instead of `data.fetch`; history: as `data.eod_chain` is used in `study.import_eod` |
| move from the free end-of-day sample to the paid archive | nothing in the code: the paid files have the sample's layout | put them in `data/vendor/historicaldata/day_by_date/`, run `cli.py eod-import` (any symbol of `config.ROOTS`); `backtest.dataset(source="eod")` then runs every trial on our own GEX |
| add a page | a script in `app_pages/` starting with `ctx = ui.context()`, registered in `app.py` | add it to `PAGES` in `tests/test_app.py` |
| change the archive schedule | `JOBS` in `cli.py` (New York times) | `python cli.py install` |
| test a new strategy idea | a rule in `backtest.raw_position` / `RULES`, or a regime in `backtest.regimes` | it is a new trial: the trials log grows and every deflated Sharpe gets stricter; record it in METHODOLOGY section 13 |
| backtest on this engine's GEX | `backtest.dataset(source="own")` once `data/history.csv` covers enough sessions | same report |

## Data

| Data | Where | Refreshed by |
|---|---|---|
| CBOE delayed chains | downloaded live by the app | the app, every minute |
| daily archive of closing chains | `data/raw/{symbol}/{date}.json.gz` (about 6 MB a day), plus SPX's pre-open republication `{date}_preopen.json.gz` (1.8 MB) | `cli.py archive` (scheduled) |
| summary history of the archive | `data/history.csv` | the archive; `cli.py rebuild` recomputes it from `data/raw/` |
| end-of-day option history (historicaldata.net: free sample 2022 H2, SPX and SPY; paid archive since 2002, same layout) | `data/vendor/historicaldata/day_by_date/` | **not in the repository** (licence: no redistribution): `cli.py eod-sample` |
| summary history of the end-of-day files, same row as the archive's | `data/history_eod.csv` | `cli.py eod-import` (derived numbers, versioned) |
| backtest trials log (append-only) | `data/backtest/trials.csv` | each new backtest configuration |
| frozen CBOE files for the tests | `tests/fixtures/` | never (tests depend on them) |
| backtest prices (SPX OHLC, S&P 500 total return, 3-month rate, VIX, VIX futures UX1/UX2) | `data/market/prices.csv`: **not in the repository** (Bloomberg data); its provenance file, versioned, lists the tickers, columns and dates | export from a terminal, then `cli.py backtest-data --prices FILE` |
| SqueezeMetrics public GEX history | `data/external/` | `cli.py backtest-data --squeeze` |

Everything except the price file is in the repository. The licence covers the code, not the third-party data (see NOTICE). Without the fixtures, the tests that need them are skipped with a message; without the backtest inputs, the Backtest page explains how to create them.

## Running on another machine

* **First run `cli.py check`.** It reports what differs between set-ups: Python and package versions, the time zone database, the network path to CBOE, the data on disk, and the local times of the schedule. When something fails, its output is the thing to send.
* **Schedule.** `cli.py install` uses launchd on macOS and the Task Scheduler on Windows (it prints a crontab line elsewhere). The jobs are set in New York time in `cli.JOBS` and converted to the machine's clock, including daylight-saving gaps and a date change in Asia. The machine must be on, or the missed run starts when it comes back; on Windows the user must be logged in. Check a Windows install once with `schtasks /Run /TN "GEX Strat archive"` and then read `data/archive.log`.
* **One archiver.** Only one machine runs `cli.py install`. The archive writes `data/` but never commits: the archiving machine commits and pushes `data/` from time to time, the other pulls. Two archives of the same day can differ (CBOE republishes the file before the open with new open interest, METHODOLOGY section 12), so if two copies ever diverge, keep the archiving machine's `data/` as a whole.
* **Proxy and TLS inspection.** `pip install` needs the same network settings as any download on the machine (proxy, or an internal package index). The app's downloads use Python's standard library. It reads `HTTPS_PROXY` (and the Windows proxy settings), and on Windows it trusts the system's certificate store. Elsewhere, point `SSL_CERT_FILE` to the corporate root certificate if `check` reports a certificate error.
* **Line endings.** `.gitattributes` keeps LF everywhere and the CSV writers use `\n`, so a Windows checkout produces no spurious diffs.

## Command line

```bash
.venv/bin/python cli.py archive                      # today's closing chains, all symbols (nothing before the close)
.venv/bin/python cli.py archive SPX --intraday       # a timestamped session snapshot
.venv/bin/python cli.py rebuild                      # recompute data/history.csv after an engine change
.venv/bin/python cli.py archive SPX --preopen        # before the open: the close with the overnight open interest (raw only)
.venv/bin/python cli.py install                      # weekdays 16:30 New York, catch-up 01:30, pre-open 07:45, in local time (uninstall to remove)
.venv/bin/python cli.py check                        # is this machine ready?
.venv/bin/python cli.py backtest-data --squeeze      # download SqueezeMetrics' GEX history
.venv/bin/python cli.py backtest-data --prices FILE  # check and copy a daily price file
.venv/bin/python cli.py eod-sample                   # free end-of-day sample (2022 H2), checked against its manifest
.venv/bin/python cli.py eod-import SPX SPY           # end-of-day files -> data/history_eod.csv (about 1 s a day a symbol)
```

The archive retries network errors three times; scheduled runs (`--log`) append to `data/archive.log`.

## Tests

| File | What it guarantees |
|---|---|
| `test_pricing.py` | Greeks equal finite differences of the price; parity; implied-vol round trips |
| `test_surface.py` | known forwards and vols recovered exactly on a synthetic chain; clock, AM/PM settlement, DST; CBOE consistency at the close and in session (0DTE gamma within 2% of CBOE); the end-of-day provider gives the same book as CBOE on the same quotes, nominal Saturday expiries settle on Friday |
| `test_exposures.py` | net GEX = strike bars = profile at spot; zero gamma equals a closed form; band; projection; tiny scenario shocks reproduce GEX and VEX; cash vs physical settlement |
| `test_study.py` | archive rules; Newey-West formula; a simulated effect is found, no look-ahead, no glued days; end-of-day import never bridges a failed day; close-only histories fall back to close-to-close |
| `test_flow.py` | Lee-Ready, EMO, BVC; inventory; flow GEX = structural GEX for the same positions |
| `test_backtest.py` | a perfect-foresight signal pays only at the right lag; no look-ahead in regimes and sizing; costs; VIX futures roll; deflated Sharpe; append-only trials log |
| `test_app.py` | every page renders; one download serves every view; stale-source warning |
| `test_cli.py` | schedule local times in Paris, London, New York, Tokyo, Los Angeles; the evening run is after the close every day of the year; launchd, Task Scheduler and cron jobs; scheduled runs log |

## Layout

```
app.py              entry: sidebar controls and page navigation
app_pages/          monitor, scenarios, study, flow, backtest (formatting only)
ui.py               page context, cached data access, formatting and table helpers
cli.py              archive, schedule (macOS, Windows, cron), backtest inputs, set-up check
gex_engine/
    config.py       every convention and threshold, commented
    data.py         normalized chain schema + validation, CBOE delayed-quotes provider
    pricing.py      generalized Black-Scholes Greeks, Black-76 implied vol (vectorized)
    surface.py      clock (DST-safe), parity forwards, implied spot, vols (mid, bid, ask), ATM term structure
    exposures.py    positions, reprice, GEX / VEX / CHEX, walls, max pain, profile, zero gamma and band,
                    front expiry and projection, scenarios
    snapshot.py     market state -> payload of one view, data-quality report
    flow.py         trade signing (Lee-Ready, EMO, BVC), dealer inventory, flow-signed GEX
    stats.py        Newey-West OLS, Sharpe and its uncertainty, bootstrap, deflated Sharpe
    study.py        daily archive and history, next-day regime study
    backtest.py     backtest inputs (ES excess returns, roll-adjusted VIX futures, GEX), rules, report
    charts.py       Plotly figures
data/               raw/ archive, history.csv, history_eod.csv, backtest/ logs, market/ and external/ inputs (archive.log, vendor/ and prices.csv stay local)
tests/              pytest and frozen fixtures
LICENSE, NOTICE     Apache 2.0 for the code; the data keeps its providers' terms
```
