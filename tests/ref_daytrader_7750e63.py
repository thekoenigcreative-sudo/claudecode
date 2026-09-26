"""THE OLD CODE, kept as the reference for tests/test_scan_speed.py (item #18, 27 Sep 2026).

Copied verbatim from commit 7750e63 (src/asxbot/arena/daytrader.py and intraday.py), before
the day trader's scan read its bars as arrays: the rules as they ran, pandas slice by pandas
slice. Nothing imports this but that test. Do not edit it to make a test pass - the new code
must give what this gives.
"""
# ruff: noqa

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as time_cls
from zoneinfo import ZoneInfo

import pandas as pd

from asxbot.arena.daytrader import Setup, _px
from asxbot.arena.intraday import CONTINUOUS_END, SESSION_OPEN, VOLUME_FROM, prior_sessions

SYD = ZoneInfo("Australia/Sydney")
OPEN = time_cls(10, 0)
ONE_MIN = timedelta(minutes=1)

def visible(
    df: pd.DataFrame | None,
    now: datetime,
    delay_minutes: int | None = None,
    day_complete: bool = False,
    index: bool = False,
) -> pd.DataFrame:
    """The traded bars final by `now`. With `delay_minutes` (the replay), also only those a
    feed that far behind would already hold: a bar starting at t is in the feed from
    t + 1 minute + the delay, and is final once the next row exists after it."""
    if df is None or not len(df):
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    now = now.astimezone(SYD)
    cutoff = now - timedelta(minutes=1)
    if delay_minutes is not None:
        # The feed holds rows starting up to now - delay - 1; the newest of those is still
        # forming, so a bar is final once a row after it exists: start <= now - delay - 2.
        cutoff = now - timedelta(minutes=int(delay_minutes) + 2)
        out = df[df.index <= cutoff]
    else:
        out = df[df.index <= cutoff]
        if not day_complete and len(out):
            newest = df.index.max()
            out = out[out.index < newest]  # the newest row may still be forming
    # Index bars carry no volume (minutes.index_open); a stock's untraded minute has none.
    return out[out["close"] > 0] if index else out[out["volume"] > 0]

def continuous(df: pd.DataFrame) -> pd.DataFrame:
    """Continuous-trading bars, 10:00 to before 16:00 (no closing auction)."""
    if not len(df):
        return df
    t = df.index.time
    return df[(t >= SESSION_OPEN) & (t < CONTINUOUS_END)]

