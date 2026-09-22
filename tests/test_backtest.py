import numpy as np
import pandas as pd
import pytest

from asxbot.backtest.baseline import run_momentum
from asxbot.backtest.costs import CostModel
from asxbot.backtest.engine import simulate
from asxbot.backtest.metrics import cagr, max_drawdown, summarise
from asxbot.backtest.signals import (
    announcement_events,
    build_panels,
    detect_events,
    own_announcements,
    reaction_sessions,
)
from fake_provider import synthetic_bars


def test_costs_brokerage_min_and_pct():
    c = CostModel()
    assert c.brokerage(1000) == 6.60
    assert c.brokerage(20000) == pytest.approx(17.6)


def test_costs_slippage_scales_with_liquidity_and_caps():
    c = CostModel()
    assert c.slippage_pct(2500, 1_000_000) == pytest.approx((0.10 + 0.5 * 0.0025) / 100)
    assert c.slippage_pct(2500, 100) == pytest.approx(0.01)  # capped at 1%
    assert c.slippage_pct(2500, None) == pytest.approx(0.01)
    assert CostModel(multiplier=2).slippage_pct(2500, 1_000_000) == pytest.approx(
        2 * (0.10 + 0.00125) / 100
    )
    assert c.buy_price(10, 2500, 1e6) > 10 > c.sell_price(10, 2500, 1e6)


def _flat_frames(n=120, tickers=("AAA", "BBB"), price=10.0, vol=100_000.0):
    idx = pd.bdate_range("2021-01-01", periods=n, name="date")
    out = {}
    for t in tickers:
        out[t] = pd.DataFrame(
            {"open": price, "high": price, "low": price, "close": price, "volume": vol}, index=idx
        )
    return out, idx


def _panels(frames, idx):
    index_close = pd.Series(7000.0, index=idx)
    index_open = pd.Series(7000.0, index=idx)
    return build_panels(frames, index_open, index_close, turnover_floor=250_000, turnover_window=20)


def test_detect_events_planted_gap():
    frames, idx = _flat_frames()
    d = idx[60]
    f = frames["AAA"]
    f.loc[d:, ["open", "high", "low", "close"]] = 11.0  # +10% gap, index flat
    f.loc[d, "volume"] = 500_000.0  # 5x
    p = _panels(frames, idx)
    ev = detect_events(p, gap_pct=5.0, vol_mult=3.0, vol_window=20)
    assert len(ev) == 1
    assert ev.loc[0, "ticker"] == "AAA" and ev.loc[0, "date"] == d
    assert ev.loc[0, "gap_rel"] == pytest.approx(10.0)
    assert ev.loc[0, "vol_mult"] == pytest.approx(5.0)
    # same gap but volume only 2x -> no event
    f.loc[d, "volume"] = 200_000.0
    assert detect_events(_panels(frames, idx), 5.0, 3.0, 20).empty


def test_detect_events_respects_turnover_floor():
    frames, idx = _flat_frames(vol=1_000.0)  # $10k/day, below floor
    d = idx[60]
    frames["AAA"].loc[d:, ["open", "high", "low", "close"]] = 11.0
    frames["AAA"].loc[d, "volume"] = 50_000.0
    assert detect_events(_panels(frames, idx), 5.0, 3.0, 20).empty


def test_gap_is_relative_to_index():
    frames, idx = _flat_frames()
    d = idx[60]
    frames["AAA"].loc[d:, ["open", "high", "low", "close"]] = 11.0
    frames["AAA"].loc[d, "volume"] = 500_000.0
    index_close = pd.Series(7000.0, index=idx)
    index_open = pd.Series(7000.0, index=idx)
    index_open.loc[d] = 7000 * 1.08  # index also gapped 8% -> relative gap 2%
    p = build_panels(frames, index_open, index_close, 250_000, 20)
    assert detect_events(p, 5.0, 3.0, 20).empty


def test_simulate_planted_trade_pnl_and_exits():
    frames, idx = _flat_frames()
    d = idx[60]
    f = frames["AAA"]
    f.loc[d:, ["open", "high", "low", "close"]] = 11.0
    f.loc[d, "volume"] = 500_000.0
    # drift up 1% a session after the event for 10 sessions
    for k in range(1, 15):
        f.loc[idx[60 + k], ["open", "high", "low", "close"]] = 11.0 * (1.01**k)
    p = _panels(frames, idx)
    ev = detect_events(p, 5.0, 3.0, 20)
    costs = CostModel()
    r = simulate(p, ev, costs, 10_000, 4, hold_days=10, stop_pct=8.0, entry_lag=1)
    assert len(r.trades) == 1
    t = r.trades.iloc[0]
    assert t["entry_date"] == idx[61]  # next open after confirmation
    assert t["exit_date"] == idx[71] and t["reason"] == "time"
    assert t["hold_sessions"] == 10
    assert t["qty"] * t["entry_px"] <= 2500
    assert t["pnl"] > 0
    assert r.equity.iloc[-1] == pytest.approx(10_000 + t["pnl"])
    # exposure was ~25% while in the trade and 0 otherwise
    assert r.exposure.loc[idx[61]] == pytest.approx(0.25, abs=0.03)
    assert r.exposure.loc[idx[80]] == 0.0


