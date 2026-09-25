"""Announcements v2 after the 26 Sep 2026 review (C1, C2, C3, C6, C8, C10, C11, C13, C15,
G5, G7 and "agent unavailable"): missing data waits and is then recorded as missed, never
as a verdict; a failed agent call is not a look; the rule bot's order is priced on the last
visible price and its record can be reproduced; the decider's block trades only the stock it
was asked about. Every test fails on the code before the review. No model is called and
nothing touches the network."""

from datetime import datetime

import pandas as pd
import pytest

from asxbot.announcements.model import Announcement
from asxbot.arena import daytrader as DT
from asxbot.arena import v2_flow
from asxbot.arena.agents import AgentCallFailed
from asxbot.arena.broker import ArenaBroker
from asxbot.arena.intraday import IntradayFeed, MarketView, ReplayFeed, visible
from asxbot.arena.levels import load_playbook
from asxbot.arena.minutes import MinuteBars
from asxbot.arena.reaction_v2 import (
    NO_BARS,
    NO_PREV_CLOSE,
    NO_USUAL,
    enqueue,
    load_bot_state,
    load_queue,
    look_status,
    save_queue,
)
from asxbot.arena.runtime import Arena
from asxbot.backtest.costs import CostModel
from asxbot.config import load_config
from asxbot.io import write_parquet_atomic
from asxbot.live.quotes import Quote, StaticQuotes
from asxbot.log import EventLog
from test_v2_daytrader import DAY, PRIOR, Clock, at, flat_day, frame, index_days, put
from test_v2_daytrader_flow import Reply, _decision, _market


# --------------------------------------------------------------------------
# fixtures (as test_v2_daytrader_flow's)
# --------------------------------------------------------------------------
@pytest.fixture
def cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )


@pytest.fixture
def mb(cfg):
    return MinuteBars(cfg.data_dir, "close")


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


@pytest.fixture
def pb(cfg):
    return load_playbook(cfg, "asx_announcements_v2")


class _Hist:
    """Prior sessions from a feed that keeps its own (IBKR's): a missing one is looked for
    again each time, so sessions that arrive later are used."""

    def __init__(self, mb):
        self.mb = mb

    def cached(self, code, day):
        return self.mb.cached(code, day)


class LiveLike(IntradayFeed):
    """A live feed like IBKR's: bars as they are final, its own history, `prepare` recorded,
    `covered` and `entries_allowed` answered as the test says."""

    name = "ibkr"
    delayed = False

    def __init__(self, mb, covered=True, allow=None):
        super().__init__(mb)
        self.prepared: list[list[str]] = []
        self.cover = covered
        self.allow = allow

    def refresh(self, codes, now):
        return list(codes)

    def bars(self, code, day, now):
        return visible(self.minutes.cached(code, day), now, 0, index=code.startswith("^"))

    def history_source(self):
        return _Hist(self.minutes)

    def ensure_history(self, code, day, sessions):
        return True  # asked for; nothing is fetched in a test

    def prepare(self, codes, day, sessions):
        self.prepared.append(list(codes))
        return len(codes)

    def covered(self, code, start, end):
        return self.cover(code, start, end) if callable(self.cover) else bool(self.cover)

    def entries_allowed(self, now, codes=()):
        return (True, "") if self.allow is None else self.allow(now, list(codes))


def _live(mb, **kw):
    feed = LiveLike(mb, **kw)
    return MarketView(mb, DAY, feed, "^AXJO", 5, 3), feed


def _replay(mb):
    return MarketView(mb, DAY, ReplayFeed(mb, 0), "^AXJO", 5, 3)


def _ann(*codes, hour=8, minute=30):
    return pd.DataFrame([
        {"code": c, "released_at": pd.Timestamp(2026, 1, 8, hour, minute),
         "price_sensitive": True, "ids_id": f"ID{c}", "headline": "h", "pdf_url": ""}
        for c in codes
    ])  # fmt: skip


