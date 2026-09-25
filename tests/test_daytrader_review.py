"""The day trader's review fixes (26 Sep 2026). Each test fails on the code before the fix:

  B1   a hole in the feed's bars is not a halt, and the scan stops at a hole it has not
       watched, picking up from it once the feed has the minutes;
  B3   a setup exactly max_signal_age_bars old goes to the agent as it goes to the bot;
  B4   prices keep their half-cents in the why-strings and the packet;
  B5   the packet's news covers the same window as the scan's news_today;
  B7   the limit goes through the newest visible price, not the trigger bar's close;
  B8   the feed is asked about the stock's own prices before either book acts, and again
       before the agent's order is placed;
  B9   an answer after 60 s is a rejection;
  B12  the day's state survives an exception after the scan;
  G5   the packet's DATA line says which feed and how old the newest bar is;
  G13  the agent's tightened stop is put through the uneconomic filter again;
  A12  the 09:55 history report runs at the first cycle after 09:55, whenever that is.
No model is called and nothing touches the network.
"""

from datetime import timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from asxbot.arena import daytrader as DT
from asxbot.arena.broker import ArenaBroker
from asxbot.arena.intraday import MarketView, ReplayFeed, visible
from asxbot.arena.levels import load_playbook
from asxbot.arena.minutes import MinuteBars
from asxbot.backtest.costs import CostModel
from asxbot.config import load_config
from asxbot.io import write_parquet_atomic
from asxbot.log import EventLog
from test_v2_daytrader import DAY, PRIOR, Clock, at, flat_day, frame, index_days, put
from test_v2_daytrader_flow import Reply, TArena


@pytest.fixture
def cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )


@pytest.fixture
def mb(cfg):
    return MinuteBars(cfg.data_dir, "close")


@pytest.fixture(autouse=True)
def _fresh_day():
    DT._DAY.clear()
    DT._PRE.clear()
    yield
    DT._DAY.clear()
    DT._PRE.clear()


