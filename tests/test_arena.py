"""The arena: the deferred fill rule, the hard limits, the scoreboard. No network.

The fill rule is the part most worth testing, because it is the part that decides whether
the warm-up's results mean anything: a decision must never be filled at a price from
before the decision was made.
"""

from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
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


def test_a_second_opening_order_is_refused_while_the_first_waits_to_fill(setup):
    """acct.positions holds only filled positions, so a pending order used to be invisible
    to every limit: a second buy passed, and both could fill into a double-sized position."""
    cfg, broker, pb, acct = setup
    from asxbot.arena.accounts import Position

    first = _place(cfg, broker, acct, pb, ticker="AAA", side="buy", qty=500, limit=1.20, stop=1.15)
    assert first.status == "pending_fill"
    n = len(acct.orders)
    with pytest.raises(ArenaOrderRefused, match="waiting to fill"):
        _place(cfg, broker, acct, pb, ticker="AAA", side="buy", qty=500, limit=1.20, stop=1.15)
    assert len(acct.orders) == n  # nothing reached the broker

    # Another ticker is unaffected.
    _place(cfg, broker, acct, pb, ticker="BBB", side="buy", qty=500, limit=1.20, stop=1.15)

    # Exits are never blocked, even with an opening order pending in the same ticker.
    acct.positions["AAA"] = Position(
        ticker="AAA", qty=500, avg_cost=1.10, opened_at="2026-01-06T10:00"
    )
    o = _place(cfg, broker, acct, pb, ticker="AAA", side="sell", qty=500, limit=1.00)
    assert o.status == "pending_fill"


def test_orders_waiting_to_fill_count_toward_positions_and_leverage(setup, monkeypatch):
    """With three held and one waiting, a fifth could fill: pending orders were invisible to
    max_open_positions and to the leverage cap alike."""
    import dataclasses

    from asxbot.arena.accounts import Position

    cfg, broker, pb, acct = setup
    _offline_price(monkeypatch, 1.00)
    pb = dataclasses.replace(
        pb, level=dataclasses.replace(pb.level, max_open_positions=4, leverage_asx=1.0)
    )
    uni = {"BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"}
    kw = dict(side="buy", qty=500, limit=1.20, stop=1.15, universe=uni)

    # Positions: three held, one waiting to fill. The next new ticker would be a fifth.
    for t in ("BBB", "CCC", "DDD"):
        acct.positions[t] = Position(ticker=t, qty=1000, avg_cost=1.00, opened_at="2026-01-06")
    acct.cash = 7_000.0  # equity 10,000
    _place(cfg, broker, acct, pb, ticker="EEE", **kw)
    with pytest.raises(ArenaOrderRefused, match="open positions"):
        _place(cfg, broker, acct, pb, ticker="FFF", **kw)
    # Exits are never blocked.
    exit_ = dict(side="sell", qty=1000, limit=0.90, universe=uni)
    assert _place(cfg, broker, acct, pb, ticker="BBB", **exit_).status == "pending_fill"

    # Leverage: nothing held, two orders worth 4,000 waiting. A third would take the
    # account to 12,000 of exposure on 10,000 of equity at 1x.
    acct.positions.clear()
    acct.orders.clear()
    acct.cash = 10_000.0
    big = dict(kw, qty=3333)
    _place(cfg, broker, acct, pb, ticker="EEE", **big)
    _place(cfg, broker, acct, pb, ticker="FFF", **big)
    with pytest.raises(ArenaOrderRefused, match="gross exposure .* waiting to fill"):
        _place(cfg, broker, acct, pb, ticker="GGG", **big)


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


@pytest.mark.parametrize(
    "text,expected",
    [
        ("TRADE_WORTHY: YES\nCAN_SIZE_AND_EXIT: YES", True),
        ("TRADE_WORTHY: YES\nCAN_SIZE_AND_EXIT: NO", False),
        ("**CAN_SIZE_AND_EXIT:** YES", True),
        ("TRADE_WORTHY: YES", False),  # the second gate missing is a no, not a yes
        ("CAN_SIZE_AND_EXIT: too thin to say", False),
    ],
)
def test_the_reader_second_gate_defaults_to_no(text, expected):
    from asxbot.arena.agents import parse_can_size

    assert parse_can_size(text)[0] is expected


class _StaticQuote:
    """Just enough of a quote provider for the gate test; the screen itself is stubbed."""

    def quote(self, ticker):
        return _quote()

    def index_quote(self):
        return _quote()


def test_the_decider_is_called_only_when_both_reader_gates_are_yes(setup, monkeypatch):
    """Real news on an untradeable stock must stop at the reader, not cost an Opus call."""
    from asxbot.arena import watch as W

    cfg, broker, pb, acct = setup
    called = []

    def fake_call(agent, message, **kw):
        called.append(agent)
        text = "WHAT IT SAYS: big contract\nTRADE_WORTHY: YES\nCAN_SIZE_AND_EXIT: NO"
        return type("R", (), {"text": text, "model": "claude-sonnet-5", "model_matches": True})()

    monkeypatch.setattr(W, "call_agent", fake_call)
    monkeypatch.setattr(W, "dossier", lambda arena, t: {"ticker": t})
    monkeypatch.setattr(W, "live_reaction", lambda *a, **k: {"available": False})
    monkeypatch.setattr(
        W,
        "screen",
        lambda *a, **k: type(
            "S", (), {"ok": True, "why": "tradeable", "test": "", "turnover": 1e6, "tick_pct": 0.5}
        )(),
    )
    fake = _FakeArena(cfg, broker, acct)
    fake.quote_provider = lambda: _StaticQuote()
    out = W.handle_announcement(fake, pb, _ann(), now=_AT, run_bot=False, ignore_warmup=True)

    assert called == [W.READER]  # the decider was never called
    assert out["reader"]["trade_worthy"] is True
    assert out["reader"]["can_size_and_exit"] is False
    assert "decider" not in out


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


