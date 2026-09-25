"""The persistent IBKR connection (ibkr/live.py), the streaming feed on it (ibkr/feed.py),
the broker's live minute overlay (arena/minutes.py) and the playbooks' pause on a feed that
is down or stale - all against a fake IB that behaves as ib_async does where it matters,
and misbehaves on demand: refused connections, dropped sockets, lost and restored links,
missed heartbeats, pacing violations, history that never comes, the line limit.

The chaos cases Rick asked for (25 Sep 2026): Gateway killed mid-stream, the socket dropped,
1100 lost then restored, a pacing violation, partial history. Each recovers without a
restart and without losing a subscription. No network, no Gateway."""

import asyncio
import json
import threading
import time
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from asxbot.arena.intraday import MarketView, YahooDelayedFeed, entries_allowed
from asxbot.arena.minutes import MinuteBars
from asxbot.config import load_config
from asxbot.ibkr import feed as F
from asxbot.ibkr import live as L
from asxbot.ibkr.gateway import GatewaySettings

SYD = ZoneInfo("Australia/Sydney")
DAY = date(2026, 9, 28)  # a Monday, a trading day
NAN = float("nan")
IN_HOURS = datetime(2026, 9, 28, 11, 0, tzinfo=SYD)
PRE_OPEN = datetime(2026, 9, 28, 8, 0, tzinfo=SYD)


def at(h, m, s=0, day=DAY):
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=SYD)


# --------------------------------------------------------------------------
# the fake IB: events fire on the connection's own loop, as ib_async's do
# --------------------------------------------------------------------------
class Ev:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, h):
        self.handlers.append(h)
        return self

    def emit(self, *a):
        for h in list(self.handlers):
            h(*a)


class Handle:
    def __init__(self, req_id, contract):
        self.reqId = req_id
        self.contract = contract
        self.updateEvent = Ev()
        self.bars = []

    def __len__(self):
        return len(self.bars)

    def __getitem__(self, i):
        return self.bars[i]

    def __delitem__(self, i):
        del self.bars[i]

    def append(self, b):
        self.bars.append(b)


class FakeTicker(SimpleNamespace):
    def __init__(self, **kw):
        base = dict(bid=NAN, ask=NAN, bidSize=NAN, askSize=NAN, last=NAN, lastSize=NAN,
                    open=NAN, close=NAN, volume=NAN, halted=NAN, auctionPrice=NAN,
                    auctionVolume=NAN, marketDataType=1, time=None)  # fmt: skip
        base.update(kw)
        super().__init__(**base)


KNOWN = {"BHP": (4036812, "STK", "ASX", "AUD"), "XJO": (46610746, "IND", "", "AUD")}


class Script:
    """What the fake does, shared across the connections one gateway makes."""

    def __init__(self):
        self.refuse = 0  # connections to refuse before accepting
        self.on_connect = []  # (code, msg) emitted during the handshake
        self.connects = 0
        self.instances = []
        self.hist = {}  # code -> list of bars, or an Exception to raise, or "hang"
        self.hist_calls = []
        self.hist_delay = 0.0
        self.heartbeat = "ok"  # ok | hang | raise
        self.rt_requests = []
        self.rt_cancels = []
        self.quotes = {}
        self.known = dict(KNOWN)
        self.max_lines = None  # emit 101 when more than this many subscriptions
        self.mdt = []


def hbar(ts: datetime, px: float, vol: float):
    return SimpleNamespace(date=ts.astimezone(UTC), open=px, high=px + 0.01, low=px - 0.01,
                           close=px, volume=vol)  # fmt: skip


def session(day: date, start="10:00", n=5, px=10.0, vol=1000.0):
    h, m = (int(x) for x in start.split(":"))
    t0 = datetime.combine(day, datetime.min.time(), tzinfo=SYD).replace(hour=h, minute=m)
    return [hbar(t0 + timedelta(minutes=i), px + 0.01 * i, vol) for i in range(n)]


