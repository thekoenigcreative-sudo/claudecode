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
EXPECT = {"trader-reader": ("anthropic/claude-sonnet-5", "low")}


def test_a_downgraded_model_or_effort_level_is_caught(cfg):
    _event(cfg, "arena_agent_calls",
           {"agent": "trader-reader", "model_ran": "claude-sonnet-5", "thinking": "low"},
           NOW - timedelta(minutes=2))  # fmt: skip
    assert S.check_agent_calls(cfg, NOW, EXPECT).ok

    _event(cfg, "arena_agent_calls",
           {"agent": "trader-reader", "model_ran": "claude-haiku-4-5", "thinking": "low"},
           NOW - timedelta(minutes=1))  # fmt: skip
    c = S.check_agent_calls(cfg, NOW, EXPECT)
    assert not c.ok and "claude-haiku-4-5" in c.detail

    _event(cfg, "arena_agent_calls",
           {"agent": "trader-reader", "model_ran": "claude-sonnet-5", "thinking": "off"},
           NOW - timedelta(minutes=1))  # fmt: skip
    assert "off effort" in S.check_agent_calls(cfg, NOW, EXPECT).detail


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
        decision_at=decided_at.isoformat(timespec="seconds"),
    )  # fmt: skip
    return acct


def test_an_order_stuck_past_its_resolve_window_is_caught(cfg):
    fresh = _FakeArena(cfg, _acct_with_pending(NOW - timedelta(minutes=30)))
    assert S.check_pending_orders(fresh, None, NOW).ok  # 22 min window + an hour of slack

    stuck = _FakeArena(cfg, _acct_with_pending(NOW - timedelta(hours=3)))
    c = S.check_pending_orders(stuck, None, NOW)
    assert not c.ok and "ARN-1" in c.detail


# -- 6. an ERROR in the log ------------------------------------------------
def _log_line(cfg, when: datetime, level: str, msg: str, logger: str = "asxbot.arena.watch"):
    p = cfg.data_dir / "logs" / "asxbot.log"
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
