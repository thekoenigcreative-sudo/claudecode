"""The operations review of 26 Sep 2026: one or more tests per finding, each written to fail
on the code before the fix (noted where it cannot). No network, no scheduled task, no
Gateway, no Telegram: every outside thing is a stand-in.

E2 Drive writes in the loop   E3 start a dead watcher     E4 warm-up waits for Drive
E5 bounded asx.com.au waits   C6 fast stages first        A5 pre-open look needs IBKR
A7 rotation and pin order     E7 prune                    E8 the watcher's own log
E9 PDF check reads 5 bytes    E10 one watcher             E11 lock end = stop time
E14 late evening run          I4 stuck cycle              I6 parse flag clears
I8 doctor speaks for IBKR     I12 grouped errors          H1-H3 guarded commands
C12 last poll after 19:30
"""

import importlib.machinery
import importlib.util
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from asxbot.announcements.model import Announcement
from asxbot.config import load_config

SYD = ZoneInfo("Australia/Sydney")
REPO = Path(__file__).resolve().parents[1]
MON = date(2026, 9, 28)  # a Monday the ASX trades


def at(h, m, s=0, day=MON):
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=SYD)


@pytest.fixture
def cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "live_provider": "ibkr",
                          "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )  # fmt: skip


def _ann(code="AAA", ids="00000001", when=None, ps=True):
    return Announcement(code, (when or at(8, 0)).replace(tzinfo=None), f"{code} news", ps, ids,
                        f"https://www.asx.com.au/{ids}.pdf")  # fmt: skip


def _load_script(name: str, alias: str):
    path = REPO / "scripts" / name
    loader = importlib.machinery.SourceFileLoader(alias, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# =========================================================================================
# E2: nothing in the loop that writes to Drive can end the watcher; C6: fast stages first
# =========================================================================================
def test_notifier_send_never_raises_when_the_event_log_cannot_be_written(cfg, monkeypatch):
    """Fails on the old code: the arena_notify append sat outside send's try."""
    from asxbot.arena.notify import Notifier

    n = Notifier(cfg, enabled=False)

    def refused(*a, **k):
        raise OSError("Drive is holding the file")

    monkeypatch.setattr(n.events, "append", refused)
    assert n.send("a fill") is False
    assert n.send("") is False  # an empty message used to IndexError in the warning


class _Poller:
    queue: list = []
    last = None

    def __init__(self, *a, **k):
        self.polls = []
        self.kwargs = k
        _Poller.last = self

    def poll_once(self, now):
        self.polls.append(now)
        out, _Poller.queue = list(_Poller.queue), []
        return out

    def fetch_pdf(self, a, stage="poll"):
        return None

    def retry_pdfs(self, now=None, budget_s=None):
        _Poller.calls.append("retry")
        return []


def _harness(cfg, monkeypatch, calls, save_fails=True):
    """watch() with every stage a recorder, the digest and the handled list failing on
    Drive, and one new announcement on the page."""
    from asxbot.announcements import live as LV
    from asxbot.arena import watch as W

    _Poller.calls = calls
    _Poller.queue = [_ann()]
    monkeypatch.setattr(LV, "LivePoller", _Poller)
    monkeypatch.setattr(LV, "is_trading_day", lambda d: True)
    monkeypatch.setattr(LV, "in_hours", lambda now, s, e: True)
    monkeypatch.setattr(W, "_feed_check", lambda arena, now: calls.append("feed"))
    monkeypatch.setattr(W, "_work_all", lambda arena, pbs: calls.append("work"))
    monkeypatch.setattr(W, "_v2_bot_now", lambda arena, pb: calls.append("v2bot"))
    monkeypatch.setattr(W, "_flat_sweeps", lambda arena, pbs, now: calls.append("flat"))
    monkeypatch.setattr(W, "catch_up", lambda arena, pb, now: calls.append("catch_up") or [])
    monkeypatch.setattr(W, "handle_announcement",
                        lambda arena, pb, a, now: calls.append(f"handle {a.code}"))
    monkeypatch.setattr(W, "_intraday", lambda arena, pb, others: calls.append("intraday"))
    monkeypatch.setattr(W, "_refresh_short_universe", lambda arena, now: None)
    monkeypatch.setattr(W, "horizon_exit", lambda arena, pb, now: None)
    monkeypatch.setattr(W.selfcheck, "report", lambda arena, pb, now: None)
    monkeypatch.setattr(W, "session_summary_text", lambda arena, pb, now: "x")

    if save_fails:
        def no_save(*a, **k):
            raise OSError("Drive refused the handled list")

        monkeypatch.setattr(W, "_save_handled", no_save)

    def no_digest(*a, **k):
        raise OSError("Drive refused pass_digest.json")

    alert = SimpleNamespace(flush_passes=no_digest, session_summary=lambda text, now: False)
    monkeypatch.setattr(W.notify, "get", lambda arena: alert)
    pb = SimpleNamespace(key="asx_announcements_v2", version=2, level=SimpleNamespace(number=1),
                         raw={}, flat_at_close=True)  # fmt: skip
    arena = SimpleNamespace(cfg=cfg, universe={"AAA"}, broker=SimpleNamespace())
    return W, arena, pb


def test_drive_failures_in_the_loop_do_not_end_the_watcher_and_fast_stages_come_first(
    cfg, monkeypatch
):
    """E2 and C6. Fails on the old code at the first flush_passes (outside any try)."""
    calls: list = []
    W, arena, pb = _harness(cfg, monkeypatch, calls)
    W.watch(arena, pb, once=True)
    fast = ["work", "v2bot", "flat"]
    assert calls == [
        "feed", *fast, "catch_up",  # the feed before the catch-up (A5), stops before it too
        *fast, "handle AAA", *fast,  # the fast stages before and after each announcement
        "work", "intraday", "retry", *fast,
    ]


def test_the_v2_rule_bot_runs_from_its_measure_time_at_the_top_of_the_cycle(monkeypatch):
    from asxbot.arena import v2_flow
    from asxbot.arena import watch as W

    ran = []
    monkeypatch.setattr(v2_flow, "v2_bot_cycle", lambda arena, pb, view, now: ran.append(now))
    monkeypatch.setattr(W, "day_view", lambda arena, now: SimpleNamespace(day=MON))
    pb = SimpleNamespace(version=2, yardstick=lambda: {"measure_at": "10:30"})
    clock = {"now": at(10, 29, 50)}

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    monkeypatch.setattr(W, "datetime", FakeDT)
    W._v2_bot_now(None, pb)
    assert ran == []
    clock["now"] = at(10, 30, 5)
    W._v2_bot_now(None, pb)
    assert ran == [at(10, 30, 5)]
    W._v2_bot_now(None, SimpleNamespace(version=1))  # v1 has no such bot
    assert len(ran) == 1


def test_the_fast_stages_run_between_the_reaction_looks_and_the_day_trader(monkeypatch):
    from asxbot.arena import daytrader, intraday, v2_flow
    from asxbot.arena import watch as W

    calls = []
    monkeypatch.setattr(W, "day_view", lambda arena, now: SimpleNamespace(day=MON))
    monkeypatch.setattr(intraday, "entries_allowed", lambda view, now: (True, ""))
    monkeypatch.setattr(v2_flow, "seed_queue", lambda *a, **k: calls.append("seed"))
    monkeypatch.setattr(v2_flow, "v2_bot_cycle", lambda *a, **k: calls.append("v2bot"))
    monkeypatch.setattr(v2_flow, "reaction_looks", lambda *a, **k: calls.append("looks"))
    monkeypatch.setattr(daytrader, "cycle", lambda *a, **k: calls.append("daytrader"))
    monkeypatch.setattr(W, "_fast_stages", lambda arena, pb, others: calls.append("fast"))
    pb = SimpleNamespace(version=2)
    W._intraday(None, pb, (SimpleNamespace(key="asx_daytrader"),))
    assert calls == ["seed", "v2bot", "looks", "fast", "daytrader"]


def test_a_handled_list_drive_cannot_read_does_not_stop_the_start(cfg):
    from asxbot.arena import watch as W

    p = W._handled_path(cfg.data_dir, MON)
    p.write_text("{not json", encoding="utf-8")
    assert W._load_handled(cfg.data_dir, MON) == set()


# =========================================================================================
# C12: at the stop (19:31) the page is polled once more
# =========================================================================================
def test_the_watcher_polls_once_more_at_its_stop_and_works_what_it_finds(cfg, monkeypatch):
    """Fails on the old code: it returned at the stop time without polling."""
    calls: list = []
    W, arena, pb = _harness(cfg, monkeypatch, calls, save_fails=False)
    W.watch(arena, pb, once=False, until="00:00")  # already past: straight to the stop
    assert len(_Poller.last.polls) == 1
    assert "handle AAA" in calls and "catch_up" not in calls
    assert W._load_handled(cfg.data_dir, datetime.now(SYD).date()) == {"00000001"}
    from asxbot.arena import heartbeat as H

    assert H.read(cfg.data_dir)["state"] == "stopped"


def test_the_watcher_stops_a_minute_after_announcements_end(cfg):
    from asxbot.arena.hours import watcher_stop_time

    assert watcher_stop_time(cfg, date(2026, 9, 28)).strftime("%H:%M") == "19:31"
    assert watcher_stop_time(cfg, date(2026, 10, 7)).strftime("%H:%M") == "20:31"  # DST


def test_the_evening_waits_for_the_watchers_last_poll(monkeypatch):
    ev = _load_script("arena_evening.pyw", "evening_wait_under_test")
    answers = iter(["pid 7, state running", "pid 7, state running", ""])
    monkeypatch.setattr(ev, "watcher_still_running", lambda: next(answers))
    lines, sleeps = [], []
    log = SimpleNamespace(write=lines.append)
    assert ev.wait_for_watcher(log, sleep=sleeps.append) is True
    assert sleeps == [ev.WATCHER_POLL_S, ev.WATCHER_POLL_S]
    assert lines[0].startswith("waiting for the watcher to stop before settling")
    assert lines[-1].startswith("the watcher has stopped")
    monkeypatch.setattr(ev, "watcher_still_running", lambda: "")
    lines.clear()
    assert ev.wait_for_watcher(log, sleep=sleeps.append) is True and lines == []


# =========================================================================================
# E3: start is allowed for a watcher that is not running, whatever the hour or the books
# =========================================================================================
def _heartbeat(data_dir: Path, **kw):
    from asxbot.arena.heartbeat import heartbeat_path

    body = {"pid": 4242, "state": "running", "started": at(7, 30).isoformat(),
            "beat": at(10, 0).isoformat(), **kw}  # fmt: skip
    p = heartbeat_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(body), encoding="utf-8")


