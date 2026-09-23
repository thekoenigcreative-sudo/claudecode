"""The one-off re-pricing of ARN-000003 under the volume-aware fill rule (TRACKER #25): what
it changes, that it refuses when it should. No network, no real books."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from asxbot.arena import capital as C
from asxbot.arena import correction as K2
from asxbot.arena import correction_arn000003 as K3
from asxbot.arena.accounts import AccountStore
from asxbot.arena.broker import ArenaBroker
from asxbot.arena.minutes import MinuteBars
from asxbot.arena.selfcheck import check_fills_after_orders
from asxbot.backtest.costs import CostModel
from asxbot.config import load_config
from asxbot.io import write_parquet_atomic
from test_correction import AGENT, EVENING, _books, _hashes

SYD = ZoneInfo("Australia/Sydney")
LATER = datetime(2026, 9, 24, 6, 0, tzinfo=SYD)
# A1M's minute bars on 23 Sep as the cache holds them, from the old fill to the close.
ROWS = [
    (10, 29, 0.840, 0.840, 0.830, 0.830, 37_518),
    (10, 30, 0.825, 0.830, 0.825, 0.830, 39_780),
    (10, 37, 0.830, 0.835, 0.830, 0.835, 21_736),
    (10, 41, 0.835, 0.837, 0.835, 0.835, 36_051),
    (10, 42, 0.840, 0.840, 0.840, 0.840, 38_335),
    (15, 45, 0.920, 0.920, 0.920, 0.920, 1_520_098),  # past the target before it was honoured
    (15, 55, 0.917, 0.917, 0.917, 0.917, 745),
    (15, 57, 0.917, 0.917, 0.917, 0.917, 1),
    (15, 58, 0.915, 0.915, 0.915, 0.915, 37),
    (15, 59, 0.915, 0.917, 0.915, 0.915, 2228),
    (16, 0, 0.915, 0.915, 0.915, 0.915, 7162),
    (16, 10, 0.920, 0.920, 0.920, 0.920, 513_634),
]


@pytest.fixture
def cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )


def _write(broker, rows):
    idx = [datetime(2026, 9, 23, h, m, tzinfo=SYD) for h, m, *_ in rows]
    df = pd.DataFrame(
        {k: [float(r[i]) for r in rows] for i, k in enumerate(("open", "high", "low", "close",
                                                                  "volume"), start=2)},
        index=pd.DatetimeIndex(idx, name="Datetime"),
    )  # fmt: skip
    write_parquet_atomic(df, broker.minutes._path("A1M", datetime(2026, 9, 23).date()))


@pytest.fixture
def broker(cfg, monkeypatch):
    monkeypatch.setattr(MinuteBars, "fetch", lambda self, code, day, force=False:
                        self.cached(code, day))  # fmt: skip
    b = ArenaBroker(cfg.data_dir, CostModel.from_config(cfg), MinuteBars(cfg.data_dir, "close"),
                    lambda t: 2_000_000.0)  # fmt: skip
    _write(b, ROWS)
    return b


@pytest.fixture
def books(cfg, broker):
    """Tonight's books: ARN-000002 corrected, then the $10,000 top-up."""
    store, _ = _books(cfg, broker)
    assert K2.run(cfg, EVENING, dry_run=False, checks=False, broker=broker) == 0
    assert C.run(cfg, EVENING, C.AMOUNT, dry_run=False, checks=False) == 0
    return store


def _events(cfg):
    path = cfg.data_dir / "events" / "arena_accounts.jsonl"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln]


def test_a_dry_run_shows_the_change_and_writes_nothing(cfg, broker, books, capsys):
    before = _hashes(cfg)
    assert K3.run(cfg, LATER, dry_run=True, checks=False, broker=broker) == 0
    out = capsys.readouterr().out
    assert "15:57 (volume 1): 3,000 @ 0.917" in out
    assert "15:58 7 of 37 @ 0.915" in out and "16:10 1,116 of 513,634 @ 0.920" in out
    assert _hashes(cfg) == before
    assert not [e for e in _events(cfg) if e.get("order_id") == "ARN-000003"]


