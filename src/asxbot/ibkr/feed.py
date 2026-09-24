"""IBKR live bars and quotes behind the arena's feed interfaces, with Yahoo as the fallback.

`data.live_provider: ibkr` in config.yaml selects this (`yfinance` keeps Yahoo's delayed
feed and never touches IB Gateway). With ibkr:

  * FailoverFeed serves IBKR's bars while IB Gateway is connected, reaches IBKR, and sends
    real-time data. If any of those fails it switches to Yahoo's delayed feed at once -
    logged as an ERROR, written to data/arena/live_data.json (which the `live_data`
    self-check reads) and to the `live_data` event log - and tries IBKR again every
    `ibkr.reconnect_every_s`. Every decision records the label of the feed it used, so the
    evening report can say which prices each decision was made on.
  * "Usual volume" and the previous close come from IBKR's own prior sessions when IBKR is
    the feed, so today's volume is never divided by another vendor's volume. They are held
    in memory for the day and never written into the Yahoo minute cache.
  * Fills stay simulated from the minute cache as before (arena/broker.py): the broker
    prices each order at the first bar after the decision, whichever feed made it.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from datetime import time as time_cls
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from asxbot.arena.intraday import (
    FeedNotConnected,
    IntradayFeed,
    YahooDelayedFeed,
    prior_sessions,
    visible,
)
from asxbot.ibkr.gateway import (
    MARKET_DATA_TYPES,
    REAL_TIME_TYPES,
    Gateway,
    end_of,
    maybe_reconnect,
    seconds_since,
    shared,
)
from asxbot.io import write_text_atomic
from asxbot.live.quotes import Quote, QuoteProvider
from asxbot.log import get_logger

log = get_logger("asxbot.ibkr.feed")
SYD = ZoneInfo("Australia/Sydney")
LIVE_LABEL = "live data (IBKR)"
FALLBACK_LABEL = "delayed data (Yahoo) - IBKR unavailable"
MARKET_OPEN, MARKET_CLOSE = time_cls(10, 0), time_cls(16, 10)
PROBE_EVERY = timedelta(minutes=10)
STATUS_EVERY = timedelta(minutes=5)


def status_path(data_dir: Path) -> Path:
    return Path(data_dir) / "arena" / "live_data.json"


def read_status(data_dir: Path) -> dict:
    p = status_path(data_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def market_hours(now: datetime) -> bool:
    from asxbot.announcements.live import is_trading_day

    now = now.astimezone(SYD)
    return (
        now.weekday() < 5
        and is_trading_day(now.date())
        and MARKET_OPEN <= now.time() < MARKET_CLOSE
    )


class IBKRLiveFeed(IntradayFeed):
    """IBKR's 1-minute bars, rotated through historical requests (gateway.py says why and
    how the pacing limits are kept). Everything is held in memory."""

    name = "ibkr_live"

    def __init__(self, minutes, gateway: Gateway, events=None):
        super().__init__(minutes)
        self.gw = gateway
        self.events = events
        self.frames: dict[tuple[str, date], pd.DataFrame] = {}
        self.hist: dict[tuple[str, date], pd.DataFrame] = {}
        self.hist_tried: set[tuple[str, date]] = set()
        self.last_refresh: dict[str, datetime] = {}

    @property
    def delayed(self) -> bool:  # type: ignore[override]
        return self.gw.health.market_data_type not in (None, *REAL_TIME_TYPES)

    @property
    def label(self) -> str:
        return LIVE_LABEL if not self.delayed else "delayed data (IBKR, not real-time)"

    def ok(self) -> bool:
        return self.gw.ready

    # -- today ---------------------------------------------------------------
    def _duration(self, code: str, day: date, now: datetime) -> str:
        have = self.frames.get((code, day))
        since = (
            have.index.max().to_pydatetime()
            if have is not None and len(have)
            else datetime.combine(day, time_cls(9, 55), tzinfo=SYD)
        )
        return seconds_since(since, now)

    def _merge(self, code: str, day: date, df: pd.DataFrame) -> None:
        part = df[[d == day for d in df.index.date]]
        if not len(part):
            return
        old = self.frames.get((code, day))
        if old is not None and len(old):
            part = pd.concat([old[old.index < part.index.min()], part])
        self.frames[(code, day)] = part

    def refresh(self, codes: list[str], now: datetime) -> list[str]:
        if not self.gw.ready:
            raise FeedNotConnected(f"IB Gateway not ready: {self.gw.health.last_error or '?'}")
        now = now.astimezone(SYD)
        day = now.date()
        epoch = datetime(2000, 1, 1, tzinfo=SYD)
        fresh = now - timedelta(seconds=float(self.gw.s.min_refetch_s))
        due = [c for c in codes if self.last_refresh.get(c, epoch) <= fresh]
        chosen = sorted(due, key=lambda c: self.last_refresh.get(c, epoch))[: self.gw.room()]
        if not chosen:
            return []
        got = self.gw.bars({c: (self._duration(c, day, now), None) for c in chosen})
        for c in chosen:
            self.last_refresh[c] = now
        for c, df in got.items():
            self._merge(c, day, df)
        if len(chosen) >= 10 and len(got) < 0.2 * len(chosen):
            why = f"IBKR returned bars for {len(got)} of {len(chosen)} stocks"
            self.gw.health.last_error = why
            self.gw.health.server_ok = False
            raise FeedNotConnected(why)
        return chosen

    def fetch_one(self, code: str, now: datetime) -> None:
        self.refresh([code], now)

    def bars(self, code: str, day: date, now: datetime) -> pd.DataFrame:
        df = self.frames.get((code, day))
        if df is None:
            df = self.hist.get((code, day))
        complete = df is not None and len(df) and df.index.max().time() >= time_cls(16, 10)
        return visible(df, now, None, day_complete=bool(complete), index=code.startswith("^"))

    # -- prior sessions ------------------------------------------------------
    def history_source(self):
        return self

    def cached(self, code: str, day: date) -> pd.DataFrame | None:
        """The prior-session bars IBKR returned (the interface MinuteBars.cached has)."""
        return self.hist.get((code, day))

    def prepare(self, codes: list[str], day: date, sessions: int) -> int:
        """Fetch the prior sessions for codes that do not have them yet, as many as the
        pacing allows now. Returns how many were asked for."""
        if not self.gw.ready:
            return 0
        need = [c for c in codes if (c, day) not in self.hist_tried][: self.gw.room()]
        if not need:
            return 0
        days = prior_sessions(day, int(sessions))
        got = self.gw.bars({c: (f"{int(sessions) + 1} D", end_of(day)) for c in need})
        for c in need:
            self.hist_tried.add((c, day))
            df = got.get(c)
            if df is None:
                continue
            for d in days:
                part = df[[x == d for x in df.index.date]]
                if len(part):
                    self.hist[(c, d)] = part
        log.info("IBKR prior sessions: %d asked, %d returned", len(need), len(got))
        return len(need)

    def ensure_history(self, code: str, day: date, sessions: int) -> bool:
        self.prepare([code], day, sessions)
        return True


class FailoverFeed(IntradayFeed):
    """IBKR while it works, Yahoo's delayed feed when it does not (module docstring)."""

    def __init__(
        self,
        minutes,
        primary: IBKRLiveFeed,
        backup: YahooDelayedFeed,
        data_dir: Path,
        events=None,
        now=None,
    ):
        super().__init__(minutes)
        self.primary, self.backup = primary, backup
        self.data_dir = Path(data_dir)
        self.events = events
        self.generation = 0
        self.why = ""
        self._probed: datetime | None = None
        self._written: datetime | None = None
        now = (now or datetime.now(SYD)).astimezone(SYD)
        self.using_primary = primary.ok()
        if not self.using_primary:
            self.why = primary.gw.health.last_error or "IB Gateway not ready"
            log.error("live prices: IBKR unavailable at start (%s); using Yahoo's delayed feed",
                      self.why)  # fmt: skip
            self._event("fallback", now)
        self.write_status(now)

    # -- which feed ----------------------------------------------------------
    @property
    def active(self) -> IntradayFeed:
        return self.primary if self.using_primary else self.backup

    @property
    def name(self) -> str:  # type: ignore[override]
        return self.active.name

    @property
    def delayed(self) -> bool:  # type: ignore[override]
        return self.active.delayed

    @property
    def label(self) -> str:
        return self.primary.label if self.using_primary else FALLBACK_LABEL

    def _event(self, what: str, now: datetime) -> None:
        if self.events is not None:
            self.events.append(
                "live_data",
                {"event": what, "at": now.isoformat(timespec="seconds"), "why": self.why,
                 "provider": "ibkr" if self.using_primary else "yfinance",
                 **self.primary.gw.health.to_dict()},
            )  # fmt: skip

    def _switch(self, to_primary: bool, why: str, now: datetime) -> None:
        self.using_primary = to_primary
        self.why = why
        self.generation += 1
        if to_primary:
            log.warning("live prices: back on IBKR (%s)", why)
            self._event("restored", now)
        else:
            log.error("live prices: IBKR unavailable (%s); falling back to Yahoo's delayed feed",
                      why)  # fmt: skip
            self._event("fallback", now)
        self.write_status(now, force=True)

    def check(self, now: datetime) -> None:
        """Switch if IBKR has failed, or has come back; probe real-time-ness in hours."""
        now = now.astimezone(SYD)
        gw = self.primary.gw
        if self.using_primary:
            if not gw.ready:
                self._switch(False, gw.health.last_error or "IB Gateway not ready", now)
            elif market_hours(now) and (self._probed is None or now - self._probed >= PROBE_EVERY):
                self._probed = now
                q = gw.quote(gw.s.probe_code)
                mdt = None if q is None else q.get("market_data_type")
                if q is not None and mdt not in REAL_TIME_TYPES:
                    self._switch(
                        False,
                        f"IBKR sent {MARKET_DATA_TYPES.get(mdt or 0, 'unknown')} data for "
                        f"{gw.s.probe_code}, not real-time: is the ASX subscription active?",
                        now,
                    )
        elif maybe_reconnect(gw):
            self._switch(True, "IB Gateway is back", now)
        self.write_status(now)

    # -- the feed interface --------------------------------------------------
    def refresh(self, codes: list[str], now: datetime) -> list[str]:
        self.check(now)
        if self.using_primary:
            try:
                return self.primary.refresh(codes, now)
            except FeedNotConnected as e:
                self._switch(False, str(e), now)
        return self.backup.refresh(codes, now)

    def fetch_one(self, code: str, now: datetime) -> None:
        self.check(now)
        if self.using_primary:
            try:
                self.primary.fetch_one(code, now)
                return
            except FeedNotConnected as e:
                self._switch(False, str(e), now)
        self.backup.fetch_one(code, now)

    def bars(self, code: str, day: date, now: datetime) -> pd.DataFrame:
        return self.active.bars(code, day, now)

    def history_source(self):
        return self.active.history_source()

    def ensure_history(self, code: str, day: date, sessions: int) -> bool:
        return self.active.ensure_history(code, day, sessions)

    def prepare(self, codes: list[str], day: date, sessions: int) -> int:
        return self.primary.prepare(codes, day, sessions) if self.using_primary else 0

    # -- status for the self-check and the report ----------------------------
    def write_status(self, now: datetime, force: bool = False) -> None:
        if not force and self._written is not None and now - self._written < STATUS_EVERY:
            return
        self._written = now
        body = {
            "at": now.isoformat(timespec="seconds"),
            "provider_config": "ibkr",
            "provider_in_use": "ibkr" if self.using_primary else "yfinance",
            "label": self.label,
            "why": self.why,
            "gateway": self.primary.gw.health.to_dict(),
            "port": self.primary.gw.s.port,
            "requests_per_scan": self.primary.gw.budget,
        }
        try:
            write_text_atomic(json.dumps(body, indent=2), status_path(self.data_dir))
        except OSError as e:
            log.warning("could not write the live-data status: %s", e)


