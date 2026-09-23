"""The watcher's heartbeat, and the watchdog that reads it from outside.

On 23 Sep 2026 the watcher's console window was closed at 13:32:51 and the process died
mid-poll. Nothing inside the watcher could report that, because everything inside it died
too. These tests pin what the watchdog calls an outage, and that it says so once.
"""

import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from asxbot.arena import heartbeat as H
from asxbot.arena import watchdog as W
from asxbot.config import load_config

SYD = ZoneInfo("Australia/Sydney")
DAY = datetime(2026, 9, 23, tzinfo=SYD)  # a Wednesday
WINDOW = (DAY.replace(hour=7, minute=33), DAY.replace(hour=19, minute=25))


def at(h, m, s=0):
    return DAY.replace(hour=h, minute=m, second=s)


def hb(**kw):
    """A heartbeat as the watcher writes it: healthy at 13:32:51 unless told otherwise."""
    base = {
        "pid": 4242, "started": at(11, 37, 22).isoformat(), "stop_at": at(19, 25).isoformat(),
        "beat": at(13, 32, 51).isoformat(), "last_activity": at(13, 32, 51).isoformat(),
        "quiet_until": None, "quiet_why": "", "state": "running",
    }  # fmt: skip
    base.update(kw)
    return base


def alive(pid):
    return True


def dead(pid):
    return False


@pytest.fixture
def cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )


# -- what counts as an outage ---------------------------------------------------------
def test_the_23_september_death_is_reported_as_down():
    """The process is gone and the log stops mid-poll with no error."""
    v = W.assess(hb(), at(13, 37), WINDOW, dead)
    assert v.expected and not v.ok and v.problem == "down"
    assert "pid 4242" in v.detail and "13:32:51" in v.detail


def test_a_healthy_watcher_is_ok():
    assert W.assess(hb(), at(13, 34), WINDOW, alive).ok


@pytest.mark.parametrize("now", [at(7, 31), at(19, 25), at(21, 0)])
def test_nothing_is_expected_outside_the_watchers_hours(now):
    v = W.assess(None, now, WINDOW, dead)
    assert v.ok and not v.expected


def test_nothing_is_expected_on_a_day_the_watcher_does_not_run():
    v = W.assess(None, at(10, 0), None, dead)
    assert v.ok and not v.expected


def test_no_heartbeat_at_all_is_down():
    v = W.assess(None, at(8, 0), WINDOW, alive)
    assert v.problem == "down" and "no watcher heartbeat" in v.detail


def test_yesterdays_heartbeat_means_it_has_not_started_today():
    y = hb(started=(at(7, 30) - timedelta(days=1)).isoformat(), state="stopped")
    v = W.assess(y, at(8, 0), WINDOW, alive)
    assert v.problem == "down" and "has not started today" in v.detail


@pytest.mark.parametrize("state", ["crashed", "stopped", "interrupted"])
def test_a_watcher_that_exited_during_its_hours_is_down(state):
    v = W.assess(hb(state=state), at(13, 34), WINDOW, alive)
    assert v.problem == "down" and state in v.detail


def test_a_stale_heartbeat_from_a_live_process_is_down_but_looked_at_again():
    v = W.assess(hb(), at(13, 38), WINDOW, alive)
    assert v.problem == "down" and v.recheck and "hung or suspended" in v.detail


def test_a_silent_log_is_reported_when_no_wait_was_announced():
    h = hb(beat=at(13, 45).isoformat(), last_activity=at(13, 39).isoformat())
    v = W.assess(h, at(13, 45), WINDOW, alive)
    assert v.problem == "silent" and "13:39:00" in v.detail


def test_a_silent_log_during_an_announced_wait_is_not_an_outage():
    """A model call may run to its timeout (600 s) with nothing logged."""
    h = hb(beat=at(13, 45).isoformat(), last_activity=at(13, 39).isoformat(),
           quiet_until=at(13, 51).isoformat(), quiet_why="waiting on trader-decider")  # fmt: skip
    assert W.assess(h, at(13, 45), WINDOW, alive).ok
    h["beat"] = at(13, 52).isoformat()  # still alive; the wait has now run out
    v = W.assess(h, at(13, 52), WINDOW, alive)
    assert v.problem == "silent" and "waiting on trader-decider" in v.detail


def test_the_watchers_hours_follow_the_trading_calendar(cfg):
    thu = datetime(2026, 9, 24, 12, 0, tzinfo=SYD)
    start, stop = W.expected_window(cfg, thu)
    assert (start.hour, start.minute) == (7, 33) and (stop.hour, stop.minute) == (19, 25)
    assert W.expected_window(cfg, datetime(2026, 9, 26, 12, 0, tzinfo=SYD)) is None  # Sat
    dst = W.expected_window(cfg, datetime(2026, 10, 7, 12, 0, tzinfo=SYD))
    assert (dst[1].hour, dst[1].minute) == (20, 25)  # daylight saving from 4 Oct


# -- one alert per outage, one on recovery ----------------------------------------------
class Phone:
    def __init__(self, works=True):
        self.works = works
        self.sent = []

    def __call__(self, text):
        if self.works:
            self.sent.append(text)
        return self.works


