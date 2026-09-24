"""The checks the system runs against itself. One test per check, and no network.

These exist because every fault of 23 September was already sitting in a file or a log
line: a terms page named .pdf, one screen test taking most of the day, a lost edit that
made every re-look die at the decider. Each test below feeds a check the shape of evidence
that morning actually produced.
"""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from asxbot.arena import selfcheck as S
from asxbot.config import load_config
from asxbot.log import EventLog

SYD = ZoneInfo("Australia/Sydney")
NOW = datetime(2026, 9, 23, 11, 0, tzinfo=SYD)


@pytest.fixture
def cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )


def _event(cfg, kind: str, rec: dict, at: datetime):
    """Append an event with a chosen timestamp; the log itself always stamps 'now'."""
    ev = EventLog(cfg.data_dir)
    ev.append(kind, rec)
    p = ev.path(kind)
    lines = p.read_text(encoding="utf-8").splitlines()
    lines[-1] = json.dumps({**json.loads(lines[-1]), "ts": at.astimezone(UTC).isoformat()})
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _pdf(cfg, name: str, body: bytes):
    d = cfg.data_dir / "announcements" / "pdf" / "2026-09-23"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(body)


# -- 1. a "PDF" that is not a PDF ------------------------------------------
def test_a_saved_terms_page_is_caught(cfg):
    _pdf(cfg, "AAA_1.pdf", b"%PDF-1.6 real")
    assert S.check_pdfs_are_pdfs(cfg).ok
    _pdf(cfg, "BBB_2.pdf", b"<html><h2>Access to this site</h2>")
    c = S.check_pdfs_are_pdfs(cfg)
    assert not c.ok and c.count == 1 and "BBB_2.pdf" in c.detail


# -- 2. a PDF fetch that failed --------------------------------------------
def test_a_failed_pdf_fetch_in_the_last_hour_is_caught(cfg):
    assert S.check_pdf_fetches(cfg, NOW).ok
    _event(
        cfg, "announcement_pdf_failures", {"code": "CMM", "error": "not a PDF"},
        NOW - timedelta(minutes=5),
    )  # fmt: skip
    c = S.check_pdf_fetches(cfg, NOW)
    assert not c.ok and "CMM" in c.detail
    # an old failure is not today's problem
    assert S.check_pdf_fetches(cfg, NOW + timedelta(hours=3)).ok


# -- 3. one screen test taking the whole day -------------------------------
_SEQ = [0]


def _screened(cfg, n, test, ok=False, at=None):
    for _ in range(n):
        _SEQ[0] += 1
        _event(
            cfg, "arena_screened",
            {"ids_id": f"A{_SEQ[0]}", "ticker": "AAA", "ok": ok, "test": "" if ok else test},
            at or NOW - timedelta(minutes=30),
        )  # fmt: skip


def test_a_screen_test_rejecting_most_of_the_day_is_caught(cfg):
    _screened(cfg, 9, "tick")
    assert S.check_screen_not_dominated(cfg, NOW).ok  # too few to judge a share

    _screened(cfg, 30, "tick")
    _screened(cfg, 5, "", ok=True)
    c = S.check_screen_not_dominated(cfg, NOW)
    assert not c.ok and "tick" in c.detail and c.count == 39
    assert c.facts["by_test"]["tick"] == 39


def test_a_spread_of_screen_rejections_is_fine(cfg):
    _screened(cfg, 8, "tick")
    _screened(cfg, 8, "turnover")
    _screened(cfg, 8, "halted")
    _screened(cfg, 8, "", ok=True)
    assert S.check_screen_not_dominated(cfg, NOW).ok


# -- 4. an agent call that did not match config ----------------------------
EXPECT = {"trader-reader": ("anthropic/claude-sonnet-5", "medium")}


def test_the_expected_agents_are_what_rick_set_on_23_sep_evening(cfg):
    # Set on the OpenClaw agents by Rick on the evening of 2026-09-23 and confirmed on a
    # live call to each. The self-check must hold calls to exactly this, from config.
    from asxbot.arena.watch import DECIDER_MODEL, READER_MODEL

    assert READER_MODEL == "anthropic/claude-sonnet-5"
    assert DECIDER_MODEL == "anthropic/claude-opus-5-5"
    assert cfg.get("arena.agents.effort") == {"reader": "medium", "decider": "high"}
    assert S.expected_agents(cfg) == {
        "trader-reader": ("anthropic/claude-sonnet-5", "medium"),
        "trader-decider": ("anthropic/claude-opus-5-5", "high"),
    }


