"""Announcements v2 and the day trader (config frozen 2026-09-24): the new limits, the
data layer's no-peeking rule, the v2 rule bot, the reaction look's timing, the four setups,
and code's trade management. No network: every bar is written to a temporary minute cache.
"""

import dataclasses
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from asxbot.arena import daytrader as DT
from asxbot.arena import reaction_v2 as R
from asxbot.arena.accounts import Position
from asxbot.arena.broker import ArenaBroker
from asxbot.arena.intraday import MarketView, ReplayFeed, counted_volume, visible
from asxbot.arena.levels import load_playbook
from asxbot.arena.minutes import MinuteBars
from asxbot.arena.orders import ArenaOrderRefused, arena_place_order
from asxbot.backtest.costs import CostModel
from asxbot.config import load_config
from asxbot.io import write_parquet_atomic

SYD = ZoneInfo("Australia/Sydney")
DAY = date(2026, 1, 8)  # a Thursday; 5, 6 and 7 Jan are its prior sessions
PRIOR = [date(2026, 1, 7), date(2026, 1, 6), date(2026, 1, 5)]


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def at(h, m, day=DAY):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=SYD)


def frame(day, rows):
    """rows: {(h, m): (open, high, low, close, volume)}"""
    idx = [at(h, m, day) for (h, m) in rows]
    vals = list(rows.values())
    return pd.DataFrame(vals, index=pd.DatetimeIndex(idx),
                        columns=["open", "high", "low", "close", "volume"])  # fmt: skip


def flat_day(day, price=1.0, vol=1000, close_px=None):
    """A quiet whole session 10:00-15:59 plus the 16:10 closing auction."""
    rows = {}
    t = at(10, 0, day)
    while t.time() < datetime.min.replace(hour=16).time():
        rows[(t.hour, t.minute)] = (price, price, price, price, vol)
        t += timedelta(minutes=1)
    cp = close_px if close_px is not None else price
    rows[(16, 10)] = (cp, cp, cp, cp, vol * 10)
    return frame(day, rows)


@pytest.fixture
def cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )


@pytest.fixture
def mb(cfg):
    return MinuteBars(cfg.data_dir, "close")


def put(mb, code, day, df):
    write_parquet_atomic(df, mb._path(code, day))


def index_days(mb, today_rows=None, level=8000.0):
    for d in PRIOR:
        put(mb, "^AXJO", d, flat_day(d, level, 0))
    if today_rows is not None:
        put(mb, "^AXJO", DAY, today_rows)


# -- config -------------------------------------------------------------------------------
def test_v1_is_retired_and_v2_and_the_day_trader_are_frozen_before_running(cfg):
    v1 = load_playbook(cfg, "asx_announcements")
    v2 = load_playbook(cfg, "asx_announcements_v2")
    dt = load_playbook(cfg, "asx_daytrader")
    assert not v1.enabled and v1.status == "retired"
    assert v1.raw["trigger"]["turnover_floor_aud"] == 250000  # v1's values kept
    assert v2.enabled and v2.version == 2 and v2.raw["frozen_on"] == "2026-09-24"
    assert dt.enabled and dt.raw["frozen_on"] == "2026-09-24"
    assert v2.raw["test_start"] == dt.raw["test_start"] == "2026-09-25"
    assert v2.agent_account == "asx_announcements_v2__agent"
    assert dt.bot_account == "asx_daytrader_v1__bot"
    assert dt.risk_per_trade_pct == 0.5 and v2.risk_per_trade_pct == 5.0
    lvl = v2.level
    assert (lvl.max_position_aud, lvl.max_open_positions, lvl.max_new_positions_per_day) == (
        5000, 3, 6
    )
    assert cfg.get("limits.max_position_aud") == 5000  # the real-money limits: unchanged
    assert cfg.get("limits.max_open_positions") == 4


# -- the new hard limits ------------------------------------------------------------------
@pytest.fixture
def book(cfg, mb):
    broker = ArenaBroker(
        cfg.data_dir, CostModel.from_config(cfg), mb, lambda t: 2_000_000.0,
        resolve_after_minutes=0, clock=Clock(at(11, 0)),
    )  # fmt: skip
    broker.median_turnover = lambda t: {"THIN": 60_000.0}.get(t, 2_000_000.0)
    pb = load_playbook(cfg, "asx_announcements_v2")
    acct = broker.store.open("v2__agent", pb.key, "agent", 1, 20_000.0)
    return broker, pb, acct


