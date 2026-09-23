"""Orders at the open fill at the opening auction (TRACKER #28, 2026-09-24). No network.

The 1-minute feed leaves out the ASX opening auction, so until 2026-09-24 an order "at the
open" filled at the close of the first traded minute after it. Now each day's bars are led by
its opening auction, priced at Yahoo's daily open, and no more than a set share of the
auction's estimated volume (the daily volume less the minute bars') fills in it; the rest
carries into the minute bars. With no auction price the arena trusts, orders fill at the first
traded minute, as before, and say so.
"""

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from asxbot.arena import minutes as M
from asxbot.arena.accounts import Position
from asxbot.arena.broker import ArenaBroker
from asxbot.arena.minutes import MinuteBars
from asxbot.backtest.costs import CostModel
from asxbot.config import load_config
from asxbot.io import write_parquet_atomic

SYD = ZoneInfo("Australia/Sydney")
DAY = date(2026, 1, 6)  # a Tuesday, far enough back that nothing reaches for the network
NEXT = date(2026, 1, 7)
ADV = 2_000_000.0


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def at(day: date, h: int, m: int, s: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=SYD)


def bars_on(broker, ticker, day, rows):
    """rows: (HH, MM, open, high, low, close, volume), then the feed's zero-volume row for
    the minute still forming (#26)."""
    h, m, *_ = rows[-1]
    c = rows[-1][5]
    rows = [*rows, (h + (m + 1) // 60, (m + 1) % 60, c, c, c, c, 0)]
    idx = [at(day, h, m) for h, m, *_ in rows]
    cols = ("open", "high", "low", "close", "volume")
    df = pd.DataFrame(
        {k: [float(r[i]) for r in rows] for i, k in enumerate(cols, start=2)},
        index=pd.DatetimeIndex(idx, name="Datetime"),
    )
    write_parquet_atomic(df, broker.minutes._path(ticker, day))


def daily(monkeypatch, rows: dict):
    """Yahoo's daily bars: {(ticker, day): {open, high, low, close, volume}}. raising=False, so
    on code without an auction this sets a harmless attribute and the tests fail on what
    they assert, not on a missing name."""
    calls = []

    def fetch(self, code, day):
        calls.append((code, day))
        return rows.get((code, day))

    monkeypatch.setattr(MinuteBars, "fetch_daily_row", fetch, raising=False)
    return calls


@pytest.fixture
def cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )


def make_broker(cfg, **kw):
    return ArenaBroker(
        cfg.data_dir, CostModel.from_config(cfg), MinuteBars(cfg.data_dir, "close"),
        lambda t: ADV, resolve_after_minutes=22, clock=Clock(at(DAY, 9, 0)), **kw,
    )  # fmt: skip


@pytest.fixture
def broker(cfg):
    return make_broker(cfg)


@pytest.fixture
def agent(broker):
    return broker.store.open("t__agent", "asx_announcements", "agent", 1, 20_000.0)


def submit(broker, acct, when, **kw):
    broker.clock = Clock(when)
    return broker.submit(acct, data_as_of=when, **kw)


def slip(broker, value):
    return broker.costs.slippage_pct(value, ADV)


# The open of DAY: the auction printed 1.02 (the daily open); the first minute bar, which the
# feed stamps 10:00, opened at 1.00 and closed at 1.04.
OPEN_BARS = [
    (10, 0, 1.00, 1.05, 1.00, 1.04, 50_000),
    (10, 1, 1.04, 1.06, 1.03, 1.05, 10_000),
]
MINUTE_VOLUME = 60_000


def row(open_=1.02, volume=MINUTE_VOLUME + 100_000, low=1.00, high=1.06):
    return {"open": open_, "high": high, "low": low, "close": 1.05, "volume": volume}


def test_an_order_recorded_before_the_open_fills_at_the_auction_price(broker, agent, monkeypatch):
    """Fails on aab9e55, which filled it at the 10:00 bar's close, 1.04."""
    bars_on(broker, "AAA", DAY, OPEN_BARS)
    daily(monkeypatch, {("AAA", DAY): row()})
    o = submit(broker, agent, at(DAY, 9, 30), ticker="AAA", side="buy", qty=3000, limit=1.10,
               stop=0.95)  # fmt: skip
    broker.work(agent, at(DAY, 10, 30))
    assert o.status == "filled"
    (f,) = o.fills
    assert f["minute"].startswith("2026-01-06T09:59") and f.get("auction") is True
    assert f["bar_price"] == pytest.approx(1.02)
    assert o.avg_price == pytest.approx(1.02 * (1 + slip(broker, 3000 * 1.10)), abs=1e-4)
    assert "opening auction" in o.fill_basis and "1.0200" in o.fill_basis
    assert agent.positions["AAA"].opened_at.startswith("2026-01-06T09:59")


def test_the_auction_fills_at_most_its_share_and_the_rest_carries_into_the_bars(
    broker, agent, monkeypatch
):
    """Auction volume 10,000 (70,000 daily less 60,000 in the minute bars): 20% is 2,000.
    Fails on aab9e55, which filled all 5,000 in the 10:00 bar at 1.04."""
    bars_on(broker, "AAA", DAY, OPEN_BARS)
    daily(monkeypatch, {("AAA", DAY): row(volume=MINUTE_VOLUME + 10_000)})
    o = submit(broker, agent, at(DAY, 9, 30), ticker="AAA", side="buy", qty=5000, limit=1.10)
    broker.work(agent, at(DAY, 10, 30))
    assert [(f["minute"][11:16], f["qty"], f["bar_price"]) for f in o.fills] == [
        ("09:59", 2000, pytest.approx(1.02)),
        ("10:00", 3000, pytest.approx(1.04)),
    ]
    assert o.fills[0]["bar_volume"] == pytest.approx(10_000)
    assert o.status == "filled"
    assert "opening auction" in o.fill_basis and "estimated volume" in o.fill_basis


def test_an_order_recorded_in_the_last_minute_of_the_pre_open_misses_the_auction(
    broker, agent, monkeypatch
):
    """The auction is stamped 09:59: an order joins it only if recorded before 09:59:00."""
    bars_on(broker, "AAA", DAY, OPEN_BARS)
    daily(monkeypatch, {("AAA", DAY): row()})
    o = submit(broker, agent, at(DAY, 9, 59, 30), ticker="AAA", side="buy", qty=3000,
               limit=1.10)  # fmt: skip
    broker.work(agent, at(DAY, 10, 30))
    assert o.fill_minute.startswith("2026-01-06T10:00")
    assert o.fills[0]["bar_price"] == pytest.approx(1.04)
    assert "auction" not in o.fills[0] and not o.open_note


def test_an_order_recorded_after_the_close_joins_the_next_mornings_auction(
    broker, agent, monkeypatch
):
    """Fails on aab9e55, which filled it at the next day's 10:00 bar."""
    bars_on(broker, "AAA", NEXT, OPEN_BARS)
    daily(monkeypatch, {("AAA", NEXT): row()})
    o = submit(broker, agent, at(DAY, 16, 30), ticker="AAA", side="buy", qty=3000, limit=1.10)
    broker.work(agent, at(NEXT, 10, 30))
    assert o.fill_minute.startswith("2026-01-07T09:59")
    assert o.fills[0]["bar_price"] == pytest.approx(1.02)


def held(acct, stop=0.90, target=None, qty=3000):
    """A long held overnight from DAY, its stop and target checked through DAY's close."""
    acct.positions["AAA"] = Position(
        ticker="AAA", qty=qty, avg_cost=1.00, opened_at="2026-01-06T10:41", stop=stop,
        target=target, opened_by="agent", target_from="2026-01-06T10:41" if target else "",
        worked_through="2026-01-06T16:10",
    )  # fmt: skip
    acct.cash -= qty * 1.00


GAP_DOWN = [(10, 0, 0.87, 0.88, 0.86, 0.87, 50_000), (10, 1, 0.87, 0.88, 0.86, 0.88, 50_000)]


def test_a_stop_the_auction_gaps_through_fills_at_the_auction_price(broker, agent, monkeypatch):
    """Stop 0.90; the auction printed 0.85. Fails on aab9e55, which took the 10:00 bar's open
    (0.87) - a better price than the market gave."""
    held(agent)
    bars_on(broker, "AAA", NEXT, GAP_DOWN)
    daily(monkeypatch, {("AAA", NEXT): row(open_=0.85, low=0.85, high=0.88,
                                              volume=100_000 + 30_000)})  # fmt: skip
    broker.work(agent, at(NEXT, 10, 30))
    (stop,) = [o for o in agent.orders.values() if o.order_type == "stop"]
    assert stop.status == "filled" and stop.trigger_price == pytest.approx(0.85)
    assert [(f["minute"][11:16], f["qty"]) for f in stop.fills] == [("09:59", 3000)]
    assert stop.avg_price == pytest.approx(0.85 * (1 - slip(broker, 3000 * 0.85)), abs=1e-4)
    assert "opening auction" in stop.fill_basis
    assert "AAA" not in agent.positions


def test_a_stop_bigger_than_the_auctions_share_carries_into_the_bars_at_market(
    broker, agent, monkeypatch
):
    """Auction volume 5,000: 1,000 goes at the auction, the rest at the minute bars' prices."""
    held(agent)
    bars_on(broker, "AAA", NEXT, GAP_DOWN)
    daily(monkeypatch, {("AAA", NEXT): row(open_=0.85, low=0.85, high=0.88,
                                              volume=100_000 + 5_000)})  # fmt: skip
    broker.work(agent, at(NEXT, 10, 30))
    (stop,) = [o for o in agent.orders.values() if o.order_type == "stop"]
    assert [(f["minute"][11:16], f["qty"], f["bar_price"]) for f in stop.fills] == [
        ("09:59", 1000, pytest.approx(0.85)),
        ("10:00", 2000, pytest.approx(0.87)),  # a market order from the auction on
    ]


def test_a_target_the_auction_gaps_through_fills_at_the_auction_price(
    broker, agent, monkeypatch
):
    """Target 1.10; the auction printed 1.15, a resting sell limit fills there. Fails on
    aab9e55, which took the 10:00 bar's open, 1.12."""
    held(agent, stop=0.80, target=1.10)
    bars_on(broker, "AAA", NEXT, [(10, 0, 1.12, 1.13, 1.11, 1.12, 50_000),
                                  (10, 1, 1.12, 1.12, 1.11, 1.11, 50_000)])  # fmt: skip
    daily(monkeypatch, {("AAA", NEXT): row(open_=1.15, low=1.11, high=1.15,
                                              volume=100_000 + 50_000)})  # fmt: skip
    broker.work(agent, at(NEXT, 10, 30))
    (tgt,) = [o for o in agent.orders.values() if o.order_type == "target"]
    assert tgt.status == "filled" and tgt.fills[0]["minute"].startswith("2026-01-07T09:59")
    assert tgt.fills[0]["bar_price"] == pytest.approx(1.15)
    assert "opening auction" in tgt.fill_basis


@pytest.mark.parametrize(
    "bad, why",
    [
        (row(open_=1.0234), "not on the ASX tick grid"),
        (row(open_=1.10), "outside the daily bar's range"),
        (row(volume=MINUTE_VOLUME), "cannot be measured"),
    ],
)
def test_an_auction_that_fails_a_check_is_not_used_and_the_fill_says_so(
    broker, agent, monkeypatch, bad, why
):
    """Fails on aab9e55: the fill says nothing about the auction."""
    bars_on(broker, "AAA", DAY, OPEN_BARS)
    daily(monkeypatch, {("AAA", DAY): bad})
    o = submit(broker, agent, at(DAY, 9, 30), ticker="AAA", side="buy", qty=3000, limit=1.10)
    broker.work(agent, at(DAY, 10, 30))
    assert o.fill_minute.startswith("2026-01-06T10:00")
    assert o.fills[0]["bar_price"] == pytest.approx(1.04)
    assert "no opening auction price" in o.fill_basis and why in o.fill_basis
    kept = json.loads(broker.minutes._auction_path("AAA", DAY).read_text(encoding="utf-8"))
    assert kept["available"] is False and why in kept["reason"]


def test_a_missing_daily_bar_holds_the_day_until_the_wait_is_over(broker, agent, monkeypatch):
    """While the feed still holds the day, a missing daily bar may yet arrive: nothing in the
    ticker is worked from the auction on. After the wait (30 minutes past the feed delay,
    10:52) the orders fill at the first traded minute, labelled. Fails on aab9e55, which
    filled at 10:30 without waiting."""
    bars_on(broker, "AAA", DAY, OPEN_BARS)
    daily(monkeypatch, {})
    monkeypatch.setattr(M, "_feed_holds", lambda day: True, raising=False)
    o = submit(broker, agent, at(DAY, 9, 30), ticker="AAA", side="buy", qty=3000, limit=1.10)
    broker.work(agent, at(DAY, 10, 30))
    assert o.filled_qty == 0 and o.status == "pending_fill"
    broker.work(agent, at(DAY, 10, 53))
    assert o.status == "filled" and o.fill_minute.startswith("2026-01-06T10:00")
    assert "no opening auction price" in o.fill_basis and "by 10:52" in o.fill_basis


def test_a_failed_check_on_a_day_the_feed_holds_is_read_again_until_the_wait_is_over(
    broker, agent, monkeypatch
):
    """Early in the day Yahoo's daily bar can lag the minute bars, so a daily volume not yet
    above the minute volumes is read again each pass, not refused for good. Here it catches
    up by 10:40 and the order fills at the auction."""
    bars_on(broker, "AAA", DAY, OPEN_BARS)
    monkeypatch.setattr(M, "_feed_holds", lambda day: True, raising=False)
    daily(monkeypatch, {("AAA", DAY): row(volume=MINUTE_VOLUME - 5_000)})
    o = submit(broker, agent, at(DAY, 9, 30), ticker="AAA", side="buy", qty=3000, limit=1.10)
    broker.work(agent, at(DAY, 10, 30))
    assert o.filled_qty == 0 and not broker.minutes._auction_path("AAA", DAY).exists()
    daily(monkeypatch, {("AAA", DAY): row()})
    broker.work(agent, at(DAY, 10, 40))
    assert o.fill_minute.startswith("2026-01-06T09:59")
    assert o.fills[0]["bar_price"] == pytest.approx(1.02)


def test_a_check_still_failing_at_the_end_of_the_wait_is_refused_and_kept(
    broker, agent, monkeypatch
):
    bars_on(broker, "AAA", DAY, OPEN_BARS)
    monkeypatch.setattr(M, "_feed_holds", lambda day: True, raising=False)
    daily(monkeypatch, {("AAA", DAY): row(volume=MINUTE_VOLUME)})
    o = submit(broker, agent, at(DAY, 9, 30), ticker="AAA", side="buy", qty=3000, limit=1.10)
    broker.work(agent, at(DAY, 10, 53))
    assert o.fill_minute.startswith("2026-01-06T10:00")
    assert "cannot be measured" in o.fill_basis
    kept = json.loads(broker.minutes._auction_path("AAA", DAY).read_text(encoding="utf-8"))
    assert kept["available"] is False


def test_a_fill_at_the_auction_is_alerted_as_the_opening_auction(broker, agent, monkeypatch):
    from asxbot.arena import notify

    sent = []
    monkeypatch.setattr(notify.Notifier, "send", lambda self, text, *a, **k: sent.append(text),
                        raising=False)  # fmt: skip
    bars_on(broker, "AAA", DAY, OPEN_BARS)
    daily(monkeypatch, {("AAA", DAY): row()})
    o = submit(broker, agent, at(DAY, 9, 30), ticker="AAA", side="buy", qty=3000, limit=1.10)
    broker.work(agent, at(DAY, 10, 30))
    notify.Notifier.filled(object.__new__(notify.Notifier), o, False, None)
    assert sent and "(opening auction)" in sent[0]


def test_an_auction_once_read_is_never_read_again(cfg, broker, agent, monkeypatch):
    """Both accounts, and every later pass, see the same auction."""
    bars_on(broker, "AAA", DAY, OPEN_BARS)
    calls = daily(monkeypatch, {("AAA", DAY): row()})
    submit(broker, agent, at(DAY, 9, 30), ticker="AAA", side="buy", qty=3000, limit=1.10)
    broker.work(agent, at(DAY, 10, 30))
    daily(monkeypatch, {("AAA", DAY): row(open_=1.03)})  # the vendor revises its open
    bot = broker.store.open("t__bot", "asx_announcements", "bot", 1, 20_000.0)
    o = submit(broker, bot, at(DAY, 9, 45), ticker="AAA", side="buy", qty=3000, limit=1.10)
    broker.work(bot, at(DAY, 10, 40))
    assert o.fills[0]["bar_price"] == pytest.approx(1.02)
    assert len(calls) == 1
    kept = json.loads(broker.minutes._auction_path("AAA", DAY).read_text(encoding="utf-8"))
    assert kept["price"] == pytest.approx(1.02) and kept["volume"] == pytest.approx(100_000)


def test_first_minute_restores_the_rule_before_the_auction(cfg, monkeypatch):
    broker = make_broker(cfg, opening_auction="first_minute")
    acct = broker.store.open("t__agent", "asx_announcements", "agent", 1, 20_000.0)
    bars_on(broker, "AAA", DAY, OPEN_BARS)
    calls = daily(monkeypatch, {("AAA", DAY): row()})
    o = submit(broker, acct, at(DAY, 9, 30), ticker="AAA", side="buy", qty=3000, limit=1.10)
    broker.work(acct, at(DAY, 10, 30))
    assert o.fill_minute.startswith("2026-01-06T10:00") and not calls


def test_the_arena_broker_is_built_with_the_auction_settings_from_config(cfg):
    from asxbot.arena.runtime import arena_broker

    b = arena_broker(cfg)
    fill = cfg.get("arena.fill")
    assert b.opening_auction == fill["opening_auction"] == "daily_open"
    assert b.auction_volume_share == pytest.approx(fill["auction_volume_share"])
    assert b.auction_wait_minutes == int(fill["auction_wait_minutes"])


def test_an_auction_fill_passes_the_fill_before_order_check(broker, agent, monkeypatch):
    """The auction is stamped 09:59 and an order must be recorded before 09:59:00 to join
    it, so the self-check that no fill predates its order holds."""
    from asxbot.arena.selfcheck import check_fills_after_orders

    bars_on(broker, "AAA", DAY, OPEN_BARS)
    daily(monkeypatch, {("AAA", DAY): row()})
    o = submit(broker, agent, at(DAY, 9, 58, 59), ticker="AAA", side="buy", qty=3000,
               limit=1.10)  # fmt: skip
    broker.work(agent, at(DAY, 10, 30))
    assert o.fill_minute.startswith("2026-01-06T09:59")

    class Arena:
        def account(self, pb, kind):
            return agent

    assert check_fills_after_orders(Arena(), None).ok
