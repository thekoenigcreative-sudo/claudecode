"""The IBKR live data layer against a fake IB Gateway: connection, pacing, bars, quotes, the
Yahoo fallback, the live_data self-check and the evening report's per-decision labels.
No network, no Gateway."""

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from asxbot.arena import selfcheck as S
from asxbot.arena.intraday import (
    DELAYED_LABEL,
    MarketView,
    YahooDelayedFeed,
    live_provider,
    make_feed,
)
from asxbot.arena.minutes import MinuteBars
from asxbot.config import load_config
from asxbot.ibkr import feed as F
from asxbot.ibkr.gateway import Gateway, GatewaySettings, bars_frame, maybe_reconnect

SYD = ZoneInfo("Australia/Sydney")
DAY = date(2026, 9, 25)  # a Friday, a trading day
NAN = float("nan")


# --------------------------------------------------------------------------
# the fake Gateway
# --------------------------------------------------------------------------
class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Ev:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, h):
        self.handlers.append(h)
        return self

    def emit(self, *a):
        for h in self.handlers:
            h(*a)


def bar(ts: datetime, px: float, vol: float):
    return SimpleNamespace(date=ts.astimezone(UTC), open=px, high=px + 0.01, low=px - 0.01,
                           close=px, volume=vol)  # fmt: skip


def session(day: date, start="10:00", n=5, px=10.0, vol=1000.0):
    h, m = (int(x) for x in start.split(":"))
    t0 = datetime.combine(day, datetime.min.time(), tzinfo=SYD).replace(hour=h, minute=m)
    return [bar(t0 + timedelta(minutes=i), px + 0.01 * i, vol) for i in range(n)]


class FakeTicker(SimpleNamespace):
    def __init__(self, **kw):
        base = dict(bid=NAN, ask=NAN, bidSize=NAN, askSize=NAN, last=NAN, lastSize=NAN,
                    open=NAN, close=NAN, volume=NAN, halted=NAN, auctionPrice=NAN,
                    auctionVolume=NAN, marketDataType=1, time=None)  # fmt: skip
        base.update(kw)
        super().__init__(**base)


# What IBKR returns for a contract lookup: symbol -> (conId, secType, primaryExchange, currency)
KNOWN = {"BHP": (4036812, "STK", "ASX", "AUD"), "XJO": (46610746, "IND", "", "AUD")}


class FakeIB:
    """Behaves as ib_async does where it matters: a quote for a contract with no conId raises
    (the ValueError the 21:32 check on 24 Sep 2026 hit), and qualifying fills the conId in."""

    def __init__(self, clock, bars=None, quotes=None, refuse=False, on_connect=(), known=None):
        self.clock = clock
        self.bars = bars or {}
        self.quotes = quotes or {}
        self.known = dict(KNOWN) if known is None else known
        for sym in [*self.bars, *self.quotes]:  # any symbol a test serves is a known ASX stock
            self.known.setdefault(sym, (1000 + len(self.known), "STK", "ASX", "AUD"))
        self.qualified = []
        self.mdt_calls = []
        self.refuse = refuse
        self.on_connect = on_connect
        self.errorEvent = Ev()
        self.disconnectedEvent = Ev()
        self.connected = False
        self.requests = []
        self.cancelled = []
        self.connect_args = None
        self.mdt = None
        self.placed_calls = 0
        self.client = SimpleNamespace(placeOrder=self.placeOrder)

    def placeOrder(self, *a):  # would be the disaster; disable_orders must replace it
        self.placed_calls += 1

    def connect(self, host, port, clientId, timeout, readonly, fetchFields):
        self.connect_args = dict(host=host, port=port, clientId=clientId, readonly=readonly)
        if self.refuse:
            raise ConnectionRefusedError(10061, "refused")
        self.connected = True
        for code, msg in self.on_connect:
            self.errorEvent.emit(-1, code, msg, None)

    def isConnected(self):
        return self.connected

    def disconnect(self):
        self.connected = False

    def reqMarketDataType(self, t):
        self.mdt = t
        self.mdt_calls.append(t)

    async def qualifyContractsAsync(self, *contracts):
        out = []
        for c in contracts:
            self.qualified.append(c.symbol)
            k = self.known.get(c.symbol)
            if k is None:
                out.append(None)
                continue
            c.conId, c.secType, c.primaryExchange, c.currency = k
            out.append(c)
        return out

    @staticmethod
    def _need_con_id(contract):
        if not contract.conId:
            raise ValueError(f"Contract {contract} can't be hashed because no 'conId' value "
                             "exists. Qualify contract to populate 'conId'.")  # fmt: skip

    async def reqHistoricalDataAsync(self, contract, end, dur, size, what, rth, formatDate=1,
                                     timeout=0):  # fmt: skip
        self._need_con_id(contract)
        self.requests.append((contract.symbol, dur, end, size, what, rth, formatDate))
        return list(self.bars.get(contract.symbol, []))

    def run(self, aw):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(aw)
        finally:
            loop.close()

    def reqMktData(self, contract, generic=""):
        self._need_con_id(contract)
        return self.quotes.get(contract.symbol, FakeTicker(marketDataType=None))

    def cancelMktData(self, contract):
        self.cancelled.append(contract.symbol)

    def sleep(self, s):
        self.clock.t += s


