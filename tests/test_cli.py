"""Archive schedule on any machine: local times from New York times, and the job each scheduler gets."""

import plistlib
import sys
import xml.etree.ElementTree as ET
from datetime import date
from zoneinfo import ZoneInfo

import pytest

import cli

START = date(2026, 10, 20)            # the coming year includes both daylight-saving mismatches
EVENING, CATCH_UP, PREOPEN = cli.JOBS["archive"], cli.JOBS["archive.catchup"], cli.JOBS["archive.preopen"]


@pytest.mark.parametrize("zone,job,expected", [
    ("Europe/Paris", EVENING, (22, 30, [0, 1, 2, 3, 4])),       # the times used since the start
    ("Europe/Paris", CATCH_UP, (7, 30, [0, 1, 2, 3, 4, 5])),
    ("Europe/Paris", PREOPEN, (13, 45, [0, 1, 2, 3, 4])),
    ("America/New_York", EVENING, (16, 30, [0, 1, 2, 3, 4])),
    ("Europe/London", EVENING, (21, 30, [0, 1, 2, 3, 4])),
    ("Asia/Tokyo", EVENING, (6, 30, [1, 2, 3, 4, 5])),          # next morning: Tuesday to Saturday
    ("America/Los_Angeles", CATCH_UP, (22, 30, [0, 1, 2, 3, 4, 6])),   # the evening before
])
def test_local_schedule(zone, job, expected):
    at, days, _ = job
    assert cli.local_schedule(at, days, START, ZoneInfo(zone)) == expected


@pytest.mark.parametrize("zone", ["Europe/Paris", "Europe/London", "Asia/Tokyo", "America/Chicago"])
@pytest.mark.parametrize("job,window", [(EVENING, ((16, 15), (17, 30))),     # after the options close
                                        (PREOPEN, ((7, 0), (9, 29)))])       # overnight OI in, before the open
def test_runs_stay_in_their_new_york_window(zone, job, window):
    """Wherever the machine is, on every day of the year, each run lands in its New York window."""
    from datetime import datetime, time, timedelta
    tz = ZoneInfo(zone)
    (h0, m0), days, _ = job
    hour, minute, _ = cli.local_schedule((h0, m0), days, START, tz)
    for i in range(366):
        d = START + timedelta(days=i)
        ny = datetime.combine(d, time(h0, m0), tzinfo=cli.config.MARKET_TZ)
        local_day = ny.astimezone(tz).date()
        run = datetime.combine(local_day, time(hour, minute), tzinfo=tz).astimezone(cli.config.MARKET_TZ)
        assert window[0] <= (run.hour, run.minute) <= window[1]


ARGV = [r"C:\Users\me\My Projects\GEX Strat\.venv\Scripts\pythonw.exe", r"C:\Users\me\My Projects\GEX Strat\cli.py",
        "archive", "--catch-up", "--log"]


def test_windows_task_runs_late_when_missed_and_quotes_paths():
    root = ET.fromstring(cli.task_xml(7, 30, [0, 1, 2, 3, 4, 5], ARGV).split("?>", 1)[1])
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find("t:Settings/t:StartWhenAvailable", ns).text == "true"
    days = root.find("t:Triggers/t:CalendarTrigger/t:ScheduleByWeek/t:DaysOfWeek", ns)
    assert [d.tag.split("}")[1] for d in days] == list(cli.DAY_NAMES[:6])
    assert root.find("t:Triggers/t:CalendarTrigger/t:StartBoundary", ns).text.endswith("T07:30:00")
    exe = root.find("t:Actions/t:Exec", ns)
    assert exe.find("t:Command", ns).text == ARGV[0]
    assert exe.find("t:Arguments", ns).text == '"C:\\Users\\me\\My Projects\\GEX Strat\\cli.py" archive --catch-up --log'


def test_launchd_job():
    job = plistlib.loads(cli.plist("com.gexstrat.archive", 22, 30, [0, 4], ARGV).encode())
    assert job["ProgramArguments"] == ARGV
    assert [(e["Weekday"], e["Hour"], e["Minute"]) for e in job["StartCalendarInterval"]] == [(1, 22, 30), (5, 22, 30)]


def test_cron_line():
    line = cli.cron_line(22, 30, [0, 1, 2, 3, 4, 5], ["/opt/py", "/home/me/GEX Strat/cli.py", "archive", "--log"])
    assert line.startswith("30 22 * * 1,2,3,4,5,6 cd '")
    assert line.endswith("&& /opt/py '/home/me/GEX Strat/cli.py' archive --log")


def test_log_option_appends_to_the_log(tmp_path, monkeypatch):
    """Scheduled runs log from Python, the same on every scheduler (no shell redirect)."""
    monkeypatch.setattr(cli, "LOG", tmp_path / "archive.log")
    monkeypatch.setattr(cli.study, "archive", lambda sym, **_: ("archived", f"{sym}: close archived"))
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    for _ in range(2):
        assert cli.main(["archive", "SPX", "--log"]) == 0
        sys.stdout.close()
    lines = (tmp_path / "archive.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and all(line.endswith("SPX: close archived") for line in lines)