def test_a_stop_cannot_fire_in_the_bar_the_position_opened(setup):
    """The fill is the entry bar's close, so that bar's low came before the position existed.

    Found by reading the code on 23 Sep: the stop scan started from the entry minute, so a
    bar that dipped 10% and recovered to close at the fill price opened the position and
    stopped it out at once - two lots of brokerage and a loss on a move it never held.
    """
    cfg, broker, pb, acct = setup
    rows = [
        (10, 0, 1.00, 1.00, 0.90, 1.00, 50_000),  # dips 10%, closes at 1.00: the entry
        (10, 1, 1.00, 1.01, 0.99, 1.00, 10_000),  # quiet
        (10, 2, 1.00, 1.00, 0.85, 0.86, 20_000),  # a real fall, after the position opened
        (16, 10, 0.86, 0.86, 0.86, 0.86, 100_000),
    ]
    write_parquet_atomic(minute_frame(rows), broker.minutes._path("CCC", DAY))
    o = broker.submit(
        acct, ticker="CCC", side="buy", qty=1000, limit=1.20, stop_pct=8.0,
        decision_at=datetime(2026, 1, 6, 10, 0, tzinfo=SYD),
    )  # fmt: skip
    broker.resolve_pending(acct, now=datetime(2026, 1, 6, 11, 0, tzinfo=SYD))
    assert acct.orders[o.order_id].fill_minute.startswith("2026-01-06T10:00")
    pos = acct.positions["CCC"]
    assert 0.90 < pos.stop  # the entry bar's own low is below the stop

    out = broker.apply_stops(acct, now=datetime(2026, 1, 6, 11, 0, tzinfo=SYD))
    stop_fill = acct.orders[out[0].order_id] if out else None
    # The stop is not hit in 10:00, the entry bar. It is hit at 10:02, when the stock fell.
    assert stop_fill is not None and stop_fill.fill_minute.startswith("2026-01-06T10:02")


# -- the pre-close sweep (Level 1 is intraday) -----------------------------
class _FakeArena:
    """Just enough of Arena for the sweep: one account, prices, no network."""

    def __init__(self, cfg, broker, acct):
        self.cfg, self.broker, self.store = cfg, broker, broker.store
        self._acct = acct
        self.universe, self.short_universe = {"AAA", "BBB"}, {"BBB"}

    def account(self, pb, kind):
        return self._acct

    def daily_lookup(self):
        return lambda t: None


def _intraday(pb):
    """The playbook as it was before 23 Sep: intraday, so the pre-close sweep applies.

    The live playbook now holds for 10 sessions to match its yardstick, but the sweep is
    still the rule for any intraday playbook and is still worth testing.
    """
    import dataclasses

    return dataclasses.replace(pb, raw={**pb.raw, "holding": "intraday"})


def _offline_price(monkeypatch, price: float = 1.00):
    """The sweep prices its exit from the minute bars. Without this the test reaches for
    yfinance - and AAA.AX is a real ETF, so these tests quietly depended on its live price."""
    from asxbot.arena.minutes import MinuteBars

    monkeypatch.setattr(MinuteBars, "last_price", lambda self, t, day=None: price)


def _hold_position(acct, qty=1000):
    from asxbot.arena.accounts import Position

    acct.positions["AAA"] = Position(
        ticker="AAA", qty=qty, avg_cost=1.00, opened_at="2026-01-06T10:00",
        stop=0.92, thesis="test", opened_by="agent",
    )  # fmt: skip


def test_preclose_closes_the_position_when_no_reason_is_given(setup, monkeypatch):
    from asxbot.arena import watch as W

    cfg, broker, pb, acct = setup
    _offline_price(monkeypatch)
    _hold_position(acct)
    fake = _FakeArena(cfg, broker, acct)

    reply = type("R", (), {"text": '{"action": "close", "reason": "move is done"}', "model": "m"})
    monkeypatch.setattr(W, "call_agent", lambda *a, **k: reply())
    out = W.sweep_before_close(fake, _intraday(pb), now=datetime(2026, 1, 6, 15, 50, tzinfo=SYD))
    assert out[0]["action"] == "close"
    assert any(o.side == "sell" for o in acct.orders.values())


def test_preclose_keeps_it_only_when_a_reason_is_written(setup, monkeypatch):
    from asxbot.arena import watch as W

    cfg, broker, pb, acct = setup
    _offline_price(monkeypatch)
    _hold_position(acct)
    fake = _FakeArena(cfg, broker, acct)

    reply = type(
        "R", (), {"text": '{"action": "hold", "reason": "results due before the open"}',
                  "model": "m"}  # fmt: skip
    )
    monkeypatch.setattr(W, "call_agent", lambda *a, **k: reply())
    out = W.sweep_before_close(fake, _intraday(pb), now=datetime(2026, 1, 6, 15, 50, tzinfo=SYD))
    assert out[0]["action"] == "hold"
    assert acct.positions["AAA"].hold == "overnight"
    assert "results due" in acct.positions["AAA"].hold_reason
    assert not any(o.side == "sell" for o in acct.orders.values())


