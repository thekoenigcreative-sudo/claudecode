"""Intraday 1-minute bars for many stocks at once: the data layer behind the day trader and
announcements v2's reaction look.

One interface, three feeds. `data.live_provider` in config.yaml picks the live one:

  * yfinance - Yahoo's 1-minute bars, ~20 minutes behind the ASX (YahooDelayedFeed).
  * ibkr     - IBKR's real-time bars through IB Gateway, falling back to Yahoo by itself
               whenever Gateway is down, cut off from IBKR, or sending delayed data
               (ibkr/feed.py: FailoverFeed). Switching is that one line.
  * replay   - bars already on disk, shown as a delayed feed would have shown them at a
               given moment. Only the plumbing replay uses it (never chosen by config).

Whatever the feed, a decision may only use bars that were final when it was made (no
peeking): a bar that traded, has ended, and is not the newest row of an intraday fetch
(Yahoo rewrites the minute still forming; minutes.final_bars, TRACKER #26). Bars live in the
same cache the broker fills from (data/arena/minutes/<code>/<day>.parquet), so the scanner
and the broker see the same market.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from datetime import time as time_cls
from zoneinfo import ZoneInfo

import pandas as pd

from asxbot.arena.minutes import MINUTE_HISTORY_DAYS, MinuteBars
from asxbot.io import write_parquet_atomic
from asxbot.log import EventLog, get_logger

log = get_logger("asxbot.arena.intraday")
SYD = ZoneInfo("Australia/Sydney")

SESSION_OPEN = time_cls(10, 0)
# Volume is counted from the 10:01 bar. Yahoo's 10:00 bar sometimes holds the opening
# auction's volume and sometimes none, stock-day by stock-day (17-24 Sep 2026, first 300
# cached codes: 171 "huge" first bars - over 10x the next ten bars' median, a median 3.9% of
# the day's volume - against 892 with none), so counting it would make "usual volume"
# depend on which days Yahoo happened to include the auction. Found before any v2 or
# day-trader rule had run; dated in config.yaml.
VOLUME_FROM = time_cls(10, 1)
CONTINUOUS_END = time_cls(16, 0)  # the closing auction (16:10) is not continuous trading
LIVE_PROVIDERS = ("ibkr", "yfinance")
DELAYED_LABEL = "delayed data - rehearsal until IBKR live prices"


class FeedNotConnected(RuntimeError):
    """The feed cannot be used right now (IB Gateway down or not reaching IBKR)."""


class FeedRefused(RuntimeError):
    """The data source refused us (rate limit): back off, never guess."""


def symbol(code: str) -> str:
    return code if code.startswith("^") else f"{code.upper()}.AX"


# --------------------------------------------------------------------------
# what a decision may see
# --------------------------------------------------------------------------
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


# --------------------------------------------------------------------------
# sessions, previous close, usual volume
# --------------------------------------------------------------------------
def prior_sessions(day: date, n: int) -> list[date]:
    """The n ASX sessions before `day`, most recent first."""
    from asxbot.announcements.live import is_trading_day

    out, d = [], day
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5 and is_trading_day(d):
            out.append(d)
        if (day - d).days > 40:
            break
    return out


def previous_close(minutes: MinuteBars, code: str, day: date, fetch: bool = False) -> float | None:
    """The previous session's last traded minute: its 16:10 closing-auction print, which
    equals Yahoo's daily close on 348 of 348 days checked (reports/auction_open_check.md)."""
    for prev in prior_sessions(day, 3):
        df = minutes.fetch(code, prev) if fetch else minutes.cached(code, prev)
        if df is None or not len(df):
            continue
        traded = df[df["volume"] > 0] if not code.startswith("^") else df
        traded = traded[traded["close"] > 0]
        if len(traded):
            return float(traded["close"].iloc[-1])
    return None


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


# --------------------------------------------------------------------------
# the feeds
# --------------------------------------------------------------------------
class IntradayFeed(ABC):
    name = "base"
    delayed = True

    def __init__(self, minutes: MinuteBars):
        self.minutes = minutes

    @property
    def label(self) -> str:
        return DELAYED_LABEL if self.delayed else "live data (IBKR)"

    @abstractmethod
    def refresh(self, codes: list[str], now: datetime) -> list[str]:
        """Bring today's bars up to date for as many of `codes` as this cycle allows,
        stalest first. Returns the codes refreshed."""

    @abstractmethod
    def bars(self, code: str, day: date, now: datetime) -> pd.DataFrame:
        """The day's bars a decision at `now` may use: traded, and final by then."""

    def history_source(self):
        """Where prior sessions come from (anything with `.cached(code, day)`): the minute
        cache, unless the feed keeps its own (IBKR's, so volumes are compared like for like)."""
        return self.minutes

    def ensure_history(self, code: str, day: date, sessions: int) -> bool:
        """Fetch one stock's prior sessions from the feed itself. False: not handled here,
        and the caller backfills the minute cache from Yahoo as before."""
        return False

    def prepare(self, codes: list[str], day: date, sessions: int) -> int:
        """Fetch prior sessions ahead of the open, for feeds that keep their own."""
        return 0


