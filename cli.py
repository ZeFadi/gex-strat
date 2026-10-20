"""Command line: daily archive, its schedule, the backtest's input data, and a setup check.

    python cli.py check                       # is this machine ready? (interpreter, packages, network, data)
    python cli.py archive                     # today's closing chains, all symbols (nothing before the close)
    python cli.py archive SPX SPY --intraday  # timestamped session snapshots
    python cli.py archive SPX --force         # re-archive a close already on disk
    python cli.py archive --preopen           # before the open: the close with the overnight open interest
    python cli.py rebuild                     # recompute data/history.csv from data/raw (after an engine change)
    python cli.py install                     # schedule the archive (macOS launchd, Windows Task Scheduler)
    python cli.py uninstall
    python cli.py backtest-data --prices FILE # check and copy the backtest's price file
    python cli.py backtest-data --squeeze     # download SqueezeMetrics' public GEX history
    python cli.py eod-sample                  # download the free end-of-day sample (2022 H2, SPX and SPY)
    python cli.py eod-import [SPX SPY]        # end-of-day files -> data/history_eod.csv

Run it with the project's interpreter: .venv/bin/python on macOS and Linux,
.venv\\Scripts\\python on Windows.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import date, datetime, time, timedelta, timezone
from importlib import metadata
from pathlib import Path
from xml.sax.saxutils import escape

import pandas as pd

from gex_engine import backtest, config, data, study

ROOT = Path(__file__).resolve().parent
CLI = Path(__file__).resolve()
LOG = config.DATA_DIR / "archive.log"

# --------------------------------------------------------------------------- archive schedule

# The two archive jobs, in New York time; weekdays 0 = Monday. The evening run (16:30, after
# the 16:15 options close) is strict: a symbol it does not archive is an error in the log.
# The catch-up run (01:30, while the CBOE file still holds the previous close) only fills
# days the evening missed and is lenient. The pre-open run (07:45: the OCC open interest is
# in the file by 06:50, and up to an hour later in daylight-saving gaps is still before the
# 09:30 open) keeps the previous close republished with it, raw only, lenient; SPX only
# (1.8 MB a day: enough to measure which open interest predicts better).
# Both schedulers also start a run missed while the machine was off or asleep as soon as it
# is back.
JOBS = {
    "archive": ((16, 30), range(0, 5), ["archive"]),                             # Monday-Friday
    "archive.catchup": ((1, 30), range(0, 6), ["archive", "--catch-up"]),         # Monday-Saturday
    "archive.preopen": ((7, 45), range(0, 5), ["archive", "SPX", "--preopen"]),   # Monday-Friday
}
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def local_schedule(at: tuple[int, int], days, start: date | None = None, tz=None) -> tuple[int, int, list[int]]:
    """Local (hour, minute, weekdays) of a job set at `at` New York time on `days`.

    Schedulers run on the machine's clock, whose daylight-saving dates may differ from New
    York's (Europe changes a few weeks apart). The job is placed at the latest local time
    that New York time maps to over the coming year, so it never runs before `at` New York
    and at most an hour after it. Weekdays move with the time when it crosses midnight
    (Asia). `tz` (a ZoneInfo) stands in for the machine's time zone in tests.
    """
    start = start or date.today()
    shift = max(datetime.combine(d, time(*at), tzinfo=config.MARKET_TZ).astimezone(tz).replace(tzinfo=None)
                - datetime.combine(d, time()) for d in (start + timedelta(days=i) for i in range(366)))
    day, minutes = divmod(int(shift.total_seconds()) // 60, 24 * 60)
    return minutes // 60, minutes % 60, sorted((d + day) % 7 for d in days)


def _interpreter() -> str:
    """This interpreter; on Windows its windowless twin, so a scheduled run opens no console."""
    quiet = Path(sys.executable).with_name("pythonw.exe")
    return str(quiet if sys.platform == "win32" and quiet.exists() else sys.executable)


def plist(label: str, hour: int, minute: int, days: list[int], argv: list[str]) -> str:
    """macOS launchd job. launchd weekdays: 0 = Sunday."""
    entries = "".join(
        f"    <dict><key>Weekday</key><integer>{(d + 1) % 7}</integer><key>Hour</key><integer>{hour}</integer>"
        f"<key>Minute</key><integer>{minute}</integer></dict>\n" for d in days)
    args = "".join(f"<string>{escape(a)}</string>" for a in argv)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>{args}</array>
  <key>WorkingDirectory</key><string>{escape(str(ROOT))}</string>
  <key>StartCalendarInterval</key>
  <array>
{entries}  </array>
  <key>StandardOutPath</key><string>{escape(str(LOG))}</string>
  <key>StandardErrorPath</key><string>{escape(str(LOG))}</string>
</dict>
</plist>
"""