class FakeIB:
    def __init__(self, script: Script):
        self.s = script
        self.loop = asyncio.get_running_loop()
        self.errorEvent = Ev()
        self.disconnectedEvent = Ev()
        self.timeoutEvent = Ev()
        self.connected = False
        self.handles = {}
        self.next_id = 100
        self.placed = 0
        self.client = SimpleNamespace(placeOrder=self.placeOrder,
                                      conn=SimpleNamespace(transport=None))  # fmt: skip
        self.mdt_calls = []
        script.instances.append(self)

    def placeOrder(self, *a):
        self.placed += 1

    async def connectAsync(self, host, port, clientId, timeout, readonly, fetchFields):
        self.s.connects += 1
        self.connect_args = dict(host=host, port=port, clientId=clientId, readonly=readonly)
        if self.s.refuse > 0:
            self.s.refuse -= 1
            raise ConnectionRefusedError(10061, "refused")
        self.connected = True
        for code, msg in self.s.on_connect:
            self.errorEvent.emit(-1, code, msg, None)
        return self

    def isConnected(self):
        return self.connected

    def disconnect(self):
        self.connected = False

    def setTimeout(self, t):
        self.timeout = t

    def reqMarketDataType(self, t):
        self.mdt_calls.append(t)
        self.s.mdt.append(t)

    async def qualifyContractsAsync(self, *contracts):
        out = []
        for c in contracts:
            k = self.s.known.get(c.symbol)
            if k is None:
                out.append(None)
                continue
            c.conId, c.secType, c.primaryExchange, c.currency = k
            out.append(c)
        return out

    def reqCurrentTimeAsync(self):
        fut = self.loop.create_future()
        if self.s.heartbeat == "ok" and self.connected:
            fut.set_result(datetime.now(UTC))
        elif self.s.heartbeat == "raise" or not self.connected:
            fut.set_exception(ConnectionError("Not connected"))
        # "hang": never resolved; the gateway's timeout catches it
        return fut

    async def reqHistoricalDataAsync(self, contract, end, dur, size, what, rth, formatDate=1,
                                     timeout=0):  # fmt: skip
        self.s.hist_calls.append((contract.symbol, dur, end))
        if self.s.hist_delay:
            await asyncio.sleep(self.s.hist_delay)
        got = self.s.hist.get(contract.symbol, [])
        if isinstance(got, Exception):
            raise got
        if got == "hang":
            await asyncio.sleep(3600)
        if got == "pace":
            self.errorEvent.emit(self.next_id, 162, "Historical Market Data Service error "
                                 "message:API historical data query cancelled: pacing "
                                 "violation", contract)  # fmt: skip
            raise TimeoutError()
        return list(got)

    def reqRealTimeBars(self, contract, size, what, rth, options=None):
        self.next_id += 1
        h = Handle(self.next_id, contract)
        self.handles[contract.symbol] = h
        self.s.rt_requests.append(contract.symbol)
        if self.s.max_lines is not None and len(self.handles) > self.s.max_lines:
            self.errorEvent.emit(self.next_id, 101, "Max number of tickers has been reached",
                                 contract)  # fmt: skip
        return h

    def cancelRealTimeBars(self, h):
        self.s.rt_cancels.append(h.contract.symbol)
        self.handles.pop(h.contract.symbol, None)

    def reqMktData(self, contract, generic="", *a):
        self.s.rt_requests.append("Q:" + contract.symbol)
        return self.s.quotes.get(contract.symbol, FakeTicker(marketDataType=None))

    def cancelMktData(self, contract):
        pass

    # -- what the tests do to it, from their own thread ------------------------
    def push_bar(self, code: str, ts: datetime, px: float, vol: float):
        h = self.handles.get(code)
        assert h is not None, f"{code} is not subscribed"
        b = SimpleNamespace(time=ts, open_=px, high=px + 0.01, low=px - 0.01, close=px,
                            volume=vol)  # fmt: skip

        def go():
            h.append(b)
            h.updateEvent.emit(h, True)

        self.loop.call_soon_threadsafe(go)

    def error(self, code: int, msg: str):
        self.loop.call_soon_threadsafe(self.errorEvent.emit, -1, code, msg, None)

    def drop(self):
        """The socket goes: as a killed Gateway or a broken network looks to ib_async."""

        def go():
            self.connected = False
            self.disconnectedEvent.emit()

        self.loop.call_soon_threadsafe(go)


def settings(**kw) -> GatewaySettings:
    base = dict(heartbeat_s=0.05, heartbeat_timeout_s=0.05, heartbeat_misses=2,
                connect_timeout_s=1.0, request_timeout_s=1.0, reconnect_backoff_max_s=0.05,
                min_dwell_s=0.0, history_min_interval_s=0.0, history_concurrency=2,
                max_data_age_s=60.0, stream_reserve_lines=2, market_data_lines=100,
                poll_per_cycle=3, quote_wait_s=0.2)  # fmt: skip
    base.update(kw)
    return GatewaySettings(**base)


class Wall:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self):
        return self.t


def wait_for(cond, timeout=3.0, every=0.01) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(every)
    return cond()


@pytest.fixture
def live(tmp_path):
    made = []

    def make(script=None, wall=None, **kw):
        script = script or Script()
        w = wall or Wall(IN_HOURS)
        gw = L.LiveGateway(settings(**kw), ib_factory=lambda: FakeIB(script), wall=w,
                           history_dir=tmp_path / "hist")  # fmt: skip
        made.append(gw)
        gw.start()
        return gw, script, w

    yield make
    for gw in made:
        gw.stop(timeout=3)