EVENING = datetime(2026, 9, 25, 21, 0, tzinfo=SYD)


def gateway(clock=None, wall=None, **kw):
    clock = clock or Clock()
    settings = GatewaySettings(**kw.pop("settings", {}))
    fake = FakeIB(clock, **kw)
    gw = Gateway(settings, ib_factory=lambda: fake, clock=clock, wall=wall or (lambda: EVENING))
    return gw, fake


@pytest.fixture
def cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data"),
                          "live_provider": "ibkr"}),
        env_file=tmp_path / "none.env",
    )  # fmt: skip


# --------------------------------------------------------------------------
# the connection
# --------------------------------------------------------------------------
def test_connects_read_only_with_order_methods_replaced():
    gw, fake = gateway()
    assert gw.connect() and gw.ready
    assert fake.connect_args == dict(host="127.0.0.1", port=4001, clientId=41, readonly=True)
    assert fake.mdt == 2  # out of hours: frozen, which only a real-time subscription delivers
    with pytest.raises(PermissionError):
        fake.placeOrder()
    with pytest.raises(PermissionError):
        fake.client.placeOrder()
    assert fake.placed_calls == 0


def test_a_refused_connection_is_reported_not_raised():
    gw, _ = gateway(refuse=True)
    assert gw.connect() is False
    assert gw.health.refused and not gw.ready and "refused" in gw.health.last_error


def test_gateway_cut_off_from_ibkr_is_not_ready():
    """What Gateway said at 21:09 on 24 Sep 2026: API up, link to IBKR broken (2110)."""
    gw, fake = gateway(on_connect=[(2110, "Connectivity between Trader Workstation and server "
                                    "is broken."), (2103, "Market data farm connection is "
                                    "broken:hfarm")])  # fmt: skip
    assert gw.connect() is False
    assert gw.health.connected and not gw.health.server_ok and not gw.ready
    assert "hfarm" in gw.health.farms_broken
    fake.errorEvent.emit(-1, 2104, "Market data farm connection is OK:hfarm", None)
    assert gw.ready


def test_client_zero_and_remote_hosts_are_refused():
    with pytest.raises(ValueError):
        GatewaySettings(client_id=0)
    with pytest.raises(ValueError):
        GatewaySettings(host="10.0.0.5")


def test_reconnect_is_not_tried_more_often_than_configured():
    clock = Clock()
    gw, fake = gateway(clock, refuse=True, settings={"reconnect_every_s": 300})
    assert maybe_reconnect(gw) is False
    fake.refuse = False
    clock.t += 100
    assert maybe_reconnect(gw) is False  # too soon: not even tried
    assert fake.connect_args is not None and not fake.connected
    clock.t += 250
    assert maybe_reconnect(gw) is True


# --------------------------------------------------------------------------
# bars and pacing
# --------------------------------------------------------------------------
def test_bars_are_sydney_bar_starts_and_an_index_has_no_volume():
    got = bars_frame(session(DAY, n=3) + [SimpleNamespace(**{**vars(session(DAY)[0]),
                                                            "volume": -1})])  # fmt: skip
    assert list(got.columns) == ["open", "high", "low", "close", "volume"]
    assert got.index[0] == datetime(2026, 9, 25, 10, 0, tzinfo=SYD)
    assert len(got) == 3  # the duplicate 10:00 bar replaced, not added
    assert (got["volume"] >= 0).all()