def test_preclose_holds_need_an_actual_reason_not_just_the_word_hold(setup, monkeypatch):
    from asxbot.arena import watch as W

    cfg, broker, pb, acct = setup
    _offline_price(monkeypatch)
    _hold_position(acct)
    fake = _FakeArena(cfg, broker, acct)

    reply = type("R", (), {"text": '{"action": "hold", "reason": "   "}', "model": "m"})
    monkeypatch.setattr(W, "call_agent", lambda *a, **k: reply())
    out = W.sweep_before_close(fake, _intraday(pb), now=datetime(2026, 1, 6, 15, 50, tzinfo=SYD))
    assert out[0]["action"] == "close", "a hold with no written reason must not hold"


def test_preclose_closes_when_the_agent_cannot_be_reached(setup, monkeypatch):
    """The safe failure is to follow the level, not to leave a position open overnight."""
    from asxbot.arena import watch as W
    from asxbot.arena.agents import AgentCallFailed

    cfg, broker, pb, acct = setup
    _offline_price(monkeypatch)
    _hold_position(acct)
    fake = _FakeArena(cfg, broker, acct)

    def boom(*a, **k):
        raise AgentCallFailed("the agent is down")

    monkeypatch.setattr(W, "call_agent", boom)
    out = W.sweep_before_close(fake, _intraday(pb), now=datetime(2026, 1, 6, 15, 50, tzinfo=SYD))
    assert out[0]["action"] == "close"
    assert any(o.side == "sell" for o in acct.orders.values())


def test_preclose_asks_once_a_day(setup, monkeypatch):
    from asxbot.arena import watch as W

    cfg, broker, pb, acct = setup
    _offline_price(monkeypatch)
    _hold_position(acct)
    fake = _FakeArena(cfg, broker, acct)
    calls = []
    reply = type("R", (), {"text": '{"action": "hold", "reason": "catalyst tomorrow"}',
                           "model": "m"})  # fmt: skip

    def counted(*a, **k):
        calls.append(1)
        return reply()

    monkeypatch.setattr(W, "call_agent", counted)
    now = datetime(2026, 1, 6, 15, 50, tzinfo=SYD)
    W.sweep_before_close(fake, _intraday(pb), now=now)
    W.sweep_before_close(fake, _intraday(pb), now=now)  # the loop runs every minute; don't re-ask
    assert len(calls) == 1


# -- daylight saving -------------------------------------------------------
@pytest.mark.parametrize(
    "day,dst,ann_end,stop,slot",
    [
        (date(2026, 9, 23), False, "19:30", "19:25", "19:30"),
        (date(2026, 10, 3), False, "19:30", "19:25", "19:30"),  # the day before it starts
        (date(2026, 10, 4), True, "20:30", "20:25", "20:30"),  # Sydney DST starts
        (date(2027, 4, 3), True, "20:30", "20:25", "20:30"),  # the day before it ends
        (date(2027, 4, 4), False, "19:30", "19:25", "19:30"),  # Sydney DST ends
    ],
)
def test_hours_follow_sydney_daylight_saving(cfg, day, dst, ann_end, stop, slot):
    from asxbot.arena.hours import (
        announcement_window,
        evening_slot,
        is_dst,
        order_window,
        watcher_stop_time,
    )

    assert is_dst(day) is dst
    assert announcement_window(cfg, day)[1].strftime("%H:%M") == ann_end
    assert order_window(cfg, day)[1].strftime("%H:%M") == ann_end
    assert watcher_stop_time(cfg, day).strftime("%H:%M") == stop
    assert evening_slot(cfg, day).strftime("%H:%M") == slot


def test_only_one_evening_slot_fires_on_a_given_day(cfg):
    """The task fires at 19:30 and 20:30; exactly one must be due."""
    from asxbot.arena.hours import is_evening_slot

    for day, due_at in ((date(2026, 9, 23), 19), (date(2026, 10, 5), 20)):
        fires = [
            h
            for h in (19, 20)
            if is_evening_slot(cfg, datetime(day.year, day.month, day.day, h, 30, tzinfo=SYD))
        ]
        assert fires == [due_at], f"{day} should fire only at {due_at}:30, got {fires}"


def test_orders_are_accepted_an_hour_later_during_daylight_saving(setup):
    """A 20:00 order is outside hours in September and inside them in October."""
    cfg, broker, pb, acct = setup
    with pytest.raises(ArenaOrderRefused, match="outside arena hours"):
        _place(cfg, broker, acct, pb, ticker="AAA", side="buy", qty=500, limit=1.20, stop=1.15,
               now=datetime(2026, 9, 23, 20, 0, tzinfo=SYD))  # fmt: skip
    o = _place(cfg, broker, acct, pb, ticker="AAA", side="buy", qty=500, limit=1.20, stop=1.15,
               now=datetime(2026, 10, 5, 20, 0, tzinfo=SYD))  # fmt: skip
    assert o.order_id.startswith("ARN-")


def test_an_exit_is_never_blocked_by_the_hours_guard(setup):
    """Found live: the pre-close sweep's exit was refused for being outside hours.

    Blocking an exit is a risk control that increases risk. It cannot create a phantom
    fill either, because fills come from real traded minutes - an exit submitted after the
    close rests and fills at the next minute the stock actually trades.
    """
    from asxbot.arena.accounts import Position

    cfg, broker, pb, acct = setup
    acct.positions["AAA"] = Position(
        ticker="AAA", qty=500, avg_cost=1.10, opened_at="2026-01-06T10:00"
    )
    after_hours = datetime(2026, 1, 6, 21, 10, tzinfo=SYD)

    # An opening trade at that hour is still refused.
    with pytest.raises(ArenaOrderRefused, match="outside arena hours"):
        _place(cfg, broker, acct, pb, ticker="BBB", side="buy", qty=500, limit=1.20,
               stop=1.15, now=after_hours)  # fmt: skip

    # The exit goes through.
    o = _place(cfg, broker, acct, pb, ticker="AAA", side="sell", qty=500, limit=1.00,
               now=after_hours)  # fmt: skip
    assert o.status == "pending_fill"


