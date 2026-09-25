"""Day-1 fixes (25 Sep 2026, from the agent's own day-trader rejections):

  1. the opening-range breakout and gap-and-go fire only on THE FIRST bar that closes
     beyond the range, with the volume test on that bar - not on a heavy bar hours after a
     quiet break (CWY, HDN, AUB, ORI, CHC, DRR on 25 Sep, each rejected as "the scanner's
     30-min low doesn't match the bars"; the range was right, the bar was not the break);
  2. a setup whose 1R at the largest size the rules allow is below 2x the round-trip cost
     is filtered by the scanner and logged (about 15 of day 1's 36 rejections);
  3. a reinstated stock with no last trade still has a quote from IBKR before the open.

The six cases are replayed from tests/fixtures/orb_day1_20260925.json, the bars Yahoo's
cache held for those stocks that day (10:00 to two minutes past the bar the scanner fired
on), with the bar the scanner fired on and the first real break named in the fixture."""

import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from asxbot.arena import daytrader as DT
from asxbot.arena.levels import load_playbook
from asxbot.backtest.costs import CostModel
from asxbot.config import load_config

SYD = ZoneInfo("Australia/Sydney")
DAY = date(2026, 9, 25)
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "orb_day1_20260925.json"


@pytest.fixture
def cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )


def _bars(rows: list) -> pd.DataFrame:
    idx = [datetime.combine(DAY, datetime.strptime(r[0], "%H:%M").time(), tzinfo=SYD)
           for r in rows]  # fmt: skip
    cols = ["open", "high", "low", "close", "volume"]
    df = pd.DataFrame([r[1:] for r in rows], index=pd.DatetimeIndex(idx), columns=cols)
    return df.astype(float)


def _ctx(cfg, bars: pd.DataFrame, code: str) -> DT.Ctx:
    conf = load_playbook(cfg, "asx_daytrader").raw["setups"]
    index = pd.DataFrame({"open": 8000.0, "high": 8000.0, "low": 8000.0, "close": 8000.0,
                          "volume": 0.0}, index=bars.index)  # fmt: skip
    usual = pd.Series(1.0, index=range(360))  # tiny "usual" volume: RVOL is never the gate
    prev = float(bars["open"].iloc[0])
    return DT.Ctx(code, DAY, bars, index, prev, 8000.0, usual, True, False, conf)


def _orb(found):
    return [s for s in found if s.setup == "opening_range_breakout"]


@pytest.mark.parametrize("code", ["CWY", "HDN", "AUB", "ORI", "CHC", "DRR"])
def test_day_1s_late_breakouts_no_longer_fire_on_the_bar_the_scanner_fired_on(cfg, code):
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    case = cases[code]
    bars = _bars(case["bars"])
    c = _ctx(cfg, bars, code)
    found = _orb(DT.detect(c, None, set(), DT._t("15:45")))
    fired = [s for s in found if s.trigger_bar[11:16] == case["fired_at"]]
    assert fired == [], f"{code}: still fires at {case['fired_at']}, {found}"
    # the range the scanner reported was the range the bars hold
    t = bars.index.time
    rng = bars[(t >= datetime.strptime("10:00", "%H:%M").time())
               & (t < datetime.strptime("10:30", "%H:%M").time())]  # fmt: skip
    hi, lo = float(rng["high"].max()), float(rng["low"].min())
    assert lo == pytest.approx(case["range_low_stated"], abs=1e-6)
    first_up, first_down = DT._first_breaks(bars, ["10:30", "14:30"], hi, lo)
    assert first_down is not None and first_down.strftime("%H:%M") == case["first_break"]
    for s in found:  # anything that does fire is the first break, and says so
        assert s.trigger_bar[11:16] == case["first_break"]
        assert s.context["first_break"] == case["first_break"]
        assert s.context["range_low"] == pytest.approx(case["range_low"])