def test_the_pacing_cap_limits_requests_in_any_ten_minutes():
    clock = Clock()
    bars = {c: session(DAY) for c in ("AAA", "BBB", "CCC")}
    gw, fake = gateway(clock, bars=bars, settings={"max_requests_per_10min": 2})
    gw.connect()
    got = gw.bars({c: ("600 S", None) for c in ("AAA", "BBB", "CCC")})
    assert len(got) == 2 and len(fake.requests) == 2
    assert gw.bars({"CCC": ("600 S", None)}) == {}  # no room left
    clock.t += 601
    assert set(gw.bars({"CCC": ("600 S", None)})) == {"CCC"}
    sym, dur, end, size, what, rth, fmt = fake.requests[0]
    assert (size, what, rth, fmt) == ("1 min", "TRADES", False, 2)


def test_a_pacing_violation_halves_the_budget():
    gw, fake = gateway()
    gw.connect()
    assert gw.budget == 90
    fake.errorEvent.emit(7, 162, "Historical Market Data Service error message:API historical "
                         "data query cancelled: pacing violation", None)  # fmt: skip
    assert gw.budget == 45 and gw.health.pacing_hits == 1


def test_a_quote_gives_its_line_back_and_says_how_real_it_is():
    q = FakeTicker(bid=45.1, ask=45.12, bidSize=500, askSize=800, last=45.11, lastSize=100,
                   open=45.0, close=44.8, volume=1.2e6, halted=0, auctionPrice=45.0,
                   auctionVolume=2e5, marketDataType=1)  # fmt: skip
    gw, fake = gateway(quotes={"BHP": q})
    fake.known["XYZ"] = (999, "STK", "ASX", "AUD")  # a real stock that sends nothing back
    gw.connect()
    got = gw.quote("BHP")
    assert got["bid"] == 45.1 and got["ask_size"] == 800 and got["open"] == 45.0
    assert got["halted"] == 0 and got["auction_price"] == 45.0
    assert got["market_data_type"] == 1 and gw.health.market_data_type == 1
    assert fake.cancelled == ["BHP"] and gw._lines == 0
    assert gw.quote("XYZ") is None and fake.cancelled == ["BHP", "XYZ"]  # nothing came back


# --------------------------------------------------------------------------
# contracts are qualified before use
# --------------------------------------------------------------------------
def test_a_quote_qualifies_its_contract_first_and_caches_it():
    """24 Sep 2026 21:32: an unqualified Stock('BHP', 'ASX', 'AUD') could not be quoted."""
    gw, fake = gateway(quotes={"BHP": FakeTicker(last=45.0, close=44.0, bid=44.9, ask=45.1)})
    gw.connect()
    assert gw.quote("BHP")["last"] == 45.0
    c = gw.contracts["BHP"]
    assert (c.conId, c.secType, c.exchange, c.primaryExchange, c.currency) == (
        4036812, "STK", "ASX", "ASX", "AUD",
    )  # fmt: skip
    gw.quote("BHP")
    gw.bars({"BHP": ("1 D", None)})
    assert fake.qualified == ["BHP"]  # looked up once, then from the cache


def test_bars_for_the_rotation_and_the_index_use_qualified_contracts():
    bars = {c: session(DAY) for c in ("AAA", "BBB")} | {"XJO": session(DAY, vol=0)}
    gw, fake = gateway(bars=bars)
    gw.connect()
    got = gw.bars({"AAA": ("600 S", None), "BBB": ("600 S", None), "^AXJO": ("1 D", None)})
    assert set(got) == {"AAA", "BBB", "^AXJO"}
    ix = gw.contracts["^AXJO"]
    assert (ix.secType, ix.symbol, ix.exchange, ix.currency, ix.conId) == (
        "IND", "XJO", "ASX", "AUD", 46610746,
    )  # fmt: skip
    assert sorted(fake.qualified) == ["AAA", "BBB", "XJO"]
    gw.bars({"AAA": ("600 S", None)})
    assert sorted(fake.qualified) == ["AAA", "BBB", "XJO"]


def test_the_auction_price_comes_through_a_qualified_contract():
    q = FakeTicker(last=NAN, close=44.8, bid=45.0, ask=45.0, auctionPrice=45.02,
                   auctionVolume=3e5, marketDataType=1)  # fmt: skip
    gw, fake = gateway(quotes={"BHP": q}, wall=lambda: datetime(2026, 9, 25, 9, 58, tzinfo=SYD))
    gw.connect()
    got = gw.quote("BHP")
    assert got["auction_price"] == 45.02 and got["auction_volume"] == 3e5
    assert "BHP" in gw.contracts


