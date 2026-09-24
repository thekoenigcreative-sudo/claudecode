"""Fixes from the night of 23-24 Sep 2026: the session summary sent more than once (#4), the
yardstick's late start logged as an error (#5). No network."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from asxbot.config import load_config

SYD = ZoneInfo("Australia/Sydney")


@pytest.fixture
def cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )


def _notifier(cfg, monkeypatch, sent, ok=True):
    from asxbot.arena.notify import Notifier

    n = Notifier(cfg)
    monkeypatch.setattr(n, "send", lambda text: sent.append(text) or ok)
    return n


# -- #4: the session summary, once per trading day -----------------------------------
def test_the_hourly_digest_does_not_re_arm_the_session_summary(cfg, monkeypatch):
    """23 Sep: the watcher restarted at 15:56, so its digest ran at :56. The summary went at
    16:10, and again after every digest: 16:56, 17:56, 18:57. Fails on ceefbfe."""
    sent: list[str] = []
    n = _notifier(cfg, monkeypatch, sent)
    start = datetime(2026, 9, 23, 15, 56, 8, tzinfo=SYD)
    n.flush_passes(start)  # starts the digest clock
    assert n.session_summary("SESSION DONE", start.replace(hour=16, minute=10)) is True
    for hour in (16, 17, 18):
        digest = start.replace(hour=hour, minute=56, second=35)
        n.passed("AAA", "Quarterly report", "too small", digest - timedelta(minutes=5))
        assert n.flush_passes(digest) is False  # no digest after the summary (24 Sep)
        assert n.session_summary("SESSION DONE", digest + timedelta(seconds=11)) is False
    # A restart (a fresh notifier reading the same state) does not send it again either.
    again = _notifier(cfg, monkeypatch, sent)
    assert again.session_summary("SESSION DONE", start.replace(hour=19, minute=20)) is False
    assert sum(1 for s in sent if s == "SESSION DONE") == 1
    # The next trading day gets its own.
    assert again.session_summary("SESSION DONE", start + timedelta(days=1, minutes=20)) is True


def test_a_summary_telegram_did_not_take_is_tried_again(cfg, monkeypatch):
    sent: list[str] = []
    n = _notifier(cfg, monkeypatch, sent, ok=False)
    at = datetime(2026, 9, 23, 16, 10, tzinfo=SYD)
    assert n.session_summary("SESSION DONE", at) is False
    monkeypatch.setattr(n, "send", lambda text: sent.append(text) or True)
    assert n.session_summary("SESSION DONE", at + timedelta(minutes=1)) is True
    assert n.session_summary("SESSION DONE", at + timedelta(minutes=2)) is False


# -- #5: a late start is a recorded miss, not an error --------------------------------
def test_a_watcher_started_after_the_open_records_the_miss_as_a_warning(cfg, caplog):
    """23 Sep: the watcher restarted at 15:56; "the yardstick missed the open" was logged as
    ERROR and tripped errors_logged, CRITICAL on Telegram. Fails on ceefbfe."""
    import dataclasses
    import json
    import logging

    from asxbot.arena import watch as W
    from v1_playbook import v1_playbook

    pb = v1_playbook(cfg)
    pb = dataclasses.replace(pb, raw={**pb.raw, "warmup_start": None})

    class _Arena:
        pass

    arena = _Arena()
    arena.cfg = cfg
    at = datetime(2026, 1, 6, 15, 56, 54, tzinfo=SYD)
    with caplog.at_level(logging.INFO):
        assert W.yardstick_entries(arena, pb, at) == []
    missed = [r for r in caplog.records if "missed" in r.getMessage()]
    assert missed and all(r.levelno == logging.WARNING for r in missed)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert "restarted late" in missed[0].getMessage()
    state = json.loads(W._yardstick_path(cfg.data_dir, at.date()).read_text())
    assert state["status"] == "missed" and "first reached at 15:56" in state["missed"]


# -- #7: dated membership is authoritative; memory never overrules it ------------------
def _members_csv(cfg, codes, as_of="2026-09-23"):
    import pandas as pd

    udir = cfg.data_dir / "universe"
    udir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "code": sorted(codes),
            "name": sorted(codes),
            "as_of": as_of,
            "source": "https://en.wikipedia.org/wiki/S%26P/ASX_200",
        }  # fmt: skip
    ).to_csv(udir / "asx200_members.csv", index=False)


def test_the_decider_packet_gives_the_dated_list_and_forbids_overruling_it(cfg, monkeypatch):
    """23 Sep: the decider and the evening report both said NUF's "outside the ASX 200" flag
    "looks wrong for a $1.2bn index member". The list was right: Nufarm left the index in
    the September 2025 rebalance. Fails on ceefbfe, whose packet had no date or rule."""
    from asxbot.arena import watch as W
    from asxbot.arena.broker import ArenaBroker
    from asxbot.arena.minutes import MinuteBars
    from asxbot.backtest.costs import CostModel
    from test_arena import _ann, _FakeArena, _offline_price
    from v1_playbook import v1_playbook

    _offline_price(monkeypatch)
    _members_csv(cfg, {"BBB", "ORA", "TUA"})
    broker = ArenaBroker(cfg.data_dir, CostModel.from_config(cfg),
                         MinuteBars(cfg.data_dir), lambda t: 2e6)  # fmt: skip
    acct = broker.store.open("t__agent", "asx_announcements", "agent", 1, 20_000.0)
    arena = _FakeArena(cfg, broker, acct)
    arena.short_universe = {"BBB", "ORA", "TUA"}
    pb = v1_playbook(cfg)
    at = datetime(2026, 9, 23, 10, 32, tzinfo=SYD)
    ctx = {"dossier": {}, "reaction": {}, "text": ""}
    packet = W.decider_packet(arena, pb, acct, _ann(code="NUF"), ctx, "s", at)
    assert "INDEX MEMBERSHIP - authoritative and dated" in packet
    assert "NUF is NOT in the S&P/ASX 200 according to S&P/ASX 200 constituent list" in packet
    assert "as of 2026-09-23 (3 members)" in packet
    assert "Do not overrule it from memory" in packet
    assert '"flag_for_claude"' in packet
    member = W.decider_packet(arena, pb, acct, _ann(code="TUA"), ctx, "s", at)
    assert "TUA is IN the S&P/ASX 200" in member


def test_a_flag_for_claude_is_recorded_not_acted_on(cfg):
    from asxbot.arena.agents import parse_decision

    d = parse_decision(
        'reasoning...\n{"action": "pass", "why": "too thin", '
        '"flag_for_claude": "Is NUF really outside the ASX 200?"}'
    )
    assert d["action"] == "pass" and d["flag_for_claude"].startswith("Is NUF")


def test_the_report_brief_carries_the_list_date_and_the_rule():
    from asxbot.arena.report import agent_brief

    facts = {"day": "2026-09-23", "asx200_list": {"list": "S&P/ASX 200 constituent list",
             "as_of": "2026-09-23", "members": 200}, "flags_for_claude": []}  # fmt: skip
    brief = agent_brief(facts)
    assert "authoritative and dated (as of 2026-09-23)" in brief
    assert "Flag for Claude:" in brief and "looks wrong" in brief


def test_both_decider_agents_files_say_the_list_is_authoritative():
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1] / "docs" / "agents" / "trader-decider.AGENTS.md"
    text = repo.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "Where the packet gives a dated fact, the packet is right." in text
    assert "Never overrule it from memory" in text and "`flag_for_claude`" in text
    live = Path.home() / ".openclaw" / "workspace-trader-decider" / "AGENTS.md"
    if live.exists():  # the copy the agent actually reads, on Rick's PC
        assert live.read_text(encoding="utf-8").replace("\r\n", "\n") == text


# -- #27 (item 2): the live yardstick measures the gap against the index's real open ------
def _yardstick_setup(cfg):
    import dataclasses

    from asxbot.arena.bots.announcement_drift import AnnouncementDriftBot
    from asxbot.arena.broker import ArenaBroker
    from asxbot.arena.minutes import MinuteBars
    from asxbot.backtest.costs import CostModel
    from test_arena import T0, _ann_frame, _index_daily, _stock_daily
    from v1_playbook import v1_playbook

    broker = ArenaBroker(cfg.data_dir, CostModel.from_config(cfg), MinuteBars(cfg.data_dir),
                         lambda t: 2e6)  # fmt: skip
    acct = broker.store.open("t__bot", "asx_announcements", "bot", 1, 20_000.0)
    pb = v1_playbook(cfg)
    # Yahoo's daily index: flat, and its "open" is the previous close, as on most days.
    frames = {"^AXJO": _index_daily(), "AAA": _stock_daily(5.5, 4)}

    def fetch(tickers, start):
        return {t: frames[t] for t in tickers if t in frames}

    def bot(pb=pb):
        return AnnouncementDriftBot(cfg, pb, broker, {"AAA"}, fetch)

    ann = _ann_frame(("AAA", "2026-01-05 08:30", True))
    return broker, acct, pb, bot, ann, T0, dataclasses


def test_the_gap_is_measured_against_the_index_open_from_its_first_minute(cfg):
    """AAA opens 5.5% up on a morning the index really opened 1% up: 4.5% relative, below
    the rule's 5%. Against Yahoo's daily open (the previous close) it read as 5.5% and
    passed. Fails on ceefbfe."""
    from test_arena import _index_minutes

    broker, acct, pb, bot, ann, t0, dataclasses = _yardstick_setup(cfg)
    at = datetime(2026, 1, 6, 7, 45, tzinfo=SYD)
    assert pb.raw["yardstick"]["index_open"] == "first_minute_bar"
    _index_minutes(broker, t0, open_=1010.0)
    out, _ = bot().entries(acct, ann, t0, at)
    assert out == []
    _index_minutes(broker, t0, open_=1000.0)  # a flat open: 5.5% relative, an event
    out, _ = bot().entries(acct, ann, t0, at)
    assert [d.ticker for d in out] == ["AAA"]
    daily = dataclasses.replace(
        pb, raw={**pb.raw, "yardstick": {**pb.raw["yardstick"], "index_open": "daily"}}
    )
    _index_minutes(broker, t0, open_=1010.0)
    out, _ = bot(daily).entries(acct, ann, t0, at)
    assert [d.ticker for d in out] == ["AAA"]  # the old source: a raw gap


def test_no_index_minute_bar_is_a_wait_not_a_fallback(cfg):
    from asxbot.arena.bots.announcement_drift import NotReady

    broker, acct, pb, bot, ann, t0, _ = _yardstick_setup(cfg)
    with pytest.raises(NotReady, match="no 10:00 minute bar"):
        bot().entries(acct, ann, t0, datetime(2026, 1, 6, 7, 45, tzinfo=SYD))


# -- #8: a PDF missed once is fetched when it matters; every failure says why ------------
def _announcement(code="TUA", ids_id="03142537"):
    from asxbot.announcements.model import Announcement

    return Announcement(
        code,
        datetime(2026, 9, 23, 8, 26),
        "Appendix 4E",
        True,
        ids_id,
        "https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do",
    )


class _PdfArena:
    def __init__(self, cfg, fetch):
        self.cfg, self.fetch_pdf = cfg, fetch


def test_an_announcement_reaching_the_reader_without_its_pdf_gets_it_fetched(cfg):
    """23 Sep: TUA's FY26 Appendix 4E was re-looked at 10:21 and 10:33 with "no PDF on
    disk": the terms page saved at 08:26 had been deleted at 09:12 and nothing fetched it
    again, though ASX served it fine. Fails on ceefbfe, which never fetched it again."""
    from asxbot.announcements.live import pdf_path
    from asxbot.arena import watch as W

    a = _announcement()
    calls = []

    def fetch(ann, stage="poll"):
        calls.append((ann.code, stage))
        p = pdf_path(cfg.data_dir, ann)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"%PDF-1.7\n")
        return p

    W._ensure_pdf(_PdfArena(cfg, fetch), a)
    assert calls == [("TUA", "reader")]
    W._ensure_pdf(_PdfArena(cfg, fetch), a)  # a real PDF on disk: no second request
    assert calls == [("TUA", "reader")]


def test_a_refusal_while_fetching_for_the_reader_does_not_stop_the_decision(cfg):
    from asxbot.announcements.http import AccessRefused
    from asxbot.arena import watch as W

    def refused(ann, stage="poll"):
        raise AccessRefused("HTTP 403")

    W._ensure_pdf(_PdfArena(cfg, refused), _announcement())  # logged, not raised


def test_the_poller_records_why_a_fetch_failed(cfg):
    from asxbot.alerts import Alerts
    from asxbot.announcements.http import PdfFetchFailed
    from asxbot.announcements.live import LivePoller
    from asxbot.log import EventLog

    class _Client:
        def get_bytes(self, url, dest):
            raise PdfFetchFailed("timeout", "read timed out (after 4 attempts)")

    poller = LivePoller(cfg.data_dir, _Client(), Alerts(cfg.data_dir), {"TUA"})
    assert poller.fetch_pdf(_announcement(), stage="reader") is None
    (ev,) = EventLog(cfg.data_dir).read("announcement_pdf_failures")
    assert ev["reason"] == "timeout" and ev["stage"] == "reader" and ev["code"] == "TUA"


def test_the_pdf_self_check_says_why(cfg):
    from asxbot.arena.selfcheck import check_pdf_fetches
    from asxbot.log import EventLog

    ev = EventLog(cfg.data_dir)
    ev.append("announcement_pdf_failures", {"code": "TUA", "ids_id": "03142537",
                                            "reason": "no document for the reader: no PDF "
                                                      "on disk"})  # fmt: skip
    ev.append("announcement_pdf_failures", {"code": "NUF", "ids_id": "03142520",
                                            "reason": "timeout"})  # fmt: skip
    c = check_pdf_fetches(cfg, datetime.now(SYD))
    assert not c.ok
    assert "timeout x1" in c.detail and "no PDF on disk" in c.detail
    assert "TUA 03142537 (no document for the reader: no PDF on disk)" in c.items