# --------------------------------------------------------------------------
# the connection
# --------------------------------------------------------------------------
def test_connects_read_only_on_its_own_thread_with_orders_disabled(live):
    gw, script, _ = live()
    assert wait_for(lambda: gw.ready)
    ib = script.instances[-1]
    assert ib.connect_args == dict(host="127.0.0.1", port=4001, clientId=41, readonly=True)
    assert threading.current_thread() is not gw._thread and gw._thread.is_alive()
    with pytest.raises(PermissionError):
        ib.placeOrder()
    with pytest.raises(PermissionError):
        ib.client.placeOrder()
    assert ib.placed == 0
    assert script.mdt[-1] == 1  # market hours: real-time asked for


def test_a_refused_gateway_is_retried_with_backoff_until_it_answers(live):
    script = Script()
    script.refuse = 3
    gw, script, _ = live(script)
    assert wait_for(lambda: gw.ready, timeout=5)
    assert script.connects == 4 and gw.connect_failures == 3
    assert gw.health.refused is False


def test_gateway_cut_off_from_ibkr_is_not_ready_until_the_link_comes_back(live):
    script = Script()
    script.on_connect = [(2110, "Connectivity between Trader Workstation and server is broken.")]
    gw, script, _ = live(script)
    assert wait_for(lambda: gw.health.connected)
    assert not gw.ready and not gw.health.server_ok
    view = SimpleNamespace(feed=SimpleNamespace(entries_allowed=gw.entries_allowed))
    assert entries_allowed(view, IN_HOURS)[0] is False
    script.instances[-1].error(1102, "Connectivity between IB and TWS has been restored - "
                               "data maintained.")  # fmt: skip
    assert wait_for(lambda: gw.ready)


# --------------------------------------------------------------------------
# the heartbeat and the chaos cases
# --------------------------------------------------------------------------
def _subscribe(gw, *codes):
    gw.set_streaming(list(codes))
    assert wait_for(lambda: all(gw.streaming(c) for c in codes)), gw.status()


def test_missed_heartbeats_reconnect_and_resubscribe(live):
    gw, script, _ = live()
    script.known["AAA"] = (1, "STK", "ASX", "AUD")
    assert wait_for(lambda: gw.ready)
    _subscribe(gw, "AAA", "^AXJO")
    script.heartbeat = "hang"
    assert wait_for(lambda: gw.reconnects >= 1, timeout=5), gw.status()
    script.heartbeat = "ok"
    assert wait_for(lambda: gw.ready and script.connects >= 2, timeout=5)
    assert wait_for(lambda: script.rt_requests.count("AAA") >= 2 and
                    script.rt_requests.count("XJO") >= 2, timeout=5)  # fmt: skip
    assert sorted(gw.subs) == ["AAA", "^AXJO"]  # the registry survived the reconnect


def test_a_dropped_socket_reconnects_and_asks_for_the_minutes_it_missed(live):
    """Gateway killed mid-stream, or the network gone: the socket closes, nothing is lost."""
    gw, script, _ = live()
    script.known["AAA"] = (1, "STK", "ASX", "AUD")
    script.hist["AAA"] = session(DAY, "10:00", 3)
    assert wait_for(lambda: gw.ready)
    _subscribe(gw, "AAA")
    assert wait_for(lambda: len(script.hist_calls) >= 1)  # the catch-up on subscribing
    n0 = len(script.hist_calls)
    ib = script.instances[-1]
    ib.push_bar("AAA", at(10, 58, 0), 10.0, 100)
    ib.push_bar("AAA", at(10, 58, 5), 10.0, 100)
    ib.drop()
    assert wait_for(lambda: script.connects >= 2 and gw.ready, timeout=5), gw.status()
    assert wait_for(lambda: script.rt_requests.count("AAA") >= 2, timeout=5)
    assert wait_for(lambda: len(script.hist_calls) > n0, timeout=5)  # the gap is filled
    sym, dur, end = script.hist_calls[-1]
    assert sym == "AAA" and dur.endswith(" S") and not end  # today, up to now
    assert gw.reconnects == 1 and gw.streaming("AAA")


def test_connectivity_lost_pauses_entries_and_restored_data_lost_resubscribes(live):
    gw, script, _ = live()
    script.known["AAA"] = (1, "STK", "ASX", "AUD")
    assert wait_for(lambda: gw.ready)
    _subscribe(gw, "AAA")
    ib = script.instances[-1]
    ib.error(1100, "Connectivity between IB and TWS has been lost.")
    assert wait_for(lambda: not gw.ready)
    ok, why = gw.entries_allowed(IN_HOURS, ["AAA"])
    assert not ok and "lost 1100" in why
    n = script.rt_requests.count("AAA")
    ib.error(1101, "Connectivity between IB and TWS has been restored - data lost.")
    assert wait_for(lambda: gw.ready)
    assert wait_for(lambda: script.rt_requests.count("AAA") > n, timeout=3)  # re-subscribed
    assert script.connects == 1  # no reconnect was needed for this one