def test_a_contract_ibkr_cannot_match_is_skipped_and_asked_again_an_hour_later():
    clock = Clock()
    gw, fake = gateway(clock, bars={"AAA": session(DAY)}, known={"XJO": KNOWN["XJO"]})
    del fake.known["AAA"]  # unknown to IBKR (a delisting, a code change)
    gw.connect()
    assert gw.bars({"AAA": ("600 S", None)}) == {} and gw.quote("AAA") is None
    assert fake.qualified == ["AAA"] and fake.requests == []  # not re-asked within the hour
    fake.known["AAA"] = (777, "STK", "ASX", "AUD")
    clock.t += 3601
    assert set(gw.bars({"AAA": ("600 S", None)})) == {"AAA"}


@pytest.mark.parametrize(
    "found, why",
    [
        ((555, "STK", "CHIXAU", "AUD"), "primary exchange"),  # not an ASX primary listing
        ((555, "STK", "ASX", "USD"), "not AUD"),
        ((0, "STK", "ASX", "AUD"), "no single matching"),  # no conId
    ],
)
def test_a_wrong_contract_is_refused_not_used(found, why, caplog):
    gw, fake = gateway(bars={"AAA": session(DAY)})
    fake.known["AAA"] = found
    gw.connect()
    assert gw.bars({"AAA": ("600 S", None)}) == {} and fake.requests == []
    assert "AAA" not in gw.contracts and why in caplog.text


def test_a_lookup_that_fails_is_not_cached_as_unknown():
    gw, fake = gateway(bars={"AAA": session(DAY)})
    gw.connect()

    async def broken(*c):
        raise TimeoutError("contract details timed out")

    real, fake.qualifyContractsAsync = fake.qualifyContractsAsync, broken
    assert gw.bars({"AAA": ("600 S", None)}) == {}
    fake.qualifyContractsAsync = real
    gw.pacer.stamps.clear()
    assert set(gw.bars({"AAA": ("600 S", None)})) == {"AAA"}  # tried again at once


# --------------------------------------------------------------------------
# market data type: real-time in the ASX's hours, frozen outside them
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "when, want",
    [
        (datetime(2026, 9, 25, 6, 59, tzinfo=SYD), 2),  # before the pre-open
        (datetime(2026, 9, 25, 7, 0, tzinfo=SYD), 1),  # pre-open: the auction price is live
        (datetime(2026, 9, 25, 11, 0, tzinfo=SYD), 1),
        (datetime(2026, 9, 25, 16, 12, tzinfo=SYD), 1),  # the closing auction
        (datetime(2026, 9, 25, 16, 15, tzinfo=SYD), 2),
        (datetime(2026, 9, 26, 11, 0, tzinfo=SYD), 2),  # a Saturday
    ],
)
def test_the_data_type_asked_for_follows_the_asx_clock(when, want):
    from asxbot.ibkr.gateway import live_data_type

    assert live_data_type(when) == want


def test_the_data_type_is_switched_when_the_window_changes_and_only_then():
    t = {"now": datetime(2026, 9, 25, 6, 50, tzinfo=SYD)}
    gw, fake = gateway(quotes={"BHP": FakeTicker(last=45.0, close=44.0, bid=1, ask=2)},
                       wall=lambda: t["now"])  # fmt: skip
    gw.connect()
    gw.quote("BHP")
    t["now"] = datetime(2026, 9, 25, 7, 30, tzinfo=SYD)
    gw.quote("BHP")
    gw.quote("BHP")
    assert fake.mdt_calls == [2, 1]
    assert gw.health.to_dict()["requested_data"] == "real-time"


def test_no_subscription_in_hours_sends_the_feed_to_yahoo(tmp_path):
    """Real-time is asked for in hours; without the subscription IBKR sends nothing (354)."""
    now = datetime(2026, 9, 25, 10, 5, tzinfo=SYD)
    gw, fake = gateway(wall=lambda: now)
    gw.connect()
    fake.reqMktData = lambda c, g="": (
        fake.errorEvent.emit(9, 354, "Requested market data is not subscribed.", c),
        FakeTicker(marketDataType=None),
    )[1]
    feed, _ = _failover(tmp_path, gw, now)
    feed.refresh(["BHP"], now)
    assert not feed.using_primary and "354" in feed.why


