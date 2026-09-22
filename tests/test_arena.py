"""The arena: the deferred fill rule, the hard limits, the scoreboard. No network.

The fill rule is the part most worth testing, because it is the part that decides whether
the warm-up's results mean anything: a decision must never be filled at a price from
before the decision was made.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from asxbot.arena.accounts import AccountStore, Mark
from asxbot.arena.agents import parse_decision, parse_verdict
from asxbot.arena.broker import ArenaBroker
from asxbot.arena.levels import load_playbook
from asxbot.arena.minutes import MinuteBars, NoTradeYet
from asxbot.arena.orders import ArenaOrderRefused, arena_place_order
from asxbot.arena.scoreboard import score
from asxbot.backtest.costs import CostModel
from asxbot.config import load_config
from asxbot.io import write_parquet_atomic

SYD = ZoneInfo("Australia/Sydney")
# A weekday well in the past, so the minute store never reaches for the network.
DAY = date(2026, 1, 6)  # a Tuesday


def minute_frame(rows):
    """rows: list of (HH, MM, open, high, low, close, volume)"""
    idx = [datetime(DAY.year, DAY.month, DAY.day, h, m, tzinfo=SYD) for h, m, *_ in rows]
    return pd.DataFrame(
        {
            "open": [r[2] for r in rows],
            "high": [r[3] for r in rows],
            "low": [r[4] for r in rows],
            "close": [r[5] for r in rows],
            "volume": [r[6] for r in rows],
        },
        index=pd.DatetimeIndex(idx, name="Datetime"),
    )


@pytest.fixture
def cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )


@pytest.fixture
def bars(cfg):
    mb = MinuteBars(cfg.data_dir, "close")
    rows = [
        (10, 0, 1.00, 1.05, 1.00, 1.04, 50_000),
        (10, 1, 1.04, 1.06, 1.03, 1.05, 10_000),
        (10, 2, 1.05, 1.05, 1.05, 1.05, 0),  # quoted but no trade
        (10, 3, 1.05, 1.05, 1.05, 1.05, 0),  # still no trade
        (10, 4, 1.06, 1.12, 1.06, 1.11, 80_000),  # the next minute that traded
        (10, 5, 1.11, 1.11, 0.90, 0.95, 40_000),  # a plunge, for the stop test
        (16, 10, 0.96, 0.96, 0.96, 0.96, 500_000),  # closing auction
    ]
    write_parquet_atomic(minute_frame(rows), mb._path("AAA", DAY))
    return mb


# -- the fill rule ---------------------------------------------------------
def test_fill_uses_the_decision_minute(bars):
    f = bars.fill_at("AAA", datetime(2026, 1, 6, 10, 1, 30, tzinfo=SYD))
    assert f.minute.hour == 10 and f.minute.minute == 1
    assert f.price == pytest.approx(1.05)  # the close of that minute
    assert f.minutes_waited == 0


def test_a_minute_with_no_trade_walks_forward_never_back(bars):
    f = bars.fill_at("AAA", datetime(2026, 1, 6, 10, 2, 10, tzinfo=SYD))
    # 10:02 and 10:03 had no trade, so the fill is 10:04 - not 10:01.
    assert (f.minute.hour, f.minute.minute) == (10, 4)
    assert f.price == pytest.approx(1.11)
    assert f.minutes_waited == 2


def test_a_decision_before_the_open_fills_at_the_open(bars):
    f = bars.fill_at("AAA", datetime(2026, 1, 6, 7, 30, tzinfo=SYD))
    assert (f.minute.hour, f.minute.minute) == (10, 0)
    assert f.price == pytest.approx(1.04)


def test_no_traded_minute_yet_raises_rather_than_guessing(bars):
    with pytest.raises(NoTradeYet):
        bars.fill_at("AAA", datetime(2026, 1, 6, 16, 30, tzinfo=SYD))


def test_stop_trigger_uses_the_bar_that_reached_it(bars):
    hit = bars.first_trigger("AAA", datetime(2026, 1, 6, 10, 0, tzinfo=SYD), 0.98, "down")
    assert hit is not None
    ts, px = hit
    assert (ts.hour, ts.minute) == (10, 5)
    assert px == pytest.approx(0.98)  # the stop itself: the bar did not gap through it


def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop(bars):
    # A stop at 1.08 is gapped through: the 10:05 bar opened at 1.11 then fell to 0.90,
    # but the bar that first reaches 1.08 is 10:05, whose open is worse than the stop
    # only if the open is below it. Use a stop above the open to prove the rule.
    hit = bars.first_trigger("AAA", datetime(2026, 1, 6, 10, 5, tzinfo=SYD), 1.20, "down")
    assert hit is not None
    _, px = hit
    assert px == pytest.approx(1.11)  # gapped: filled at the bar's open, not at 1.20


# -- the broker ------------------------------------------------------------
@pytest.fixture
def setup(cfg, bars):
    broker = ArenaBroker(
        cfg.data_dir, CostModel.from_config(cfg), bars, lambda t: 2_000_000.0,
        resolve_after_minutes=0,
    )  # fmt: skip
    pb = load_playbook(cfg, "asx_announcements")
    acct = broker.store.open("t__agent", "asx_announcements", "agent", 1, 10_000.0)
    return cfg, broker, pb, acct


def test_an_order_is_pending_until_it_is_resolved(setup):
    cfg, broker, pb, acct = setup
    o = broker.submit(
        acct, ticker="AAA", side="buy", qty=1000, limit=1.20,
        decision_at=datetime(2026, 1, 6, 10, 1, 30, tzinfo=SYD), stop=1.00,
    )  # fmt: skip
    assert o.status == "pending_fill"
    assert o.avg_price is None  # nothing may claim a fill before one exists

    broker.resolve_pending(acct, now=datetime(2026, 1, 6, 11, 0, tzinfo=SYD))
    done = acct.orders[o.order_id]
    assert done.status == "filled"
    assert done.avg_price == pytest.approx(1.05 * 1.001, rel=1e-3)  # close + slippage
    assert acct.positions["AAA"].qty == 1000
    assert "10:01" in done.fill_basis


def test_a_limit_the_market_never_reached_expires(setup):
    cfg, broker, pb, acct = setup
    o = broker.submit(
        acct, ticker="AAA", side="buy", qty=100, limit=0.50,  # far below the market
        decision_at=datetime(2026, 1, 6, 10, 1, tzinfo=SYD), stop=0.40,
    )  # fmt: skip
    broker.resolve_pending(acct, now=datetime(2026, 1, 7, 11, 0, tzinfo=SYD))
    assert acct.orders[o.order_id].status == "expired"
    assert "AAA" not in acct.positions


def test_the_stop_is_enforced_by_code(setup):
    cfg, broker, pb, acct = setup
    o = broker.submit(
        acct, ticker="AAA", side="buy", qty=1000, limit=1.20,
        decision_at=datetime(2026, 1, 6, 10, 0, tzinfo=SYD), stop=0.98,
    )  # fmt: skip
    broker.resolve_pending(acct, now=datetime(2026, 1, 6, 11, 0, tzinfo=SYD))
    assert acct.orders[o.order_id].status == "filled"
    out = broker.apply_stops(acct, now=datetime(2026, 1, 6, 16, 0, tzinfo=SYD))
    assert any(r.status == "filled" for r in out)
    assert "AAA" not in acct.positions  # the stop closed it


# -- the hard limits -------------------------------------------------------
def _place(cfg, broker, acct, pb, **kw):
    kw.setdefault("universe", {"AAA", "BBB"})
    kw.setdefault("short_universe", {"BBB"})
    kw.setdefault("now", datetime(2026, 1, 6, 11, 0, tzinfo=SYD))
    return arena_place_order(cfg, broker, acct, pb, **kw)


def test_risk_per_trade_is_capped_by_the_level(setup):
    cfg, broker, pb, acct = setup
    # Level 1 allows 5% of 10,000 = 500 of risk. A 0.60 stop distance on 1000 shares = 600.
    with pytest.raises(ArenaOrderRefused, match="risk"):
        _place(cfg, broker, acct, pb, ticker="AAA", side="buy", qty=1000, limit=1.60, stop=1.00)


def test_an_opening_trade_without_a_stop_is_refused(setup):
    cfg, broker, pb, acct = setup
    with pytest.raises(ArenaOrderRefused, match="needs a stop"):
        _place(cfg, broker, acct, pb, ticker="AAA", side="buy", qty=500, limit=1.20, stop=None)


def test_shorts_are_refused_outside_the_asx_200(setup):
    cfg, broker, pb, acct = setup
    with pytest.raises(ArenaOrderRefused, match="ASX 200"):
        _place(cfg, broker, acct, pb, ticker="AAA", side="short", qty=500, limit=1.20, stop=1.30)
    # BBB is in the short universe, so the same order is allowed there.
    o = _place(cfg, broker, acct, pb, ticker="BBB", side="short", qty=500, limit=1.20, stop=1.30)
    assert o.order_id.startswith("ARN-")


def test_leverage_is_capped_by_the_level(setup):
    cfg, broker, pb, acct = setup
    # 3x of 10,000 is 30,000, but the per-order guard is 8,000, so build up positions.
    with pytest.raises(ArenaOrderRefused, match="exceeds the guard"):
        _place(cfg, broker, acct, pb, ticker="AAA", side="buy", qty=10_000, limit=1.20, stop=1.19)


def test_the_daily_loss_limit_blocks_new_positions_but_never_exits(setup):
    cfg, broker, pb, acct = setup
    store = AccountStore(cfg.data_dir)
    # Yesterday closed at 10,000; today the account is worth 8,000: a 20% loss, past 15%.
    store.write_mark(acct.name, Mark(day="2026-01-05", equity=10_000.0, cash=10_000.0,
                                     gross_exposure=0.0, positions=0, realised_day=0.0,
                                     fees_day=0.0))  # fmt: skip
    acct.cash = 8_000.0
    with pytest.raises(ArenaOrderRefused, match="daily loss limit"):
        _place(cfg, broker, acct, pb, ticker="AAA", side="buy", qty=500, limit=1.20, stop=1.15)

    # An exit is still allowed, because blocking exits would increase risk.
    from asxbot.arena.accounts import Position

    acct.positions["AAA"] = Position(
        ticker="AAA", qty=500, avg_cost=1.10, opened_at="2026-01-06T10:00"
    )
    o = _place(cfg, broker, acct, pb, ticker="AAA", side="sell", qty=500, limit=1.00)
    assert o.order_id.startswith("ARN-")


def test_adding_to_a_losing_position_is_refused(setup, monkeypatch):
    cfg, broker, pb, acct = setup
    from asxbot.arena.accounts import Position

    # Held at 1.50, currently 0.96: a loss.
    monkeypatch.setattr(broker.minutes, "last_price", lambda t, day=None: 0.96)
    acct.positions["AAA"] = Position(
        ticker="AAA", qty=100, avg_cost=1.50, opened_at="2026-01-06T10:00"
    )
    with pytest.raises(ArenaOrderRefused, match="losing position"):
        _place(cfg, broker, acct, pb, ticker="AAA", side="buy", qty=500, limit=1.00, stop=0.90)


def test_the_arena_refuses_to_run_outside_sim(setup, config_file, tmp_path):
    cfg, broker, pb, acct = setup
    paper = load_config(
        config_file(broker="paper", data={"provider": "yfinance", "dir": str(tmp_path / "d2")}),
        env_file=tmp_path / "none.env",
    )
    with pytest.raises(ArenaOrderRefused, match="fake money only"):
        _place(paper, broker, acct, pb, ticker="AAA", side="buy", qty=500, limit=1.20, stop=1.15)


# -- the scoreboard --------------------------------------------------------
def test_scoreboard_counts_green_and_red_days_and_the_drawdown(cfg):
    store = AccountStore(cfg.data_dir)
    acct = store.open("s__agent", "asx_announcements", "agent", 1, 10_000.0)
    for day, equity in (
        ("2026-01-06", 10_500.0),  # green
        ("2026-01-07", 9_000.0),  # red, and the trough
        ("2026-01-08", 9_900.0),  # green
    ):
        store.write_mark(acct.name, Mark(day=day, equity=equity, cash=equity, gross_exposure=0.0,
                                         positions=0, realised_day=0.0, fees_day=0.0))  # fmt: skip
    acct.cash = 9_900.0
    s = score(store, acct, {})
    assert (s.green_days, s.red_days) == (2, 1)
    assert s.worst_day_pct == pytest.approx(-14.29, abs=0.01)
    assert s.max_drawdown_pct == pytest.approx(14.29, abs=0.01)  # 10,500 -> 9,000
    ok, reasons = s.passes_level_test()
    assert not ok and any("not profitable" in r for r in reasons)


# -- parsing the agents' replies ------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("WHAT IT SAYS: x\nTRADE_WORTHY: YES", True),
        ("TRADE_WORTHY: NO", False),
        ("**TRADE_WORTHY:** YES", True),
        ("no verdict line at all", False),  # a malformed answer must never trade
        ("TRADE_WORTHY: maybe", False),
    ],
)
def test_reader_verdict_defaults_to_no(text, expected):
    assert parse_verdict(text)[0] is expected


def test_an_unparseable_decision_is_a_pass():
    assert parse_decision("I think we should buy a lot of it")["action"] == "pass"
    d = parse_decision('reasoning...\n{"action": "trade", "side": "buy", "qty": 10}')
    assert d["action"] == "trade" and d["qty"] == 10


def test_short_borrow_is_charged_daily(setup):
    cfg, broker, pb, acct = setup
    from asxbot.arena.accounts import Position

    acct.positions["BBB"] = Position(
        ticker="BBB", qty=-1000, avg_cost=1.00, opened_at="2026-01-06T10:00",
        last_borrow_day="2026-01-06",
    )  # fmt: skip
    charged = broker.accrue_borrow(acct, DAY + timedelta(days=10))
    # 1000 shares at ~1.00 (no minute data for BBB, so the average cost is used),
    # 3% a year for 10 days.
    assert charged == pytest.approx(1000 * 1.00 * 0.03 * 10 / 365, rel=1e-6)
    assert acct.borrow_paid == pytest.approx(charged)


def test_a_stop_on_the_wrong_side_of_the_fill_is_re_derived(setup):
    """Deferred fills can land far from the delayed quote a stop was computed against.

    A long that fills below its own stop would be opened and stopped out in the same
    minute, for two lots of brokerage and no trade. The stop's intent is a distance, so
    it is re-derived at the same distance from the price actually paid.
    """
    cfg, broker, pb, acct = setup
    # Limit 1.20 with a stop at 1.10 is an 8.33% stop. The real 10:01 bar closes at 1.05,
    # so the fill lands BELOW the stop.
    o = broker.submit(
        acct, ticker="AAA", side="buy", qty=100, limit=1.20,
        decision_at=datetime(2026, 1, 6, 10, 1, tzinfo=SYD), stop=1.10,
    )  # fmt: skip
    broker.resolve_pending(acct, now=datetime(2026, 1, 6, 11, 0, tzinfo=SYD))
    filled = acct.orders[o.order_id]
    assert filled.status == "filled"
    pos = acct.positions["AAA"]
    assert pos.stop < filled.avg_price, "the stop must end up below a long's entry"
    # Same 8.33% distance, now measured from what was actually paid.
    assert pos.stop == pytest.approx(filled.avg_price * (1 - 0.10 / 1.20), rel=1e-3)
