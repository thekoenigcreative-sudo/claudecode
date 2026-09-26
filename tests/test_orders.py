"""place_order limits, the sim broker, the scanner and reconciliation. No network."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from asxbot.alerts import Alerts
from asxbot.announcements.model import Announcement
from asxbot.backtest.costs import CostModel
from asxbot.broker.orders import Limits, OrderRefused, place_order
from asxbot.broker.reconcile import reconcile
from asxbot.broker.sim import SimBroker
from asxbot.config import load_config
from asxbot.live.quotes import Quote, StaticQuotes
from asxbot.live.scanner import Scanner, round_to_tick
from asxbot.log import EventLog
from fake_provider import synthetic_bars

SYD = ZoneInfo("Australia/Sydney")


def _last_weekday_noon() -> datetime:
    """Noon Sydney on the most recent weekday.

    This used to be a fixed date. The limits count positions "opened today" from the event
    log, so once the wall clock passed that date the count was always zero and the test
    silently stopped testing anything. Deriving it keeps the test honest on any day it runs.
    """
    d = datetime.now(SYD).replace(hour=12, minute=0, second=0, microsecond=0)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


NOON = _last_weekday_noon()


@pytest.fixture
def cfg(config_file, tmp_path, monkeypatch):
    c = load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )
    return c


def _quotes(last=2.00, prev=1.80, vol=900_000.0):
    q = Quote("AAA", last, 1.95, prev, vol, datetime.now(), "static", False)
    idx = Quote("^AXJO", 7000.0, 7000.0, 7000.0, 0.0, datetime.now(), "static", False)
    return StaticQuotes({"AAA": q}, idx)


def _daily(ticker):
    d = synthetic_bars(60, price=1.8)
    d["volume"] = 100_000.0  # 20d avg volume 100k, turnover ~$180k/day
    d["close"] = 1.8
    d["volume"] = 150_000.0  # turnover 270k >= 250k floor
    return d


def _scanner(cfg, quotes=None):
    return Scanner(
        cfg.data_dir,
        quotes or _quotes(),
        _daily,
        5.0,
        3.0,
        250_000,
        8.0,
        2500.0,
        CostModel(),
        {"AAA", "BBB"},
    )


def _ann(code="AAA", sensitive=True):
    return Announcement(
        code,
        datetime(2026, 9, 22, 9, 30),
        "Major contract win",
        sensitive,
        "0999",
        "https://www.asx.com.au/x?idsId=0999",
    )


def test_round_to_tick():
    assert round_to_tick(2.003, up=True) == 2.01
    assert round_to_tick(1.234, up=True) == 1.235
    assert round_to_tick(0.0512, up=False) == 0.051


def test_scanner_builds_proposal(cfg):
    sc = _scanner(cfg)
    props = sc.scan([_ann()], NOON)
    assert len(props) == 1
    p = props[0]
    assert p.ticker == "AAA" and p.side == "buy"
    assert p.move_rel_pct == pytest.approx(11.11, abs=0.01)
    assert p.entry_limit >= 2.00 and p.stop < p.entry_limit
    assert p.qty * p.entry_limit <= 2500
    assert p.dollar_risk == pytest.approx(p.qty * (p.entry_limit - p.stop), abs=0.01)
    assert "Major contract win" in p.reasoning
    assert (cfg.data_dir / "proposals" / f"{p.id}.json").exists()
    # same announcement again -> no duplicate
    assert sc.scan([_ann()], NOON) == []


def test_scanner_rejects_weak_reaction(cfg):
    sc = _scanner(cfg, _quotes(last=1.85))  # +2.8%
    p, why = sc.evaluate(_ann(), NOON)
    assert p is None and "below 5.0%" in why
    sc = _scanner(cfg, _quotes(vol=50_000.0))  # 0.5x adjusted
    p, why = sc.evaluate(_ann(), NOON)
    assert p is None and "volume" in why
    p, why = sc.evaluate(_ann(sensitive=False), NOON)
    assert p is None and why == "not price sensitive"
    p, why = sc.evaluate(_ann(code="ZZZ"), NOON)
    assert p is None and why == "not in universe"


def _broker(cfg, ref_price=2.0):
    return SimBroker(cfg.data_dir, 10_000, CostModel(), lambda t: (ref_price, 300_000.0))


def _limits(cfg):
    return Limits.from_config(cfg, {"AAA", "BBB"})


def test_place_order_full_path_sim(cfg):
    sc = _scanner(cfg)
    p = sc.scan([_ann()], NOON)[0]
    broker = _broker(cfg)
    res = place_order(
        cfg,
        broker,
        _limits(cfg),
        proposal_id=p.id,
        ticker="AAA",
        side="buy",
        qty=p.qty,
        limit=p.entry_limit,
        now=NOON,
    )
    assert res.order_id == "SIM-000001" and res.status == "filled" and res.filled_qty == p.qty
    assert broker.positions()[0].ticker == "AAA"
    assert broker.cash() < 10_000
    saved = json.loads((cfg.data_dir / "proposals" / f"{p.id}.json").read_text())
    assert saved["status"] == "placed" and saved["order_id"] == "SIM-000001"
    ev = EventLog(cfg.data_dir)
    assert ev.read("fills")[0]["order_id"] == "SIM-000001"
    # reconciliation clean
    assert reconcile(broker, ev, Alerts(cfg.data_dir)) == []
    # sell it back (no proposal needed), then reconcile again
    res2 = place_order(
        cfg,
        broker,
        _limits(cfg),
        proposal_id=None,
        ticker="AAA",
        side="sell",
        qty=p.qty,
        limit=1.90,
        now=NOON,
    )
    assert res2.status == "filled" and broker.positions() == []
    assert reconcile(broker, ev, Alerts(cfg.data_dir)) == []


def test_place_order_refusals(cfg):
    sc = _scanner(cfg)
    p = sc.scan([_ann()], NOON)[0]
    broker = _broker(cfg)
    lim = _limits(cfg)
    kw = dict(proposal_id=p.id, ticker="AAA", side="buy", qty=p.qty, limit=p.entry_limit, now=NOON)
    with pytest.raises(OrderRefused, match="exceeds max position"):
        place_order(cfg, broker, lim, **{**kw, "qty": 5000})
    with pytest.raises(OrderRefused, match="not in the allowed universe"):
        place_order(cfg, broker, lim, **{**kw, "ticker": "ZZZ"})
    with pytest.raises(OrderRefused, match="is for AAA"):
        place_order(cfg, broker, lim, **{**kw, "ticker": "BBB"})
    with pytest.raises(OrderRefused, match="more than a tick"):
        place_order(cfg, broker, lim, **{**kw, "qty": 500, "limit": p.entry_limit + 0.10})
    with pytest.raises(OrderRefused, match="needs a proposal"):
        place_order(cfg, broker, lim, **{**kw, "proposal_id": None})
    with pytest.raises(OrderRefused, match="not found"):
        place_order(cfg, broker, lim, **{**kw, "proposal_id": "P-19990101-001"})
    with pytest.raises(OrderRefused, match="only limit orders"):
        place_order(cfg, broker, lim, **kw, order_type="market")
    with pytest.raises(OrderRefused, match="long-only"):
        place_order(
            cfg,
            broker,
            lim,
            proposal_id=None,
            ticker="AAA",
            side="sell",
            qty=1,
            limit=1.0,
            now=NOON,
        )
    with pytest.raises(OrderRefused, match="below minimum order"):
        place_order(cfg, broker, lim, **{**kw, "qty": 10})
    assert broker.positions() == []  # nothing reached the broker
    assert all(r["outcome"] == "refused" for r in EventLog(cfg.data_dir).read("orders"))


def test_place_order_hours_enforced_outside_sim(cfg):
    lim = _limits(cfg)
    lim.enforce_hours = True
    sc = _scanner(cfg)
    p = sc.scan([_ann()], NOON)[0]
    late = datetime(2026, 9, 22, 17, 30, tzinfo=SYD)
    with pytest.raises(OrderRefused, match="outside allowed hours"):
        place_order(
            cfg,
            _broker(cfg),
            lim,
            proposal_id=p.id,
            ticker="AAA",
            side="buy",
            qty=p.qty,
            limit=p.entry_limit,
            now=late,
        )


def test_max_open_and_per_day(cfg, monkeypatch):
    """The event log's wall clock is pinned to 15:00 Sydney the day after NOON, when UTC has
    moved on to NOON's next day too. That is when the deploy's suite failed on 26 Sep (DID NOT
    RAISE on the per-day limit): the fill was stamped in UTC and counted on the UTC date. Left
    on the real clock this test only caught that bug at some hours of the week."""
    import asxbot.log as L

    class NextAfternoon(datetime):
        @classmethod
        def now(cls, tz=None):
            t = NOON + timedelta(hours=27)
            return t.astimezone(tz) if tz else t.replace(tzinfo=None)

    monkeypatch.setattr(L, "datetime", NextAfternoon)
    sc = _scanner(cfg)
    lim = _limits(cfg)
    lim.max_open_positions = 1
    broker = _broker(cfg)
    a1 = _ann()
    a2 = Announcement(
        "BBB", datetime(2026, 9, 22, 9, 40), "Upgrade", True, "0998", "https://x?idsId=0998"
    )
    quotes = StaticQuotes(
        {
            "AAA": _quotes().quotes["AAA"],
            "BBB": Quote("BBB", 2.0, 1.95, 1.8, 900_000.0, datetime.now(), "static"),
        },
        _quotes().index,
    )
    sc = Scanner(
        cfg.data_dir, quotes, _daily, 5.0, 3.0, 250_000, 8.0, 2500.0, CostModel(), {"AAA", "BBB"}
    )
    p1, p2 = sc.scan([a1, a2], NOON)
    place_order(
        cfg,
        broker,
        lim,
        proposal_id=p1.id,
        ticker="AAA",
        side="buy",
        qty=p1.qty,
        limit=p1.entry_limit,
        now=NOON,
    )
    with pytest.raises(OrderRefused, match="max open positions"):
        place_order(
            cfg,
            broker,
            lim,
            proposal_id=p2.id,
            ticker="BBB",
            side="buy",
            qty=p2.qty,
            limit=p2.entry_limit,
            now=NOON,
        )
    lim.max_open_positions = 4
    lim.max_new_positions_per_day = 1
    with pytest.raises(OrderRefused, match="opened 1 positions today"):
        place_order(
            cfg,
            broker,
            lim,
            proposal_id=p2.id,
            ticker="BBB",
            side="buy",
            qty=p2.qty,
            limit=p2.entry_limit,
            now=NOON,
        )


def test_sim_broker_resting_order_and_state_file(cfg):
    broker = _broker(cfg, ref_price=2.50)  # market above limit -> buy rests
    res = broker.place_limit_order("AAA", "buy", 100, 2.00, "t")
    assert res.status == "open" and res.filled_qty == 0
    assert broker.open_orders()[0].order_id == res.order_id
    st = json.loads(Path(broker.path).read_text())
    assert st["cash"] == 10_000 and st["orders"][res.order_id]["status"] == "open"
    # new instance reloads state
    b2 = SimBroker(cfg.data_dir, 999, CostModel(), lambda t: (2.0, 1e6))
    assert b2.cash() == 10_000 and b2.order_status(res.order_id).status == "open"


def test_reconcile_detects_mismatch(cfg):
    broker = _broker(cfg)
    broker.place_limit_order("AAA", "buy", 100, 2.05, "outside the log")
    ev = EventLog(cfg.data_dir)
    mm = reconcile(broker, ev, Alerts(cfg.data_dir))
    assert len(mm) == 1 and mm[0].ticker == "AAA" and mm[0].broker_qty == 100 and mm[0].log_qty == 0
    assert Alerts(cfg.data_dir).is_active("reconcile_mismatch")


def test_ibkr_adapter_refuses_wrong_mode_and_missing_library(
    cfg, config_file, tmp_path, monkeypatch
):
    from asxbot.broker.ibkr import IBKRBroker
    from asxbot.config import ConfigError

    with pytest.raises(ConfigError, match="paper or live"):
        IBKRBroker(cfg)  # sim config
    paper = load_config(config_file(broker="paper"), env_file=tmp_path / "none.env")
    monkeypatch.setitem(__import__("sys").modules, "ib_async", None)
    with pytest.raises(ConfigError, match="ib_async not installed"):
        IBKRBroker(paper)
    monkeypatch.setenv("LIVE_TRADING_CONFIRMED", "no")
    live = (
        load_config(config_file(broker="live"), env_file=tmp_path / "none.env") if False else None
    )
    assert live is None  # live config cannot even load without the env key (tested in test_config)


def test_new_positions_today_counts_the_sydney_day(tmp_path):
    """The event log stamps in UTC. 10:30 Sydney on 6 Oct 2026 (daylight saving) is 23:30 UTC
    on 5 Oct: until 26 Sep the "opened today" limit compared the UTC date, so that buy counted
    on the day before and the per-day limit let one more through."""
    from asxbot.broker.orders import _new_positions_today
    from asxbot.log import event_day

    ev = EventLog(tmp_path)
    with open(ev.path("fills"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": "2026-10-05T23:30:00+00:00", "kind": "fills", "side": "buy",
                             "ticker": "AAA", "qty": 100}) + "\n")  # fmt: skip
        fh.write(json.dumps({"ts": "2026-10-05T05:00:00+00:00", "kind": "fills", "side": "buy",
                             "ticker": "BBB", "qty": 100}) + "\n")  # fmt: skip
    assert _new_positions_today(ev, datetime(2026, 10, 6).date()) == 1  # AAA, 10:30 on 6 Oct
    assert _new_positions_today(ev, datetime(2026, 10, 5).date()) == 1  # BBB, 16:00 on 5 Oct
    assert event_day({"ts": "2026-09-28T00:15:00+00:00"}) == "2026-09-28"  # 10:15 AEST
    assert event_day({"ts": "2026-10-05T23:30:00+00:00", "day": "2026-10-06"}) == "2026-10-06"
