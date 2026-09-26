"""The day trader (config.yaml `asx_daytrader`, version 1, frozen 2026-09-24 before it ran).

Rick, 24 Sep: "why can't it be doing the work of a day trader". So instead of waiting for
announcements, code scans the liquid market every cycle, finds four setups by exact rule,
and hands each one to the agent to confirm or reject with the context a trader checks. The
rule bot takes every setup mechanically. Code sizes every trade (risk <= 0.5% of the
account), enforces the stop, manages the trade (breakeven at +1R, half off and a trail at
+2R - in the broker, bar by bar) and is flat by the 15:50 sweep.

Everything here is plain code except the one agent call per setup. The setups are pure
functions of the bars a decision may see (intraday.visible), so the live scanner and the
plumbing replay run the same code.

DATA HONESTY: on Yahoo's delayed bars a setup is seen ~20 minutes after its trigger bar and
filled at the first bar after the decision. Every report says "delayed data - rehearsal
until IBKR live prices". On IBKR's live bars (from 25 Sep) the decider's packet says so, with
the age of the newest bar it shows (`data_line`).
"""

from __future__ import annotations

import json
import math
import time as time_mod
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as time_cls
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from asxbot.arena import notify
from asxbot.arena.agents import DECIDER, AgentCallFailed, call_agent
from asxbot.arena.broker import OPENING_SIDES
from asxbot.arena.intraday import (
    CONTINUOUS_END,
    VOLUME_FROM,
    BarArrays,
    MarketView,
    continuous,
    entries_allowed,
    minute_of_session,
    time_us,
    vwap,
)
from asxbot.arena.intraday import moment_ns as _ns
from asxbot.arena.levels import Playbook
from asxbot.arena.liquid import SizeRule, liquid_universe, size_rule
from asxbot.arena.orders import ArenaOrderRefused, arena_place_order
from asxbot.io import write_text_atomic
from asxbot.live.scanner import round_to_tick
from asxbot.log import EventLog, get_logger

log = get_logger("asxbot.arena.daytrader")
SYD = ZoneInfo("Australia/Sydney")
OPEN = time_cls(10, 0)
ONE_MIN = timedelta(minutes=1)
SETUPS = ("gap_and_go", "opening_range_breakout", "vwap_reclaim", "halt_resumption")


def _px(x) -> str:
    """A price as the agent and Rick read it: every decimal it has up to four, at least two.
    26 Sep 2026: prices were printed `:.4g`, which drops a half-cent above $100 (a close of
    108.85 read "closed back above it at 108.8 (VWAP 108.8)") and rounds 12.345 to 12.35."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    if not math.isfinite(v):
        return str(v)
    head, _, tail = f"{v:.4f}".rstrip("0").partition(".")
    return f"{head}.{tail.ljust(2, '0')}"


@dataclass
class Setup:
    ticker: str
    setup: str
    side: str  # buy | short
    trigger_bar: str  # ISO minute of the bar that triggered it
    last: float  # that bar's close
    stop: float  # the setup's invalidation level
    rvol: float | None
    why: str
    context: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.ticker}|{self.setup}|{self.side}"

    def to_dict(self) -> dict:
        return asdict(self)


def _t(s: str) -> time_cls:
    return time_cls.fromisoformat(str(s))


def _in(ts, window) -> bool:
    return _t(window[0]) <= ts.time() <= _t(window[1])


def _close_at(df: pd.DataFrame, ts) -> float | None:
    part = df[df.index <= ts]
    return None if not len(part) else float(part["close"].iloc[-1])


# --------------------------------------------------------------------------
# the bars as numbers (27 Sep 2026, item #18). A simulated day took 12.5 minutes, 97% of it
# here: every setup, at every bar, for every stock, every minute, re-sliced the day's
# DataFrame (a boolean mask, a new frame, a Timestamp per bar), so the work grew with the
# square of the day. Now each stock's bars are arrays (intraday.BarArrays: built once per
# stock and day in the replay, once per scan live) and the rules read them.
# THE RULES ARE UNCHANGED: every figure is the one pandas computed - the same rows, in the
# same order, reduced the way pandas reduces them (a sum or mean skips NaN, a max or min of
# nothing is NaN, a cumulative sum skips NaN) - proven by replaying 20 days before and after
# and diffing every setup, order and fill (scripts/replay_identity.py, TRACKER.md), and by
# tests/test_scan_speed.py against the old code, kept there as the reference.
# --------------------------------------------------------------------------
US_PER_MIN = 60_000_000


def _nsum(a: np.ndarray) -> float:
    """pandas' sum: NaN skipped, 0.0 for nothing."""
    m = np.isnan(a)
    return float(np.where(m, 0.0, a).sum() if m.any() else a.sum())


def _nmean(a: np.ndarray) -> float:
    """pandas' mean: NaN skipped, NaN for nothing."""
    m = np.isnan(a)
    n = len(a) - int(m.sum())
    if n <= 0:
        return math.nan
    return float((np.where(m, 0.0, a).sum() if m.any() else a.sum()) / np.float64(n))


def _nmax(a: np.ndarray) -> float:
    """pandas' max: NaN skipped, NaN for nothing."""
    a = a[~np.isnan(a)]
    return float(a.max()) if len(a) else math.nan


def _nmin(a: np.ndarray) -> float:
    """pandas' min: NaN skipped, NaN for nothing."""
    a = a[~np.isnan(a)]
    return float(a.min()) if len(a) else math.nan


def _cumsum(a: np.ndarray) -> np.ndarray:
    """pandas' cumsum: NaN skipped (and left NaN in place)."""
    m = np.isnan(a)
    if not m.any():
        return np.cumsum(a)
    out = np.cumsum(np.where(m, 0.0, a))
    out[m] = np.nan
    return out


def _vwap_arr(a: BarArrays) -> np.ndarray:
    """intraday.vwap, as numbers: typical price (h+l+c)/3 weighted by volume, running."""
    tp = (a.h + a.l + a.c) / 3.0
    pv = _cumsum(tp * a.v)
    v = _cumsum(a.v)
    with np.errstate(invalid="ignore", divide="ignore"):
        return pv / np.where(v > 0, v, np.nan)


_VOL_FROM_US, _CONT_END_US = time_us(VOLUME_FROM), time_us(CONTINUOUS_END)


