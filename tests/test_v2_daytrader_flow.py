"""The v2 and day-trader paths end to end, with stand-in agents: an announcement arriving
before the open, the reaction look, the v2 rule bot, a day-trader scan with the agent
confirming, and the flat sweep. No model is called and nothing touches the network."""

from dataclasses import dataclass
from datetime import datetime

import pandas as pd
import pytest

from asxbot.announcements.model import Announcement
from asxbot.arena import daytrader as DT
from asxbot.arena import v2_flow
from asxbot.arena import watch as W
from asxbot.arena.broker import ArenaBroker
from asxbot.arena.intraday import MarketView, ReplayFeed
from asxbot.arena.levels import load_playbook
from asxbot.arena.minutes import MinuteBars
from asxbot.arena.reaction_v2 import enqueue, load_bot_state, load_queue
from asxbot.arena.runtime import Arena
from asxbot.backtest.costs import CostModel
from asxbot.config import load_config
from asxbot.live.quotes import Quote, StaticQuotes
from test_v2_daytrader import DAY, PRIOR, Clock, at, flat_day, frame, index_days, put


@pytest.fixture
def cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )


@pytest.fixture
def mb(cfg):
    return MinuteBars(cfg.data_dir, "close")


@dataclass
class Reply:
    text: str
    model: str = "anthropic/claude-opus-5-5"
    model_matches: bool = True


class TArena(Arena):
    def daily_lookup(self):
        def daily(t):
            return pd.DataFrame({"close": [1.0] * 30, "volume": [2_000_000.0] * 30})

        return daily

    def quote_provider(self):
        q = Quote("NEWS", 1.0, 1.0, 1.0, 0, datetime(2026, 1, 8, 8, 0), "static")
        return StaticQuotes({"NEWS": q, "DTX": q})


