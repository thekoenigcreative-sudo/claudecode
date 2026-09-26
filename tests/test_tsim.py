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


# --------------------------------------------------------------------------- the search
class _Cfg:
    def __init__(self, root):
        self.root = root
        self.data_dir = root / "data"
        self.raw = {}

    def get(self, key, default=None):
        return default


def test_search_counts_every_idea_and_refuses_repeats(tmp_path):
    from asxbot.lab.tsim import search

    cfg = _Cfg(tmp_path)
    a = search.register(cfg, {"kind": "rules", "family": "orb", "params": {"window": 10}}, "r", "t")
    assert a["n"] == 1 and a["stage"] == "queued"
    assert search.register(cfg, {"kind": "rules", "family": "orb", "params": {"window": 10}},
                           "again", "t") is None  # fmt: skip
    assert search.register(cfg, {"kind": "rules", "family": "nope"}, "r", "t") is None
    assert search.register(cfg, {"kind": "rules", "family": "orb", "params": {"leverage": 5}},
                           "not a parameter", "t") is None  # fmt: skip
    assert search.register(cfg, {"kind": "ai", "addendum": "x" * 601}, "too long", "t") is None
    b = search.register(cfg, {"kind": "new_family", "describe": "pairs"}, "r", "t")
    assert b["stage"] == "needs_build" and search.n_tried(cfg) == 1


def test_the_bar_rises_with_the_count_and_samples_rotate():
    from asxbot.lab.tsim import search

    assert search.check_bar(100) > search.check_bar(10) >= 1.0
    assert search.practice_bar(100) > search.practice_bar(10)
    days = [date(2026, 4, 1) + timedelta(days=i) for i in range(60)]
    assert search.sample(days, 1) != search.sample(days, 2)
    assert search.sample(days, 3) == sorted(search.sample(days, 3))


def test_gate_demands_profit_trades_t_and_beating_yardsticks_and_old_rules():
    from asxbot.lab.tsim import search

    good = {"net": 900.0, "trades": 30, "t_stat": 3.0, "old_rules": -500.0, "worst_day": -100}
    assert search.gate("check", good, 10, best_yard=100.0)[0]
    ok, why = search.gate("check", {**good, "net": 50.0}, 10, best_yard=100.0)
    assert not ok and "yardstick" in why
    ok, why = search.gate("check", {**good, "old_rules": 1000.0}, 10, best_yard=0.0)
    assert not ok and "old rules" in why


def test_the_sealed_block_is_run_once_per_finalist_and_rotates_when_worn(tmp_path, monkeypatch):
    from asxbot.lab.tsim import splits as S

    cfg = _Cfg(tmp_path)
    with pytest.raises(S.Sealed):
        S.days(cfg, "sealed")
    assert S.use_seal(cfg, "I0001") == 1
    with pytest.raises(S.Sealed):
        S.use_seal(cfg, "I0001")
    S.use_seal(cfg, "I0002")
    assert not S.rotate_if_worn(cfg, newest=date(2026, 12, 1))  # only 2 uses
    S.use_seal(cfg, "I0003")
    assert not S.rotate_if_worn(cfg, newest=date(2026, 10, 5))  # not 30 new days yet
    assert S.rotate_if_worn(cfg, newest=date(2026, 12, 1))
    d = S.current(cfg)
    assert d["sealed"][0] > "2026-09-25" and ["2026-08-17", "2026-09-25"] in d["check"]
    assert d["uses"] == []


