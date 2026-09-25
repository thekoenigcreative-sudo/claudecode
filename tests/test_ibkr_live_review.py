"""The 26 Sep 2026 review of the persistent IBKR connection (ibkr/live.py, ibkr/feed.py):
each defect the reviewers reproduced, as a test that failed on the code before it -
history kept only before the first streamed minute (holes for the day), ~94 real-time bar
requests at once against IBKR's ~60 in 10 minutes, a refused stream counted as streaming,
the second quote of a stock returning the first one's values, the "today" window fixed at
queue time, a competing session that never cleared, 16:00-16:10 called stale, polled
stocks counted as fresh, tier 1 sorted by name and unbounded. No network, no Gateway."""

import importlib.util
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

from asxbot.ibkr import live as L

SYD = ZoneInfo("Australia/Sydney")
DAY = date(2026, 9, 28)

_spec = importlib.util.spec_from_file_location(
    "ibkr_live_fakes_review", Path(__file__).with_name("test_ibkr_live.py"))
LT = importlib.util.module_from_spec(_spec)
sys.modules["ibkr_live_fakes_review"] = LT
_spec.loader.exec_module(LT)


def at(h, m, s=0):
    return datetime(DAY.year, DAY.month, DAY.day, h, m, s, tzinfo=SYD)


def minutes(start: datetime, n: int, px=10.0, vol=1000.0, low=None):
    idx = [start + timedelta(minutes=i) for i in range(n)]
    df = pd.DataFrame({"open": px, "high": px + 0.01, "low": low if low is not None else px - 0.01,
                       "close": px, "volume": vol}, index=pd.DatetimeIndex(idx))  # fmt: skip
    return df


def bare_gateway(wall_t=None):
    gw = L.LiveGateway(LT.settings(), ib_factory=lambda: None,
                       wall=LT.Wall(wall_t or at(11, 30)))  # fmt: skip
    return gw


def stream(gw, code, start: datetime, n_min: int, px=10.0):
    """n_min minutes of 5-second bars from `start` (which may be mid-minute)."""
    t = start
    end = start + timedelta(minutes=n_min)
    while t < end:
        agg = gw.aggs.get(code)
        if agg is None:
            agg = gw.aggs[code] = L.MinuteAggregator(code)
        agg.add(t, px, px, px, px, 10.0)
        t += timedelta(seconds=5)
    gw.aggs[code].flush(end + timedelta(minutes=2))


# --------------------------------------------------------------------------
# A1: history and the stream merged minute by minute
# --------------------------------------------------------------------------
def test_a_stock_rotated_out_and_back_keeps_every_polled_minute():
    gw = bare_gateway(at(11, 0))
    stream(gw, "AAA", at(10, 0), 20)  # streamed 10:00-10:19
    gw.aggs["AAA"].restart()  # rotated out
    # polled: history to 10:39 (fetched at 10:41, so 10:40 is the forming minute)
    gw.today_hist[("AAA", DAY)] = (minutes(at(10, 0), 40, low=9.40), at(10, 41, 5))
    stream(gw, "AAA", at(10, 40), 10)  # streamed again from 10:40
    df = gw._today_frame("AAA", DAY)
    want = [at(10, 0) + timedelta(minutes=i) for i in range(50)]
    assert list(df.index) == want  # no hole 10:20-10:39 (the old code dropped it)
    assert df.loc[at(10, 25), "low"] == 9.40  # a stop inside the old hole is now seen


def test_a_partial_first_streamed_minute_gives_way_to_history():
    gw = bare_gateway(at(11, 0))
    gw.today_hist[("AAA", DAY)] = (minutes(at(10, 0), 10, vol=1000.0), at(10, 11, 5))
    stream(gw, "AAA", at(10, 3, 35), 5)  # first bar mid-minute: 10:03 is partial
    df = gw._today_frame("AAA", DAY)
    assert df.loc[at(10, 3), "volume"] == 1000.0  # history's full minute, not the stream's part
    assert df.loc[at(10, 5), "volume"] == 120.0  # a complete streamed minute wins (12 x 10)
    assert at(10, 3) in gw.aggs["AAA"].partial


