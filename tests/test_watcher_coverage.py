"""The watcher's day record and the evening report's coverage line (26 Sep 2026, review
D6): a day the watcher was down, or a cycle stalled, in market hours is named as a CANDIDATE
partial day - never silently an ordinary flat test day. How it counts is Rick's call."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from asxbot.arena import heartbeat as H
from asxbot.arena.report import watcher_coverage

SYD = ZoneInfo("Australia/Sydney")
MON = date(2026, 9, 28)


def at(h, m):
    return datetime(2026, 9, 28, h, m, tzinfo=SYD)


def beat_through(hb, start, end, step_s=60, skip=None):
    t = start
    while t <= end:
        if not (skip and skip[0] <= t < skip[1]):
            hb.session_beat(t)
        t += timedelta(seconds=step_s)


def test_a_full_day_is_one_line_and_not_partial(tmp_path):
    hb = H.Heartbeat(tmp_path)
    beat_through(hb, at(7, 30), at(19, 31))
    hb.session_beat(at(19, 31), final=True)
    cov = watcher_coverage(tmp_path, MON)
    assert not cov["candidate_partial"] and "ran 07:30-19:31, no gap" in cov["line"]


def test_a_crash_in_market_hours_is_a_candidate_partial_day(tmp_path):
    hb = H.Heartbeat(tmp_path)
    beat_through(hb, at(7, 30), at(19, 31), skip=(at(11, 2), at(11, 40)))
    cov = watcher_coverage(tmp_path, MON)
    assert cov["candidate_partial"] and "DOWN 11:01-11:40 (39 min)" in cov["line"]


def test_a_stalled_cycle_is_recorded(tmp_path):
    hb = H.Heartbeat(tmp_path)
    beat_through(hb, at(7, 30), at(10, 50))
    hb.cycle_start(at(10, 52))
    beat_through(hb, at(10, 53), at(11, 17))
    hb.cycle_end(at(11, 17))
    beat_through(hb, at(11, 18), at(19, 31))
    cov = watcher_coverage(tmp_path, MON)
    assert cov["candidate_partial"] and "a cycle stalled 10:52-11:17" in cov["line"]


def test_a_quiet_wait_is_not_a_stall(tmp_path):
    hb = H.Heartbeat(tmp_path)
    hb.cycle_start(at(10, 0))
    hb.quiet_until = at(10, 30)
    beat_through(hb, at(10, 0), at(10, 20))
    assert H.read_session(tmp_path, MON)["stalls"] == []


def test_no_record_on_a_trading_day_from_monday_is_named(tmp_path):
    cov = watcher_coverage(tmp_path, MON)
    assert cov["candidate_partial"] and "NO RECORD" in cov["line"]
    assert watcher_coverage(tmp_path, date(2026, 9, 25))["line"] == ""  # before the record
    assert watcher_coverage(tmp_path, date(2026, 9, 26))["line"] == ""  # a Saturday


def test_a_restart_keeps_one_record_for_the_day(tmp_path):
    hb = H.Heartbeat(tmp_path)
    beat_through(hb, at(7, 30), at(10, 30))
    hb.session_beat(at(10, 30), final=True)
    hb2 = H.Heartbeat(tmp_path)
    hb2.pid = hb.pid + 1
    beat_through(hb2, at(10, 36), at(19, 31))
    s = H.read_session(tmp_path, MON)
    assert s["first_beat"].startswith("2026-09-28T07:30") and len(s["pids"]) == 2
    assert s["gaps"] == [["2026-09-28T10:30:00+10:00", "2026-09-28T10:36:00+10:00"]]