def _today(mb, code, early=1.05, late=None, vol=5000, until=45):
    """Today's bars: `early` 10:00-10:29, then `late` (default `early`) to 10:`until`."""
    late = early if late is None else late
    rows = {}
    for m in range(0, until):
        px = early if m < 30 else late
        rows[(10, m)] = (px, px + 0.01, px - 0.02, px, vol)
    put(mb, code, DAY, frame(DAY, rows))


def _index(mb, until=45):
    index_days(mb, frame(DAY, {(10, m): (8000, 8000, 8000, 8000, 0) for m in range(0, until)}))


def _prior(mb, code, n=3):
    for d in PRIOR[:n]:
        put(mb, code, d, flat_day(d, 1.0, 1000))


def _news(hour=8, minute=30):
    return Announcement("NEWS", datetime(2026, 1, 8, hour, minute), "Record quarterly result",
                        True, f"ID{hour}{minute}", "")  # fmt: skip


def _agents(monkeypatch, decider=None):
    """Stand-in agents; returns the list of (agent, message) calls."""
    calls = []

    def agent(name, msg, **k):
        calls.append((name, msg))
        if name == "trader-decider":
            return decider(msg) if decider else Reply(_decision())
        return Reply("reader summary\nTRADE_WORTHY: NO")

    monkeypatch.setattr(v2_flow, "call_agent", agent)
    return calls


def _deciders(calls):
    return [m for a, m in calls if a == "trader-decider"]


# --------------------------------------------------------------------------
# C1, C6, C11: the rule bot decides each candidate as its data arrives
# --------------------------------------------------------------------------
def test_a_candidate_without_prices_waits_and_is_decided_when_they_arrive(arena, pb, mb):
    _index(mb)
    _prior(mb, "DTX")
    _today(mb, "DTX")
    _today(mb, "NEWS", early=1.05, late=1.10)  # NEWS: no prior sessions yet (25 Sep, 10:30)
    view, _ = _live(mb)
    ann = _ann("DTX", "NEWS")
    arena.broker.clock = Clock(at(10, 31))
    first = v2_flow.v2_bot_cycle(arena, pb, view, at(10, 31), ann=ann)
    assert [o["ticker"] for o in first] == ["DTX"]
    st = load_bot_state(arena.cfg.data_dir, DAY)
    assert st["status"] == "waiting" and st["pending"] == {"NEWS": NO_PREV_CLOSE}
    assert [c["ticker"] for c in st["candidates"]] == ["DTX"]  # NEWS is not "no signal"
    dtx = st["candidates"][0]
    # C11: the inputs of the 10:30 measure, so the decision can be reproduced
    inp = dtx["inputs"]
    assert inp["previous_close"] == 1.0 and inp["index_previous_close"] == 8000.0
    assert inp["last"] == 1.05 and inp["last_bar"] == "10:29" and inp["index_last"] == 8000.0
    assert inp["volume_1001_1029"] == 29 * 5000 and inp["usual_volume_1001_1029"] == 29 * 1000
    assert inp["usual_sessions"] == [d.isoformat() for d in PRIOR]
    assert inp["previous_close_session"] == PRIOR[0].isoformat()
    assert inp["data"] == "live data (IBKR)" and dtx["lag_min"] == 1.0

    _prior(mb, "NEWS")  # its prior sessions arrive
    arena.broker.clock = Clock(at(10, 40))
    second = v2_flow.v2_bot_cycle(arena, pb, view, at(10, 40), ann=ann)
    assert [o["ticker"] for o in second] == ["NEWS"]
    st = load_bot_state(arena.cfg.data_dir, DAY)
    assert st["status"] == "done" and "pending" not in st
    news = next(c for c in st["candidates"] if c["ticker"] == "NEWS")
    assert news["signal"] is True and news["lag_min"] == 10.0
    assert news["inputs"]["last"] == 1.05  # measured at 10:30, whenever it is decided
    # C6: the limit is 2% through the last price visible at 10:40 (1.10), not the 10:29 close
    order = next(o for o in st["orders"] if o["ticker"] == "NEWS")
    assert order["limit"] == pytest.approx(1.125) and order["stop"] == pytest.approx(1.03)
    assert order["limit_from"] == {"price": 1.10, "bar": "10:38"}
    assert order["decision_lag_min"] == 10.0
    assert v2_flow.v2_bot_cycle(arena, pb, view, at(10, 45), ann=ann) == []


