"""The one-off $10,000 top-up of the arena accounts: its safety checks, and that it moves
cash and starting cash together and nothing else. No network, no real account files."""

import json
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from asxbot.arena import capital as C
from asxbot.arena.accounts import AccountStore, Mark
from asxbot.config import load_config

SYD = ZoneInfo("Australia/Sydney")
WED = date(2026, 9, 23)
EVENING = datetime(2026, 9, 23, 21, 0, tzinfo=SYD)


@pytest.fixture
def cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )


@pytest.fixture
def books(cfg):
    """Both accounts as they stood on 23 Sep: $10,000 books, one holding A1M, a day's mark."""
    store = AccountStore(cfg.data_dir)
    agent = store.open("asx_announcements__agent", "asx_announcements", "agent", 1, 10_000.0)
    bot = store.open("asx_announcements__bot", "asx_announcements", "bot", 1, 10_000.0)
    agent.cash, agent.fees_paid = 7_500.89, 6.6
    store.save(agent)
    for acct, equity in ((agent, 10_244.11), (bot, 10_000.0)):
        store.write_mark(acct.name, Mark(day=WED.isoformat(), equity=equity, cash=acct.cash,
                                         gross_exposure=0.0, positions=0, realised_day=0.0,
                                         fees_day=0.0, note="prev equity 10,000.00"))  # fmt: skip
    return store


def test_the_watcher_and_the_evening_routine_are_seen_but_this_process_is_not():
    listing = [
        (10, 1, r'powershell.exe -File "G:\My Drive\asx-bot\scripts\arena_warmup.ps1"'),
        (11, 10, r'"C:\venvs\asx-bot\Scripts\asxbot.exe" arena watch --until auto'),
        (12, 1, r'"C:\venvs\asx-bot\Scripts\python.exe" "C:\venvs\asx-bot\Scripts\asxbot.exe" '
                r"arena resolve"),
        (13, 1, r'C:\venvs\asx-bot\Scripts\pythonw.exe "G:\My Drive\asx-bot'
                r'\scripts\arena_warmup.pyw"'),  # the hidden launcher
        (20, 1, "powershell.exe"),
        (21, 20, r'python.exe "G:\My Drive\asx-bot\scripts\arena_add_capital.py"'),
        (30, 1, r"C:\venvs\etf-agent\Scripts\python.exe core_read.py links.json VAS VGS"),
        (40, 1, r'C:\venvs\asx-bot\Scripts\pythonw.exe "G:\My Drive\asx-bot'
                r'\scripts\arena_watchdog.pyw"'),  # it only reads; it must not block the top-up
    ]  # fmt: skip
    found = C.arena_processes(listing, me=21)
    assert [f.split(":")[0] for f in found] == ["pid 10", "pid 11", "pid 12", "pid 13"]
    assert C.arena_processes(listing[4:], me=21) == []


LOG = """skipped 2026-09-22 20:52: not today's evening slot

=== evening report 2026-09-23 19:30 ===
--- asxbot arena resolve ---
nothing to resolve
--- asxbot arena mark ---
asx_announcements__agent: equity 10,244.11
--- asxbot arena report --agent --send ---
"""


def test_the_evening_must_have_returned_from_its_report_step():
    slot = time(19, 30)
    assert C.evening_finished(LOG + "report sent\n", WED, slot) is None
    assert C.evening_finished(LOG + "\n", WED, slot) is None  # an empty output still returned
    assert "not returned" in C.evening_finished(LOG, WED, slot)  # still running
    started = LOG.split("--- asxbot arena mark")[0]
    assert "report step" in C.evening_finished(started, WED, slot)
    assert "no evening report" in C.evening_finished(LOG + "x\n", date(2026, 9, 24), slot)
    # A report run by hand at 14:00 is not tonight's evening routine.
    early = LOG.replace("2026-09-23 19:30", "2026-09-23 14:00") + "x\n"
    assert "no evening report" in C.evening_finished(early, WED, slot)