def _order(cfg, broker, acct, pb, **kw):
    base = dict(ticker="AAA", side="buy", qty=1000, limit=4.0, stop=3.8, placed_by="agent",
                universe=None, short_universe={"AAA"})  # fmt: skip
    return arena_place_order(cfg, broker, acct, pb, **{**base, **kw})


def test_a_position_is_capped_at_5000_dollars(cfg, book):
    broker, pb, acct = book
    with pytest.raises(ArenaOrderRefused, match="per position"):
        _order(cfg, broker, acct, pb, qty=1300, limit=4.0, stop=3.9)  # $5,200
    assert _order(cfg, broker, acct, pb, qty=1250, limit=4.0, stop=3.9).order_id


def test_the_size_aware_rule_refuses_an_order_over_5pct_of_turnover(cfg, book):
    broker, pb, acct = book
    with pytest.raises(ArenaOrderRefused, match="median"):
        _order(cfg, broker, acct, pb, ticker="THIN", qty=1000, limit=4.0, stop=3.9)  # > $3,000
    assert _order(cfg, broker, acct, pb, ticker="THIN", qty=700, limit=4.0, stop=3.9).order_id


def test_six_new_positions_a_day_and_no_entry_after_the_last_entry_time(cfg, book):
    broker, pb, acct = book
    pb = dataclasses.replace(pb, level=dataclasses.replace(pb.level, max_open_positions=10))
    for i in range(6):
        _order(cfg, broker, acct, pb, ticker=f"S{i}", qty=100, limit=10.0, stop=9.5)
    with pytest.raises(ArenaOrderRefused, match="6 a day"):
        _order(cfg, broker, acct, pb, ticker="S7", qty=100, limit=10.0, stop=9.5)
    broker.clock = Clock(at(15, 41))
    acct2 = broker.store.open("v2b__agent", pb.key, "agent", 1, 20_000.0)
    with pytest.raises(ArenaOrderRefused, match="after 15:40"):
        _order(cfg, broker, acct2, pb, ticker="LATE", qty=100, limit=10.0, stop=9.5)


def test_the_day_traders_risk_cap_is_half_a_percent(cfg, book):
    broker, _, acct = book
    dt = load_playbook(cfg, "asx_daytrader")
    with pytest.raises(ArenaOrderRefused, match="0.5% of equity"):
        _order(cfg, broker, acct, dt, qty=1000, limit=4.0, stop=3.8)  # $200 risk > $100
    assert _order(cfg, broker, acct, dt, qty=1000, limit=4.0, stop=3.91).order_id


# -- the broker: good-till and trade management -------------------------------------------
def test_an_entry_stops_filling_at_its_good_till_minute_and_expires(cfg, mb, book):
    broker, pb, acct = book
    rows = {(11, m): (4.0, 4.0, 4.0, 4.0, 10_000) for m in range(0, 30)}
    rows[(11, 1)] = (4.5, 4.5, 4.5, 4.5, 10_000)  # above the limit until 11:05
    rows[(11, 2)] = rows[(11, 3)] = rows[(11, 4)] = rows[(11, 1)]
    put(mb, "AAA", DAY, frame(DAY, rows))
    broker.clock = Clock(at(11, 0, ))
    o = broker.submit(acct, ticker="AAA", side="buy", qty=100, limit=4.2, stop=3.9,
                      good_till=at(11, 3))  # fmt: skip
    broker.work(acct, at(11, 29))
    assert o.status == "expired" and o.filled_qty == 0


def _managed_position(broker, acct, entry=10.0, stop=9.0, qty=1000, opened=(11, 0)):
    manage = {"breakeven_at_r": 1.0, "half_at_r": 2.0, "trail_at_r": 2.0,
              "trail_distance_r": 1.0, "entry": entry, "r": entry - stop,
              "best": entry}  # fmt: skip
    acct.positions["MGD"] = Position(
        ticker="MGD", qty=qty, avg_cost=entry, opened_at=at(*opened).isoformat(timespec="minutes"),
        stop=stop, opened_by="bot", manage=manage,
    )  # fmt: skip


