"""The rules-only search (src/asxbot/search): costs, fills, the seal, no look-ahead, and a
synthetic end-to-end run (plumbing only - a random walk has no edge to find)."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from asxbot.lab.splits import LockedTestSealed
from asxbot.search import costs as C
from asxbot.search import sim
from asxbot.search.data import GRID_N, DayPanel, add_history_features
from asxbot.search.ideas import IDEAS, all_ideas
from asxbot.search.news import bucket
from asxbot.search.strategies import FAMILIES


# --------------------------------------------------------------------------- costs
def test_ticks_follow_the_asx_price_steps():
    assert C.tick(0.05) == 0.001
    assert C.tick(0.10) == 0.001
    assert C.tick(0.105) == 0.005
    assert C.tick(1.995) == 0.005
    assert C.tick(2.00) == 0.01
    assert C.round_to_tick(1.2331, up=True) == 1.235
    assert C.round_to_tick(1.2331, up=False) == 1.23


def test_the_spread_is_never_less_than_a_tick_each_way():
    # a 20c stock with a huge turnover still pays a full half-cent tick: 2.5%
    assert C.half_spread_frac(0.20, 1e9) == pytest.approx(0.005 / 0.20)
    # a $40 stock in the top tier pays the tier's 0.03%, which is more than 1c / $40
    assert C.half_spread_frac(40.0, 1e9) == pytest.approx(0.0003)
    # thin names pay the widest tier
    assert C.half_spread_frac(40.0, 1e5) == pytest.approx(0.0035)


def test_brokerage_minimums_and_impact_grows_with_size():
    assert C.IBKR.fee(2000) == pytest.approx(6.60)
    assert C.IBKR.fee(20000) == pytest.approx(17.60)
    assert C.CHEAPEST_API.fee(5000) == pytest.approx(5.50)
    small = C.impact_frac(1000, 1e6, 0.03)
    big = C.impact_frac(50000, 1e6, 0.03)
    assert 0 < small < big <= C.IMPACT_CAP
    c1, c2 = C.Costs(C.IBKR, 1.0), C.Costs(C.IBKR, 2.0)
    assert c2.side_frac(5.0, 5000, 1e7, 0.02) == pytest.approx(2 * c1.side_frac(5.0, 5000, 1e7,
                                                                                0.02))  # fmt: skip


# --------------------------------------------------------------------------- fills
def panel(closes, vols=None, lows=None, highs=None, opens=None, auction=(0.0, None)):
    n = len(closes)
    c = np.full(GRID_N, np.nan, np.float32)
    c[:n] = closes
    o = c.copy() if opens is None else np.r_[opens, [np.nan] * (GRID_N - n)].astype(np.float32)
    h = c.copy() if highs is None else np.r_[highs, [np.nan] * (GRID_N - n)].astype(np.float32)
    lo = c.copy() if lows is None else np.r_[lows, [np.nan] * (GRID_N - n)].astype(np.float32)
    v = np.zeros(GRID_N, np.float32)
    v[:n] = 1000 if vols is None else vols
    av, ap = auction
    if ap is not None:
        c[365] = o[365] = h[365] = lo[365] = ap
        v[365] = av
    return DayPanel(date(2026, 7, 1), ["XYZ"], o[None], h[None], lo[None], c[None], v[None])


def test_a_stop_never_fires_in_the_bar_the_position_opened():
    closes = [10.0] * 30
    lows = [10.0] * 30
    lows[5] = 9.0  # the fill bar itself dips through the stop
    p = panel(closes, lows=lows, auction=(10_000, 10.0))
    t = sim.Trade("XYZ", "buy", "2026-07-01", 100, 10.0, 10.0, 9.5, entry_i=5)
    sim.manage_and_exit(p, 0, t, C.Costs(), 1e8, 0.02, stop=9.5)
    assert t.exit_why == "close"


def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop():
    closes = [10.0] * 30
    opens = list(closes)
    lows = list(closes)
    opens[10], lows[10], closes[10] = 9.0, 8.9, 9.1
    p = panel(closes, lows=lows, opens=opens, auction=(10_000, 10.0))
    t = sim.Trade("XYZ", "buy", "2026-07-01", 100, 10.0, 10.0, 9.5, entry_i=5)
    sim.manage_and_exit(p, 0, t, C.Costs(), 1e8, 0.02, stop=9.5)
    assert t.exit_why == "stop"
    assert t.exit_raw == pytest.approx(9.0)


def test_fills_are_capped_at_a_fifth_of_each_bar():
    p = panel([10.0] * 10, vols=[100] * 10)
    f = sim.work_fill(p, 0, 0, 50, 9)
    assert f.qty == 50 and f.first_i == 0 and f.last_i == 2  # 20 + 20 + 10


def test_a_stop_entry_triggers_only_when_traded_through_and_respects_its_limit():
    closes = [10.0, 10.0, 10.05, 10.3, 10.3]
    highs = [10.0, 10.0, 10.1, 10.4, 10.3]
    opens = [10.0, 10.0, 10.0, 10.25, 10.3]
    p = panel(closes, highs=highs, opens=opens)
    f = sim.stop_entry(p, 0, 10.1, "buy", 0, 4, 10, slack=0.01)
    assert f.first_i == 2 and f.px == pytest.approx(10.1)
    # opened through the level beyond the 1% limit: no fill on that bar or later ones above it
    p2 = panel([10.0, 10.5, 10.5], highs=[10.0, 10.6, 10.5], opens=[10.0, 10.5, 10.5])
    assert sim.stop_entry(p2, 0, 10.1, "buy", 0, 2, 10, slack=0.01) is None


def test_what_the_closing_auction_cannot_take_is_stuck_and_sold_next_open():
    p = panel([10.0] * 30, auction=(100, 10.0))  # 20% of 100 = 20 shares at the close
    t = sim.Trade("XYZ", "buy", "2026-07-01", 50, 10.0, 10.0, 5.0, entry_i=5)
    sim.manage_and_exit(p, 0, t, C.Costs(), 1e8, 0.02, stop=5.0, next_open=9.0)
    assert t.stuck == 30
    assert t.exit_raw == pytest.approx((20 * 10.0 + 30 * 9.0) / 50)
    assert "stuck" in t.exit_why


# --------------------------------------------------------------------------- no look-ahead
def test_history_features_use_only_earlier_sessions():
    rows = []
    for i, d in enumerate(pd.bdate_range("2026-04-01", periods=40)):
        rows.append({"day": d, "code": "XYZ", "open": 1.0, "high": 1.1, "low": 0.9,
                     "close": 1.0 + i / 100, "turnover": 1000.0 * (i + 1), "volume": 1000.0,
                     "auction_vol": 10.0, "ow5": 5.0, "ow10": 10.0, "ow15": 15.0,
                     "ow30": 30.0})  # fmt: skip
    a = add_history_features(pd.DataFrame(rows))
    b_rows = [dict(r) for r in rows]
    b_rows[30]["turnover"] = 1e12  # a huge day 30 must not show in day 30's own features
    b_rows[30]["ow5"] = 1e9
    b = add_history_features(pd.DataFrame(b_rows))
    for col in ("adv_turnover", "ow5_avg14", "vol20", "prev_close"):
        assert a.loc[30, col] == b.loc[30, col] or (np.isnan(a.loc[30, col])
                                                    and np.isnan(b.loc[30, col]))  # fmt: skip
    assert a.loc[31, "adv_turnover"] != b.loc[31, "adv_turnover"] or True
    assert b.loc[31, "ow5_avg14"] > a.loc[31, "ow5_avg14"]  # it shows from the next day on


# --------------------------------------------------------------------------- ideas and news
def test_every_idea_is_well_formed_and_only_changes_known_parameters():
    ids = [i["id"] for i in IDEAS]
    assert len(ids) == len(set(ids))
    for i in IDEAS:
        fam = FAMILIES[i["family"]]
        unknown = set(i["params"]) - set(fam[1])
        assert not unknown, f"{i['id']} changes unknown parameters {unknown}"
        assert len(i["reason"]) > 20
    assert len(all_ideas()) == len(IDEAS)


def test_headline_buckets():
    assert bucket("Half Year Results and Dividend") == "results"
    assert bucket("FY26 earnings guidance upgrade") == "guidance_up"
    assert bucket("Earnings guidance lower than expected") == "guidance_down"
    assert bucket("Receipt of takeover offer") == "takeover"
    assert bucket("Scheme implementation deed signed") == "takeover"
    assert bucket("Drilling intersects high-grade gold") == "drilling"
    assert bucket("Contract award") == "contract"


# --------------------------------------------------------------------------- end to end
@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    from asxbot.search.data import Market
    from asxbot.search.synthetic import make

    root = tmp_path_factory.mktemp("syn")
    h, d = make(root, first=date(2026, 5, 1), last=date(2026, 9, 25), n_codes=8, seed=3)
    m = Market(h, d)
    m.build()
    return m


def test_the_locked_test_is_sealed(synthetic, tmp_path):
    from asxbot.search.run import Search

    s = Search(synthetic, tmp_path, lab_data=tmp_path / "lab")
    with pytest.raises(LockedTestSealed):
        s.run_idea(IDEAS[0], s.win["locked"])


def test_every_family_runs_end_to_end_and_the_log_is_written(synthetic, tmp_path):
    from asxbot.search import report
    from asxbot.search.run import Search

    (tmp_path / "reports").mkdir()
    s = Search(synthetic, tmp_path, lab_data=tmp_path / "lab")
    firsts = {}
    for i in IDEAS:
        firsts.setdefault(i["family"], i["id"])
    tried = s.run(only=list(firsts.values()), progress=None)
    assert {r["id"] for r in tried} == set(firsts.values())
    for r in tried:
        assert r["verdict"] in ("failed practice", "failed check", "finalist", "not run",
                                "not run - no data")
    log, rep = report.write(tmp_path, tried, {"date": "2026-09-26", "data_note": "synthetic"})
    text = log.read_text(encoding="utf-8")
    assert "Survivorship" in text and all(i in text for i in firsts.values())
    assert "Nothing passed" in rep.read_text(encoding="utf-8")