def test_a_pacing_violation_pauses_the_history_queue_and_the_request_is_tried_again(live):
    gw, script, _ = live()
    script.known["AAA"] = (1, "STK", "ASX", "AUD")
    script.hist["AAA"] = "pace"
    assert wait_for(lambda: gw.ready)
    gw.pacing_until = 0.0
    assert gw.queue_history("AAA", "today", DAY)
    assert wait_for(lambda: gw.health.pacing_hits >= 1, timeout=3)
    assert gw.pacing_until > gw.clock()  # paused for a while
    script.hist["AAA"] = session(DAY, "10:00", 2)
    gw.pacing_until = 0.0  # the pause is over (30 s in real life)
    assert wait_for(lambda: gw.bars_today("AAA", DAY) is not None, timeout=5), gw.status()
    assert len(script.hist_calls) >= 2  # the same job, tried again


def test_partial_history_is_given_up_after_three_asks_and_named(live):
    gw, script, _ = live(wall=Wall(PRE_OPEN))
    script.known["AAA"] = (1, "STK", "ASX", "AUD")
    script.known["BBB"] = (2, "STK", "ASX", "AUD")
    prior = []
    for d in (date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25)):
        prior += session(d, n=30, vol=500.0)
    script.hist["AAA"] = prior
    script.hist["BBB"] = []  # IBKR has nothing for it
    assert wait_for(lambda: gw.ready)
    feed = F.IBKRLiveFeed(MinuteBars(gw.history_dir.parent / "m"), gw)
    assert feed.prepare(["AAA", "BBB"], DAY, 3) == 2
    assert wait_for(lambda: gw.history_done("AAA", DAY), timeout=5)
    for _ in range(3):
        wait_for(lambda: not gw.hist_pending, timeout=5)
        feed.prepare(["BBB"], DAY, 3)
    assert wait_for(lambda: ("BBB", DAY) in gw.hist_given_up, timeout=5), gw.status()
    st = feed.history_status(["AAA", "BBB"], DAY, 3)
    assert st == {"complete": ["AAA"], "missing": [], "given_up": ["BBB"]}
    # cached on local disk, day by day, and read back through the feed
    assert (gw.history_dir / "AAA" / "2026-09-25.parquet").exists()
    assert len(feed.cached("AAA", date(2026, 9, 25))) == 30
    view = MarketView(MinuteBars(gw.history_dir.parent / "m"), DAY, feed, sessions=3,
                      min_sessions=3)  # fmt: skip
    assert view.prev_close("AAA") == pytest.approx(10.29)
    assert view.usual("AAA").iloc[5] == pytest.approx(5 * 500.0)


def test_the_line_limit_comes_from_ibkr_and_the_rotation_stays_inside_it(live):
    script = Script()
    script.max_lines = 4
    gw, script, _ = live(script, stream_reserve_lines=0)
    for i in range(8):
        script.known[f"S{i}"] = (10 + i, "STK", "ASX", "AUD")
    assert wait_for(lambda: gw.ready)
    gw.set_streaming([f"S{i}" for i in range(8)])
    assert wait_for(lambda: gw.line_limit_from_ibkr, timeout=3), gw.status()
    assert gw.line_limit == 4
    gw.set_streaming([f"S{i}" for i in range(8)])
    time.sleep(0.2)
    assert len([s for s in gw.subs.values() if s.handle is not None]) <= 4


def test_a_slot_is_held_for_its_dwell_then_rotated(live):
    gw, script, _ = live(min_dwell_s=0.3, stream_reserve_lines=0, market_data_lines=2)
    for c in ("AAA", "BBB", "CCC"):
        script.known[c] = (len(c), "STK", "ASX", "AUD")
    assert wait_for(lambda: gw.ready)
    _subscribe(gw, "AAA", "BBB")
    gw.set_streaming(["CCC", "AAA"])
    time.sleep(0.1)
    assert gw.streaming("BBB") and not gw.streaming("CCC")  # BBB keeps its slot for now
    time.sleep(0.3)
    gw.set_streaming(["CCC", "AAA"])
    assert wait_for(lambda: gw.streaming("CCC") and not gw.streaming("BBB"))
    assert "BBB" in script.rt_cancels


# --------------------------------------------------------------------------
# bars: 5-second bars into minutes, history behind them, staleness
# --------------------------------------------------------------------------
def test_five_second_bars_become_minutes_and_the_forming_one_is_held_back():
    agg = L.MinuteAggregator("AAA")
    for s in range(0, 60, 5):
        assert agg.add(at(10, 0, s), 10.0 + s / 1000, 10.1, 9.9, 10.0 + s / 1000, 100) is None
    done = agg.add(at(10, 1, 0), 11.0, 11.0, 11.0, 11.0, 5)
    assert done.ts == at(10, 0) and done.open == 10.0 and done.close == pytest.approx(10.055)
    assert done.high == 10.1 and done.low == 9.9 and done.volume == 1200
    assert agg.frame(DAY).index.tolist() == [at(10, 0)]
    assert agg.flush(at(10, 1, 30)) is None  # 10:01 is still forming
    assert agg.flush(at(10, 2, 13)).ts == at(10, 1)  # closed by time once it is over
    assert agg.add(at(10, 0, 30), 1, 1, 1, 1, 1) is None and agg.dropped == 1  # a late bar


