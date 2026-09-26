"""The market, minute by minute, from the stored IBKR 1-minute history.

One trading day is a grid of 373 one-minute slots, 09:59 to 16:11 Sydney (slot i starts at
09:59 + i minutes; slot 0 is the opening auction's bar). Every stock in the universe and the
ASX 200 index (^AXJO) are loaded onto
it as arrays, so the engine and the scanner work in plain numpy, not per-bar pandas.

What a trader may see at the simulated time `now` (the rule the whole simulator keeps):
  * a bar only once it is complete: slot i is visible when 10:00 + (i+1) min + FEED_LAG <= now
    (FEED_LAG: the live IBKR feed hands over a minute ~20 s after it closes, as the lab's
    time machine assumes);
  * announcements only once released (released_at <= now);
  * daily history only up to the day before (summaries.py, built from the same history).

The opening auction: IBKR's history stamps every stock's opening auction as a 09:59 bar (checked
26 Sep on the cloud copy: BHP, CBA, FMG, NAB, WBC, S32, ZIP, PLS, MIN, A2M on 24 Sep all have a
09:59 bar with the auction's volume, S-Z included - the ASX's staggered open, A-B 10:00 to S-Z
~10:09, does not show in the stamps, TRACKER #33). So slot 0 (09:59) is the auction: its price
is the bar's open and its volume the auction's. A stock with no 09:59 trade opens at its first
traded bar at or after its group's minute. The closing auction is the 16:10 bar (slot 371).
Both are ASSUMPTIONS about IBKR's bars, checked by the fidelity run before results are trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

SYD = ZoneInfo("Australia/Sydney")
GRID_START = time(9, 59)
SLOTS = 373  # 09:59 (the opening auction) .. 16:11
CLOSE_AUCTION_SLOT = 371  # 16:10
CONTINUOUS_END_SLOT = 361  # 16:00: continuous trading ends; the pre-close follows
FEED_LAG = timedelta(seconds=20)
INDEX = "^AXJO"
# The ASX's staggered open: (first character up to, minute offset from 10:00)
OPEN_GROUPS = (("B", 0), ("F", 2), ("M", 4), ("R", 6), ("Z", 9))


def slot_time(day: date, slot: int) -> datetime:
    return datetime.combine(day, GRID_START, tzinfo=SYD) + timedelta(minutes=int(slot))


def slot_of(ts: datetime) -> int:
    """The slot a moment falls in (may be < 0 before 10:00 or >= SLOTS after 16:11)."""
    ts = ts.astimezone(SYD)
    start = datetime.combine(ts.date(), GRID_START, tzinfo=SYD)
    return int((ts - start).total_seconds() // 60)


def visible_slots(day: date, now: datetime) -> int:
    """How many slots of `day` are complete and delivered at `now` (0..SLOTS)."""
    start = datetime.combine(day, GRID_START, tzinfo=SYD)
    n = int(((now.astimezone(SYD) - FEED_LAG) - start).total_seconds() // 60)
    return max(0, min(SLOTS, n))


def open_offset(code: str) -> int:
    """The slot of the stock's group open when it has no 09:59 auction bar (slot 1 = 10:00)."""
    c = (code or "A").upper()[0]
    if not c.isalpha():
        return 1
    for last, off in OPEN_GROUPS:
        if c <= last:
            return 1 + off
    return 10


@dataclass
class DayBars:
    """One stock's day on the grid. NaN price / 0 volume where it did not trade."""

    code: str
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    v: np.ndarray
    open_slot: int  # the auction's slot: the first traded bar at/after its group's minute
    auction_price: float | None
    auction_volume: float

    @property
    def traded(self) -> np.ndarray:
        return self.v > 0

    def last_close(self, upto: int) -> float | None:
        """The last traded close in slots [0, upto)."""
        if upto <= 0:
            return None
        idx = np.flatnonzero(self.v[:upto] > 0)
        return None if not len(idx) else float(self.c[idx[-1]])

    def frame(self, day: date, upto: int) -> pd.DataFrame:
        idx = [slot_time(day, i) for i in range(upto)]
        df = pd.DataFrame(
            {"open": self.o[:upto], "high": self.h[:upto], "low": self.l[:upto],
             "close": self.c[:upto], "volume": self.v[:upto]}, index=idx)  # fmt: skip
        return df[df["volume"] > 0]