# --------------------------------------------------------------------------
# the feed
# --------------------------------------------------------------------------
class FakeYahoo:
    name = "yahoo_delayed"
    delayed = True
    label = DELAYED_LABEL

    def __init__(self, minutes):
        self.minutes = minutes
        self.refreshed = []

    def refresh(self, codes, now):
        self.refreshed.append(list(codes))
        return list(codes)

    def fetch_one(self, code, now):
        self.refreshed.append([code])

    def bars(self, code, day, now):
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    def history_source(self):
        return self.minutes

    def ensure_history(self, code, day, sessions):
        return False

    def prepare(self, codes, day, sessions):
        return 0


def test_the_live_feed_shows_only_final_bars(tmp_path):
    gw, fake = gateway(bars={"BHP": session(DAY, n=6)})
    gw.connect()
    feed = F.IBKRLiveFeed(MinuteBars(tmp_path), gw)
    now = datetime(2026, 9, 25, 10, 6, 20, tzinfo=SYD)
    assert feed.refresh(["BHP"], now) == ["BHP"]
    b = feed.bars("BHP", DAY, now)
    # 10:05 is the newest row (still forming): 10:00-10:04 are final
    assert list(b.index.strftime("%H:%M")) == ["10:00", "10:01", "10:02", "10:03", "10:04"]
    assert not feed.delayed and feed.label == F.LIVE_LABEL
    # asked again inside 15 s: not asked (no identical request within 15 s)
    assert feed.refresh(["BHP"], now + timedelta(seconds=10)) == []


def test_usual_volume_comes_from_ibkr_history_when_ibkr_is_the_feed(tmp_path):
    prior = []
    for d in (date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24)):
        prior += session(d, n=30, vol=500.0)
    gw, fake = gateway(bars={"BHP": prior})
    gw.connect()
    feed = F.IBKRLiveFeed(MinuteBars(tmp_path), gw)
    view = MarketView(MinuteBars(tmp_path), DAY, feed, sessions=3, min_sessions=3)
    assert view.prepare(["BHP"]) == 1
    sym, dur, end, *_ = fake.requests[0]
    assert dur == "4 D" and end == datetime(2026, 9, 25, 0, 0, tzinfo=SYD)
    u = view.usual("BHP")
    assert u is not None and u.iloc[5] == pytest.approx(5 * 500.0)  # 10:01-10:05, from IBKR
    assert view.prev_close("BHP") == pytest.approx(10.29)
    assert view.prepare(["BHP"]) == 0  # once a day


def _failover(tmp_path, gw, now):
    minutes = MinuteBars(tmp_path / "data")
    from asxbot.log import EventLog

    ev = EventLog(tmp_path / "data")
    return F.FailoverFeed(minutes, F.IBKRLiveFeed(minutes, gw, ev), FakeYahoo(minutes),
                          tmp_path / "data", ev, now=now), ev  # fmt: skip


def test_gateway_down_at_the_start_means_yahoo_labelled_and_flagged(tmp_path):
    now = datetime(2026, 9, 25, 7, 30, tzinfo=SYD)
    gw, fake = gateway(refuse=True)
    gw.connect()
    feed, ev = _failover(tmp_path, gw, now)
    assert feed.label == F.FALLBACK_LABEL and feed.delayed
    st = F.read_status(tmp_path / "data")
    assert st["provider_in_use"] == "yfinance" and st["gateway"]["refused"]
    assert [r["event"] for r in ev.read("live_data")] == ["fallback"]
    feed.refresh(["BHP"], now)
    assert feed.backup.refreshed == [["BHP"]]


def test_the_feed_falls_back_and_comes_back(tmp_path):
    clock = Clock()
    now = datetime(2026, 9, 25, 10, 5, tzinfo=SYD)
    gw, fake = gateway(
        clock,
        bars={"BHP": session(DAY)},
        quotes={"BHP": FakeTicker(last=45.0, close=44.0, bid=1, ask=2)},
    )
    gw.connect()
    feed, ev = _failover(tmp_path, gw, now)
    assert feed.using_primary and feed.label == F.LIVE_LABEL
    feed.refresh(["BHP"], now)
    assert fake.requests and not feed.backup.refreshed

    fake.errorEvent.emit(-1, 1100, "Connectivity between IB and TWS has been lost.", None)
    feed.refresh(["BHP"], now + timedelta(minutes=1))
    assert not feed.using_primary and feed.generation == 1
    assert feed.backup.refreshed == [["BHP"]]
    assert F.read_status(tmp_path / "data")["provider_in_use"] == "yfinance"

    fake.errorEvent.emit(-1, 1102, "Connectivity restored - data maintained.", None)
    clock.t += 301
    feed.refresh(["BHP"], now + timedelta(minutes=7))
    assert feed.using_primary and feed.generation == 2
    assert [r["event"] for r in ev.read("live_data")] == ["fallback", "restored"]