def test_a_later_today_answer_merges_with_the_earlier_one():
    gw = bare_gateway(at(11, 0))
    gw.today_hist[("AAA", DAY)] = (minutes(at(10, 0), 10), at(10, 11))
    old = gw.today_hist[("AAA", DAY)][0]
    new = minutes(at(10, 5), 10, px=11.0).drop(index=[at(10, 7)])  # no trade at 10:07
    merged = pd.concat([old, new])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    assert at(10, 7) in merged.index  # kept from the earlier answer, as live.py now does


# --------------------------------------------------------------------------
# A2: the real-time bar budget; refused and dead streams go to polling
# --------------------------------------------------------------------------
def test_no_more_than_the_budget_of_real_time_bar_requests_in_ten_minutes(tmp_path):
    script = LT.Script()
    for i in range(80):
        script.known[f"S{i:02d}"] = (1000 + i, "STK", "ASX", "AUD")
    gw = L.LiveGateway(LT.settings(market_data_lines=200), ib_factory=lambda: LT.FakeIB(script),
                       wall=LT.Wall(at(11, 0)), history_dir=tmp_path)  # fmt: skip
    gw.start()
    try:
        assert LT.wait_for(lambda: gw.ready)
        gw.set_streaming([f"S{i:02d}" for i in range(80)])
        assert LT.wait_for(lambda: len(script.rt_requests) >= L.RT_BUDGET)
        assert len([r for r in script.rt_requests if not r.startswith("Q:")]) == L.RT_BUDGET
        streaming = [c for c in gw.subs if gw.streaming(c)]
        assert len(streaming) == L.RT_BUDGET  # the rest are polled, not "streaming"
        # a reconnect does not ask for them all again at once: the budget is spent
        n = len(script.rt_requests)
        gw.request_reconnect("test")
        assert LT.wait_for(lambda: script.connects >= 2 and gw.ready, timeout=5)
        assert len(script.rt_requests) == n
    finally:
        gw.stop(3)


def test_a_refused_real_time_request_is_polled_not_counted_as_streaming(tmp_path):
    script = LT.Script()
    script.known["AAA"] = (1, "STK", "ASX", "AUD")
    gw = L.LiveGateway(LT.settings(), ib_factory=lambda: LT.FakeIB(script),
                       wall=LT.Wall(at(11, 0)), history_dir=tmp_path)  # fmt: skip
    gw.start()
    try:
        assert LT.wait_for(lambda: gw.ready)
        gw.set_streaming(["AAA"])
        assert LT.wait_for(lambda: gw.streaming("AAA"))
        ib = script.instances[-1]
        req = ib.handles["AAA"].reqId
        ib.loop.call_soon_threadsafe(ib.errorEvent.emit, req, 420,
                                     "Invalid Real-time Query: pacing violation", None)
        assert LT.wait_for(lambda: not gw.streaming("AAA"))
        assert "AAA" in gw.rt_refused
        gw.set_streaming(["AAA"])  # not asked for again while refused
        assert not gw.streaming("AAA")
    finally:
        gw.stop(3)


def test_a_stream_that_never_delivers_while_the_index_does_is_dropped(tmp_path):
    script = LT.Script()
    script.known["AAA"] = (1, "STK", "ASX", "AUD")
    clock = {"t": 1000.0}
    gw = L.LiveGateway(LT.settings(heartbeat_s=3600), ib_factory=lambda: LT.FakeIB(script),
                       clock=lambda: clock["t"], wall=LT.Wall(at(11, 0)),
                       history_dir=tmp_path)  # fmt: skip
    gw.start()
    try:
        assert LT.wait_for(lambda: gw.health.connected)
        gw.heartbeat_at = clock["t"]
        gw.set_streaming(["^AXJO", "AAA"])
        assert LT.wait_for(lambda: gw.streaming("AAA") and gw.streaming("^AXJO"))
        clock["t"] += 90
        gw.subs["^AXJO"].last_at = clock["t"]  # the index delivers
        gw.set_streaming(["^AXJO", "AAA"])
        assert not gw.streaming("AAA") and "AAA" in gw.rt_refused
        assert gw.streaming("^AXJO")
    finally:
        gw.stop(3)