def test_simulate_stop_loss_exits_next_open():
    frames, idx = _flat_frames()
    d = idx[60]
    f = frames["AAA"]
    f.loc[d:, ["open", "high", "low", "close"]] = 11.0
    f.loc[d, "volume"] = 500_000.0
    f.loc[idx[63] :, ["open", "high", "low", "close"]] = 9.5  # -13.6% on session 63's close
    p = _panels(frames, idx)
    ev = detect_events(p, 5.0, 3.0, 20)
    r = simulate(p, ev, CostModel(), 10_000, 4, hold_days=10, stop_pct=8.0)
    t = r.trades.iloc[0]
    assert t["reason"] == "stop" and t["exit_date"] == idx[64]
    assert t["pnl"] < 0


def test_simulate_position_cap_and_priority():
    frames, idx = _flat_frames(tickers=("AAA", "BBB", "CCC"))
    d = idx[60]
    for t, vm in (("AAA", 400_000.0), ("BBB", 800_000.0), ("CCC", 600_000.0)):
        frames[t].loc[d:, ["open", "high", "low", "close"]] = 11.0
        frames[t].loc[d, "volume"] = vm
    p = _panels(frames, idx)
    ev = detect_events(p, 5.0, 3.0, 20)
    assert len(ev) == 3
    r = simulate(p, ev, CostModel(), 10_000, max_positions=2, hold_days=5, stop_pct=8.0)
    assert sorted(r.trades["ticker"]) == ["BBB", "CCC"]  # highest volume multiples win


def test_reaction_sessions_pre_and_post_open():
    sessions = pd.bdate_range("2024-01-01", periods=10)
    rel = pd.Series(
        pd.to_datetime(["2024-01-02 08:30", "2024-01-02 10:00", "2024-01-06 12:00"])
    )  # Tue pre-open, Tue at open, Saturday
    out = reaction_sessions(rel, sessions)
    assert out.iloc[0] == pd.Timestamp("2024-01-02")
    assert out.iloc[1] == pd.Timestamp("2024-01-03")
    assert out.iloc[2] == pd.Timestamp("2024-01-08")


def test_own_announcements_drops_foreign_rows():
    ann = pd.DataFrame(
        {
            "code": ["BHP", "BHP", "BHP"],
            "headline": [
                "Quarterly report",
                "WPL: CNOOC agreement",
                "ESS's ann: contract with BHP",
            ],
        }
    )
    assert own_announcements(ann)["headline"].tolist() == ["Quarterly report"]


def test_announcement_events_join():
    frames, idx = _flat_frames()
    d = idx[60]
    frames["AAA"].loc[d:, ["open", "high", "low", "close"]] = 11.0
    frames["AAA"].loc[d, "volume"] = 500_000.0
    p = _panels(frames, idx)
    ev_b = detect_events(p, 5.0, 3.0, 20)
    ann = pd.DataFrame(
        {
            "code": ["AAA", "AAA", "BBB"],
            "released_at": [
                d - pd.Timedelta(days=1) + pd.Timedelta(hours=17),
                d + pd.Timedelta(hours=8),
                d,
            ],
            "headline": ["Big contract", "Routine", "Other"],
            "type": ["contract", "other", "other"],
            "price_sensitive": [True, False, True],
            "ids_id": ["1", "2", "3"],
            "pre_open": [False, True, True],
        }
    )
    ev_a = announcement_events(ev_b, ann, p.dates)
    assert len(ev_a) == 1 and ev_a.loc[0, "headline"] == "Big contract"
    # without a sensitive announcement on the reaction session there is no A event
    ann2 = ann.copy()
    ann2.loc[0, "price_sensitive"] = False
    assert announcement_events(ev_b, ann2, p.dates).empty


def test_momentum_baseline_runs_and_respects_regime():
    n = 400
    idx = pd.bdate_range("2020-01-01", periods=n, name="date")
    frames = {}
    for k, t in enumerate(("AAA", "BBB", "CCC", "DDD", "EEE")):
        base = synthetic_bars(n, seed=k, start="2020-01-01")
        base.index = idx
        base["volume"] = 100_000.0
        frames[t] = base
    up = pd.Series(np.linspace(5000, 8000, n), index=idx)
    p = build_panels(frames, up, up, 250_000, 20)
    r = run_momentum(
        p, CostModel(), 10_000, top_n=4, lookback_months=12, skip_months=1, regime_sma_days=200
    )
    assert len(r.trades) > 0
    assert r.trades["reason"].isin(["rebalance", "end", "regime_off"]).all()
    # regime off everywhere -> no trades
    down = pd.Series(np.linspace(8000, 5000, n), index=idx)
    p2 = build_panels(frames, down, down, 250_000, 20)
    r2 = run_momentum(p2, CostModel(), 10_000, 4, 12, 1, 200)
    assert r2.trades.empty and r2.equity.iloc[-1] == 10_000


def test_metrics():
    eq = pd.Series(
        [100, 110, 99, 120.0],
        index=pd.to_datetime(["2020-01-01", "2020-07-01", "2020-10-01", "2021-01-01"]),
    )
    assert max_drawdown(eq) == pytest.approx(99 / 110 - 1)
    assert cagr(eq) == pytest.approx(0.2, abs=0.01)
    trades = pd.DataFrame(
        {"pnl": [10, -5], "ret_pct": [4, -2], "entry_value": [250, 250], "costs": [7, 7]}
    )
    s = summarise(eq, trades, pd.Series(0.5, index=eq.index))
    assert s["trades"] == 2 and s["win_rate_pct"] == 50 and s["small_sample"]