def test_opus_5_and_opus_5_5_are_different_models(cfg):
    want = {"trader-decider": ("anthropic/claude-opus-5-5", "high")}
    _event(cfg, "arena_agent_calls",
           {"agent": "trader-decider", "model_ran": "claude-opus-5", "thinking": "high"},
           NOW - timedelta(minutes=2))  # fmt: skip
    c = S.check_agent_calls(cfg, NOW, want)
    assert not c.ok and "claude-opus-5," in c.detail
    # and the other way round: a call on 5.5 does not pass a check that wants 5
    _event(cfg, "arena_agent_calls",
           {"agent": "trader-decider", "model_ran": "claude-opus-5-5", "thinking": "high"},
           NOW - timedelta(minutes=1))  # fmt: skip
    c = S.check_agent_calls(cfg, NOW, {"trader-decider": ("anthropic/claude-opus-5", "high")})
    assert not c.ok and c.count == 1 and "ran claude-opus-5-5" in c.detail


def test_a_reply_matches_its_model_exactly_not_by_prefix():
    from asxbot.arena.agents import AgentReply

    def reply(ran, asked):
        return AgentReply("trader-decider", "", ran, asked, "high", "", 0, True, {})

    assert reply("claude-opus-5-5", "anthropic/claude-opus-5-5").model_matches
    assert reply("anthropic/claude-opus-5-5", "anthropic/claude-opus-5-5").model_matches
    assert not reply("claude-opus-5", "anthropic/claude-opus-5-5").model_matches
    assert not reply("claude-opus-5-5", "anthropic/claude-opus-5").model_matches
    assert not reply("", "anthropic/claude-opus-5-5").model_matches
    # the provider prefix is dropped only as a prefix, never from the middle of a name
    assert not reply("x-anthropic/claude-opus-5-5", "claude-opus-5-5").model_matches


def test_calls_made_yesterday_under_the_old_settings_do_not_fail_today(cfg):
    # 23 Sep 19:30: the evening report, the last call on Opus 5 / reader low
    _event(cfg, "arena_agent_calls",
           {"agent": "trader-decider", "model_ran": "claude-opus-5", "thinking": "high"},
           datetime(2026, 9, 23, 19, 30, tzinfo=SYD))  # fmt: skip
    _event(cfg, "arena_agent_calls",
           {"agent": "trader-reader", "model_ran": "claude-sonnet-5", "thinking": "low"},
           datetime(2026, 9, 23, 19, 29, tzinfo=SYD))  # fmt: skip
    morning = datetime(2026, 9, 24, 7, 30, tzinfo=SYD)
    c = S.check_agent_calls(cfg, morning, S.expected_agents(cfg))
    assert c.ok and c.detail.startswith("0 agent call(s)")


def test_a_downgraded_model_or_effort_level_is_caught(cfg):
    _event(cfg, "arena_agent_calls",
           {"agent": "trader-reader", "model_ran": "claude-sonnet-5", "thinking": "medium"},
           NOW - timedelta(minutes=2))  # fmt: skip
    assert S.check_agent_calls(cfg, NOW, EXPECT).ok

    _event(cfg, "arena_agent_calls",
           {"agent": "trader-reader", "model_ran": "claude-haiku-4-5", "thinking": "medium"},
           NOW - timedelta(minutes=1))  # fmt: skip
    c = S.check_agent_calls(cfg, NOW, EXPECT)
    assert not c.ok and "claude-haiku-4-5" in c.detail

    _event(cfg, "arena_agent_calls",
           {"agent": "trader-reader", "model_ran": "claude-sonnet-5", "thinking": "off"},
           NOW - timedelta(minutes=1))  # fmt: skip
    assert "off effort" in S.check_agent_calls(cfg, NOW, EXPECT).detail

    _event(cfg, "arena_agent_calls",
           {"agent": "trader-reader", "model_ran": "claude-sonnet-5", "thinking": "low"},
           NOW - timedelta(minutes=1))  # fmt: skip
    assert "low effort, config says medium" in S.check_agent_calls(cfg, NOW, EXPECT).detail