def test_the_stop_moves_to_breakeven_at_1r_and_trails_after_2r_taking_half_off(cfg, mb, book):
    broker, pb, acct = book
    rows = {
        (11, 1): (10.0, 10.5, 9.9, 10.4, 50_000),
        (11, 2): (10.4, 11.1, 10.3, 11.0, 50_000),  # +1.1R: stop -> 10.0 from 11:03
        (11, 3): (11.0, 12.2, 10.9, 12.1, 50_000),  # +2.2R: half at 12.0, trail to 11.2
        (11, 4): (12.1, 12.1, 11.5, 11.6, 50_000),  # above the trail: holds
        (11, 5): (11.6, 11.6, 11.1, 11.2, 50_000),  # low 11.1 < 11.2: stopped
        (11, 6): (11.2, 11.2, 11.2, 11.2, 50_000),
    }
    put(mb, "MGD", DAY, frame(DAY, rows))
    _managed_position(broker, acct)
    broker.clock = Clock(at(11, 30))
    broker.work(acct, at(11, 30))
    orders = sorted(acct.orders.values(), key=lambda o: o.order_id)
    half = [o for o in orders if o.reason.startswith("HALF")]
    stop = [o for o in orders if o.order_type == "stop"]
    assert len(half) == 1 and half[0].filled_qty == 500 and "T11:03" in half[0].fill_minute
    assert abs(half[0].fills[0]["bar_price"] - 12.0) < 1e-9
    assert len(stop) == 1 and stop[0].limit == pytest.approx(11.2)
    assert "T11:05" in stop[0].fill_minute and stop[0].filled_qty == 500
    assert "MGD" not in acct.positions


def test_a_bar_that_sets_breakeven_is_not_stopped_by_it(cfg, mb, book):
    """The move is effective from the NEXT bar: the bar that reached +1R and fell back to
    the entry is not a stop at breakeven."""
    broker, pb, acct = book
    rows = {(11, 1): (10.0, 11.2, 9.95, 10.0, 50_000), (11, 2): (10.0, 10.1, 10.05, 10.1, 50_000)}
    put(mb, "MGD", DAY, frame(DAY, rows))
    _managed_position(broker, acct)
    broker.clock = Clock(at(11, 30))
    broker.work(acct, at(11, 30))
    assert acct.positions["MGD"].stop == pytest.approx(10.0)
    assert not [o for o in acct.orders.values() if o.order_type == "stop"]


def test_the_stop_never_loosens(cfg, mb, book):
    broker, pb, acct = book
    put(mb, "MGD", DAY, frame(DAY, {(11, 1): (10.0, 10.2, 9.95, 10.1, 50_000)}))
    _managed_position(broker, acct, stop=9.9)
    acct.positions["MGD"].manage["r"] = 0.1  # +2R already reached: trail 10.1
    broker.clock = Clock(at(11, 30))
    broker.work(acct, at(11, 30))
    assert acct.positions["MGD"].stop >= 9.9


# -- the data layer: no peeking -------------------------------------------------------------
def test_a_delayed_feed_only_shows_bars_final_by_then(mb):
    df = frame(DAY, {(10, m): (1, 1, 1, 1, 100) for m in range(0, 40)})
    seen = visible(df, at(10, 35), delay_minutes=20)
    assert seen.index.max() == at(10, 13)  # 10:35 - 20 delay - 2 (the newest row is forming)
    held = df[df.index <= at(10, 34)]  # live, the feed holds rows up to the forming minute
    live = visible(held, at(10, 35))
    assert live.index.max() == at(10, 33)  # the newest held row (10:34) may still be forming
    assert counted_volume(df) == 39 * 100  # never the 10:00 bar


# -- the v2 rule bot ---------------------------------------------------------------------------
def _v2_market(mb, move=0.05, vol=5000, base_vol=1000, code="NEWS"):
    for d in PRIOR:
        put(mb, code, d, flat_day(d, 1.0, base_vol))
    put(mb, code, PRIOR[0], flat_day(PRIOR[0], 1.0, base_vol, close_px=1.0))
    px = 1.0 + move
    rows = {(10, m): (px, px + 0.01, px - 0.02, px, vol) for m in range(0, 45)}
    put(mb, code, DAY, frame(DAY, rows))
    index_days(mb, frame(DAY, {(10, m): (8000, 8000, 8000, 8000, 0) for m in range(0, 45)}))


def _view(mb, delay=20):
    return MarketView(mb, DAY, ReplayFeed(mb, delay), "^AXJO", 5, 3)


def test_the_v2_rule_buys_3pct_over_the_index_on_3x_volume_with_the_opening_low_as_stop(mb):
    _v2_market(mb)
    params = {"move_vs_index_pct": 3.0, "volume_multiple": 3.0, "measure_at": "10:30"}
    none, why = R.v2_bot_signal(_view(mb), "NEWS", at(10, 50), params, False)
    assert none is None and "not reached 10:30" in why  # 10:29 is not final at 10:50 (delay)
    sig, why = R.v2_bot_signal(_view(mb), "NEWS", at(10, 52), params, False)
    assert sig.side == "buy" and sig.move_vs_index_pct == pytest.approx(5.0)
    assert sig.volume_multiple == pytest.approx(5.0) and sig.stop == pytest.approx(1.03)


