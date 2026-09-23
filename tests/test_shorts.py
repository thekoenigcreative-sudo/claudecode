"""S1 and S2, the short-side backtest: the rules as frozen in config.yaml, and a simulator
whose costs, borrow and stop arithmetic can be checked by hand. No network."""

import pandas as pd
import pytest

from asxbot.backtest.costs import CostModel
from asxbot.backtest.shorts import s1_events, s2_events, simulate_shorts
from asxbot.backtest.signals import build_panels


def _frames(n=120, tickers=("AAA", "BBB"), price=10.0, vol=100_000.0):
    """Flat at $10 on 100k shares: $1m a day, well above the $250k floor."""
    idx = pd.bdate_range("2021-01-01", periods=n, name="date")
    out = {
        t: pd.DataFrame(
            {"open": price, "high": price, "low": price, "close": price, "volume": vol}, index=idx
        )
        for t in tickers
    }
    return out, idx


def _panels(frames, idx):
    flat = pd.Series(7000.0, index=idx)
    return build_panels(frames, flat, flat, turnover_floor=250_000, turnover_window=20)


def _ann(*rows):
    """rows: (code, released_at, headline, type, price_sensitive)"""
    return pd.DataFrame(
        [
            {"code": c, "released_at": pd.Timestamp(ts), "headline": h, "type": ty,
             "price_sensitive": ps, "ids_id": f"{i:08d}", "pre_open": False}
            for i, (c, ts, h, ty, ps) in enumerate(rows)
        ]
    )  # fmt: skip


@pytest.fixture
def rules(base_config):
    """The rules exactly as committed in config.yaml."""
    s = base_config["strategies"]
    return s["S1_earnings_miss"], s["S2_placement_supply"]


def _at(d, hhmm):
    return f"{d.date()} {hhmm}"


def test_s1_needs_a_results_announcement_a_5pct_fall_and_3x_volume(rules):
    s1, _ = rules
    frames, idx = _frames(tickers=("AAA", "BBB", "CCC", "DDD"))
    d = idx[60]
    for t, close in (("AAA", 9.0), ("BBB", 9.0), ("CCC", 9.6), ("DDD", 9.0)):
        frames[t].loc[d, ["close", "low"]] = close
        frames[t].loc[d, "volume"] = 500_000.0  # 5x
    ann = _ann(
        ("AAA", _at(d, "08:30"), "Half Year Results", "results", True),
        ("BBB", _at(d, "08:30"), "Contract award", "contract", True),  # not results
        ("CCC", _at(d, "08:30"), "Half Year Results", "results", True),  # only -4%
        ("DDD", _at(d, "11:00"), "Half Year Results", "results", True),  # reacts the next day
    )
    ev = s1_events(_panels(frames, idx), ann, s1)
    assert list(ev["ticker"]) == ["AAA"]
    assert ev.loc[0, "date"] == d
    assert ev.loc[0, "fall_rel"] == pytest.approx(-10.0)
    assert ev.loc[0, "vol_mult"] == pytest.approx(5.0)


def test_s2_finds_equity_placements_and_covers_from_the_quotation_notice(rules):
    _, s2 = rules
    frames, idx = _frames(tickers=("AAA", "BBB", "CCC", "DDD", "EEE"))
    d = idx[60]
    ann = _ann(
        ("AAA", _at(d, "08:00"), "Successful completion of $50m Placement", "capital_raising",
         True),
        # the quotation notice two sessions later, after 10:00: it reacts the session after
        ("AAA", _at(idx[62], "17:00"), "Application for quotation of securities - AAA",
         "other", False),
        ("BBB", _at(d, "08:00"), "Placement", "capital_raising", True),  # no notice follows
        ("CCC", _at(d, "08:00"), "Replacement of Chief Executive", "other", True),
        ("DDD", _at(d, "08:00"), "Successful US Private Placement", "capital_raising", True),
        ("EEE", _at(d, "08:00"), "$300m Placement of Subordinated Notes", "capital_raising",
         True),
    )  # fmt: skip
    ev = s2_events(_panels(frames, idx), ann, s2).set_index("ticker")
    assert sorted(ev.index) == ["AAA", "BBB"]  # replacement, US private placement, notes out
    assert ev.loc["AAA", "cover_pos"] == 63 + 5  # quotation reacts at 63, cover 5 later
    assert ev.loc["BBB", "cover_pos"] == 60 + 10  # no notice: the fixed 10 sessions