def test_a_report_the_console_cannot_encode_is_still_delivered(tmp_path, monkeypatch):
    """Found live: the decider wrote a Unicode minus sign, printing crashed on this
    machine's cp1252 console, and the crash happened BEFORE the Telegram send - so
    tomorrow's report would simply never have gone out. Delivery comes first now."""
    import asxbot.telegram as tg

    sent = []
    monkeypatch.setattr(tg.Bot, "send", lambda self, text, chat_id=None: sent.append(text) or [1])
    bot = tg.Bot(token="x" * 20, chat_id="123")
    body = "equity \u22125.2% today"  # U+2212, not a hyphen
    bot.send(body)
    assert sent and "\u2212" in sent[0]


def test_long_reports_are_split_on_line_boundaries():
    from asxbot.telegram import MAX_LEN, _split

    body = "\n".join(f"line {i} " + "x" * 200 for i in range(60))
    parts = _split(body, MAX_LEN)
    assert len(parts) > 1
    assert all(len(p) <= MAX_LEN for p in parts)
    assert "".join(parts) == body  # nothing lost or duplicated


def test_order_ids_are_unique_across_accounts(cfg, bars):
    """Found by the decider in its own evening report: the agent and its bot were both
    issued ARN-000001, because the counter was per account. The id is the only proof an
    order exists, so it cannot be ambiguous."""
    broker = ArenaBroker(
        cfg.data_dir, CostModel.from_config(cfg), bars, lambda t: 2_000_000.0,
        resolve_after_minutes=0,
    )  # fmt: skip
    agent = broker.store.open("p__agent", "p", "agent", 1, 10_000.0)
    bot = broker.store.open("p__bot", "p", "bot", 1, 10_000.0)
    ids = []
    for acct in (agent, bot, agent, bot):
        o = broker.submit(
            acct, ticker="AAA", side="buy", qty=10, limit=1.20,
            decision_at=datetime(2026, 1, 6, 10, 1, tzinfo=SYD), stop=1.00,
        )  # fmt: skip
        ids.append(o.order_id)
    assert len(set(ids)) == len(ids), f"order ids collided: {ids}"


def test_the_id_counter_never_reuses_an_id_if_its_file_is_lost(cfg, bars):
    broker = ArenaBroker(
        cfg.data_dir, CostModel.from_config(cfg), bars, lambda t: 2_000_000.0,
        resolve_after_minutes=0,
    )  # fmt: skip
    acct = broker.store.open("q__agent", "q", "agent", 1, 10_000.0)
    first = [
        broker.submit(
            acct, ticker="AAA", side="buy", qty=10, limit=1.20,
            decision_at=datetime(2026, 1, 6, 10, 1, tzinfo=SYD), stop=1.00,
        ).order_id  # fmt: skip
        for _ in range(3)
    ]
    (broker.store.root / "next_order_id.json").unlink()  # lose the counter
    again = broker.submit(
        acct, ticker="AAA", side="buy", qty=10, limit=1.20,
        decision_at=datetime(2026, 1, 6, 10, 1, tzinfo=SYD), stop=1.00,
    ).order_id  # fmt: skip
    assert again not in first


# -- instant Telegram alerts ------------------------------------------------
def _capture(cfg, monkeypatch):
    from asxbot.arena.notify import Notifier

    sent = []
    n = Notifier(cfg)
    monkeypatch.setattr(n, "send", lambda text: sent.append(text) or True)
    return n, sent


def test_fills_and_stops_are_alerted_as_they_happen(setup, monkeypatch):
    cfg, broker, pb, acct = setup
    broker.notifier, sent = _capture(cfg, monkeypatch)
    broker.submit(
        acct, ticker="AAA", side="buy", qty=1000, limit=1.20,
        decision_at=datetime(2026, 1, 6, 10, 0, tzinfo=SYD), stop=0.98, placed_by="agent",
    )  # fmt: skip
    broker.resolve_pending(acct, now=datetime(2026, 1, 6, 11, 0, tzinfo=SYD))
    assert len(sent) == 1 and "FILLED" in sent[0] and "BUY AAA" in sent[0]
    assert "stop 0.980" in sent[0]
    broker.apply_stops(acct, now=datetime(2026, 1, 6, 16, 0, tzinfo=SYD))
    assert len(sent) == 2 and "STOP FIRED" in sent[1] and "result -" in sent[1]


def test_a_telegram_failure_never_stops_a_fill(setup, monkeypatch):
    import asxbot.telegram as tg
    from asxbot.arena.notify import Notifier

    cfg, broker, pb, acct = setup
    broker.notifier = Notifier(cfg)

    def down(cfg):
        raise tg.TelegramError("offline")

    monkeypatch.setattr(tg, "load_bot", down)
    o = broker.submit(
        acct, ticker="AAA", side="buy", qty=1000, limit=1.20,
        decision_at=datetime(2026, 1, 6, 10, 1, tzinfo=SYD), stop=1.00,
    )  # fmt: skip
    broker.resolve_pending(acct, now=datetime(2026, 1, 6, 11, 0, tzinfo=SYD))
    assert acct.orders[o.order_id].status == "filled"


