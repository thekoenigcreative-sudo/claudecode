"""The live market as the team sees it: the simulator's grid of one-minute slots (lab/tsim/
market.py - 09:59 to 16:11, the opening auction in slot 0, the closing auction at 16:10), filled
from the bars the WATCHER already holds, as they complete.

Why the simulator's grid and not the watcher's MarketView: the team, its tools (scanner,
quotes, charts), its alerts and its broker were built and tested on this grid. Feeding the live
bars into the same arrays keeps everything the team does identical to the simulator, so the
calibration (each live day replayed in the simulator) compares like with like.

Where the bars come from: `feed.bars_today(code, day)` - IBKR's completed minutes that the
watcher's one connection streams or polls for the day trader's universe and the day's news
stocks (docs/ibkr_live_data.md). The team reads them and asks IBKR for nothing: no stream, no
poll, no quote is added, so the frozen books' data is exactly what it would be without it. A
stock without a line is only as fresh as its last poll (about every 6 minutes); `covered` says
how far the feed has watched each stock, and the session holds the broker for the team's own
stocks until their minutes are in (session.py).

What a trader may see follows the simulator's rule: a slot only once it has ended and been
delivered (20 s after it closes), announcements only once released, daily history only up to
the day before (the simulator's summaries, built from the same IBKR history cache, never from
the day being traded).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from asxbot.lab.tsim.market import INDEX, SLOTS, SYD, DayBars, Market, open_offset, to_grid


def empty_bars(code: str) -> DayBars:
    o = np.full(SLOTS, np.nan)
    return DayBars(
        code,
        o,
        o.copy(),
        o.copy(),
        o.copy(),
        np.zeros(SLOTS),
        0 if code == INDEX else open_offset(code),
        None,
        0.0,
    )


class LiveMarket(Market):
    """tsim's Market for today, filled minute by minute from a bar source.

    `source(code, day)` returns the completed minutes held for today (a DataFrame indexed by
    the minute's start, Sydney time) or None; `announcements()` returns the announcement table
    when it has changed since the last call, else None."""

    def __init__(
        self, day: date, codes, summaries, shortable, source, announcements=None, covered=None
    ):
        # Nothing is loaded from history for today: the grid starts empty and fills.
        self.day = day
        self.history = None
        self.summaries = summaries
        self.shortable = {c.upper() for c in (shortable or set())}
        self.bars: dict[str, DayBars] = {}
        self.index: DayBars | None = None
        self.ann = pd.DataFrame(
            columns=["code", "released_at", "headline", "type", "price_sensitive", "ids_id"]
        )
        self.now = datetime.combine(day, time(8, 0), tzinfo=SYD)
        self._prev: dict[str, float | None] = {}
        self._turnover: dict[str, float | None] = {}
        self.source = source
        self._ann_source = announcements
        self._covered = covered
        self._seen: dict[str, tuple] = {}
        self.add_codes(codes)

    def add_codes(self, codes) -> None:
        for c in codes:
            c = str(c).upper()
            if c and c != INDEX and c not in self.bars:
                self.bars[c] = empty_bars(c)

    def ingest(self) -> int:
        """Bring every stock's arrays up to what the source holds; returns how many changed."""
        changed = 0
        for code in [INDEX, *list(self.bars)]:
            try:
                df = self.source(code, self.day)
            except Exception:  # noqa: BLE001 - one stock's bars must never stop the others
                continue
            if df is None or not len(df):
                continue
            key = (len(df), df.index[-1], float(df["volume"].iloc[-1] or 0))
            if self._seen.get(code) == key:
                continue
            g = to_grid(code, self.day, df)
            self._seen[code] = key
            if g is None:
                continue
            if code == INDEX:
                self.index = g
            else:
                self.bars[code] = g
            changed += 1
        if self._ann_source is not None:
            try:
                a = self._ann_source()
            except Exception:  # noqa: BLE001 - an unreadable file is read again next minute
                a = None
            if a is not None:
                self.set_announcements(a)
        return changed

    def set_announcements(self, ann: pd.DataFrame) -> None:
        if not len(ann):
            return
        ann = ann.copy()
        ann["released_at"] = pd.to_datetime(ann["released_at"])
        if ann["released_at"].dt.tz is None:
            ann["released_at"] = ann["released_at"].dt.tz_localize(SYD)
        else:
            ann["released_at"] = ann["released_at"].dt.tz_convert(SYD)
        ann["ids_id"] = ann["ids_id"].astype(str)
        self.ann = ann.sort_values("released_at").reset_index(drop=True)

    def has_minute(self, code: str, slot: int) -> bool:
        """Has the feed delivered `code` through `slot` - a bar at or after it, or a watched
        span covering it (a quiet minute in a watched span is a minute with no trade)?"""
        b = self.bars.get(code)
        if b is not None and (b.v[slot:] > 0).any():
            return True
        if self._covered is None:
            return False
        from asxbot.lab.tsim.market import slot_time

        start = slot_time(self.day, slot)
        try:
            # The feed's check allows a minute's slack at each end; asking to one minute past
            # this one's end means the watched span reached at least its end - a history
            # answer taken while the minute was still forming does not count for it.
            return bool(self._covered(code, start, start + timedelta(seconds=119)))
        except Exception:  # noqa: BLE001
            return False


class AnnouncementFiles:
    """The live collector's files (data/announcements/live/<day>.parquet, written by the
    watcher's poller) with the week before, read again whenever today's file changes."""

    def __init__(self, cfg, day: date):
        self.cfg = cfg
        self.day = day
        self._base: pd.DataFrame | None = None
        self._stamp = None

    def path(self) -> Path:
        return (
            Path(self.cfg.data_dir) / "announcements" / "live" / f"{self.day.isoformat()}.parquet"
        )

    def __call__(self) -> pd.DataFrame | None:
        from asxbot.arena.replay_ibkr import COLS, announcements

        p = self.path()
        stamp = (p.stat().st_mtime, p.stat().st_size) if p.exists() else None
        if self._base is not None and stamp == self._stamp:
            return None
        if self._base is None:
            self._base = announcements(
                self.cfg, self.day - timedelta(days=7), self.day - timedelta(days=1)
            )
        frames = [self._base]
        if p.exists():
            today = pd.read_parquet(p)
            for c in COLS:
                if c not in today.columns:
                    today[c] = None
            frames.append(today[COLS])
        self._stamp = stamp
        df = (
            pd.concat([f for f in frames if len(f)], ignore_index=True)
            if any(len(f) for f in frames)
            else self._base
        )
        df = df.copy()
        df["ids_id"] = df["ids_id"].astype(str)
        return df.drop_duplicates("ids_id", keep="last")


def build_summaries(codes, day: date, history_root: Path | None = None):
    """The simulator's daily summaries (previous close, usual volume, turnover, the closing
    auction's usual size) for `codes`, from the IBKR history cache, days before `day` only.
    Sequential: this runs in the watcher's process (no worker processes there)."""
    from asxbot.arena.replay_ibkr import history_root as hr
    from asxbot.lab.tsim.market import History
    from asxbot.lab.tsim.run import tsim_local
    from asxbot.lab.tsim.summaries import Summaries

    hist = History(Path(history_root or hr()))
    summ = Summaries(tsim_local() / "summary")
    summ.build(hist, sorted({*codes, INDEX}), workers=1, until=day)
    return summ


__all__ = ["AnnouncementFiles", "LiveMarket", "build_summaries", "empty_bars"]