def test_the_v2_rule_needs_the_volume_and_shorts_only_index_members(mb):
    _v2_market(mb, vol=2000)
    params = {"move_vs_index_pct": 3.0, "volume_multiple": 3.0, "measure_at": "10:30"}
    sig, why = R.v2_bot_signal(_view(mb), "NEWS", at(10, 52), params, False)
    assert sig is None and "volume below" in why
    _v2_market(mb, move=-0.05)
    sig, why = R.v2_bot_signal(_view(mb), "NEWS", at(10, 52), params, False)
    assert sig is None and "not an ASX 200 member" in why
    sig, _ = R.v2_bot_signal(_view(mb), "NEWS", at(10, 52), params, True)
    assert sig.side == "short" and sig.stop == pytest.approx(0.96)


def test_the_reaction_look_waits_for_10_minutes_and_is_missed_after_40(cfg, mb):
    _v2_market(mb)
    pb = load_playbook(cfg, "asx_announcements_v2")
    item = {"ticker": "NEWS", "first_release": datetime(2026, 1, 8, 8, 30).isoformat()}
    assert R.look_status(_view(mb, 0), item, at(10, 5), pb).status == "wait"
    ready = R.look_status(_view(mb, 0), item, at(10, 12), pb)
    assert ready.status == "ready" and ready.ref == at(10, 0)
    assert R.look_status(_view(mb, 20), item, at(10, 25), pb).status == "wait"  # delayed
    assert R.look_status(_view(mb, 0), item, at(10, 44), pb).status == "missed"


def test_the_v2_screen_is_size_aware(cfg):
    from asxbot.announcements.model import Announcement
    from asxbot.live.quotes import Quote

    pb = load_playbook(cfg, "asx_announcements_v2")
    a = Announcement("AAA", datetime(2026, 1, 8, 8, 0), "Quarterly results", True, "1", "")
    q = Quote("AAA", 1.0, 1.0, 1.0, 0, datetime(2026, 1, 8, 8, 0), "static")

    def daily(turnover):
        return pd.DataFrame({"close": [1.0] * 20, "volume": [turnover] * 20})

    assert not R.screen_v2(a, q, daily(90_000), pb, 5000).ok  # needs $100,000
    assert R.screen_v2(a, q, daily(110_000), pb, 5000).ok
    assert R.screen_v2(a, None, daily(110_000), pb, 5000).test == "no_quote"


# -- the day trader's setups ------------------------------------------------------------------
def _ctx(mb, bars, conf, prev=1.0, shortable=True, news=False, usual_vol=1000):
    for d in PRIOR:
        put(mb, "DTX", d, flat_day(d, prev, usual_vol))
    index_days(mb)
    idx = frame(DAY, {(h, m): (8000, 8000, 8000, 8000, 0) for h in range(10, 16)
                      for m in range(60)})  # fmt: skip
    from asxbot.arena.intraday import usual_cum_volume

    return DT.Ctx("DTX", DAY, bars, idx, prev, 8000.0, usual_cum_volume(mb, "DTX", DAY),
                  shortable, news, conf)  # fmt: skip


def _conf(cfg):
    return load_playbook(cfg, "asx_daytrader").raw["setups"]


def test_gap_and_go_fires_on_the_first_close_above_the_15_minute_high(cfg, mb):
    rows = {(10, m): (1.05, 1.06, 1.04, 1.05, 3000) for m in range(0, 15)}
    rows[(10, 15)] = (1.05, 1.08, 1.05, 1.07, 3000)
    bars = frame(DAY, rows)
    c = _ctx(mb, bars, _conf(cfg))
    found = DT.detect(c, None, set(), DT._t("15:45"))
    assert [(s.setup, s.side) for s in found] == [("gap_and_go", "buy")]
    assert found[0].stop == pytest.approx(1.04) and "T10:15" in found[0].trigger_bar


def test_opening_range_breakout_needs_rising_volume(cfg, mb):
    rows = {(10, m): (1.0, 1.01, 0.99, 1.0, 2000) for m in range(0, 40)}
    rows[(10, 35)] = (1.0, 1.03, 1.0, 1.02, 3000)  # breaks out on too little volume
    rows[(10, 38)] = (1.0, 1.03, 1.0, 1.02, 9000)  # breaks out on 4.5x
    c = _ctx(mb, frame(DAY, rows), _conf(cfg))
    found = [s for s in DT.detect(c, None, set(), DT._t("15:45"))
             if s.setup == "opening_range_breakout"]  # fmt: skip
    assert len(found) == 1 and "T10:38" in found[0].trigger_bar
    assert found[0].stop == pytest.approx(1.0)  # the midpoint of 0.99-1.01