def test_a_first_break_with_the_volume_fires_there_and_one_without_never_fires(cfg):
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # AUB's first close below the range (10:38) came on 8.6x the prior ten bars: a setup
    # THERE, with the stop at the range's midpoint and the range in its context.
    c = _ctx(cfg, _bars(cases["AUB"]["bars"]), "AUB")
    found = _orb(DT.detect(c, None, set(), DT._t("15:45")))
    assert [s.trigger_bar[11:16] for s in found] == ["10:38"] and found[0].side == "short"
    assert found[0].stop == pytest.approx((27.48 + 27.76) / 2, abs=1e-6)
    assert "first close" in found[0].why
    # HDN's first close below (10:57) came on a tenth of the prior bars' volume: no
    # opening-range short at all that day, however heavy a later bar is.
    c = _ctx(cfg, _bars(cases["HDN"]["bars"]), "HDN")
    assert _orb(DT.detect(c, None, set(), DT._t("15:45"))) == []


def test_the_first_break_is_stable_as_bars_arrive(cfg):
    """Seen minute by minute, as the live scan sees it: the verdict at the first break is
    the verdict for the day; later bars never turn it into a breakout."""
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    bars = _bars(cases["CWY"]["bars"])
    fired: set = set()
    seen = []
    since = None
    for n in range(20, len(bars) + 1):
        c = _ctx(cfg, bars.iloc[:n], "CWY")
        seen += _orb(DT.detect(c, since, fired, DT._t("15:45")))
        since = bars.index[n - 1]
    assert seen == []


def test_gap_and_go_also_takes_only_the_first_close_beyond_the_range(cfg):
    rows = {(10, m): (1.05, 1.06, 1.04, 1.05, 3000) for m in range(0, 15)}
    rows[(10, 15)] = (1.05, 1.08, 1.05, 1.07, 3000)  # the first close above 1.06
    rows[(10, 16)] = (1.07, 1.09, 1.06, 1.08, 30000)  # heavier, but not the first
    bars = pd.DataFrame(
        list(rows.values()),
        index=pd.DatetimeIndex([datetime(2026, 9, 25, h, m, tzinfo=SYD) for h, m in rows]),
        columns=["open", "high", "low", "close", "volume"],
    ).astype(float)
    conf = load_playbook(cfg, "asx_daytrader").raw["setups"]
    index = pd.DataFrame({"open": 8000.0, "high": 8000.0, "low": 8000.0, "close": 8000.0,
                          "volume": 0.0}, index=bars.index)  # fmt: skip
    usual = pd.Series(1.0, index=range(360))
    c = DT.Ctx("GAP", DAY, bars, index, 1.0, 8000.0, usual, True, False, conf)
    found = [s for s in DT.detect(c, None, set(), DT._t("15:45")) if s.setup == "gap_and_go"]
    assert [s.trigger_bar[11:16] for s in found] == ["10:15"]
    assert found[0].context["first_break"] == "10:15" and found[0].context["range_high"] == 1.06
    # the same day with no volume at the first close above: nothing, not the 10:16 bar
    c.usual = pd.Series(1e9, index=range(360))  # RVOL tiny everywhere
    assert [s for s in DT.detect(c, None, set(), DT._t("15:45")) if s.setup == "gap_and_go"] == []


# -- 2. uneconomic setups are filtered by the scanner -----------------------------------
def _arena(cfg):
    broker = SimpleNamespace(costs=CostModel.from_config(cfg), adv_lookup=lambda t: 2_000_000.0,
                             prices=lambda acct: {},
                             median_turnover=lambda t: 5_000_000.0)  # fmt: skip
    return SimpleNamespace(cfg=cfg, broker=broker)