def test_a_batch_that_timed_out_does_not_keep_the_watcher_on_yahoo(tmp_path):
    """25 Sep 2026, 10:17: the first bars batch on a freshly logged-in Gateway timed out; the
    link was marked down while the socket stayed open, and only a Gateway "restored" message
    could clear it - one never sent for a link Gateway itself never reported lost. A fresh
    connection after `reconnect_every_s` now puts the watcher back on IBKR."""
    clock = Clock()
    now = datetime(2026, 9, 25, 10, 17, tzinfo=SYD)
    gw, fake = gateway(clock, bars={"BHP": session(DAY)},
                       quotes={"BHP": FakeTicker(last=45.0, close=44.0, bid=1, ask=2)},
                       settings={"reconnect_every_s": 60})  # fmt: skip
    gw.connect()
    feed, ev = _failover(tmp_path, gw, now)
    real_run = fake.run

    def timed_out(aw):
        aw.close()
        raise TimeoutError()

    fake.run = timed_out
    feed.refresh(["BHP"], now)
    fake.run = real_run
    assert gw.health.last_error == "bars batch failed: TimeoutError()" and not gw.ready
    feed.refresh(["BHP"], now + timedelta(seconds=30))
    assert not feed.using_primary and fake.connected  # on Yahoo, socket still open

    clock.t += 30
    feed.refresh(["BHP"], now + timedelta(seconds=60))
    assert not feed.using_primary  # not before reconnect_every_s
    clock.t += 31
    feed.refresh(["BHP"], now + timedelta(seconds=91))
    assert feed.using_primary and gw.ready
    assert [r["event"] for r in ev.read("live_data")] == ["fallback", "restored"]
    assert F.read_status(tmp_path / "data")["provider_in_use"] == "ibkr"


def test_a_gateway_that_dies_and_comes_back_is_used_again_by_itself(tmp_path):
    """Gateway's process gone (08:58 on 25 Sep), the port refusing while it is down, then a
    new Gateway logged in: the watcher goes to Yahoo and comes back with no restart."""
    clock = Clock()
    now = datetime(2026, 9, 25, 9, 0, tzinfo=SYD)
    gw, fake = gateway(clock, bars={"BHP": session(DAY)},
                       quotes={"BHP": FakeTicker(last=45.0, close=44.0, bid=1, ask=2)},
                       settings={"reconnect_every_s": 60})  # fmt: skip
    gw.connect()
    feed, ev = _failover(tmp_path, gw, now)
    assert feed.using_primary

    fake.connected = False  # the process is killed: the socket closes
    fake.refuse = True  # and nothing listens on 4001 while it is down
    for h in fake.disconnectedEvent.handlers:
        h()
    feed.refresh(["BHP"], now + timedelta(minutes=1))
    assert not feed.using_primary and feed.backup.refreshed
    for m in range(2, 6):
        clock.t += 61
        feed.refresh(["BHP"], now + timedelta(minutes=m))
        assert not feed.using_primary  # still down: tried, refused, still on Yahoo
    assert gw.health.refused

    fake.refuse = False  # the supervisor's Gateway is up and logged in
    clock.t += 61
    feed.refresh(["BHP"], now + timedelta(minutes=7))
    assert feed.using_primary and feed.label == F.LIVE_LABEL
    assert [r["event"] for r in ev.read("live_data")] == ["fallback", "restored"]


def test_a_gateway_still_cut_off_from_ibkr_after_a_fresh_connection_stays_on_yahoo(tmp_path):
    clock = Clock()
    now = datetime(2026, 9, 25, 10, 5, tzinfo=SYD)
    gw, fake = gateway(clock, quotes={"BHP": FakeTicker(last=45.0, close=44.0, bid=1, ask=2)},
                       settings={"reconnect_every_s": 60})  # fmt: skip
    gw.connect()
    feed, _ = _failover(tmp_path, gw, now)
    fake.errorEvent.emit(-1, 1100, "Connectivity between IB and TWS has been lost.", None)
    fake.on_connect = [(2110, "Connectivity between TWS and server is broken.")]
    feed.refresh(["BHP"], now + timedelta(minutes=1))
    clock.t += 61
    feed.refresh(["BHP"], now + timedelta(minutes=2))
    assert not feed.using_primary and fake.connected and not gw.health.server_ok


