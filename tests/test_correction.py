"""The one-off re-pricing of ARN-000002 (TRACKER #24): what it changes, that it refuses when
it should, and that the $10,000 top-up still works after it. No network, no real books."""

import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from asxbot.arena import capital as C
from asxbot.arena import correction as K
from asxbot.arena.accounts import AccountStore
from asxbot.arena.broker import ArenaBroker
from asxbot.arena.minutes import MinuteBars
from asxbot.backtest.costs import CostModel
from asxbot.config import load_config
from asxbot.io import write_parquet_atomic

SYD = ZoneInfo("Australia/Sydney")
EVENING = datetime(2026, 9, 23, 21, 0, tzinfo=SYD)
AGENT, BOT = "asx_announcements__agent", "asx_announcements__bot"
# A1M's minute bars on 23 Sep, as the cache holds them.
ROWS = [
    (10, 29, 0.840, 0.840, 0.830, 0.830, 37_518),
    (10, 30, 0.825, 0.830, 0.825, 0.830, 39_780),
    (10, 37, 0.830, 0.835, 0.830, 0.835, 21_736),
    (10, 41, 0.835, 0.837, 0.835, 0.835, 36_051),
    (10, 42, 0.840, 0.840, 0.840, 0.840, 38_335),
    (15, 57, 0.917, 0.917, 0.917, 0.917, 1),
    (15, 58, 0.915, 0.915, 0.915, 0.915, 37),
]


@pytest.fixture
def cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )


@pytest.fixture
def broker(cfg, monkeypatch):
    # The cache is all there is: 23 Sep is "today" or recent, and would otherwise be refetched.
    monkeypatch.setattr(MinuteBars, "fetch", lambda self, code, day, force=False:
                        self.cached(code, day))  # fmt: skip
    b = ArenaBroker(cfg.data_dir, CostModel.from_config(cfg), MinuteBars(cfg.data_dir, "close"),
                    lambda t: 2_000_000.0)  # fmt: skip
    idx = [datetime(2026, 9, 23, h, m, tzinfo=SYD) for h, m, *_ in ROWS]
    df = pd.DataFrame(
        {k: [r[i] for r in ROWS] for i, k in enumerate(("open", "high", "low", "close",
                                                           "volume"), start=2)},
        index=pd.DatetimeIndex(idx, name="Datetime"),
    )  # fmt: skip
    write_parquet_atomic(df, b.minutes._path("A1M", datetime(2026, 9, 23).date()))
    return b


def _slip(broker):
    return broker.costs.slippage_pct(3000 * 0.84, 2_000_000.0)


