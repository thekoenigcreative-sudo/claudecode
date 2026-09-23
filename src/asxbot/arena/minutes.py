"""1-minute bars, and the rule that turns a decision time into a fill price.

Why this exists: the free feed is ~20 minutes delayed, so at the moment the agent decides,
the true market price is not knowable. Every arena order is recorded pending with its
decision timestamp, and filled later from the minute bar covering that minute.

The rule (fixed 2026-09-22 with Rick):
  * take the 1-minute bar whose minute contains the decision timestamp;
  * a minute counts as traded only if its volume is above zero;
  * if that minute did not trade, walk FORWARD to the next minute that did - never back
    to an earlier one;
  * the fill price within the chosen bar is `arena.fill.minute_price` (default `close`:
    the last price of that minute, so never earlier than the decision itself).

Bars are cached to data/arena/minutes/<code>/<date>.parquet so a day is fetched once.
A finished day (one that already holds the 16:10 closing auction print) is never refetched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from asxbot.io import safe_stem, write_parquet_atomic
from asxbot.log import get_logger

log = get_logger("asxbot.arena.minutes")
SYD = ZoneInfo("Australia/Sydney")

# yfinance serves 1-minute bars for roughly the last 7 days only.
MINUTE_HISTORY_DAYS = 7


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


class MinuteBars:
    def __init__(self, data_dir: Path, price_field: str = "close"):
        self.dir = Path(data_dir) / "arena" / "minutes"
        self.dir.mkdir(parents=True, exist_ok=True)
        if price_field not in ("close", "open", "typical"):
            raise ValueError(
                f"arena.fill.minute_price must be close|open|typical, got {price_field!r}"
            )
        self.price_field = price_field

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

    # -- the fill rule ------------------------------------------------------
    def fill_at(self, code: str, decision_at: datetime, max_wait_minutes: int = 390) -> MinuteFill:
        """The price for a decision made at `decision_at`.

        Raises NoTradeYet if the feed has no traded minute at or after that minute yet.
        """
        target = decision_at.astimezone(SYD).replace(second=0, microsecond=0)
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
                    f"the decision, beyond the {max_wait_minutes}-minute limit"
                )
            bar = after.iloc[0]
            return MinuteFill(
                minute=ts.to_pydatetime(),
                price=float(_price_of(bar, self.price_field)),
                basis=(
                    f"{self.price_field} of the {ts:%Y-%m-%d %H:%M} minute bar "
                    f"(+{waited} min after the decision); volume {float(bar['volume']):,.0f}"
                ),
                bar_open=float(bar["open"]),
                bar_high=float(bar["high"]),
                bar_low=float(bar["low"]),
                bar_close=float(bar["close"]),
                volume=float(bar["volume"]),
                minutes_waited=waited,
            )
        raise NoTradeYet(
            f"{code}: no traded minute at or after {target:%Y-%m-%d %H:%M} is available yet"
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

    def first_open(
        self, code: str, since: datetime, until: date | None = None
    ) -> tuple[datetime, float] | None:
        """The first traded minute at or after `since`, and its open. None if none yet."""
        target = since.astimezone(SYD).replace(second=0, microsecond=0)
        for day_offset in range(0, _days_to_scan(target, until)):
            day = (target + timedelta(days=day_offset)).date()
            df = self.fetch(code, day)
            if df is None or not len(df):
                continue
            window = df[(df.index >= target) & (df["volume"] > 0)]
            if len(window):
                return window.index[0].to_pydatetime(), float(window.iloc[0]["open"])
        return None

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


def _days_to_scan(start: datetime, until: date | None) -> int:
    """Calendar days from `start` through `until` inclusive; five when `until` is unset."""
    if until is None:
        return 5
    return max(1, (until - start.date()).days + 1)


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