def test_the_exit_is_re_priced_across_the_bars_that_had_the_volume(cfg, broker, books):
    before = books.open(AGENT, "asx_announcements", "agent", 1, 0.0)
    (mark_before,) = books.marks(AGENT)
    assert K3.run(cfg, LATER, dry_run=False, checks=False, broker=broker) == 0
    acct = books.open(AGENT, "asx_announcements", "agent", 1, 0.0)
    o = acct.orders["ARN-000003"]
    assert [(f["minute"][11:16], f["qty"]) for f in o.fills] == [
        ("15:58", 7),
        ("15:59", 445),
        ("16:00", 1432),
        ("16:10", 1116),
    ]
    slip = broker.costs.slippage_pct(3000 * 0.917, 2_000_000.0)
    old_px = 0.917 * (1 - slip)
    new_value = (1884 * 0.915 + 1116 * 0.920) * (1 - slip)
    assert o.avg_price == round(new_value / 3000, 4)
    assert o.fill_minute == "2026-09-23T15:58+10:00" and o.order_type == "target"
    assert o.decided_at == "2026-09-23T16:19:45+10:00"
    assert o.data_as_of.startswith("2026-09-23T15:57") and o.rests_from.endswith("15:56+10:00")
    assert "reached in the 2026-09-23 15:57 minute bar, first filled in the 15:58" in o.fill_basis
    assert o.commission == 6.60
    d = new_value - 3000 * old_px
    assert acct.cash == pytest.approx(before.cash + d)
    assert acct.realised_pnl == pytest.approx(before.realised_pnl + d)
    assert acct.fees_paid == pytest.approx(before.fees_paid)
    assert acct.starting_cash == before.starting_cash == 20_000.0
    assert acct.orders["ARN-000002"] == before.orders["ARN-000002"]
    (mark,) = books.marks(AGENT)
    assert mark.cash == pytest.approx(mark_before.cash + d, abs=0.011)
    assert mark.realised_day == pytest.approx(mark_before.realised_day + d, abs=0.011)
    (ev,) = [e for e in _events(cfg) if e.get("order_id") == "ARN-000003"]
    assert ev["event"] == "fill_corrected" and ev["after"]["fill_minute"].endswith("15:58+10:00")

    class _Arena:  # the self-check reads it as it reads the live books
        def account(self, pb, kind):
            if kind == "agent":
                return acct
            return books.open("asx_announcements__bot", "asx_announcements", "bot", 1, 0.0)

    assert check_fills_after_orders(_Arena(), None).ok


def test_a_second_run_refuses_and_writes_nothing(cfg, broker, books):
    K3.run(cfg, LATER, dry_run=False, checks=False, broker=broker)
    before = _hashes(cfg)
    with pytest.raises(C.Refused, match="already applied"):
        K3.run(cfg, LATER, dry_run=False, checks=False, broker=broker)
    assert _hashes(cfg) == before


def test_it_refuses_before_arn000002_is_corrected(cfg, broker):
    _books(cfg, broker)
    before = _hashes(cfg)
    with pytest.raises(C.Refused, match="correct_fill_arn000002"):
        K3.run(cfg, LATER, dry_run=False, checks=False, broker=broker)
    assert _hashes(cfg) == before


def test_it_refuses_without_the_whole_day_in_the_cache(cfg, broker, books):
    _write(broker, ROWS[:-1])  # no closing auction
    before = _hashes(cfg)
    with pytest.raises(C.Refused, match="missing or incomplete"):
        K3.run(cfg, LATER, dry_run=False, checks=False, broker=broker)
    assert _hashes(cfg) == before


def test_it_refuses_while_the_watcher_runs(cfg, broker, books, monkeypatch, capsys):
    before = _hashes(cfg)
    watcher = [(11, 1, r'"C:\venvs\asx-bot\Scripts\asxbot.exe" arena watch --until auto')]
    monkeypatch.setattr(K3, "process_listing", lambda: watcher)
    monkeypatch.setattr(K3, "task_status", lambda name: ["Ready"])
    with pytest.raises(C.Refused, match="arena process is running"):
        K3.run(cfg, LATER, dry_run=False, broker=broker)
    assert K3.run(cfg, LATER, dry_run=True, broker=broker) == 2
    assert "would REFUSE" in capsys.readouterr().out
    monkeypatch.setattr(K3, "process_listing", lambda: [])
    monkeypatch.setattr(K3, "task_status", lambda name: ["Running"])
    with pytest.raises(C.Refused, match="is running"):
        K3.run(cfg, LATER, dry_run=False, broker=broker)
    assert _hashes(cfg) == before


def test_a_replay_that_stops_out_is_refused(cfg, broker, books):
    rows = [*ROWS[:5], (12, 0, 0.80, 0.80, 0.70, 0.72, 1_000_000), *ROWS[5:]]
    _write(broker, rows)
    with pytest.raises(C.Refused, match="exactly one target exit"):
        K3.run(cfg, LATER, dry_run=True, checks=False, broker=broker)


def test_the_books_load_in_the_store(cfg, broker, books):
    K3.run(cfg, LATER, dry_run=False, checks=False, broker=broker)
    acct = AccountStore(cfg.data_dir).open(AGENT, "asx_announcements", "agent", 1, 0.0)
    assert acct.orders["ARN-000003"].filled_qty == 3000