def test_bars_today_joins_history_and_the_stream_and_a_decision_sees_final_minutes(live):
    gw, script, wall = live()
    script.known["AAA"] = (1, "STK", "ASX", "AUD")
    script.hist["AAA"] = session(DAY, "10:00", 58)  # 10:00 .. 10:57
    assert wait_for(lambda: gw.ready)
    wall.t = at(10, 57, 30)
    _subscribe(gw, "AAA")
    assert wait_for(lambda: gw.bars_today("AAA", DAY) is not None, timeout=5)
    df = gw.bars_today("AAA", DAY)
    assert df.index.max() == at(10, 55)  # fetched at 10:57: 10:56 (forming) held back
    ib = script.instances[-1]
    for s in range(0, 60, 5):
        ib.push_bar("AAA", at(10, 58, s), 20.0, 50)
    ib.push_bar("AAA", at(10, 59, 0), 21.0, 1)
    assert wait_for(lambda: at(10, 58) in gw.bars_today("AAA", DAY).index, timeout=3)
    df = gw.bars_today("AAA", DAY)
    assert df.loc[at(10, 58), "volume"] == 600 and df.loc[at(10, 58), "close"] == 20.0
    feed = F.IBKRLiveFeed(MinuteBars(gw.history_dir.parent / "m"), gw)
    seen = feed.bars("AAA", DAY, at(10, 59, 20))
    assert seen.index.max() == at(10, 58)  # the completed minute is final and visible
    assert gw.data_age_s("AAA") < 5


def test_stale_streamed_data_pauses_entries_in_market_hours_only(live):
    gw, script, wall = live(max_data_age_s=0.2)
    script.known["AAA"] = (1, "STK", "ASX", "AUD")
    assert wait_for(lambda: gw.ready)
    _subscribe(gw, "^AXJO", "AAA")
    ib = script.instances[-1]
    ib.push_bar("XJO", at(11, 0, 0), 8000, 0)
    ib.push_bar("AAA", at(11, 0, 0), 10, 5)
    assert wait_for(lambda: gw.data_age_s("AAA") is not None)
    assert gw.entries_allowed(IN_HOURS, ["AAA"]) == (True, "")
    time.sleep(0.4)
    ok, why = gw.entries_allowed(IN_HOURS, ["AAA"])
    assert not ok and "stale" in why and "the index" in why
    assert gw.entries_allowed(PRE_OPEN, ["AAA"]) == (True, "")  # outside hours: not judged
    ib.push_bar("XJO", at(11, 0, 5), 8000, 0)
    ib.push_bar("AAA", at(11, 0, 5), 10, 5)
    assert wait_for(lambda: gw.entries_allowed(IN_HOURS, ["AAA"])[0])


def test_a_quote_comes_through_and_the_line_is_given_back(live):
    gw, script, _ = live()
    script.quotes["BHP"] = FakeTicker(bid=45.1, ask=45.12, bidSize=500, askSize=800, last=45.11,
                                      lastSize=100, open=45.0, close=44.8, volume=1.2e6,
                                      halted=0, auctionPrice=45.0, auctionVolume=2e5)  # fmt: skip
    assert wait_for(lambda: gw.ready)
    q = gw.quote("BHP")
    assert q["bid"] == 45.1 and q["ask_size"] == 800 and q["auction_price"] == 45.0
    assert q["market_data_type"] == 1 and gw.health.market_data_type == 1
    assert gw.quote("ZZZ") is None  # unknown to IBKR


# --------------------------------------------------------------------------
# the feed: rotation, failover, pause, the status file
# --------------------------------------------------------------------------
class FakeYahoo:
    name = "yahoo_delayed"
    delayed = True
    label = "delayed data - rehearsal until IBKR live prices"

    def __init__(self, minutes):
        self.minutes = minutes
        self.refreshed = []
        self.frames = {}

    def refresh(self, codes, now):
        self.refreshed.append(list(codes))
        return list(codes)

    def fetch_one(self, code, now):
        self.refreshed.append([code])

    def bars(self, code, day, now):
        empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return self.frames.get(code, empty)

    def history_source(self):
        return self.minutes

    def ensure_history(self, code, day, sessions):
        return False

    def prepare(self, codes, day, sessions):
        return 0