def test_tonight_means_the_last_session_before_the_next_morning():
    assert C.evening_day(EVENING) == WED
    assert C.evening_day(datetime(2026, 9, 24, 6, 30, tzinfo=SYD)) == WED  # before the warm-up
    assert C.evening_day(datetime(2026, 9, 24, 12, 0, tzinfo=SYD)) == date(2026, 9, 24)
    assert C.evening_day(datetime(2026, 9, 26, 12, 0, tzinfo=SYD)) == date(2026, 9, 25)  # Sat
    assert C.evening_day(datetime(2026, 9, 28, 3, 0, tzinfo=SYD)) == date(2026, 9, 25)  # Mon


def test_it_adds_to_cash_and_starting_cash_and_restates_the_marks_only(cfg, books):
    before = {n: json.loads(books.path(n).read_text(encoding="utf-8")) for n in books.names()}
    assert C.run(cfg, EVENING, 10_000.0, dry_run=False, checks=False) == 0

    for name, old in before.items():
        new = json.loads(books.path(name).read_text(encoding="utf-8"))
        assert new["cash"] == pytest.approx(old["cash"] + 10_000)
        assert new["starting_cash"] == pytest.approx(old["starting_cash"] + 10_000)
        assert C.only_these_changed(old, new, {"cash", "starting_cash"}) == []
        (mark,) = books.marks(name)
        assert mark.equity == pytest.approx(
            (10_244.11 if name.endswith("agent") else 10_000.0) + 10_000
        )
        assert mark.realised_day == 0.0 and mark.note == "prev equity 10,000.00"

    # Profit and loss is unchanged: equity less starting cash, before and after.
    agent = books.open("asx_announcements__agent", "asx_announcements", "agent", 1, 0.0)
    assert agent.cash - agent.starting_cash == pytest.approx(7_500.89 - 10_000.0)
    # The next day's loss limit starts from the restated mark, not a $10,000 one.
    assert books.day_start_equity(agent, date(2026, 9, 24)) == pytest.approx(20_244.11)

    backups = list((cfg.data_dir / "arena" / "backups").glob("add_capital_*/*"))
    assert len(backups) == 4  # both books and both marks files
    logged = (cfg.data_dir / "events" / "arena_accounts.jsonl").read_text(encoding="utf-8")
    assert logged.count('"capital_added"') == 2


def test_a_second_run_refuses_instead_of_adding_it_twice(cfg, books):
    C.run(cfg, EVENING, 10_000.0, dry_run=False, checks=False)
    with pytest.raises(C.Refused, match="Already applied"):
        C.run(cfg, EVENING, 10_000.0, dry_run=False, checks=False)


def test_it_refuses_before_the_evening_mark_and_a_dry_run_writes_nothing(cfg, books):
    before = {n: books.path(n).read_bytes() for n in books.names()}
    with pytest.raises(C.Refused, match="no mark for 2026-09-24"):
        C.run(cfg, datetime(2026, 9, 24, 21, 0, tzinfo=SYD), 10_000.0, False, checks=False)
    assert C.run(cfg, EVENING, 10_000.0, dry_run=True, checks=False) == 0
    assert {n: books.path(n).read_bytes() for n in books.names()} == before
    assert not (cfg.data_dir / "arena" / "backups").exists()


def test_a_running_arena_process_refuses_before_anything_is_read(cfg, books, monkeypatch):
    monkeypatch.setattr(C, "process_listing", lambda: [(5, 1, "asxbot.exe arena watch")])
    with pytest.raises(C.Refused, match="arena process is running"):
        C.run(cfg, EVENING, 10_000.0, dry_run=False)
    monkeypatch.setattr(C, "process_listing", lambda: [])
    monkeypatch.setattr(C, "task_status", lambda name: ["Running"])
    with pytest.raises(C.Refused, match="is running"):
        C.run(cfg, EVENING, 10_000.0, dry_run=False)
    monkeypatch.setattr(C, "task_status", lambda name: ["Ready"])
    with pytest.raises(C.Refused, match="no evening report"):  # no evening log at all
        C.run(cfg, EVENING, 10_000.0, dry_run=False)