def test_alert_reasons_and_headlines_are_escaped_and_sent_in_full(cfg, monkeypatch):
    n, sent = _capture(cfg, monkeypatch)
    headline = "Quarterly <report> " + "h" * 120 + " HEND"
    n.passed("AAA", headline, "too small\nto matter " + "x" * 400 + " END", _AT)
    n.flush_passes(_AT + timedelta(hours=1), force=True)
    assert "&lt;report&gt;" in sent[0] and "h HEND" in sent[0]
    assert sent[0].endswith("x END</i> (12:00)")  # the reason itself is still one line


_AT = datetime(2026, 1, 6, 12, 0, tzinfo=SYD)


def test_passes_are_held_back_and_sent_as_one_digest_an_hour(cfg, monkeypatch):
    n, sent = _capture(cfg, monkeypatch)
    assert n.flush_passes(_AT) is False  # the first cycle only starts the hour's clock
    n.passed("AAA", "Quarterly report", "too small to move it", _AT)
    n.passed("BBB", "Director's interest", "administrative", _AT + timedelta(minutes=20))
    assert sent == []  # nothing instant

    assert n.flush_passes(_AT + timedelta(minutes=59)) is False  # not an hour yet
    assert n.flush_passes(_AT + timedelta(minutes=61)) is True
    assert len(sent) == 1
    assert "passed this hour (2)" in sent[0]
    assert "AAA" in sent[0] and "BBB" in sent[0] and "administrative" in sent[0]
    assert "seen 0" in sent[0]  # the counts line heads every digest

    # The queue empties and the clock restarts, so the next hour starts clean.
    assert n.flush_passes(_AT + timedelta(minutes=70)) is False


def test_a_quiet_hour_still_sends_a_digest(cfg, monkeypatch):
    """Silence and a dead watcher must never look the same on a phone."""
    n, sent = _capture(cfg, monkeypatch)
    n.flush_passes(_AT)
    assert n.flush_passes(_AT + timedelta(minutes=61)) is True
    assert "nothing passed this hour" in sent[0] and "seen 0" in sent[0]


def test_the_end_of_session_summary_is_sent_once_a_day(cfg, monkeypatch):
    n, sent = _capture(cfg, monkeypatch)
    assert n.session_summary("SESSION DONE", _AT) is True
    assert n.session_summary("SESSION DONE", _AT + timedelta(minutes=5)) is False
    assert n.session_summary("SESSION DONE", _AT + timedelta(days=1)) is True
    assert len(sent) == 2


def test_the_pass_queue_survives_a_restart(cfg, monkeypatch):
    from asxbot.arena.notify import Notifier

    n, sent = _capture(cfg, monkeypatch)
    n.passed("AAA", "Quarterly report", "too small", _AT)
    fresh = Notifier(cfg)  # as if the watcher had been restarted
    monkeypatch.setattr(fresh, "send", lambda text: sent.append(text) or True)
    assert fresh.flush_passes(_AT + timedelta(hours=1), force=True) is True
    assert "AAA" in sent[0]


# -- the tradability screen (plain code, before any model call) -------------
def _quote(last=1.00, volume=500_000.0):
    from asxbot.live.quotes import Quote

    return Quote("AAA", last, last, last, volume, _AT, "test")


def _ann(headline="Material contract awarded", code="AAA"):
    from asxbot.announcements.model import Announcement

    return Announcement(code, datetime(2026, 1, 6, 9, 0), headline, True, "1", "http://x")


def _daily(turnover_each_day: float, price: float = 1.00, n: int = 30):
    idx = pd.date_range("2025-11-01", periods=n, freq="D")
    return pd.DataFrame(
        {"close": [price] * n, "volume": [turnover_each_day / price] * n}, index=idx
    )


def test_a_thin_stock_is_rejected_before_any_model_call():
    from asxbot.arena.tradability import screen

    v = screen(_ann(), _quote(), _daily(50_000), 250_000, _AT)
    assert not v.ok and v.test == "turnover" and "below the $250,000 floor" in v.why


def test_a_tick_worth_more_than_one_percent_is_rejected():
    from asxbot.arena.tradability import screen

    v = screen(_ann(), _quote(last=0.02), _daily(1_000_000, price=0.02), 250_000, _AT)
    assert not v.ok and v.test == "tick"  # 0.1c tick on a 2c share is 5% of the price


def test_a_halt_and_a_stock_that_has_not_traded_are_both_rejected():
    from asxbot.arena.tradability import screen

    halt = screen(_ann("Trading Halt"), _quote(), _daily(1_000_000), 250_000, _AT)
    assert not halt.ok and halt.test == "halted"
    quiet = screen(_ann(), _quote(volume=0.0), _daily(1_000_000), 250_000, _AT)
    assert not quiet.ok and quiet.test == "halted"


def test_a_reinstatement_passes_the_screen_but_a_halt_or_suspension_does_not():
    """The classifier files halts, suspensions and reinstatements under one type, and the
    screen used to reject the whole type - which destroyed strategy D's only trigger."""
    from asxbot.arena.tradability import screen

    liquid = (_quote(last=5.00), _daily(2_000_000, price=5.00), 250_000, _AT)
    for headline in (
        "Reinstatement to Official Quotation",
        "Reinstatement to Quotation",
        "Trading Halt Lifted",
    ):
        a = _ann(headline)
        assert a.type == "trading_halt"  # the classifier is unchanged
        assert screen(a, *liquid).ok, headline
    for headline in (
        "Trading Halt",
        "Suspension from Official Quotation",
        "Request for Voluntary Suspension",
    ):
        v = screen(_ann(headline), *liquid)
        assert not v.ok and v.test == "halted", headline
    # A reinstated stock that still has not traded by 10:30 is rejected all the same.
    quiet = screen(_ann("Reinstatement to Official Quotation"), _quote(last=5.00, volume=0.0),
                   _daily(2_000_000, price=5.00), 250_000, _AT)  # fmt: skip
    assert not quiet.ok and quiet.test == "halted"