def test_a_candidate_still_without_prices_at_the_deadline_is_missed_not_no_signal(arena, pb, mb):
    _index(mb)
    _today(mb, "NEWS")
    view, _ = _live(mb)
    ann = _ann("NEWS")
    arena.broker.clock = Clock(at(10, 31))
    assert v2_flow.v2_bot_cycle(arena, pb, view, at(10, 31), ann=ann) == []
    assert load_bot_state(arena.cfg.data_dir, DAY)["status"] == "waiting"
    arena.broker.clock = Clock(at(11, 16))
    v2_flow.v2_bot_cycle(arena, pb, view, at(11, 16), ann=ann)
    st = load_bot_state(arena.cfg.data_dir, DAY)
    (c,) = st["candidates"]
    assert c["missed"] == "no data" and "signal" not in c
    assert c["why"].startswith("missed: no data by 11:15") and NO_PREV_CLOSE in c["why"]
    assert st["status"] == "missed" and "no data for any candidate" in st["why"]


@pytest.mark.parametrize("covered, status, want", [
    (False, "waiting", None),
    (True, "done", "no trade 10:00-10:30"),
])  # fmt: skip
def test_no_bars_is_a_verdict_only_if_the_feed_watched_the_stock(
    arena, pb, mb, covered, status, want
):
    _index(mb)
    _prior(mb, "NEWS")  # prior sessions, but no bars today
    view, _ = _live(mb, covered=covered)
    arena.broker.clock = Clock(at(10, 31))
    v2_flow.v2_bot_cycle(arena, pb, view, at(10, 31), ann=_ann("NEWS"))
    st = load_bot_state(arena.cfg.data_dir, DAY)
    assert st["status"] == status
    if want is None:
        assert st["pending"] == {"NEWS": NO_BARS}
    else:
        assert st["candidates"][0]["signal"] is False and st["candidates"][0]["why"] == want


def test_prior_sessions_are_asked_for_before_10_30(arena, pb, mb):
    _index(mb)
    view, feed = _live(mb)
    arena.broker.clock = Clock(at(9, 40))
    assert v2_flow.v2_bot_cycle(arena, pb, view, at(9, 40), ann=_ann("NEWS", "DTX")) == []
    assert feed.prepared == [["^AXJO", "DTX", "NEWS"]]
    v2_flow.v2_bot_cycle(arena, pb, view, at(9, 40), ann=_ann("NEWS", "DTX"))
    assert len(feed.prepared) == 1  # at most once a minute
    # and a reaction look's stock as soon as it is queued (news during the session)
    enqueue(arena.cfg.data_dir, DAY, _news(10, 50), "tradeable")
    v2_flow.reaction_looks(arena, pb, view, at(10, 51))
    assert ["NEWS"] in feed.prepared


# --------------------------------------------------------------------------
# C2: no usual volume is missing data, not "quiet, volume unknown"
# --------------------------------------------------------------------------
def test_a_look_with_no_usual_volume_waits_and_is_then_missed_for_want_of_data(
    arena, pb, mb, monkeypatch
):
    _index(mb)
    _prior(mb, "NEWS", n=1)  # a previous close, but too few sessions for a usual volume
    _today(mb, "NEWS", early=1.002, vol=1000)  # +0.2%: the move alone does not wake it
    enqueue(arena.cfg.data_dir, DAY, _news(), "tradeable")
    calls = _agents(monkeypatch)
    view = _replay(mb)
    v2_flow.reaction_looks(arena, pb, view, at(10, 12))
    item = load_queue(arena.cfg.data_dir, DAY)["NEWS"]
    assert item["status"] == "queued" and item["waiting"] == NO_USUAL
    v2_flow.reaction_looks(arena, pb, view, at(10, 44))  # the 40-minute window has closed
    item = load_queue(arena.cfg.data_dir, DAY)["NEWS"]
    assert item["status"] == "missed" and item["why"].startswith(f"no data ({NO_USUAL})")
    assert not calls