# -- 5. an order stuck pending ---------------------------------------------
class _FakeArena:
    def __init__(self, cfg, acct, resolve_after_minutes=22):
        self.cfg = cfg
        self._acct = acct
        self.broker = type("B", (), {"resolve_after_minutes": resolve_after_minutes})()

    def account(self, pb, kind):
        return self._acct if kind == "agent" else type("A", (), {"orders": {}})()


def _acct_with_pending(decided_at):
    from asxbot.arena.accounts import Account, ArenaOrder

    acct = Account(
        name="t__agent", playbook="p", kind="agent", level=1,
        starting_cash=10_000.0, cash=10_000.0,
    )  # fmt: skip
    acct.orders["ARN-1"] = ArenaOrder(
        order_id="ARN-1", account="t__agent", ticker="AAA", side="buy", qty=100, limit=1.0,
        decided_at=decided_at.isoformat(timespec="seconds"),
    )  # fmt: skip
    return acct


def test_an_order_stuck_past_its_resolve_window_is_caught(cfg):
    fresh = _FakeArena(cfg, _acct_with_pending(NOW - timedelta(minutes=30)))
    assert S.check_pending_orders(fresh, None, NOW).ok  # 22 min window + an hour of slack

    # Decided in yesterday's session. (NOW - 3h is 08:00, before today's open: not stuck.)
    stuck = _FakeArena(cfg, _acct_with_pending(datetime(2026, 9, 22, 14, 0, tzinfo=SYD)))
    c = S.check_pending_orders(stuck, None, NOW)
    assert not c.ok and "ARN-1" in c.detail


def test_a_pre_open_order_is_timed_from_the_open_not_from_its_decision(cfg):
    """The yardstick places its entries before 10:00; they cannot fill until the open, and
    an alarm every such morning would be an alarm nobody reads."""
    pre_open = _FakeArena(cfg, _acct_with_pending(datetime(2026, 9, 23, 7, 35, tzinfo=SYD)))
    assert S.check_pending_orders(pre_open, None, NOW).ok  # 3h25m old, 1h after the open
    late = datetime(2026, 9, 23, 11, 30, tzinfo=SYD)  # 90 min after the open: past 82
    assert not S.check_pending_orders(pre_open, None, late).ok


# -- 6. an ERROR in the log ------------------------------------------------
def _log_line(cfg, when: datetime, level: str, msg: str, logger: str = "asxbot.arena.watch"):
    p = cfg.logs_dir / "asxbot.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(f"{when:%Y-%m-%d %H:%M:%S},123 {level} {logger}: {msg}\n")


def test_an_error_logged_in_the_last_hour_is_caught(cfg):
    _log_line(cfg, NOW - timedelta(hours=4), "ERROR", "old news")
    _log_line(cfg, NOW - timedelta(minutes=10), "INFO", "all fine")
    assert S.check_errors_logged(cfg, NOW).ok

    # the exact line that killed every re-look on 23 September
    _log_line(cfg, NOW - timedelta(minutes=5), "ERROR",
              "re-look on A1M failed: decider_packet() takes from 6 to 7 positional arguments "
              "but 8 were given")  # fmt: skip
    c = S.check_errors_logged(cfg, NOW)
    assert not c.ok and "decider_packet()" in c.detail


# -- reporting: loud, then quiet, then clear -------------------------------
class _Alerting(_FakeArena):
    def __init__(self, cfg, acct):
        super().__init__(cfg, acct)
        self.sent = []
        self.broker.notifier = type(
            "N", (), {"send": lambda _self, text: self.sent.append(text)}
        )()


