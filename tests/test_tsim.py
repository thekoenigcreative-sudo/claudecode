"""The proper trading simulator (src/asxbot/lab/tsim): no future data, realistic fills, honest
costs, continuity, the AI's thinking time on the clock."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

from asxbot.lab.tsim import llm
from asxbot.lab.tsim.alerts import AlertBook
from asxbot.lab.tsim.anon import NoAnon, RunAnon
from asxbot.lab.tsim.broker import Account, OrderRejected, SimBroker
from asxbot.lab.tsim.costs import CostModel, Fees, tick_size
from asxbot.lab.tsim.engine import Decision, Trader, run_day
from asxbot.lab.tsim.journal import Journal
from asxbot.lab.tsim.market import (
    CLOSE_AUCTION_SLOT,
    SLOTS,
    SYD,
    History,
    Market,
    slot_time,
    visible_slots,
)
from asxbot.lab.tsim.summaries import Summaries
from asxbot.lab.tsim.tools import TraderView

D1, D2 = date(2026, 7, 14), date(2026, 7, 15)


def write_day(root, code, day, bars):
    """bars: {slot: (o, h, l, c, v)}"""
    idx = [slot_time(day, s) for s in sorted(bars)]
    df = pd.DataFrame([bars[s] for s in sorted(bars)], index=pd.DatetimeIndex(idx),
                      columns=["open", "high", "low", "close", "volume"])  # fmt: skip
    p = root / code / f"{day.isoformat()}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p)


def flat_day(price=10.0, vol=10_000, moves=None):
    bars = {s: (price, price, price, price, vol) for s in range(0, 361)}
    bars[CLOSE_AUCTION_SLOT] = (price, price, price, price, vol * 5)
    for s, b in (moves or {}).items():
        bars[s] = b
    return bars


@pytest.fixture
def mkt(tmp_path):
    root = tmp_path / "hist"
    for d in (date(2026, 7, 13), D1):
        write_day(root, "AAA", d, flat_day(10.0))
        write_day(root, "BBB", d, flat_day(1.0, 100_000))
        write_day(root, "^AXJO", d, flat_day(8000.0, 1))
    summ = Summaries(tmp_path / "summ")
    summ.build(History(root), ["AAA", "BBB", "^AXJO"], workers=1)
    return root, summ


def broker_for(m, shortable=(), cash=100_000.0, lev=1.0):
    b = SimBroker(Account("t", cash, cash, lev), CostModel(), 0.20, set(shortable))
    b.market = m
    return b


def work_until(b, last_slot):
    fills = []
    for s in range(0, last_slot + 1):
        b.market.now = slot_time(b.market.day, s + 1) + timedelta(seconds=20)
        fills += b.work_slot(s)
    return fills


def test_no_bar_is_visible_before_it_completes(mkt, tmp_path):
    root, summ = mkt
    write_day(root, "AAA", D1, flat_day(10.0, moves={60: (11, 11, 11, 11, 5000)}))
    m = Market(D1, History(root), ["AAA"], None, summ)
    m.now = slot_time(D1, 61)  # 11:00:00: the 10:59 bar ended but is not delivered yet
    assert visible_slots(D1, m.now) == 60 and m.last("AAA") == 10.0
    m.now = slot_time(D1, 61) + timedelta(seconds=20)
    assert m.last("AAA") == 11.0
    m.now = datetime.combine(D1, time(9, 0), tzinfo=SYD)
    assert m.last("AAA") == 10.0  # before the open: yesterday's close, from the summaries


def test_market_order_fills_after_it_arrives_and_pays_spread(mkt):
    root, summ = mkt
    write_day(root, "AAA", D1, flat_day(10.0, moves={31: (10.2, 10.3, 10.1, 10.25, 10_000)}))
    m = Market(D1, History(root), ["AAA"], None, summ)
    b = broker_for(m)
    at = slot_time(D1, 30) + timedelta(seconds=40)  # during the 10:29 bar
    m.now = at
    o = b.place("AAA", "buy", 100, "market", at=at)
    fills = work_until(b, 31)
    assert len(fills) == 1 and fills[0].at.startswith(slot_time(D1, 31).isoformat()[:16])
    assert fills[0].price >= 10.2 + tick_size(10.2) - 1e-9  # the bar's open plus >= a tick
    assert o.commission == pytest.approx(6.60)


def test_resting_limit_needs_a_trade_through_and_fills_at_its_limit(mkt):
    root, summ = mkt
    moves = {20: (10.0, 10.0, 9.95, 9.95, 10_000), 40: (9.95, 9.95, 9.90, 9.92, 10_000)}
    write_day(root, "AAA", D1, flat_day(10.0, moves=moves))
    m = Market(D1, History(root), ["AAA"], None, summ)
    b = broker_for(m)
    m.now = slot_time(D1, 5)
    o = b.place("AAA", "buy", 100, "limit", at=m.now, limit=9.95)
    work_until(b, 30)
    assert o.filled == 0  # touched 9.95 at 10:19, never traded below it
    work_until(b, 45)
    assert o.filled == 100 and o.avg_price == pytest.approx(9.95)


def test_no_bar_fills_more_than_its_volume_share(mkt):
    root, summ = mkt
    m = Market(D1, History(root), ["AAA"], None, summ)
    b = broker_for(m)
    m.now = slot_time(D1, 10)
    o = b.place("AAA", "buy", 5_000, "market", at=m.now)
    work_until(b, 11)  # two bars of 10,000 shares: at most 2,000 each
    assert o.filled == 2_000 + 2_000 or o.filled == 2_000
    assert all(f["qty"] <= 2_000 for f in o.fills)


def test_stop_through_a_gap_fills_at_the_open_not_the_stop(mkt):
    root, summ = mkt
    write_day(root, "AAA", D1, flat_day(10.0, moves={50: (9.0, 9.1, 8.9, 9.0, 10_000)}))
    m = Market(D1, History(root), ["AAA"], None, summ)
    b = broker_for(m)
    m.now = slot_time(D1, 5)
    b.place("AAA", "buy", 100, "market", at=m.now)
    work_until(b, 10)
    s = b.place("AAA", "sell", 100, "stop", at=m.now, stop=9.8, reduce_only=True)
    work_until(b, 55)
    assert s.filled == 100 and s.avg_price < 9.0  # the open (9.0) less the spread, not 9.8


def test_shorts_only_where_borrowable(mkt):
    root, summ = mkt
    m = Market(D1, History(root), ["AAA", "BBB"], None, summ)
    b = broker_for(m, shortable={"AAA"})
    m.now = slot_time(D1, 5)
    with pytest.raises(OrderRejected, match="borrowable"):
        b.place("BBB", "short", 100, "market", at=m.now)
    b.place("AAA", "short", 100, "market", at=m.now)


def test_buying_power_is_the_account_not_a_style_rule(mkt):
    root, summ = mkt
    m = Market(D1, History(root), ["AAA"], None, summ)
    b = broker_for(m, cash=20_000.0)
    m.now = slot_time(D1, 5)
    with pytest.raises(OrderRejected, match="buying power"):
        b.place("AAA", "buy", 3_000, "market", at=m.now)  # $30,000 on $20,000 at 1x


def test_minimum_commission_once_per_order_across_partial_fills(mkt):
    root, summ = mkt
    m = Market(D1, History(root), ["AAA"], None, summ)
    b = broker_for(m)
    m.now = slot_time(D1, 5)
    o = b.place("AAA", "buy", 3_000, "market", at=m.now)
    work_until(b, 8)
    assert len(o.fills) >= 2 and o.commission == pytest.approx(max(6.60, o.value * 0.00088))


def test_opening_and_closing_auctions(mkt):
    root, summ = mkt
    bars = flat_day(10.0, moves={0: (10.5, 10.5, 10.5, 10.5, 50_000),
                                 CLOSE_AUCTION_SLOT: (10.8, 10.8, 10.8, 10.8, 60_000)})  # fmt: skip
    write_day(root, "AAA", D1, bars)
    m = Market(D1, History(root), ["AAA"], None, summ)
    b = broker_for(m)
    m.now = datetime.combine(D1, time(9, 40), tzinfo=SYD)
    o = b.place("AAA", "buy", 1_000, "market", at=m.now)
    work_until(b, 0)
    assert o.filled == 1_000 and "opening auction" in o.fills[0]["basis"]
    assert o.avg_price == pytest.approx(10.5, rel=0.002)  # no spread in the auction
    m.now = slot_time(D1, 300)
    c = b.place("AAA", "sell", 1_000, "moc", at=m.now)
    work_until(b, SLOTS - 1)
    assert c.filled == 1_000 and "closing auction" in c.fills[0]["basis"]
    assert c.avg_price == pytest.approx(10.8, rel=0.002)


def test_bracket_exits_follow_the_entry_and_cancel_each_other(mkt):
    root, summ = mkt
    write_day(root, "AAA", D1, flat_day(10.0, moves={60: (10.0, 10.6, 10.0, 10.5, 10_000)}))
    m = Market(D1, History(root), ["AAA"], None, summ)
    b = broker_for(m)
    m.now = slot_time(D1, 5)
    e = b.place("AAA", "buy", 1_000, "market", at=m.now, attach={"stop": 9.5, "target": 10.4})
    work_until(b, 70)
    kids = [b.acct.orders[c] for c in e.children]
    tgt = next(k for k in kids if k.type == "limit")
    stp = next(k for k in kids if k.type == "stop")
    assert tgt.filled == 1_000 and stp.status.startswith("cancel")
    assert "AAA" not in b.acct.positions and len(b.acct.trades) == 1


def test_disguise_keeps_dollar_values_and_round_trips(mkt):
    a = RunAnon("salt", [D1, D2])
    al = a.alias("AAA")
    assert al != "AAA" and a.real(al) == "AAA"
    f = a.factor("AAA")
    assert 0.5 <= f <= 2.0
    shown_px, shown_qty = a.price("AAA", 10.0), 1_000
    real_qty = a.shares_to_real("AAA", shown_qty)
    assert real_qty * 10.0 == pytest.approx(shown_qty * shown_px, rel=0.01)
    assert "[name]" in a.headline("Acme Lithium signs offtake with Globex") and "Acme" not in (
        a.headline("Acme Lithium signs offtake with Globex"))  # fmt: skip
    assert NoAnon().alias("aaa") == "AAA"


def test_journal_lessons_only_from_earlier_days(tmp_path):
    j = Journal(tmp_path)
    j.write("2026-07-14", {"journal": "x", "lessons": ["one"]})
    j.write("2026-07-15", {"journal": "y", "lessons": ["two"]})
    assert j.lessons("2026-07-15") == ["one"]


class Slow(Trader):
    """Thinks 95 seconds at 10:30 and buys."""

    kind = "ai"

    def pre_open(self, v, ctx):
        return Decision(alerts=[{"type": "time", "at": "10:30"}], latency_s=5)

    def wake(self, v, reasons, ctx):
        if any(r.get("type") == "time" for r in reasons):
            return Decision(actions=[{"op": "place", "code": "AAA", "side": "buy", "qty": 100,
                                      "type": "market"}], latency_s=95, calls=1)  # fmt: skip
        return None


def test_thinking_time_moves_the_clock(mkt):
    root, summ = mkt
    write_day(root, "AAA", D1, flat_day(10.0))
    m = Market(D1, History(root), ["AAA"], None, summ)
    b = broker_for(m)
    book = AlertBook()
    v = TraderView(m, b, NoAnon())
    r = run_day(m, b, Slow(), book, v)
    (o,) = b.acct.orders.values()
    sub = datetime.fromisoformat(o.submitted_at)
    assert sub >= datetime.combine(D1, time(10, 31, 55), tzinfo=SYD)  # woken 10:31:20 + 95 s
    assert datetime.fromisoformat(o.fills[0]["at"]) > sub and r.calls == 1


def test_rules_orb_runs_a_synthetic_span_with_continuity(tmp_path, monkeypatch):
    from asxbot.arena.replay_ibkr import sessions
    from asxbot.lab.tsim import run as R
    from asxbot.lab.tsim.synthetic import make_history

    monkeypatch.setenv("ASXBOT_LAB_LOCAL", str(tmp_path / "lab"))
    days = sessions(date(2026, 7, 1), date(2026, 8, 5))
    codes = [f"{a}{b}X" for a in "ABS" for b in "ABCD"]
    ann = make_history(tmp_path / "h", days, codes)
    inp = R.prepare_inputs(None, days, history=tmp_path / "h", workers=1, ann=ann)
    inp.shortable = codes[:4]
    out = R.run("t_drift", {"kind": "rules", "family": "drift"}, days[15:22], inp, resume=False)
    marks = out["account"]["marks"]
    assert len(marks) == 7
    # drift holds for days: some day ends with an open position carried to the next
    assert any(m["positions"] for m in marks[:-1])
    from asxbot.lab.tsim.report import score

    s = score(out)
    assert s["days"] == 7 and "ibkr" in s["net_by_broker"]


def test_every_broker_schedule_scores_the_same_orders():
    c = CostModel(fees={"ibkr": Fees("ibkr", 0.088, 6.6), "cheap": Fees("cheap", 0.03, 3.0)})
    assert c.fees["cheap"].commission(2_000) == 3.0 and c.fees["ibkr"].commission(2_000) == 6.6


def test_parse_json_takes_the_last_object():
    assert llm.parse_json('thinking {"a": 1} then {"orders": []}') == {"orders": []}
    assert llm.parse_json("no json") is None


def test_the_simulator_never_touches_a_real_broker():
    """Nothing in tsim imports the real-money path or IBKR."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "asxbot" / "lab" / "tsim"
    for p in src.rglob("*.py"):
        t = p.read_text(encoding="utf-8")
        assert "place_order" not in t and "asxbot.ibkr" not in t and "ib_async" not in t, p


_ = np