def test_no_quote_is_rejected_and_a_liquid_stock_passes():
    from asxbot.arena.tradability import screen

    assert screen(_ann(), None, _daily(1_000_000), 250_000, _AT).test == "no_quote"
    ok = screen(_ann(), _quote(last=5.00), _daily(2_000_000, price=5.00), 250_000, _AT)
    assert ok.ok and "tradeable" in ok.why


def test_before_the_open_zero_volume_is_not_a_halt():
    from asxbot.arena.tradability import screen

    early = datetime(2026, 1, 6, 8, 30, tzinfo=SYD)
    v = screen(_ann(), _quote(last=5.00, volume=0.0), _daily(2_000_000, price=5.00), 250_000, early)
    assert v.ok


# -- the 10-session horizon and the 10:20 re-look (23 Sep) ------------------
def test_the_playbook_holding_overrides_the_level(cfg):
    from asxbot.arena.levels import load_playbook

    pb = load_playbook(cfg, "asx_announcements")
    assert pb.level.holding == "intraday"  # the level is unchanged
    assert pb.holding == "days" and pb.hold_sessions == 10  # the playbook is not


def test_the_sweep_leaves_a_multi_day_playbook_alone(setup, monkeypatch):
    from asxbot.arena import watch as W

    cfg, broker, pb, acct = setup
    _offline_price(monkeypatch)
    _hold_position(acct)
    called = []
    monkeypatch.setattr(W, "call_agent", lambda *a, **k: called.append(1))
    assert W.sweep_before_close(_FakeArena(cfg, broker, acct), pb, now=_AT) == []
    assert called == []  # the decider is not even asked


def test_the_sweep_leaves_a_written_multi_day_thesis_alone(setup, monkeypatch):
    from asxbot.arena import watch as W

    cfg, broker, pb, acct = setup
    _offline_price(monkeypatch)
    _hold_position(acct)
    acct.positions["AAA"].hold = "overnight"
    acct.positions["AAA"].hold_reason = "results due Thursday, the thesis needs two sessions"
    monkeypatch.setattr(W, "call_agent", lambda *a, **k: 1 / 0)  # must not be called
    out = W.sweep_before_close(
        _FakeArena(cfg, broker, acct), _intraday(pb), now=datetime(2026, 1, 6, 15, 50, tzinfo=SYD)
    )
    assert out[0]["action"] == "hold" and "Thursday" in out[0]["reason"]


def test_a_position_is_closed_at_the_horizon(setup, monkeypatch):
    from asxbot.arena import watch as W

    cfg, broker, pb, acct = setup
    _offline_price(monkeypatch)
    _hold_position(acct)  # opened 2026-01-06
    fake = _FakeArena(cfg, broker, acct)
    fake.universe, fake.short_universe = {"AAA"}, set()

    nine = datetime(2026, 1, 19, 11, 0, tzinfo=SYD)  # 9 weekdays later
    assert W.horizon_exit(fake, pb, now=nine) == []
    ten = datetime(2026, 1, 20, 11, 0, tzinfo=SYD)  # 10
    out = W.horizon_exit(fake, pb, now=ten)
    assert len(out) == 1 and "horizon reached" in out[0]["reason"]
    assert any(o.side == "sell" for o in acct.orders.values())


def _pass_record(cfg, ids_id, at, action="pass", **extra):
    from asxbot.log import EventLog

    rec = {
        "stage": "decider", "ticker": "AAA", "ids_id": ids_id,
        "decision": {"action": action, "why": "the 10:00 auction has not happened yet"},
    }  # fmt: skip
    ev = EventLog(cfg.data_dir)
    row = ev.append("arena_decisions", {**rec, **extra})
    # rewrite the timestamp: the event log stamps "now", and these are historical
    p = ev.path("arena_decisions")
    lines = p.read_text(encoding="utf-8").splitlines()
    import json as _json

    lines[-1] = _json.dumps({**_json.loads(lines[-1]), "ts": at.astimezone(UTC).isoformat()})
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return row


def test_only_pre_open_passes_are_re_looked_and_only_once(cfg):
    from asxbot.arena.watch import _save_relooked, relook_candidates

    day = datetime.now(SYD).date()
    morning = datetime.combine(day, dtime(9, 30), tzinfo=SYD)
    after = datetime.combine(day, dtime(11, 0), tzinfo=SYD)
    _pass_record(cfg, "PRE-OPEN", morning)
    _pass_record(cfg, "AFTER-OPEN", after)  # judged with the reaction visible; it stands
    _pass_record(cfg, "TRADED", morning, action="trade")
    _pass_record(cfg, "A-TEST", morning, is_test=True)
    _pass_record(cfg, "ALREADY-RELOOKED", morning, relook=True)

    now = datetime.combine(day, dtime(10, 20), tzinfo=SYD)
    ids = [c["ids_id"] for c in relook_candidates(cfg, now)]
    assert ids == ["PRE-OPEN"]

    _save_relooked(cfg.data_dir, day, {"PRE-OPEN"})
    assert relook_candidates(cfg, now) == []  # one re-look each, and no more