# --------------------------------------------------------------------------
# C3: no bars is "no trade" only if the feed watched the stock
# --------------------------------------------------------------------------
def test_no_bars_after_the_news_waits_unless_the_feed_watched_the_stock(pb, mb):
    _index(mb)
    _prior(mb, "NEWS")
    item = {"ticker": "NEWS", "first_release": datetime(2026, 1, 8, 8, 30).isoformat()}
    unwatched, _ = _live(mb, covered=False)
    assert look_status(unwatched, item, at(10, 12), pb).status == "wait"  # was "halted"
    late = look_status(unwatched, item, at(10, 44), pb)
    assert late.status == "missed" and late.why.startswith("no data")
    watched, _ = _live(mb, covered=True)
    st = look_status(watched, item, at(10, 12), pb)
    assert st.status == "no_trade" and "(thin, halted or no data)" in st.why


# --------------------------------------------------------------------------
# C10: "wait" is not a verdict, and the feed is checked before each look
# --------------------------------------------------------------------------
def test_a_look_that_is_not_ready_by_its_turn_stays_queued(arena, pb, mb, monkeypatch):
    _market(mb)
    enqueue(arena.cfg.data_dir, DAY, _news(), "tradeable")
    calls = _agents(monkeypatch)
    arena.broker.clock = Clock(at(10, 5))  # its turn: the feed has not reached 10:09
    v2_flow.reaction_looks(arena, pb, _replay(mb), at(10, 12))
    assert load_queue(arena.cfg.data_dir, DAY)["NEWS"]["status"] == "queued"  # was "missed"
    assert not calls


def test_the_feed_is_checked_for_the_stock_before_each_look(arena, pb, mb, monkeypatch):
    _market(mb)
    enqueue(arena.cfg.data_dir, DAY, _news(), "tradeable")
    calls = _agents(monkeypatch)

    def stale(now, codes):
        return (False, "stale: no bar from IBKR for NEWS") if "NEWS" in codes else (True, "")

    view, _ = _live(mb, allow=stale)
    arena.broker.clock = Clock(at(10, 12))
    v2_flow.reaction_looks(arena, pb, view, at(10, 12))
    assert not _deciders(calls)
    assert load_queue(arena.cfg.data_dir, DAY)["NEWS"]["status"] == "queued"


# --------------------------------------------------------------------------
# agent unavailable: a failed decider call is not a look
# --------------------------------------------------------------------------
def test_a_failed_decider_call_keeps_the_look_queued_until_its_window_closes(
    arena, pb, mb, monkeypatch
):
    _market(mb)
    enqueue(arena.cfg.data_dir, DAY, _news(), "tradeable")

    def unavailable(msg):
        e = AgentCallFailed("You've hit your usage limit")
        e.kind = "usage_limit"
        raise e

    calls = _agents(monkeypatch, unavailable)
    view = _replay(mb)
    for t in (at(10, 12), at(10, 13)):
        arena.broker.clock = Clock(t)
        v2_flow.reaction_looks(arena, pb, view, t)
    item = load_queue(arena.cfg.data_dir, DAY)["NEWS"]
    assert item["status"] == "queued" and item["agent_failed"]["kind"] == "usage_limit"
    assert len(_deciders(calls)) == 1  # not again within AGENT_RETRY
    arena.broker.clock = Clock(at(10, 15))
    v2_flow.reaction_looks(arena, pb, view, at(10, 15))
    assert len(_deciders(calls)) == 2
    arena.broker.clock = Clock(at(10, 44))
    v2_flow.reaction_looks(arena, pb, view, at(10, 44))
    item = load_queue(arena.cfg.data_dir, DAY)["NEWS"]
    assert item["status"] == "missed" and item["why"].startswith("agent unavailable (usage_limit)")
    assert "outcome" not in item and not arena.account(pb, "agent").orders