def _book(data_dir: Path, name: str, positions=None):
    d = data_dir / "arena" / "accounts"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps({"name": name, "positions": positions or {},
                                                "orders": {}}), encoding="utf-8")  # fmt: skip


def test_a_dead_watcher_with_a_position_open_may_be_started(tmp_path):
    from asxbot.arena import lock as L

    _book(tmp_path, "v2__agent", positions={"ZIP": {"qty": 1000}})
    _heartbeat(tmp_path, state="crashed")
    note = L.check_start(tmp_path, pid_alive=lambda pid: True)
    assert note.startswith("clear to start") and "ZIP +1000" in note
    _heartbeat(tmp_path, state="running")  # says running, but the process is gone
    assert L.check_start(tmp_path, pid_alive=lambda pid: False).startswith("clear to start")
    with pytest.raises(L.Locked, match="never started beside it"):
        L.check_start(tmp_path, pid_alive=lambda pid: True)  # a live one: no second
    # stop/restart of a live watcher keep both locks
    with pytest.raises(L.Locked, match="never restarted with one open"):
        L.check_restart(tmp_path, at(20, 0), force=True, reason="x",
                        is_trading_day=lambda d: True)  # fmt: skip


def test_watcher_py_start_runs_the_task_for_a_dead_watcher_in_market_hours(tmp_path, monkeypatch):
    """Fails on the old script: start went through check_restart and was refused."""
    mod = _load_script("watcher.py", "watcher_ctl_under_test")
    _book(tmp_path, "v2__agent", positions={"ZIP": {"qty": 1000}})
    _heartbeat(tmp_path, state="crashed")
    ran = []
    monkeypatch.setattr(mod, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(mod, "schtasks", lambda *a: ran.append(a) or (0, "SUCCESS"))
    monkeypatch.setattr(mod, "task_status", lambda: "status")
    monkeypatch.setattr(mod, "log_line", lambda text: None)
    monkeypatch.setattr(sys, "argv", ["watcher.py", "start"])
    assert mod.main() == 0
    assert ran == [("/run", "/tn", mod.TASK)]  # started; nothing was ended
    monkeypatch.setattr(sys, "argv", ["watcher.py", "restart", "--force", "--reason", "x"])
    ran.clear()
    assert mod.main() == 3 and ran == []  # restart: refused with the position open


def test_the_watchdogs_down_message_says_what_a_start_will_do():
    from asxbot.arena import watchdog as WD

    v = WD.Verdict(True, False, "down", "the watcher process (pid 1) is gone.", 1)
    text = WD.down_message(v, at(11, 0))
    assert "scripts\\watcher.py start" in text and "positions open" in text
    assert "schtasks /run" not in text


# =========================================================================================
# E4: the warm-up waits for Google Drive until 09:45
# =========================================================================================
def _warmup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("ASXBOT_HOME", str(home))
    mod = _load_script("arena_warmup.pyw", "warmup_wait_under_test")
    monkeypatch.setattr(mod, "FAILURES", tmp_path / "task-failures.log")
    assert mod.REPO == home
    return mod, home


def test_the_warmup_waits_for_the_settings_folder_and_starts_when_it_appears(
    tmp_path, monkeypatch
):
    mod, home = _warmup(tmp_path, monkeypatch)
    clock = {"now": at(7, 30)}
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        clock["now"] += timedelta(seconds=s)
        if len(sleeps) == 5:  # Drive mounts 2.5 minutes in
            home.mkdir()
            (home / "config.yaml").write_text("broker: sim\n", encoding="utf-8")

    assert mod.wait_for_home(sleep=sleep, clock=lambda: clock["now"],
                             is_trading_day=lambda d: True) is True  # fmt: skip
    assert sleeps == [30] * 5
    log = mod.LOG.read_text(encoding="utf-8")
    assert "waiting for it until 09:45" in log and "is available; starting" in log
    assert log.count("still waiting for") == 2  # once a minute
    failures = (tmp_path / "task-failures.log").read_text(encoding="utf-8")
    assert failures.count("WAITING (warm-up)") == 1 and "ABORTED" not in failures


def test_the_warmup_gives_up_at_09_45_and_does_not_wait_on_a_closed_day(tmp_path, monkeypatch):
    mod, _ = _warmup(tmp_path, monkeypatch)
    clock = {"now": at(9, 44)}

    def sleep(s):
        clock["now"] += timedelta(seconds=s)

    assert mod.wait_for_home(sleep=sleep, clock=lambda: clock["now"],
                             is_trading_day=lambda d: True) is False  # fmt: skip
    failures = (tmp_path / "task-failures.log").read_text(encoding="utf-8")
    assert "still not available at 09:45" in failures
    slept = []
    assert mod.wait_for_home(sleep=slept.append, clock=lambda: at(8, 0),
                             is_trading_day=lambda d: False) is False  # fmt: skip
    assert slept == []


# =========================================================================================
# E5: the watcher's client waits 15 s and retries once; failed PDFs are retried later
# =========================================================================================
def test_the_watchers_client_gives_up_on_a_dead_asx_in_seconds_not_half_an_hour(
    tmp_path, monkeypatch
):
    import requests

    from asxbot.announcements import http as H

    c = H.PacedClient.inline(tmp_path, "ua", pause_s=0)
    asked, slept = [], []

    def dead(url, timeout=None, **k):
        asked.append(timeout)
        raise requests.Timeout("read timed out")

    monkeypatch.setattr(c.session, "get", dead)
    monkeypatch.setattr(H.time, "sleep", slept.append)
    with pytest.raises(H.PdfFetchFailed) as e:
        c.get_bytes("https://www.asx.com.au/x.pdf", tmp_path / "X_1.pdf")
    assert e.value.reason == "timeout"
    assert asked == [15.0, 15.0] and slept == [5.0]  # one retry; the old client: 20 tries


class _PdfClient:
    """get_bytes fails with `fail` reasons in turn, then writes a real PDF."""

    def __init__(self, fails):
        self.fails, self.asked = list(fails), []

    def get_bytes(self, url, dest):
        from asxbot.announcements.http import PdfFetchFailed

        self.asked.append(url)
        if self.fails:
            raise PdfFetchFailed(self.fails.pop(0), f"{url} failed")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.7 real")
        return dest


def test_a_failed_pdf_is_retried_on_a_later_cycle_not_twice_in_one(cfg):
    from asxbot.alerts import Alerts
    from asxbot.announcements.live import LivePoller, pdf_path
    from asxbot.log import EventLog

    client = _PdfClient(["timeout"])
    poller = LivePoller(cfg.data_dir, client, Alerts(cfg.data_dir), {"AAA"}, pdf_budget_s=45)
    a, t0 = _ann(), at(8, 1)
    assert poller.fetch_pdf(a, stage="poll", now=t0) is None
    assert poller.pdf_retry[a.ids_id]["next"] == t0 + timedelta(minutes=2)
    # the reader, seconds later in the same cycle, does not wait on it a second time
    assert poller.fetch_pdf(a, stage="reader", now=t0 + timedelta(seconds=20)) is None
    assert len(client.asked) == 1
    assert poller.retry_pdfs(t0 + timedelta(minutes=1)) == []  # not due yet
    assert poller.retry_pdfs(t0 + timedelta(minutes=2)) == [a.ids_id]
    assert pdf_path(cfg.data_dir, a).read_bytes().startswith(b"%PDF")
    assert poller.pdf_retry == {} and len(client.asked) == 2
    (ev,) = EventLog(cfg.data_dir).read("announcement_pdf_failures")
    assert ev["reason"] == "timeout" and ev["stage"] == "poll"


def test_a_pdf_that_never_comes_is_given_up_with_a_record(cfg):
    from asxbot.alerts import Alerts
    from asxbot.announcements.live import PDF_RETRY_AFTER_MIN, LivePoller
    from asxbot.log import EventLog

    client = _PdfClient(["server error"] * 10)
    poller = LivePoller(cfg.data_dir, client, Alerts(cfg.data_dir), {"AAA"}, pdf_budget_s=45)
    a, now = _ann(), at(8, 1)
    poller.fetch_pdf(a, stage="poll", now=now)
    for wait in PDF_RETRY_AFTER_MIN:
        now += timedelta(minutes=wait)
        poller.retry_pdfs(now)
    assert poller.pdf_retry == {} and len(client.asked) == len(PDF_RETRY_AFTER_MIN) + 1
    last = EventLog(cfg.data_dir).read("announcement_pdf_failures")[-1]
    assert last["reason"].startswith("given up after 6 tries")


def test_an_asx_outage_holds_the_rest_of_the_polls_pdfs_for_the_next_cycle(cfg, monkeypatch):
    from asxbot.alerts import Alerts
    from asxbot.announcements import live as LV

    items = [_ann("AAA", "00000001"), _ann("BBB", "00000002")]
    monkeypatch.setattr(LV, "parse_today", lambda html: items)
    client = _PdfClient(["timeout"])
    client.get = lambda url, use_cache=True: "<html/>"
    poller = LV.LivePoller(cfg.data_dir, client, Alerts(cfg.data_dir), {"AAA", "BBB"},
                           pdf_budget_s=45)  # fmt: skip
    t0 = at(8, 5)
    assert len(poller.poll_once(t0)) == 2
    assert len(client.asked) == 1  # BBB was not waited on after AAA timed out
    assert poller.pdf_retry["00000002"]["tries"] == 0
    assert poller.retry_pdfs(t0 + timedelta(seconds=30)) == []  # the outage still holds
    got = poller.retry_pdfs(t0 + timedelta(minutes=2, seconds=5))
    assert sorted(got) == ["00000001", "00000002"]


def test_a_restart_puts_todays_missing_pdfs_back_on_the_list(cfg):
    import pandas as pd

    from asxbot.alerts import Alerts
    from asxbot.announcements.live import LivePoller
    from asxbot.announcements.model import to_frame
    from asxbot.io import write_parquet_atomic

    day = datetime.now(SYD).date()
    a = _ann(when=datetime.combine(day, datetime.min.time()).replace(hour=8, tzinfo=SYD))
    write_parquet_atomic(to_frame([a, _ann("ZZZ", "00000009", ps=False)]),
                         cfg.data_dir / "announcements" / "live" / f"{day}.parquet")  # fmt: skip
    assert len(pd.read_parquet(cfg.data_dir / "announcements" / "live" / f"{day}.parquet")) == 2
    client = _PdfClient([])
    poller = LivePoller(cfg.data_dir, client, Alerts(cfg.data_dir), {"AAA", "ZZZ"},
                        pdf_budget_s=45)  # fmt: skip
    assert poller.retry_pdfs(datetime.now(SYD)) == [a.ids_id]  # ZZZ is not price-sensitive


# =========================================================================================
# I6: the parse-error flag clears when a page parses again, and says when it was raised
# =========================================================================================
def test_the_parse_error_flag_clears_on_the_next_good_page_and_carries_its_date(tmp_path):
    from asxbot.alerts import Alerts
    from asxbot.announcements.live import LivePoller
    from asxbot.announcements.parser import ParseError

    fixtures = REPO / "tests" / "fixtures"
    page = {"html": (fixtures / "todayAnns_layout_changed.html").read_text(encoding="utf-8")}
    client = SimpleNamespace(get=lambda url, use_cache=True: page["html"])
    al = Alerts(tmp_path)
    poller = LivePoller(tmp_path, client, al, {"RND"}, fetch_pdfs=False)
    with pytest.raises(ParseError):
        poller.poll_once(datetime(2026, 9, 24, 9, 0, tzinfo=SYD))
    ((key, msg),) = al.active()
    assert key == "collector_parse_error" and "Thu 24 Sep 09:00" in msg
    page["html"] = (fixtures / "todayAnns_empty.html").read_text(encoding="utf-8")
    poller.poll_once(datetime(2026, 9, 24, 9, 1, tzinfo=SYD))
    assert al.active() == []  # fails on the old code: nothing ever cleared it


# =========================================================================================
# A5: the pre-open look needs IBKR's own real-time quote
# =========================================================================================
def _v2_arrival(cfg, monkeypatch, quote):
    from asxbot.arena import reaction_v2, v2_flow
    from asxbot.arena import watch as W

    looks = []
    verdict = SimpleNamespace(ok=True, why="passes", test="", turnover=1e6, tick_pct=0.5)
    monkeypatch.setattr(reaction_v2, "screen_v2", lambda *a, **k: verdict)
    monkeypatch.setattr(reaction_v2, "enqueue", lambda *a, **k: True)
    monkeypatch.setattr(W, "_ensure_pdf", lambda arena, a: None)
    monkeypatch.setattr(W, "pdf_text_why", lambda d, a: ("text", ""))
    monkeypatch.setattr(W, "dossier", lambda arena, t: {})
    monkeypatch.setattr(W, "_entries_allowed", lambda arena, now, codes=(): (True, ""))
    reply = SimpleNamespace(text="TRADE_WORTHY: YES\nCAN_SIZE_AND_EXIT: YES", model="m")
    monkeypatch.setattr(W, "call_agent", lambda *a, **k: reply)
    monkeypatch.setattr(v2_flow, "pre_open_decider", lambda *a, **k: looks.append(a) or "looked")
    arena = SimpleNamespace(
        cfg=cfg, broker=SimpleNamespace(clock=lambda: at(8, 5)),
        quote_provider=lambda: SimpleNamespace(quote=lambda code: quote),
        daily_lookup=lambda: (lambda t: None), universe={"AAA"}, short_universe=set(),
    )  # fmt: skip
    pb = SimpleNamespace(key="asx_announcements_v2", version=2, raw={"pre_open_look": True},
                         level=SimpleNamespace(max_position_aud=5000.0))  # fmt: skip
    out = W._handle_v2(arena, pb, _ann(), at(8, 5), None, "", False,
                       lambda kind, rec: None, {}, None)  # fmt: skip
    return out, looks


def test_a_pre_open_look_on_a_yahoo_quote_is_paused_not_made(cfg, monkeypatch):
    """Fails on the old code: the feed was up, so the decider decided on Yahoo's quote."""
    from asxbot.live.quotes import Quote

    yahoo = Quote("AAA", 1.0, 1.0, 0.9, 0, at(8, 5), "yfinance (delayed)", True)
    out, looks = _v2_arrival(cfg, monkeypatch, yahoo)
    assert looks == [] and "not IBKR" in out["paused"]
    delayed = Quote("AAA", 1.0, 1.0, 0.9, 0, at(8, 5), "ibkr (delayed)", True)
    out, looks = _v2_arrival(cfg, monkeypatch, delayed)
    assert looks == [] and "delayed" in out["paused"]
    live = Quote("AAA", 1.0, 1.0, 0.9, 0, at(8, 5), "ibkr (real-time)", False)
    out, looks = _v2_arrival(cfg, monkeypatch, live)
    assert len(looks) == 1 and out["decider"] == "looked"


def test_the_packets_say_where_the_prices_came_from(cfg):
    from asxbot.arena import watch as W
    from asxbot.live.quotes import Quote

    assert W.reaction_header({"prices": "live data (IBKR)"}) == \
        "LIVE PRICE REACTION (live data (IBKR))"  # fmt: skip
    assert W.reaction_header({"available": False, "why": "no quote"}) == "LIVE PRICE REACTION"
    live = Quote("AAA", 1.0, 1.0, 0.9, 0, at(10, 5), "ibkr (real-time)", False)
    assert W._prices_label(cfg, live) == "live data (IBKR)"


# =========================================================================================
# A7/A4: the rotation runs every cycle 09:50-16:12, in priority order
# =========================================================================================
def test_pinned_codes_are_in_priority_order_and_finished_looks_are_dropped(cfg, monkeypatch):
    from asxbot.arena import daytrader
    from asxbot.arena import watch as W
    from asxbot.arena.reaction_v2 import save_queue

    agent = SimpleNamespace(
        positions={"ZIP": SimpleNamespace(qty=100), "OLD": SimpleNamespace(qty=0)},
        orders={"o1": SimpleNamespace(ticker="BHP", working=True),
                "o2": SimpleNamespace(ticker="CBA", working=False)},
    )  # fmt: skip
    empty = SimpleNamespace(positions={}, orders={})
    arena = SimpleNamespace(cfg=cfg, universe={"NWS", "AAA", "ZIP"}, playbooks=lambda: ["pb"],
                            account=lambda p, k: agent if k == "agent" else empty)  # fmt: skip
    save_queue(cfg.data_dir, MON, {"QQQ": {"status": "queued"}, "LKD": {"status": "looked"},
                                   "LKG": {"status": "looking"}, "_seeded": {}})  # fmt: skip
    monkeypatch.setattr(daytrader, "news_today", lambda d, day: {"NWS", "AAA", "QQQ", "XYZ"})
    assert W.pinned_codes(arena, MON) == ["ZIP", "BHP", "LKG", "QQQ", "AAA", "NWS"]


@pytest.mark.parametrize("when, rotates", [(at(9, 49), False), (at(9, 50), True),
                                           (at(15, 50), True), (at(16, 13), False)])
def test_the_feed_rotates_every_cycle_in_its_hours(monkeypatch, when, rotates):
    from asxbot.arena import watch as W

    seen = []
    feed = SimpleNamespace(pin=lambda codes: seen.append(("pin", list(codes))),
                           check=lambda now: seen.append(("check",)),
                           rotate=lambda codes, now: seen.append(("rotate", codes)))  # fmt: skip
    monkeypatch.setattr(W, "day_view", lambda arena, now: SimpleNamespace(feed=feed, day=MON))
    monkeypatch.setattr(W, "pinned_codes", lambda arena, day: ["ZIP", "BHP"])
    monkeypatch.setattr(W, "_daytrader_universe", lambda arena, day: ["BHP", "CSL", "WES"])
    W._feed_check(None, when)
    want = [("pin", ["ZIP", "BHP"]), ("check",)]
    if rotates:
        want.append(("rotate", ["ZIP", "BHP", "CSL", "WES"]))
    assert seen == want


# =========================================================================================
# E7: prune never removes a young release, or one a task last started from
# =========================================================================================
def test_prune_keeps_young_releases_and_the_ones_tasks_run_from(tmp_path):
    spec = importlib.util.spec_from_file_location("deploy_review", REPO / "scripts" / "deploy.py")
    d = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(d)
    local = tmp_path / "local"
    base = local / "releases"
    names = ["20260910-080000-aaaaaaaaaa", "20260911-080000-bbbbbbbbbb",
             "20260925-183300-cccccccccc", "20260925-201500-dddddddddd",
             "20260925-221000-eeeeeeeeee", "20260925-233000-ffffffffff",
             "20260926-001500-gggggggggg", "20260926-004300-hhhhhhhhhh"]  # fmt: skip
    for n in names:
        (base / n).mkdir(parents=True)
    d.set_pointer("CURRENT", names[-1], base)
    d.set_pointer("PREVIOUS", names[-2], base)
    (local / "logs").mkdir()
    (local / "logs" / "releases.log").write_text(
        f"2026-09-12 07:00:00 chat: running from {names[0]}\n"
        f"2026-09-26 07:30:00 arena_warmup: running from {names[2]}\n", encoding="utf-8")
    gone = d.prune(base, now=datetime(2026, 9, 26, 9, 0, tzinfo=SYD))
    assert gone == [names[1]]  # old and unused; the old code also took the chat's and 18:33's
    left = sorted(p.name for p in base.iterdir() if p.is_dir())
    assert left == sorted(n for n in names if n != names[1])


# =========================================================================================
# E8: the watcher logs to its own file; the error check reads only that
# =========================================================================================
def _line(path: Path, when: datetime, level: str, msg: str, logger="asxbot.arena.watch"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{when:%Y-%m-%d %H:%M:%S},123 {level} {logger}: {msg}\n")


def test_other_processes_errors_are_not_the_watchers(cfg):
    from asxbot.arena import selfcheck as S
    from asxbot.log import WATCH_LOG

    now = at(19, 0)
    _line(cfg.logs_dir / "asxbot.log", at(18, 57), "ERROR", "chaos test: socket dropped",
          "asxbot.ibkr.live")  # fmt: skip
    assert S.check_errors_logged(cfg, now).ok  # fails on the old code: it read asxbot.log
    _line(cfg.logs_dir / WATCH_LOG, at(18, 58), "ERROR", "the watcher's own failure")
    c = S.check_errors_logged(cfg, now)
    assert not c.ok and "the watcher's own failure" in c.detail


def test_setup_logging_writes_the_named_file(tmp_path, monkeypatch):
    import logging

    from asxbot import log as L

    monkeypatch.setattr(L, "_configured", False)
    lg = L.setup_logging(tmp_path, name="asxbot_review_probe", filename=L.WATCH_LOG)
    try:
        lg.error("hello")
        for h in lg.handlers:
            h.flush()
        assert "hello" in (tmp_path / L.WATCH_LOG).read_text(encoding="utf-8")
        assert not (tmp_path / "asxbot.log").exists()
    finally:
        for h in list(lg.handlers):
            h.close()
            lg.removeHandler(h)
        logging.getLogger("asxbot_review_probe").propagate = True


def test_arena_watch_logs_to_the_watchers_file(monkeypatch):
    from asxbot.arena import cli as C
    from asxbot.arena import watch as W
    from asxbot.log import WATCH_LOG

    asked = []
    pb = SimpleNamespace(key="asx_announcements_v2", enabled=True)
    arena = SimpleNamespace(playbooks=lambda: [pb])

    def fake_arena(log_file=None, cfg=None):
        asked.append(log_file)
        return None, SimpleNamespace(info=lambda *a: None, error=lambda *a: None), arena

    monkeypatch.setattr(C, "_arena", fake_arena)
    monkeypatch.setattr(W, "watch", lambda *a, **k: None)
    args = SimpleNamespace(playbook=None, once=True, interval=None, until=None)
    assert C.cmd_watch(args) == 0 and asked == [WATCH_LOG]


def test_the_evening_mirror_skips_lock_files(tmp_path):
    from asxbot.log import mirror_logs

    src = tmp_path / "logs"
    src.mkdir()
    (src / "arena_watch.log").write_text("x\n", encoding="utf-8")
    (src / "arena_watch.lock").write_text("123", encoding="utf-8")
    assert mirror_logs(src, tmp_path / "mirror") == ["arena_watch.log"]


# =========================================================================================
# I12: repeats of one kind of error within 30 minutes are one message
# =========================================================================================
def test_the_same_404_twice_in_a_minute_is_one_message(cfg, monkeypatch):
    from asxbot.arena import selfcheck as S
    from asxbot.log import WATCH_LOG

    sent = []
    arena = SimpleNamespace(cfg=cfg, broker=SimpleNamespace(
        notifier=SimpleNamespace(send=lambda text: sent.append(text))))  # fmt: skip
    monkeypatch.setattr(S, "run_checks", lambda a, p, n: [S.check_errors_logged(cfg, n)])
    log = cfg.logs_dir / WATCH_LOG
    msg = "PDF FETCH FAILED for TUA 0314{} (HTTP 404): 404 Client Error"
    _line(log, at(8, 2, 5), "ERROR", msg.format("2537"), "asxbot.announcements.live")
    S.report(arena, None, at(8, 2, 30))
    _line(log, at(8, 3, 5), "ERROR", msg.format("2538"), "asxbot.announcements.live")
    S.report(arena, None, at(8, 3, 30))
    assert len(sent) == 1  # fails on the old code: every new line was a new message
    c = S.check_errors_logged(cfg, at(8, 3, 30))
    assert "x2, the last at 08:03:05" in c.detail
    _line(log, at(8, 40), "ERROR", msg.format("2539"), "asxbot.announcements.live")
    S.report(arena, None, at(8, 40, 30))  # back after a quiet half hour: said again
    assert len(sent) == 2 and "1 NEW" in sent[1]


# =========================================================================================
# E9: the PDF check reads five bytes, and only of files it has not seen
# =========================================================================================
def test_the_pdf_check_reads_five_bytes_of_new_files_only(cfg, monkeypatch):
    import builtins

    from asxbot.arena import selfcheck as S

    d = cfg.data_dir / "announcements" / "pdf" / "2026-09-28"
    d.mkdir(parents=True)
    (d / "AAA_1.pdf").write_bytes(b"%PDF-1.7" + b"x" * 100_000)
    (d / "BBB_2.pdf").write_bytes(b"<html>terms</html>")
    opened = []
    real_open = builtins.open

    def counting_open(path, mode="r", *a, **k):
        if str(path).endswith(".pdf"):
            opened.append(Path(path).name)
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr(S, "open", counting_open, raising=False)
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("a whole file was read"))
    c = S.check_pdfs_are_pdfs(cfg)
    assert not c.ok and c.count == 1 and "BBB_2.pdf" in c.detail
    assert sorted(opened) == ["AAA_1.pdf", "BBB_2.pdf"]
    assert S.check_pdfs_are_pdfs(cfg).count == 1 and len(opened) == 2  # nothing read again
    (d / "CCC_3.pdf").write_bytes(b"%PDF-1.4")
    S.check_pdfs_are_pdfs(cfg)
    assert opened[2:] == ["CCC_3.pdf"]


# =========================================================================================
# E10: one watcher at a time
# =========================================================================================
def test_a_second_watcher_is_refused(cfg, monkeypatch):
    from asxbot.arena import watch as W

    monkeypatch.setattr(W, "LOCK_WAIT_S", 0.0)
    first = W.WatcherLock()
    assert first.acquire()
    try:
        assert not W.WatcherLock().acquire()
        with pytest.raises(W.WatcherAlreadyRunning, match="already running"):
            W.watch(SimpleNamespace(cfg=cfg), SimpleNamespace(), once=True)
    finally:
        first.release()
    again = W.WatcherLock()
    assert again.acquire()
    again.release()


def test_arena_watch_says_refused_when_another_is_running(monkeypatch):
    from asxbot.arena import cli as C
    from asxbot.arena import watch as W

    pb = SimpleNamespace(key="asx_announcements_v2", enabled=True)
    arena = SimpleNamespace(playbooks=lambda: [pb])
    log = SimpleNamespace(info=lambda *a: None, error=lambda *a: None)
    monkeypatch.setattr(C, "_arena", lambda log_file=None, cfg=None: (None, log, arena))

    def busy(*a, **k):
        raise W.WatcherAlreadyRunning("another arena watcher is already running (pid 9)")

    monkeypatch.setattr(W, "watch", busy)
    args = SimpleNamespace(playbook=None, once=True, interval=None, until=None)
    assert C.cmd_watch(args) == C.EXIT_ALREADY_WATCHING


# =========================================================================================
# E11: the lock ends at the watcher's stop time, 20:31 on daylight saving
# =========================================================================================
def test_the_lock_covers_the_daylight_saving_evening(cfg):
    from asxbot.arena.lock import in_market_lock

    wed_dst = datetime(2026, 10, 7, 20, 0, tzinfo=SYD)
    assert in_market_lock(wed_dst, lambda d: True, cfg)  # fails on the old code (19:25)
    assert not in_market_lock(wed_dst.replace(hour=20, minute=31), lambda d: True, cfg)


# =========================================================================================
# E14: a late evening start still settles the day
# =========================================================================================
def test_a_late_evening_start_runs_when_the_report_has_not_gone(cfg):
    from asxbot.arena.hours import evening_due

    thu = date(2026, 10, 1)  # no daylight saving yet: slot 19:30
    late = datetime(2026, 10, 1, 22, 40, tzinfo=SYD)
    due, why = evening_due(cfg, late, last_sent=datetime(2026, 9, 30, 19, 34, tzinfo=SYD))
    assert due and why.startswith("due LATE")  # fails on the old code: not the slot
    sent = datetime(thu.year, thu.month, thu.day, 19, 33, tzinfo=SYD)
    assert not evening_due(cfg, late, last_sent=sent)[0]
    assert not evening_due(cfg, late.replace(hour=20, minute=30), last_sent=sent)[0]
    dst_1930 = datetime(2026, 10, 7, 19, 30, tzinfo=SYD)  # slot 20:30: 19:30 is early, not late
    assert not evening_due(cfg, dst_1930, last_sent=None)[0]
    assert evening_due(cfg, datetime(2026, 10, 1, 19, 31, tzinfo=SYD), None)[0]


# =========================================================================================
# I4: a cycle stuck outside any announced wait is reported once, and its end once
# =========================================================================================
def _beat(**kw):
    base = {"pid": 4242, "state": "running", "started": at(7, 30).isoformat(),
            "beat": at(11, 0).isoformat(), "last_activity": at(10, 59).isoformat(),
            "cycle_started": at(10, 52).isoformat(), "cycle_finished": at(10, 51).isoformat(),
            "quiet_until": None, "wait_ended": None}  # fmt: skip
    base.update(kw)
    return base


def test_a_cycle_running_long_outside_any_wait_is_stuck():
    from asxbot.arena.watchdog import stuck_cycle

    assert stuck_cycle(_beat(), at(10, 58)) == ""  # 6 minutes: not yet
    assert "10:52:00 has not finished after 7 minutes" in stuck_cycle(_beat(), at(10, 59))
    # inside a declared wait (a model call): not stuck
    assert stuck_cycle(_beat(quiet_until=at(11, 5).isoformat()), at(11, 0)) == ""
    # three model calls back to back, the last ended a minute ago: slow, not stuck
    assert stuck_cycle(_beat(wait_ended=at(10, 59).isoformat()), at(11, 0)) == ""
    # finished, or no cycle stamps at all (an older watcher): nothing to say
    assert stuck_cycle(_beat(cycle_finished=at(10, 53).isoformat()), at(11, 30)) == ""
    assert stuck_cycle(_beat(cycle_started=None), at(11, 30)) == ""


def test_a_stuck_cycle_is_told_once_and_its_end_once(cfg, monkeypatch):
    from asxbot.arena import watchdog as WD

    window = (at(7, 33), at(19, 31))
    monkeypatch.setattr(WD, "expected_window", lambda cfg, now: window)
    sent = []

    def run(beat, now):
        return WD.run_once(cfg, now, lambda t: sent.append(t) or True, hb_reader=lambda: beat,
                           alive=lambda pid: True, sleep=lambda s: None)  # fmt: skip

    stuck = _beat(beat=at(11, 0).isoformat(), last_activity=at(11, 0).isoformat())
    assert run(stuck, at(11, 0)) == "ok; cycle stuck: alert sent"
    assert run({**stuck, "beat": at(11, 5).isoformat(), "last_activity": at(11, 5).isoformat()},
               at(11, 5)).endswith("already reported at 2026-09-28T11:00:00+10:00")
    moving = {**stuck, "beat": at(11, 17).isoformat(), "last_activity": at(11, 17).isoformat(),
              "cycle_finished": at(11, 17).isoformat()}  # fmt: skip
    assert run(moving, at(11, 18)) == "ok; cycle moving again: message sent"
    later = {**moving, "beat": at(11, 22).isoformat(), "last_activity": at(11, 22).isoformat()}
    assert run(later, at(11, 23)) == "ok"
    assert len(sent) == 2 and "WATCHER CYCLE STUCK" in sent[0]
    assert "MOVING AGAIN" in sent[1] and "10:52:00" in sent[1]


def test_the_heartbeat_carries_the_cycle_and_the_end_of_the_last_wait(tmp_path):
    from asxbot.arena import heartbeat as H

    hb = H.Heartbeat(tmp_path, beat_s=3600)
    hb.cycle_start(at(10, 52))
    with hb.quiet(60, "waiting on trader-decider"):
        pass
    p = hb.payload()
    assert p["cycle_started"] == at(10, 52).isoformat() and p["cycle_finished"] is None
    assert p["wait_ended"] is not None
    hb.cycle_end(at(10, 53))
    assert hb.payload()["cycle_finished"] == at(10, 53).isoformat()


# =========================================================================================
# I8: the connection doctor speaks for IBKR outages while it is ticking
# =========================================================================================
def _doctor(path: Path, last_at: datetime, episode):
    path.write_text(json.dumps({"last": {"at": last_at.isoformat(), "cause": "login_needed",
                                         "evidence": "x"},
                                "episode": episode}), encoding="utf-8")  # fmt: skip


def test_the_doctor_speaks_only_while_fresh_and_in_an_episode(tmp_path):
    from asxbot.arena.selfcheck import doctor_speaking

    p = tmp_path / "doctor.json"
    assert doctor_speaking(at(10, 0), p) == ""  # missing
    _doctor(p, at(9, 58), {"cause": "gateway_down", "since": at(9, 50).isoformat()})
    assert doctor_speaking(at(10, 0), p) == "gateway_down"
    assert doctor_speaking(at(10, 6), p) == ""  # its last tick is stale
    _doctor(p, at(9, 58), None)
    assert doctor_speaking(at(10, 0), p) == ""  # healthy: no episode


def test_an_ibkr_failure_is_recorded_not_sent_while_the_doctor_speaks(cfg, monkeypatch):
    from asxbot.alerts import Alerts
    from asxbot.arena import selfcheck as S
    from asxbot.log import EventLog

    sent = []
    arena = SimpleNamespace(cfg=cfg, broker=SimpleNamespace(
        notifier=SimpleNamespace(send=lambda text: sent.append(text))))  # fmt: skip
    down = S.Check("live_data", False, "IBKR prices are not available (x); new entries are "
                   "paused, exits keep working", items=["x"])  # fmt: skip
    monkeypatch.setattr(S, "run_checks", lambda a, p, n: [down])
    speaking = {"cause": "gateway_down"}
    monkeypatch.setattr(S, "doctor_speaking", lambda now: speaking["cause"])
    S.report(arena, None, at(10, 0))
    S.report(arena, None, at(10, 2))
    assert sent == [] and Alerts(cfg.data_dir).is_active("live_data")
    ev = EventLog(cfg.data_dir).read("arena_selfcheck")[-1]
    assert ev["not_sent"].startswith("told by the connection doctor (gateway_down)")
    speaking["cause"] = ""  # the doctor's episode is over, the check still fails: say it now
    S.report(arena, None, at(10, 4))
    assert len(sent) == 1 and "SELF-CHECK: live_data" in sent[0] and "Yahoo" not in sent[0]
    S.report(arena, None, at(10, 6))
    assert len(sent) == 1  # and then the usual hourly repeat


# =========================================================================================
# H1-H3: the commands that could touch the frozen test
# =========================================================================================
def _cli_arena(cfg, status="test", flat=True):
    pb = SimpleNamespace(key="asx_announcements_v2", status=status, flat_at_close=flat,
                         agent_account="v2__agent", bot_account="v2__bot", enabled=True)
    return pb, SimpleNamespace(playbooks=lambda only_enabled=True: [pb],
                               playbook=lambda key: pb)  # fmt: skip


def _no_log():
    return SimpleNamespace(info=lambda *a: None, error=lambda *a: None)


def test_preclose_is_refused_for_a_flat_at_close_playbook(cfg, monkeypatch):
    from asxbot.arena import cli as C
    from asxbot.arena import watch as W

    pb, arena = _cli_arena(cfg)
    monkeypatch.setattr(C, "_arena", lambda log_file=None, **k: (cfg, _no_log(), arena))
    monkeypatch.setattr(W, "sweep_before_close", lambda *a, **k: pytest.fail("swept"))
    assert C.cmd_preclose(SimpleNamespace(playbook=None)) == 3


def test_a_fake_announcement_is_refused_during_a_test_without_a_scratch_folder(cfg, monkeypatch):
    from asxbot.arena import cli as C
    from asxbot.arena import watch as W

    pb, arena = _cli_arena(cfg)
    monkeypatch.setattr(C, "_arena", lambda log_file=None, **k: (cfg, _no_log(), arena))
    monkeypatch.setattr(W, "handle_announcement", lambda *a, **k: pytest.fail("handled"))
    args = SimpleNamespace(playbook=None, ticker="AAA", at=None, prev_close=None, data_dir=None)
    assert C.cmd_fake(args) == 3


def test_a_fake_announcement_in_a_scratch_folder_never_touches_the_live_data(
    tmp_path, monkeypatch
):
    from asxbot.arena import cli as C
    from asxbot.arena import watch as W

    seen = {}

    def fake_arena(log_file=None, cfg=None):
        seen["cfg"] = cfg
        pb, arena = _cli_arena(cfg)
        arena.account = lambda p, k: SimpleNamespace(positions={})
        arena.broker = SimpleNamespace(resolve_pending=lambda a, n: [],
                                       apply_exits=lambda a, n: [])  # fmt: skip
        return cfg, _no_log(), arena

    monkeypatch.setattr(C, "_arena", fake_arena)
    monkeypatch.setattr(W, "handle_announcement", lambda *a, **k: {"handled": True})
    scratch = tmp_path / "scratch"
    args = SimpleNamespace(playbook=None, ticker="AAA", at=None, prev_close=None,
                           data_dir=str(scratch), headline="FAKE", text_file=None,
                           no_bot=False, no_agent=False)  # fmt: skip
    assert C.cmd_fake(args) == 0
    cfg = seen["cfg"]
    assert cfg.data_dir == scratch and cfg.get("data.live_provider") == "yfinance"
    assert cfg.get("arena.alerts.telegram") is False


def test_reset_is_refused_for_the_test_books_and_in_market_hours(cfg, monkeypatch):
    from asxbot.arena import cli as C
    from asxbot.arena import lock as L
    from asxbot.log import EventLog

    _book(cfg.data_dir, "v2__agent")
    pb, arena = _cli_arena(cfg)
    monkeypatch.setattr(C, "_arena", lambda log_file=None, **k: (cfg, _no_log(), arena))
    monkeypatch.setattr(L, "in_market_lock", lambda now, is_trading_day=None, cfg=None: False)
    assert C.cmd_reset(SimpleNamespace(yes=True, account=None)) == 3
    assert (cfg.data_dir / "arena" / "accounts" / "v2__agent.json").exists()
    _book(cfg.data_dir, "old__agent")
    assert C.cmd_reset(SimpleNamespace(yes=True, account="old__")) == 0  # not a test book
    assert not (cfg.data_dir / "arena" / "accounts" / "old__agent.json").exists()
    monkeypatch.setattr(L, "in_market_lock", lambda now, is_trading_day=None, cfg=None: True)
    _book(cfg.data_dir, "old__agent")
    assert C.cmd_reset(SimpleNamespace(yes=True, account="old__")) == 3
    events = EventLog(cfg.data_dir).read("arena_reset")
    assert [bool(e.get("refused")) for e in events] == [True, False, True]


def test_hand_orders_in_the_bots_book_or_a_test_need_what_they_need(cfg, monkeypatch):
    from asxbot.arena import cli as C
    from asxbot.arena import orders as O
    from asxbot.log import EventLog

    placed = []
    order = SimpleNamespace(order_id="ARN-1", status="pending_fill", side="buy", ticker="AAA",
                            qty=10, limit=1.0, stop=0.9, message="ok")  # fmt: skip
    monkeypatch.setattr(O, "arena_place_order", lambda *a, **k: placed.append(k) or order)
    pb, arena = _cli_arena(cfg)
    arena.account = lambda p, k: SimpleNamespace(name=f"v2__{k}")
    arena.broker = None
    arena.universe, arena.short_universe = set(), set()
    monkeypatch.setattr(C, "_arena", lambda log_file=None, **k: (cfg, _no_log(), arena))
    base = dict(playbook=None, ticker="AAA", side="buy", qty=10, limit=1.0, stop=0.9,
                target=None, model="", force=False, reason="")  # fmt: skip
    assert C.cmd_place_order(SimpleNamespace(**base, by="bot")) == 3
    assert C.cmd_place_order(SimpleNamespace(**{**base, "force": True, "reason": "x"},
                                             by="bot")) == 3  # never, forced or not
    assert C.cmd_place_order(SimpleNamespace(**base, by="agent")) == 3  # the test: needs force
    assert placed == []
    ok = SimpleNamespace(**{**base, "force": True, "reason": "Rick asked, 26 Sep"}, by="agent")
    assert C.cmd_place_order(ok) == 0 and len(placed) == 1
    (ev,) = EventLog(cfg.data_dir).read("arena_hand_orders")
    assert ev["why"] == "Rick asked, 26 Sep" and ev["ticker"] == "AAA"
    pb.status = "build"  # outside a test, an agent order by hand goes as before
    assert C.cmd_place_order(SimpleNamespace(**base, by="agent")) == 0 and len(placed) == 2


# =========================================================================================
# The agents' failures (with the agents fixer, 26 Sep 2026): fresh sessions, and a call
# that got no answer is "agent_unavailable" with its kind, never a pass
# =========================================================================================
def test_a_reader_that_cannot_be_reached_is_recorded_as_unavailable(cfg, monkeypatch):
    from asxbot.arena import agents as AG
    from asxbot.arena import reaction_v2
    from asxbot.arena import watch as W
    from asxbot.live.quotes import Quote

    verdict = SimpleNamespace(ok=True, why="passes", test="", turnover=1e6, tick_pct=0.5)
    monkeypatch.setattr(reaction_v2, "screen_v2", lambda *a, **k: verdict)
    monkeypatch.setattr(reaction_v2, "enqueue", lambda *a, **k: True)
    monkeypatch.setattr(W, "_ensure_pdf", lambda arena, a: None)
    monkeypatch.setattr(W, "pdf_text_why", lambda d, a: ("text", ""))
    monkeypatch.setattr(W, "dossier", lambda arena, t: {})
    asked = []

    def limit(*a, **k):
        asked.append(k)
        raise AG.AgentCallFailed("usage limit: the plan refused", "usage_limit")

    monkeypatch.setattr(W, "call_agent", limit)
    live = Quote("AAA", 1.0, 1.0, 0.9, 0, at(8, 5), "ibkr (real-time)", False)
    arena = SimpleNamespace(
        cfg=cfg, broker=SimpleNamespace(clock=lambda: at(8, 5)),
        quote_provider=lambda: SimpleNamespace(quote=lambda code: live),
        daily_lookup=lambda: (lambda t: None), universe={"AAA"}, short_universe=set(),
    )  # fmt: skip
    pb = SimpleNamespace(key="asx_announcements_v2", version=2, raw={},
                         level=SimpleNamespace(max_position_aud=5000.0))  # fmt: skip
    recs = []
    W._handle_v2(arena, pb, _ann(), at(8, 5), None, "", False,
                 lambda kind, rec: recs.append((kind, rec)), {}, None)  # fmt: skip
    ((_, rec),) = [r for r in recs if r[0] == "arena_decisions"]
    assert rec["outcome"] == "agent_unavailable" and rec["kind"] == "usage_limit"
    assert rec["stage"] == "reader"
    assert asked and asked[0]["fresh_session"] == AG.fresh_sessions_on(cfg)


def test_a_pre_close_call_that_fails_is_recorded_as_unavailable_and_still_closes(
    cfg, monkeypatch
):
    from asxbot.arena import agents as AG
    from asxbot.arena import watch as W
    from asxbot.log import EventLog

    def timeout(*a, **k):
        raise AG.AgentCallFailed("no answer in time", "timeout")

    placed = []
    monkeypatch.setattr(W, "call_agent", timeout)
    monkeypatch.setattr(W, "arena_place_order",
                        lambda *a, **k: placed.append(k) or SimpleNamespace(order_id="ARN-9"))
    pos = SimpleNamespace(qty=1000, avg_cost=1.0, opened_at="2026-09-28T10:31", stop=0.95,
                          target=None, thesis="t", hold="intraday", hold_reason="",
                          hold_asked_on="")  # fmt: skip
    acct = SimpleNamespace(positions={"ZIP": pos})
    arena = SimpleNamespace(
        cfg=cfg, account=lambda pb, kind: acct, daily_lookup=lambda: (lambda t: None),
        broker=SimpleNamespace(clock=lambda: at(15, 50), day_loss_pct=lambda a, n: 0.0,
                               minutes=SimpleNamespace(last_price=lambda t: 1.02)),
        universe=set(), short_universe=set(), store=None,
    )  # fmt: skip
    pb = SimpleNamespace(holding="intraday", level=SimpleNamespace(number=1, name="Daily"))
    out = W.sweep_before_close(arena, pb, at(15, 50))
    assert out[0]["action"] == "close" and placed
    (ev,) = EventLog(cfg.data_dir).read("arena_decisions")
    assert ev["outcome"] == "agent_unavailable" and ev["kind"] == "timeout"


def test_the_evening_report_asks_for_a_fresh_session(cfg, monkeypatch):
    from asxbot.arena import agents as AG
    from asxbot.arena import cli as C
    from asxbot.arena import report as RP

    asked = []
    monkeypatch.setattr(C, "_arena", lambda log_file=None, **k: (cfg, _no_log(), None))
    monkeypatch.setattr(RP, "gather", lambda arena: {})
    monkeypatch.setattr(RP, "render_plain", lambda facts: "the report")
    monkeypatch.setattr(RP, "agent_brief", lambda facts: "brief")
    reply = SimpleNamespace(text="written", model="m")
    monkeypatch.setattr(AG, "call_agent", lambda *a, **k: asked.append(k) or reply)
    assert C.cmd_report(SimpleNamespace(agent=True, send=False)) == 0
    assert asked and asked[0]["fresh_session"] == AG.fresh_sessions_on(cfg)


def test_the_filter_cost_line_names_each_tests_playbook(cfg, tmp_path, monkeypatch):
    from asxbot.arena import cli as C
    from asxbot.arena import filtercost as FC
    from asxbot.arena import notify as N

    cost = FC.TestCost(test="no_quote", rejected=4, playbook="asx_announcements_v2", measured=3,
                       rel_returns=[1.0, 2.0, 3.0])  # fmt: skip
    monkeypatch.setattr(C, "_arena", lambda log_file=None, **k: (cfg, _no_log(), None))
    monkeypatch.setattr(FC, "measure", lambda arena, weeks: {"k": cost})
    monkeypatch.setattr(FC, "render", lambda costs, weeks: "report")
    sent = []
    monkeypatch.setattr(N, "build_notifier", lambda cfg: SimpleNamespace(send=sent.append))
    args = SimpleNamespace(weeks=4, out=str(tmp_path / "fc.md"), send=True)
    assert C.cmd_filter_cost(args) == 0
    assert "no_quote (asx_announcements_v2) 3/4" in sent[0]


def test_feed_down_deferrals_are_not_screen_verdicts(cfg):
    from asxbot.arena import selfcheck as S
    from asxbot.log import EventLog

    ev = EventLog(cfg.data_dir)
    for i in range(12):  # twelve deferred while the feed was down, three real rejections
        ev.append("arena_screened", {"ids_id": f"D{i}", "ok": False, "test": "deferred"})
    for i in range(3):
        ev.append("arena_screened", {"ids_id": f"R{i}", "ok": False, "test": "turnover"})
    c = S.check_screen_not_dominated(cfg, datetime.now(SYD))
    assert c.ok and "only 3 screened today" in c.detail  # the old code: deferred 80% of 15


def _call(cfg, when, ok, kind=None, agent="trader-decider"):
    body = {"agent": agent, "ok": ok, "ts": when.astimezone(ZoneInfo("UTC")).isoformat()}
    if kind:
        body["kind"] = kind
    p = cfg.data_dir / "events" / "arena_agent_calls.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(body) + "\n")


def test_unanswered_agent_calls_are_a_check_and_the_usage_limit_is_told_once_each_way(
    cfg, monkeypatch
):
    from asxbot.arena import selfcheck as S

    _call(cfg, at(10, 0), True)
    assert S.check_agent_unavailable(cfg, at(10, 5)).ok
    _call(cfg, at(10, 31), False, "usage_limit")
    _call(cfg, at(10, 40), False, "usage_limit", "trader-reader")
    c = S.check_agent_unavailable(cfg, at(10, 45))
    assert not c.ok and "usage_limit x2 since 10:31" in c.detail and c.items == ["usage_limit"]

    sent = []
    arena = SimpleNamespace(cfg=cfg, broker=SimpleNamespace(
        notifier=SimpleNamespace(send=lambda text: sent.append(text))))  # fmt: skip
    monkeypatch.setattr(S, "run_checks", lambda a, p, n: [S.check_agent_unavailable(cfg, n)])
    for minute in (45, 46, 50):  # every cycle: one line, not one a cycle
        S.report(arena, None, at(10, minute))
    assert len(sent) == 1 and "usage limit" in sent[0] and "since 10:31" in sent[0]
    _call(cfg, at(11, 2), True)  # a call is answered again
    S.report(arena, None, at(11, 3))
    assert len(sent) == 2 and "answered again (11:02)" in sent[1]
    S.report(arena, None, at(11, 4))
    assert len(sent) == 2
    _call(cfg, at(11, 10), False, "timeout")  # another kind: an ordinary self-check message
    S.report(arena, None, at(11, 11))
    assert len(sent) == 3 and "SELF-CHECK: agent_unavailable" in sent[2]