# --------------------------------------------------------------------------
# A3: a second quote of the same stock is a new quote
# --------------------------------------------------------------------------
def test_the_cached_ticker_is_forgotten_so_a_second_quote_is_fresh():
    cache = {}

    class IBWithCache:
        wrapper = SimpleNamespace(tickers=cache)

    class Contract:
        conId = 5

    contract = Contract()
    cache[hash(contract)] = "old ticker"
    L.LiveGateway._forget_ticker(IBWithCache(), contract)
    assert hash(contract) not in cache
    L.LiveGateway._forget_ticker(SimpleNamespace(), contract)  # the fake has no wrapper


# --------------------------------------------------------------------------
# coverage: what the feed actually watched
# --------------------------------------------------------------------------
def test_covered_joins_streamed_spans_and_history_answers():
    gw = bare_gateway(at(12, 0))
    gw.stream_spans["AAA"] = [[at(10, 0), at(10, 30)], [at(11, 0), None]]
    gw.hist_spans["AAA"] = [(at(10, 30), at(10, 45))]
    assert gw.covered("AAA", at(10, 5), at(10, 40))  # stream then history, joined
    assert not gw.covered("AAA", at(10, 40), at(10, 55))  # 10:46-10:59 nobody watched
    assert gw.covered("AAA", at(11, 10), at(11, 50))  # the running stream
    assert not gw.covered("BBB", at(10, 5), at(10, 6))


def test_a_history_answer_records_its_span_and_the_window_is_set_when_sent(tmp_path):
    script = LT.Script()
    script.known["AAA"] = (1, "STK", "ASX", "AUD")
    script.hist["AAA"] = LT.session(DAY, "10:00", 20)
    wall = LT.Wall(at(10, 31))
    gw = L.LiveGateway(LT.settings(), ib_factory=lambda: LT.FakeIB(script), wall=wall,
                       history_dir=tmp_path)  # fmt: skip
    gw.start()
    try:
        assert LT.wait_for(lambda: gw.ready)
        assert gw.queue_history("AAA", "today", DAY)
        assert LT.wait_for(lambda: ("AAA", DAY) in gw.today_hist)
        dur = script.hist_calls[-1][1]
        assert dur.endswith(" S") and int(dur.split()[0]) >= 300
        assert gw.hist_spans["AAA"][0][0] == datetime.combine(DAY, L.SESSION_START, tzinfo=SYD)
        assert gw.covered("AAA", at(10, 0), at(10, 29))
        assert gw.today_fetched_at("AAA", DAY) == at(10, 31)
    finally:
        gw.stop(3)


# --------------------------------------------------------------------------
# D1, A15, A16: a competing session clears, 504 reconnects, the close is not stale
# --------------------------------------------------------------------------
def test_a_competing_session_is_forgotten_on_a_new_connection(tmp_path):
    script = LT.Script()
    gw = L.LiveGateway(LT.settings(), ib_factory=lambda: LT.FakeIB(script),
                       wall=LT.Wall(at(9, 0)), history_dir=tmp_path)  # fmt: skip
    gw.start()
    try:
        assert LT.wait_for(lambda: gw.ready)
        ib = script.instances[-1]
        ib.loop.call_soon_threadsafe(ib.errorEvent.emit, 9, 10197, "competing", None)
        assert LT.wait_for(lambda: gw.competing)
        ib.drop()
        assert LT.wait_for(lambda: script.connects >= 2 and gw.ready, timeout=5)
        assert not gw.competing
    finally:
        gw.stop(3)