# --------------------------------------------------------------------------
# C13: a look cut off by a crash is not left "looking" for good
# --------------------------------------------------------------------------
def _cut_off(arena, since):
    q = load_queue(arena.cfg.data_dir, DAY)
    q["NEWS"].update(status="looking", looking_since=since.isoformat())
    save_queue(arena.cfg.data_dir, DAY, q)


def test_a_look_cut_off_before_the_decider_answered_is_looked_at_again(arena, pb, mb, monkeypatch):
    _market(mb)
    enqueue(arena.cfg.data_dir, DAY, _news(), "tradeable")
    _cut_off(arena, at(10, 0))
    calls = _agents(monkeypatch)
    arena.broker.clock = Clock(at(10, 12))
    v2_flow.reaction_looks(arena, pb, _replay(mb), at(10, 12))
    assert len(_deciders(calls)) == 1
    assert load_queue(arena.cfg.data_dir, DAY)["NEWS"]["status"] == "looked"


def test_a_look_cut_off_after_the_decider_answered_is_not_asked_again(arena, pb, mb, monkeypatch):
    _market(mb)
    enqueue(arena.cfg.data_dir, DAY, _news(), "tradeable")
    _cut_off(arena, at(10, 0))
    EventLog(arena.cfg.data_dir).append("arena_decisions", {
        "ts": at(10, 1).isoformat(), "stage": "decider", "ticker": "NEWS", "v2": "reaction",
        "decision": {"action": "pass", "why": "no follow-through"}})  # fmt: skip
    calls = _agents(monkeypatch)
    v2_flow.reaction_looks(arena, pb, _replay(mb), at(10, 12))
    item = load_queue(arena.cfg.data_dir, DAY)["NEWS"]
    assert item["status"] == "looked" and item["outcome"]["decision"] == "pass"
    assert "cut off" in item["outcome"]["recovered"] and not calls


# --------------------------------------------------------------------------
# C15: no model call for a look after the last entry time
# --------------------------------------------------------------------------
def test_no_look_is_made_after_the_entry_cut_off(arena, pb, mb, monkeypatch):
    _prior(mb, "NEWS")
    put(mb, "NEWS", DAY, frame(DAY, {(15, m): (1.05, 1.06, 1.03, 1.05, 5000)
                                     for m in range(20, 45)}))  # fmt: skip
    index_days(mb, frame(DAY, {(h, m): (8000, 8000, 8000, 8000, 0)
                               for h in (10, 11, 12, 13, 14, 15) for m in range(60)}))  # fmt: skip
    enqueue(arena.cfg.data_dir, DAY, _news(15, 25), "tradeable")
    calls = _agents(monkeypatch)
    arena.broker.clock = Clock(at(15, 45))
    v2_flow.reaction_looks(arena, pb, _replay(mb), at(15, 45))
    item = load_queue(arena.cfg.data_dir, DAY)["NEWS"]
    assert item["status"] == "missed" and "after the entry cut-off (15:40)" in item["why"]
    assert not calls


# --------------------------------------------------------------------------
# G5: the reaction packet says which prices it is on
# --------------------------------------------------------------------------
def test_the_reaction_packet_names_the_feed_and_the_age_of_its_newest_bar(
    arena, pb, mb, monkeypatch
):
    _market(mb)
    enqueue(arena.cfg.data_dir, DAY, _news(), "tradeable")
    calls = _agents(monkeypatch)
    view, _ = _live(mb)
    arena.broker.clock = Clock(at(10, 12))
    v2_flow.reaction_looks(arena, pb, view, at(10, 12))
    (packet,) = _deciders(calls)
    assert "DELAYED (~20 minutes" not in packet and "Sonnet 5" not in packet
    assert ("Its newest bar is the 10:10 minute, which ended 1 minute before this decision; "
            "the prices are live data (IBKR).") in packet  # fmt: skip
    assert "READER'S SUMMARY (trader-reader)\n" in packet