def _failover(tmp_path, gw, now):
    from asxbot.log import EventLog

    minutes = MinuteBars(tmp_path / "data")
    ev = EventLog(tmp_path / "data")
    yahoo = FakeYahoo(minutes)
    return F.FailoverFeed(minutes, F.IBKRLiveFeed(minutes, gw, ev), yahoo, tmp_path / "data",
                          ev, now=now), ev, yahoo  # fmt: skip


def test_the_rotation_streams_the_index_the_pinned_and_the_interesting(live, tmp_path):
    gw, script, _ = live(stream_reserve_lines=0, market_data_lines=4, poll_per_cycle=2)
    for i in range(8):
        script.known[f"S{i}"] = (10 + i, "STK", "ASX", "AUD")
    assert wait_for(lambda: gw.ready)
    feed = F.IBKRLiveFeed(MinuteBars(tmp_path / "m"), gw)
    feed.pin(["S7"])
    feed.note_interest({"S3": 5.0, "S1": 2.0})
    codes = [f"S{i}" for i in range(8)]
    assert feed.rotation_order(codes, IN_HOURS)[:4] == ["^AXJO", "S7", "S3", "S1"]
    got = feed.refresh(codes, IN_HOURS)
    assert wait_for(lambda: gw.streaming("S3") and gw.streaming("S7") and gw.streaming("^AXJO"))
    assert not gw.streaming("S5")
    assert feed.rotation["streaming"] <= 4 and feed.rotation["polled"] == 2
    assert wait_for(lambda: len(script.hist_calls) >= 2, timeout=3)  # the polled ones
    assert set(got) >= {"^AXJO", "S7", "S3"}
    feed.refresh(codes, IN_HOURS + timedelta(seconds=30))
    assert feed.rotation["polled"] == 2  # the next two stalest, not the same two again


def test_the_feed_pauses_entries_while_ibkr_is_down_and_resumes_by_itself(live, tmp_path):
    gw, script, wall = live()
    script.known["BHP"] = KNOWN["BHP"]
    assert wait_for(lambda: gw.ready)
    feed, ev, yahoo = _failover(tmp_path, gw, IN_HOURS)
    assert feed.using_primary and not feed.paused and feed.entries_allowed(IN_HOURS)[0]
    st = F.read_status(tmp_path / "data")
    assert st["provider_in_use"] == "ibkr" and st["entries"] == "allowed"
    assert st["gateway"]["streaming"] == 0 and st["gateway"]["line_limit"] == 100

    ib = script.instances[-1]
    ib.error(1100, "Connectivity between IB and TWS has been lost.")
    assert wait_for(lambda: not gw.ready)
    feed.check(IN_HOURS + timedelta(seconds=10))
    assert not feed.using_primary and feed.paused and "lost 1100" in feed.pause_why
    assert feed.entries_allowed(IN_HOURS)[0] is False
    assert feed.refresh(["BHP"], IN_HOURS) == [] and not yahoo.refreshed  # nothing scanned on Yahoo
    st = F.read_status(tmp_path / "data")
    assert st["entries"] == "paused" and st["provider_in_use"] == "yfinance"
    yahoo.frames["BHP"] = pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
                                        "volume": [5.0]}, index=[at(10, 30)])  # fmt: skip
    assert len(feed.bars("BHP", DAY, IN_HOURS)) == 1  # gap-fill for marks and context only

    ib.error(1102, "Connectivity between IB and TWS has been restored - data maintained.")
    assert wait_for(lambda: gw.ready)
    feed.check(IN_HOURS + timedelta(minutes=1))
    assert feed.using_primary and not feed.paused
    events = [r["event"] for r in ev.read("live_data")]
    assert events == ["fallback", "paused", "restored", "resumed"]


def test_delayed_ibkr_data_in_market_hours_pauses_entries(live, tmp_path):
    gw, script, _ = live()
    script.quotes["BHP"] = FakeTicker(last=45.0, close=44.0, bid=1, ask=2, marketDataType=3)
    assert wait_for(lambda: gw.ready)
    feed, _, _ = _failover(tmp_path, gw, IN_HOURS)
    assert feed.paused and "delayed" in feed.pause_why and "BHP" in feed.pause_why
    assert feed.using_primary  # connected, but no entries on delayed prices


def test_one_config_value_switches_the_feed(tmp_path, monkeypatch, config_file):
    cfg = load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data"),
                          "live_provider": "yfinance"}),
        env_file=tmp_path / "none.env",
    )  # fmt: skip
    from asxbot.arena.intraday import live_provider, make_feed

    minutes = MinuteBars(tmp_path / "m")
    assert isinstance(make_feed(cfg, minutes), YahooDelayedFeed)
    cfg.raw["data"]["live_provider"] = "ibkr"
    script = Script()
    script.refuse = 10**6
    gw = L.LiveGateway(settings(), ib_factory=lambda: FakeIB(script), wall=Wall(IN_HOURS))
    monkeypatch.setattr(F, "shared_live", lambda c: gw)
    gw.start()
    try:
        feed = make_feed(cfg, minutes)
        assert isinstance(feed, F.FailoverFeed) and not feed.using_primary and feed.paused
    finally:
        gw.stop(3)
    cfg.raw["data"]["live_provider"] = "bloomberg"
    with pytest.raises(ValueError):
        live_provider(cfg)


