"""The option chain: the normalized schema every provider meets, and the CBOE provider.

Schema
------
The normalized option chain every provider must return.

The engine never sees a vendor's field names. A provider (CBOE delayed JSON today, a
licensed feed such as Bloomberg or ThetaData later) turns its payload into a `Chain`, and
`validate` checks the contract below before any number is computed from it. Swapping the
source then changes one module and no formula.

CBOE delayed quotes
-------------------
CBOE delayed-quotes provider (development source).

Free, keyless, 15-minute delayed, the full listed chain with OI, vendor IV and Greeks.
Good enough to build and check the engine; not a production feed (no SLA, no history, the
URL already moved once: cdn.cboe.com now answers 307 to cdn-api.cboe.com).

Two timestamps matter in the payload:

* `timestamp` (top level): when CBOE generated the file, UTC. Used for staleness only.
* `data.last_trade_time`: the time of the underlying's last print, New York time.
* each option's `last_trade_time`: the latest of them is how far the option data reaches.

The valuation time `asof` is the EARLIER of the underlying's print and the latest option
trade. Both are 15 minutes behind in session and at the close after hours, in which case
they agree to the second. If the index print ever ran ahead of the delayed option data, the
earlier time keeps T measured from when the option quotes were taken; measuring from the
later one would understate a 0DTE's T and overstate its gamma. Both times are kept in
`meta` and shown in the data-quality table.

`data.prev_day_close` is NOT the previous session's close once the session is over: in the
closing files it equals the day's own close (SPX 2026-10-12: close 7807.24, prev_day_close
7807.2402, price_change -4.30). It is not read. The day's `price_change` is kept; the study
derives the previous close from it where needed (study.build_dataset).

`data.current_price` is NOT always the price the options were quoted against: after the
close SPY's carries the extended-hours print (777.735 on the fixture while the options
imply 778.11). The engine therefore takes its reference spot from put-call parity
(surface.build) and keeps this one for the data-quality report.

End-of-day vendor files (historicaldata.net)
--------------------------------------------
History provider: one CSV per session (`{date}_options.csv`, 34 columns, every listed contract
of every market). The free sample (2022 H2, SPX and SPY) and the paid archive (2002 on) share
the layout, so moving to the paid data means more files in config.EOD_DIR, no code change.
Their README fixes the conventions this provider relies on:

* quotes are the closing quotes of `quote_date`; `asof` is set to the options close, 16:15
  New York (index and ETF options quote until then);
* `open_interest` is the figure published for the session, i.e. positions at the PRIOR
  close: the start-of-session open interest, the same convention as the evening CBOE archive;
* `underlying_close` is the official close; there is no underlying open, high or low;
* the root is the contract symbol without its last 15 characters (SPX vs SPXW), not the
  `underlying` column, which changed convention in 2014;
* before 2015-02 monthly expirations carry the nominal SATURDAY: the provider moves them to
  the Friday, when they settle (AM for SPX, NDX, RUT through config.SETTLEMENT_TIME);
* a blank field means "not available", never zero: blank bids and asks stay NaN.
"""

from __future__ import annotations

import gzip
import json
import re
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import config


# --------------------------------------------------------------------------- schema

# One row per listed contract. Prices in the underlying's currency, sizes in contracts.
COLUMNS = {
    "contract": "object",       # vendor / OCC symbol, unique
    "root": "object",           # option root (SPX, SPXW, SPY...): drives settlement time
    "expiry": "object",         # datetime.date of the expiration
    "is_call": "bool",
    "strike": "float64",
    "bid": "float64",
    "ask": "float64",
    "last": "float64",
    "volume": "float64",        # contracts traded in the session the quote belongs to
    "oi": "float64",            # open interest, as published by OCC before the session
    "iv_vendor": "float64",     # vendor IV, decimal; kept for validation and as a fallback
    "delta_vendor": "float64",
    "gamma_vendor": "float64",
}


@dataclass(frozen=True)
class Chain:
    """An option chain as of one instant.

    `asof` is the time the quotes and the underlying price belong to, timezone-aware. It is
    NOT the time the file was fetched: with delayed or after-hours data the two differ by
    minutes or by a night, and time to expiry is measured from `asof`.
    """

    symbol: str
    asof: datetime
    spot_quoted: float
    contracts: pd.DataFrame
    meta: dict = field(default_factory=dict)