def build_failover(cfg, minutes, yahoo: YahooDelayedFeed, events=None) -> FailoverFeed:
    gw = shared(cfg)
    maybe_reconnect(gw)
    return FailoverFeed(minutes, IBKRLiveFeed(minutes, gw, events), yahoo, cfg.data_dir, events)


# --------------------------------------------------------------------------
# quotes
# --------------------------------------------------------------------------
class IBKRQuotes(QuoteProvider):
    name = "ibkr"

    def __init__(self, gateway: Gateway, index_ticker: str = "^AXJO"):
        self.gw = gateway
        self.index_ticker = index_ticker

    def _q(self, code: str) -> Quote | None:
        q = self.gw.quote(code)
        if q is None:
            return None
        prev = q["prev_close"]
        last = q["last"] or prev
        if not (last and prev):
            return None
        mdt = q["market_data_type"]
        live = mdt in REAL_TIME_TYPES
        return Quote(
            code, float(last), float(q["open"] or last), float(prev), float(q["volume"] or 0.0),
            datetime.now(SYD),
            f"ibkr ({MARKET_DATA_TYPES.get(mdt or 0, 'unknown')})", not live,
            bid=q["bid"], ask=q["ask"], bid_size=q["bid_size"], ask_size=q["ask_size"],
            last_size=q["last_size"], halted=q["halted"], auction_price=q["auction_price"],
            auction_volume=q["auction_volume"],
        )  # fmt: skip

    def quote(self, ticker: str) -> Quote | None:
        return self._q(ticker.removesuffix(".AX").upper())

    def index_quote(self) -> Quote | None:
        return self._q(self.index_ticker)


class FailoverQuotes(QuoteProvider):
    """IBKR's quote while IB Gateway is ready, else Yahoo's. Each Quote's `source` says
    which it came from."""

    name = "ibkr+yfinance"

    def __init__(self, primary: IBKRQuotes, backup: QuoteProvider):
        self.primary, self.backup = primary, backup

    def quote(self, ticker: str) -> Quote | None:
        if self.primary.gw.ready:
            q = self.primary.quote(ticker)
            if q is not None:
                return q
        return self.backup.quote(ticker)

    def index_quote(self) -> Quote | None:
        if self.primary.gw.ready:
            q = self.primary.index_quote()
            if q is not None:
                return q
        return self.backup.index_quote()


def quote_provider(cfg, backup: QuoteProvider) -> QuoteProvider:
    gw = shared(cfg)
    maybe_reconnect(gw)
    return FailoverQuotes(IBKRQuotes(gw, str(cfg.get("backtest.index_ticker", "^AXJO"))), backup)