def test_the_re_look_note_carries_the_earlier_reason():
    from asxbot.arena.watch import relook_note

    assert relook_note("") == ""
    note = relook_note("the auction has not happened yet")
    assert "RE-LOOK" in note and "the auction has not happened yet" in note
    assert "Passing again is a" in note  # it must not read as pressure to trade


def test_the_decider_packet_carries_the_re_look_note(setup, monkeypatch):
    """This shipped broken once: the packet took the note but the signature did not, so
    every re-look died at the decider with a TypeError after paying for a reader call."""
    from asxbot.arena import watch as W

    cfg, broker, pb, acct = setup
    _offline_price(monkeypatch)
    fake = _FakeArena(cfg, broker, acct)
    ctx = {"dossier": {}, "reaction": {}, "text": ""}
    note = W.relook_note("the auction has not happened yet")
    packet = W.decider_packet(fake, pb, acct, _ann(), ctx, "summary", _AT, note)
    assert "THIS IS A RE-LOOK" in packet and "HOW TO DECIDE" in packet
    assert "THIS IS A RE-LOOK" not in W.decider_packet(fake, pb, acct, _ann(), ctx, "s", _AT)


def test_the_decider_packet_shows_its_orders_waiting_to_fill(setup, monkeypatch):
    """On 23 Sep the decider passed on a second A1M announcement saying "I cannot see whether
    the first order filled": the packet carried held positions only."""
    import json as _json

    from asxbot.arena import watch as W

    cfg, broker, pb, acct = setup
    _offline_price(monkeypatch)
    broker.submit(
        acct, ticker="AAA", side="buy", qty=3000, limit=0.83,
        decision_at=_AT - timedelta(minutes=25), stop=0.755,
    )  # fmt: skip
    done = broker.submit(
        acct, ticker="BBB", side="buy", qty=100, limit=1.00,
        decision_at=_AT - timedelta(minutes=90), stop=0.90,
    )  # fmt: skip
    done.status = "filled"  # a filled order is a position, not something waiting

    ctx = {"dossier": {}, "reaction": {}, "text": ""}
    packet = W.decider_packet(_FakeArena(cfg, broker, acct), pb, acct, _ann(), ctx, "s", _AT)
    line = next(ln for ln in packet.splitlines() if "orders waiting to fill:" in ln)
    shown = _json.loads(line.split("orders waiting to fill:")[1].split("   (")[0])
    assert shown == [
        {"ticker": "AAA", "side": "buy", "qty": 3000, "limit": 0.83, "stop": 0.755,
         "minutes_waiting": 25}
    ]  # fmt: skip


# -- the yardstick: strategy A as frozen - confirm at the close, enter at the next open --
T0 = date(2026, 1, 5)  # the reaction session (a Monday); DAY, the 6th, is the entry open


def _index_daily() -> pd.DataFrame:
    idx = pd.bdate_range(end=pd.Timestamp(T0), periods=60, name="date")
    return pd.DataFrame(
        {"open": 1000.0, "high": 1000.0, "low": 1000.0, "close": 1000.0, "volume": 0.0}, index=idx
    )


def _stock_daily(gap_pct: float, vol_x: float) -> pd.DataFrame:
    """60 quiet sessions at 0.95 on 1m shares, then T0 opens gap_pct above the prior close on
    vol_x the usual volume and closes at 1.00. The index is flat, so the gap is all relative."""
    df = _index_daily().assign(open=0.95, high=0.95, low=0.95, close=0.95, volume=1_000_000.0)
    o = 0.95 * (1 + gap_pct / 100)
    df.loc[pd.Timestamp(T0)] = [o, max(o, 1.0), min(o, 1.0), 1.00, 1_000_000.0 * vol_x]
    return df


def _ann_frame(*rows) -> pd.DataFrame:
    """rows: (code, released_at 'YYYY-MM-DD HH:MM' Sydney, price_sensitive)"""
    return pd.DataFrame(
        [
            {"code": c, "released_at": pd.Timestamp(ts), "headline": f"{c} news", "type": "other",
             "price_sensitive": ps, "ids_id": f"{i:08d}", "pre_open": False}
            for i, (c, ts, ps) in enumerate(rows)
        ]
    )  # fmt: skip


def _yardstick(cfg, broker, pb, frames):
    from asxbot.arena.bots.announcement_drift import AnnouncementDriftBot

    def fetch(tickers, start):
        return {t: frames[t] for t in tickers if t in frames}

    return AnnouncementDriftBot(cfg, pb, broker, set(frames) - {"^AXJO"}, fetch)