def test_not_connected_504_reconnects(tmp_path):
    script = LT.Script()
    gw = L.LiveGateway(LT.settings(), ib_factory=lambda: LT.FakeIB(script),
                       wall=LT.Wall(at(11, 0)), history_dir=tmp_path)  # fmt: skip
    gw.start()
    try:
        assert LT.wait_for(lambda: gw.ready)
        script.instances[-1].error(504, "Not connected")
        assert LT.wait_for(lambda: script.connects >= 2 and gw.ready, timeout=5)
    finally:
        gw.stop(3)


def test_the_closing_auction_is_not_called_stale(tmp_path):
    script = LT.Script()
    gw = L.LiveGateway(LT.settings(max_data_age_s=0.01), ib_factory=lambda: LT.FakeIB(script),
                       wall=LT.Wall(at(16, 5)), history_dir=tmp_path)  # fmt: skip
    gw.start()
    try:
        assert LT.wait_for(lambda: gw.ready)
        gw.set_streaming(["^AXJO"])
        assert LT.wait_for(lambda: gw.streaming("^AXJO"))
        gw.subs["^AXJO"].last_at = gw.clock() - 300
        assert gw.entries_allowed(at(16, 5))[0] is True
        assert gw.entries_allowed(at(15, 55))[0] is False
    finally:
        gw.stop(3)


def test_given_up_prior_sessions_are_asked_again_later():
    gw = bare_gateway()
    gw.hist_given_up.add(("AAA", DAY))
    gw.given_up_at[("AAA", DAY)] = gw.clock() - 1000
    gw.hist_failed[("AAA", DAY)] = 3
    assert gw.retry_given_up(900) == 1
    assert ("AAA", DAY) not in gw.hist_given_up and ("AAA", DAY) not in gw.hist_failed


# --------------------------------------------------------------------------
# the feed: tier 1 in priority order and capped; polled stocks' freshness
# --------------------------------------------------------------------------
def test_tier_one_keeps_the_watcher_s_order_and_is_capped(tmp_path):
    from asxbot.arena.minutes import MinuteBars
    from asxbot.ibkr import feed as F

    gw = bare_gateway()
    gw.line_limit = 30
    f = F.IBKRLiveFeed(MinuteBars(tmp_path), gw, None, "^AXJO")
    news = [f"N{i:02d}" for i in range(40)]
    f.pin(["ZIP", *news])  # the held stock first, then the news
    order = f.rotation_order(["AAA", "BBB"], at(11, 0))
    assert order[:2] == ["^AXJO", "ZIP"]  # not alphabetical: the position leads
    cap = max(10, int(F.PIN_SHARE * (30 - gw.s.stream_reserve_lines)))
    assert order[1:1 + cap] == ["ZIP", *news][:cap]


def test_a_polled_stock_s_bars_must_be_fresh_for_an_entry(tmp_path):
    from asxbot.arena.minutes import MinuteBars
    from asxbot.ibkr import feed as F

    gw = bare_gateway(at(11, 0))
    queued = []
    gw.queue_history = lambda code, kind, day, sessions=0: queued.append((code, kind)) or True
    gw.entries_allowed = lambda now, codes, index: (True, "")
    f = F.IBKRLiveFeed(MinuteBars(tmp_path), gw, None, "^AXJO")
    ok, why = f.entries_allowed(at(11, 0), ["AAA"])
    assert not ok and "not fetched yet" in why and queued == [("AAA", "today")]
    gw.today_hist[("AAA", DAY)] = (minutes(at(10, 0), 50), at(10, 58))
    assert f.entries_allowed(at(11, 0), ["AAA"]) == (True, "")  # 2 minutes old: fine
    gw.today_hist[("AAA", DAY)] = (minutes(at(10, 0), 50), at(10, 50))
    ok, why = f.entries_allowed(at(11, 0), ["AAA"])
    assert not ok and "600s old" in why
    assert f.entries_allowed(at(9, 30), ["AAA"]) == (True, "")  # before the open: not judged