def test_a_setup_whose_1r_cannot_beat_twice_the_costs_is_filtered_and_says_why(cfg):
    pb = load_playbook(cfg, "asx_daytrader")
    arena = _arena(cfg)
    acct = SimpleNamespace(equity=lambda prices: 20_000.0)
    tight = DT.Setup("PME", "vwap_reclaim", "buy", "2026-09-25T12:47", 160.0, 159.5, 1.0, "")
    t = DT.terms(arena, pb, acct, tight)
    assert t["qty"] == 30 and t["value"] <= 5000  # the $5,000 cap binds, not the risk budget
    ok, why = DT.economic(arena, pb, t, tight)
    assert not ok and why.startswith("uneconomic: 1R $") and "round-trip cost" in why
    assert float(why.split("1R $")[1].split(" ")[0].replace(",", "")) == 15  # 30 x $0.50
    wide = DT.Setup("PME", "vwap_reclaim", "buy", "2026-09-25T12:47", 160.0, 155.0, 1.0, "")
    t2 = DT.terms(arena, pb, acct, wide)
    assert t2["risk"] == pytest.approx(100.0, abs=7.0)  # the 0.5% budget binds
    assert DT.economic(arena, pb, t2, wide) == (True, "")


def test_the_filter_is_off_without_the_dated_key_and_on_in_the_committed_config(cfg):
    pb = load_playbook(cfg, "asx_daytrader")
    assert float(pb.raw["entry"]["min_r_over_costs"]) == 2.0
    off = SimpleNamespace(raw={**pb.raw, "entry": {**pb.raw["entry"], "min_r_over_costs": 0}})
    t = {"qty": 30, "limit": 161.6, "stop": 159.5, "risk": 63.0, "value": 4848.0}
    s = DT.Setup("PME", "vwap_reclaim", "buy", "2026-09-25T12:47", 160.0, 159.5, 1.0, "")
    assert DT.economic(_arena(cfg), off, t, s) == (True, "")


def test_the_report_counts_what_the_filter_took_out(cfg):
    from asxbot.arena.report import daytrader_facts

    DT.save_state(cfg.data_dir, DAY, {"signals": [
        {"setup": "vwap_reclaim", "bot": {"skipped": "uneconomic: 1R $15 ..."},
         "agent": {"skipped": "uneconomic: 1R $15 ..."}},
        {"setup": "vwap_reclaim", "bot": {"order_id": "ARN-1"}, "agent": {"rejected": "no"}},
    ], "universe": ["A"], "history": {"missing": ["ZZZ"]}})  # fmt: skip
    f = daytrader_facts(cfg, DAY)
    assert f["uneconomic"] == 1 and f["setups_found"] == 2 and f["history"]["missing"] == ["ZZZ"]


def test_the_agent_sees_the_range_the_scanner_used(cfg):
    ctx = {"range_high": 27.76, "range_low": 27.48, "range_minutes": 30, "first_break": "10:38"}
    s = DT.Setup("AUB", "opening_range_breakout", "short", "2026-09-25T10:38", 27.3, 27.62,
                 2.0, "first close below", ctx)  # fmt: skip
    line = DT._range_line(s)
    assert "low 27.48" in line and "high 27.76" in line and "FIRST close" in line
    assert DT._range_line(DT.Setup("X", "vwap_reclaim", "buy", "t", 1, 0.9, 1, "")) == ""


# -- 3. a reinstated stock with no last trade still has a quote before the open -----------
def test_a_pre_open_quote_with_only_a_previous_close_comes_through(tmp_path):
    from asxbot.ibkr.live import LiveGateway
    from test_ibkr_live import FakeIB, FakeTicker, Script, Wall, settings, wait_for

    script = Script()
    script.known["NXN"] = (777, "STK", "ASX", "AUD")
    script.quotes["NXN"] = FakeTicker(close=0.115, halted=0)  # reinstated: no trade yet
    gw = LiveGateway(settings(), ib_factory=lambda: FakeIB(script),
                     wall=Wall(datetime(2026, 9, 28, 9, 43, tzinfo=SYD)))  # fmt: skip
    gw.start()
    try:
        assert wait_for(lambda: gw.ready)
        q = gw.quote("NXN")
        assert q is not None and q["prev_close"] == 0.115 and q["last"] is None
        from asxbot.ibkr.feed import IBKRQuotes

        quote = IBKRQuotes(gw).quote("NXN")
        assert quote is not None and quote.last == 0.115 and quote.prev_close == 0.115
    finally:
        gw.stop(3)
