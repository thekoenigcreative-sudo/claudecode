"""Item #18 (27 Sep 2026): the day trader's scan reads its bars as arrays - and finds exactly
what it found before.

A simulated day took 12.5 minutes, 97% of it in the scan re-slicing each stock's DataFrame
for every setup at every bar. The rules now read numpy arrays built once per scan (once per
stock and day in the replay). This file holds the new code to the OLD code, kept verbatim
in tests/ref_daytrader_7750e63.py: on made-up days built to hit every path (holes the feed
did not watch, halts, late starts, gaps both ways, NaN highs and lows, untraded minutes,
variants' settings, setups switched off) every setup, every "evaluated up to", every halt,
every move, RVOL and VWAP must be the same - not close, the same.

The 20-day replay before and after (scripts/replay_identity.py; TRACKER.md) is the proof on
real bars; this is the proof on the corners real days rarely reach.
"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta
from datetime import time as time_cls
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

import ref_daytrader_7750e63 as REF
from asxbot.arena import daytrader as DT
from asxbot.arena import intraday as I

SYD = ZoneInfo("Australia/Sydney")
DAY = date(2026, 7, 14)  # a Tuesday
PRIOR = [date(2026, 7, 13), date(2026, 7, 10), date(2026, 7, 9), date(2026, 7, 8),
         date(2026, 7, 7)]  # fmt: skip
END = time_cls(15, 45)


# --------------------------------------------------------------------------------------------
# made-up days
# --------------------------------------------------------------------------------------------
def _minutes(day: date, first=(9, 59), last=(16, 0), close_print=True) -> list[datetime]:
    t = datetime.combine(day, time_cls(*first), tzinfo=SYD)
    end = datetime.combine(day, time_cls(*last), tzinfo=SYD)
    out = []
    while t < end:
        out.append(t)
        t += timedelta(minutes=1)
    if close_print:
        out.append(datetime.combine(day, time_cls(16, 10), tzinfo=SYD))
    return out


def make_day(rng, day: date, *, last=(16, 0), holes=0, halt=False, late=False, nan=False,
             trend=0.0, gap=0.0) -> pd.DataFrame:  # fmt: skip
    """One stock's minute bars: a random walk (with a trend, a gap at the open, a halt with a
    jump after it), some untraded minutes (volume 0), some missing minutes."""
    idx = _minutes(day, (10, 40) if late else (9, 59), last)
    n = len(idx)
    steps = rng.normal(trend, 0.004, n)
    if halt:
        k = int(rng.integers(n // 4, n // 2))
        steps[k] += rng.choice([-1, 1]) * rng.uniform(0.03, 0.06)
    wave = 1 + 0.02 * np.sin(np.arange(n) * 2 * np.pi / rng.uniform(12, 30))  # pullbacks
    close = (1.0 + gap) * np.exp(np.cumsum(steps)) * wave
    close = np.round(close, 3)
    open_ = np.round(np.r_[close[0], close[:-1]], 3)
    high = np.round(np.maximum(open_, close) * (1 + rng.uniform(0, 0.004, n)), 3)
    low = np.round(np.minimum(open_, close) * (1 - rng.uniform(0, 0.004, n)), 3)
    vol = rng.integers(100, 5000, n).astype(float)
    vol[rng.random(n) < 0.1] = 0.0  # untraded minutes
    vol[rng.random(n) < 0.05] *= 12  # heavy bars
    vol *= 1 + 300 * np.abs(np.diff(np.log(wave), prepend=0.0))  # busier on the swings
    if nan:
        for arr in (high, low):
            arr[rng.choice(n, 3, replace=False)] = np.nan
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol},
                      index=pd.DatetimeIndex(idx))  # fmt: skip
    drop = set()
    for _ in range(holes):
        k = int(rng.integers(5, n - 20))
        drop |= set(range(k, k + int(rng.integers(1, 5))))
    if halt:
        k = int(np.argmax(np.abs(steps)))
        drop |= set(range(max(1, k - int(rng.integers(11, 20))), k))
    keep = [i for i in range(n) if i not in drop]
    return df.iloc[keep]


def make_index(rng, day: date) -> pd.DataFrame:
    idx = _minutes(day, (10, 0), (16, 0), close_print=True)
    close = np.round(8000 * np.exp(np.cumsum(rng.normal(0, 0.0005, len(idx)))), 1)
    return pd.DataFrame({"open": close, "high": close, "low": close, "close": close,
                         "volume": 0.0}, index=pd.DatetimeIndex(idx))  # fmt: skip


def usual_for(rng, kind: str):
    if kind == "none":
        return None
    base = np.cumsum(rng.uniform(50, 400, 360))
    if kind == "zeros":
        base[:40] = 0.0
    return pd.Series(base, index=range(360))


def covered_fn(unwatched: list[tuple[datetime, datetime]] | None):
    if unwatched is None:
        return None

    def covered(a, b):
        return not any(a <= e and s <= b for s, e in unwatched)

    return covered


def conf_variants(frozen: dict) -> list[dict]:
    """The frozen setups, and variants that fire far more often (and switch rules off)."""
    loose = copy.deepcopy(frozen)
    loose["gap_and_go"].update(gap_pct_vs_index=0.5, min_rvol=0.1)
    loose["opening_range_breakout"].update(bar_volume_multiple=1.1, min_rvol=0.1,
                                           range_minutes=15, window=["10:15", "15:30"])  # fmt: skip
    loose["vwap_reclaim"].update(
        min_move_vs_index_pct=0.2, vwap_slope_bars=5, pullback_lookback=8, min_bars_below=1,
        stop_lookback=1)  # fmt: skip
    loose["halt_resumption"].update(min_move_vs_index_pct=0.5, prior_activity_share=0.3)
    off = copy.deepcopy(loose)
    off["vwap_reclaim"]["enabled"] = False
    off["gap_and_go"]["enabled"] = False
    odd = copy.deepcopy(loose)
    odd["vwap_reclaim"].update(pullback_lookback=0, bar_volume_lookback=0, stop_lookback=0)
    odd["opening_range_breakout"].update(bar_volume_lookback=2, window=["10:30:30", "14:30"])
    return [frozen, loose, off, odd]


SEEN: set = set()  # (setup, side) the made-up days reached


def _key(setups) -> list[str]:
    SEEN.update((s.setup, s.side) for s in setups)
    return [json.dumps(s.to_dict(), sort_keys=True, default=str) for s in setups]


def _same(x, y) -> bool:
    return repr(x) == repr(y)  # NaN == NaN here, and 1.0 != 1


# --------------------------------------------------------------------------------------------
# the rules, bar by bar, as the scan calls them
# --------------------------------------------------------------------------------------------
def run_both(conf, bars_all, index_all, *, prev, iprev, usual, shortable, news, covered,
             rng) -> int:  # fmt: skip
    """Walk the day as the scan does (a few new bars at a time, `since` = the last bar
    evaluated), the new code and the old side by side. Returns the setups found."""
    fired_new, fired_old = set(), set()
    since = None
    n = int(rng.integers(3, 25))
    found = 0
    while True:
        bars = bars_all.iloc[:n]
        index = index_all[index_all.index <= bars.index[-1]]
        args = ("TST", DAY, bars, index, prev, iprev, usual, shortable, news, conf)
        new, old = DT.Ctx(*args, covered=covered), REF.Ctx(*args, covered=covered)
        t_new, t_old = DT.watched_through(new, since), REF.watched_through(old, since)
        assert _same(t_new, t_old), (n, t_new, t_old)
        if t_old is not None:
            s_new = DT.detect(new, since, fired_new, END, until=t_new)
            s_old = REF.detect(old, since, fired_old, END, until=t_old)
            assert _key(s_new) == _key(s_old), n
            assert fired_new == fired_old
            found += len(s_old)
            if since is None or t_old > since:
                since = t_old
        assert _same(DT.resumptions(new), REF.resumptions(old))
        ts = bars.index.max()
        assert _same(new.move_vs_index(ts), old.move_vs_index(ts))
        assert _same(new.rvol(ts), old.rvol(ts))
        np.testing.assert_array_equal(new.vwa(), old.vw.to_numpy())
        # every bar from scratch too (a first scan, since=None), now and then
        if rng.random() < 0.08:
            a, b = DT.Ctx(*args, covered=covered), REF.Ctx(*args, covered=covered)
            assert _key(DT.detect(a, None, set(), END)) == _key(REF.detect(b, None, set(), END))
        if n >= len(bars_all):
            return found
        n = min(len(bars_all), n + int(rng.integers(1, 12)))


def _case(rng, conf, *, last=(12, 0), usual="small", **kw):
    full = make_day(rng, DAY, last=last, **kw)
    bars = REF.continuous(full[full["volume"] > 0])
    if kw.get("nan"):  # the scan never sees a NaN volume (visible drops it); a Ctx may
        bars = bars.copy()
        bars.iloc[rng.choice(len(bars), 4, replace=False), bars.columns.get_loc("volume")] = np.nan
    index = make_index(rng, DAY)
    gap = kw.get("gap", 0.0)
    unwatched = None
    if rng.random() < 0.4:
        t = datetime.combine(DAY, time_cls(10, int(rng.integers(20, 50))), tzinfo=SYD)
        unwatched = [(t, t + timedelta(minutes=int(rng.integers(2, 6))))]
    return dict(
        conf=conf,
        bars_all=bars,
        index_all=REF.continuous(index),
        prev=float(full["open"].iloc[0]) / (1 + gap) if gap else float(rng.uniform(0.95, 1.05)),
        iprev=float(index["close"].iloc[0]) * float(rng.uniform(0.995, 1.005)),
        usual=usual_for(rng, usual),
        shortable=bool(rng.random() < 0.7),
        news=bool(rng.random() < 0.5),
        covered=covered_fn(unwatched),
        rng=rng,
    )


@pytest.fixture
def frozen(base_config) -> dict:
    return base_config["arena"]["playbooks"]["asx_daytrader"]["setups"]


def test_every_setup_is_the_one_the_old_code_found(frozen):
    total = 0
    for seed, kw in [(10 * r + i, kw) for r in range(3) for i, kw in enumerate([
        dict(trend=0.0002, gap=0.05),
        dict(trend=-0.0002, gap=-0.05, holes=2),
        dict(trend=0.0001, halt=True, gap=0.01),
        dict(trend=-0.0001, halt=True, holes=1),
        dict(late=True, gap=0.04),
        dict(nan=True, trend=0.0001, gap=0.03),
        dict(holes=3, trend=-0.0001, gap=-0.01),
        dict(trend=0.0),
    ])]:  # fmt: skip
        for k, conf in enumerate(conf_variants(frozen)):
            usual = "none" if seed % 10 == 7 else "zeros" if k == 3 else "small"
            total += run_both(**_case(np.random.default_rng(10 * seed + k), conf, usual=usual,
                                      last=(11, 45), **kw))  # fmt: skip
    # the made-up days reach every rule, both ways - not only the "nothing" paths
    assert total > 25
    assert {(s, d) for s in DT.SETUPS for d in ("buy", "short")} <= SEEN


def test_a_whole_day_to_the_close_and_past_the_scan_s_end(frozen):
    rng = np.random.default_rng(99)
    loose = conf_variants(frozen)[1]
    assert run_both(**_case(rng, loose, last=(16, 0), trend=0.0005, gap=0.04, halt=True)) > 0


def test_the_old_first_breaks_still_answers_as_before(frozen):
    rng = np.random.default_rng(5)
    bars = REF.continuous(make_day(rng, DAY, last=(12, 0), trend=0.001))
    c = DT.Ctx("TST", DAY, bars, bars, 1.0, 1.0, None, True, False, frozen)
    hi, lo = c.opening_range(30)[1:]
    up, down = DT._first_breaks(bars, ["10:30", "14:30"], hi, lo)
    assert (up, down) == REF._first_breaks(bars, ["10:30", "14:30"], hi, lo)
    k_up, k_down = c.first_breaks(["10:30", "14:30"], hi, lo)
    assert (None if k_up is None else bars.index[k_up]) == up
    assert (None if k_down is None else bars.index[k_down]) == down


# --------------------------------------------------------------------------------------------
# the data layer: what the replay feed shows, times of day, usual volume
# --------------------------------------------------------------------------------------------
class Minutes:
    def __init__(self, frames: dict):
        self.frames = frames

    def cached(self, code, day):
        return self.frames.get((code, day))


@pytest.mark.parametrize("delay", [None, 20])
def test_the_replay_feed_shows_the_rows_visible_and_continuous_showed(delay):
    rng = np.random.default_rng(7)
    stock = make_day(rng, DAY, holes=4)
    shuffled = stock.iloc[rng.permutation(len(stock))]  # unsorted: the old path, unchanged
    mins = Minutes({("TST", DAY): stock, ("^AXJO", DAY): make_index(rng, DAY),
                    ("MIX", DAY): shuffled, ("NIL", DAY): stock.iloc[:0]})  # fmt: skip
    feed = I.ReplayFeed(mins, delay)
    for code in ("TST", "^AXJO", "MIX", "NIL", "NONE"):
        for hh, mm, ss in [(9, 0, 0), (10, 0, 20), (10, 1, 20), (11, 17, 20), (12, 0, 0),
                           (15, 59, 20), (16, 11, 20), (19, 0, 0)]:  # fmt: skip
            now = datetime.combine(DAY, time_cls(hh, mm, ss), tzinfo=SYD)
            old = REF.visible(mins.cached(code, DAY), now, delay, day_complete=delay is None,
                              index=code.startswith("^"))  # fmt: skip
            got = feed.bars(code, DAY, now)
            pd.testing.assert_frame_equal(got, old)
            cont, arrays = feed.continuous_bars(code, DAY, now)
            pd.testing.assert_frame_equal(cont, REF.continuous(old))
            if arrays is not None:
                fresh = I.BarArrays(cont)
                for f in ("ns", "tod", "o", "h", "l", "c", "v"):
                    np.testing.assert_array_equal(getattr(arrays, f), getattr(fresh, f))
                assert arrays.src is cont


def test_times_of_day_are_the_times_pandas_gives():
    rng = np.random.default_rng(3)
    # both Sydney clock changes of 2026, sub-microsecond parts, a naive index, UTC
    base = [datetime(2026, 4, 5, 1, 30, tzinfo=SYD), datetime(2026, 10, 4, 1, 30, tzinfo=SYD),
            datetime(2026, 7, 14, 9, 59, tzinfo=SYD)]  # fmt: skip
    for start in base:
        idx = pd.DatetimeIndex([pd.Timestamp(start) + pd.Timedelta(minutes=int(m), seconds=int(s),
                                                                   nanoseconds=int(ns))
                                for m, s, ns in zip(rng.integers(0, 900, 300),
                                                    rng.integers(0, 60, 300),
                                                    rng.integers(0, 1000, 300),
                                                    strict=True)])  # fmt: skip
        for ix in (idx, idx.tz_convert("UTC"), idx.tz_localize(None), idx.as_unit("us")):
            want = [t.hour * 3_600_000_000 + t.minute * 60_000_000 + t.second * 1_000_000
                    + t.microsecond for t in ix.time]  # fmt: skip
            assert I.tod_us(ix).tolist() == want
            df = pd.DataFrame({"close": 1.0, "volume": 1.0}, index=ix)
            pd.testing.assert_frame_equal(I.continuous(df), REF.continuous(df))


def test_moments_compare_as_pandas_compares_them():
    idx = pd.DatetimeIndex([datetime(2026, 7, 14, 10, m, tzinfo=SYD) for m in range(30)])
    ns = I.BarArrays(pd.DataFrame({"close": 1.0}, index=idx)).ns
    for t in (datetime(2026, 7, 14, 10, 7, 30, tzinfo=SYD), pd.Timestamp("2026-07-14 00:05Z"),
              datetime(2026, 7, 14, 10, 7, 30, 1, tzinfo=ZoneInfo("UTC")),
              pd.Timestamp("2026-07-14 10:12:00.000000001+10:00")):  # fmt: skip
        assert (ns <= I.moment_ns(t, True)).tolist() == list(idx <= t)
    with pytest.raises(TypeError):
        I.moment_ns(datetime(2026, 7, 14, 10, 7), True)


def test_usual_volume_is_the_same_curve():
    rng = np.random.default_rng(11)
    frames = {}
    for i, d in enumerate(PRIOR):
        df = make_day(rng, d, holes=2)
        if i == 1:
            df.iloc[5, df.columns.get_loc("volume")] = np.nan  # a missing volume
        if i == 2:
            df = pd.concat([df, df.iloc[40:45]]).sort_index()  # repeated minutes
        frames[("TST", d)] = df
    mins = Minutes(frames)
    new, old = I.usual_cum_volume(mins, "TST", DAY), REF.usual_cum_volume(mins, "TST", DAY)
    pd.testing.assert_series_equal(new, old, check_exact=True)


# --------------------------------------------------------------------------------------------
# the whole scan: the replay's prepared arrays against arrays built from the frames (live)
# --------------------------------------------------------------------------------------------
class PlainView:
    """The same view without `continuous_bars`: the scan builds arrays from each frame, as
    it does on the live feeds."""

    def __init__(self, view):
        self._v = view

    def __getattr__(self, name):
        if name == "continuous_bars":
            raise AttributeError(name)
        return getattr(self._v, name)


def test_the_scan_finds_the_same_with_prepared_arrays_and_without(frozen):
    rng = np.random.default_rng(21)
    frames = {("^AXJO", d): make_index(rng, d) for d in [DAY, *PRIOR]}
    codes = []
    for k in range(12):
        code = f"S{k:02d}"
        codes.append(code)
        for d in PRIOR:
            frames[(code, d)] = make_day(rng, d)
        frames[(code, DAY)] = make_day(rng, DAY, trend=float(rng.normal(0, 0.0005)),
                                       gap=float(rng.choice([0, 0.05, -0.05])),
                                       halt=bool(k % 4 == 0), holes=k % 3)  # fmt: skip
    mins = Minutes(frames)
    pb = SimpleNamespace(raw={"setups": conf_variants(frozen)[1], "scan": {"end": "15:45"},
                              "max_signal_age_bars": 5})  # fmt: skip
    views = [I.MarketView(mins, DAY, I.ReplayFeed(mins, None), "^AXJO", 5, 3)]
    views.append(PlainView(I.MarketView(mins, DAY, I.ReplayFeed(mins, None), "^AXJO", 5, 3)))
    states = [{"fired": [], "last_eval": {}}, {"fired": [], "last_eval": {}}]
    found = 0
    t = datetime.combine(DAY, time_cls(10, 0, 20), tzinfo=SYD)
    while t.time() < time_cls(16, 0):
        a = DT.scan(views[0], pb, codes, t, states[0], {"S01", "S02"}, {"S04"})
        b = DT.scan(views[1], pb, codes, t, states[1], {"S01", "S02"}, {"S04"})
        assert _key(a[0]) == _key(b[0]) and _same(a[1], b[1]) and states[0] == states[1]
        found += len(a[0])
        t += timedelta(minutes=int(rng.integers(1, 4)))
    assert found > 5


def test_the_reductions_are_pandas_own():
    """Sum, mean, max, min and the running sum, NaN and all, against pandas itself."""
    rng = np.random.default_rng(17)
    arrays = [np.array([]), np.array([np.nan]), np.array([np.nan, np.nan]), np.array([3.0])]
    for n in (2, 5, 9, 17, 130, 400):
        a = rng.integers(0, 10**6, n).astype(float)
        arrays.append(a.copy())
        a[rng.random(n) < 0.2] = np.nan
        arrays.append(a)
        arrays.append(rng.normal(0, 1e3, n))  # not whole numbers: the summing order shows
    for a in arrays:
        s = pd.Series(a)
        assert _same(DT._nsum(a), float(s.sum()))
        assert _same(DT._nmean(a), float(s.mean()))
        assert _same(DT._nmax(a), float(s.max()))
        assert _same(DT._nmin(a), float(s.min()))
        np.testing.assert_array_equal(DT._cumsum(a), s.cumsum().to_numpy())
        for lo, hi in ((0, len(a) // 2), (len(a) // 3, len(a))):  # slices, as the rules take
            assert _same(DT._nmean(a[lo:hi]), float(s.iloc[lo:hi].mean()))
            assert _same(DT._nsum(a[lo:hi]), float(s.iloc[lo:hi].sum()))