def test_delayed_ibkr_data_in_market_hours_is_not_used(tmp_path):
    now = datetime(2026, 9, 25, 10, 5, tzinfo=SYD)
    gw, fake = gateway(quotes={"BHP": FakeTicker(last=45.0, close=44.0, bid=1, ask=2,
                                                 marketDataType=3)})  # fmt: skip
    gw.connect()
    feed, _ = _failover(tmp_path, gw, now)
    feed.refresh(["BHP"], now)
    assert not feed.using_primary and "delayed" in feed.why and "BHP" in feed.why


def test_a_mostly_empty_batch_is_a_failure_not_a_quiet_market(tmp_path):
    now = datetime(2026, 9, 25, 10, 30, tzinfo=SYD)
    codes = [f"S{i:02d}" for i in range(12)]
    gw, fake = gateway(
        bars={"S00": session(DAY)}, quotes={"BHP": FakeTicker(last=45.0, close=44.0, bid=1, ask=2)}
    )
    gw.connect()
    feed, _ = _failover(tmp_path, gw, now)
    feed.refresh(codes, now)
    assert not feed.using_primary and "1 of 12" in feed.why


# --------------------------------------------------------------------------
# the switch in config
# --------------------------------------------------------------------------
def test_one_config_value_switches_the_feed(cfg, tmp_path, monkeypatch):
    minutes = MinuteBars(tmp_path / "m")
    cfg.raw["data"]["live_provider"] = "yfinance"
    assert isinstance(make_feed(cfg, minutes), YahooDelayedFeed)
    cfg.raw["data"]["live_provider"] = "ibkr"
    gw, _ = gateway(refuse=True)
    monkeypatch.setattr(F, "shared", lambda c: gw)
    feed = make_feed(cfg, minutes)
    assert isinstance(feed, F.FailoverFeed) and not feed.using_primary
    cfg.raw["data"]["live_provider"] = "bloomberg"
    with pytest.raises(ValueError):
        live_provider(cfg)
    cfg.raw["data"]["live_provider"] = "ibkr"
    cfg.raw["arena"]["intraday_data"]["provider"] = "ibkr_live"
    with pytest.raises(ValueError, match="data.live_provider"):
        live_provider(cfg)


def test_the_committed_config_has_the_switch_and_ibkr_settings():
    c = load_config()
    assert live_provider(c) in ("ibkr", "yfinance")
    assert c.get("ibkr.port") == 4001 and c.get("ibkr.client_id") not in (0, None)


# --------------------------------------------------------------------------
# the self-check and the login message
# --------------------------------------------------------------------------
def _status(cfg, at, in_use="yfinance", **gw):
    health = {"connected": False, "server_ok": False, "refused": True, "market_data": "unknown",
              "last_error": "connect refused", **gw}  # fmt: skip
    p = F.status_path(cfg.data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"at": at.isoformat(), "provider_in_use": in_use, "why": "refused",
                             "gateway": health}), encoding="utf-8")  # fmt: skip


def test_the_live_data_check(cfg):
    now = datetime(2026, 9, 25, 10, 30, tzinfo=SYD)
    cfg.raw["data"]["live_provider"] = "yfinance"
    assert S.check_live_data(cfg, now).ok
    cfg.raw["data"]["live_provider"] = "ibkr"
    assert S.check_live_data(cfg, now).ok  # no status yet: the watcher has not started
    _status(cfg, now - timedelta(minutes=2))
    c = S.check_live_data(cfg, now)
    assert not c.ok and c.facts["login_needed"] and "Yahoo" in c.detail
    _status(cfg, now - timedelta(minutes=40), "ibkr", connected=True, server_ok=True,
            refused=False)  # fmt: skip
    c = S.check_live_data(cfg, now)
    assert not c.ok and "40 minutes" in c.detail  # stale in market hours
    _status(cfg, now - timedelta(minutes=2), "ibkr", connected=True, server_ok=True,
            refused=False, market_data="real-time")  # fmt: skip
    assert S.check_live_data(cfg, now).ok