def _books(cfg, broker, *, sold=True):
    """The books as the old code left them tonight: ARN-000002 stamped 10:29:46 and filled
    at the 10:29 bar, ARN-000003 the 15:57 target sale, and the evening's marks."""
    slip = _slip(broker)
    buy = 0.830 * (1 + slip)
    sell = 0.917 * (1 - broker.costs.slippage_pct(3000 * 0.917, 2_000_000.0))
    fee_b, fee_s = broker.costs.brokerage(3000 * buy), broker.costs.brokerage(3000 * sell)
    arn2 = {
        "order_id": "ARN-000002", "account": AGENT, "ticker": "A1M", "side": "buy", "qty": 3000,
        "limit": 0.84, "decision_at": "2026-09-23T10:29:46+10:00", "status": "filled",
        "filled_qty": 3000, "avg_price": round(buy, 4), "commission": round(fee_b, 2),
        "fill_minute": "2026-09-23T10:29+10:00",
        "fill_basis": f"close of the 2026-09-23 10:29 minute bar (+0 min after the decision) at "
                      f"0.8300, plus {slip * 100:.3f}% slippage",
        "stop": 0.755, "target": 0.9, "reason": "drift", "model": "claude-opus-5",
        "placed_by": "agent", "message": "filled", "realised": 0.0, "hold": "overnight",
    }  # fmt: skip
    arn3 = {
        "order_id": "ARN-000003", "account": AGENT, "ticker": "A1M", "side": "sell",
        "qty": 3000, "limit": round(sell, 4), "decision_at": "2026-09-23T15:57:00+10:00",
        "status": "filled", "filled_qty": 3000, "avg_price": round(sell, 4),
        "commission": round(fee_s, 2), "fill_minute": "2026-09-23T15:57+10:00",
        "fill_basis": "target 0.900 reached", "stop": None, "target": None,
        "reason": "TARGET reached at 0.900", "model": "code (target, not the agent)",
        "placed_by": "code", "message": "filled", "realised": round((sell - buy) * 3000, 2),
        "hold": "intraday",
    }  # fmt: skip
    cash = 10_000.0 - (3000 * buy + fee_b)
    raw = {
        "name": AGENT, "playbook": "asx_announcements", "kind": "agent", "level": 1,
        "starting_cash": 10_000.0, "cash": cash, "positions": {},
        "orders": {"ARN-000002": arn2}, "next_id": 1, "realised_pnl": 0.0,
        "fees_paid": fee_b, "borrow_paid": 0.0, "created": "2026-09-22T21:40:00+10:00",
    }  # fmt: skip
    if sold:
        raw["orders"]["ARN-000003"] = arn3
        raw["cash"] = cash + 3000 * sell - fee_s
        raw["realised_pnl"] = (sell - buy) * 3000
        raw["fees_paid"] = fee_b + fee_s
    else:
        raw["positions"]["A1M"] = {
            "ticker": "A1M", "qty": 3000, "avg_cost": buy, "opened_at": "2026-09-23T10:29+10:00",
            "stop": 0.755, "target": 0.9, "thesis": "drift", "opened_by": "agent",
            "model": "claude-opus-5", "borrow_accrued": 0.0, "last_borrow_day": "2026-09-23",
            "hold": "overnight", "hold_reason": "", "hold_asked_on": "",
        }  # fmt: skip
    store = AccountStore(cfg.data_dir)
    store.path(AGENT).write_text(json.dumps(raw, indent=2), encoding="utf-8")
    store.open(BOT, "asx_announcements", "bot", 1, 10_000.0)
    equity = raw["cash"] + (0 if sold else 3000 * 0.915)
    for name, eq, cash_, real, fees in (
        (AGENT, equity, raw["cash"], raw["realised_pnl"], raw["fees_paid"]),
        (BOT, 10_000.0, 10_000.0, 0.0, 0.0),
    ):
        store.marks_path(name).write_text(json.dumps({
            "day": "2026-09-23", "equity": round(eq, 2), "cash": round(cash_, 2),
            "gross_exposure": 0.0, "positions": 0, "realised_day": round(real, 2),
            "fees_day": round(fees, 2), "note": "prev equity 10,000.00",
        }) + "\n", encoding="utf-8")  # fmt: skip
    return store, raw


def _hashes(cfg):
    root = cfg.data_dir / "arena"
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in list((root / "accounts").glob("*.json")) + list((root / "marks").glob("*"))
    }


def test_a_dry_run_shows_the_change_and_writes_nothing(cfg, broker, capsys):
    _books(cfg, broker)
    before = _hashes(cfg)
    assert K.run(cfg, EVENING, dry_run=True, checks=False, broker=broker) == 0
    out = capsys.readouterr().out
    assert "10:29 close 0.8300" in out and "10:41 close 0.8350" in out
    assert _hashes(cfg) == before
    events = (cfg.data_dir / "events" / "arena_accounts.jsonl").read_text(encoding="utf-8")
    assert "fill_corrected" not in events