def run(cfg, monkeypatch, beat, now, phone, alive_fn=dead, sleeps=None):
    monkeypatch.setattr(W, "expected_window", lambda cfg, now: WINDOW)
    return W.run_once(cfg, now, phone, hb_reader=lambda: beat, alive=alive_fn,
                      sleep=(sleeps.append if sleeps is not None else lambda s: None))  # fmt: skip


def test_an_outage_is_reported_once_and_its_recovery_once(cfg, monkeypatch):
    phone = Phone()
    assert run(cfg, monkeypatch, hb(), at(13, 35), phone) == "down: alert sent"
    assert run(cfg, monkeypatch, hb(), at(13, 40), phone).startswith("down: already reported")
    assert run(cfg, monkeypatch, hb(), at(13, 45), phone).startswith("down: already reported")
    assert len(phone.sent) == 1 and "WATCHER DOWN" in phone.sent[0]

    back = hb(pid=5151, started=at(13, 47).isoformat(), beat=at(13, 49).isoformat(),
              last_activity=at(13, 49).isoformat())  # fmt: skip
    assert run(cfg, monkeypatch, back, at(13, 50), phone, alive) == "back up: message sent"
    assert run(cfg, monkeypatch, back, at(13, 50), phone, alive) == "ok"
    assert len(phone.sent) == 2 and "BACK UP" in phone.sent[1]
    assert "pid 5151" in phone.sent[1] and "13:35:00" in phone.sent[1]
    state = json.loads((cfg.data_dir / "arena" / W.STATE_FILE).read_text(encoding="utf-8"))
    assert state["outage"] is None and state["last_result"] == "ok"
    log = (cfg.data_dir / "logs" / W.LOG_FILE).read_text(encoding="utf-8")
    assert "DOWN:" in log and "BACK UP:" in log


def test_an_alert_that_could_not_be_sent_is_tried_again(cfg, monkeypatch):
    phone = Phone(works=False)
    assert "NOT sent" in run(cfg, monkeypatch, hb(), at(13, 35), phone)
    phone.works = True
    assert run(cfg, monkeypatch, hb(), at(13, 40), phone) == "down: alert sent"
    assert len(phone.sent) == 1


def test_a_pc_waking_from_sleep_is_looked_at_again_before_any_alert(cfg, monkeypatch):
    """Stale heartbeat, live process: wait 90 s and read it again."""
    beats = iter([hb(), hb(beat=at(13, 39, 30).isoformat(),
                           last_activity=at(13, 39, 30).isoformat())])  # fmt: skip
    monkeypatch.setattr(W, "expected_window", lambda cfg, now: WINDOW)
    phone, sleeps = Phone(), []
    out = W.run_once(cfg, at(13, 38), phone, hb_reader=lambda: next(beats), alive=alive,
                     sleep=sleeps.append)  # fmt: skip
    assert out == "ok" and phone.sent == [] and sleeps == [W.RECHECK_S]


def test_the_watchdog_sends_nothing_outside_the_watchers_hours(cfg, monkeypatch):
    phone = Phone()
    assert run(cfg, monkeypatch, None, at(20, 0), phone) == "not expected to run"
    assert phone.sent == []


# -- the heartbeat itself -----------------------------------------------------------------
def test_the_heartbeat_records_the_process_its_log_and_its_end(tmp_path):
    beat = H.Heartbeat(tmp_path, stop_at=at(19, 25), beat_s=3600).start()
    try:
        first = H.read(tmp_path)
        assert first["state"] == "running" and first["pid"] == beat.pid
        before = beat.last_activity
        logging.getLogger("asxbot.arena.watch").warning("a line from the watcher")
        assert beat.last_activity >= before
        with H.quiet(600, "waiting on trader-decider"):
            assert beat.quiet_why == "waiting on trader-decider"
            assert beat.write() and H.read(tmp_path)["quiet_until"] is not None
        assert beat.quiet_until is None  # cleared when the wait is over
    finally:
        beat.stop("stopped")
    assert H.read(tmp_path)["state"] == "stopped"
    assert H._current is None


def test_quiet_without_a_running_watcher_does_nothing():
    assert H._current is None
    with H.quiet(10, "nothing"):
        pass


def test_the_watchers_sleeps_are_announced(monkeypatch, tmp_path):
    from asxbot.arena import watch

    beat = H.Heartbeat(tmp_path, beat_s=3600).start()
    seen = []
    monkeypatch.setattr(watch.time, "sleep", lambda s: seen.append((s, beat.quiet_why)))
    try:
        watch._sleep(300, "outside announcement hours")
    finally:
        beat.stop()
    assert seen == [(300, "outside announcement hours")]


def test_agent_calls_are_announced(monkeypatch, tmp_path):
    """A model call can take up to its timeout; the watchdog must not call that silence."""
    import subprocess

    from asxbot.arena import agents

    beat = H.Heartbeat(tmp_path, beat_s=3600).start()
    seen = []

    def fake_run(cmd, **kw):
        seen.append(beat.quiet_why)
        return subprocess.CompletedProcess(cmd, 0, stdout='{"status": "ok"}', stderr="")

    monkeypatch.setattr(agents, "_openclaw_bin", lambda: "openclaw")
    monkeypatch.setattr(agents.subprocess, "run", fake_run)
    try:
        agents.call_agent("trader-decider", "hello", timeout_s=600)
    finally:
        beat.stop()
    assert seen == ["waiting on trader-decider"]