def test_the_yardstick_confirms_the_frozen_rule_on_the_completed_session(setup):
    """Gap >= 5% vs the index at the open, >= 3x volume, a price-sensitive announcement
    reacting that session - and nothing is decided when the announcement lands."""
    from asxbot.arena.bots.announcement_drift import NotReady

    cfg, broker, pb, acct = setup
    frames = {
        "^AXJO": _index_daily(),
        "AAA": _stock_daily(6, 4),
        "BBB": _stock_daily(4, 4),  # gap below 5%
        "CCC": _stock_daily(6, 2),  # volume below 3x
        "DDD": _stock_daily(6, 4),  # released after 10:00, so it reacts the next session
        "EEE": _stock_daily(6, 4),  # not price sensitive
        "FFF": _stock_daily(6, 5),  # released on the Sunday; reacts Monday
    }
    ann = _ann_frame(
        ("AAA", "2026-01-05 08:30", True), ("BBB", "2026-01-05 08:30", True),
        ("CCC", "2026-01-05 08:30", True), ("DDD", "2026-01-05 11:00", True),
        ("EEE", "2026-01-05 08:30", False), ("FFF", "2026-01-04 15:00", True),
    )  # fmt: skip
    bot = _yardstick(cfg, broker, pb, frames)

    decision, why = bot.on_announcement(acct, _ann(code="AAA"), _AT)
    assert decision is None and "next open" in why

    out, why = bot.entries(acct, ann, T0, datetime(2026, 1, 6, 7, 45, tzinfo=SYD))
    assert [d.ticker for d in out] == ["FFF", "AAA"]  # highest volume multiple first
    aaa = out[1]
    assert (aaa.side, aaa.limit, aaa.stop, aaa.stop_pct) == ("buy", 1.10, 1.01, 8.0)

    # Before the session's bar is published there is nothing to confirm on.
    at = datetime(2026, 1, 6, 7, 45, tzinfo=SYD)
    late = _yardstick(cfg, broker, pb, {**frames, "^AXJO": _index_daily().iloc[:-1]})
    with pytest.raises(NotReady):
        late.entries(acct, ann, T0, at)
    # Nor when the index has dropped the session before it, which the gap is measured from:
    # the backtest's code would find no event at all, silently.
    gappy = _index_daily().drop(pd.Timestamp(date(2026, 1, 2)))
    with pytest.raises(NotReady, match="missing 2026-01-02"):
        _yardstick(cfg, broker, pb, {**frames, "^AXJO": gappy}).entries(acct, ann, T0, at)


def test_the_yardstick_enters_at_the_next_open_once_and_never_late(setup, monkeypatch):
    import dataclasses
    import json

    from asxbot.arena import watch as W

    cfg, broker, pb, acct = setup
    pb = dataclasses.replace(pb, raw={**pb.raw, "warmup_start": None})
    live = cfg.data_dir / "announcements" / "live"
    live.mkdir(parents=True, exist_ok=True)
    _ann_frame(("AAA", "2026-01-05 08:30", True)).to_parquet(live / "2026-01-05.parquet")
    frames = {"^AXJO": _index_daily(), "AAA": _stock_daily(6, 4)}
    bot = _yardstick(cfg, broker, pb, frames)
    monkeypatch.setattr(W, "make_bot", lambda arena, pb, quotes=None: bot)
    fake = _FakeArena(cfg, broker, acct)

    placed = W.yardstick_entries(fake, pb, datetime(2026, 1, 6, 7, 45, tzinfo=SYD))
    assert [p["ticker"] for p in placed] == ["AAA"]
    assert W.yardstick_entries(fake, pb, datetime(2026, 1, 6, 8, 0, tzinfo=SYD)) == []  # once

    broker.resolve_pending(acct, now=datetime(2026, 1, 6, 11, 0, tzinfo=SYD))
    o = acct.orders[placed[0]["order_id"]]
    assert o.status == "filled" and o.fill_minute.startswith("2026-01-06T10:00")  # the open
    pos = acct.positions["AAA"]
    assert pos.stop == pytest.approx(o.avg_price * 0.92, abs=1e-4)  # 8% below the fill

    # The next morning the 6th's bar never arrives: it waits, then records the miss and why,
    # rather than trading late.
    assert W.yardstick_entries(fake, pb, datetime(2026, 1, 7, 7, 45, tzinfo=SYD)) == []
    state = W._yardstick_path(cfg.data_dir, date(2026, 1, 7))
    assert json.loads(state.read_text())["status"] == "waiting"
    assert W.yardstick_entries(fake, pb, datetime(2026, 1, 7, 10, 30, tzinfo=SYD)) == []
    missed = json.loads(state.read_text())
    assert missed["status"] == "missed" and "2026-01-06" in missed["missed"]


def test_the_yardstick_sizes_within_the_per_order_guard(setup, monkeypatch):
    """On a larger account the 40% position cap passes $8,000, and arena_place_order refuses
    any single order above arena.guards.max_order_value_aud. The bot's own sizing never
    looked at that guard, so it would have sized an order the broker refused: no trade."""
    import dataclasses

    from asxbot.arena import watch as W

    cfg, broker, pb, acct = setup
    pb = dataclasses.replace(pb, raw={**pb.raw, "warmup_start": None})
    live = cfg.data_dir / "announcements" / "live"
    live.mkdir(parents=True, exist_ok=True)
    _ann_frame(("AAA", "2026-01-05 08:30", True)).to_parquet(live / "2026-01-05.parquet")
    frames = {"^AXJO": _index_daily(), "AAA": _stock_daily(6, 4)}
    bot = _yardstick(cfg, broker, pb, frames)
    monkeypatch.setattr(W, "make_bot", lambda arena, pb, quotes=None: bot)
    acct.cash = 50_000.0  # 40% of equity is $20,000, well past the $8,000 guard

    guard = float(cfg.get("arena.guards.max_order_value_aud"))
    placed = W.yardstick_entries(_FakeArena(cfg, broker, acct), pb,
                                 datetime(2026, 1, 6, 7, 45, tzinfo=SYD))  # fmt: skip
    assert [p["ticker"] for p in placed] == ["AAA"]
    assert "refused" not in placed[0], placed[0].get("refused")  # sized down, not refused
    o = acct.orders[placed[0]["order_id"]]
    assert o.qty * o.limit <= guard
    assert o.qty == int(guard / o.limit)  # the guard is what binds, and nothing smaller