# --------------------------------------------------------------------------
# the broker's live minute overlay
# --------------------------------------------------------------------------
class Overlay:
    def __init__(self, frames, ok=True):
        self.frames, self.ok = frames, ok

    def bars_today(self, code, day):
        return self.frames.get((code, day))

    def live_ok(self):
        return self.ok


def test_the_broker_works_from_live_minutes_and_treats_them_as_final(tmp_path, monkeypatch):
    mb = MinuteBars(tmp_path / "data")
    today = datetime.now(SYD).date()
    t0 = datetime.combine(today, datetime.min.time(), tzinfo=SYD).replace(hour=10)
    live = pd.DataFrame({"open": [1.0, 1.1], "high": [1.0, 1.1], "low": [1.0, 1.1],
                         "close": [1.0, 1.1], "volume": [10.0, 20.0]},
                        index=[t0, t0 + timedelta(minutes=1)])  # fmt: skip
    mb.set_live(Overlay({("AAA", today): live}))
    monkeypatch.setattr(mb, "_yahoo_fetch", lambda *a, **k: pytest.fail("Yahoo was asked"))
    df = mb.fetch("AAA", today)
    assert list(df["volume"]) == [10.0, 20.0]
    got = list(mb.final_bars("AAA", t0, t0 + timedelta(minutes=2, seconds=5), 0, 22, None))
    assert [ts for ts, _ in got] == [t0, t0 + timedelta(minutes=1)]  # the newest is final too


def test_with_the_stream_down_yahoos_later_bars_fill_in_behind_the_live_ones(tmp_path, monkeypatch):
    mb = MinuteBars(tmp_path / "data")
    today = datetime.now(SYD).date()
    t0 = datetime.combine(today, datetime.min.time(), tzinfo=SYD).replace(hour=10)
    live = pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
                         "volume": [10.0]}, index=[t0])  # fmt: skip
    yahoo = pd.DataFrame({"open": [9.0, 2.0], "high": [9.0, 2.0], "low": [9.0, 2.0],
                          "close": [9.0, 2.0], "volume": [99.0, 30.0]},
                         index=[t0, t0 + timedelta(minutes=1)])  # fmt: skip
    mb.set_live(Overlay({("AAA", today): live}, ok=False))
    monkeypatch.setattr(mb, "_yahoo_fetch", lambda *a, **k: yahoo)
    df = mb.fetch("AAA", today)
    assert list(df["close"]) == [1.0, 2.0]  # the live minute stands; Yahoo adds what follows


# --------------------------------------------------------------------------
# the playbooks pause, the self-check says so, Rick is told once
# --------------------------------------------------------------------------
def test_the_day_trader_does_not_scan_while_the_feed_is_paused(tmp_path, monkeypatch, config_file):
    from asxbot.arena import daytrader as DT
    from asxbot.arena.levels import load_playbook

    cfg = load_config(config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
                      env_file=tmp_path / "none.env")  # fmt: skip
    pb = load_playbook(cfg, "asx_daytrader")
    mb = MinuteBars(cfg.data_dir)

    class Feed:
        name = "x"
        delayed = False

        def entries_allowed(self, now, codes=()):
            return False, "lost 1100"

        def history_source(self):
            return mb

        def bars(self, code, day, now):
            idx = [at(10, 0), at(10, 1)]
            return pd.DataFrame({"open": [1, 1], "high": [1, 1], "low": [1, 1], "close": [1, 1],
                                 "volume": [0, 0]}, index=idx)  # fmt: skip

        def refresh(self, codes, now):
            pytest.fail("the scan refreshed the feed while paused")

        def fetch_one(self, code, now):
            pass

    view = MarketView(mb, DAY, Feed(), "^AXJO", 5, 3)
    arena = SimpleNamespace(cfg=cfg, broker=SimpleNamespace(clock=lambda: at(10, 5)),
                            short_universe=set())  # fmt: skip
    DT._DAY.clear()
    DT._DAY.update(day=DAY, data_dir=str(cfg.data_dir), codes=["AAA"], news=set())
    st = DT.load_state(cfg.data_dir, DAY)
    st["universe"] = ["AAA"]
    DT.save_state(cfg.data_dir, DAY, st)
    assert DT.cycle(arena, pb, view, at(10, 5), use_agent=False) == []
    assert DT._DAY.get("paused") == "lost 1100"
    from asxbot.log import EventLog

    scans = EventLog(cfg.data_dir).read("daytrader_scan")
    assert scans and scans[-1].get("paused") == "lost 1100"