def vwap(df: pd.DataFrame) -> pd.Series:
    """The day's running VWAP, typical price (h+l+c)/3 weighted by volume."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = (tp * df["volume"]).cumsum()
    v = df["volume"].cumsum()
    return pv / v.where(v > 0)

def minute_of_session(ts) -> int:
    return (ts.hour * 60 + ts.minute) - (SESSION_OPEN.hour * 60 + SESSION_OPEN.minute)

def usual_cum_volume(
    minutes: MinuteBars, code: str, day: date, sessions: int = 5, min_sessions: int = 3
) -> pd.Series | None:
    """For each minute of the session (0 = 10:00), the mean over the prior sessions in the
    cache of the volume traded from 10:00 through that minute. None with too few sessions."""
    curves = []
    for prev in prior_sessions(day, sessions):
        df = minutes.cached(code, prev)
        if df is None or not len(df):
            continue
        c = continuous(df)
        if not len(c):
            continue
        per_min = pd.Series(0.0, index=range(360))
        for ts, v in c["volume"].items():
            m = minute_of_session(ts)
            if 1 <= m < 360:  # from the 10:01 bar (VOLUME_FROM)
                per_min[m] += float(v)
        curves.append(per_min.cumsum())
    if len(curves) < min_sessions:
        return None
    return pd.concat(curves, axis=1).mean(axis=1)

def rvol_at(today: pd.DataFrame, usual: pd.Series | None, ts) -> float | None:
    """Volume from 10:00 through bar `ts`, against the usual volume by that minute."""
    if usual is None:
        return None
    m = minute_of_session(ts)
    if m < 0 or m >= len(usual):
        return None
    base = float(usual.iloc[m])
    if base <= 0:
        return None
    got = float(counted_volume(today, None, ts))
    return got / base

def counted_volume(df: pd.DataFrame, start=None, end=None) -> float:
    """Continuous-trading volume from `start` through `end` (inclusive), never counting the
    10:00 bar (VOLUME_FROM)."""
    c = continuous(df)
    if not len(c):
        return 0.0
    c = c[c.index.time >= VOLUME_FROM]
    if start is not None:
        c = c[c.index >= start]
    if end is not None:
        c = c[c.index <= end]
    return float(c["volume"].sum())


def _t(s: str) -> time_cls:
    return time_cls.fromisoformat(str(s))


def _in(ts, window) -> bool:
    return _t(window[0]) <= ts.time() <= _t(window[1])


def _close_at(df: pd.DataFrame, ts) -> float | None:
    part = df[df.index <= ts]
    return None if not len(part) else float(part["close"].iloc[-1])


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

    def watched(self, start, end) -> bool:
        """True when a stretch with no bars from `start` to `end` (minutes, inclusive) means
        the stock did not trade, not that the feed was not looking."""
        return True if self.covered is None else bool(self.covered(start, end))

    @property
    def vw(self) -> pd.Series:
        if self._vwap is None:
            self._vwap = vwap(self.bars)
        return self._vwap

    def rvol(self, ts) -> float | None:
        return rvol_at(self.bars, self.usual, ts)

    def move_vs_index(self, ts) -> float | None:
        px, ix = _close_at(self.bars, ts), _close_at(self.index, ts)
        if px is None or ix is None:
            return None
        return ((px / self.prev_close - 1) - (ix / self.index_prev_close - 1)) * 100


def gap_and_go(c: Ctx, i: int) -> Setup | None:
    p = c.conf["gap_and_go"]
    ts = c.bars.index[i]
    if not _in(ts, p["window"]):
        return None
    day_open = datetime.combine(c.day, OPEN, tzinfo=SYD)
    rng_end = day_open + timedelta(minutes=int(p["range_minutes"]))
    opening = c.bars[(c.bars.index >= day_open) & (c.bars.index < rng_end)]
    if not len(opening) or not len(c.index):
        return None
    iopen_row = c.index[c.index.index >= day_open]
    if not len(iopen_row):
        return None
    gap = (
        (float(opening["open"].iloc[0]) / c.prev_close - 1)
        - (float(iopen_row["open"].iloc[0]) / c.index_prev_close - 1)
    ) * 100
    need = float(p["gap_pct_vs_index"])
    bar = c.bars.iloc[i]
    so_far = c.bars.iloc[: i + 1]
    hi, lo = float(opening["high"].max()), float(opening["low"].min())
    # The rule names THE FIRST bar in the window that closes beyond the range. Only that
    # bar can be the trigger; its RVOL is then tested. Until 2026-09-25 any later bar that
    # happened to have the volume fired, hours after the range had gone (see
    # opening_range_breakout, the same fault, found in the agent's day-1 rejections).
    first_up, first_down = _first_breaks(c.bars, p["window"], hi, lo)
    rv = c.rvol(ts)
    ctx = {"range_high": hi, "range_low": lo, "range_minutes": int(p["range_minutes"])}
    if gap >= need and first_up == ts and float(so_far["low"].min()) >= c.prev_close:
        if rv is None or rv < float(p["min_rvol"]):
            return None  # the first close above the range had no volume: no setup today
        return Setup(
            c.ticker,
            "gap_and_go",
            "buy",
            ts.isoformat(timespec="minutes"),
            float(bar["close"]),
            lo,
            rv,
            f"gap {gap:+.1f}% vs index, first close ({_px(bar['close'])}) above the "
            f"{p['range_minutes']}-min high {_px(hi)}, gap unfilled, RVOL {rv:.1f}",
            {**ctx, "first_break": ts.strftime("%H:%M")},
        )
    if (
        gap <= -need
        and c.shortable
        and first_down == ts
        and float(so_far["high"].max()) <= c.prev_close
    ):
        if rv is None or rv < float(p["min_rvol"]):
            return None
        return Setup(
            c.ticker,
            "gap_and_go",
            "short",
            ts.isoformat(timespec="minutes"),
            float(bar["close"]),
            hi,
            rv,
            f"gap {gap:+.1f}% vs index, first close ({_px(bar['close'])}) below the "
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
    ts = c.bars.index[i]
    if not _in(ts, p["window"]):
        return None
    day_open = datetime.combine(c.day, OPEN, tzinfo=SYD)
    rng = c.bars[
        (c.bars.index >= day_open)
        & (c.bars.index < day_open + timedelta(minutes=int(p["range_minutes"])))
    ]
    if not len(rng):
        return None
    hi, lo = float(rng["high"].max()), float(rng["low"].min())
    # THE FIRST bar in the window that closes beyond the range is the only bar that can be
    # the breakout; the volume test is applied to it. Until 2026-09-25 the code fired on
    # the first bar that closed beyond the range AND had the volume, which let a range
    # broken quietly at 10:31 produce a "breakout" on a heavy bar hours later, with the
    # stop (the range's midpoint) a long way off: CWY, HDN, AUB, ORI, CHC and DRR on 25 Sep,
    # each rejected by the agent as "the scanner's 30-min low doesn't match the bars". The
    # range was right; the bar was not the break. A first break without the volume means
    # no opening-range setup on that side today (tests/test_day1_fixes.py).
    first_up, first_down = _first_breaks(c.bars, p["window"], hi, lo)
    bar = c.bars.iloc[i]
    up = first_up == ts and bar["close"] > hi
    down = first_down == ts and bar["close"] < lo and c.shortable
    if not (up or down):
        return None
    look = int(p["bar_volume_lookback"])
    before = c.bars.iloc[max(0, i - look) : i]
    if len(before) < max(3, look // 2):
        return None
    avg = float(before["volume"].mean())
    if avg <= 0 or float(bar["volume"]) < float(p["bar_volume_multiple"]) * avg:
        return None  # the break came without the volume: no setup on this side today
    rv = c.rvol(ts)
    if rv is None or rv < float(p["min_rvol"]):
        return None
    mid = (hi + lo) / 2
    vol_x = float(bar["volume"]) / avg
    ctx = {"range_high": hi, "range_low": lo, "range_minutes": int(p["range_minutes"]),
           "first_break": ts.strftime("%H:%M")}  # fmt: skip
    if up and mid < bar["close"]:
        return Setup(
            c.ticker,
            "opening_range_breakout",
            "buy",
            ts.isoformat(timespec="minutes"),
            float(bar["close"]),
            mid,
            rv,
            f"first close ({_px(bar['close'])}) above the 30-min high {_px(hi)} on {vol_x:.1f}x "
            f"the prior {look} bars' volume, RVOL {rv:.1f}",
            ctx,
        )
    if down and mid > bar["close"]:
        return Setup(
            c.ticker,
            "opening_range_breakout",
            "short",
            ts.isoformat(timespec="minutes"),
            float(bar["close"]),
            mid,
            rv,
            f"first close ({_px(bar['close'])}) below the 30-min low {_px(lo)} on {vol_x:.1f}x "
            f"the prior {look} bars' volume, RVOL {rv:.1f}",
            ctx,
        )
    return None


def vwap_reclaim(c: Ctx, i: int) -> Setup | None:
    p = c.conf["vwap_reclaim"]
    ts = c.bars.index[i]
    if not _in(ts, p["window"]):
        return None
    slope, lookback = int(p["vwap_slope_bars"]), int(p["pullback_lookback"])
    if i - 1 - slope < 0 or i < lookback:
        return None
    vw, closes = c.vw, c.bars["close"]
    prev_ts = c.bars.index[i - 1]
    move = c.move_vs_index(prev_ts)
    if move is None:
        return None
    window = range(i - lookback, i)
    below = sum(1 for j in window if closes.iloc[j] < vw.iloc[j])
    above = sum(1 for j in window if closes.iloc[j] > vw.iloc[j])
    vol_before = c.bars["volume"].iloc[max(0, i - int(p["bar_volume_lookback"])) : i]
    if not len(vol_before) or float(c.bars["volume"].iloc[i]) < float(vol_before.mean()):
        return None
    n = int(p["stop_lookback"])
    recent = c.bars.iloc[max(0, i - n + 1) : i + 1]
    need_move, need_bars = float(p["min_move_vs_index_pct"]), int(p["min_bars_below"])
    rv = c.rvol(ts)
    bar = c.bars.iloc[i]
    if (
        move >= need_move
        and vw.iloc[i - 1] > vw.iloc[i - 1 - slope]
        and below >= need_bars
        and closes.iloc[i - 1] < vw.iloc[i - 1]
        and closes.iloc[i] > vw.iloc[i]
    ):
        return Setup(
            c.ticker,
            "vwap_reclaim",
            "buy",
            ts.isoformat(timespec="minutes"),
            float(bar["close"]),
            float(recent["low"].min()),
            rv,
            f"up {move:+.1f}% vs index, VWAP rising, {below} of the last {lookback} "
            f"bars below VWAP, closed back above it at {_px(bar['close'])} "
            f"(VWAP {_px(vw.iloc[i])})",
        )
    if (
        c.shortable
        and move <= -need_move
        and vw.iloc[i - 1] < vw.iloc[i - 1 - slope]
        and above >= need_bars
        and closes.iloc[i - 1] > vw.iloc[i - 1]
        and closes.iloc[i] < vw.iloc[i]
    ):
        return Setup(
            c.ticker,
            "vwap_reclaim",
            "short",
            ts.isoformat(timespec="minutes"),
            float(bar["close"]),
            float(recent["high"].max()),
            rv,
            f"down {move:+.1f}% vs index, VWAP falling, {above} of the last {lookback} "
            f"bars above VWAP, closed back below it at {_px(bar['close'])} "
            f"(VWAP {_px(vw.iloc[i])})",
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
        steps = idx[1:] - idx[:-1]
        for k in (steps >= gap).nonzero()[0]:
            a, b = idx[k], idx[k + 1]
            if not (lo_t <= a.time() < hi_t):
                continue
            n_prior = idx.searchsorted(a, side="right") - idx.searchsorted(
                a - timedelta(minutes=30), side="right"
            )
            if n_prior < float(p["prior_activity_share"]) * 30:
                continue
            # 26 Sep 2026 (review B1): a gap is a halt only if the feed watched those
            # minutes. A hole in the IBKR feed's bars (a stretch it was not streaming or
            # fetching the stock) read as a halt, and a bar after it fired a "resumption"
            # (reproduced: resumed 11:45 after 11:29, SETUP halt_resumption buy 11:55). The
            # halt rule is unchanged; a hole is simply not a halt.
            if not c.watched(a + ONE_MIN, b - ONE_MIN):
                continue
            out.append((b, float(c.bars["close"].iloc[k]), a))
    day_open = datetime.combine(c.day, OPEN, tzinfo=SYD)
    if (
        len(idx)
        and idx[0].time() >= _t(p["late_first_trade"])
        and c.news
        and c.watched(day_open, idx[0] - ONE_MIN)  # a late start, not a late first look
    ):
        out.append((idx[0], c.prev_close, None))
    c._resumed = out
    return out


def halt_resumption(c: Ctx, i: int) -> Setup | None:
    p = c.conf["halt_resumption"]
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
    out = []
    for i, ts in enumerate(c.bars.index):
        if since is not None and ts <= since:
            continue
        if ts.time() > scan_end or (until is not None and ts > until):
            break
        for name, fn in DETECTORS.items():
            # A setup whose block says `enabled: false` is not looked for (Practice Lab
            # variants, 26 Sep 2026; the frozen blocks don't set it, so nothing changes live).
            if (c.conf.get(name) or {}).get("enabled", True) is False:
                continue
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
        steps = idx[1:] - idx[:-1]
        for k in (steps > ONE_MIN).nonzero()[0]:
            a, b = idx[k], idx[k + 1]
            if since is not None and b <= since:
                continue
            if not c.watched(a + ONE_MIN, b - ONE_MIN):
                return a
    return idx[-1]