def test_only_the_ai_is_disguised_and_rules_orders_are_not_refused(tmp_path, monkeypatch):
    """26 Sep: a rules yardstick on pre-cutoff days was disguised and every order refused as an
    unknown code - 66 days of zero trades that looked like 'no setups'."""
    from asxbot.arena.replay_ibkr import sessions
    from asxbot.lab.tsim import run as R
    from asxbot.lab.tsim.report import score
    from asxbot.lab.tsim.synthetic import make_history

    monkeypatch.setenv("ASXBOT_LAB_LOCAL", str(tmp_path / "lab"))
    days = sessions(date(2026, 4, 1), date(2026, 5, 15))  # before the knowledge cutoff
    codes = [f"{a}{b}X" for a in "ABS" for b in "ABCD"]
    ann = make_history(tmp_path / "h", days, codes)
    inp = R.prepare_inputs(None, days, history=tmp_path / "h", workers=1, ann=ann)
    out = R.run("t_orb_pre", {"kind": "rules", "family": "orb"}, days[15:], inp, resume=False)
    assert out["spec"]["disguised"] is False
    s = score(out)
    refused = [r["error"] for d in out["days"] for r in d["rejected"]]
    assert not [e for e in refused if "unknown code" in e] and s["n_trades_all"] > 0
    assert not refused  # entries sized in one call leave room for each other


def test_the_index_is_live_without_volume(mkt):
    """IBKR's index bars carry no volume; the index must still move (regime filters, the AI's
    'index today', the up/down-day split all read it)."""
    root, summ = mkt
    bars = {s: (8000 + s, 8000 + s, 8000 + s, 8000 + s, 0) for s in range(373)}
    write_day(root, "^AXJO", D1, bars)
    s2 = Summaries(root.parent / "summ2")
    s2.build(History(root), ["^AXJO"], workers=1)
    m = Market(D1, History(root), ["AAA"], None, s2)
    m.now = slot_time(D1, 100) + timedelta(seconds=30)
    assert m.index is not None and m.index_move_pct() is not None


def test_the_team_runs_its_stages_in_parallel_and_the_risk_manager_vetoes(mkt, tmp_path):
    """Readers, scanner, specialists (in parallel), decider, risk manager with a veto."""
    import threading

    from asxbot.lab.tsim.team import TeamTrader

    root, summ = mkt
    m = Market(D1, History(root), ["AAA", "BBB"], None, summ)
    b = broker_for(m)
    seen = []
    lock = threading.Lock()

    def ask(prompt, *, system, model, effort, cfg=None, **kw):
        line = system[system.index("ROLE:"):].split("\n")[0]
        role = next(r for r in ("announcement reader", "market scanner", "strategy specialist",
                                "DECISION-MAKER", "RISK MANAGER", "RESEARCHER") if r in line)
        role = {"market scanner": "scanner", "announcement reader": "reader"}.get(role, role)
        with lock:
            seen.append((role, model))
        text = {
            "scanner": '{"watchlist": [{"code": "AAA", "why": "x"}, {"code": "BBB", "why": "y"}]}',
            "strategy specialist": '{"proposals": [{"code": "AAA", "side": "buy"}]}',
            "DECISION-MAKER": '{"orders": [{"op": "place", "code": "AAA", "side": "buy", '
                              '"qty": 100, "type": "market"}, {"op": "place", "code": "BBB", '
                              '"side": "buy", "qty": 100, "type": "market"}], "note": "go"}',
            "RISK MANAGER": '{"verdicts": [{"index": 0, "approve": true}, {"index": 1, '
                            '"approve": false, "why": "too thin"}]}',
            "RESEARCHER": '{"lessons": ["l"], "ideas": []}',
        }.get(role, "{}")  # fmt: skip
        return {"text": text, "seconds": 5.0, "usage": {"output": 10}}

    t = TeamTrader(journal=Journal(tmp_path / "run"), ask=ask, register_ideas=False)
    book = AlertBook()
    v = TraderView(m, b, NoAnon())
    t.bind_alerts(book, v.anon)
    r = run_day(m, b, t, book, v)
    roles = {x[0] for x in seen}
    assert {"scanner", "strategy specialist", "DECISION-MAKER", "RISK MANAGER",
            "RESEARCHER"} <= roles  # fmt: skip
    assert sum(1 for x in seen if x[0] == "strategy specialist") % 5 == 0  # five at once
    top = ("DECISION-MAKER", "RISK MANAGER")
    assert all(mo.startswith("claude-opus") for ro, mo in seen if ro in top)
    codes = {o.code for o in b.acct.orders.values()}
    assert codes == {"AAA"}  # BBB was vetoed
    assert r.calls >= 8 and "opus:output" in r.usage and "sonnet:output" in r.usage