# --------------------------------------------------------------------------
# G7: the decision block
# --------------------------------------------------------------------------
def _block(**kw):
    d = {
        "action": "trade",
        "side": "buy",
        "ticker": "NEWS",
        "qty": 1000,
        "limit": 1.06,
        "stop": 0.95,
        "target": None,
        "expected_net_after_costs_pct": 0.8,
        "why": "x",
    }
    return {**d, **kw}  # fmt: skip


def test_the_decision_trades_only_the_stock_it_was_asked_about(arena, pb, mb):
    _market(mb)
    _market(mb, code="DTX")
    arena.broker.clock = Clock(at(10, 12))
    acct = arena.account(pb, "agent")
    out = v2_flow.place_decision(arena, pb, acct, "NEWS", _block(ticker="DTX"), "m", at(10, 12),
                                 "reaction")  # fmt: skip
    assert out["refused"].startswith("the decision names DTX, not NEWS")
    assert not arena.account(pb, "agent").orders


def test_the_side_is_normalised_before_the_prices_are_rounded(arena, pb, mb):
    _market(mb)
    arena.broker.clock = Clock(at(10, 12))
    acct = arena.account(pb, "agent")
    out = v2_flow.place_decision(arena, pb, acct, "NEWS",
                                 _block(side="BUY", limit=1.062, stop=0.951), "m", at(10, 12),
                                 "reaction")  # fmt: skip
    assert out["side"] == "buy"
    assert out["limit"] == pytest.approx(1.065) and out["stop"] == pytest.approx(0.95)


def test_an_unusable_target_is_refused_with_its_reason(arena, pb, mb):
    _market(mb)
    arena.broker.clock = Clock(at(10, 12))
    acct = arena.account(pb, "agent")
    out = v2_flow.place_decision(arena, pb, acct, "NEWS", _block(target="about 1.2"), "m",
                                 at(10, 12), "reaction")  # fmt: skip
    assert "not usable" in out["refused"] and "about 1.2" in out["refused"]
    assert not arena.account(pb, "agent").orders
    none = v2_flow.place_decision(arena, pb, acct, "NEWS", _block(target="none"), "m",
                                  at(10, 12), "reaction")  # fmt: skip
    assert none["order_id"]  # "none" is no target, as null is


# --------------------------------------------------------------------------
# C8: an announcement screened out on arrival is not screened again
# --------------------------------------------------------------------------
def test_the_safety_net_does_not_rescreen_a_verdict_only_a_deferral(arena, pb, cfg):
    live = cfg.data_dir / "announcements" / "live"
    live.mkdir(parents=True, exist_ok=True)
    write_parquet_atomic(_ann("NEWS", "DTX"), live / f"{DAY.isoformat()}.parquet")
    ev = EventLog(cfg.data_dir)
    for code, test in (("NEWS", "tick"), ("DTX", "deferred")):  # the verdicts on arrival
        ev.append("arena_screened", {"ticker": code, "ids_id": f"ID{code}", "ok": False,
                                     "test": test, "why": "x", "v2": True})  # fmt: skip
    added = v2_flow.seed_queue(arena, pb, DAY, at(10, 5))
    assert added == ["DTX"]
    again = [r for r in ev.read("arena_screened") if r.get("seeded")]
    assert [r["ids_id"] for r in again] == ["IDDTX"]
    assert sorted(load_queue(cfg.data_dir, DAY)["_seeded"]) == ["IDDTX", "IDNEWS"]
    assert v2_flow.seed_queue(arena, pb, DAY, at(10, 6)) == []