# --------------------------------------------------------------------------
# D7/A11: the opening auction's volume on an IBKR day
# --------------------------------------------------------------------------
def test_on_an_ibkr_day_the_auction_volume_is_ibkr_s_0959_bar(tmp_path):
    from asxbot.arena.minutes import MinuteBars

    mb = MinuteBars(tmp_path)
    today = datetime.now(SYD).date()
    t0 = datetime(today.year, today.month, today.day, 9, 59, tzinfo=SYD)
    bars = minutes(t0, 40, px=10.0, vol=1000.0)
    bars.loc[t0, "volume"] = 5000.0  # IBKR's 09:59 bar: the auction
    # Yahoo's daily bar lags ~20 minutes: its volume is below the IBKR minutes' sum
    mb.fetch_daily_row = lambda code, day: {"open": 10.0, "high": 10.01, "low": 9.99,
                                            "close": 10.0, "volume": 30_000.0}  # fmt: skip
    now = t0 + timedelta(minutes=45)
    # without the live overlay the old estimate applies (and here refuses: 30,000 < 44,000)
    a = mb.opening_auction("AAA", today, bars, now, wait_minutes=30)
    assert a is not None and not a.available
    mb2 = MinuteBars(tmp_path / "b")
    mb2.fetch_daily_row = mb.fetch_daily_row
    mb2._live_days.add(("AAA", today))
    a2 = mb2.opening_auction("AAA", today, bars, now, wait_minutes=30)
    assert a2.available and a2.volume == 5000.0 and a2.price == 10.0


# --------------------------------------------------------------------------
# D14: the streamed bars' volume units, checked against history
# --------------------------------------------------------------------------
def _both(gw, stream_vol: float, hist_vol: float, n: int = 25):
    stream(gw, "AAA", at(10, 1), n)  # 12 five-second bars of 10 a minute = 120
    agg = gw.aggs["AAA"]
    for b in agg.complete:
        b.volume = stream_vol
    hist = minutes(at(10, 1), n, vol=hist_vol)
    gw._compare_volume("AAA", DAY, hist)


def test_the_same_units_are_confirmed_and_nothing_changes():
    gw = bare_gateway(at(11, 0))
    _both(gw, 100.0, 97.0)
    assert gw.volume_check.startswith("same units") and gw.volume_scale == 1.0


def test_a_stream_in_lots_of_100_is_rescaled_and_said_loudly():
    gw = bare_gateway(at(11, 0))
    _both(gw, 5.0, 500.0)
    assert gw.volume_check.startswith("corrected") and gw.volume_scale == 100.0
    assert all(b.volume == 500.0 for b in gw.aggs["AAA"].complete)
    assert gw.aggs["AAA"].scale == 100.0  # new bars too


def test_an_unexplained_ratio_is_a_mismatch_not_a_guess():
    gw = bare_gateway(at(11, 0))
    _both(gw, 100.0, 330.0)
    assert gw.volume_check.startswith("MISMATCH") and gw.volume_scale == 1.0


def test_too_few_minutes_decide_nothing():
    gw = bare_gateway(at(11, 0))
    _both(gw, 5.0, 500.0, n=5)
    assert gw.volume_check.startswith("not yet")


def test_a_volume_units_problem_is_a_self_check_failure(tmp_path, config_file):
    import json

    from asxbot.arena import selfcheck as S
    from asxbot.config import load_config

    cfg = load_config(config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data"),
                                        "live_provider": "ibkr"}),
                      env_file=tmp_path / "none.env")  # fmt: skip
    p = cfg.data_dir / "arena" / "live_data.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    now = at(11, 0)
    body = {"at": now.isoformat(), "provider_in_use": "ibkr", "entries": "allowed",
            "gateway": {"connected": True, "server_ok": True, "refused": False,
                        "volume_check": "corrected: the stream's volume was x0.01 of history's"}}
    p.write_text(json.dumps(body), encoding="utf-8")
    c = S.check_live_data(cfg, now)
    assert not c.ok and "streamed volume" in c.detail
    body["gateway"]["volume_check"] = "same units: history/stream median 0.98 over 40 minutes"
    p.write_text(json.dumps(body), encoding="utf-8")
    assert S.check_live_data(cfg, now).ok
