"""1-minute bars, and the rule that turns a decision time into a fill price.

Why this exists: the free feed is ~20 minutes delayed, so at the moment the agent decides,
the true market price is not knowable. Every arena order is recorded pending with the wall
clock time it was recorded (`decided_at`), and filled later from the minute bars after it.

The rule (fixed 2026-09-22 with Rick; tightened 2026-09-23):
  * take the first 1-minute bar that STARTS strictly after `decided_at` - a decision at
    10:37:41 fills in the 10:38 bar at the earliest, never the 10:37 bar it was made in;
  * a minute counts as traded only if its volume is above zero;
  * if that minute did not trade, walk FORWARD to the next minute that did - never back
    to an earlier one;
  * the fill price within the chosen bar is `arena.fill.minute_price` (default `close`);
  * if the delayed feed does not hold that bar yet, the order stays pending until it does.

Until 2026-09-23 the rule took the bar containing the decision minute, and `decided_at` was
the start of the watcher's cycle rather than the moment the order was recorded - so an order
decided after minutes of model calls filled at a price from before it existed (ARN-000002).

The opening auction (TRACKER #28, from 2026-09-24). The 1-minute feed leaves out the ASX
opening auction: its first bar of the day usually has no volume, and the day's minute volumes
sum short of the daily volume. So until 2026-09-24 an order "at the open" filled at the first
traded minute after the auction, not at the auction. Now each day has an auction step ahead of
its first minute bar, priced at Yahoo's daily open and sized from the auction's estimated
volume (opening_auction below, which says how both are checked). It is stamped 09:59, the
last minute of the pre-open, so it sorts before every continuous-trading bar: an order joins
it if it was recorded before 09:59:00 (first_minute_after gives 09:59 at the latest).

Bars are cached to data/arena/minutes/<code>/<date>.parquet so a day is fetched once.
A finished day (one that already holds the 16:10 closing auction print) is never refetched.
Each day's opening auction, once read, is kept beside them as <date>.auction.json and never
read again: every account and every later pass sees the same auction.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from asxbot.io import safe_stem, write_parquet_atomic, write_text_atomic
from asxbot.log import get_logger

log = get_logger("asxbot.arena.minutes")
SYD = ZoneInfo("Australia/Sydney")

# yfinance serves 1-minute bars for roughly the last 7 days only.
MINUTE_HISTORY_DAYS = 7

# The opening auction's place in the day's bars: the last minute of the pre-open. An order
# recorded before this minute starts joins the auction.
AUCTION_MINUTE = time(9, 59)
SESSION_OPEN = time(10, 0)


class NoTradeYet(RuntimeError):
    """No traded minute at or after the decision yet. Try again later; do not fill."""


@dataclass(frozen=True)
class MinuteFill:
    minute: datetime  # the traded minute used
    price: float
    basis: str
    bar_open: float
    bar_high: float
    bar_low: float
    bar_close: float
    volume: float
    minutes_waited: int


@dataclass(frozen=True)
class OpeningAuction:
    """One stock's opening auction on one day, as the arena fills against it.

    `available` False means no auction price the arena will trust: orders that would have
    joined it fill at the first traded minute after it instead, and say so.
    """

    day: str
    available: bool
    reason: str  # why it is unavailable; "" when it is available
    price: float | None = None  # Yahoo's daily open
    volume: float | None = None  # estimated: the daily volume less the minute bars' volume
    daily_volume: float | None = None
    minute_volume: float | None = None  # the day's minute volumes, as held when read
    first_minute_open: float | None = None  # the first traded minute's open, for comparison
    read_at: str = ""

    def bar(self) -> pd.Series:
        """The auction as a bar the broker can work: one price, its estimated volume."""
        p = self.price if self.available else math.nan
        return pd.Series(
            {"open": p, "high": p, "low": p, "close": p,
             "volume": float(self.volume or 0.0) if self.available else 0.0,
             "auction": True, "available": self.available, "reason": self.reason},
            dtype=object,
        )  # fmt: skip


def is_auction(bar) -> bool:
    """True for the opening-auction bar final_bars puts ahead of a day's minute bars."""
    try:
        return bool(bar.get("auction", False))
    except AttributeError:
        return False


