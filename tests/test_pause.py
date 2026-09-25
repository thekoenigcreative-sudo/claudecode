"""Rick's "no new entries today" switch end to end (arena/pause.py, 26 Sep 2026): the chat
writes the day's file, the watcher's view refuses every new entry that day, the next day is
clear, and resuming lifts it. Exits never ask (arena_place_order refuses openings only -
tests/test_books_review.py)."""

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from asxbot.arena import pause as P
from asxbot.arena.intraday import MarketView, entries_allowed

SYD = ZoneInfo("Australia/Sydney")
MON = datetime(2026, 9, 28, 11, 0, tzinfo=SYD)


def view(tmp_path, feed_ok=True):
    feed = SimpleNamespace(entries_allowed=lambda now, codes: (feed_ok, "" if feed_ok else "x"))
    v = MarketView(None, MON.date(), feed)
    v.pause_dir = tmp_path
    return v


def test_a_pause_stops_new_entries_for_the_day_and_only_that_day(tmp_path):
    v = view(tmp_path)
    assert entries_allowed(v, MON) == (True, "")
    P.set_pause(tmp_path, MON.replace(hour=10, minute=42), "Rick", "stop it for today")
    ok, why = entries_allowed(v, MON, ["BHP"])
    assert not ok and "entries paused for today by Rick at 10:42" in why
    tue = datetime(2026, 9, 29, 10, 5, tzinfo=SYD)
    assert entries_allowed(view(tmp_path), tue) == (True, "")  # the file is Monday's
    assert P.clear_pause(tmp_path, MON, "Rick")
    assert entries_allowed(v, MON) == (True, "")


def test_the_replay_and_tests_have_no_pause_dir(tmp_path):
    P.set_pause(tmp_path, MON, "Rick")
    v = MarketView(None, date(2026, 9, 28), SimpleNamespace())
    assert v.pause_dir is None and entries_allowed(v, MON) == (True, "")


def test_an_unreadable_pause_file_is_still_a_pause(tmp_path):
    p = P.pause_path(tmp_path, MON.date())
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    assert P.paused(tmp_path, MON)[0] is True


def test_the_watchers_view_carries_the_pause_folder(monkeypatch, tmp_path):
    from asxbot.arena import watch as W

    W._VIEW.clear()
    monkeypatch.setattr("asxbot.arena.intraday.make_feed", lambda cfg, minutes: SimpleNamespace())
    cfg = SimpleNamespace(data_dir=tmp_path, get=lambda k, d=None: d)
    arena = SimpleNamespace(cfg=cfg, broker=SimpleNamespace(minutes=None))
    v = W.day_view(arena, MON)
    assert v.pause_dir == tmp_path
    W._VIEW.clear()