def test_a_failure_alerts_once_an_hour_and_clears_when_fixed(cfg, monkeypatch):
    from asxbot.alerts import Alerts

    arena = _Alerting(cfg, _acct_with_pending(NOW - timedelta(hours=3)))
    monkeypatch.setattr(S, "run_checks", lambda a, p, n: [S.Check("stuck_pending", False, "stuck")])

    assert len(S.report(arena, None, NOW)) == 1
    assert len(arena.sent) == 1 and "SELF-CHECK" in arena.sent[0]
    assert Alerts(cfg.data_dir).is_active("stuck_pending")

    S.report(arena, None, NOW + timedelta(minutes=5))  # still failing; not re-sent
    assert len(arena.sent) == 1
    S.report(arena, None, NOW + timedelta(minutes=75))  # an hour on; said again
    assert len(arena.sent) == 2

    monkeypatch.setattr(S, "run_checks", lambda a, p, n: [S.Check("stuck_pending", True, "clear")])
    assert S.report(arena, None, NOW + timedelta(hours=2)) == []
    assert not Alerts(cfg.data_dir).is_active("stuck_pending")


def test_a_check_that_itself_breaks_is_a_failure_not_a_crash(cfg, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("the check is broken")

    monkeypatch.setattr(S, "check_pdfs_are_pdfs", boom)
    arena = _FakeArena(cfg, _acct_with_pending(NOW))
    checks = S.run_checks(arena, None, NOW)
    bad = [c for c in checks if c.key == "pdf_not_pdf"]
    assert bad and not bad[0].ok and "the check itself failed" in bad[0].detail


def test_the_error_check_does_not_feed_on_its_own_alerts(cfg):
    """It did, within a minute of going live: raising an alert logs CRITICAL, the next
    cycle counted that line, and the count climbed on its own with nothing wrong."""
    _log_line(
        cfg, NOW - timedelta(minutes=3), "CRITICAL",
        "ALERT [errors_logged] 8 ERROR line(s)", "asxbot.alerts",
    )  # fmt: skip
    _log_line(
        cfg, NOW - timedelta(minutes=2), "CRITICAL",
        "SELF-CHECK FAILED [errors_logged] x", "asxbot.arena.selfcheck",
    )  # fmt: skip
    assert S.check_errors_logged(cfg, NOW).ok  # its own voice does not count

    _log_line(cfg, NOW - timedelta(minutes=1), "ERROR", "a real failure somewhere else")
    assert not S.check_errors_logged(cfg, NOW).ok


def test_a_standing_failure_is_not_logged_every_cycle(cfg, monkeypatch, caplog):
    import logging

    arena = _Alerting(cfg, _acct_with_pending(NOW))
    monkeypatch.setattr(S, "run_checks", lambda a, p, n: [S.Check("stuck_pending", False, "stuck")])
    with caplog.at_level(logging.INFO, logger="asxbot.arena.selfcheck"):
        S.report(arena, None, NOW)
        S.report(arena, None, NOW + timedelta(minutes=1))
    criticals = [
        r
        for r in caplog.records
        if r.levelno >= logging.CRITICAL and r.name == "asxbot.arena.selfcheck"
    ]
    assert len(criticals) == 1  # shouted once, not once a minute


# -- 7. the short universe that shrinks without erroring -------------------
def _universe(cfg, codes, as_of="2026-09-23", members=True):
    import pandas as pd

    udir = cfg.data_dir / "universe"
    udir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {"code": list(codes), "as_of": as_of, "source": "a constituent list"}
    ).to_csv(udir / "asx200_members.csv", index=False)


def _letters(n, start=0):
    from itertools import product
    from string import ascii_uppercase

    return ["".join(c) for c in product(ascii_uppercase, repeat=3)][start : start + n]


def test_a_full_current_short_universe_passes(cfg):
    _universe(cfg, _letters(200), as_of=datetime.now(SYD).date().isoformat())
    c = S.check_short_universe(cfg)
    assert c.ok and "200 ASX 200 codes" in c.detail


def test_a_shrunken_short_universe_is_caught(cfg):
    _universe(cfg, _letters(150), as_of=datetime.now(SYD).date().isoformat())
    c = S.check_short_universe(cfg)
    assert not c.ok and "only 150 codes" in c.detail
    assert "refused without an error" in c.detail


def test_a_short_universe_that_stopped_refreshing_is_caught(cfg):
    _universe(cfg, _letters(200), as_of="2026-01-01")
    c = S.check_short_universe(cfg)
    assert not c.ok and "has not refreshed" in c.detail