class ChainError(ValueError):
    """The provider returned something the engine cannot price."""


def validate(chain: Chain) -> Chain:
    """Check the schema and the invariants the engine relies on; raise on violation."""
    df = chain.contracts
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise ChainError(f"{chain.symbol}: missing columns {missing}")
    if chain.asof.tzinfo is None:
        raise ChainError(f"{chain.symbol}: asof must be timezone-aware")
    if not chain.spot_quoted > 0:
        raise ChainError(f"{chain.symbol}: non-positive spot {chain.spot_quoted}")
    if df.empty:
        raise ChainError(f"{chain.symbol}: empty chain")
    if df["contract"].duplicated().any():
        raise ChainError(f"{chain.symbol}: duplicated contracts")
    if (df["strike"] <= 0).any():
        raise ChainError(f"{chain.symbol}: non-positive strikes")
    if (df["oi"] < 0).any():
        raise ChainError(f"{chain.symbol}: negative open interest")
    return chain


# --------------------------------------------------------------------------- CBOE provider

# Root, YYMMDD, C/P, strike x 1000 on 8 digits (OCC symbology without the padding).
_OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def download(symbol: str, attempts: int = 1) -> dict:
    """The raw CBOE payload of `symbol`; network errors are retried `attempts` times in total.

    The app tries once (a failed refresh keeps the last good state on screen). The daily
    archive tries config.ARCHIVE_ATTEMPTS times, with growing pauses: a single timeout used to
    lose the day for good (XSP, 2026-10-16).
    """
    sym = symbol.upper().lstrip("^")
    if sym not in config.CBOE_FILE:
        raise ChainError(f"unsupported symbol {symbol!r}; expected one of {config.SYMBOLS}")
    req = urllib.request.Request(_url(sym), headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except OSError:                      # URLError, timeouts and connection resets
            if attempt == attempts:
                raise
            time.sleep(config.RETRY_PAUSE_SECONDS * attempt)


def fetch(symbol: str) -> Chain:
    """Download and parse the current delayed chain of `symbol`."""
    sym = symbol.upper().lstrip("^")
    return parse(download(sym), sym)


def _url(sym: str) -> str:
    """CBOE delayed-quotes URL of a supported symbol."""
    return config.CBOE_URL.format(file=config.CBOE_FILE[sym])


def load(path: str | Path, symbol: str) -> Chain:
    """Parse a saved payload (plain or gzipped JSON). Used by the tests and for replay."""
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        payload = json.load(fh)
    return parse(payload, symbol.upper().lstrip("^"))


def parse(payload: dict, symbol: str) -> Chain:
    """Turn a CBOE payload into a validated `Chain`."""
    block = payload.get("data") or {}
    options = block.get("options") or []
    if not options:
        raise ChainError(f"{symbol}: payload has no options")

    rows, last_option_trade = [], ""
    for o in options:
        m = _OCC.match(o.get("option", ""))
        if m is None:
            continue
        root, ymd, cp, k = m.groups()
        last_option_trade = max(last_option_trade, o.get("last_trade_time") or "")
        rows.append((
            o["option"], root, datetime.strptime(ymd, "%y%m%d").date(), cp == "C", int(k) / 1000.0,
            o.get("bid"), o.get("ask"), o.get("last_trade_price"), o.get("volume"),
            o.get("open_interest"), o.get("iv"), o.get("delta"), o.get("gamma"),
        ))
    df = pd.DataFrame(rows, columns=list(_columns()))
    num = ["strike", "bid", "ask", "last", "volume", "oi", "iv_vendor", "delta_vendor", "gamma_vendor"]
    df[num] = df[num].apply(pd.to_numeric, errors="coerce").astype(float)
    # Missing sizes are zero; missing prices stay NaN so they can never pass as a quote.
    df[["volume", "oi"]] = df[["volume", "oi"]].fillna(0.0)

    underlying_time = _ny(block.get("last_trade_time"))
    options_time = _ny(last_option_trade)
    asof = min(t for t in (underlying_time, options_time) if t is not None) \
        if (underlying_time or options_time) else _file_time(payload)
    meta = {
        "generated_utc": payload.get("timestamp"),
        "underlying_time": underlying_time.isoformat() if underlying_time else None,
        "options_last_trade": options_time.isoformat() if options_time else None,
        "iv30_vendor": block.get("iv30"),
        **{k: block.get(k) for k in ("open", "high", "low", "close", "price_change")},
    }
    chain = Chain(symbol=symbol, asof=asof, spot_quoted=float(block.get("current_price") or np.nan),
                  contracts=df, meta=meta)
    return validate(chain)


def _columns():
    """Column order of the parsed rows (the COLUMNS schema)."""
    return ("contract", "root", "expiry", "is_call", "strike", "bid", "ask", "last", "volume",
            "oi", "iv_vendor", "delta_vendor", "gamma_vendor")


def _ny(stamp: str | None) -> datetime | None:
    """CBOE local timestamps are New York time without an offset."""
    return datetime.fromisoformat(stamp).replace(tzinfo=config.MARKET_TZ) if stamp else None


def _file_time(payload: dict) -> datetime:
    """File generation time (UTC in the payload), as New York time; last-resort as-of."""
    ts = payload.get("timestamp")
    if not ts:
        raise ChainError("payload carries no timestamp")
    return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).astimezone(config.MARKET_TZ)


