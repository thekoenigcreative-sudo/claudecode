"""Fills take the bar's volume into account (TRACKER #25, 2026-09-24). No network.

On 23 Sep A1M's 3,000-share take-profit (ARN-000003) filled in the 15:57 bar, where 1 share
traded. Every arena fill now takes at most `max_volume_share` of a bar's traded volume
(default 20%); the rest carries to later bars at their prices.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from asxbot.arena.accounts import Position
from asxbot.arena.broker import ArenaBroker
from asxbot.arena.minutes import MinuteBars
from asxbot.arena.orders import ArenaOrderRefused, arena_place_order
from asxbot.backtest.costs import CostModel
from asxbot.config import load_config
from asxbot.io import write_parquet_atomic
from v1_playbook import v1_playbook

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


def bars_on(broker, ticker, day, rows, forming=True):
    """rows: (HH, MM, open, high, low, close, volume). Like the real feed, a zero-volume row
    for the minute still forming follows them unless `forming` is False (#26)."""
    if forming:
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


@pytest.fixture
def cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )


def make_broker(cfg, share=0.20, settle=0):
    return ArenaBroker(
        cfg.data_dir, CostModel.from_config(cfg), MinuteBars(cfg.data_dir, "close"),
        lambda t: ADV, resolve_after_minutes=22, clock=Clock(at(DAY, 9, 0)),
        max_volume_share=share, settle_minutes=settle,
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


def a1m_position(acct, qty=3000):
    """A1M as it stood at 15:56 on 23 Sep: 3,000 bought at 0.8358, target 0.90 armed at
    15:56, stop 0.755."""
    acct.positions["AAA"] = Position(
        ticker="AAA", qty=qty, avg_cost=0.8358, opened_at=at(DAY, 10, 41).isoformat(
            timespec="minutes"),
        stop=0.755, target=0.90, opened_by="agent", target_from=at(DAY, 15, 56).isoformat(
            timespec="minutes"), target_past_when_armed=True, worked_through=at(
            DAY, 15, 56).isoformat(timespec="minutes"),
    )  # fmt: skip


# The cached A1M bars of 23 Sep from 15:57, as they were: 1 share traded in the 15:57 bar.
A1M_CLOSE = [
    (15, 57, 0.917, 0.917, 0.917, 0.917, 1),
    (15, 58, 0.915, 0.915, 0.915, 0.915, 37),
    (15, 59, 0.915, 0.917, 0.915, 0.915, 2228),
    (16, 0, 0.915, 0.915, 0.915, 0.915, 7162),
    (16, 10, 0.920, 0.920, 0.920, 0.920, 513634),
]


def test_a_target_cannot_sell_more_than_a_fifth_of_any_bar(broker, agent):
    """ARN-000003: 3,000 shares sold in a bar where 1 traded. Fails on ceefbfe, which
    filled all 3,000 in the 15:57 bar."""
    bars_on(broker, "AAA", DAY, A1M_CLOSE)
    a1m_position(agent)
    broker.work(agent, at(DAY, 16, 40))
    (o,) = agent.orders.values()
    assert o.order_type == "target" and o.status == "filled" and o.filled_qty == 3000
    by_minute = {f["minute"][11:16]: f["qty"] for f in o.fills}
    # 20% of 1 share rounds down to nothing; then 7, 445, 1,432 and the rest at the auction.
    assert "15:57" not in by_minute
    assert by_minute == {"15:58": 7, "15:59": 445, "16:00": 1432, "16:10": 1116}
    for f in o.fills:
        assert f["qty"] <= 0.20 * f["bar_volume"]
    assert o.fill_minute.startswith(f"{DAY.isoformat()}T15:58")
    s = slip(broker, 3000 * 0.917)  # sized from the bar that reached the target
    expect = (1884 * 0.915 + 1116 * 0.920) / 3000 * (1 - s)
    assert o.avg_price == pytest.approx(expect, abs=1e-4)
    assert "AAA" not in agent.positions
    assert "filled over 4 bars" in o.fill_basis


def test_one_brokerage_minimum_per_order_per_day_not_per_bar(broker, agent):
    bars_on(broker, "AAA", DAY, A1M_CLOSE)
    a1m_position(agent)
    broker.work(agent, at(DAY, 16, 40))
    (o,) = agent.orders.values()
    value = sum(f["qty"] * f["price"] for f in o.fills)
    assert o.commission == pytest.approx(broker.costs.brokerage(value), abs=0.01)
    assert o.commission == pytest.approx(6.60)  # one minimum, not four
    assert agent.fees_paid == pytest.approx(6.60)


def test_a_thin_entry_fills_across_bars_and_a_day_order_ends_partial(broker, agent):
    """The rest of a day order expires with its session; the position keeps what filled."""
    bars_on(broker, "AAA", DAY, [
        (10, 0, 1.00, 1.00, 1.00, 1.00, 1000),   # 200
        (10, 1, 1.00, 1.00, 1.00, 1.00, 500),    # 100
        (10, 2, 1.10, 1.10, 1.10, 1.10, 50000),  # above the limit: nothing
        (16, 10, 1.00, 1.00, 1.00, 1.00, 2000),  # 400 in the auction
    ])  # fmt: skip
    o = submit(broker, agent, at(DAY, 9, 30), ticker="AAA", side="buy", qty=3000, limit=1.01,
               stop=0.90)  # fmt: skip
    broker.work(agent, at(DAY, 10, 5))
    assert o.status == "pending_fill" and o.filled_qty == 300
    assert agent.positions["AAA"].qty == 300  # the filled part is already a position
    broker.work(agent, at(DAY, 16, 20))  # the close is in, the feed has not caught up yet
    assert o.status == "pending_fill" and o.filled_qty == 700
    broker.work(agent, at(DAY, 16, 33))
    assert o.status == "partial" and o.filled_qty == 700 and o.remaining == 2300
    assert agent.positions["AAA"].qty == 700
    assert "expired with the day" in o.message
    # Nothing more fills tomorrow: it was a day order.
    bars_on(broker, "AAA", NEXT, [(10, 0, 1.00, 1.00, 1.00, 1.00, 1_000_000)])
    broker.work(agent, at(NEXT, 11, 0))
    assert o.filled_qty == 700 and agent.positions["AAA"].qty == 700


def test_a_stop_exit_still_short_at_the_close_keeps_working_next_session(broker, agent):
    bars_on(broker, "AAA", DAY, [
        (10, 0, 1.00, 1.00, 1.00, 1.00, 100_000),
        (15, 0, 0.95, 0.95, 0.85, 0.86, 1000),   # the stop: 200 at 0.90
        (16, 10, 0.86, 0.86, 0.86, 0.86, 500),   # 100
    ])  # fmt: skip
    bars_on(broker, "AAA", NEXT, [
        (10, 0, 0.80, 0.81, 0.79, 0.80, 100_000),  # the rest, at the next open
    ])  # fmt: skip
    submit(broker, agent, at(DAY, 9, 30), ticker="AAA", side="buy", qty=1000, limit=1.01,
           stop=0.90)  # fmt: skip
    broker.work(agent, at(DAY, 16, 40))
    stop = next(o for o in agent.orders.values() if o.order_type == "stop")
    assert stop.status == "pending_fill" and stop.filled_qty == 300
    assert agent.positions["AAA"].qty == 700  # still held, and still being sold
    assert stop.fills[0]["bar_price"] == pytest.approx(0.90)  # the stop, not the open
    assert stop.fills[1]["bar_price"] == pytest.approx(0.86)  # a market order after that
    broker.work(agent, at(NEXT, 9, 0))  # the evening routine and a restart change nothing
    assert stop.status == "pending_fill"
    broker.work(agent, at(NEXT, 10, 30))
    assert stop.status == "filled" and stop.filled_qty == 1000
    assert "AAA" not in agent.positions
    assert stop.fills[-1]["minute"].startswith(f"{NEXT.isoformat()}T10:00")
    # A brokerage minimum on each day it filled.
    assert stop.commission == pytest.approx(13.20)


def test_a_stop_takes_over_from_a_target_still_filling_and_gets_the_volume_first(broker, agent):
    bars_on(broker, "AAA", DAY, [
        (10, 0, 1.00, 1.00, 1.00, 1.00, 100_000),
        (11, 0, 1.10, 1.12, 1.10, 1.11, 1000),   # target 1.10: 200 sold
        (11, 1, 1.00, 1.00, 0.85, 0.86, 2000),   # stop 0.90: the stop takes 400 of 400
        (11, 2, 0.86, 0.86, 0.86, 0.86, 1_000_000),
    ])  # fmt: skip
    submit(broker, agent, at(DAY, 9, 30), ticker="AAA", side="buy", qty=1000, limit=1.01,
           stop=0.90, target=1.10)  # fmt: skip
    broker.work(agent, at(DAY, 12, 0))
    target = next(o for o in agent.orders.values() if o.order_type == "target")
    stop = next(o for o in agent.orders.values() if o.order_type == "stop")
    assert target.status == "partial" and target.filled_qty == 200
    assert "stop" in target.message
    assert [f["qty"] for f in stop.fills] == [400, 400]
    assert stop.status == "filled" and stop.qty == 800
    assert "AAA" not in agent.positions


def test_one_bar_reaching_both_takes_the_stop_and_the_target_gets_nothing(broker, agent):
    bars_on(broker, "AAA", DAY, [
        (10, 0, 1.00, 1.00, 1.00, 1.00, 100_000),
        (11, 0, 1.00, 1.15, 0.85, 0.95, 100_000),
    ])  # fmt: skip
    submit(broker, agent, at(DAY, 9, 30), ticker="AAA", side="buy", qty=1000, limit=1.01,
           stop=0.90, target=1.10)  # fmt: skip
    broker.work(agent, at(DAY, 12, 0))
    kinds = sorted(o.order_type for o in agent.orders.values())
    assert kinds == ["limit", "stop"]


def test_orders_in_one_ticker_share_the_bar_and_the_stop_goes_first(broker, agent):
    """A stop exit and the agent's own sell in the same bar do not each get 20%."""
    bars_on(broker, "AAA", DAY, [
        (10, 0, 1.00, 1.00, 1.00, 1.00, 100_000),
        (11, 0, 1.00, 1.00, 1.00, 1.00, 100_000),
        (11, 5, 0.95, 0.95, 0.85, 0.86, 1000),
    ])  # fmt: skip
    submit(broker, agent, at(DAY, 9, 30), ticker="AAA", side="buy", qty=1000, limit=1.01,
           stop=0.90)  # fmt: skip
    broker.work(agent, at(DAY, 10, 1))
    sell = submit(broker, agent, at(DAY, 11, 4, 30), ticker="AAA", side="sell", qty=1000,
                  limit=0.50)  # fmt: skip
    broker.work(agent, at(DAY, 11, 30))
    stop = next(o for o in agent.orders.values() if o.order_type == "stop")
    # The stop cancels the agent's working sell and takes the 200 the bar allows.
    assert sell.status == "cancelled" and sell.filled_qty == 0
    assert stop.filled_qty == 200


def test_the_two_accounts_do_not_share_a_bar(broker, agent):
    bot = broker.store.open("t__bot", "asx_announcements", "bot", 1, 20_000.0)
    bars_on(broker, "AAA", DAY, [(10, 0, 1.00, 1.00, 1.00, 1.00, 1000)])
    a = submit(broker, agent, at(DAY, 9, 30), ticker="AAA", side="buy", qty=200, limit=1.01,
               stop=0.90)  # fmt: skip
    b = submit(broker, bot, at(DAY, 9, 31), ticker="AAA", side="buy", qty=200, limit=1.01,
               stop_pct=8.0, stop=0.90)  # fmt: skip
    broker.work(agent, at(DAY, 10, 1))
    broker.work(bot, at(DAY, 10, 1))
    assert a.filled_qty == 200 and b.filled_qty == 200


def test_a_stop_pct_is_measured_from_the_average_fill(broker):
    bot = broker.store.open("t__bot", "asx_announcements", "bot", 1, 20_000.0)
    bars_on(broker, "AAA", DAY, [
        (10, 0, 1.00, 1.00, 1.00, 1.00, 1000),
        (10, 1, 1.02, 1.02, 1.02, 1.02, 100_000),
    ])  # fmt: skip
    o = submit(broker, bot, at(DAY, 9, 30), ticker="AAA", side="buy", qty=1000, limit=1.05,
               stop_pct=8.0, stop=0.90)  # fmt: skip
    broker.work(bot, at(DAY, 10, 5))
    assert o.status == "filled"
    assert bot.positions["AAA"].stop == pytest.approx(o.avg_price * 0.92, abs=1e-4)


def test_a_bar_is_not_used_until_it_has_ended(broker, agent):
    bars_on(broker, "AAA", DAY, [(10, 0, 1.00, 1.00, 1.00, 1.00, 100_000)])
    o = submit(broker, agent, at(DAY, 9, 30), ticker="AAA", side="buy", qty=100, limit=1.01,
               stop=0.90)  # fmt: skip
    broker.work(agent, at(DAY, 10, 0, 40))  # the 10:00 bar is still forming
    assert o.filled_qty == 0
    broker.work(agent, at(DAY, 10, 1))
    assert o.status == "filled"


def test_the_newest_row_of_an_intraday_fetch_is_never_used(broker, agent):
    """Yahoo's newest row is the minute still forming (#26): it waits for the next row."""
    bars_on(broker, "AAA", DAY, [(10, 0, 1.00, 1.00, 1.00, 1.00, 100_000)], forming=False)
    o = submit(broker, agent, at(DAY, 9, 30), ticker="AAA", side="buy", qty=100, limit=1.01,
               stop=0.90)  # fmt: skip
    broker.work(agent, at(DAY, 10, 25))
    assert o.filled_qty == 0
    bars_on(broker, "AAA", DAY, [
        (10, 0, 1.00, 1.00, 1.00, 1.00, 100_000),
        (10, 1, 1.00, 1.00, 1.00, 1.00, 0),  # the next row appears: 10:00 is final
    ], forming=False)  # fmt: skip
    broker.work(agent, at(DAY, 10, 26))
    assert o.status == "filled" and o.fill_minute.endswith("10:00+11:00")


def test_the_newest_row_counts_once_the_close_is_in(broker, agent):
    bars_on(broker, "AAA", DAY, [(15, 0, 1.00, 1.00, 1.00, 1.00, 100)], forming=False)
    o = submit(broker, agent, at(DAY, 14, 0), ticker="AAA", side="buy", qty=100, limit=1.01,
               stop=0.90)  # fmt: skip
    broker.work(agent, at(DAY, 15, 30))
    assert o.filled_qty == 0  # 15:00 is the newest row, intraday
    broker.work(agent, at(DAY, 16, 32))  # the close plus the feed's delay: the day is done
    assert o.filled_qty == 20  # 20% of 100


def test_settle_minutes_holds_back_rows_near_the_newest(cfg):
    broker = make_broker(cfg, settle=3)
    acct = broker.store.open("t__agent", "asx_announcements", "agent", 1, 20_000.0)
    rows = [(10, m, 1.00, 1.00, 1.00, 1.00, 100) for m in range(0, 6)]
    bars_on(broker, "AAA", DAY, rows, forming=False)
    o = submit(broker, acct, at(DAY, 9, 30), ticker="AAA", side="buy", qty=100, limit=1.01,
               stop=0.90)  # fmt: skip
    broker.work(acct, at(DAY, 10, 30))
    # Newest 10:05; 10:02 and later are within 3 minutes of it. 10:00 and 10:01 fill.
    assert [f["minute"][11:16] for f in o.fills] == ["10:00", "10:01"]


def test_an_order_recorded_after_the_close_works_in_the_next_session(broker, agent):
    agent.positions["AAA"] = Position(
        ticker="AAA", qty=500, avg_cost=1.0, opened_at=at(DAY, 10, 0).isoformat(timespec="minutes")
    )
    bars_on(broker, "AAA", DAY, [(16, 10, 1.00, 1.00, 1.00, 1.00, 100_000)])
    bars_on(broker, "AAA", NEXT, [(10, 0, 1.00, 1.00, 1.00, 1.00, 100_000)])
    o = submit(broker, agent, at(DAY, 16, 30), ticker="AAA", side="sell", qty=500, limit=0.90)
    broker.work(agent, at(DAY, 17, 0))  # after the day's close + delay: not its session
    assert o.status == "pending_fill"
    broker.work(agent, at(NEXT, 10, 30))
    assert o.status == "filled" and o.fill_minute.startswith(f"{NEXT.isoformat()}T10:00")


def test_the_share_comes_from_config(cfg):
    from asxbot.arena.runtime import arena_broker

    assert cfg.get("arena.fill.max_volume_share") == 0.20
    assert arena_broker(cfg).max_volume_share == pytest.approx(0.20)


def test_an_exit_cannot_oversell_what_a_working_exit_already_covers(broker, agent, cfg):
    pb = v1_playbook(cfg)
    bars_on(broker, "AAA", DAY, [
        (10, 0, 1.00, 1.00, 1.00, 1.00, 100_000),
        (15, 0, 0.95, 0.95, 0.85, 0.86, 1000),
    ])  # fmt: skip
    submit(broker, agent, at(DAY, 9, 30), ticker="AAA", side="buy", qty=1000, limit=1.01,
           stop=0.90)  # fmt: skip
    broker.work(agent, at(DAY, 15, 10))
    assert agent.positions["AAA"].qty == 800  # the stop sold 200, 800 still working
    broker.clock = Clock(at(DAY, 15, 20))
    with pytest.raises(ArenaOrderRefused, match="already working to close"):
        arena_place_order(
            cfg, broker, agent, pb, ticker="AAA", side="sell", qty=800, limit=0.80,
            reason="close", model="t", placed_by="agent", now=at(DAY, 15, 20),
        )  # fmt: skip


def test_the_legacy_single_bar_rule_would_have_filled_arn000003_in_one_share(broker, agent):
    """What the bug was, stated as a test: without the volume cap, a 1-share bar takes it."""
    bars_on(broker, "AAA", DAY, A1M_CLOSE)
    a1m_position(agent)
    wide = make_broker_like(broker, share=1.0)
    wide.work(agent, at(DAY, 16, 40))
    (o,) = agent.orders.values()
    assert o.fills[0]["minute"][11:16] == "15:57" and o.fills[0]["qty"] == 1


def make_broker_like(broker, share):
    b = ArenaBroker(
        broker.store.root.parent, broker.costs, broker.minutes, broker.adv_lookup,
        resolve_after_minutes=22, clock=broker.clock, max_volume_share=share,
    )  # fmt: skip
    return b


def test_a_stop_exit_carried_overnight_is_not_called_stuck(broker, agent, cfg):
    from asxbot.arena.selfcheck import check_pending_orders

    bars_on(broker, "AAA", DAY, [
        (10, 0, 1.00, 1.00, 1.00, 1.00, 100_000),
        (15, 0, 0.95, 0.95, 0.85, 0.86, 1000),
        (16, 10, 0.86, 0.86, 0.86, 0.86, 500),
    ])  # fmt: skip
    submit(broker, agent, at(DAY, 9, 30), ticker="AAA", side="buy", qty=1000, limit=1.01,
           stop=0.90)  # fmt: skip
    broker.work(agent, at(DAY, 16, 40))
    assert any(o.order_type == "stop" and o.working for o in agent.orders.values())

    class _Arena:
        def account(self, pb, kind):
            return agent

    _Arena.broker = broker
    assert check_pending_orders(_Arena(), None, at(DAY, 19, 0)).ok  # the evening
    assert check_pending_orders(_Arena(), None, at(NEXT, 10, 30)).ok  # the next morning
    assert not check_pending_orders(_Arena(), None, at(NEXT, 12, 0)).ok  # no bars since