@pytest.fixture
def arena(cfg, mb, monkeypatch):
    import asxbot.data.universe as U

    monkeypatch.setattr(U, "fetch_directory", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(DT, "_industries", lambda arena: {})
    broker = ArenaBroker(
        cfg.data_dir, CostModel.from_config(cfg), mb, lambda t: 2_000_000.0,
        resolve_after_minutes=0, clock=Clock(at(8, 30)),
    )  # fmt: skip
    broker.median_turnover = lambda t: 2_000_000.0
    return TArena(cfg, broker, broker.store, {"NEWS", "DTX"}, {"NEWS", "DTX"})


def _market(bars, code="NEWS", move=0.05, vol=5000):
    for d in PRIOR:
        put(bars, code, d, flat_day(d, 1.0, 1000))
    px = 1.0 + move
    put(bars, code, DAY, frame(DAY, {(10, m): (px, px + 0.01, px - 0.02, px, vol)
                                    for m in range(0, 45)}))  # fmt: skip
    index_days(bars, frame(DAY, {(10, m): (8000, 8000, 8000, 8000, 0) for m in range(0, 45)}))


def _decision(net=0.8, side="buy", stop=0.95):
    return (
        "thinking...\n"
        f'{{"action": "trade", "side": "{side}", "ticker": "NEWS", "qty": 4000, "limit": 1.06, '
        f'"stop": {stop}, "target": null, "confidence_pct": 60, '
        f'"expected_move_rest_of_day_pct": 1.4, "expected_net_after_costs_pct": {net}, '
        '"why": "reaction holding"}'
    )


def _announcement(hour=8):
    return Announcement("NEWS", datetime(2026, 1, 8, hour, 30), "Record quarterly result", True,
                        f"ID{hour}", "")  # fmt: skip


def test_news_before_the_open_is_read_queued_and_gets_the_pre_open_look(arena, cfg, monkeypatch):
    pb = load_playbook(cfg, "asx_announcements_v2")
    seen = []
    monkeypatch.setattr(W, "call_agent", lambda agent, msg, **k: Reply(
        "summary\nTRADE_WORTHY: YES\nCAN_SIZE_AND_EXIT: NO"))  # fmt: skip

    def decider(agent, msg, **k):
        seen.append(msg)
        return Reply(_decision())

    monkeypatch.setattr(v2_flow, "call_agent", decider)
    out = W.handle_announcement(arena, pb, _announcement(), at(8, 30))
    assert "PRE-OPEN LOOK" in seen[0] and "0.4% AFTER COSTS" in seen[0]
    acct = arena.account(pb, "agent")
    (o,) = acct.orders.values()  # CAN_SIZE_AND_EXIT: NO no longer stops v2
    assert o.side == "buy" and o.good_till.startswith("2026-01-08T15:40")
    assert out["decider"]["decision"] == "trade"
    assert "NEWS" in load_queue(cfg.data_dir, DAY)


def test_a_trade_below_the_decider_s_own_bar_is_refused(arena, cfg, monkeypatch):
    pb = load_playbook(cfg, "asx_announcements_v2")
    monkeypatch.setattr(W, "call_agent", lambda *a, **k: Reply("x\nTRADE_WORTHY: YES"))
    monkeypatch.setattr(v2_flow, "call_agent", lambda *a, **k: Reply(_decision(net=0.2)))
    out = W.handle_announcement(arena, pb, _announcement(), at(8, 30))
    assert "below the 0.4% bar" in out["decider"]["refused"]
    assert not arena.account(pb, "agent").orders


def test_the_reaction_look_asks_once_with_the_minute_bar_reaction(arena, cfg, mb, monkeypatch):
    pb = load_playbook(cfg, "asx_announcements_v2")
    _market(mb)
    enqueue(cfg.data_dir, DAY, _announcement(), "tradeable")
    calls = []

    def decider(agent, msg, **k):
        calls.append(msg)
        return Reply(_decision() if agent == "trader-decider" else "reader\nTRADE_WORTHY: NO")

    monkeypatch.setattr(v2_flow, "call_agent", decider)
    view = MarketView(mb, DAY, ReplayFeed(mb, 0), "^AXJO", 5, 3)
    arena.broker.clock = Clock(at(10, 12))
    v2_flow.reaction_looks(arena, pb, view, at(10, 12))
    decider_calls = [c for c in calls if "REACTION LOOK" in c]
    assert len(decider_calls) == 1 and '"move_vs_index_pct": 5.0' in decider_calls[0]
    assert load_queue(cfg.data_dir, DAY)["NEWS"]["status"] == "looked"
    assert len(arena.account(pb, "agent").orders) == 1
    v2_flow.reaction_looks(arena, pb, view, at(10, 20))  # one look a day
    assert len([c for c in calls if "REACTION LOOK" in c]) == 1


def test_a_quiet_reaction_does_not_wake_the_decider(arena, cfg, mb, monkeypatch):
    pb = load_playbook(cfg, "asx_announcements_v2")
    _market(mb, move=0.002, vol=1000)
    enqueue(cfg.data_dir, DAY, _announcement(), "tradeable")
    monkeypatch.setattr(v2_flow, "call_agent", lambda *a, **k: pytest.fail("woke the decider"))
    view = MarketView(mb, DAY, ReplayFeed(mb, 0), "^AXJO", 5, 3)
    v2_flow.reaction_looks(arena, pb, view, at(10, 12))
    assert load_queue(cfg.data_dir, DAY)["NEWS"]["status"] == "quiet"


def test_the_v2_rule_bot_buys_once_and_the_flat_sweep_closes_at_its_exit(arena, cfg, mb):
    pb = load_playbook(cfg, "asx_announcements_v2")
    _market(mb)
    bars = mb.cached("NEWS", DAY)
    bars.loc[bars.index >= at(10, 30), "low"] = 1.045  # above the 1.03 stop after the entry
    put(mb, "NEWS", DAY, bars)
    ann = pd.DataFrame([{"code": "NEWS", "released_at": pd.Timestamp("2026-01-08 08:30"),
                         "price_sensitive": True, "ids_id": "X", "headline": "h"}])  # fmt: skip
    view = MarketView(mb, DAY, ReplayFeed(mb, 0), "^AXJO", 5, 3)
    arena.broker.clock = Clock(at(10, 31))
    orders = v2_flow.v2_bot_cycle(arena, pb, view, at(10, 31), ann=ann)
    assert [o["ticker"] for o in orders] == ["NEWS"] and orders[0]["stop"] == pytest.approx(1.03)
    assert load_bot_state(cfg.data_dir, DAY)["status"] == "done"
    assert v2_flow.v2_bot_cycle(arena, pb, view, at(10, 40), ann=ann) == []
    arena.broker.work(arena.account(pb, "bot"), at(10, 45))
    bot = arena.account(pb, "bot")
    assert "NEWS" in bot.positions
    assert v2_flow.flatten(arena, pb, at(15, 52)) == []  # the bot's rule exits at 15:55
    arena.broker.clock = Clock(at(15, 55))
    out = v2_flow.flatten(arena, pb, at(15, 55))
    assert [o["ticker"] for o in out] == ["NEWS"]
    assert v2_flow.flatten(arena, pb, at(15, 56)) == []  # already working to close


def test_a_day_trader_scan_the_bot_takes_and_the_agent_confirms(arena, cfg, mb, monkeypatch):
    pb = load_playbook(cfg, "asx_daytrader")
    for d in PRIOR:
        put(mb, "DTX", d, flat_day(d, 1.0, 1000))
    rows = {(10, m): (1.05, 1.06, 1.04, 1.05, 3000) for m in range(0, 15)}
    rows[(10, 15)] = (1.05, 1.08, 1.05, 1.07, 3000)
    rows[(10, 16)] = (1.07, 1.08, 1.06, 1.07, 3000)
    put(mb, "DTX", DAY, frame(DAY, rows))
    index_days(mb, frame(DAY, {(10, m): (8000, 8000, 8000, 8000, 0) for m in range(0, 20)}))
    st = DT.load_state(cfg.data_dir, DAY)
    st["universe"] = ["DTX"]
    DT.save_state(cfg.data_dir, DAY, st)
    DT._DAY.clear()
    asked = []

    def agent(name, msg, **k):
        asked.append((msg, k.get("timeout_s"), k.get("process_timeout_s")))
        return Reply('{"action": "take", "stop": 1.045, "why": "gap holding, market flat"}')

    monkeypatch.setattr(DT, "call_agent", agent)
    view = MarketView(mb, DAY, ReplayFeed(mb, 0), "^AXJO", 5, 3)
    arena.broker.clock = Clock(at(10, 18))
    out = DT.cycle(arena, pb, view, at(10, 18), refresh=False)
    assert [(r["setup"], r["side"]) for r in out] == [("gap_and_go", "buy")]
    assert out[0]["bot"]["order_id"] and out[0]["agent"]["order_id"]
    assert asked[0][1] == 60 and "ANSWER WITHIN 60 SECONDS" in asked[0][0]
    bot_o = next(iter(arena.account(pb, "bot").orders.values()))
    agent_o = next(iter(arena.account(pb, "agent").orders.values()))
    assert bot_o.stop == pytest.approx(1.04) and agent_o.stop == pytest.approx(1.045)
    assert bot_o.manage["half_at_r"] == 2.0 and bot_o.good_till.startswith("2026-01-08T10:28")
    # risk: $100 (0.5% of $20,000) / 0.04 a share at a 1.08 limit -> 2,500 shares
    assert bot_o.qty * abs(bot_o.limit - bot_o.stop) <= 100.0 + 1e-6
    assert DT.cycle(arena, pb, view, at(10, 19), refresh=False) == []  # fires once


def test_the_agent_is_not_asked_about_a_stale_setup(arena, cfg, mb, monkeypatch):
    pb = load_playbook(cfg, "asx_daytrader")
    for d in PRIOR:
        put(mb, "DTX", d, flat_day(d, 1.0, 1000))
    rows = {(10, m): (1.05, 1.06, 1.04, 1.05, 3000) for m in range(0, 15)}
    rows.update({(10, m): (1.05, 1.08, 1.05, 1.07, 3000) for m in range(15, 30)})
    put(mb, "DTX", DAY, frame(DAY, rows))
    index_days(mb, frame(DAY, {(10, m): (8000, 8000, 8000, 8000, 0) for m in range(0, 40)}))
    st = DT.load_state(cfg.data_dir, DAY)
    st["universe"] = ["DTX"]
    DT.save_state(cfg.data_dir, DAY, st)
    DT._DAY.clear()
    monkeypatch.setattr(DT, "call_agent", lambda *a, **k: pytest.fail("asked about a stale one"))
    view = MarketView(mb, DAY, ReplayFeed(mb, 0), "^AXJO", 5, 3)
    arena.broker.clock = Clock(at(10, 35))
    out = DT.cycle(arena, pb, view, at(10, 35), refresh=False)
    assert out and out[0]["skipped"].startswith("stale")


def test_a_working_pre_open_order_postpones_the_look_rather_than_cancelling_it(
    arena, cfg, mb, monkeypatch
):
    pb = load_playbook(cfg, "asx_announcements_v2")
    _market(mb)
    enqueue(cfg.data_dir, DAY, _announcement(), "tradeable")
    acct = arena.account(pb, "agent")
    arena.broker.clock = Clock(at(8, 40))
    o = arena.broker.submit(acct, ticker="NEWS", side="buy", qty=100, limit=0.5, stop=0.4)
    calls = []
    def agent(name, msg, **k):
        calls.append(name)
        return Reply(_decision())

    monkeypatch.setattr(v2_flow, "call_agent", agent)
    view = MarketView(mb, DAY, ReplayFeed(mb, 0), "^AXJO", 5, 3)
    v2_flow.reaction_looks(arena, pb, view, at(10, 12))
    assert "trader-decider" not in calls
    assert load_queue(cfg.data_dir, DAY)["NEWS"]["status"] == "queued"
    o.status = "expired"  # the auction never met its limit
    arena.broker.store.save(acct)
    arena.broker.clock = Clock(at(10, 14))
    v2_flow.reaction_looks(arena, pb, view, at(10, 14))
    assert "trader-decider" in calls


def test_the_evening_report_says_day_n_of_10_and_delayed_data_for_each_playbook(arena, cfg):
    from datetime import date

    from asxbot.arena.report import agent_brief, gather, render_plain

    facts = gather(arena, date(2026, 9, 25))
    text = render_plain(facts)
    assert "ASX announcements v2 (trade the reaction)</b>: day 1 of 10 (v2)" in text
    assert "ASX day trader</b>: day 1 of 10 (v1)" in text
    assert text.count("delayed data - rehearsal until IBKR live prices") >= 2
    brief = agent_brief(facts)
    assert "day 1 of 10 (v2)" in brief and "word for word" in brief