def test_the_fill_is_re_priced_at_the_first_bar_after_10_37_41(cfg, broker):
    store, raw = _books(cfg, broker)
    assert K.run(cfg, EVENING, dry_run=False, checks=False, broker=broker) == 0

    slip = _slip(broker)
    old, new = 0.830 * (1 + slip), 0.835 * (1 + slip)
    acct = store.open(AGENT, "asx_announcements", "agent", 1, 0.0)
    o = acct.orders["ARN-000002"]
    assert o.decided_at == "2026-09-23T10:37:41+10:00"
    assert o.data_as_of == "2026-09-23T10:36:02+10:00"
    assert o.fill_minute == "2026-09-23T10:41+10:00"
    assert o.avg_price == round(new, 4)
    fee = broker.costs.brokerage(3000 * new)
    assert o.commission == round(fee, 2) == 6.60  # the minimum both times
    assert acct.cash == pytest.approx(raw["cash"] - 3000 * (new - old))
    assert acct.realised_pnl == pytest.approx(raw["realised_pnl"] - 3000 * (new - old))
    assert acct.orders["ARN-000003"].realised == round(
        raw["orders"]["ARN-000003"]["realised"] - 3000 * (new - old), 2
    )
    assert acct.starting_cash == 10_000.0  # untouched: the top-up checks it
    (mark,) = store.marks(AGENT)
    assert mark.cash == pytest.approx(acct.cash, abs=0.011)  # restated by the same delta
    assert mark.equity == pytest.approx(acct.cash, abs=0.011)  # nothing held: equity is cash
    assert mark.realised_day == pytest.approx(round(acct.realised_pnl, 2), abs=0.011)
    (ev,) = [r for r in json.loads("[" + ",".join(
        (cfg.data_dir / "events" / "arena_accounts.jsonl").read_text().splitlines()) + "]")
        if r["event"] == "fill_corrected"]  # fmt: skip
    assert ev["order_id"] == "ARN-000002" and ev["after"]["fill_minute"].endswith("10:41+10:00")
    assert (cfg.data_dir / "arena" / "backups").exists()


def test_a_second_run_refuses_and_writes_nothing(cfg, broker):
    _books(cfg, broker)
    K.run(cfg, EVENING, dry_run=False, checks=False, broker=broker)
    before = _hashes(cfg)
    with pytest.raises(C.Refused, match="already applied"):
        K.run(cfg, EVENING, dry_run=False, checks=False, broker=broker)
    assert _hashes(cfg) == before


def test_it_refuses_while_the_watcher_runs(cfg, broker, monkeypatch, capsys):
    _books(cfg, broker)
    before = _hashes(cfg)
    watcher = [(11, 1, r'"C:\venvs\asx-bot\Scripts\asxbot.exe" arena watch --until auto')]
    monkeypatch.setattr(K, "process_listing", lambda: watcher)
    monkeypatch.setattr(K, "task_status", lambda name: ["Ready"])
    with pytest.raises(C.Refused, match="arena process is running"):
        K.run(cfg, EVENING, dry_run=False, broker=broker)
    assert K.run(cfg, EVENING, dry_run=True, broker=broker) == 2  # shows it, says it would refuse
    assert "would REFUSE" in capsys.readouterr().out
    monkeypatch.setattr(K, "process_listing", lambda: [])
    monkeypatch.setattr(K, "task_status", lambda name: ["Running"])
    with pytest.raises(C.Refused, match="is running"):
        K.run(cfg, EVENING, dry_run=False, broker=broker)
    assert _hashes(cfg) == before


def test_an_open_position_has_its_average_cost_restated(cfg, broker):
    store, raw = _books(cfg, broker, sold=False)
    K.run(cfg, EVENING, dry_run=False, checks=False, broker=broker)
    pos = store.open(AGENT, "asx_announcements", "agent", 1, 0.0).positions["A1M"]
    assert pos.avg_cost == pytest.approx(0.835 * (1 + _slip(broker)))
    assert pos.opened_at == "2026-09-23T10:41+10:00"  # stops and targets check from 10:42


def test_the_top_up_still_works_after_the_correction(cfg, broker):
    store, _ = _books(cfg, broker)
    K.run(cfg, EVENING, dry_run=False, checks=False, broker=broker)
    corrected = store.open(AGENT, "asx_announcements", "agent", 1, 0.0)
    (mark,) = store.marks(AGENT)

    assert C.run(cfg, EVENING, C.AMOUNT, dry_run=False, checks=False) == 0
    after = store.open(AGENT, "asx_announcements", "agent", 1, 0.0)
    assert after.starting_cash == 20_000.0
    assert after.cash == pytest.approx(corrected.cash + 10_000.0)
    assert after.orders["ARN-000002"].decided_at == "2026-09-23T10:37:41+10:00"
    (mark2,) = store.marks(AGENT)
    assert mark2.equity == pytest.approx(mark.equity + 10_000.0)