def test_the_v2_rule_bot_waits_while_paused_and_records_the_miss_with_the_reason(
    tmp_path, config_file
):
    from asxbot.arena import v2_flow
    from asxbot.arena.levels import load_playbook
    from asxbot.arena.reaction_v2 import load_bot_state

    cfg = load_config(config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
                      env_file=tmp_path / "none.env")  # fmt: skip
    pb = load_playbook(cfg, "asx_announcements_v2")
    mb = MinuteBars(cfg.data_dir)

    class Feed:
        name = "x"
        delayed = False

        def entries_allowed(self, now, codes=()):
            return False, "stale: no bar from IBKR for the index for 90s"

        def history_source(self):
            return mb

        def bars(self, code, day, now):
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        def fetch_one(self, code, now):
            pass

    view = MarketView(mb, DAY, Feed(), "^AXJO", 5, 3)
    arena = SimpleNamespace(cfg=cfg, broker=SimpleNamespace(clock=lambda: at(10, 31)))
    assert v2_flow.v2_bot_cycle(arena, pb, view, at(10, 31), ann=pd.DataFrame()) == []
    st = load_bot_state(cfg.data_dir, DAY)
    assert st["status"] == "waiting" and st["why"].startswith("paused: stale")
    assert v2_flow.v2_bot_cycle(arena, pb, view, at(11, 16), ann=pd.DataFrame()) == []
    st = load_bot_state(cfg.data_dir, DAY)
    assert st["status"] == "missed" and "paused: stale" in st["why"]


def test_the_supervisor_check_tells_rick_once_when_gateway_needs_him_and_once_when_back(
    tmp_path, monkeypatch, config_file
):
    from asxbot.arena import selfcheck as S
    from asxbot.ibkr import supervisor as SV

    cfg = load_config(config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
                      env_file=tmp_path / "none.env")  # fmt: skip
    state = tmp_path / "supervisor.json"
    monkeypatch.setattr(SV, "SUPERVISOR_STATE", state)
    now = datetime(2026, 9, 28, 10, 0, tzinfo=SYD)
    state.write_text(json.dumps({
        "last_check": (now - timedelta(minutes=1)).isoformat(timespec="seconds"),
        "last_result": "RESTART: IB Gateway is not running | port closed; login stored",
        "outage": {"since": (now - timedelta(minutes=3)).isoformat(timespec="seconds"),
                   "notice": "phone", "notified_at": None},
    }), encoding="utf-8")  # fmt: skip
    c = S.check_gateway_supervisor(now)
    assert not c.ok and c.facts["login_needed"] and "approval on the phone" in c.detail

    sent = []
    arena = SimpleNamespace(cfg=cfg, broker=SimpleNamespace(notifier=SimpleNamespace(
        send=lambda text: sent.append(text) or True)))  # fmt: skip
    monkeypatch.setattr(S, "run_checks", lambda a, p, n: [S.check_gateway_supervisor(n)])
    S.report(arena, None, now)
    S.report(arena, None, now + timedelta(minutes=2))
    assert len(sent) == 1 and sent[0].startswith("IB Gateway needs you")  # once, one line
    state.write_text(json.dumps({
        "last_check": (now + timedelta(minutes=4)).isoformat(timespec="seconds"),
        "last_result": "NONE: healthy", "outage": None,
    }), encoding="utf-8")  # fmt: skip
    S.report(arena, None, now + timedelta(minutes=5))
    assert sent[-1] == S.BACK_LINE and sent.count(S.BACK_LINE) == 1
    S.report(arena, None, now + timedelta(minutes=6))
    assert sent.count(S.BACK_LINE) == 1
    # the task itself silent in its hours is a failure
    stale = {"last_check": (now - timedelta(minutes=20)).isoformat(), "last_result": "x",
             "outage": None}  # fmt: skip
    state.write_text(json.dumps(stale), encoding="utf-8")
    assert "has not checked for 20 minutes" in S.check_gateway_supervisor(now).detail


def test_the_live_data_check_reports_a_pause_in_market_hours(tmp_path, config_file):
    from asxbot.arena import selfcheck as S

    cfg = load_config(config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data"),
                                        "live_provider": "ibkr"}),
                      env_file=tmp_path / "none.env")  # fmt: skip
    now = datetime(2026, 9, 28, 11, 0, tzinfo=SYD)
    (cfg.data_dir / "arena").mkdir(parents=True)
    F.status_path(cfg.data_dir).write_text(json.dumps({
        "at": now.isoformat(timespec="seconds"), "provider_in_use": "ibkr",
        "entries": "paused", "paused_why": "stale: no bar from IBKR for the index for 75s",
        "gateway": {"connected": True, "server_ok": True, "refused": False},
    }), encoding="utf-8")  # fmt: skip
    c = S.check_live_data(cfg, now)
    assert not c.ok and "entries paused" in c.detail and c.facts["paused"]
