"""The search's view of the data: whole days of IBKR 1-minute bars as arrays, a per-stock
daily table derived from them, the index, and the announcement archive.

Where the data lives (never in git):
  history   <root>/<CODE>/<YYYY-MM-DD>.parquet, open/high/low/close/volume, Sydney bar-start
            index (scripts/ibkr_fetch_history.py; the caret kept for ^AXJO; PRN as PRN_).
            Found from, in order: --history, ASXBOT_HISTORY_DIR, the lab's own cache
            (%LOCALAPPDATA%\\asx-bot\\ibkr\\history, or ASXBOT_LOCAL_APPDATA on Linux).
  data      the repo's data/ folder (announcements/history, announcements/live, universe/):
            --data, ASXBOT_DATA_DIR, or <repo>/data.
  cache     derived arrays, rebuilt from the history on demand: <data>/search/cache.

The minute grid runs 09:59 to 16:12 Sydney. In the IBKR bars (checked 26 Sep on three days,
~1,000 stock-days) the OPENING AUCTION is in a 09:59 bar (grid index 0): that bar's OPEN equals
Yahoo's official open on 1,174 of 1,174 stock-days checked (12 codes, May-Sep, dividend
adjustment removed), but the bar also has a range in 88% of them - some continuous trades are
folded into it - so the auction price is its open, and its volume is the auction plus a little.
Every opening group's continuous trading starts in the 10:00 bar (index 1) - the ASX's
staggered open is not visible in them. Continuous trading ends with the 16:00 bar (zero volume
in the sample); the CLOSING AUCTION prints in the 16:10 bar. A bar with no volume is no trade:
its prices are NaN in the grid, so nothing can fill on it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

SYD = ZoneInfo("Australia/Sydney")
GRID_START = 599  # 09:59 (the opening auction's bar) in minutes after midnight
GRID_N = 374  # 09:59 .. 16:12
OPEN_AUCTION = 0  # the 09:59 bar
CONT_START = 1  # the 10:00 bar
CONT_END = 361  # the 16:00 bar: continuous trading is over after it
AUCTION_FROM = 362  # bars from 16:01 carry the closing auction (16:10 in the data)
INDEX = "^AXJO"
LOOKBACK = 20  # sessions of daily history for turnover and volatility


def minute_index(t: time) -> int:
    return t.hour * 60 + t.minute - GRID_START


def at(day: date, i: int) -> datetime:
    return datetime.combine(day, time(0), tzinfo=SYD) + timedelta(minutes=GRID_START + i)


def history_dir(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("ASXBOT_HISTORY_DIR")
    if env:
        return Path(env)
    from asxbot.localdir import asx_local

    return asx_local() / "ibkr" / "history"


def data_dir(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit)
    env = os.environ.get("ASXBOT_DATA_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3] / "data"


def code_of(folder: str) -> str:
    return folder[:-1] if folder.endswith("_") else folder


@dataclass
class DayPanel:
    """One session, every stock: arrays of shape (stocks, GRID_N)."""

    day: date
    codes: list[str]
    o: np.ndarray
    h: np.ndarray
    lo: np.ndarray
    c: np.ndarray
    v: np.ndarray
    row: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        self.row = {c: i for i, c in enumerate(self.codes)}


def _read_day(path: Path) -> pd.DataFrame | None:
    try:
        df = pd.read_parquet(path)
    except Exception:  # noqa: BLE001 - an unreadable file is a missing day, and is counted
        return None
    if not len(df):
        return None
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize(SYD) if idx.tz is None else idx.tz_convert(SYD)
    df = df.set_axis(idx)
    return df


def grid_rows(df: pd.DataFrame, is_index: bool = False) -> np.ndarray:
    """(5, GRID_N) float32: open, high, low, close, volume on the minute grid."""
    out = np.full((5, GRID_N), np.nan, dtype=np.float32)
    out[4] = 0.0
    mins = df.index.hour * 60 + df.index.minute - GRID_START
    keep = (mins >= 0) & (mins < GRID_N)
    if not keep.any():
        return out
    sub = df[keep]
    m = np.asarray(mins[keep])
    vol = sub["volume"].to_numpy(dtype=float)
    vol = np.where(np.isfinite(vol), vol, 0.0)
    traded = np.ones(len(sub), bool) if is_index else vol > 0
    for k, col in enumerate(("open", "high", "low", "close")):
        vals = sub[col].to_numpy(dtype=float)
        ok = traded & np.isfinite(vals) & (vals > 0)
        out[k, m[ok]] = vals[ok]
    out[4, m] = np.maximum(vol, 0.0)
    return out


class Market:
    """The data for a run. Build the cache once (`build`), then read panels and the daily
    table from it; nothing here reads a day that was not asked for."""

    def __init__(self, history: str | Path | None = None, data: str | Path | None = None):
        self.history = history_dir(history)
        self.data = data_dir(data)
        self.cache = self.data / "search" / "cache"
        self._daily: pd.DataFrame | None = None
        self._index: pd.DataFrame | None = None
        self._ann: pd.DataFrame | None = None
        self._panels: dict[date, DayPanel] = {}

    # ------------------------------------------------------------------ discovery
    def codes(self) -> list[str]:
        if not self.history.exists():
            return []
        return sorted(
            code_of(p.name)
            for p in self.history.iterdir()
            if p.is_dir() and not p.name.startswith(("^", "_", "."))
        )

    def days_on_disk(self) -> list[date]:
        d = self.history / INDEX
        if not d.exists():
            return []
        out = []
        for p in d.glob("*.parquet"):
            try:
                out.append(date.fromisoformat(p.stem))
            except ValueError:
                continue
        return sorted(out)

    def _folder(self, code: str) -> Path:
        from asxbot.io import safe_stem

        return self.history / safe_stem(code)

    # ------------------------------------------------------------------ building the cache
    def build(self, days: list[date] | None = None, workers: int = 0, progress=None) -> dict:
        """Turn the history into one .npz per day (every stock's grid) plus daily.parquet and
        index.parquet. Returns counts. Days already cached are kept."""
        days = days or self.days_on_disk()
        codes = self.codes()
        self.cache.mkdir(parents=True, exist_ok=True)
        todo = [d for d in days if not (self.cache / f"{d.isoformat()}.npz").exists()]
        jobs = [(str(self.history), str(self.cache), d.isoformat(), codes) for d in todo]
        if workers and workers > 1 and len(jobs) > 1:
            from concurrent.futures import ProcessPoolExecutor

            with ProcessPoolExecutor(workers) as ex:
                for i, _ in enumerate(ex.map(_build_day, jobs)):
                    if progress:
                        progress(i + 1, len(jobs))
        else:
            for i, j in enumerate(jobs):
                _build_day(j)
                if progress:
                    progress(i + 1, len(jobs))
        self._daily = None
        daily = self._build_daily(days)
        return {"days": len(days), "built": len(todo), "codes": len(codes), "rows": len(daily)}

    def _build_daily(self, days: list[date]) -> pd.DataFrame:
        rows, idx_rows = [], []
        for d in days:
            p = self.panel(d)
            if p is None:
                continue
            rows.append(summarise(p))
            ir = self._index_row(d)
            if ir is not None:
                idx_rows.append(ir)
        daily = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
        if len(daily):
            daily = add_history_features(daily)
        from asxbot.io import write_parquet_atomic

        write_parquet_atomic(daily, self.cache / "daily.parquet")
        index = pd.DataFrame(idx_rows).set_index("day") if idx_rows else pd.DataFrame()
        if len(index):
            index = index.sort_index()
            index["prev_close"] = index["close"].shift(1)
            index["ret"] = index["close"] / index["prev_close"] - 1
            index["ma10"] = index["close"].rolling(10, min_periods=10).mean().shift(1)
            index["ma20"] = index["close"].rolling(20, min_periods=20).mean().shift(1)
            index["prev_close_above_ma20"] = index["prev_close"] > index["ma20"]
        write_parquet_atomic(index, self.cache / "index.parquet")
        self._daily, self._index = daily, index
        return daily

    def _index_row(self, d: date) -> dict | None:
        f = self.history / INDEX / f"{d.isoformat()}.parquet"
        df = _read_day(f) if f.exists() else None
        if df is None:
            return None
        g = grid_rows(df, is_index=True)
        c = g[3]
        ok = np.where(np.isfinite(c))[0]
        ok = ok[ok >= CONT_START]  # the index's 09:59 value is a pre-open calculation
        if not len(ok):
            return None
        first = ok[0]
        cont = ok[ok <= CONT_END]
        i30 = minute_index(time(10, 30))
        at30 = cont[cont <= i30]
        return {
            "day": pd.Timestamp(d),
            "open": float(g[0, first]),
            "close": float(c[ok[-1]]),
            "px_1030": float(c[at30[-1]]) if len(at30) else np.nan,
            "grid_close": c.astype(float),
        }

    # ------------------------------------------------------------------ reading
    def panel(self, d: date) -> DayPanel | None:
        if d in self._panels:
            return self._panels[d]
        f = self.cache / f"{d.isoformat()}.npz"
        if not f.exists():
            return None
        z = np.load(f, allow_pickle=False)
        codes = [str(x) for x in z["codes"]]
        a = z["a"]
        p = DayPanel(d, codes, a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4])
        if len(self._panels) > 6:
            self._panels.pop(next(iter(self._panels)))
        self._panels[d] = p
        return p

    @property
    def daily(self) -> pd.DataFrame:
        if self._daily is None:
            f = self.cache / "daily.parquet"
            self._daily = pd.read_parquet(f) if f.exists() else pd.DataFrame()
        return self._daily

    @property
    def index(self) -> pd.DataFrame:
        if self._index is None:
            f = self.cache / "index.parquet"
            self._index = pd.read_parquet(f) if f.exists() else pd.DataFrame()
        return self._index

    def index_close_grid(self, d: date) -> np.ndarray | None:
        ix = self.index
        ts = pd.Timestamp(d)
        if not len(ix) or ts not in ix.index:
            return None
        return np.asarray(ix.at[ts, "grid_close"], dtype=float)

    def market_move_pct(self, d: date) -> float | None:
        ix = self.index
        ts = pd.Timestamp(d)
        if not len(ix) or ts not in ix.index or not np.isfinite(ix.at[ts, "ret"]):
            return None
        return round(float(ix.at[ts, "ret"]) * 100, 3)

    def announcements(self, first: date, last: date) -> pd.DataFrame:
        """Every announcement released from `first` to `last` (inclusive), with the repo's
        headline type and the search's direction words (search/news.py)."""
        if self._ann is None:
            from types import SimpleNamespace

            from asxbot.arena.replay_ibkr import announcements

            ann = announcements(SimpleNamespace(data_dir=self.data), date(2000, 1, 1),
                                date(2100, 1, 1))  # fmt: skip
            from asxbot.search.news import enrich

            self._ann = enrich(ann)
        a = self._ann
        if not len(a):
            return a
        lo, hi = pd.Timestamp(first), pd.Timestamp(last) + pd.Timedelta(days=1)
        return a[(a["released_at"] >= lo) & (a["released_at"] < hi)]

    def shortable(self) -> set[str]:
        """The dated ASX 200 list on disk (data/universe), else nothing: long only."""
        u = self.data / "universe"
        for name in ("asx200_manual.csv", "asx200_members.csv"):
            f = u / name
            if f.exists():
                return set(pd.read_csv(f)["code"].astype(str).str.strip().str.upper())
        return set()


def _build_day(job) -> None:
    history, cache, day, codes = job
    history, cache = Path(history), Path(cache)
    from asxbot.io import safe_stem

    got_codes, arrays = [], []
    for code in codes:
        f = history / safe_stem(code) / f"{day}.parquet"
        if not f.exists():
            continue
        df = _read_day(f)
        if df is None:
            continue
        g = grid_rows(df)
        if not np.isfinite(g[3]).any():
            continue
        got_codes.append(code)
        arrays.append(g)
    a = np.stack(arrays) if arrays else np.zeros((0, 5, GRID_N), np.float32)
    tmp = cache / f"{day}.tmp.npz"
    np.savez_compressed(tmp, a=a, codes=np.array(got_codes, dtype=str))
    os.replace(tmp, cache / f"{day}.npz")


def summarise(p: DayPanel) -> pd.DataFrame:
    """Per stock for one session: the opening auction (the 09:59 bar's close and volume), the
    first continuous trade, the open (the auction's price, else the first continuous trade),
    the close (the closing auction's print, else the last traded minute), the day's range and
    volume, the closing auction's volume, and the volume in the first 5/10/15/30 minutes of
    the stock's own continuous trading, after its auction."""
    n = len(p.codes)
    traded = p.v > 0
    cont = traded[:, CONT_START:AUCTION_FROM]
    anyt = cont.any(axis=1)
    first = np.where(anyt, cont.argmax(axis=1) + CONT_START, -1)
    rows = np.arange(n)
    first_open = np.where(first >= 0, p.o[rows, np.maximum(first, 0)], np.nan)
    auc_open_vol = p.v[:, OPEN_AUCTION].astype(float)
    auc_open_px = np.where(auc_open_vol > 0, p.o[:, OPEN_AUCTION], np.nan)
    open_ = np.where(np.isfinite(auc_open_px), auc_open_px, first_open)
    auc_v = p.v[:, AUCTION_FROM:].sum(axis=1)
    last_idx = np.where(traded.any(axis=1), GRID_N - 1 - traded[:, ::-1].argmax(axis=1), -1)
    close = np.where(last_idx >= 0, p.c[rows, np.maximum(last_idx, 0)], np.nan)
    cont_traded = traded[:, : CONT_END + 1]
    last_cont = np.where(cont_traded.any(axis=1),
                         CONT_END - cont_traded[:, ::-1].argmax(axis=1), -1)  # fmt: skip
    last_cont_px = np.where(last_cont >= 0, p.c[rows, np.maximum(last_cont, 0)], np.nan)
    with np.errstate(all="ignore"):
        high = np.nanmax(np.where(traded, p.h, np.nan), axis=1)
        low = np.nanmin(np.where(traded, p.lo, np.nan), axis=1)
    vol = p.v.sum(axis=1)
    turnover = np.nansum(np.where(traded, p.c * p.v, 0.0), axis=1)
    out = {
        "day": pd.Timestamp(p.day),
        "code": p.codes,
        "first_idx": first,
        "first_open": first_open,
        "auc_open_px": auc_open_px,
        "auc_open_vol": auc_open_vol,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "last_cont_close": last_cont_px,
        "volume": vol,
        "turnover": turnover,
        "auction_vol": auc_v,
        "has_auction": auc_v > 0,
    }
    for k in (5, 10, 15, 30):
        s = np.zeros(n)
        for i in range(n):
            if first[i] >= 0:
                s[i] = p.v[i, first[i] : first[i] + k].sum()
        out[f"ow{k}"] = s
    last30 = minute_index(time(15, 30))
    out["vol_last30"] = p.v[:, last30 : CONT_END + 1].sum(axis=1)
    df = pd.DataFrame(out)
    num = df.select_dtypes("float32").columns
    df[num] = df[num].astype("float64")  # the grids are float32; the table is not
    return df


def add_history_features(daily: pd.DataFrame) -> pd.DataFrame:
    """Features that use ONLY sessions before the row's day (every rolling window is shifted
    one session): previous close, 20-session median turnover and volatility, 14-session
    average opening-window volume, median closing-auction volume."""
    d = daily.sort_values(["code", "day"]).reset_index(drop=True)
    g = d.groupby("code", sort=False)
    d["prev_close"] = g["close"].shift(1)
    d["prev_day"] = g["day"].shift(1)
    d["ret"] = d["close"] / d["prev_close"] - 1
    d["gap"] = d["open"] / d["prev_close"] - 1
    d["intraday"] = d["close"] / d["open"] - 1

    def roll(col, n, how, minp):
        r = g[col].shift(1).groupby(d["code"], sort=False).rolling(n, min_periods=minp)
        r = getattr(r, how)()
        return r.reset_index(level=0, drop=True)

    d["adv_turnover"] = roll("turnover", LOOKBACK, "median", 10)
    d["vol20"] = roll("ret", LOOKBACK, "std", 10)
    for k in (5, 10, 15, 30):
        d[f"ow{k}_avg14"] = roll(f"ow{k}", 14, "mean", 7)
    d["auction_vol_med14"] = roll("auction_vol", 14, "median", 7)
    d["vol_med20"] = roll("volume", LOOKBACK, "median", 10)
    d["high20"] = roll("high", 20, "max", 20)
    d["low20"] = roll("low", 20, "min", 20)
    d["ret5_prior"] = g["close"].shift(1) / g["close"].shift(6) - 1
    return d


__all__ = ["AUCTION_FROM", "CONT_END", "CONT_START", "OPEN_AUCTION", "DayPanel", "GRID_N",
           "INDEX", "Market", "at",
           "grid_rows", "minute_index", "summarise"]  # fmt: skip