@pytest.fixture
def arena(cfg, mb, monkeypatch):
    import asxbot.data.universe as U

    monkeypatch.setattr(U, "fetch_directory", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(DT, "_industries", lambda arena: {})
    broker = ArenaBroker(
        cfg.data_dir, CostModel.from_config(cfg), mb, lambda t: 2_000_000.0,
        resolve_after_minutes=0, clock=Clock(at(10, 22)),
    )  # fmt: skip
    broker.median_turnover = lambda t: 2_000_000.0
    return TArena(cfg, broker, broker.store, {"NEWS", "DTX"}, {"NEWS", "DTX"})


class Tick:
    """A wall clock that moves on a second at every look, as it does live."""

    def __init__(self, t):
        self.t = t

    def __call__(self):
        self.t += timedelta(seconds=1)
        return self.t


class Feed(ReplayFeed):
    """The replay's bars, with the answers the IBKR feed gives: which minutes it watched,
    and whether new entries are allowed on a stock's prices."""

    def __init__(self, mb, holes=(), blocked=()):
        super().__init__(mb, 0)
        self.holes = list(holes)  # (start, end) stretches the feed did not watch
        self.blocked = set(blocked)
        self.asked: list = []

    def covered(self, code, start, end):
        return not any(start <= h_end and h_start <= end for h_start, h_end in self.holes)

    def entries_allowed(self, now, codes=()):
        self.asked.append(list(codes))
        bad = [c for c in codes if c in self.blocked]
        return (False, f"stale: no bar from IBKR for {bad[0]} for 200s") if bad else (True, "")


def _universe(cfg, codes=("DTX",)):
    st = DT.load_state(cfg.data_dir, DAY)
    st["universe"] = list(codes)
    DT.save_state(cfg.data_dir, DAY, st)


def _gap_market(mb, through=20, after=(1.07, 1.08, 1.06, 1.07, 3000)):
    """DTX gaps 5% over the index and first closes above its 15-minute high at 10:15
    (gap-and-go, stop 1.04); then `after` every minute to 10:`through`."""
    for d in PRIOR:
        put(mb, "DTX", d, flat_day(d, 1.0, 1000))
    rows = {(10, m): (1.05, 1.06, 1.04, 1.05, 3000) for m in range(0, 15)}
    rows[(10, 15)] = (1.05, 1.08, 1.05, 1.07, 3000)
    rows.update({(10, m): after for m in range(16, through + 1)})
    put(mb, "DTX", DAY, frame(DAY, rows))
    index_days(mb, frame(DAY, {(10, m): (8000, 8000, 8000, 8000, 0) for m in range(0, 50)}))


def _take(stop="null"):
    return Reply(f'{{"action": "take", "stop": {stop}, "why": "gap holding"}}')


# -- B1: a hole is not a halt ------------------------------------------------------------
def _halt_market(mb):
    """Trading every minute to 11:19, nothing 11:20-11:34, a resumption range 11:35-11:44
    and a close above it at 11:46 (the halt setup of test_v2_daytrader)."""
    for d in PRIOR:
        put(mb, "DTX", d, flat_day(d, 1.0, 1000))
    rows = {(10, m): (1.0, 1.0, 1.0, 1.0, 2000) for m in range(0, 60)}
    rows.update({(11, m): (1.0, 1.0, 1.0, 1.0, 2000) for m in range(0, 20)})
    rows.update({(11, m): (1.05, 1.06, 1.04, 1.05, 8000) for m in range(35, 45)})
    rows[(11, 46)] = (1.06, 1.08, 1.06, 1.07, 8000)
    put(mb, "DTX", DAY, frame(DAY, rows))
    index_days(mb, frame(DAY, {(h, m): (8000, 8000, 8000, 8000, 0) for h in (10, 11)
                               for m in range(60)}))  # fmt: skip


def _scan(cfg, feed, mb, now, state, news=()):
    view = MarketView(mb, DAY, feed, "^AXJO", 5, 3)
    pb = load_playbook(cfg, "asx_daytrader")
    return DT.scan(view, pb, ["DTX"], now, state, {"DTX"}, set(news))


def test_a_stretch_the_feed_did_not_watch_is_not_a_halt(cfg, mb):
    _halt_market(mb)
    feed = Feed(mb, holes=[(at(11, 20), at(11, 34))])
    state = DT.load_state(cfg.data_dir, DAY)
    found, summary = _scan(cfg, feed, mb, at(12, 0), state)
    assert [s for s in found if s.setup == "halt_resumption"] == []
    assert summary["resumed"] == []
    # evaluated up to the hole, and no further: the minutes are looked at again later
    assert state["last_eval"]["DTX"].startswith("2026-01-08T11:19")
    # the feed now has those minutes and they had no trades: a real halt, found at 11:46
    feed.holes = []
    found, _ = _scan(cfg, feed, mb, at(12, 0), state)
    halts = [s for s in found if s.setup == "halt_resumption"]
    assert [(s.trigger_bar[11:16], s.side) for s in halts] == [("11:46", "buy")]
    assert state["last_eval"]["DTX"].startswith("2026-01-08T11:46")


def test_a_bar_after_a_hole_is_not_taken_for_the_first_break(cfg, mb):
    """LEARNINGS #28 through a hole: the real first close above the range was in minutes
    the feed missed, and was quiet. The heavy 10:35 bar is not the first, so no breakout."""
    for d in PRIOR:
        put(mb, "DTX", d, flat_day(d, 1.0, 1000))
    rows = {(10, m): (1.0, 1.01, 0.99, 1.0, 2000) for m in range(0, 31)}
    rows.update({(10, m): (1.02, 1.03, 1.0, 1.02, 9000 if m == 35 else 2000)
                 for m in range(35, 41)})  # fmt: skip
    put(mb, "DTX", DAY, frame(DAY, rows))
    index_days(mb, frame(DAY, {(10, m): (8000, 8000, 8000, 8000, 0) for m in range(60)}))
    feed = Feed(mb, holes=[(at(10, 31), at(10, 34))])
    state = DT.load_state(cfg.data_dir, DAY)
    found, _ = _scan(cfg, feed, mb, at(10, 42), state)
    assert [s for s in found if s.setup == "opening_range_breakout"] == []
    assert state["last_eval"]["DTX"].startswith("2026-01-08T10:30")
    # the feed fills the hole: 10:31 was the first close above the range, on no volume
    rows.update({(10, m): (1.0, 1.02, 1.0, 1.015, 2000) for m in range(31, 35)})
    put(mb, "DTX", DAY, frame(DAY, rows))
    feed._cache.clear()
    feed.holes = []
    found, _ = _scan(cfg, feed, mb, at(10, 42), state)
    assert [s for s in found if s.setup == "opening_range_breakout"] == []


def test_a_late_first_bar_the_feed_did_not_watch_for_is_not_a_late_start(cfg, mb):
    conf = load_playbook(cfg, "asx_daytrader").raw["setups"]
    bars = frame(DAY, {(10, m): (1.05, 1.06, 1.04, 1.05, 5000) for m in range(45, 60)})
    index = frame(DAY, {(10, m): (8000, 8000, 8000, 8000, 0) for m in range(60)})
    usual = pd.Series(1.0, index=range(360))

    def ctx(covered):
        return DT.Ctx("DTX", DAY, bars, index, 1.0, 8000.0, usual, True, True, conf,
                      covered=covered)  # fmt: skip

    assert DT.resumptions(ctx(lambda a, b: False)) == []
    assert DT.resumptions(ctx(None)) == [(at(10, 45), 1.0, None)]  # a feed that saw the day


# -- B3: one freshness test for both books ------------------------------------------------
def test_a_setup_exactly_at_the_age_limit_goes_to_the_agent_as_to_the_bot(
    arena, cfg, mb, monkeypatch
):
    pb = load_playbook(cfg, "asx_daytrader")
    _gap_market(mb, through=20)  # the 10:15 trigger is 5 bars old at 10:22
    _universe(cfg)
    asked = []
    monkeypatch.setattr(DT, "call_agent", lambda *a, **k: asked.append(1) or _take())
    arena.broker.clock = Tick(at(10, 22))
    view = MarketView(mb, DAY, ReplayFeed(mb, 0), "^AXJO", 5, 3)
    out = DT.cycle(arena, pb, view, at(10, 22), refresh=False)
    assert out[0]["context"]["age_bars"] == int(pb.raw["max_signal_age_bars"]) == 5
    assert out[0]["bot"]["order_id"]
    assert asked and out[0]["agent"].get("order_id"), out[0]["agent"]


# -- B4: prices keep their decimals -------------------------------------------------------
def test_prices_keep_their_half_cents(cfg):
    conf = load_playbook(cfg, "asx_daytrader").raw["setups"]
    rows = {(10, m): (108.0, 108.5, 107.9, 108.2, 3000) for m in range(0, 15)}
    rows[(10, 15)] = (108.2, 108.9, 108.2, 108.85, 3000)
    bars = frame(DAY, rows)
    index = frame(DAY, {(10, m): (8000, 8000, 8000, 8000, 0) for m in range(60)})
    c = DT.Ctx("XYZ", DAY, bars, index, 100.0, 8000.0, pd.Series(1.0, index=range(360)),
               True, False, conf)  # fmt: skip
    (s,) = DT.detect(c, None, set(), DT._t("15:45"))
    assert "first close (108.85) above the 15-min high 108.50" in s.why
    ctx = {"range_high": 27.76, "range_low": 27.485, "range_minutes": 30, "first_break": "10:38"}
    line = DT._range_line(DT.Setup("AUB", "opening_range_breakout", "short", "t", 27.3, 27.62,
                                   2.0, "", ctx))  # fmt: skip
    assert "low 27.485, high 27.76" in line
    assert [DT._px(x) for x in (108.85, 1.0, 0.115, 27.485, 12.345)] == [
        "108.85", "1.00", "0.115", "27.485", "12.345"]  # fmt: skip


# -- B5: the packet's news window ---------------------------------------------------------
def test_the_packet_carries_last_evenings_news_as_the_scan_does(cfg):
    def ann(rows):
        return pd.DataFrame([{"code": c, "released_at": pd.Timestamp(t), "price_sensitive": ps,
                              "ids_id": c + t, "headline": h} for c, t, ps, h in rows])  # fmt: skip

    live = cfg.data_dir / "announcements" / "live"
    write_parquet_atomic(ann([("NEWS", "2026-01-07 15:00", True, "Before the close"),
                              ("DTX", "2026-01-07 17:30", True, "Takeover offer")]),
                         live / "2026-01-07.parquet")  # fmt: skip
    write_parquet_atomic(ann([("DTX", "2026-01-08 08:30", False, "Response to offer")]),
                         live / "2026-01-08.parquet")  # fmt: skip
    assert DT.news_today(cfg.data_dir, DAY) == {"DTX"}
    rows = DT._news_rows(SimpleNamespace(cfg=cfg), DAY)
    assert rows == {"DTX": ["Wed 07 Jan 17:30 [price sensitive] Takeover offer",
                            "08:30 Response to offer"]}  # fmt: skip


# -- B7: the limit through the last visible price -----------------------------------------
def test_the_limit_goes_through_the_newest_visible_price(arena, cfg, mb, monkeypatch):
    pb = load_playbook(cfg, "asx_daytrader")
    _gap_market(mb, through=17, after=(1.09, 1.10, 1.08, 1.09, 3000))
    _universe(cfg)
    monkeypatch.setattr(DT, "call_agent", lambda *a, **k: _take())
    arena.broker.clock = Clock(at(10, 19))
    view = MarketView(mb, DAY, ReplayFeed(mb, 0), "^AXJO", 5, 3)
    out = DT.cycle(arena, pb, view, at(10, 19), refresh=False)
    assert out[0]["last"] == pytest.approx(1.07)  # the trigger bar's close
    bot_o = next(iter(arena.account(pb, "bot").orders.values()))
    agent_o = next(iter(arena.account(pb, "agent").orders.values()))
    assert bot_o.limit == agent_o.limit == pytest.approx(1.105)  # 1.09 + 1%, to the tick
    assert out[0]["bot"]["last"] == pytest.approx(1.09)


# -- B8: this stock's prices, before each book acts ---------------------------------------
def test_no_entry_on_a_stock_whose_own_prices_are_stale(arena, cfg, mb, monkeypatch):
    pb = load_playbook(cfg, "asx_daytrader")
    _gap_market(mb, through=17)
    _universe(cfg)
    monkeypatch.setattr(DT, "call_agent", lambda *a, **k: pytest.fail("asked on stale prices"))
    feed = Feed(mb, blocked={"DTX"})
    arena.broker.clock = Clock(at(10, 19))
    view = MarketView(mb, DAY, feed, "^AXJO", 5, 3)
    out = DT.cycle(arena, pb, view, at(10, 19), refresh=False)
    assert out[0]["skipped"].startswith("no new entry on DTX's prices now: stale")
    assert not arena.account(pb, "bot").orders and not arena.account(pb, "agent").orders
    assert ["DTX"] in feed.asked


def test_the_agents_order_is_not_placed_if_the_prices_went_stale_during_the_call(
    arena, cfg, mb, monkeypatch
):
    pb = load_playbook(cfg, "asx_daytrader")
    _gap_market(mb, through=17)
    _universe(cfg)
    feed = Feed(mb)

    def agent(*a, **k):
        feed.blocked.add("DTX")  # the stream stalls while the agent thinks
        return _take()

    monkeypatch.setattr(DT, "call_agent", agent)
    arena.broker.clock = Clock(at(10, 19))
    view = MarketView(mb, DAY, feed, "^AXJO", 5, 3)
    out = DT.cycle(arena, pb, view, at(10, 19), refresh=False)
    assert out[0]["bot"]["order_id"]
    assert "no new entry on DTX's prices now" in out[0]["agent"]["refused"]
    assert not arena.account(pb, "agent").orders


# -- B9: 60 seconds means 60 seconds ------------------------------------------------------
def test_an_answer_after_60_seconds_is_a_rejection(cfg, monkeypatch):
    pb = load_playbook(cfg, "asx_daytrader")
    ticks = iter([1000.0, 1065.0])
    monkeypatch.setattr(DT, "time_mod", SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(DT, "agent_packet", lambda *a, **k: "packet")
    monkeypatch.setattr(DT, "call_agent", lambda *a, **k: _take())
    s = DT.Setup("DTX", "gap_and_go", "buy", at(10, 15).isoformat(), 1.07, 1.04, 3.0, "")
    t = {"qty": 100, "limit": 1.085, "stop": 1.04, "risk": 4.5, "value": 108.5}
    d = DT.ask_agent(SimpleNamespace(cfg=cfg), pb, s, t, {}, at(10, 18))
    assert d["action"] == "reject" and "past 60s" in d["why"] and d["late_answer"] == "take"


# -- B12: the state survives an exception after the scan ----------------------------------
def test_an_exception_after_the_scan_loses_neither_the_state_nor_the_other_book(
    arena, cfg, mb, monkeypatch
):
    pb = load_playbook(cfg, "asx_daytrader")
    _gap_market(mb, through=17)
    _universe(cfg)

    def boom(*a, **k):
        raise RuntimeError("the agent path broke")

    monkeypatch.setattr(DT, "_agent_take", boom)
    arena.broker.clock = Clock(at(10, 19))
    view = MarketView(mb, DAY, ReplayFeed(mb, 0), "^AXJO", 5, 3)
    out = DT.cycle(arena, pb, view, at(10, 19), refresh=False)
    assert out[0]["bot"]["order_id"] and out[0]["agent"]["error"].startswith("RuntimeError")
    st = DT.load_state(cfg.data_dir, DAY)
    assert "DTX|gap_and_go|buy" in st["fired"] and st["last_eval"]["DTX"]
    assert st["signals"][0]["bot"]["order_id"] == out[0]["bot"]["order_id"]
    assert DT.cycle(arena, pb, view, at(10, 20), refresh=False) == []  # not found again


def test_the_state_is_on_disk_before_anyone_acts(arena, cfg, mb, monkeypatch):
    pb = load_playbook(cfg, "asx_daytrader")
    _gap_market(mb, through=17)
    _universe(cfg)
    monkeypatch.setattr(DT, "_peer_moves", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    arena.broker.clock = Clock(at(10, 19))
    view = MarketView(mb, DAY, ReplayFeed(mb, 0), "^AXJO", 5, 3)
    with pytest.raises(OSError):
        DT.cycle(arena, pb, view, at(10, 19), refresh=False)
    assert "DTX|gap_and_go|buy" in DT.load_state(cfg.data_dir, DAY)["fired"]


# -- G5: the DATA line is the truth -------------------------------------------------------
def test_the_packet_says_live_ibkr_data_and_how_old_the_newest_bar_is(
    arena, cfg, mb, monkeypatch
):
    pb = load_playbook(cfg, "asx_daytrader")
    _gap_market(mb, through=20)
    _universe(cfg)
    seen = []
    monkeypatch.setattr(DT, "call_agent", lambda n, msg, **k: seen.append(msg) or _take())
    arena.broker.clock = Clock(at(10, 22))
    view = MarketView(mb, DAY, ReplayFeed(mb, 0), "^AXJO", 5, 3)  # no delay: labelled live
    DT.cycle(arena, pb, view, at(10, 22), refresh=False)
    assert "DATA: live data (IBKR); the newest DTX bar below ended at 10:21, 1 minute ago." in (
        seen[0])  # fmt: skip
    assert "20 minutes behind" not in seen[0] and "rehearsal" not in seen[0]
    late = MarketView(mb, DAY, ReplayFeed(mb, 20), "^AXJO", 5, 3)
    line = DT.data_line(late, "DTX", at(10, 42))
    assert line.startswith("DATA: delayed data - rehearsal until IBKR live prices; ")
    assert line.endswith("ended at 10:21, 21 minutes ago.")


def test_the_industry_peers_move_is_against_the_index_as_labelled(cfg, mb):
    for code, px in (("AAA", 1.0), ("BBB", 2.0)):
        for d in PRIOR:
            put(mb, code, d, flat_day(d, px, 1000))
        put(mb, code, DAY, frame(DAY, {(10, m): (px, px, px, px, 1000) for m in range(10)}))
    index_days(mb, frame(DAY, {(10, m): (7920, 7920, 7920, 7920, 0) for m in range(10)}))
    view = MarketView(mb, DAY, ReplayFeed(mb, 0), "^AXJO", 5, 3)
    rows, lasts = DT._peer_moves(view, ["AAA", "BBB"], at(10, 20))
    assert rows == {"AAA": pytest.approx(1.0), "BBB": pytest.approx(1.0)}  # flat, index -1%
    assert lasts == {"AAA": 1.0, "BBB": 2.0}


# -- G13: the agent's stop is filtered like the bot's ------------------------------------
def test_an_agent_stop_that_makes_the_trade_uneconomic_is_not_placed(
    arena, cfg, mb, monkeypatch
):
    pb = load_playbook(cfg, "asx_daytrader")
    _gap_market(mb, through=17)
    _universe(cfg)
    monkeypatch.setattr(DT, "call_agent", lambda *a, **k: _take(stop=1.065))
    arena.broker.clock = Clock(at(10, 19))
    view = MarketView(mb, DAY, ReplayFeed(mb, 0), "^AXJO", 5, 3)
    out = DT.cycle(arena, pb, view, at(10, 19), refresh=False)
    assert out[0]["bot"]["order_id"]  # the setup's own stop is economic
    # 1.065: 4,608 shares (the $5,000 cap), 1R = 0.005 x 4,608 = $23 < 2 x ~$23 of costs
    assert out[0]["agent"]["refused"].startswith("with the agent's stop, uneconomic: 1R $23")
    assert not arena.account(pb, "agent").orders


# -- A12: the history report, whenever the first cycle after 09:55 comes ------------------
class IBKRLike:
    """A feed that keeps its own prior sessions (history_source is not the minute cache)."""

    name = "ibkr-like"
    delayed = False
    label = "live data (IBKR)"

    def __init__(self, mb):
        self.mb = mb
        self.status_asked = 0

    def history_source(self):
        return SimpleNamespace(cached=self.mb.cached)

    def history_status(self, codes, day, sessions):
        self.status_asked += 1
        return {"complete": [c for c in codes if c != "BBB"], "missing": ["BBB"],
                "given_up": []}  # fmt: skip

    def ensure_history(self, code, day, sessions):
        return True

    def prepare(self, codes, day, sessions):
        return 0

    def fetch_one(self, code, now):
        pass

    def refresh(self, codes, now):
        return []

    def bars(self, code, day, now):
        return visible(self.mb.cached(code, day), now, None, day_complete=True,
                       index=code.startswith("^"))  # fmt: skip


def test_the_history_report_runs_at_the_first_cycle_after_0955_even_after_10(
    cfg, mb, monkeypatch
):
    pb = load_playbook(cfg, "asx_daytrader")
    for code in ("AAA", "BBB"):
        for d in PRIOR:
            put(mb, code, d, flat_day(d, 1.0, 1000))
    index_days(mb, frame(DAY, {(10, m): (8000, 8000, 8000, 8000, 0) for m in range(10)}))
    monkeypatch.setattr(DT, "build_universe", lambda arena, pb: (["AAA", "BBB"], {}))
    feed = IBKRLike(mb)
    view = MarketView(mb, DAY, feed, "^AXJO", 5, 3)
    arena = SimpleNamespace(cfg=cfg, broker=SimpleNamespace(clock=lambda: at(10, 5)),
                            short_universe=set())  # fmt: skip
    # the last pre-open cycle was at 09:50; the next comes at 10:05
    DT.cycle(arena, pb, view, at(10, 5), use_agent=False, refresh=False)
    st = DT.load_state(cfg.data_dir, DAY)
    assert st["history"]["missing"] == ["BBB"] and st["universe"] == ["AAA"]
    assert DT._DAY["codes"] == ["AAA"]
    assert [r["missing"] for r in EventLog(cfg.data_dir).read("ibkr_history")] == [["BBB"]]
    DT.cycle(arena, pb, view, at(10, 6), use_agent=False, refresh=False)
    DT._PRE.clear()  # a restart: the day's state remembers the report was made
    DT.cycle(arena, pb, view, at(10, 7), use_agent=False, refresh=False)
    assert feed.status_asked == 1


def test_a_replay_or_yahoo_feed_never_reports_history(cfg, mb, monkeypatch):
    monkeypatch.setattr(DT, "history_deadline_report", lambda *a: pytest.fail("reported"))
    pb = load_playbook(cfg, "asx_daytrader")
    arena = SimpleNamespace(cfg=cfg)
    view = MarketView(mb, DAY, ReplayFeed(mb, 0), "^AXJO", 5, 3)
    assert DT._history_report_once(arena, pb, view, at(10, 5)) is False