def test_a_short_pays_costs_and_borrow_and_is_covered_at_the_horizon():
    frames, idx = _frames(tickers=("AAA",))
    frames["AAA"].loc[idx[62] :, ["open", "high", "low", "close"]] = 9.0  # falls 10%
    p = _panels(frames, idx)
    ev = pd.DataFrame([{"date": idx[60], "ticker": "AAA", "priority": 1.0}])
    costs = CostModel()
    kw = dict(starting_capital=20_000, max_positions=4, stop_pct=8.0, entry_lag=1, hold_days=10)
    r = simulate_shorts(p, ev, costs, borrow_pct_annual=1.0, **kw)
    t = r.trades.iloc[0]
    assert t["entry_date"] == idx[61] and t["exit_date"] == idx[71] and t["reason"] == "time"

    fill_in = 10.0 * (1 - costs.slippage_pct(5_000, 1_000_000))
    qty = int(5_000 // fill_in)
    assert t["qty"] == qty
    # Borrow: the short's value at each close, for the nights until the next session.
    nights = [(idx[i + 1] - idx[i]).days for i in range(61, 71)]
    closes = [10.0] + [9.0] * 9
    borrow = sum(c * qty * 0.01 / 365 * n for c, n in zip(closes, nights, strict=True))
    assert t["borrow"] == pytest.approx(borrow)
    adv_out = p.adv_dollar.at[idx[71], "AAA"]  # the prior 20 sessions now include the $9 days
    fill_out = 9.0 * (1 + costs.slippage_pct(9.0 * qty, adv_out))
    pnl = fill_in * qty - 6.60 - fill_out * qty - 6.60 - borrow  # both fees at the minimum
    assert t["pnl"] == pytest.approx(pnl)
    assert 9.0 < t["ret_pct"] < 10.0

    five = simulate_shorts(p, ev, costs, borrow_pct_annual=5.0, **kw).trades.iloc[0]
    assert five["borrow"] == pytest.approx(5 * borrow)


def test_a_stop_is_tested_on_the_close_and_a_gap_goes_straight_through_it():
    """A short's loss has no ceiling: the stop is a close above entry + 8%, covered at the
    next open, wherever that open is."""
    frames, idx = _frames(tickers=("AAA",))
    f = frames["AAA"]
    f.loc[idx[63], ["close", "high"]] = 11.5  # above 10 x 1.08
    f.loc[idx[64] :, ["open", "high", "low", "close"]] = 13.0  # and gaps higher
    ev = pd.DataFrame([{"date": idx[60], "ticker": "AAA", "priority": 1.0}])
    r = simulate_shorts(
        _panels(frames, idx), ev, CostModel(), 1.0, 20_000, 4, stop_pct=8.0, entry_lag=1,
        hold_days=10,
    )  # fmt: skip
    t = r.trades.iloc[0]
    assert t["reason"] == "stop" and t["exit_date"] == idx[64]
    assert t["ret_pct"] < -30  # far past the 8% the stop was meant to cap


def test_the_book_is_never_shorter_than_the_gross_limit_allows():
    frames, idx = _frames(tickers=("AAA", "BBB", "CCC"))
    ev = pd.DataFrame(
        [{"date": idx[60], "ticker": t, "priority": p} for t, p in (("AAA", 3), ("BBB", 2),
                                                                     ("CCC", 1))]
    )  # fmt: skip
    r = simulate_shorts(
        _panels(frames, idx), ev, CostModel(), 1.0, 20_000, 4, stop_pct=8.0, entry_lag=1,
        hold_days=10, max_gross_x=0.5,
    )  # fmt: skip
    # a quarter of equity each: two fit under 0.5x, the third would not; priority decides
    assert sorted(r.trades["ticker"]) == ["AAA", "BBB"]
