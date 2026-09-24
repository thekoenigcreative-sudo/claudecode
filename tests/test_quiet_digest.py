"""The hourly digest only when the hour had something; nothing after the session summary.

24 Sep 2026: the Trader sent "nothing passed this hour" at 11:33, 12:33, 13:33, 14:33,
15:34, 16:34, 17:35 and 18:35, and a last digest at 19:25 when the watcher stopped - four
of them after the 16:10 summary. No network.
"""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from asxbot.config import load_config

SYD = ZoneInfo("Australia/Sydney")
DAY = datetime(2026, 9, 24, tzinfo=SYD)


def at(hh: int, mm: int, ss: int = 0) -> datetime:
    return DAY.replace(hour=hh, minute=mm, second=ss)


@pytest.fixture
def cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )


@pytest.fixture
def notifier(cfg, monkeypatch):
    from asxbot.arena.notify import Notifier

    sent: list[str] = []
    n = Notifier(cfg)
    monkeypatch.setattr(n, "send", lambda text: sent.append(text) or True)
    return n, sent


def _order(cfg, when: datetime, **kw) -> None:
    """An order the broker recorded at `when` (the event log stamps UTC)."""
    p = cfg.data_dir / "events" / "arena_orders.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": when.astimezone(UTC).isoformat(timespec="seconds"),
        "kind": "arena_orders",
        "event": "submitted",
        "placed_by": "agent",
        "ticker": "AAA",
        "side": "buy",
        "qty": 3000,
        **kw,
    }
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def test_24_sep_replayed_sends_one_digest_and_the_summary(notifier):
    """The day as it ran: passes before 10:32, then nothing but counts. Fails on a8fc3f3,
    which sends the eight quiet digests and the 19:25 one."""
    n, sent = notifier
    n.flush_passes(at(9, 31, 20))  # the watcher's first cycle starts the clock
    n.passed("BTR", "Sandstone PFS Metallurgy", "already priced in", at(10, 5))
    assert n.flush_passes(at(10, 32, 49)) is True
    for hh, mm in ((11, 33), (12, 33), (13, 33), (14, 33), (15, 34)):
        assert n.flush_passes(at(hh, mm, 30)) is False
    assert n.session_summary("SESSION DONE", at(16, 10, 39)) is True
    # After the summary: a pass and a screen-out still arrive, and nothing is sent.
    n.passed("LKE", "A$3.0 Million At-the-Market Raise", "a small raise", at(17, 11))
    n.screened_out("RCT", "Bidder's Statement", "no trades at all by 17:17", at(17, 17))
    for hh, mm in ((16, 34), (17, 35), (18, 35)):
        assert n.flush_passes(at(hh, mm, 45)) is False
    assert n.flush_passes(at(19, 25, 7), force=True) is False  # the watcher stopping
    assert len(sent) == 2
    assert "BTR" in sent[0] and sent[1] == "SESSION DONE"
    assert not any("nothing passed" in s for s in sent)


def test_what_arrived_after_the_summary_goes_in_the_next_days_first_digest(notifier):
    n, sent = notifier
    n.flush_passes(at(9, 31))
    n.session_summary("SESSION DONE", at(16, 10))
    n.passed("LKE", "A$3.0 Million At-the-Market Raise", "a small raise", at(17, 11))
    assert n.flush_passes(at(19, 25), force=True) is False
    friday = at(7, 30) + timedelta(days=1)
    assert n.flush_passes(friday) is True
    assert "Thu 16:10–07:30" in sent[-1] and "LKE" in sent[-1] and "(Thu 17:11)" in sent[-1]


def test_the_summary_sends_the_last_part_hour_first_if_it_had_anything(notifier):
    n, sent = notifier
    n.flush_passes(at(15, 34))
    n.passed("SGR", "Licence Suspension Deferred", "no edge", at(15, 58))
    assert n.session_summary("SESSION DONE", at(16, 10)) is True
    assert len(sent) == 2 and "SGR" in sent[0] and "15:34–16:10" in sent[0]
    assert sent[1] == "SESSION DONE"


def test_a_quiet_last_part_hour_adds_nothing_to_the_summary(notifier):
    n, sent = notifier
    n.flush_passes(at(15, 34))
    assert n.session_summary("SESSION DONE", at(16, 10)) is True
    assert sent == ["SESSION DONE"]


def test_an_order_in_the_hour_is_enough_for_a_digest(cfg, notifier):
    n, sent = notifier
    n.flush_passes(at(10, 0))
    _order(cfg, at(9, 59), ticker="OLD")  # before the hour: not this hour's
    _order(cfg, at(10, 20), placed_by="bot", ticker="OFX", side="buy", qty=1200)
    assert n.flush_passes(at(11, 1)) is True
    assert "orders this hour (1)" in sent[0] and "BOT BUY <b>OFX</b> 1,200 (10:20)" in sent[0]
    assert "OLD" not in sent[0]
    assert n.flush_passes(at(12, 2)) is False  # that order does not make the next hour


def test_a_screen_out_worth_reading_is_enough_for_a_digest(notifier):
    n, sent = notifier
    n.flush_passes(at(10, 32))
    n.screened_out(
        "MHC", "Drilling Delivers 21.34m at 1.72g/t Au",
        "no trades at all by 10:59, which usually means halted or suspended", at(10, 59),
    )  # fmt: skip
    assert n.flush_passes(at(11, 33)) is True
    assert "screened out, worth a look (1)" in sent[0] and "MHC" in sent[0]
    assert "no trades at all by 10:59" in sent[0]


def test_which_screen_outs_are_worth_reading():
    """Screen-outs decided by today's market data are listed; standing facts are counts.
    The verdicts are 24 Sep's real ones."""
    from asxbot.announcements.model import Announcement
    from asxbot.arena.tradability import Screen, worth_reading

    def ann(headline):
        return Announcement(
            code="AAA", released_at=datetime(2026, 9, 24, 10, 0), headline=headline,
            price_sensitive=True, ids_id="1", pdf_url="",
        )  # fmt: skip

    no_trades = Screen(False, "no trades at all by 10:59, which usually means halted", "halted")
    notice = Screen(False, "the announcement is a halt or suspension (Trading Halt)", "halted")
    assert worth_reading(no_trades, ann("Drilling Delivers 21.34m")) is True
    assert worth_reading(notice, ann("Trading Halt")) is False
    assert worth_reading(Screen(False, "no live quote", "no_quote"), ann("Placement")) is True
    assert worth_reading(Screen(False, "no history", "no_history"), ann("Update")) is True
    assert worth_reading(Screen(False, "one tick is 12.50%", "tick"), ann("Update")) is False
    assert worth_reading(Screen(False, "median turnover", "turnover"), ann("Update")) is False
    assert worth_reading(Screen(True, "tradeable"), ann("Update")) is False