def test_a_market_cap_proxy_is_reported_as_not_the_index(cfg):
    """The 23 September fault itself: 'ASX 200' that was really the 200 largest."""
    import pandas as pd

    udir = cfg.data_dir / "universe"
    udir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {"code": _letters(400), "name": "x", "industry": "y", "listing_date": "",
         "market_cap": [float(10_000 - i) for i in range(400)]}
    ).to_csv(udir / "asx_directory.csv", index=False)  # fmt: skip
    c = S.check_short_universe(cfg)
    assert not c.ok and "not a constituent list" in c.detail


# -- alerting on new findings, not just on state ---------------------------
def test_a_new_error_inside_the_same_hour_is_still_reported(cfg, monkeypatch):
    """The gap: the state said 'already failing', so a genuinely new error was silent."""
    arena = _Alerting(cfg, _acct_with_pending(NOW))
    first = S.Check("errors_logged", False, "1 ERROR", items=["10:22 boom"])
    monkeypatch.setattr(S, "run_checks", lambda a, p, n: [first])
    S.report(arena, None, NOW)
    assert len(arena.sent) == 1

    # same finding, five minutes later: silence, as before
    S.report(arena, None, NOW + timedelta(minutes=5))
    assert len(arena.sent) == 1

    # a DIFFERENT error, still inside the hour: this must speak
    second = S.Check("errors_logged", False, "2 ERRORs", items=["10:22 boom", "10:40 a new one"])
    monkeypatch.setattr(S, "run_checks", lambda a, p, n: [second])
    S.report(arena, None, NOW + timedelta(minutes=10))
    assert len(arena.sent) == 2
    assert "NEW since the last report" in arena.sent[1] and "10:40 a new one" in arena.sent[1]
    assert "10:22 boom" not in arena.sent[1]  # the message carries only what is new

    # and it is not repeated either
    S.report(arena, None, NOW + timedelta(minutes=12))
    assert len(arena.sent) == 2


# -- 8. a log that has stopped being written (24 Sep 2026, 08:14) ----------
# Google Drive cut off the watcher's long-open append handles. The watcher went on trading
# and logging; asxbot.log and the launcher's arena_warmup.log both stopped at 08:14:12, and
# the heartbeat (rewritten whole) stayed fresh, so nothing noticed until 08:23.
T0814 = datetime(2026, 9, 24, 8, 14, 12, tzinfo=SYD)


def _beat(record: datetime | None, state: str = "running") -> dict:
    return {
        "pid": 130492, "started": datetime(2026, 9, 24, 7, 30, 1, tzinfo=SYD).isoformat(),
        "beat": (record or T0814).isoformat(), "last_activity": (record or T0814).isoformat(),
        "last_record": record.isoformat() if record else None, "state": state,
    }  # fmt: skip