# --------------------------------------------------------------------------- end-of-day vendor files

EOD_COLUMNS = ("contract", "expiration", "type", "strike", "quote_date", "bid", "ask", "close", "volume",
               "open_interest", "underlying_close", "iv", "delta", "gamma")


def eod_files(directory: str | Path = config.EOD_DIR) -> list[Path]:
    """The daily files of an end-of-day folder, in date order (provisional files excluded)."""
    return sorted(Path(directory).glob("????-??-??_options.csv"))


def eod_read(path: str | Path) -> pd.DataFrame:
    """One vendor day, the columns the provider uses, with the contract root added."""
    df = pd.read_csv(path, usecols=list(EOD_COLUMNS), dtype={"contract": str, "type": str})
    return df.assign(root=df["contract"].str[:-15])


def eod_chain(day: pd.DataFrame, symbol: str) -> Chain:
    """The `Chain` of `symbol` (all its roots, config.ROOTS) in one vendor day."""
    rows = day.loc[day["root"].isin(config.ROOTS[symbol])]
    if rows.empty:
        raise ChainError(f"{symbol}: no contract in this file")
    quote_date = pd.Timestamp(rows["quote_date"].iloc[0]).date()
    expiry = pd.to_datetime(rows["expiration"])
    expiry = expiry - pd.to_timedelta((expiry.dt.dayofweek == 5).astype(int), unit="D")   # nominal Saturday
    df = pd.DataFrame({
        "contract": rows["contract"].to_numpy(), "root": rows["root"].to_numpy(),
        "expiry": expiry.dt.date.to_numpy(), "is_call": (rows["type"] == "call").to_numpy(),
        "strike": rows["strike"].to_numpy(float), "bid": rows["bid"].to_numpy(float),
        "ask": rows["ask"].to_numpy(float), "last": rows["close"].to_numpy(float),
        "volume": rows["volume"].fillna(0.0).to_numpy(float), "oi": rows["open_interest"].fillna(0.0).to_numpy(float),
        "iv_vendor": rows["iv"].to_numpy(float), "delta_vendor": rows["delta"].to_numpy(float),
        "gamma_vendor": rows["gamma"].to_numpy(float),
    })
    close = float(rows["underlying_close"].dropna().iloc[0]) if rows["underlying_close"].notna().any() else np.nan
    asof = datetime.combine(quote_date, datetime.min.time().replace(hour=config.OPTIONS_CLOSE[0],
                                                                    minute=config.OPTIONS_CLOSE[1]),
                            tzinfo=config.MARKET_TZ)
    meta = {"source": "historicaldata.net end of day", "close": close,
            "open": None, "high": None, "low": None, "price_change": None, "iv30_vendor": None}
    return validate(Chain(symbol=symbol, asof=asof, spot_quoted=close, contracts=df, meta=meta))