def to_grid(code: str, day: date, df: pd.DataFrame | None) -> DayBars | None:
    if df is None or not len(df):
        return None
    idx = df.index
    if idx.tz is None:
        idx = idx.tz_localize(SYD)
    else:
        idx = idx.tz_convert(SYD)
    start = pd.Timestamp(datetime.combine(day, GRID_START, tzinfo=SYD))
    slots = ((idx - start).total_seconds() // 60).astype(int)
    keep = (slots >= 0) & (slots < SLOTS)
    if not keep.any():
        return None
    slots = np.asarray(slots[keep])
    sub = df[keep]
    o = np.full(SLOTS, np.nan)
    h, l, c = o.copy(), o.copy(), o.copy()  # noqa: E741
    v = np.zeros(SLOTS)
    vol = np.nan_to_num(sub["volume"].to_numpy(dtype=float), nan=0.0)
    o[slots] = sub["open"].to_numpy(dtype=float)
    h[slots] = sub["high"].to_numpy(dtype=float)
    l[slots] = sub["low"].to_numpy(dtype=float)
    c[slots] = sub["close"].to_numpy(dtype=float)
    v[slots] = vol
    # A bar with no volume is not a trade (IBKR's placeholder rows): price blanked.
    dead = v <= 0
    o[dead] = h[dead] = l[dead] = c[dead] = np.nan
    first = 0 if v[0] > 0 else open_offset(code)
    traded = np.flatnonzero(
        (v > 0) & (np.arange(SLOTS) >= first) & (np.arange(SLOTS) < CONTINUOUS_END_SLOT)
    )
    if len(traded):
        s = int(traded[0])
        ap, av = float(o[s]), float(v[s])
    else:
        s, ap, av = first, None, 0.0
    return DayBars(code, o, h, l, c, v, s, ap, av)


class History:
    """Read-only access to <root>/<CODE>/<YYYY-MM-DD>.parquet (the live history cache's layout;
    ASXBOT_IBKR_HISTORY points at a copy). Nothing is fetched or written."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, code: str, day: date) -> Path:
        from asxbot.io import safe_stem

        return self.root / safe_stem(code.upper()) / f"{day.isoformat()}.parquet"

    def codes(self) -> list[str]:
        if not self.root.exists():
            return []
        out = []
        for p in self.root.iterdir():
            if p.is_dir():
                name = p.name[:-1] if p.name.endswith("_") and len(p.name) == 4 else p.name
                out.append(name.upper())
        return sorted(out)

    def days(self, code: str) -> list[date]:
        d = self.path(code, date(2000, 1, 1)).parent
        out = []
        for p in d.glob("*.parquet") if d.exists() else []:
            try:
                out.append(date.fromisoformat(p.stem))
            except ValueError:
                continue
        return sorted(out)

    def load(self, code: str, day: date) -> pd.DataFrame | None:
        p = self.path(code, day)
        if not p.exists():
            return None
        try:
            return pd.read_parquet(p)
        except Exception:  # noqa: BLE001 - a broken file is a missing day, reported by coverage
            return None


class Market:
    """One simulated day of the whole universe."""

    def __init__(
        self,
        day: date,
        history: History,
        codes: list[str],
        announcements: pd.DataFrame | None = None,
        summaries=None,
        shortable: set[str] | None = None,
    ):
        from asxbot.lab.tsim.summaries import Summaries

        self.day = day
        self.history = history
        self.summaries: Summaries | None = summaries
        self.shortable = {c.upper() for c in (shortable or set())}
        self.bars: dict[str, DayBars] = {}
        for c in codes:
            g = to_grid(c, day, history.load(c, day))
            if g is not None:
                self.bars[c.upper()] = g
        self.index = to_grid(INDEX, day, history.load(INDEX, day))
        self.bars.pop(INDEX, None)
        ann = announcements if announcements is not None else pd.DataFrame()
        if len(ann):
            ann = ann.copy()
            ann["released_at"] = pd.to_datetime(ann["released_at"])
            if ann["released_at"].dt.tz is None:
                ann["released_at"] = ann["released_at"].dt.tz_localize(SYD)
            ann = ann.sort_values("released_at").reset_index(drop=True)
        self.ann = ann
        self.now = datetime.combine(day, time(8, 0), tzinfo=SYD)
        self._prev: dict[str, float | None] = {}
        self._turnover: dict[str, float | None] = {}

    # -- the clock ---------------------------------------------------------
    @property
    def n_visible(self) -> int:
        return visible_slots(self.day, self.now)

    def codes(self) -> list[str]:
        return sorted(self.bars)

    # -- prices ------------------------------------------------------------
    def prev_close(self, code: str) -> float | None:
        code = code.upper()
        if code not in self._prev:
            s = self.summaries.before(code, self.day, 1) if self.summaries else None
            self._prev[code] = None if s is None or not len(s) else float(s["close"].iloc[-1])
        return self._prev[code]

    def last(self, code: str) -> float | None:
        """The last traded price a trader can see now (the previous close before the open)."""
        b = self.bars.get(code.upper())
        px = b.last_close(self.n_visible) if b is not None else None
        return px if px is not None else self.prev_close(code)

    def turnover(self, code: str) -> float | None:
        """Median daily dollar turnover over the 20 sessions before the day."""
        code = code.upper()
        if code not in self._turnover:
            s = self.summaries.before(code, self.day, 20) if self.summaries is not None else None
            self._turnover[code] = None if s is None or not len(s) else float(
                s["turnover"].median())  # fmt: skip
        return self._turnover[code]

    def visible_announcements(self, since: datetime | None = None) -> pd.DataFrame:
        if not len(self.ann):
            return self.ann
        m = self.ann["released_at"] <= pd.Timestamp(self.now)
        if since is not None:
            m &= self.ann["released_at"] > pd.Timestamp(since)
        return self.ann[m]

    def index_move_pct(self) -> float | None:
        """The index's move today, as far as can be seen."""
        if self.index is None:
            return None
        last = self.index.last_close(self.n_visible)
        prev = self.prev_close(INDEX)
        if last is None or not prev:
            return None
        return (last / prev - 1) * 100