def test_rick_is_told_to_log_in_once_in_one_line(cfg, monkeypatch):
    sent = []
    arena = SimpleNamespace(cfg=cfg, broker=SimpleNamespace(
        notifier=SimpleNamespace(send=lambda text: sent.append(text))))  # fmt: skip
    now = datetime(2026, 9, 25, 7, 31, tzinfo=SYD)
    down = S.Check("live_data", False, "IBKR unavailable", facts={"login_needed": True},
                   items=["refused"])  # fmt: skip
    monkeypatch.setattr(S, "run_checks", lambda a, p, n: [down])
    for k in range(4):  # all morning, and past the hourly repeat
        S.report(arena, None, now + timedelta(minutes=45 * k))
    assert sent == [S.LOGIN_LINE] and "\n" not in S.LOGIN_LINE
    monkeypatch.setattr(S, "run_checks", lambda a, p, n: [S.Check("live_data", True, "ok")])
    S.report(arena, None, now + timedelta(hours=4))
    monkeypatch.setattr(S, "run_checks", lambda a, p, n: [down])
    S.report(arena, None, now + timedelta(hours=5))  # a new outage is told again
    assert sent == [S.LOGIN_LINE, S.LOGIN_LINE]


# --------------------------------------------------------------------------
# the evening report says which prices each decision used
# --------------------------------------------------------------------------
def test_the_report_counts_decisions_by_prices(cfg):
    from asxbot.arena import report as R
    from asxbot.arena.daytrader import save_state

    pb = SimpleNamespace(data_basis="delayed data - rehearsal until IBKR live prices")
    assert R.data_line(pb, {}) == pb.data_basis
    assert R.data_line(pb, {DELAYED_LABEL: 4}) == pb.data_basis
    save_state(cfg.data_dir, DAY, {"signals": [{"data": F.LIVE_LABEL}, {"data": F.LIVE_LABEL},
                                               {"data": F.FALLBACK_LABEL}]})  # fmt: skip
    used = R.decision_data(cfg, DAY)
    assert used["asx_daytrader"] == {F.LIVE_LABEL: 2, F.FALLBACK_LABEL: 1}
    line = R.data_line(pb, used["asx_daytrader"])
    assert "2 on live data (IBKR)" in line and "1 on delayed data (Yahoo)" in line


# --------------------------------------------------------------------------
# the one-command check
# --------------------------------------------------------------------------
def test_the_check_passes_on_live_data_and_fails_plainly_otherwise(cfg):
    from asxbot.ibkr.check import run_check

    lines = []
    quotes = {
        "BHP": FakeTicker(bid=45.1, ask=45.12, last=45.11, close=44.8, marketDataType=2),
        "XJO": FakeTicker(last=8800.0, close=8790.0, marketDataType=2),
    }
    gw, fake = gateway(bars={"BHP": session(DAY), "XJO": session(DAY, vol=0)}, quotes=quotes)
    evening = datetime(2026, 9, 25, 21, 0, tzinfo=SYD)
    assert run_check(cfg, lines.append, gateway=gw, now=evening) == 0
    assert lines[-1].startswith("PASS") and "BHP quote: frozen" in lines[4]
    assert "asked for frozen (2), got frozen (2)" in lines[3] and fake.mdt == 2
    saved = json.loads((cfg.data_dir / "arena" / "ibkr_check.json").read_text(encoding="utf-8"))
    assert saved["ok"] and saved["real_time_proven"]
    assert (saved["requested_market_data"], saved["received_market_data"]) == ("frozen", "frozen")

    lines.clear()  # no subscription: frozen is refused and nothing comes back
    gw, fake = gateway(bars={"BHP": session(DAY)})
    fake.reqMktData = lambda c, g="": (
        fake.errorEvent.emit(9, 354, "Requested market data is not subscribed.", c),
        FakeTicker(marketDataType=None),
    )[1]
    assert run_check(cfg, lines.append, gateway=gw, now=evening) == 1
    assert "asked for frozen" in lines[-1] and "354" in lines[-1]

    lines.clear()
    gw, _ = gateway(refuse=True)
    assert run_check(cfg, lines.append, gateway=gw, now=evening) == 1
    assert lines[-1].startswith("FAIL: IB Gateway is not accepting connections")

    lines.clear()
    quotes["BHP"] = FakeTicker(last=45.11, close=44.8, bid=1, ask=2, marketDataType=3)
    gw, _ = gateway(bars={"BHP": session(DAY)}, quotes=quotes)
    in_hours = datetime(2026, 9, 25, 11, 0, tzinfo=SYD)
    assert run_check(cfg, lines.append, gateway=gw, now=in_hours) == 1
    assert "not real-time" in lines[-1]