def test_each_setup_fires_once_a_day_and_bars_after_the_scan_end_do_not_trigger(cfg, mb):
    rows = {(10, m): (1.05, 1.06, 1.04, 1.05, 3000) for m in range(0, 15)}
    rows[(10, 15)] = (1.05, 1.08, 1.05, 1.07, 3000)
    c = _ctx(mb, frame(DAY, rows), _conf(cfg))
    fired: set = set()
    assert DT.detect(c, None, fired, DT._t("15:45"))
    assert DT.detect(c, None, fired, DT._t("15:45")) == []
    assert DT.detect(c, None, set(), DT._t("10:14")) == []


def test_a_halt_is_found_and_its_resumption_continuation_fires(cfg, mb):
    rows = {(10, m): (1.0, 1.0, 1.0, 1.0, 2000) for m in range(0, 60)}
    # halted: no bar from 11:20 to 11:34
    rows.update({(11, m): (1.0, 1.0, 1.0, 1.0, 2000) for m in range(0, 20)})
    rows.update({(11, m): (1.05, 1.06, 1.04, 1.05, 8000) for m in range(35, 45)})
    rows[(11, 46)] = (1.06, 1.08, 1.06, 1.07, 8000)
    c = _ctx(mb, frame(DAY, rows), _conf(cfg))
    res = DT.resumptions(c)
    assert res and res[0][0] == at(11, 35)
    found = [s for s in DT.detect(c, None, set(), DT._t("15:45")) if s.setup == "halt_resumption"]
    assert found and found[0].side == "buy" and found[0].stop == pytest.approx(1.04)


def test_a_setup_is_sized_by_half_a_percent_risk_and_capped_at_5000(cfg, book):
    broker, _, acct = book
    dt = load_playbook(cfg, "asx_daytrader")

    class A:
        pass

    arena = A()
    arena.cfg, arena.broker = cfg, broker
    s = DT.Setup("AAA", "orb", "buy", at(10, 40).isoformat(), 10.0, 9.9, 2.0, "")
    t = DT.terms(arena, dt, acct, s)
    # limit 10.10 (1% through), risk 0.20 a share: $100 budget -> 500 shares ($5,050 > cap)
    assert t["limit"] == pytest.approx(10.1) and t["qty"] == 495
    assert t["value"] <= 5000


def test_one_re_entry_per_stock_per_day(cfg, book):
    broker, _, acct = book
    dt = load_playbook(cfg, "asx_daytrader")
    ok, _ = DT.eligible(acct, dt, "AAA", DAY)
    assert ok
    for _ in range(2):
        o = broker.submit(acct, ticker="AAA", side="buy", qty=10, limit=10.0, stop=9.9)
        o.decided_at = at(10, 40).isoformat()
        o.status, o.filled_qty = "filled", 10
    ok, why = DT.eligible(acct, dt, "AAA", DAY)
    assert not ok and "re-entry" in why


def test_the_reaction_day_of_evening_news_is_the_next_session():
    from asxbot.arena.watch import reaction_day

    assert reaction_day(datetime(2026, 1, 8, 9, 0)) == DAY
    assert reaction_day(datetime(2026, 1, 8, 17, 0)) == date(2026, 1, 9)
    assert reaction_day(datetime(2026, 1, 9, 18, 0)) == date(2026, 1, 12)  # Friday evening


def test_an_evening_pre_open_order_is_allowed_and_good_till_the_next_session(cfg, book):
    """An order after the close is for the next session's auction: the 15:40 last-entry
    rule does not refuse it, and it stops filling at 15:40 of THAT session."""
    from asxbot.arena import v2_flow

    broker, pb, acct = book
    broker.clock = Clock(at(18, 0))
    o = _order(cfg, broker, acct, pb, qty=100, limit=10.0, stop=9.5,
               good_till=v2_flow.good_till(pb, at(18, 0)))  # fmt: skip
    assert o.good_till.startswith("2026-01-09T15:40")
    broker.clock = Clock(at(15, 45))
    with pytest.raises(ArenaOrderRefused, match="after 15:40"):
        _order(cfg, broker, acct, pb, ticker="LATE", qty=100, limit=10.0, stop=9.5)
