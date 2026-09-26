"""Daily summaries built once from the 1-minute history, so no day has to re-read two weeks of
minute files to know a stock's usual volume or yesterday's close.

One parquet per code under <lab_local>/tsim/summary/<CODE>.parquet, one row per day:
  open (the auction price), high, low, close (the 16:10 closing-auction print, else the last
  trade), volume, turnover (close x volume), vwap, auction_volume, close_auction_volume,
  ow5/ow10/ow15 (volume in the first 5/10/15 minutes AFTER the stock's own opening auction,
  the auction bar left out), cv<k> (cumulative volume through slot k, every 15 minutes, for
  relative volume at any time of day).
A day already summarised is never re-read; a new history day is added on the next build.
Daily history shown to a trader comes from here and always ends the day before (`before`).
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from asxbot.lab.tsim.market import CLOSE_AUCTION_SLOT, SLOTS, History, to_grid

CHECKPOINTS = tuple(range(15, 361, 15)) + (SLOTS,)
OPEN_WINDOWS = (5, 10, 15)


def summarise(code: str, day: date, df: pd.DataFrame | None) -> dict | None:
    g = to_grid(code, day, df)
    if g is None or not g.traded.any():
        return None
    v = g.v
    tr = g.traded
    close = float(g.c[CLOSE_AUCTION_SLOT]) if v[CLOSE_AUCTION_SLOT] > 0 else float(g.c[tr][-1])
    vol = float(v.sum())
    typical = np.where(tr, (np.nan_to_num(g.h) + np.nan_to_num(g.l) + np.nan_to_num(g.c)) / 3, 0)
    row = {
        "day": day.isoformat(),
        "open": g.auction_price if g.auction_price is not None else float(g.o[tr][0]),
        "high": float(np.nanmax(g.h)),
        "low": float(np.nanmin(g.l)),
        "close": close,
        "volume": vol,
        "turnover": close * vol,
        "vwap": float((typical * v).sum() / vol) if vol > 0 else close,
        "auction_volume": float(g.auction_volume),
        "close_auction_volume": float(v[CLOSE_AUCTION_SLOT]),
    }
    for w in OPEN_WINDOWS:
        row[f"ow{w}"] = float(v[g.open_slot + 1 : g.open_slot + 1 + w].sum())
    cum = np.cumsum(v)
    for k in CHECKPOINTS:
        row[f"cv{k}"] = float(cum[k - 1])
    return row


def _build_code(args) -> tuple[str, int]:
    root, out_dir, code = args
    hist = History(Path(root))
    p = Path(out_dir) / f"{code.replace('^', '_')}.parquet"
    old = pd.read_parquet(p) if p.exists() else None
    have = set(old["day"]) if old is not None else set()
    rows = []
    for d in hist.days(code):
        if d.isoformat() in have:
            continue
        r = summarise(code, d, hist.load(code, d))
        if r is not None:
            rows.append(r)
    if not rows:
        return code, 0
    new = pd.DataFrame(rows)
    df = new if old is None else pd.concat([old, new], ignore_index=True)
    df = df.drop_duplicates("day", keep="last").sort_values("day").reset_index(drop=True)
    from asxbot.io import write_parquet_atomic

    p.parent.mkdir(parents=True, exist_ok=True)
    write_parquet_atomic(df, p)
    return code, len(rows)


class Summaries:
    def __init__(self, out_dir: Path):
        self.dir = Path(out_dir)
        self._mem: dict[str, pd.DataFrame | None] = {}

    def path(self, code: str) -> Path:
        return self.dir / f"{code.upper().replace('^', '_')}.parquet"

    def build(self, history: History, codes: list[str], workers: int = 4) -> int:
        jobs = [(str(history.root), str(self.dir), c) for c in codes]
        self.dir.mkdir(parents=True, exist_ok=True)
        if workers <= 1:
            done = [_build_code(j) for j in jobs]
        else:
            with ProcessPoolExecutor(workers) as ex:
                done = list(ex.map(_build_code, jobs, chunksize=8))
        self._mem.clear()
        return sum(n for _, n in done)

    def table(self, code: str) -> pd.DataFrame | None:
        code = code.upper()
        if code not in self._mem:
            p = self.path(code)
            self._mem[code] = pd.read_parquet(p) if p.exists() else None
        return self._mem[code]

    def before(self, code: str, day: date, n: int) -> pd.DataFrame | None:
        """The last `n` summarised days strictly before `day` (never the day itself)."""
        t = self.table(code)
        if t is None:
            return None
        return t[t["day"] < day.isoformat()].tail(n)

    def usual_cum_volume(self, code: str, day: date, slot: int, n: int = 14) -> float | None:
        """The average cumulative volume through `slot` over the last n days (interpolated
        between the 15-minute checkpoints)."""
        s = self.before(code, day, n)
        if s is None or len(s) < max(3, n // 3):
            return None
        pts = [0, *CHECKPOINTS]
        vals = [0.0, *[float(s[f"cv{k}"].mean()) for k in CHECKPOINTS]]
        return float(np.interp(slot, pts, vals))

    def usual_open_window(self, code: str, day: date, w: int, n: int = 14) -> float | None:
        s = self.before(code, day, n)
        if s is None or len(s) < max(3, n // 3) or f"ow{w}" not in s:
            return None
        return float(s[f"ow{w}"].mean())