class YahooDelayedFeed(IntradayFeed):
    """Yahoo's delayed 1-minute bars, fetched in batches and kept IN MEMORY for today.

    Today's bars are not written to the minute cache as they arrive: the cache is inside the
    Google Drive folder, and ~90 files a minute rewritten all session is exactly the kind of
    load that cut off the watcher's log handles on 24 Sep (TRACKER #35). Whole sessions are
    written once, after the close (daytrader.end_of_day). The broker keeps fetching and
    caching the few stocks it holds orders in, as before.
    """

    name = "yahoo_delayed"
    delayed = True

    def __init__(self, minutes: MinuteBars, max_requests_per_cycle: int = 90, events=None):
        super().__init__(minutes)
        self.budget = max(1, int(max_requests_per_cycle))
        self.last_refresh: dict[str, datetime] = {}
        self.frames: dict[tuple[str, date], pd.DataFrame] = {}
        self.events = events

    def _refused(self, e: Exception) -> None:
        self.budget = max(10, self.budget // 2)
        log.error("Yahoo refused a batch (%s); %d requests a cycle from now on", e, self.budget)
        if self.events is not None:
            self.events.append(
                "intraday_feed", {"event": "refused", "why": str(e), "budget": self.budget}
            )

    def _keep(self, got: dict, day: date) -> None:
        for code, df in got.items():
            part = df[[d == day for d in df.index.date]]
            if len(part):
                self.frames[(code, day)] = part

    def refresh(self, codes: list[str], now: datetime) -> list[str]:
        now = now.astimezone(SYD)
        day = now.date()
        epoch = datetime(2000, 1, 1, tzinfo=SYD)
        chosen = sorted(codes, key=lambda c: self.last_refresh.get(c, epoch))[: self.budget]
        if not chosen:
            return []
        try:
            got = fetch_batch(chosen, day, day + timedelta(days=1))
        except FeedRefused as e:
            self._refused(e)
            return []
        for code in chosen:
            self.last_refresh[code] = now
        self._keep(got, day)
        if len(chosen) >= 10 and len(got) < 0.2 * len(chosen):
            # yf.download does not raise when Yahoo refuses: it prints "Failed download" per
            # stock and returns nothing for it. Only called in market hours, when every
            # liquid stock has bars, so a mostly empty batch is a refusal.
            self._refused(RuntimeError(f"a batch of {len(chosen)} returned {len(got)}"))
        return chosen

    def fetch_one(self, code: str, now: datetime) -> None:
        """One stock (or the index) now, outside the batch."""
        day = now.astimezone(SYD).date()
        try:
            got = fetch_batch([code], day, day + timedelta(days=1))
        except FeedRefused as e:
            self._refused(e)
            return
        self.last_refresh[code] = now
        self._keep(got, day)

    def bars(self, code: str, day: date, now: datetime) -> pd.DataFrame:
        df = self.frames.get((code, day))
        if df is None:
            df = self.minutes.cached(code, day)
        complete = df is not None and len(df) and df.index.max().time() >= time_cls(16, 10)
        return visible(df, now, None, day_complete=bool(complete), index=code.startswith("^"))


class ReplayFeed(IntradayFeed):
    """Bars on disk, shown as a feed `delay_minutes` behind would have shown them at `now`.
    For the plumbing replay only; never fetches."""

    name = "replay"

    def __init__(self, minutes: MinuteBars, delay_minutes: int = 20):
        super().__init__(minutes)
        self.delay = int(delay_minutes)
        self.delayed = self.delay > 0
        self._cache: dict[tuple[str, date], pd.DataFrame | None] = {}

    def refresh(self, codes: list[str], now: datetime) -> list[str]:
        return list(codes)

    def full(self, code: str, day: date) -> pd.DataFrame | None:
        key = (code, day)
        if key not in self._cache:
            self._cache[key] = self.minutes.cached(code, day)
        return self._cache[key]

    def bars(self, code: str, day: date, now: datetime) -> pd.DataFrame:
        return visible(self.full(code, day), now, self.delay, index=code.startswith("^"))


def live_provider(cfg) -> str:
    """`data.live_provider`: ibkr | yfinance."""
    conf = cfg.get("arena.intraday_data") or {}
    if "provider" in conf:
        raise ValueError(
            "arena.intraday_data.provider was replaced on 2026-09-24 by data.live_provider "
            "(ibkr | yfinance); remove it"
        )
    provider = str(cfg.get("data.live_provider", "yfinance"))
    if provider not in LIVE_PROVIDERS:
        raise ValueError(f"data.live_provider must be one of {LIVE_PROVIDERS}, got {provider!r}")
    return provider


def make_feed(cfg, minutes: MinuteBars, events=None) -> IntradayFeed:
    """The feed config.yaml names. One line (`data.live_provider`) switches Yahoo for IBKR."""
    conf = cfg.get("arena.intraday_data") or {}
    provider = live_provider(cfg)
    events = events if events is not None else EventLog(cfg.data_dir)
    yahoo = YahooDelayedFeed(minutes, int(conf.get("max_requests_per_cycle", 90)), events)
    if provider == "yfinance":
        return yahoo
    from asxbot.ibkr.feed import build_failover

    return build_failover(cfg, minutes, yahoo, events)


# --------------------------------------------------------------------------
# one day's market, as a decision may see it
# --------------------------------------------------------------------------
class MarketView:
    """Today's bars, previous closes and usual volumes, for the scanner, the reaction look
    and both rule bots - one object, so they all see the same market.

    Live (Yahoo's feed, or IBKR's with its Yahoo fallback): `bars` refetches a stock at most every
    `refetch_s` seconds unless told not to fetch (the scanner refreshes its universe itself,
    in budgeted batches). Replay (a ReplayFeed): never fetches; bars are shown as the feed
    would have shown them at `now`.
    """

    def __init__(
        self,
        minutes: MinuteBars,
        day: date,
        feed: IntradayFeed,
        index: str = "^AXJO",
        sessions: int = 5,
        min_sessions: int = 3,
        refetch_s: float = 50.0,
    ):
        self.minutes = minutes
        self.day = day
        self.feed = feed
        self.index = index
        self.sessions = int(sessions)
        self.min_sessions = int(min_sessions)
        self.refetch_s = float(refetch_s)
        self.replay = isinstance(feed, ReplayFeed)
        self._fetched: dict[str, datetime] = {}
        self._prev: dict[str, float | None] = {}
        self._usual: dict[str, pd.Series | None] = {}
        self._history_tried: set[str] = set()
        self._generation = getattr(feed, "generation", 0)

    def _sync(self) -> None:
        """When the feed has switched source (IBKR <-> Yahoo), forget what came from the old
        one: previous closes and usual volumes must come from the feed now in use."""
        gen = getattr(self.feed, "generation", 0)
        if gen != self._generation:
            self._generation = gen
            self._fetched.clear()
            self._prev.clear()
            self._usual.clear()
            self._history_tried.clear()

    def prepare(self, codes: list[str]) -> int:
        """Ahead of the open: prior sessions for a feed that keeps its own (IBKR)."""
        if self.replay:
            return 0
        self._sync()
        return self.feed.prepare(codes, self.day, self.sessions)

    @property
    def label(self) -> str:
        return self.feed.label

    def bars(self, code: str, now: datetime, fetch: bool = True) -> pd.DataFrame:
        if self.replay:
            return self.feed.bars(code, self.day, now)
        self._sync()
        last = self._fetched.get(code)
        if fetch and (last is None or (now - last).total_seconds() >= self.refetch_s):
            if hasattr(self.feed, "fetch_one"):
                self.feed.fetch_one(code, now)
            else:
                self.feed.refresh([code], now)
            self._fetched[code] = now
        return self.feed.bars(code, self.day, now)

    def mark_fetched(self, codes: list[str], now: datetime) -> None:
        for c in codes:
            self._fetched[c] = now

    def data_time(self, now: datetime) -> datetime | None:
        """The market time the feed has reached: the index's newest final bar."""
        b = self.bars(self.index, now)
        return None if not len(b) else b.index.max().to_pydatetime()

    def _history(self):
        return self.minutes if self.replay else self.feed.history_source()

    def _known(self, memo: dict, code: str) -> bool:
        """Remembered for the day? A value is. A missing one only when the minute cache is the
        history (Yahoo, replay); from a feed that keeps its own (IBKR) it is looked for again,
        so prior sessions that arrive later - after a batch timed out - are used."""
        if code not in memo:
            return False
        return memo[code] is not None or self._history() is self.minutes

    def prev_close(self, code: str) -> float | None:
        self._sync()
        if not self._known(self._prev, code):
            px = previous_close(self._history(), code, self.day)
            if px is None and not self.replay:
                self.ensure_history(code)
                px = previous_close(self._history(), code, self.day)
            self._prev[code] = px
        return self._prev[code]

    def usual(self, code: str) -> pd.Series | None:
        self._sync()
        if not self._known(self._usual, code):
            h = self._history()
            u = usual_cum_volume(h, code, self.day, self.sessions, self.min_sessions)
            if u is None and not self.replay:
                self.ensure_history(code)
                h = self._history()
                u = usual_cum_volume(h, code, self.day, self.sessions, self.min_sessions)
            self._usual[code] = u
        return self._usual[code]

    def ensure_history(self, code: str) -> None:
        """Fetch the prior sessions' bars for one stock if they are missing: from Yahoo once a
        day; from a feed that keeps its own (IBKR) as often as that feed allows (it counts
        its own attempts)."""
        if self.replay or code in self._history_tried:
            return
        if self.feed.ensure_history(code, self.day, self.sessions):
            return
        self._history_tried.add(code)
        days = prior_sessions(self.day, self.sessions)
        try:
            backfill(self.minutes, [code], days, batch=1, pause_s=0.0)
        except Exception as e:  # noqa: BLE001
            log.warning("could not fetch prior sessions for %s: %s", code, e)


# --------------------------------------------------------------------------
# fetching many stocks at once
# --------------------------------------------------------------------------
def fetch_batch(codes: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
    """1-minute bars for many ASX codes from Yahoo, [start, end), Sydney-local index,
    lower-case columns. A code with no data is left out."""
    import yfinance as yf

    if not codes:
        return {}
    syms = [symbol(c) for c in codes]
    try:
        raw = yf.download(
            syms,
            start=start.isoformat(),
            end=end.isoformat(),
            interval="1m",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
    except Exception as e:  # noqa: BLE001
        if "rate" in str(e).lower() or "too many" in str(e).lower():
            raise FeedRefused(str(e)) from e
        log.warning("batch minute fetch failed (%d codes): %s", len(codes), e)
        return {}
    out: dict[str, pd.DataFrame] = {}
    if raw is None or not len(raw):
        return out
    for code, sym in zip(codes, syms, strict=True):
        try:
            part = raw[sym] if isinstance(raw.columns, pd.MultiIndex) else raw
        except KeyError:
            continue
        part = part.rename(columns=str.lower)
        if not {"open", "high", "low", "close", "volume"} <= set(part.columns):
            continue
        part = part[["open", "high", "low", "close", "volume"]].dropna(how="all")
        part = part.dropna(subset=["close"])
        if not len(part):
            continue
        idx = part.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        part = part.copy()
        part.index = idx.tz_convert(SYD)
        part["volume"] = part["volume"].fillna(0.0)
        out[code] = part
    return out


def backfill(
    minutes: MinuteBars,
    codes: list[str],
    days: list[date],
    batch: int = 60,
    pause_s: float = 2.0,
) -> dict:
    """Fetch and cache whole past sessions for many codes: the baseline for "usual volume"
    and the plumbing replay. Only days not already complete in the cache are asked for, and
    only days the free feed still holds (~7). Returns counts."""
    now = datetime.now(SYD)
    today = now.date()
    after_close = now.time() >= time_cls(16, 40)
    days = sorted(
        d
        for d in days
        if 0 <= (today - d).days <= MINUTE_HISTORY_DAYS and (d < today or after_close)
    )
    stats = {
        "codes": len(codes),
        "days": [d.isoformat() for d in days],
        "written": 0,
        "already": 0,
        "empty": 0,
    }
    if not days:
        return stats
    need = []
    for c in codes:
        missing = [d for d in days if not _complete(minutes.cached(c, d), d)]
        if missing:
            need.append(c)
        else:
            stats["already"] += 1
    for i in range(0, len(need), batch):
        chunk = need[i : i + batch]
        got = fetch_batch(chunk, days[0], days[-1] + timedelta(days=1))
        for c in chunk:
            df = got.get(c)
            if df is None or not len(df):
                stats["empty"] += 1
                continue
            for d in days:
                part = df[[x == d for x in df.index.date]]
                if len(part) and not _complete(minutes.cached(c, d), d):
                    write_parquet_atomic(part, minutes._path(c, d))
                    stats["written"] += 1
        log.info("backfill: %d/%d codes fetched", min(i + batch, len(need)), len(need))
        time.sleep(pause_s)
    return stats


def _complete(df: pd.DataFrame | None, day: date) -> bool:
    if df is None or not len(df):
        return False
    return df.index.max().time() >= time_cls(16, 10) or (
        df.index.max().time() >= time_cls(15, 59) and day < datetime.now(SYD).date()
    )