def _stamped(path, when: datetime, msg: str = "asxbot.data.yf: yfinance batch 1-150 of 381"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{when:%Y-%m-%d %H:%M:%S},835 INFO {msg}\n")


def test_the_08_14_failure_is_caught_once_the_log_is_5_minutes_behind(cfg, tmp_path):
    log = cfg.logs_dir / "asxbot.log"
    _stamped(log, T0814)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("Traceback (most recent call last):\n  a line with no stamp\n")
    out = tmp_path / "arena_warmup.log"
    _stamped(out, T0814)

    # Still logging a few minutes later: not yet 5 minutes behind.
    near = T0814 + timedelta(minutes=4, seconds=59)
    assert S.check_log_growing(cfg, _beat(near), str(out)).ok

    c = S.check_log_growing(cfg, _beat(T0814 + timedelta(minutes=5)), str(out))
    assert not c.ok and c.count == 2
    assert "asxbot.log (last line 08:14:12)" in c.detail
    assert "arena_warmup.log (last line 08:14:12)" in c.detail
    assert "logged at 08:19:12" in c.detail


def test_a_log_that_keeps_up_is_fine_and_either_file_alone_is_caught(cfg, tmp_path):
    later = T0814 + timedelta(minutes=9)
    _stamped(cfg.logs_dir / "asxbot.log", later)
    out = tmp_path / "arena_warmup.log"
    _stamped(out, later - timedelta(seconds=2))
    assert S.check_log_growing(cfg, _beat(later), str(out)).ok

    _stamped(out, T0814)  # an older line appended last: the file's last line is what counts
    c = S.check_log_growing(cfg, _beat(later), str(out))
    assert not c.ok and c.items == [f"{out} (last line 08:14:12)"]


def test_nothing_to_hold_the_log_to_is_not_a_failure(cfg):
    assert S.check_log_growing(cfg, _beat(None)).ok  # a heartbeat without last_record
    stopped = _beat(T0814 + timedelta(hours=1), state="stopped")
    assert S.check_log_growing(cfg, stopped).ok  # the watcher has stopped
    assert S.check_log_growing(cfg, {}).ok  # no heartbeat at all
    # A log with no line yet is measured from the watcher's start (07:30:01).
    assert S.check_log_growing(cfg, _beat(datetime(2026, 9, 24, 7, 33, tzinfo=SYD))).ok
    c = S.check_log_growing(cfg, _beat(datetime(2026, 9, 24, 7, 36, tzinfo=SYD)))
    assert not c.ok and "no timestamped line at all" in c.detail


def test_the_check_reads_the_heartbeat_file_and_the_launcher_log_it_is_told_of(
    cfg, tmp_path, monkeypatch
):
    from asxbot.arena import heartbeat as H
    from asxbot.io import write_text_atomic

    beat = _beat(T0814 + timedelta(minutes=6))
    write_text_atomic(json.dumps(beat), H.heartbeat_path(cfg.data_dir))
    _stamped(cfg.logs_dir / "asxbot.log", T0814 + timedelta(minutes=6))
    out = tmp_path / "arena_warmup.log"
    _stamped(out, T0814)
    assert S.check_log_growing(cfg).ok  # not told of the launcher's log: only asxbot.log
    monkeypatch.setenv("ASXBOT_STDOUT_LOG", str(out))
    c = S.check_log_growing(cfg)
    assert not c.ok and "arena_warmup.log" in c.detail
    checks = S.run_checks(_FakeArena(cfg, _acct_with_pending(NOW)), None, NOW)
    assert [k.ok for k in checks if k.key == "log_silent"] == [False]  # it runs every cycle


def test_a_silent_log_is_shouted_to_telegram_once(cfg, monkeypatch):
    from asxbot.alerts import Alerts

    _stamped(cfg.logs_dir / "asxbot.log", T0814)
    beat = {"at": T0814 + timedelta(minutes=6)}
    monkeypatch.setattr(
        S, "run_checks", lambda a, p, n: [S.check_log_growing(cfg, _beat(beat["at"]), "")]
    )
    arena = _Alerting(cfg, _acct_with_pending(NOW))
    for minute in range(6, 12):  # every watcher cycle, still silent
        beat["at"] = T0814 + timedelta(minutes=minute)
        S.report(arena, None, beat["at"])
    assert len(arena.sent) == 1
    assert "SELF-CHECK: log_silent" in arena.sent[0] and "08:14:12" in arena.sent[0]
    assert Alerts(cfg.data_dir).is_active("log_silent")

    _stamped(cfg.logs_dir / "asxbot.log", T0814 + timedelta(minutes=12))  # written again
    beat["at"] = T0814 + timedelta(minutes=12)
    assert S.report(arena, None, beat["at"]) == []
    assert not Alerts(cfg.data_dir).is_active("log_silent") and len(arena.sent) == 1


def test_the_heartbeat_records_the_last_log_line_apart_from_waits(cfg):
    import logging

    from asxbot.arena import heartbeat as H

    hb = H.Heartbeat(cfg.data_dir, beat_s=3600)
    assert hb.payload()["last_record"] is None
    line = logging.LogRecord("asxbot.x", logging.INFO, __file__, 1, "hi", None, None)
    line.created -= 60  # a minute ago
    hb._handler.emit(line)
    first = hb.last_record
    assert first is not None and hb.payload()["last_record"] == first.isoformat(timespec="seconds")
    with hb.quiet(1, "a model call"):
        pass
    assert hb.last_record == first  # the end of a wait is activity, not a line in the log
    assert hb.last_activity > first