# --------------------------------------------------------------------------
# the four setups, exactly as config.yaml writes them. Each looks at bar i of `bars` (the
# day's continuous bars a decision may see) and returns a Setup if bar i triggers it.
# --------------------------------------------------------------------------
@dataclass
class Ctx:
    """What every setup needs about one stock on one day."""

    ticker: str
    day: date
    bars: pd.DataFrame  # continuous bars, visible
    index: pd.DataFrame  # the index's continuous bars, visible
    prev_close: float
    index_prev_close: float
    usual: pd.Series | None
    shortable: bool
    news: bool  # price-sensitive or reinstatement announcement today
    conf: dict  # config setups block
    # Did the feed watch this stock from `start` to `end` (MarketView.covered)? None: it
    # always did (a feed that returns whole days, the replay, the tests). 26 Sep 2026.
    covered: Callable[[datetime, datetime], bool] | None = None
    _vwap: pd.Series | None = None
    _resumed: list | None = None
    # The bars and the index as arrays (item #18): built on first use and again if `bars` or
    # `index` is replaced; the scan builds the index's once and hands it to every stock.
    _cols: BarArrays | None = None
    _icols: BarArrays | None = None
    _usual: tuple | None = None

    def watched(self, start, end) -> bool:
        """True when a stretch with no bars from `start` to `end` (minutes, inclusive) means
        the stock did not trade, not that the feed was not looking."""
        return True if self.covered is None else bool(self.covered(start, end))

    @property
    def a(self) -> BarArrays:
        if self._cols is None or self._cols.src is not self.bars:
            self._cols = BarArrays(self.bars)
        return self._cols

    @property
    def ia(self) -> BarArrays:
        if self._icols is None or self._icols.src is not self.index:
            self._icols = BarArrays(self.index)
        return self._icols

    @property
    def vw(self) -> pd.Series:
        if self._vwap is None:
            self._vwap = vwap(self.bars)
        return self._vwap

    def vwa(self) -> np.ndarray:
        """`vw`'s values, as an array."""
        a = self.a
        if "vwap" not in a.memo:
            a.memo["vwap"] = _vwap_arr(a)
        return a.memo["vwap"]

    def _usual_arr(self) -> np.ndarray | None:
        u = self.usual
        if u is None:
            return None
        if self._usual is None or self._usual[0] is not u:
            self._usual = (u, u.to_numpy(dtype=np.float64))
        return self._usual[1]

    def _rvol(self, m: int, t_ns: int) -> float | None:
        """intraday.rvol_at: the volume from 10:01 through the bar at `t_ns` (minute `m` of
        the session) against the usual volume by that minute."""
        u = self._usual_arr()
        if u is None or m < 0 or m >= len(u):
            return None
        base = float(u[m])
        if base <= 0:
            return None
        a = self.a
        counted = (a.tod >= _VOL_FROM_US) & (a.tod < _CONT_END_US) & (a.ns <= t_ns)
        return _nsum(a.v[counted]) / base

    def rvol(self, ts) -> float | None:
        if self.usual is None:
            return None
        return self._rvol(minute_of_session(ts), _ns(ts, self.a.aware))

    def rvol_i(self, i: int) -> float | None:
        """`rvol` at bar i."""
        a = self.a
        return self._rvol(int(a.tod[i] // US_PER_MIN) - 600, int(a.ns[i]))

    def _move(self, t_ns: int) -> float | None:
        k, j = self.a.last_at(t_ns), self.ia.last_at(t_ns)
        if k is None or j is None:
            return None
        px, ix = float(self.a.c[k]), float(self.ia.c[j])
        return ((px / self.prev_close - 1) - (ix / self.index_prev_close - 1)) * 100

    def move_vs_index(self, ts) -> float | None:
        return self._move(_ns(ts, self.a.aware))

    def in_window(self, i: int, window) -> bool:
        """`_in(bar i's time, window)`."""
        return time_us(_t(window[0])) <= int(self.a.tod[i]) <= time_us(_t(window[1]))

    def opening_range(self, minutes: int) -> tuple[float, float, float] | None:
        """The bars from 10:00 for `minutes`: (the first one's open, their high, their low);
        None when there is none."""
        a = self.a
        key = ("range", minutes, self.day)
        if key not in a.memo:
            day_open = datetime.combine(self.day, OPEN, tzinfo=SYD)
            start = _ns(day_open, a.aware)
            end = _ns(day_open + timedelta(minutes=minutes), a.aware)
            inside = (a.ns >= start) & (a.ns < end)
            a.memo[key] = (
                (float(a.o[inside][0]), _nmax(a.h[inside]), _nmin(a.l[inside]))
                if inside.any()
                else None
            )
        return a.memo[key]

    def first_breaks(self, window, hi: float, lo: float) -> tuple[int | None, int | None]:
        """`_first_breaks`, as bar positions."""
        a = self.a
        key = ("breaks", str(window[0]), str(window[1]), hi, lo)
        if key not in a.memo:
            inside = (a.tod >= time_us(_t(window[0]))) & (a.tod <= time_us(_t(window[1])))
            up = np.flatnonzero(inside & (a.c > hi))
            down = np.flatnonzero(inside & (a.c < lo))
            a.memo[key] = (int(up[0]) if len(up) else None, int(down[0]) if len(down) else None)
        return a.memo[key]

    def is_bar(self, k: int | None, i: int) -> bool:
        """`first == ts`: is the bar at position k (a first break; None: none) bar i's time?"""
        return k is not None and bool(self.a.ns[k] == self.a.ns[i])


def gap_and_go(c: Ctx, i: int) -> Setup | None:
    p = c.conf["gap_and_go"]
    if not c.in_window(i, p["window"]):
        return None
    opening = c.opening_range(int(p["range_minutes"]))
    if opening is None or not len(c.index):
        return None
    ia = c.ia
    key = ("open_row", c.day)  # the index's first bar from 10:00
    if key not in ia.memo:
        k = np.flatnonzero(ia.ns >= _ns(datetime.combine(c.day, OPEN, tzinfo=SYD), ia.aware))
        ia.memo[key] = int(k[0]) if len(k) else None
    iopen = ia.memo[key]
    if iopen is None:
        return None
    first_open, hi, lo = opening
    gap = ((first_open / c.prev_close - 1) - (float(ia.o[iopen]) / c.index_prev_close - 1)) * 100
    need = float(p["gap_pct_vs_index"])
    a = c.a
    close = float(a.c[i])
    # The rule names THE FIRST bar in the window that closes beyond the range. Only that
    # bar can be the trigger; its RVOL is then tested. Until 2026-09-25 any later bar that
    # happened to have the volume fired, hours after the range had gone (see
    # opening_range_breakout, the same fault, found in the agent's day-1 rejections).
    first_up, first_down = c.first_breaks(p["window"], hi, lo)
    ctx = {"range_high": hi, "range_low": lo, "range_minutes": int(p["range_minutes"])}
    if gap >= need and c.is_bar(first_up, i) and _nmin(a.l[: i + 1]) >= c.prev_close:
        rv = c.rvol_i(i)
        if rv is None or rv < float(p["min_rvol"]):
            return None  # the first close above the range had no volume: no setup today
        ts = c.bars.index[i]
        return Setup(
            c.ticker,
            "gap_and_go",
            "buy",
            ts.isoformat(timespec="minutes"),
            close,
            lo,
            rv,
            f"gap {gap:+.1f}% vs index, first close ({_px(close)}) above the "
            f"{p['range_minutes']}-min high {_px(hi)}, gap unfilled, RVOL {rv:.1f}",
            {**ctx, "first_break": ts.strftime("%H:%M")},
        )
    if (
        gap <= -need
        and c.shortable
        and c.is_bar(first_down, i)
        and _nmax(a.h[: i + 1]) <= c.prev_close
    ):
        rv = c.rvol_i(i)
        if rv is None or rv < float(p["min_rvol"]):
            return None
        ts = c.bars.index[i]
        return Setup(
            c.ticker,
            "gap_and_go",
            "short",
            ts.isoformat(timespec="minutes"),
            close,
            hi,
            rv,
            f"gap {gap:+.1f}% vs index, first close ({_px(close)}) below the "
            f"{p['range_minutes']}-min low {_px(lo)}, gap unfilled, RVOL {rv:.1f}",
            {**ctx, "first_break": ts.strftime("%H:%M")},
        )
    return None


def _first_breaks(bars: pd.DataFrame, window, hi: float, lo: float):
    """The first bar in the window that closed above `hi`, and the first that closed below
    `lo` (each None if none has). Stable: earlier bars never change."""
    t = bars.index.time
    lo_t, hi_t = _t(window[0]), _t(window[1])
    inside = bars[(t >= lo_t) & (t <= hi_t)]
    up = inside[inside["close"] > hi]
    down = inside[inside["close"] < lo]
    return (up.index[0] if len(up) else None), (down.index[0] if len(down) else None)


def opening_range_breakout(c: Ctx, i: int) -> Setup | None:
    p = c.conf["opening_range_breakout"]
    if not c.in_window(i, p["window"]):
        return None
    rng = c.opening_range(int(p["range_minutes"]))
    if rng is None:
        return None
    _, hi, lo = rng
    # THE FIRST bar in the window that closes beyond the range is the only bar that can be
    # the breakout; the volume test is applied to it. Until 2026-09-25 the code fired on
    # the first bar that closed beyond the range AND had the volume, which let a range
    # broken quietly at 10:31 produce a "breakout" on a heavy bar hours later, with the
    # stop (the range's midpoint) a long way off: CWY, HDN, AUB, ORI, CHC and DRR on 25 Sep,
    # each rejected by the agent as "the scanner's 30-min low doesn't match the bars". The
    # range was right; the bar was not the break. A first break without the volume means
    # no opening-range setup on that side today (tests/test_day1_fixes.py).
    first_up, first_down = c.first_breaks(p["window"], hi, lo)
    a = c.a
    close, volume = float(a.c[i]), float(a.v[i])
    up = c.is_bar(first_up, i) and close > hi
    down = c.is_bar(first_down, i) and close < lo and c.shortable
    if not (up or down):
        return None
    look = int(p["bar_volume_lookback"])
    before = a.v[max(0, i - look) : i]
    if len(before) < max(3, look // 2):
        return None
    avg = _nmean(before)
    if avg <= 0 or volume < float(p["bar_volume_multiple"]) * avg:
        return None  # the break came without the volume: no setup on this side today
    rv = c.rvol_i(i)
    if rv is None or rv < float(p["min_rvol"]):
        return None
    mid = (hi + lo) / 2
    vol_x = volume / avg
    ts = c.bars.index[i]
    ctx = {"range_high": hi, "range_low": lo, "range_minutes": int(p["range_minutes"]),
           "first_break": ts.strftime("%H:%M")}  # fmt: skip
    if up and mid < close:
        return Setup(
            c.ticker,
            "opening_range_breakout",
            "buy",
            ts.isoformat(timespec="minutes"),
            close,
            mid,
            rv,
            f"first close ({_px(close)}) above the 30-min high {_px(hi)} on {vol_x:.1f}x "
            f"the prior {look} bars' volume, RVOL {rv:.1f}",
            ctx,
        )
    if down and mid > close:
        return Setup(
            c.ticker,
            "opening_range_breakout",
            "short",
            ts.isoformat(timespec="minutes"),
            close,
            mid,
            rv,
            f"first close ({_px(close)}) below the 30-min low {_px(lo)} on {vol_x:.1f}x "
            f"the prior {look} bars' volume, RVOL {rv:.1f}",
            ctx,
        )
    return None


def vwap_reclaim(c: Ctx, i: int) -> Setup | None:
    p = c.conf["vwap_reclaim"]
    if not c.in_window(i, p["window"]):
        return None
    slope, lookback = int(p["vwap_slope_bars"]), int(p["pullback_lookback"])
    if i - 1 - slope < 0 or i < lookback:
        return None
    a = c.a
    vw, closes = c.vwa(), a.c
    move = c._move(int(a.ns[i - 1]))
    if move is None:
        return None
    below = int(np.count_nonzero(closes[i - lookback : i] < vw[i - lookback : i]))
    above = int(np.count_nonzero(closes[i - lookback : i] > vw[i - lookback : i]))
    vol_before = a.v[max(0, i - int(p["bar_volume_lookback"])) : i]
    if not len(vol_before) or float(a.v[i]) < _nmean(vol_before):
        return None
    n = int(p["stop_lookback"])
    recent = slice(max(0, i - n + 1), i + 1)
    need_move, need_bars = float(p["min_move_vs_index_pct"]), int(p["min_bars_below"])
    close = float(closes[i])
    if (
        move >= need_move
        and vw[i - 1] > vw[i - 1 - slope]
        and below >= need_bars
        and closes[i - 1] < vw[i - 1]
        and closes[i] > vw[i]
    ):
        return Setup(
            c.ticker,
            "vwap_reclaim",
            "buy",
            c.bars.index[i].isoformat(timespec="minutes"),
            close,
            _nmin(a.l[recent]),
            c.rvol_i(i),
            f"up {move:+.1f}% vs index, VWAP rising, {below} of the last {lookback} "
            f"bars below VWAP, closed back above it at {_px(close)} "
            f"(VWAP {_px(vw[i])})",
        )
    if (
        c.shortable
        and move <= -need_move
        and vw[i - 1] < vw[i - 1 - slope]
        and above >= need_bars
        and closes[i - 1] > vw[i - 1]
        and closes[i] < vw[i]
    ):
        return Setup(
            c.ticker,
            "vwap_reclaim",
            "short",
            c.bars.index[i].isoformat(timespec="minutes"),
            close,
            _nmax(a.h[recent]),
            c.rvol_i(i),
            f"down {move:+.1f}% vs index, VWAP falling, {above} of the last {lookback} "
            f"bars above VWAP, closed back below it at {_px(close)} "
            f"(VWAP {_px(vw[i])})",
        )
    return None


def resumptions(c: Ctx) -> list[tuple]:
    """Halts found in the day's bars: (resumed_at, price before, time before). Worked out
    once per stock per scan."""
    if c._resumed is not None:
        return c._resumed
    p = c.conf["halt_resumption"]
    idx = c.bars.index
    out = []
    gap = timedelta(minutes=int(p["min_gap_minutes"]) + 1)  # 10 untraded minutes between
    lo_t, hi_t = time_cls(10, 5), time_cls(15, 30)
    if len(idx) > 1:
        ns, tod = c.a.ns, c.a.tod
        lo_us, hi_us = time_us(lo_t), time_us(hi_t)
        gap_ns, half_hour = gap // timedelta(microseconds=1) * 1000, 30 * 60 * 10**9
        for k in np.flatnonzero(np.diff(ns) >= gap_ns):
            if not (lo_us <= tod[k] < hi_us):
                continue
            n_prior = np.searchsorted(ns, ns[k], side="right") - np.searchsorted(
                ns, ns[k] - half_hour, side="right"
            )
            if n_prior < float(p["prior_activity_share"]) * 30:
                continue
            a, b = idx[k], idx[k + 1]
            # 26 Sep 2026 (review B1): a gap is a halt only if the feed watched those
            # minutes. A hole in the IBKR feed's bars (a stretch it was not streaming or
            # fetching the stock) read as a halt, and a bar after it fired a "resumption"
            # (reproduced: resumed 11:45 after 11:29, SETUP halt_resumption buy 11:55). The
            # halt rule is unchanged; a hole is simply not a halt.
            if not c.watched(a + ONE_MIN, b - ONE_MIN):
                continue
            out.append((b, float(c.a.c[k]), a))
    day_open = datetime.combine(c.day, OPEN, tzinfo=SYD)
    if (
        len(idx)
        and c.news
        and idx[0].time() >= _t(p["late_first_trade"])
        and c.watched(day_open, idx[0] - ONE_MIN)  # a late start, not a late first look
    ):
        out.append((idx[0], c.prev_close, None))
    c._resumed = out
    return out


def halt_resumption(c: Ctx, i: int) -> Setup | None:
    p = c.conf["halt_resumption"]
    if not resumptions(c):  # no halt today: nothing to look at (item #18)
        return None
    ts = c.bars.index[i]
    rng_m, within = int(p["resumption_range_minutes"]), int(p["within_minutes"])
    for resumed, before_px, before_ts in resumptions(c):
        if not (resumed + timedelta(minutes=rng_m) <= ts <= resumed + timedelta(minutes=within)):
            continue
        rng = c.bars[
            (c.bars.index >= resumed) & (c.bars.index < resumed + timedelta(minutes=rng_m))
        ]
        if not len(rng):
            continue
        end_px = float(rng["close"].iloc[-1])
        ix_end = _close_at(c.index, rng.index[-1])
        ix_before = c.index_prev_close if before_ts is None else _close_at(c.index, before_ts)
        if ix_end is None or not ix_before:
            continue
        move = ((end_px / before_px - 1) - (ix_end / ix_before - 1)) * 100
        need = float(p["min_move_vs_index_pct"])
        bar = c.bars.iloc[i]
        hi, lo = float(rng["high"].max()), float(rng["low"].min())
        # only the first bar past the range after it formed
        after = c.bars[(c.bars.index >= resumed + timedelta(minutes=rng_m)) & (c.bars.index < ts)]
        if move >= need and bar["close"] > hi and not (after["close"] > hi).any():
            return Setup(
                c.ticker,
                "halt_resumption",
                "buy",
                ts.isoformat(timespec="minutes"),
                float(bar["close"]),
                lo,
                c.rvol(ts),
                f"resumed {resumed:%H:%M} {move:+.1f}% vs index; closed {_px(bar['close'])} "
                f"above the resumption range high {_px(hi)}",
            )
        if move <= -need and c.shortable and bar["close"] < lo and not (after["close"] < lo).any():
            return Setup(
                c.ticker,
                "halt_resumption",
                "short",
                ts.isoformat(timespec="minutes"),
                float(bar["close"]),
                hi,
                c.rvol(ts),
                f"resumed {resumed:%H:%M} {move:+.1f}% vs index; closed {_px(bar['close'])} "
                f"below the resumption range low {_px(lo)}",
            )
    return None


DETECTORS = {
    "gap_and_go": gap_and_go,
    "opening_range_breakout": opening_range_breakout,
    "vwap_reclaim": vwap_reclaim,
    "halt_resumption": halt_resumption,
}


def detect(
    c: Ctx, since: datetime | None, fired: set[str], scan_end: time_cls, until=None
) -> list[Setup]:
    """Setups triggered by bars after `since` (and up to `until`, when given), first trigger
    of each (stock, setup, side) only. Bars after the scan's end time are not triggers."""
    out: list[Setup] = []
    a = c.a
    if not len(a.ns):
        return out
    # A setup whose block says `enabled: false` is not looked for (Practice Lab variants,
    # 26 Sep 2026; the frozen blocks don't set it, so nothing changes live).
    rules = [
        (name, fn)
        for name, fn in DETECTORS.items()
        if (c.conf.get(name) or {}).get("enabled", True) is not False
    ]
    since_ns = None if since is None else _ns(since, a.aware)
    until_ns = None if until is None else _ns(until, a.aware)
    end_us = time_us(scan_end)
    # The bars at or before `since` were evaluated by an earlier scan: on a sorted index they
    # are the first ones, so the loop starts after them instead of walking past them.
    start = 0 if since_ns is None or not a.sorted else int(
        np.searchsorted(a.ns, since_ns, side="right"))  # fmt: skip
    for i in range(start, len(a.ns)):
        t = a.ns[i]
        if since_ns is not None and t <= since_ns:
            continue
        if a.tod[i] > end_us or (until_ns is not None and t > until_ns):
            break
        for _name, fn in rules:
            s = fn(c, i)
            if s is not None and s.key not in fired:
                fired.add(s.key)
                out.append(s)
    return out


def watched_through(c: Ctx, since) -> pd.Timestamp | None:
    """The newest bar the scan may evaluate: the last bar before the first stretch without
    bars, after `since`, that the feed did not watch (a hole in the data, not a stretch with
    no trades) - every bar when there is none. None: not even the first (the feed has not
    watched the day from its open).

    26 Sep 2026 (review B1). Bars after a hole were evaluated as if the hole were real, and
    `last_eval` moved past it, so bars that filled it later were never evaluated (`detect`
    skips every bar at or before `last_eval`). A bar after a hole could then pass for "the
    first close beyond the range" (LEARNINGS #28) or a halt resumption. Now the scan stops
    at the hole and picks up from it once the feed has the minutes; the rules are unchanged.
    """
    idx = c.bars.index
    if not len(idx):
        return None
    day_open = datetime.combine(c.day, OPEN, tzinfo=SYD)
    if since is None and idx[0] > day_open and not c.watched(day_open, idx[0] - ONE_MIN):
        return None
    if len(idx) > 1:
        ns = c.a.ns
        holes = np.diff(ns) > ONE_MIN // timedelta(microseconds=1) * 1000
        if since is not None:
            holes &= ns[1:] > _ns(since, c.a.aware)  # a stretch ending after `since`
        for k in np.flatnonzero(holes):
            a, b = idx[k], idx[k + 1]
            if not c.watched(a + ONE_MIN, b - ONE_MIN):
                return a
    return idx[-1]


# --------------------------------------------------------------------------
# the day's state
# --------------------------------------------------------------------------
def _state_path(data_dir: Path, day: date) -> Path:
    return Path(data_dir) / "arena" / "daytrader" / f"{day.isoformat()}.json"


def load_state(data_dir: Path, day: date) -> dict:
    p = _state_path(data_dir, day)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"fired": [], "last_eval": {}, "signals": [], "universe": []}


def save_state(data_dir: Path, day: date, state: dict) -> None:
    write_text_atomic(json.dumps(state, indent=1, default=str), _state_path(data_dir, day))


def build_universe(arena, pb: Playbook) -> tuple[list[str], dict]:
    """The liquid universe: ASX 300 names passing the size-aware rule and the tick limit,
    from cached daily bars (no network)."""
    from asxbot.data.factory import get_store
    from asxbot.data.universe import build_universes

    conf = pb.raw.get("universe") or {}
    a, _ = build_universes(arena.cfg.data_dir, arena.cfg.get("collector.user_agent"))
    store = get_store(arena.cfg)

    def daily(code):
        try:
            return store.load(code)
        except Exception:  # noqa: BLE001
            return None

    rule = size_rule(pb) or SizeRule(0.05, 20)
    order = pb.level.max_position_aud or 5000.0
    return liquid_universe(list(a.codes), daily, rule, order, float(conf.get("max_tick_pct", 1.0)))


def announcements_since_close(data_dir: Path, day: date) -> pd.DataFrame | None:
    """Every announcement the live collector holds since the previous session's 16:10
    close: the window of "news today" for the scan and for the agent's packet alike."""
    from asxbot.arena.intraday import prior_sessions

    frames = []
    prev = prior_sessions(day, 1)
    for d in ([prev[0]] if prev else []) + [day]:
        p = Path(data_dir) / "announcements" / "live" / f"{d.isoformat()}.parquet"
        if p.exists():
            frames.append(pd.read_parquet(p))
    if not frames:
        return None
    df = pd.concat(frames)
    start = datetime.combine(prev[0], time_cls(16, 10)) if prev else datetime.combine(day, OPEN)
    return df[df["released_at"] >= start]


def news_today(data_dir: Path, day: date) -> set[str]:
    """Codes with a price-sensitive announcement (or a reinstatement) since the previous
    session's close."""
    df = announcements_since_close(data_dir, day)
    if df is None:
        return set()
    keep = df["price_sensitive"].astype(bool) | df["headline"].str.contains(
        "reinstat", case=False, na=False
    )
    return {str(c).upper() for c in df.loc[keep, "code"]}


# --------------------------------------------------------------------------
# one scan
# --------------------------------------------------------------------------
def _continuous_bars(view, code: str, now: datetime) -> tuple[pd.DataFrame, BarArrays | None]:
    """`continuous(view.bars(code, now, fetch=False))`, with its arrays when the view has
    them ready (the replay's, item #18)."""
    fn = getattr(view, "continuous_bars", None)
    if fn is None:
        return continuous(view.bars(code, now, fetch=False)), None
    return fn(code, now, fetch=False)


def scan(
    view: MarketView,
    pb: Playbook,
    codes: list[str],
    now: datetime,
    state: dict,
    shortable: set[str],
    news: set[str],
) -> tuple[list[Setup], dict]:
    """Evaluate every code's new bars. Returns (fresh setups, the scan's top lists)."""
    conf = pb.raw.get("setups") or {}
    scan_conf = pb.raw.get("scan") or {}
    end = _t(scan_conf.get("end", "15:45"))
    max_age = int(pb.raw.get("max_signal_age_bars", 5))
    index, icols = _continuous_bars(view, view.index, now)
    iprev = view.prev_close(view.index)
    fired = set(state.get("fired", []))
    rows, found = [], []
    if iprev is None or not len(index):
        return [], {}
    icols = icols or BarArrays(index)  # the index as arrays, once for every stock (item #18)
    for code in codes:
        bars, cols = _continuous_bars(view, code, now)
        if not len(bars):
            continue
        prev = view.prev_close(code)
        if prev is None:
            continue
        c = Ctx(
            code,
            view.day,
            bars,
            index,
            prev,
            iprev,
            view.usual(code),
            code in shortable,
            code in news,
            conf,
            covered=lambda a, b, code=code: view.covered(code, a, b),
            _cols=cols,
            _icols=icols,
        )
        last_eval = state["last_eval"].get(code)
        since = datetime.fromisoformat(last_eval) if last_eval else None
        # Only up to the first hole the feed has not watched; `last_eval` stays before it,
        # so the bars that fill it are evaluated when they come (26 Sep 2026, B1).
        through = watched_through(c, since)
        new = [] if through is None else detect(c, since, fired, end, until=through)
        if through is not None and (since is None or through > since):
            state["last_eval"][code] = through.isoformat(timespec="minutes")
        for s in new:
            pos = bars.index.get_loc(pd.Timestamp(s.trigger_bar))
            age = len(bars) - 1 - int(pos)
            s.context = {**s.context, "age_bars": age}
            if age > max_age:
                s.context["stale"] = True
            found.append(s)
        a = c.a
        k = int(np.argmax(a.ns))  # the newest bar (`bars.index.max()`)
        hhmm = int(a.tod[k]) // US_PER_MIN
        rows.append(
            {
                "ticker": code,
                "bar": f"{hhmm // 60:02d}:{hhmm % 60:02d}",
                "move_vs_index": c._move(int(a.ns[k])),
                "rvol": c.rvol_i(k),
                "new_high": bool(len(bars) > 1 and a.h[-1] >= _nmax(a.h[:-1])),
                "new_low": bool(len(bars) > 1 and a.l[-1] <= _nmin(a.l[:-1])),
                "resumed": bool(resumptions(c)),
                "news": code in news,
            }
        )
    state["fired"] = sorted(fired)
    top = int(scan_conf.get("top_n", 10))
    df = pd.DataFrame(rows)
    summary: dict = {"stocks": len(rows)}
    summary["interest"] = {r["ticker"]: interest_score(r) for r in rows}
    if len(df):
        df = df.dropna(subset=["move_vs_index"])
        summary["up"] = _top(df.sort_values("move_vs_index", ascending=False), top)
        summary["down"] = _top(df.sort_values("move_vs_index"), top)
        summary["rvol"] = _top(df.dropna(subset=["rvol"]).sort_values("rvol", ascending=False), top)
        summary["new_highs"] = int(df["new_high"].sum())
        summary["new_lows"] = int(df["new_low"].sum())
        summary["resumed"] = sorted(df.loc[df["resumed"], "ticker"].tolist())
        summary["news"] = sorted(df.loc[df["news"], "ticker"].tolist())
    return found, summary


def interest_score(row: dict) -> float:
    """How likely a stock is to trigger a setup in the next minutes, from what the scan
    just saw: its move against the index (in 1% units), its relative volume (in 1.5x
    units), a new day high or low (near the edge of any range), a halt resumption, news.
    Ranks tier 2 of the streaming rotation (ibkr/feed.py); nothing else reads it."""
    mv = row.get("move_vs_index")
    rv = row.get("rvol")
    score = 0.0
    if mv is not None and mv == mv:
        score += abs(float(mv)) / 1.0
    if rv is not None and rv == rv:
        score += float(rv) / 1.5
    if row.get("new_high") or row.get("new_low"):
        score += 1.5
    if row.get("resumed"):
        score += 2.0
    if row.get("news"):
        score += 1.0
    return round(score, 3)


def _top(df: pd.DataFrame, n: int) -> list:
    out = []
    for r in df.head(n).itertuples():
        rv = "" if r.rvol is None or r.rvol != r.rvol else f" {r.rvol:.1f}x"
        out.append(f"{r.ticker} {r.move_vs_index:+.1f}%{rv}")
    return out


# --------------------------------------------------------------------------
# sizing, eligibility, the orders
# --------------------------------------------------------------------------
def entries_today(acct, code: str, day: date) -> list:
    return [
        o
        for o in acct.orders.values()
        if o.ticker == code
        and o.side in OPENING_SIDES
        and o.placed_by != "code"
        and o.decided_at[:10] == day.isoformat()
        and (o.filled_qty > 0 or o.working)
    ]


def eligible(acct, pb: Playbook, code: str, day: date) -> tuple[bool, str]:
    """One re-entry per stock per day, never while a position or entry is working."""
    if code in acct.positions:
        return False, f"already holding {code}"
    if any(o.working and o.ticker == code for o in acct.orders.values()):
        return False, f"an order in {code} is still working"
    n = len(entries_today(acct, code, day))
    cap = int(pb.raw.get("max_entries_per_stock_per_day", 2))
    if n >= cap:
        return False, f"{n} entries in {code} today already (one re-entry a day at most)"
    lvl = pb.level
    becoming = {
        o.ticker
        for o in acct.orders.values()
        if o.working and o.side in OPENING_SIDES and o.ticker not in acct.positions
    }
    if len(acct.positions) + len(becoming) >= lvl.max_open_positions:
        return False, f"the account is full ({lvl.max_open_positions} open or working)"
    if lvl.max_new_positions_per_day is not None:
        opened = sum(
            1
            for o in acct.orders.values()
            if o.side in OPENING_SIDES
            and o.placed_by != "code"
            and o.decided_at[:10] == day.isoformat()
            and (o.filled_qty > 0 or o.working)
        )
        if opened >= lvl.max_new_positions_per_day:
            return False, f"{opened} new positions today already (the limit)"
    return True, ""


def terms(
    arena, pb: Playbook, acct, s: Setup, stop: float | None = None, last: float | None = None
) -> dict | None:
    """(qty, limit, stop) by the rules: a limit through the last visible price, risk <= the
    playbook's share of equity, <= the level's max position, <= 5% of turnover.

    `last` is the stock's newest visible close when the order is made. 26 Sep 2026 (review
    B7): the limit was set through the trigger bar's close (`s.last`), but the written rule
    is "a limit this far through the LAST VISIBLE PRICE" - up to five bars later, the price
    can have moved past a limit set from the trigger. Without `last` (the replay's old
    callers), the trigger bar's close, as before."""
    entry = pb.raw.get("entry") or {}
    slack = float(entry.get("limit_slack_pct", 1.0)) / 100.0
    buy = s.side == "buy"
    ref = float(s.last if last is None else last)
    limit = round_to_tick(ref * (1 + slack if buy else 1 - slack), up=buy)
    stop = round_to_tick(float(stop if stop is not None else s.stop), up=not buy)
    per_share = abs(limit - stop)
    if per_share <= 0 or (buy and stop >= limit) or (not buy and stop <= limit):
        return None
    equity = acct.equity(arena.broker.prices(acct))
    budget = equity * pb.risk_per_trade_pct / 100.0
    caps = [budget / per_share, (pb.level.max_position_aud or 5000.0) / limit]
    rule = size_rule(pb)
    lookup = getattr(arena.broker, "median_turnover", None)
    if rule is not None and lookup is not None:
        t = lookup(s.ticker)
        if t:
            caps.append(rule.max_order(t) / limit)
    qty = int(math.floor(min(caps)))
    min_order = float((arena.cfg.get("arena.guards") or {}).get("min_order_aud", 500))
    if qty <= 0 or qty * limit < min_order:
        return None
    return {
        "qty": qty,
        "limit": limit,
        "stop": stop,
        "risk": round(per_share * qty, 2),
        "value": round(qty * limit, 2),
        "last": ref,  # the price the limit went through; economic() measures 1R from it
    }


def round_trip_cost(arena, value: float, ticker: str) -> float:
    """Brokerage both ways plus slippage both ways for a position of this value, in dollars
    (the arena's own cost model, so the filter and the fills agree)."""
    costs = arena.broker.costs
    adv = arena.broker.adv_lookup(ticker) if getattr(arena.broker, "adv_lookup", None) else None
    slip = costs.slippage_pct(value, adv)
    return 2 * costs.brokerage(value) + 2 * slip * value


def economic(arena, pb: Playbook, t: dict, s: Setup) -> tuple[bool, str]:
    """Rick's brief (25 Sep 2026, day-1 fixes): the scanner must not send a setup whose 1R at
    the largest size the rules allow cannot exceed `entry.min_r_over_costs` (2) times the
    round-trip cost - about 15 of day 1's 36 agent rejections said exactly that ("1R $6-$24
    against roughly $23 of costs"). Sizing is the written rule (risk budget, the $5,000
    cap, 5% of turnover); this only filters, and says why, before anyone is asked."""
    entry = pb.raw.get("entry") or {}
    mult = float(entry.get("min_r_over_costs", 0) or 0)
    if mult <= 0:
        return True, ""
    cost = round_trip_cost(arena, float(t["value"]), s.ticker)
    # 1R as the trade will actually carry it: the fill is at the next bar, near the last
    # price, so R is |last - stop| a share (the limit's 1% slack is a cap on the fill, and
    # `t["risk"]` is the conservative figure the risk cap is checked against). The last
    # price is the one terms() set the limit through (26 Sep 2026, B7), not the trigger's.
    last = float(t.get("last", s.last))
    r = abs(last - float(t["stop"])) * int(t["qty"])
    if r < mult * cost:
        return False, (
            f"uneconomic: 1R ${r:,.0f} at the largest size the rules allow "
            f"(${t['value']:,.0f}, stop {abs(last - t['stop']) / last * 100:.2f}% "
            f"from the last price) is below {mult:g}x the round-trip cost ${cost:,.2f}"
        )
    return True, ""


def place(
    arena, pb: Playbook, acct, s: Setup, t: dict, kind: str, model: str, why: str, seen_at: datetime
) -> dict:
    entry = pb.raw.get("entry") or {}
    good = arena.broker.clock() + timedelta(minutes=int(entry.get("good_for_minutes", 10)))
    alert = notify.get(arena)
    try:
        o = arena_place_order(
            arena.cfg,
            arena.broker,
            acct,
            pb,
            ticker=s.ticker,
            side=s.side,
            qty=t["qty"],
            limit=t["limit"],
            stop=t["stop"],
            reason=f"[{s.setup}] {why}",
            model=model,
            placed_by=kind,
            hold="intraday",
            universe=arena.universe,
            short_universe=arena.short_universe,
            now=seen_at,
            good_till=good,
            manage=dict(pb.raw.get("manage") or {}),
        )
        if alert:
            alert.decided(o)
        return {"order_id": o.order_id, **t}
    except ArenaOrderRefused as e:
        if alert:
            alert.refused(kind, s.ticker, s.side, t["qty"], str(e))
        return {"refused": str(e)}


# --------------------------------------------------------------------------
# the agent: confirm or reject, one call per setup
# --------------------------------------------------------------------------
def data_line(view: MarketView, code: str, at: datetime) -> str:
    """The packet's DATA line: the feed in use and how old the newest bar of this stock is.
    26 Sep 2026 (review G5/I1): the line was fixed text - "delayed data - rehearsal until
    IBKR live prices. The bars below are ~20 minutes behind the market" - and false since 25
    Sep's switch to IBKR, when the newest bar was a median 1.6 minutes old. A factual
    correction of the prompt, not a change of rule."""
    at = at.astimezone(SYD)
    bars = continuous(view.bars(code, at, fetch=False))
    if not len(bars):
        return f"DATA: {view.label}; no bar for {code} yet today."
    ended = bars.index.max().to_pydatetime() + ONE_MIN  # a bar is named by its start
    mins = (at - ended).total_seconds() / 60.0
    ago = "under a minute ago" if mins < 1 else (
        f"{mins:.0f} minute{'' if round(mins) == 1 else 's'} ago")  # fmt: skip
    return f"DATA: {view.label}; the newest {code} bar below ended at {ended:%H:%M}, {ago}."


def agent_packet(
    arena, pb: Playbook, s: Setup, t: dict, context: dict, now: datetime, data: str = ""
) -> str:
    from asxbot.arena.watch import UNTRUSTED, _membership

    return f"""You are trader-decider, working as a DAY TRADER (playbook "{pb.title}", level
{pb.level.number}, fake money). Code's scanner found the setup below by an exact rule. Confirm
or reject it the way a day trader would, from the context. ANSWER WITHIN 60 SECONDS: no
answer in time is a rejection. Be brief.

{UNTRUSTED}

{data or "DATA: the feed was not stated."}
Your order fills at the first bar after it is recorded.
YOU ARE DECIDING AT: {now:%Y-%m-%d %H:%M} Sydney

THE SETUP ({s.setup}, {s.side.upper()}) - {s.ticker}
  trigger bar {s.trigger_bar[11:16]}: {s.why}
{_range_line(s)}  the setup's stop (its invalidation): {_px(t["stop"])}   entry limit: \
{_px(t["limit"])}
  code's size: {t["qty"]:,} shares (${t["value"]:,.0f}), risk ${t["risk"]:,.0f}
  (risk per trade is capped at {pb.risk_per_trade_pct}% of the account)
  code manages the trade: stop to breakeven at +1R, half off at +2R, then a 1R trail;
  everything is closed at 15:50
  {_membership(arena, s.ticker)}

CONTEXT (plain code)
{json.dumps(context, indent=2, default=str)}

Check what a trader checks: the news behind the move (if any), the market's direction, the
stock's industry, how extended it already is (from VWAP, from the open), and whether the stop
is in a sensible place. You may TIGHTEN the stop (move it closer to the entry), never widen
it. Positive expected value after costs is the bar; do not reject to look careful.

End with one JSON block and nothing after it:
{{"action": "take" | "reject", "stop": <optional tighter stop, or null>,
  "why": "<one or two sentences>"}}
"""


def _range_line(s: Setup) -> str:
    """The opening range the setup broke, and that this bar is the FIRST close beyond it,
    so the agent can see what the scanner saw (day 1's "doesn't match the bars")."""
    c = s.context or {}
    if c.get("range_high") is None:
        return ""
    return (
        f"  the {c.get('range_minutes')}-min opening range: low {_px(c.get('range_low'))}, high "
        f"{_px(c.get('range_high'))}; this bar ({c.get('first_break')}) is the FIRST close "
        "beyond it today\n"
    )


def setup_context(
    view: MarketView, s: Setup, now: datetime, industry_of: dict, scan_rows: dict, news_rows: dict
) -> dict:
    bars = continuous(view.bars(s.ticker, now, fetch=False))
    index = continuous(view.bars(view.index, now, fetch=False))
    prev, iprev = view.prev_close(s.ticker), view.prev_close(view.index)
    last = float(bars["close"].iloc[-1]) if len(bars) else s.last
    vw = vwap(bars) if len(bars) else None
    ind = industry_of.get(s.ticker, "")
    peers = [v for k, v in scan_rows.items() if industry_of.get(k) == ind and k != s.ticker]
    out = {
        "last_visible_bar": bars.index.max().strftime("%H:%M") if len(bars) else None,
        "last": last,
        "move_since_prev_close_pct": None if not prev else round((last / prev - 1) * 100, 2),
        "move_since_open_pct": None
        if not len(bars)
        else round((last / float(bars["open"].iloc[0]) - 1) * 100, 2),
        "distance_from_vwap_pct": None
        if vw is None
        else round((last / float(vw.iloc[-1]) - 1) * 100, 2),
        "day_high": None if not len(bars) else float(bars["high"].max()),
        "day_low": None if not len(bars) else float(bars["low"].min()),
        "rvol": None if s.rvol is None else round(s.rvol, 2),
        "market_asx200_since_prev_close_pct": None
        if not (len(index) and iprev)
        else round((float(index["close"].iloc[-1]) / iprev - 1) * 100, 2),
        "market_asx200_since_open_pct": None
        if not len(index)
        else round((float(index["close"].iloc[-1]) / float(index["open"].iloc[0]) - 1) * 100, 2),
        "industry": ind,
        "industry_peers_median_move_vs_index_pct": None
        if not peers
        else round(float(pd.Series(peers).median()), 2),
        "industry_peers_n": len(peers),
        "news_today": news_rows.get(s.ticker, []),
        "last_15_bars": [
            f"{ts:%H:%M} o{_px(r.open)} h{_px(r.high)} l{_px(r.low)} c{_px(r.close)} "
            f"v{int(r.volume)}"
            for ts, r in bars.tail(15).iterrows()
        ],
    }
    return out


def ask_agent(
    arena, pb: Playbook, s: Setup, t: dict, context: dict, now: datetime, data: str = ""
) -> dict:
    """One decider call, answer within the configured seconds, or it is a rejection. A call
    that could not be made at all (the plan's usage limit, OpenClaw down) is "unavailable",
    never a rejection (26 Sep 2026, review G1: the agent's side would otherwise read as a day
    of judgement it never made)."""
    from asxbot.arena.agents import expected_model, fresh_sessions_on, parse_decision

    limit_s = int((pb.raw.get("agent") or {}).get("timeout_s", 60))
    started = time_mod.monotonic()
    try:
        reply = call_agent(
            DECIDER,
            agent_packet(arena, pb, s, t, context, now, data),
            expect_model=expected_model(arena.cfg, "decider"),
            timeout_s=limit_s,
            data_dir=arena.cfg.data_dir,
            purpose=f"day trader {s.setup} {s.ticker}",
            process_timeout_s=limit_s + 15,
            fresh_session=fresh_sessions_on(arena.cfg),
        )
    except AgentCallFailed as e:
        kind = str(getattr(e, "kind", "error"))
        if kind == "timeout":  # the rule: no answer within the limit is a rejection
            return {"action": "reject", "why": f"no answer within {limit_s}s ({e})",
                    "model": ""}  # fmt: skip
        return {"action": "unavailable", "kind": kind, "why": f"agent unavailable ({kind}): {e}",
                "model": ""}  # fmt: skip
    took = time_mod.monotonic() - started
    d = parse_decision(reply.text) if "action" in reply.text else {"action": "reject"}
    # The rule: "No answer within 60 s is a rejection." 26 Sep 2026 (review B9): this was
    # `took > limit_s + 15`, and the process is itself killed at limit_s + 15, so an answer
    # at 60-75 s was accepted and the check could never fire. The extra 15 s stay on the
    # process only, so a late answer still comes back and is recorded - as a rejection.
    if took > limit_s:
        return {
            "action": "reject",
            "why": f"answered after {took:.0f}s, past {limit_s}s",
            "model": reply.model,
            "seconds": round(took, 1),
            "late_answer": str(d.get("action", "")),
        }
    action = str(d.get("action", "reject")).lower()
    return {
        "action": "take" if action in ("take", "trade", "confirm") else "reject",
        "stop": d.get("stop"),
        "why": str(d.get("why", "")),
        "model": reply.model,
        "seconds": round(took, 1),
    }


# --------------------------------------------------------------------------
# the cycle
# --------------------------------------------------------------------------
_DAY: dict = {}
# After the close, today's whole session is written to the minute cache once: tomorrow's
# previous close and usual volume come from it (the scan keeps today's bars in memory).
EOD_AT = time_cls(16, 40)


def end_of_day(arena, pb: Playbook, view: MarketView, now: datetime) -> dict:
    """Write today's complete sessions for the scan universe, today's news stocks and the
    index to the minute cache. Once a day; recorded in the day's state."""
    from asxbot.arena.intraday import backfill
    from asxbot.arena.reaction_v2 import load_queue

    cfg = arena.cfg
    day = view.day
    state = load_state(cfg.data_dir, day)
    if state.get("eod"):
        return state["eod"]
    codes = set(state.get("universe") or _DAY.get("codes") or [])
    codes |= {c for c in load_queue(cfg.data_dir, day) if not c.startswith("_")}
    codes |= news_today(cfg.data_dir, day)
    index = str(cfg.get("backtest.index_ticker", "^AXJO"))
    try:
        res = backfill(view.minutes, [index, *sorted(codes)], [day], batch=60, pause_s=2.0)
    except Exception as e:  # noqa: BLE001
        log.error("end-of-day minute backfill failed: %s", e)
        return {}
    state["eod"] = {"at": now.isoformat(timespec="seconds"), **res}
    save_state(cfg.data_dir, day, state)
    log.info("end of day: %d sessions written to the minute cache", res.get("written", 0))
    return state["eod"]


def cycle(
    arena,
    pb: Playbook,
    view: MarketView,
    now: datetime | None = None,
    use_agent: bool = True,
    refresh: bool = True,
) -> list[dict]:
    """One scan: refresh the stalest stocks, find setups, bot and agent act on each."""
    now = (now or arena.broker.clock()).astimezone(SYD)
    cfg = arena.cfg
    day = view.day
    scan_conf = pb.raw.get("scan") or {}
    if now.weekday() >= 5:
        return []
    if not view.replay and now.time() >= EOD_AT:
        end_of_day(arena, pb, view, now)
        return []
    if now.time() < _t(scan_conf.get("start", "10:00")):
        _prepare_history(arena, pb, view)
        return []
    reported = _history_report_once(arena, pb, view, now)  # late, if 09:55-10:00 was missed
    ev = EventLog(cfg.data_dir)
    state = load_state(cfg.data_dir, day)
    if reported and _DAY.get("day") == day and state.get("universe"):
        _DAY["codes"] = state["universe"]  # the report leaves out stocks with no history
    if _DAY.get("day") != day or _DAY.get("data_dir") != str(cfg.data_dir):
        codes, _why = (
            (state["universe"], None) if state.get("universe") else build_universe(arena, pb)
        )
        state["universe"] = codes
        _DAY.clear()
        _DAY.update(
            day=day,
            data_dir=str(cfg.data_dir),
            codes=codes,
            news=news_today(cfg.data_dir, day),
        )
        log.info("day trader: %d stocks in today's liquid universe", len(codes))
        save_state(cfg.data_dir, day, state)
    codes = _DAY["codes"]
    data_time = view.data_time(now)
    if data_time is None or data_time.time() < OPEN:
        return []
    if data_time.time() > _t(scan_conf.get("end", "15:45")):
        return []  # the scan's window is over in market time: no more requests today
    ok, why = entries_allowed(view, now)
    if not ok:
        # Rick, 25 Sep: no entry on anything but fresh IBKR prices. Nothing is scanned while
        # the feed is down or stale, so nothing can trigger; when it is back, a trigger that
        # happened meanwhile is older than max_signal_age_bars and is skipped as stale.
        # Exits keep working in the broker from the best bars held.
        if _DAY.get("paused") != why:
            _DAY["paused"] = why
            log.error("day trader paused: live feed down (%s); no scan and no entries until "
                      "it is back; exits keep working", why)  # fmt: skip
            ev.append("daytrader_scan", {"at": now.isoformat(timespec="seconds"), "paused": why})
        return []
    if _DAY.pop("paused", None):
        log.warning("day trader resumed: the live feed is back")
    if refresh and not view.replay:
        _DAY["news"] = news_today(cfg.data_dir, day) if now.minute % 5 == 0 else _DAY["news"]
        view.prepare([view.index, *codes])  # IBKR: prior sessions still missing, if any
        refreshed = view.feed.refresh(codes, now)
        view.mark_fetched(refreshed, now)
    found, summary = scan(view, pb, codes, now, state, set(arena.short_universe), _DAY["news"])
    # 26 Sep 2026 (review B12): saved as soon as the scan has marked what fired and how far
    # each stock was evaluated, and again after every decision below. It was saved only at
    # the end of the cycle, so an exception after an order was placed lost `fired` and
    # `last_eval`, and the next cycle found the same setup and asked the agent again.
    save_state(cfg.data_dir, day, state)
    interest = summary.pop("interest", {}) if summary else {}
    if interest and hasattr(view.feed, "note_interest"):
        view.feed.note_interest(interest)  # who streams next cycle (ibkr/feed.py rotation)
    if summary:
        ev.append(
            "daytrader_scan",
            {
                "at": now.isoformat(timespec="seconds"),
                "feed_at": data_time.isoformat(timespec="minutes"),
                "found": [s.key for s in found],
                **summary,
            },
        )
    if not found:
        return []
    rows, lasts = _peer_moves(view, codes, now)
    out = []
    order = sorted(found, key=lambda s: -(s.rvol or 0.0))
    last_entry = pb.last_entry_time
    max_age = int(pb.raw.get("max_signal_age_bars", 5))
    scanned_at = arena.broker.clock()
    recs = []
    # The rule bot first, on every setup: it needs no call, so it acts at the scan's moment.
    for s in order:
        rec = {"at": now.isoformat(timespec="seconds"), **s.to_dict(), "data": view.label,
               "bot": None, "agent": None}  # fmt: skip
        if s.context.get("stale"):
            rec["skipped"] = f"stale: the trigger bar is {s.context['age_bars']} bars old"
        elif last_entry is not None and scanned_at.astimezone(SYD).time() > last_entry:
            rec["skipped"] = f"after the last entry time {last_entry:%H:%M}"
        else:
            # 26 Sep 2026 (review B8/A6): the feed is asked about THIS stock's prices before
            # either book acts. The check above the scan named no stock, so only the index's
            # freshness was ever tested (ibkr/live.py checks each named, streamed stock).
            ok, why = entries_allowed(view, now, [s.ticker])
            if not ok:
                rec["skipped"] = f"no new entry on {s.ticker}'s prices now: {why}"
            else:
                rec["bot"] = _guarded("the bot", s, lambda s=s: _bot_take(
                    arena, pb, s, now, day, last=lasts.get(s.ticker)))  # fmt: skip
        recs.append((s, rec))
        state["signals"].append(rec)
    save_state(cfg.data_dir, day, state)
    # Then the agent, one call per setup, while the setup is still fresh: its trigger bar's
    # age plus the whole minutes spent on the calls before it must stay within
    # max_signal_age_bars - the same test the bot passed (`age > max_age` is stale).
    for s, rec in recs:
        if use_agent and not rec.get("skipped"):
            waited = (arena.broker.clock() - scanned_at).total_seconds() / 60.0
            # 26 Sep 2026 (review B3/G4): was `age + waited > max_age` with `waited` in
            # fractional minutes, never exactly 0 live, so a setup exactly at the limit went
            # to the bot and never to the agent (25 Sep: NWL and DOW at 11:20, "5 bars old at
            # the scan, 0.0 minutes"). Whole minutes, as bars are.
            if int(s.context.get("age_bars", 0)) + math.floor(waited) > max_age:
                rec["agent"] = {
                    "skipped": f"stale by its turn: {s.context.get('age_bars', 0)} bars old at "
                    f"the scan, {waited:.1f} minutes of agent calls before it"
                }
            else:
                rec["agent"] = _guarded("the agent", s, lambda s=s: _agent_take(
                    arena, pb, view, s, now, day, rows))  # fmt: skip
            save_state(cfg.data_dir, day, state)
        ev.append("daytrader_setups", rec)
        out.append(rec)
    save_state(cfg.data_dir, day, state)
    return out


def _guarded(who: str, s: Setup, fn) -> dict:
    """One book's action on one setup. An exception is logged and recorded, and the other
    setups in the cycle still get theirs (26 Sep 2026, with B12: one failure lost them all)."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        log.exception("day trader: %s failed on %s %s: %s", who, s.ticker, s.setup, e)
        return {"error": f"{type(e).__name__}: {e}"}


def _peer_moves(view: MarketView, codes: list[str], now: datetime) -> tuple[dict, dict]:
    """Each stock's move against the ASX 200 since the previous close (for the agent's
    industry peers), and its newest visible close (for the bot's limit, B7).

    26 Sep 2026: the peers' figure was the plain move since the previous close, handed to the
    agent as "industry_peers_median_move_vs_index_pct"; on a day the index fell 1%, a flat
    industry read as 1% ahead of the market. Now it is against the index, as it says."""
    index, _ = _continuous_bars(view, view.index, now)
    iprev = view.prev_close(view.index)
    ix = (float(index["close"].iloc[-1]) / iprev - 1) * 100 if len(index) and iprev else None
    rows, lasts = {}, {}
    for code in codes:
        b, _ = _continuous_bars(view, code, now)
        if not len(b):
            continue
        lasts[code] = float(b["close"].iloc[-1])
        p = view.prev_close(code)
        if p and ix is not None:
            rows[code] = (lasts[code] / p - 1) * 100 - ix
    return rows, lasts


def _last_visible(view: MarketView, code: str, at: datetime) -> float | None:
    """The stock's newest visible close at `at`: the "last visible price" of the entry rule."""
    b = continuous(view.bars(code, at, fetch=False))
    return float(b["close"].iloc[-1]) if len(b) else None


def _pre_codes(arena, pb: Playbook, view: MarketView) -> bool:
    """Today's universe and the index, for the prior-session fetch and the history report."""
    if _PRE.get("day") != view.day:
        try:
            codes, _why = build_universe(arena, pb)
        except Exception as e:  # noqa: BLE001
            log.warning("could not list the universe for the pre-open history: %s", e)
            return False
        _PRE.clear()
        _PRE.update(day=view.day, codes=[view.index, *codes])
    return True


def _prepare_history(arena, pb: Playbook, view: MarketView) -> None:
    """Before the scan starts, and only for a feed that keeps its own prior sessions (IBKR):
    fetch them for the liquid universe, as far as the pacing allows each cycle, so the first
    scans have "usual volume" from the same source as today's bars. Yahoo's feed reads the
    minute cache instead, so nothing is done for it."""
    if view.replay or view.feed.history_source() is view.minutes:
        return
    if not _pre_codes(arena, pb, view):
        return
    view.prepare(_PRE["codes"])
    _history_report_once(arena, pb, view, arena.broker.clock().astimezone(SYD))


def _history_report_once(arena, pb: Playbook, view: MarketView, now: datetime) -> bool:
    """The 09:55 history report, at the first cycle at or after 09:55 whatever the time,
    once a day (the day's state remembers it across a restart). True when it ran now.

    26 Sep 2026 (review A12): it ran only from the pre-open path, so on a busy pre-open with
    no cycle between 09:55 and 10:00 it never ran - no report, no alert to Rick, and stocks
    with no prior sessions stayed in the scan."""
    if view.replay or view.feed.history_source() is view.minutes:
        return False
    if now.astimezone(SYD).time() < HISTORY_DEADLINE or _PRE.get("reported") == view.day:
        return False
    if load_state(arena.cfg.data_dir, view.day).get("history"):
        _PRE["reported"] = view.day  # reported before a restart
        return False
    if not _pre_codes(arena, pb, view):
        return False
    _PRE["reported"] = view.day
    try:
        history_deadline_report(arena, view, _PRE["codes"], now)
    except Exception as e:  # noqa: BLE001 - a report must never stop the scan
        log.exception("the IBKR history report failed: %s", e)
        return False
    return True


HISTORY_DEADLINE = time_cls(9, 55)
_PRE: dict = {}


def history_deadline_report(arena, view: MarketView, codes: list[str], now: datetime) -> dict:
    """09:55: which stocks have their prior sessions from IBKR and which do not. The ones
    without are named, logged, told to Rick in one line, and left OUT of today's scan
    universe rather than stalling it (Rick's brief, 25 Sep). A stock IBKR could not serve
    keeps being asked for by the v2 looks on demand (ensure_history); the day trader does
    not wait for it."""
    status_fn = getattr(view.feed, "history_status", None)
    if status_fn is None:
        return {}
    st = status_fn(codes, view.day, view.sessions)
    missing = sorted(set(st.get("missing", [])) | set(st.get("given_up", [])))
    universe = [c for c in codes if c != view.index and c not in missing]
    state = load_state(arena.cfg.data_dir, view.day)
    state["history"] = {
        "at": now.isoformat(timespec="seconds"), "complete": len(st.get("complete", [])),
        "missing": missing, "given_up": sorted(st.get("given_up", [])),
    }  # fmt: skip
    state["universe"] = universe
    save_state(arena.cfg.data_dir, view.day, state)
    EventLog(arena.cfg.data_dir).append("ibkr_history", {"day": view.day.isoformat(),
                                                          **state["history"]})  # fmt: skip
    if missing:
        log.warning(
            "IBKR history incomplete at %s: %d of %d stocks have no prior sessions and are "
            "out of today's day-trader scan: %s", now.strftime("%H:%M"), len(missing),
            len(codes) - 1, ", ".join(missing[:40]) + (" ..." if len(missing) > 40 else ""),
        )  # fmt: skip
        alert = notify.get(arena)
        if alert:
            alert.send(
                f"IBKR history at {now:%H:%M}: {len(missing)} of {len(codes) - 1} stocks have no "
                f"prior sessions and are out of today's day-trader scan: "
                + ", ".join(missing[:25]) + (" ..." if len(missing) > 25 else "")
            )
    else:
        log.info("IBKR history complete at %s for all %d stocks and the index",
                 now.strftime("%H:%M"), len(codes) - 1)  # fmt: skip
    return state["history"]


def _bot_take(arena, pb, s: Setup, now: datetime, day: date, last: float | None = None) -> dict:
    acct = arena.account(pb, "bot")
    ok, why = eligible(acct, pb, s.ticker, day)
    if not ok:
        return {"skipped": why}
    t = terms(arena, pb, acct, s, last=last)
    if t is None:
        return {"skipped": "cannot be sized (stop on the wrong side, or below the minimum order)"}
    ok, why = economic(arena, pb, t, s)
    if not ok:
        log.info("day trader: %s %s not sent to the bot - %s", s.ticker, s.setup, why)
        return {"skipped": why, **t}
    return place(arena, pb, acct, s, t, "bot", "none (rule-based bot)", f"rule: {s.why}", now)


def _agent_take(arena, pb, view, s: Setup, now: datetime, day: date, rows: dict) -> dict:
    acct = arena.account(pb, "agent")
    ok, why = eligible(acct, pb, s.ticker, day)
    if not ok:
        return {"skipped": why}
    # Earlier agent calls in this cycle take time: the stock's prices are asked about again,
    # and the packet shows the market as it is now, not as it was at the scan (26 Sep 2026).
    at = arena.broker.clock().astimezone(SYD)
    ok, why = entries_allowed(view, at, [s.ticker])
    if not ok:
        return {"skipped": f"no new entry on {s.ticker}'s prices now: {why}"}
    t = terms(arena, pb, acct, s, last=_last_visible(view, s.ticker, at))
    if t is None:
        return {"skipped": "cannot be sized"}
    ok, why = economic(arena, pb, t, s)
    if not ok:
        log.info("day trader: %s %s not sent to the agent - %s", s.ticker, s.setup, why)
        return {"skipped": why, **t}
    if "industry" not in _DAY:
        _DAY["industry"] = _industries(arena)
    ctx = setup_context(view, s, at, _DAY["industry"], rows, _news_rows(arena, day))
    seen_at = arena.broker.clock()
    answer = ask_agent(arena, pb, s, t, ctx, at, data_line(view, s.ticker, at))
    EventLog(arena.cfg.data_dir).append(
        "arena_decisions",
        {
            "stage": "daytrader",
            "ticker": s.ticker,
            "setup": s.setup,
            "side": s.side,
            "decision": answer,
            "model": answer.get("model", ""),
            **({"outcome": "agent_unavailable", "kind": answer.get("kind")}
               if answer["action"] == "unavailable" else {}),  # fmt: skip
        },
    )
    if answer["action"] == "unavailable":
        return {"unavailable": answer.get("kind"), "why": answer.get("why", "")}
    if answer["action"] != "take":
        # Not queued for the hourly digest: the scan can find dozens of setups a day and
        # the digest is for passes worth reading (#37). They are counted in the evening
        # report and each is in the event log (arena_decisions, stage daytrader).
        return {"rejected": answer.get("why", ""), "seconds": answer.get("seconds")}
    # Placed at the prices as they are once the agent has answered: the entry rule's "last
    # visible price", and the feed asked once more about this stock (26 Sep 2026, B7, B8).
    placing = arena.broker.clock().astimezone(SYD)
    ok, why = entries_allowed(view, placing, [s.ticker])
    if not ok:
        log.warning("day trader: %s %s confirmed by the agent but not placed - %s",
                    s.ticker, s.setup, why)  # fmt: skip
        return {"refused": f"confirmed, but no new entry on {s.ticker}'s prices now: {why}",
                "seconds": answer.get("seconds")}  # fmt: skip
    last = _last_visible(view, s.ticker, placing)
    ref = float(s.last if last is None else last)
    stop = s.stop
    try:
        new = float(answer.get("stop")) if answer.get("stop") not in (None, "") else None
    except (TypeError, ValueError):
        new = None
    if new is not None:
        tighter = (s.side == "buy" and s.stop < new < ref) or (
            s.side == "short" and ref < new < s.stop
        )
        stop = new if tighter else s.stop
    t = terms(arena, pb, acct, s, stop, last=last)
    if t is None:
        return {"skipped": "cannot be sized with the agent's stop"}
    # 26 Sep 2026 (review G13): the filter the bot's order passed is asked again of the order
    # the agent will actually place. A tighter stop re-sizes the order; without this check
    # the agent could place what the uneconomic filter refuses the bot.
    ok, why = economic(arena, pb, t, s)
    if not ok:
        log.info("day trader: %s %s confirmed by the agent but not placed - %s",
                 s.ticker, s.setup, why)  # fmt: skip
        return {"refused": f"with the agent's stop, {why}", "seconds": answer.get("seconds"),
                **t}  # fmt: skip
    res = place(
        arena,
        pb,
        acct,
        s,
        t,
        "agent",
        answer.get("model") or "agent",
        f"agent confirmed: {answer.get('why', '')}",
        seen_at,
    )
    return {**res, "seconds": answer.get("seconds")}


def _industries(arena) -> dict:
    try:
        from asxbot.data.universe import fetch_directory

        d = fetch_directory(arena.cfg.data_dir, arena.cfg.get("collector.user_agent"))
        return {str(r.code).upper(): str(r.industry) for r in d.itertuples()}
    except Exception:  # noqa: BLE001
        return {}


def _news_rows(arena, day: date) -> dict:
    """The agent's "news_today", over the window the scan's `news_today` uses. 26 Sep 2026
    (review B5): it read only today's file, so a stock flagged for last evening's
    price-sensitive announcement reached the agent with no news in its packet."""
    df = announcements_since_close(arena.cfg.data_dir, day)
    if df is None or not len(df):
        return {}
    df = df.drop_duplicates(subset=["code", "released_at", "headline"])
    out: dict = {}
    for r in df.sort_values("released_at").itertuples():
        when = f"{r.released_at:%H:%M}" if r.released_at.date() == day else (
            f"{r.released_at:%a %d %b %H:%M}")  # fmt: skip
        sens = "[price sensitive] " if r.price_sensitive else ""
        out.setdefault(str(r.code).upper(), []).append(f"{when} {sens}{r.headline}")
    return out