def task_xml(hour: int, minute: int, days: list[int], argv: list[str]) -> str:
    """Windows Task Scheduler job: weekly, for the logged-in user, started late if missed."""
    weekdays = "".join(f"<{DAY_NAMES[d]} />" for d in days)
    start = datetime.combine(date.today(), time(hour, minute)).isoformat()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>GEX Strat: {escape(" ".join(argv[1:]))}</Description></RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek><DaysOfWeek>{weekdays}</DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(argv[0])}</Command>
      <Arguments>{escape(subprocess.list2cmdline(argv[1:]))}</Arguments>
      <WorkingDirectory>{escape(str(ROOT))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def cron_line(hour: int, minute: int, days: list[int], argv: list[str]) -> str:
    """crontab entry (cron weekdays: 0 = Sunday)."""
    dow = ",".join(str((d + 1) % 7) for d in days)
    return f"{minute} {hour} * * {dow} cd {shlex.quote(str(ROOT))} && {shlex.join(argv)}"


def _launchd_path(name: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"com.gexstrat.{name}.plist"


def _task_name(name: str) -> str:
    return f"GEX Strat {name}"


def install() -> int:
    """Schedule the archive jobs with this machine's scheduler, in its local time."""
    LOG.parent.mkdir(exist_ok=True)
    for name, (at, days, args) in JOBS.items():
        hour, minute, local_days = local_schedule(at, days)
        argv = [_interpreter(), str(CLI), *args, "--log"]
        if sys.platform == "darwin":
            path = _launchd_path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
            path.write_text(plist(f"com.gexstrat.{name}", hour, minute, local_days, argv), encoding="utf-8")
            subprocess.run(["launchctl", "load", str(path)], check=True)
            where = str(path)
        elif sys.platform == "win32":
            with tempfile.TemporaryDirectory() as tmp:
                xml = Path(tmp) / "task.xml"
                xml.write_text(task_xml(hour, minute, local_days, argv), encoding="utf-16")
                subprocess.run(["schtasks", "/Create", "/TN", _task_name(name), "/XML", str(xml), "/F"], check=True)
            where = f"Task Scheduler, '{_task_name(name)}'"
        else:
            print("no scheduler set up on this system; add to `crontab -e`:")
            print("  " + cron_line(hour, minute, local_days, argv))
            continue
        print(f"{' '.join(args)}: {hour}:{minute:02d} local time ({at[0]}:{at[1]:02d} New York), "
              f"{', '.join(DAY_NAMES[d][:3] for d in local_days)} -> {where}")
    print(f"log: {LOG}. The machine must be on at those times; a missed run starts when it wakes up.")
    return 0


def uninstall() -> int:
    """Remove the scheduled archive jobs."""
    for name in JOBS:
        if sys.platform == "darwin":
            path = _launchd_path(name)
            subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
            path.unlink(missing_ok=True)
            print(f"removed {path}")
        elif sys.platform == "win32":
            subprocess.run(["schtasks", "/Delete", "/TN", _task_name(name), "/F"])
        else:
            print("remove the GEX Strat lines from `crontab -e`")
            break
    return 0


def archive(symbols: list[str], intraday: bool, force: bool, catch_up: bool, preopen: bool = False) -> int:
    """Archive each symbol; one failing does not stop the others.

    Exit code 1 if any symbol failed, or, outside catch-up and pre-open modes, was skipped
    (not a close, not quoted enough): a missing day must show in the log, not pass silently.
    """
    errors = 0
    for sym in symbols:
        stamp = f"{datetime.now():%Y-%m-%d %H:%M}"
        try:
            status, message = study.archive(sym.upper(), intraday=intraday, force=force, preopen=preopen)
        except Exception as exc:
            errors += 1
            print(f"{stamp} {sym}: FAILED {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            continue
        if status == "skipped" and not (catch_up or preopen):
            errors += 1
            message = "NOT ARCHIVED " + message
        print(f"{stamp} {message}", flush=True)
    return 1 if errors else 0


# --------------------------------------------------------------------------- backtest inputs

SQUEEZE_URL = "https://squeezemetrics.com/monitor/static/DIX.csv"
PRICE_COLUMNS = ("spx_open", "spx_high", "spx_low", "spx_close", "spxt", "us3m_pct", "vix", "ux1", "ux2")


def _provenance(path: Path, **info) -> None:
    """Write `<file>.provenance.json`: where the file came from, when, and its md5."""
    info["md5"] = hashlib.md5(path.read_bytes()).hexdigest()
    info["written_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    Path(f"{path}.provenance.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")


def fetch_squeeze() -> int:
    """Download SqueezeMetrics' public daily SPX GEX and report revisions of past values.

    Their methodology, not gex_engine's. Check their terms before redistributing the file.
    """
    req = urllib.request.Request(SQUEEZE_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    path = config.DATA_DIR / backtest.SQUEEZE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        old = pd.read_csv(path, index_col="date")
        new = pd.read_csv(io.BytesIO(raw), index_col="date")
        common = old.index.intersection(new.index)
        revised = int(((old.loc[common, "gex"] - new.loc[common, "gex"]).abs() > 1.0).sum())
        print(f"squeezemetrics: {revised} past values revised since the previous download")
    path.write_bytes(raw)
    _provenance(path, source=SQUEEZE_URL, note="SqueezeMetrics methodology; date = session of the GEX")
    print("written", path)
    return 0


def copy_prices(source: Path, label: str) -> int:
    """Check a daily price file (date + PRICE_COLUMNS, closes only) and copy it into data/market/."""
    prices = pd.read_csv(source, index_col="date", parse_dates=True)
    missing = [c for c in PRICE_COLUMNS if c not in prices.columns]
    if missing:
        print(f"{source}: missing columns {missing}; expected date, {', '.join(PRICE_COLUMNS)}", file=sys.stderr)
        return 1
    path = config.DATA_DIR / backtest.PRICES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, path)
    _provenance(path, source=label or source.name, columns=list(PRICE_COLUMNS),
                first=str(prices.index.min().date()), last=str(prices.index.max().date()))
    print("written", path)
    return 0


# --------------------------------------------------------------------------- end-of-day history

def fetch_eod_sample(url: str = config.EOD_SAMPLE_URL, target: Path = config.EOD_DIR) -> int:
    """Download the vendor's free sample and unpack its daily files into `target`, checked.

    Only the CSVs, the manifest, README and LICENSE are taken (the sample's own script is
    not); every CSV must match the SHA-256 of the manifest. The paid archive is delivered in
    the same layout: point its download at the same folder, then run eod-import.
    """
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive_path = Path(tmp) / "sample.zip"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=600) as resp, open(archive_path, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        with zipfile.ZipFile(archive_path) as z:
            manifest = json.loads(z.read("day_by_date/manifest.json"))
            expected = {f["name"]: f["sha256"] for f in manifest["files"]}
            for name in ("README.md", "LICENSE.txt"):
                (target.parent / name).write_bytes(z.read(name))
            (target / "manifest.json").write_bytes(z.read("day_by_date/manifest.json"))
            bad = []
            for name, sha in expected.items():
                content = z.read(f"day_by_date/{name}")
                if hashlib.sha256(content).hexdigest() != sha:
                    bad.append(name)
                    continue
                (target / name).write_bytes(content)
    print(f"{len(expected) - len(bad)} daily files in {target}" + (f"; checksum mismatch, not written: {bad}" if bad else ""))
    print("licence: data/vendor/historicaldata/LICENSE.txt (no redistribution: the folder stays out of git)")
    return 1 if bad else 0


def import_eod(symbols: list[str]) -> int:
    """Recompute data/history_eod.csv from the end-of-day files."""
    if not data.eod_files():
        print(f"no end-of-day files in {config.EOD_DIR}; run `cli.py eod-sample` first", file=sys.stderr)
        return 1
    hist, failed = study.import_eod([s.upper() for s in symbols])
    print(f"history_eod.csv: {len(hist)} rows, {hist['date'].min()} to {hist['date'].max()}" if len(hist) else "no rows")
    for line in failed:
        print(f"not imported: {line}", file=sys.stderr)
    return 1 if failed else 0


# --------------------------------------------------------------------------- setup check

def check() -> int:
    """Report whether this machine can run everything; send the output when something fails.

    Covers what differs from one set-up to another: interpreter and package versions, the
    time zone database, the network path to CBOE (proxy, TLS inspection), the data on disk
    and where the schedule would put the archive in local time.
    """
    failures = 0

    def report(ok: bool, what: str, detail: str = "") -> None:
        nonlocal failures
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'}  {what}" + (f": {detail}" if detail else ""))

    report(sys.version_info >= (3, 11), "Python", f"{sys.version.split()[0]} at {sys.executable} (3.11 or later)")
    for req in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        name, _, pinned = req.split("#")[0].strip().partition("==")
        if name and pinned:
            try:
                found = metadata.version(name)
            except metadata.PackageNotFoundError:
                found = "missing"
            report(found == pinned, name, found if found == pinned else f"{found}, requirements.txt pins {pinned}")
    now = datetime.now(timezone.utc)
    report(True, "clock", f"{now.astimezone(config.MARKET_TZ):%Y-%m-%d %H:%M} New York, "
                          f"{now.astimezone():%H:%M} local ({now.astimezone().tzname()})")
    for path in (study.HISTORY_FILE, backtest.TRIALS_FILE, backtest.SQUEEZE_FILE):
        report((config.DATA_DIR / path).exists(), f"data/{Path(path).as_posix()}")
    if not (config.DATA_DIR / backtest.PRICES_FILE).exists():      # licensed, not a set-up failure
        print(f"info  data/{backtest.PRICES_FILE.as_posix()}: absent (licensed, not in the repository); "
              "only the Backtest page needs it")
    days = data.eod_files()
    print(f"info  end-of-day vendor files: {len(days)}"
          + (f" ({days[0].name[:10]} to {days[-1].name[:10]})" if days else " (optional: cli.py eod-sample)"))
    try:
        with tempfile.TemporaryFile(dir=config.DATA_DIR):
            pass
        report(True, "data/ writable")
    except OSError as exc:
        report(False, "data/ writable", str(exc))
    try:
        chain = data.parse(data.download("SPX"), "SPX")
        report(True, "CBOE download", f"SPX as of {chain.asof:%Y-%m-%d %H:%M} New York, {len(chain.contracts)} contracts")
    except Exception as exc:
        proxies = urllib.request.getproxies()
        report(False, "CBOE download", f"{type(exc).__name__}: {exc}; proxies seen: {proxies or 'none'} "
                                       "(set HTTPS_PROXY; for TLS inspection, SSL_CERT_FILE)")
    for name, (at, days, args) in JOBS.items():
        hour, minute, local_days = local_schedule(at, days)
        report(True, f"schedule {' '.join(args)}", f"{hour}:{minute:02d} local, "
                                                    f"{', '.join(DAY_NAMES[d][:3] for d in local_days)}")
    print("all good" if not failures else f"{failures} problem(s)")
    return 1 if failures else 0


# --------------------------------------------------------------------------- entry point

def main(argv=None) -> int:
    """Parse the command line and run the sub-command (see the module docstring)."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="check this machine's set-up")
    a = sub.add_parser("archive", help="archive closing chains (or intraday snapshots)")
    a.add_argument("symbols", nargs="*", default=list(config.SYMBOLS))
    a.add_argument("--intraday", action="store_true", help="timestamped snapshot during the session")
    a.add_argument("--force", action="store_true", help="overwrite a close already archived")
    a.add_argument("--catch-up", action="store_true", help="fill missed days; skips are not errors")
    a.add_argument("--preopen", action="store_true", help="keep the close republished with the overnight open interest")
    a.add_argument("--log", action="store_true", help="append the output to data/archive.log (scheduled runs)")
    sub.add_parser("rebuild", help="recompute data/history.csv from data/raw")
    sub.add_parser("install", help="schedule the archive (macOS, Windows; prints a crontab line elsewhere)")
    sub.add_parser("uninstall", help="remove the schedule")
    b = sub.add_parser("backtest-data", help="refresh the backtest's inputs")
    b.add_argument("--prices", type=Path, help=f"CSV with columns date, {', '.join(PRICE_COLUMNS)}")
    b.add_argument("--label", default="", help="provenance label of the price file (e.g. its vendor)")
    b.add_argument("--squeeze", action="store_true", help="download SqueezeMetrics' GEX history")
    sub.add_parser("eod-sample", help="download the free end-of-day sample (historicaldata.net, 2022 H2)")
    e = sub.add_parser("eod-import", help="end-of-day files -> data/history_eod.csv")
    e.add_argument("symbols", nargs="*", default=["SPX", "SPY"])
    args = ap.parse_args(argv)

    if args.command == "check":
        return check()
    if args.command == "archive":
        if args.log:               # in Python, not a shell redirect: the same on every scheduler
            LOG.parent.mkdir(exist_ok=True)
            sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8", buffering=1)
        if args.intraday and args.preopen:
            ap.error("--intraday and --preopen are exclusive")
        return archive(args.symbols, args.intraday, args.force, args.catch_up, args.preopen)
    if args.command == "rebuild":
        hist, failed = study.rebuild_history()
        print(f"history rebuilt: {len(hist)} rows")
        for line in failed:
            print(f"kept the previous row for {line}", file=sys.stderr)
        return 0
    if args.command == "eod-sample":
        return fetch_eod_sample()
    if args.command == "eod-import":
        return import_eod(args.symbols)
    if args.command == "install":
        return install()
    if args.command == "uninstall":
        return uninstall()
    status = 0
    if args.squeeze:
        status |= fetch_squeeze()
    if args.prices:
        status |= copy_prices(args.prices, args.label)
    if not (args.squeeze or args.prices):
        ap.error("backtest-data needs --prices FILE and/or --squeeze")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