class MinuteBars:
    def __init__(self, data_dir: Path, price_field: str = "close"):
        self.dir = Path(data_dir) / "arena" / "minutes"
        self.dir.mkdir(parents=True, exist_ok=True)
        if price_field not in ("close", "open", "typical"):
            raise ValueError(
                f"arena.fill.minute_price must be close|open|typical, got {price_field!r}"
            )
        self.price_field = price_field
        self._warned: set = set()  # (code, day) whose missing auction has been logged

    # -- storage ------------------------------------------------------------
    def _path(self, code: str, day: date) -> Path:
        return self.dir / safe_stem(code.upper().lstrip("^")) / f"{day.isoformat()}.parquet"

    def cached(self, code: str, day: date) -> pd.DataFrame | None:
        p = self._path(code, day)
        if not p.exists():
            return None
        df = pd.read_parquet(p)
        if df.index.tz is None:
            df.index = df.index.tz_localize(SYD)
        return df

    # -- fetching -----------------------------------------------------------
    def fetch(self, code: str, day: date, force: bool = False) -> pd.DataFrame | None:
        """Minute bars for one ASX day, Sydney-local index. None if unavailable."""
        cached = self.cached(code, day)
        if not force and cached is not None and _day_is_complete(cached, day):
            return cached
        if day > date.today() or day.weekday() >= 5:
            return cached  # the future, and weekends, are never worth a request
        if (date.today() - day).days > MINUTE_HISTORY_DAYS:
            log.warning("minute bars for %s on %s are older than the free feed keeps", code, day)
            return cached
        symbol = code if code.startswith("^") else f"{code.upper()}.AX"
        import yfinance as yf

        try:
            raw = yf.Ticker(symbol).history(
                start=day.isoformat(),
                end=(day + timedelta(days=1)).isoformat(),
                interval="1m",
                auto_adjust=False,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("minute fetch failed for %s %s: %s", symbol, day, e)
            return cached
        if raw is None or not len(raw):
            return cached
        df = raw.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].copy()
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert(SYD)
        df = df[[d == day for d in df.index.date]]
        if not len(df):
            return cached
        write_parquet_atomic(df, self._path(code, day))
        return df

    # -- the opening auction -------------------------------------------------
    def _auction_path(self, code: str, day: date) -> Path:
        return self.dir / safe_stem(code.upper().lstrip("^")) / f"{day.isoformat()}.auction.json"

    def cached_auction(self, code: str, day: date) -> OpeningAuction | None:
        p = self._auction_path(code, day)
        if not p.exists():
            return None
        return OpeningAuction(**json.loads(p.read_text(encoding="utf-8")))

    def fetch_daily_row(self, code: str, day: date) -> dict | None:
        """Yahoo's daily bar for one ASX day: {open, high, low, close, volume}. None if the
        feed has none, or the day is outside the window the minute feed covers (the auction
        is only ever read beside that day's minute bars, so older days are never fetched)."""
        if not _feed_holds(day):
            return None
        import yfinance as yf

        symbol = f"{code.upper()}.AX"
        try:
            raw = yf.Ticker(symbol).history(
                start=day.isoformat(),
                end=(day + timedelta(days=1)).isoformat(),
                interval="1d",
                auto_adjust=False,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("daily bar fetch failed for %s %s: %s", symbol, day, e)
            return None
        if raw is None or not len(raw):
            return None
        raw = raw.rename(columns=str.lower)
        rows = raw[[d.date() == day for d in raw.index]]
        if not len(rows):
            return None
        r = rows.iloc[0]
        return {k: float(r[k]) for k in ("open", "high", "low", "close", "volume")}

    def opening_auction(
        self, code: str, day: date, minutes: pd.DataFrame, now: datetime, wait_minutes: int
    ) -> OpeningAuction | None:
        """The day's opening auction: its price and its estimated volume. None means not yet
        known - the broker then works nothing in this ticker from this day on until it is.

        Price: Yahoo's daily open. How it was checked (2026-09-23, 50 stocks, 348 stock-days,
        15-23 Sep; reports/auction_open_check.md): the daily close equals the closing-auction
        (16:10) minute bar on 348 of 348 days, so the daily bar carries the official auction
        prints; the open is on the ASX tick grid on 348 of 348; it lies outside every minute
        bar's range on 20% of days, as a print the minute feed leaves out would, and equals the
        first traded minute's open on only 12%; CNBC's daily bars give the same opens (the
        same vendor, not a second witness). No free source independent of that vendor could
        be read (Stooq, MarketWatch, Market Index, Google and the FT all refuse automated
        reading), so it is NOT confirmed against the ASX's own record.

        Volume: the daily volume less the sum of the day's minute volumes, read together. It
        is an upper bound: the difference also holds off-market trades reported to the ASX
        and, intraday, any trades the daily bar has ahead of the minute bars.

        Refused - the auction is unavailable, and orders that would have joined it fill at the
        first traded minute instead, labelled - when Yahoo has no daily bar with an open, the
        open lies outside the daily bar's own low-high range, the open is off the ASX tick
        grid, or the estimated volume is not above zero. While the feed still holds the day,
        a failed check is read again each pass until `wait_minutes` past the open (a daily bar
        that lags the minute bars must not be refused for good in its first minutes), and only
        then refused. An auction is kept as first accepted or finally refused
        (<date>.auction.json), so nothing is revised after it has been used.
        """
        frozen = self.cached_auction(code, day)
        if frozen is not None:
            return frozen
        traded = minutes[minutes["volume"] > 0]
        if not len(traded):
            return None  # the day has not started trading in the feed
        now = now.astimezone(SYD)
        read_at = now.isoformat(timespec="seconds")
        row = self.fetch_daily_row(code, day)
        live = _feed_holds(day)
        first_open = float(traded["open"].iloc[0])

        def done(auction: OpeningAuction, keep: bool = True) -> OpeningAuction:
            if keep:
                write_text_atomic(
                    json.dumps(asdict(auction), indent=2), self._auction_path(code, day)
                )
            if not auction.available and (code, day) not in self._warned:
                self._warned.add((code, day))
                log.warning(
                    "%s %s: no opening auction price (%s); orders that would have joined it "
                    "fill at the first traded minute after it", code, day, auction.reason,
                )  # fmt: skip
            return auction

        deadline = datetime.combine(day, SESSION_OPEN, tzinfo=SYD) + timedelta(
            minutes=int(wait_minutes)
        )
        waiting = live and now < deadline
        opened = None if row is None else row.get("open")
        if not opened or not math.isfinite(opened) or opened <= 0:
            if waiting:
                return None
            why = "Yahoo has no daily bar with an opening price for the day" + (
                f" by {deadline:%H:%M}" if live else ", and the feed no longer holds the day"
            )
            return done(
                OpeningAuction(
                    day.isoformat(), False, why, first_minute_open=first_open, read_at=read_at
                ),
                keep=live,
            )
        price = float(opened)
        daily_volume = float(row.get("volume") or 0.0)
        minute_volume = float(minutes["volume"].sum())
        est = daily_volume - minute_volume
        base = dict(
            day=day.isoformat(), price=price, daily_volume=daily_volume,
            minute_volume=minute_volume, first_minute_open=first_open, read_at=read_at,
        )  # fmt: skip
        lo, hi = float(row.get("low", price)), float(row.get("high", price))
        tol = 1e-6 * max(1.0, price)
        why = ""
        if not lo - tol <= price <= hi + tol:
            why = f"the daily open {price:.4f} lies outside the daily bar's range {lo}-{hi}"
        elif not _on_tick(price):
            why = f"the daily open {price:.4f} is not on the ASX tick grid"
        elif not est > 0:
            why = (
                f"the auction's volume cannot be measured: the daily volume {daily_volume:,.0f} "
                f"is not above the minute bars' {minute_volume:,.0f}"
            )
        if why:
            if waiting:
                return None
            return done(OpeningAuction(available=False, reason=why, **base))
        # On the grid (checked above) but carried as a float32 by the feed (0.800000011920929).
        base["price"] = _snap_to_tick(price)
        return done(OpeningAuction(available=True, reason="", volume=est, **base))

    # -- the fill rule ------------------------------------------------------
    def fill_at(self, code: str, decided_at: datetime, max_wait_minutes: int = 390) -> MinuteFill:
        """The price for an order recorded at `decided_at`: the first traded bar that starts
        strictly after it.

        Raises NoTradeYet if the feed has no such bar yet.
        """
        target = first_minute_after(decided_at)
        # A decision can be made before the open or after the close, so walk forward across
        # days until a session actually traded.
        for day_offset in range(0, 5):
            day = (target + timedelta(days=day_offset)).date()
            df = self.fetch(code, day)
            if df is None or not len(df):
                continue
            after = df[(df.index >= target) & (df["volume"] > 0)]
            if not len(after):
                continue
            ts = after.index[0]
            waited = int((ts - target).total_seconds() // 60)
            if waited > max_wait_minutes:
                raise NoTradeYet(
                    f"{code}: first traded minute {ts:%Y-%m-%d %H:%M} is {waited} minutes after "
                    f"the first minute after the decision, beyond the {max_wait_minutes}-minute "
                    "limit"
                )
            bar = after.iloc[0]
            return MinuteFill(
                minute=ts.to_pydatetime(),
                price=float(_price_of(bar, self.price_field)),
                basis=(
                    f"{self.price_field} of the {ts:%Y-%m-%d %H:%M} minute bar, the first "
                    f"traded bar after the decision (+{waited} min); "
                    f"volume {float(bar['volume']):,.0f}"
                ),
                bar_open=float(bar["open"]),
                bar_high=float(bar["high"]),
                bar_low=float(bar["low"]),
                bar_close=float(bar["close"]),
                volume=float(bar["volume"]),
                minutes_waited=waited,
            )
        raise NoTradeYet(
            f"{code}: no traded minute at or after {target:%Y-%m-%d %H:%M} (the first minute "
            f"after the decision at {decided_at.astimezone(SYD):%H:%M:%S}) is available yet"
        )

    def traded_minutes(self, code: str, since: datetime, days: int = 5):
        """Yield (timestamp, bar, price) for every traded minute at or after `since`.

        `price` uses the same convention as fill_at, so a resting limit order and an
        immediate fill are priced the same way.
        """
        target = since.astimezone(SYD).replace(second=0, microsecond=0)
        for day_offset in range(0, days):
            day = (target + timedelta(days=day_offset)).date()
            df = self.fetch(code, day)
            if df is None or not len(df):
                continue
            window = df[(df.index >= target) & (df["volume"] > 0)]
            for ts, bar in window.iterrows():
                yield ts.to_pydatetime(), bar, _price_of(bar, self.price_field)

    def final_bars(
        self,
        code: str,
        since: datetime,
        now: datetime,
        settle_minutes: int = 0,
        feed_delay_minutes: int = 22,
        auction_wait_minutes: int | None = None,
    ):
        """Yield (timestamp, bar) for every traded minute from `since` that is final by `now`.

        With `auction_wait_minutes` set, each day that has traded is led by its opening
        auction (opening_auction), stamped 09:59 (AUCTION_MINUTE), unless that is before
        `since`: a bar with one price and the auction's estimated volume, and `auction` True
        (is_auction). An unavailable auction is yielded too, with no volume and `available`
        False, so the broker can label what fills instead. While the auction is not yet
        known, nothing from that day on is yielded: a later bar must not be worked before the
        auction that precedes it. The wait is `auction_wait_minutes` past the feed delay.

        The broker works each bar once and keeps what it saw, so it must only see bars that
        will not change (TRACKER #26, checked 2026-09-24). The rule:
          * the bar traded (volume above zero);
          * it has ended: it started at least a minute before `now`;
          * it is not the newest row of an intraday fetch, nor within `settle_minutes` of
            it. Yahoo appends the minute still forming as a placeholder row (volume 0,
            open = high = low = close = the last trade) and rewrites it until the next row
            appears; no row was seen to change after that. A day is no longer intraday once
            its 16:10 closing-auction bar is in, or `feed_delay_minutes` after the close.
        What was checked: A1M, BHP, FMG and DUG cached files matched a fresh fetch bar for
        bar, and 180 polls of six London and Frankfurt stocks while those markets were open
        saw only the newest row change. ASX bars were not polled during a session.
        """
        start = since.astimezone(SYD).replace(second=0, microsecond=0)
        now = now.astimezone(SYD)
        cutoff = now - timedelta(minutes=1)
        day = start.date()
        while day <= cutoff.date():
            if day.weekday() < 5:
                df = self.fetch(code, day)
                if df is not None and len(df):
                    at_auction = datetime.combine(day, AUCTION_MINUTE, tzinfo=SYD)
                    if (
                        auction_wait_minutes is not None
                        and at_auction >= start
                        and (df["volume"] > 0).any()
                    ):
                        auction = self.opening_auction(
                            code, day, df, now, int(auction_wait_minutes) + int(feed_delay_minutes)
                        )
                        if auction is None:
                            return
                        yield at_auction, auction.bar()
                        df = df[df.index > at_auction]
                    usable = df.index <= cutoff
                    closed = datetime.combine(day, time(16, 10), tzinfo=SYD)
                    intraday = df.index.max() < closed and now < closed + timedelta(
                        minutes=int(feed_delay_minutes)
                    )
                    if intraday:
                        newest = df.index.max()
                        usable &= df.index < newest - timedelta(minutes=int(settle_minutes))
                    window = df[usable & (df.index >= start) & (df["volume"] > 0)]
                    for ts, bar in window.iterrows():
                        yield ts.to_pydatetime(), bar
            day += timedelta(days=1)

    def first_trigger(
        self,
        code: str,
        since: datetime,
        level: float,
        direction: str,
        until: date | None = None,
    ) -> tuple[datetime, float] | None:
        """First traded minute at or after `since` whose range touches `level`.

        direction 'down': the bar's low reached the level (a long's stop, a short's target).
        direction 'up':   the bar's high reached it (a short's stop, a long's target).

        Returns (minute, fill price) with the gap rule applied - the level itself, or the
        bar's open when the bar opened straight through it. For a stop that is the worse of
        the two; for a target it is the better, as a resting limit order would fill.
        None if the level has not been touched yet.

        `until` is the last day to look at. Without it only five calendar days from `since`
        are scanned, which is what every caller got until 2026-09-23 - so a stop on a
        position held longer than that (the yardstick holds ten sessions) went dead.
        """
        target = since.astimezone(SYD).replace(second=0, microsecond=0)
        for day_offset in range(0, _days_to_scan(target, until)):
            day = (target + timedelta(days=day_offset)).date()
            df = self.fetch(code, day)
            if df is None or not len(df):
                continue
            window = df[(df.index >= target) & (df["volume"] > 0)]
            if not len(window):
                continue
            hit = (
                window[window["low"] <= level]
                if direction == "down"
                else window[window["high"] >= level]
            )
            if not len(hit):
                continue
            ts = hit.index[0]
            op = float(hit.iloc[0]["open"])
            price = min(level, op) if direction == "down" else max(level, op)
            return ts.to_pydatetime(), float(price)
        return None

    def index_open(self, code: str, day: date) -> float | None:
        """An index's real opening level on `day`: the open of its 10:00 minute bar.

        Yahoo's daily ^AXJO open equals the previous close on most days (TRACKER #27), so a
        gap measured against it is a raw gap. The 10:00 minute bar's open is the index as
        first computed after the opening auction. Index bars carry no volume, so none is
        required. None if the feed holds no bar stamped exactly 10:00 that day.
        """
        df = self.fetch(code, day)
        if df is None or not len(df):
            return None
        at = datetime.combine(day, time(10, 0), tzinfo=SYD)
        if at not in df.index:
            return None
        px = float(df.loc[at, "open"])
        return px if px > 0 else None

    def price_at(self, code: str, when: datetime, days_back: int = 7) -> float | None:
        """The close of the last traded minute at or before `when`. None if none is known."""
        at = when.astimezone(SYD)
        for day_offset in range(0, days_back + 1):
            day = (at - timedelta(days=day_offset)).date()
            df = self.fetch(code, day)
            if df is None or not len(df):
                continue
            traded = df[(df.index <= at) & (df["volume"] > 0)]
            if len(traded):
                return float(traded["close"].iloc[-1])
        return None

    def last_price(self, code: str, day: date | None = None) -> float | None:
        """Last traded price of a day, for marking positions to market."""
        day = day or date.today()
        df = self.fetch(code, day)
        if df is None or not len(df):
            return None
        traded = df[df["volume"] > 0]
        if not len(traded):
            return None
        return float(traded["close"].iloc[-1])


def first_minute_after(t: datetime) -> datetime:
    """The start of the first whole minute strictly after `t`, in Sydney time.

    Bars are stamped with the minute they start, so "the first bar strictly after t" is the
    first bar at or after this. 10:37:41 -> 10:38:00, and 10:38:00 -> 10:39:00: a bar that
    starts at the very instant of the decision already has its open in the past.
    """
    t = t.astimezone(SYD)
    return t.replace(second=0, microsecond=0) + timedelta(minutes=1)


def _days_to_scan(start: datetime, until: date | None) -> int:
    """Calendar days from `start` through `until` inclusive; five when `until` is unset."""
    if until is None:
        return 5
    return max(1, (until - start.date()).days + 1)


def _feed_holds(day: date) -> bool:
    """True while the free feed still serves this day's minute bars: today and the last
    MINUTE_HISTORY_DAYS days."""
    return 0 <= (date.today() - day).days <= MINUTE_HISTORY_DAYS


def _on_tick(price: float) -> bool:
    """On the ASX price grid: 0.1c under 10c, 0.5c from 10c to $2, 1c from $2."""
    from asxbot.live.scanner import asx_tick

    n = price / asx_tick(price)
    return abs(n - round(n)) < 1e-3


def _snap_to_tick(price: float) -> float:
    from asxbot.live.scanner import asx_tick

    tick = asx_tick(price)
    return round(round(price / tick) * tick, 4)


def _price_of(bar, field: str) -> float:
    if field == "close":
        return float(bar["close"])
    if field == "open":
        return float(bar["open"])
    return (float(bar["high"]) + float(bar["low"]) + float(bar["close"])) / 3.0


def _day_is_complete(df: pd.DataFrame, day: date) -> bool:
    """True once the cached day holds a bar at or after 16:10, the ASX closing auction."""
    if not len(df) or day >= date.today():
        return False
    last = df.index.max()
    return (last.hour, last.minute) >= (16, 10)
