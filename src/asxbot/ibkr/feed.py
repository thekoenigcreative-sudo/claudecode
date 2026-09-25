"""IBKR live bars and quotes behind the arena's feed interfaces, on the persistent
connection (ibkr/live.py), with Yahoo only for history gap-fill and reports.

`data.live_provider: ibkr` in config.yaml selects this (`yfinance` keeps Yahoo's delayed
feed and never touches IB Gateway). With ibkr:

  * IBKRLiveFeed streams 1-minute bars (5-second real-time bars aggregated in live.py) for
    the stocks the playbooks need most, ROTATING the day trader's universe inside the
    account's market data line limit, and refreshes the rest with small, paced historical
    requests (docs/ibkr_live_data.md says how):
      - tier 1, always streaming: the ASX 200 index; every stock with a position or an
        order working in any arena account; every stock queued for a v2 reaction look or a
        v2 rule-bot candidate (news since the last close); the watcher pins them each cycle
        (`pinned`);
      - tier 2, rotating: the rest of the liquid universe, ranked by an interest score the
        day trader's scan hands back (`note_interest`: how far it has moved against the
        index, its relative volume, how close it is to its opening range or to VWAP) and by
        how long since it was last refreshed. A slot is held at least `min_dwell_s`;
      - polled: a stock without a line gets one small history request (today, since the
        last minute held) at most every few minutes, `poll_per_cycle` of them a cycle,
        stalest first. Never a batch.
  * FailoverFeed says whether NEW ENTRIES are allowed (`entries_allowed`): not while the
    connection is down, the heartbeat stale, the data delayed rather than real-time, or the
    index's or the stock's streamed bars older than `max_data_age_s` in market hours. The
    playbooks make no entry on a "no" (they log "paused: live feed down"); the broker keeps
    working stops, targets and trailing from the best bars it holds (minutes.py: the live
    bars while they flow, Yahoo's delayed bars behind them). Yahoo is never the source of an
    entry decision. Everything is written to data/arena/live_data.json every cycle for the
    `live_data` self-check and the evening report.
  * "Usual volume" and the previous close come from IBKR's own prior sessions, fetched
    before the open through the paced queue and cached on local disk, never from another
    vendor's volume.
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
    market_hours,
)
from asxbot.ibkr.live import LiveGateway, shared_live
from asxbot.io import write_text_atomic
from asxbot.live.quotes import Quote, QuoteProvider
from asxbot.log import get_logger

log = get_logger("asxbot.ibkr.feed")
SYD = ZoneInfo("Australia/Sydney")
LIVE_LABEL = "live data (IBKR)"
FALLBACK_LABEL = "delayed data (Yahoo) - IBKR unavailable"
PAUSED_LABEL = "paused: live feed down"
PROBE_EVERY = timedelta(minutes=10)
STATUS_EVERY = timedelta(minutes=5)
HISTORY_TRIES = 3  # asks for a stock's prior sessions before giving up (live.py)
POLL_MIN_GAP_S = 120.0  # a stock without a line is polled at most this often


def status_path(data_dir: Path) -> Path:
    return Path(data_dir) / "arena" / "live_data.json"


def quote_label(q, provider: str = "ibkr") -> str:
    """The label of the prices a quote came from, in the words the feeds use: a decision
    made on a quote records this (the evening report counts decisions by it)."""
    from asxbot.arena.intraday import DELAYED_LABEL

    if q is None:
        return "no quote"
    if str(q.source).startswith("ibkr"):
        return "delayed data (IBKR, not real-time)" if q.delayed else LIVE_LABEL
    return FALLBACK_LABEL if provider == "ibkr" else DELAYED_LABEL


def read_status(data_dir: Path) -> dict:
    p = status_path(data_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


class IBKRLiveFeed(IntradayFeed):
    """IBKR's 1-minute bars from the persistent connection: streamed for the stocks that
    matter most, polled for the rest (module docstring). Everything is held in memory
    except prior sessions, which live.py caches on local disk."""

    name = "ibkr_live"

    def __init__(self, minutes, gateway: LiveGateway, events=None, index: str = "^AXJO"):
        super().__init__(minutes)
        self.gw = gateway
        self.events = events
        self.index = index
        self.pinned: set[str] = set()
        self.interest: dict[str, float] = {}
        self.last_refresh: dict[str, datetime] = {}
        self.polled_at: dict[str, datetime] = {}
        self.rotation: dict = {}

    @property
    def delayed(self) -> bool:  # type: ignore[override]
        return self.gw.health.market_data_type not in (None, *REAL_TIME_TYPES)

    @property
    def label(self) -> str:
        return LIVE_LABEL if not self.delayed else "delayed data (IBKR, not real-time)"

    def ok(self) -> bool:
        return self.gw.ready

    # -- what the watcher tells the feed -------------------------------------------
    def pin(self, codes) -> None:
        """Tier 1: always streaming while pinned (positions, working orders, news)."""
        self.pinned = {c.upper() for c in codes}

    def note_interest(self, scores: dict[str, float]) -> None:
        """Tier 2 ranking from the day trader's scan: higher streams first."""
        self.interest = {k.upper(): float(v) for k, v in scores.items()}

    def rotation_order(self, codes: list[str], now: datetime) -> list[str]:
        """The index, then the pinned stocks, then the rest by interest and staleness."""
        epoch = datetime(2000, 1, 1, tzinfo=SYD)

        def stale(c: str) -> float:
            return (now - self.last_refresh.get(c, epoch)).total_seconds()

        rest = [c for c in dict.fromkeys(c.upper() for c in codes) if c not in self.pinned
                and c != self.index]  # fmt: skip
        rest.sort(key=lambda c: (-self.interest.get(c, 0.0), -stale(c), c))
        return [self.index, *sorted(self.pinned - {self.index}), *rest]

    # -- today ---------------------------------------------------------------
    def refresh(self, codes: list[str], now: datetime) -> list[str]:
        if not self.gw.ready:
            raise FeedNotConnected(f"IB Gateway not ready: {self.gw.health.last_error or '?'}")
        now = now.astimezone(SYD)
        day = now.date()
        order = self.rotation_order(list(codes), now)
        self.gw.set_streaming(order)
        streaming = [c for c in order if self.gw.streaming(c)]
        for c in streaming:
            self.last_refresh[c] = now
        cold = [c for c in order if not self.gw.streaming(c)]
        epoch = datetime(2000, 1, 1, tzinfo=SYD)
        cold.sort(key=lambda c: self.polled_at.get(c, epoch))
        polled = []
        for c in cold:
            if len(polled) >= int(self.gw.s.poll_per_cycle):
                break
            last = self.polled_at.get(c)
            if last is not None and (now - last).total_seconds() < POLL_MIN_GAP_S:
                continue
            if self.gw.queue_history(c, "today", day):
                self.polled_at[c] = now
                self.last_refresh[c] = now
                polled.append(c)
        self.rotation = {
            "at": now.isoformat(timespec="seconds"), "universe": len(order),
            "streaming": len(streaming), "pinned": len(self.pinned), "polled": len(polled),
            "cold": len(cold), "line_limit": self.gw.line_limit,
        }  # fmt: skip
        if self.events is not None and now.second < 5 and now.minute % 10 == 0:
            self.events.append("ibkr_rotation", self.rotation)
        return [*streaming, *polled]

    def fetch_one(self, code: str, now: datetime) -> None:
        code = code.upper()
        if self.gw.streaming(code):
            return
        now = now.astimezone(SYD)
        last = self.polled_at.get(code)
        if last is not None and (now - last).total_seconds() < POLL_MIN_GAP_S:
            return
        if self.gw.queue_history(code, "today", now.date()):
            self.polled_at[code] = now

    def bars(self, code: str, day: date, now: datetime) -> pd.DataFrame:
        df = self.gw.bars_today(code, day)
        if df is None:
            df = self.cached(code, day)
        # Every row is a completed minute (live.py drops a history fetch's forming minute),
        # so the newest row is final: nothing is held back beyond the "ended a minute ago"
        # rule.
        return visible(df, now, None, day_complete=True, index=code.startswith("^"))

    def bars_today(self, code: str, day: date) -> pd.DataFrame | None:
        """The completed minutes held for today (for the broker's minute overlay)."""
        return self.gw.bars_today(code, day)

    def fresh(self, code: str, now: datetime) -> bool:
        ok, _ = self.gw.entries_allowed(now, [code], self.index)
        return ok

    # -- prior sessions ------------------------------------------------------
    def history_source(self):
        return self

    def cached(self, code: str, day: date) -> pd.DataFrame | None:
        """The prior-session bars IBKR returned (the interface MinuteBars.cached has)."""
        return self.gw.cached_history(code, day)

    def _has_history(self, code: str, day: date, sessions: int) -> bool:
        if self.gw.history_done(code, day):
            return True
        days = prior_sessions(day, int(sessions))
        return all(self.cached(code, d) is not None for d in days)

    def prepare(self, codes: list[str], day: date, sessions: int) -> int:
        """Queue the prior sessions for codes that do not have them yet, through the paced
        history queue (a few in flight, a gap between them, retries with backoff; live.py).
        Returns how many were queued this call. Non-blocking: the answers arrive in the
        background and `history_status` says who is complete."""
        if not self.gw.ready:
            return 0
        n = 0
        for c in dict.fromkeys(x.upper() for x in codes):
            if (c, day) in self.gw.hist_given_up or c in self.gw.unknown_codes:
                continue
            if self._has_history(c, day, sessions):
                continue
            if self.gw.queue_history(c, "prior", day, sessions):
                n += 1
        if n:
            log.info("IBKR prior sessions: %d stock(s) queued", n)
        return n

    def history_status(self, codes: list[str], day: date, sessions: int) -> dict:
        """Who has their prior sessions and who does not, for the 09:55 report."""
        complete, missing, given_up = [], [], []
        for c in dict.fromkeys(x.upper() for x in codes):
            if (c, day) in self.gw.hist_given_up or c in self.gw.unknown_codes:
                given_up.append(c)
            elif self._has_history(c, day, sessions):
                complete.append(c)
            else:
                missing.append(c)
        return {"complete": complete, "missing": missing, "given_up": given_up}

    def ensure_history(self, code: str, day: date, sessions: int) -> bool:
        self.prepare([code], day, sessions)
        return True

    def entries_allowed(self, now: datetime, codes=()) -> tuple[bool, str]:
        return self.gw.entries_allowed(now, list(codes), self.index)


class FailoverFeed(IntradayFeed):
    """IBKR's live feed for every decision; Yahoo's delayed bars only to fill gaps for
    marks, context and reports when IBKR has none (module docstring). While IBKR is down
    or stale, `entries_allowed` is False and the playbooks pause their entries."""

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
        self.paused = False
        self.pause_why = ""
        self.paused_since: datetime | None = None
        self.delayed_why = ""
        self._probed: datetime | None = None
        self._written: datetime | None = None
        now = (now or datetime.now(SYD)).astimezone(SYD)
        self.using_primary = primary.ok()
        if not self.using_primary:
            self.why = primary.gw.health.last_error or "IB Gateway not ready"
            log.error("live prices: IBKR unavailable at start (%s); entries paused until it "
                      "is back", self.why)  # fmt: skip
            self._event("fallback", now)
        self.check(now)
        self.write_status(now, force=True)

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
                 "paused": self.paused, "pause_why": self.pause_why,
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
            log.error("live prices: IBKR unavailable (%s); NEW ENTRIES PAUSED. Stops, targets "
                      "and trailing keep working from the bars held and Yahoo's delayed bars",
                      why)  # fmt: skip
            self._event("fallback", now)
        self.write_status(now, force=True)

    def _set_paused(self, paused: bool, why: str, now: datetime) -> None:
        if paused and not self.paused:
            self.paused, self.pause_why, self.paused_since = True, why, now
            log.error("%s (%s): no new entries until IBKR data is fresh again", PAUSED_LABEL, why)
            self._event("paused", now)
        elif not paused and self.paused:
            since = self.paused_since
            self.paused, self.pause_why, self.paused_since = False, "", None
            gone = f"{(now - since).total_seconds() / 60:.0f} min" if since else "?"
            log.warning("resumed: live feed back after %s (was: %s); entries allowed again",
                        gone, why)  # fmt: skip
            self._event("resumed", now)
        elif paused:
            self.pause_why = why

    def check(self, now: datetime) -> None:
        """Switch when IBKR has failed or come back; probe real-time-ness in hours; decide
        whether entries are allowed; write the status."""
        now = now.astimezone(SYD)
        gw = self.primary.gw
        ready = gw.ready
        if self.using_primary and not ready:
            self._switch(False, gw.health.last_error or "IB Gateway not ready", now)
        elif not self.using_primary and ready:
            self._switch(True, "IB Gateway is back", now)
        due = self._probed is None or now - self._probed >= PROBE_EVERY
        if ready and market_hours(now) and due:
            self._probed = now
            q = gw.quote(gw.s.probe_code)
            mdt = None if q is None else q.get("market_data_type")
            if q is not None and mdt not in REAL_TIME_TYPES:
                self.delayed_why = (
                    f"IBKR sent {MARKET_DATA_TYPES.get(mdt or 0, 'unknown')} data for "
                    f"{gw.s.probe_code}, not real-time: is the ASX subscription active?"
                )
            elif q is None and gw.health.permission_denied:
                self.delayed_why = (
                    f"IBKR refused real-time data for {gw.s.probe_code} "
                    f"({gw.health.last_request_error or 'no subscription'}): is the ASX "
                    "subscription active?"
                )
            else:
                self.delayed_why = ""
        if not self.using_primary:
            self._set_paused(True, self.why or "IB Gateway not ready", now)
        elif self.delayed_why:
            self._set_paused(True, self.delayed_why, now)
        else:
            ok, why = self.primary.entries_allowed(now)
            self._set_paused(not ok, why, now)
        self.write_status(now)

    def entries_allowed(self, now: datetime, codes=()) -> tuple[bool, str]:
        """May a playbook make a NEW entry now, on these stocks' prices?"""
        if self.paused:
            return False, self.pause_why or PAUSED_LABEL
        if not self.using_primary:
            return False, self.why or "IB Gateway not ready"
        return self.primary.entries_allowed(now, codes)

    # -- the feed interface --------------------------------------------------
    def refresh(self, codes: list[str], now: datetime) -> list[str]:
        self.check(now)
        if not self.using_primary:
            return []  # nothing is scanned on Yahoo: entries are paused
        try:
            return self.primary.refresh(codes, now)
        except FeedNotConnected as e:
            self._switch(False, str(e), now)
            self._set_paused(True, str(e), now)
            return []

    def fetch_one(self, code: str, now: datetime) -> None:
        if self.using_primary:
            self.primary.fetch_one(code, now)

    def bars(self, code: str, day: date, now: datetime) -> pd.DataFrame:
        """IBKR's bars when it holds any for the day; Yahoo's otherwise (gap-fill for marks,
        context and reports - never an entry decision, which `entries_allowed` guards)."""
        if self.using_primary or self.primary.gw.bars_today(code, day) is not None:
            df = self.primary.bars(code, day, now)
            if len(df):
                return df
        return self.backup.bars(code, day, now)

    def bars_today(self, code: str, day: date) -> pd.DataFrame | None:
        return self.primary.bars_today(code, day)

    def live_ok(self) -> bool:
        return self.using_primary and self.primary.gw.ready

    def history_source(self):
        return self.primary.history_source() if self.using_primary else self.backup.history_source()

    def ensure_history(self, code: str, day: date, sessions: int) -> bool:
        self.write_status(datetime.now(SYD))
        return self.primary.ensure_history(code, day, sessions) if self.using_primary else False

    def prepare(self, codes: list[str], day: date, sessions: int) -> int:
        self.write_status(datetime.now(SYD))
        return self.primary.prepare(codes, day, sessions) if self.using_primary else 0

    def history_status(self, codes: list[str], day: date, sessions: int) -> dict:
        return self.primary.history_status(codes, day, sessions)

    def pin(self, codes) -> None:
        self.primary.pin(codes)

    def note_interest(self, scores: dict[str, float]) -> None:
        self.primary.note_interest(scores)

    # -- status for the self-check and the report ----------------------------
    def write_status(self, now: datetime, force: bool = False) -> None:
        if not force and self._written is not None and (
            timedelta(0) <= now - self._written < STATUS_EVERY
        ):
            return
        self._written = now
        gw = self.primary.gw
        body = {
            "at": now.isoformat(timespec="seconds"),
            "provider_config": "ibkr",
            "provider_in_use": "ibkr" if self.using_primary else "yfinance",
            "quotes_from": "ibkr" if gw.ready else "yfinance",
            "label": self.label,
            "why": self.why,
            "entries": "paused" if self.paused else "allowed",
            "paused_why": self.pause_why,
            "paused_since": (self.paused_since.isoformat(timespec="seconds")
                             if self.paused_since else None),  # fmt: skip
            "gateway": gw.status(),
            "rotation": self.primary.rotation,
            "port": gw.s.port,
        }
        try:
            write_text_atomic(json.dumps(body, indent=2, default=str), status_path(self.data_dir))
        except OSError as e:
            log.warning("could not write the live-data status: %s", e)


def build_failover(cfg, minutes, yahoo: YahooDelayedFeed, events=None) -> FailoverFeed:
    gw = shared_live(cfg)
    gw.wait_ready(3.0)
    index = str(cfg.get("backtest.index_ticker", "^AXJO"))
    return FailoverFeed(minutes, IBKRLiveFeed(minutes, gw, events, index), yahoo, cfg.data_dir,
                        events)  # fmt: skip


# --------------------------------------------------------------------------
# quotes
# --------------------------------------------------------------------------
class IBKRQuotes(QuoteProvider):
    name = "ibkr"

    def __init__(self, gateway: LiveGateway, index_ticker: str = "^AXJO"):
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
    """IBKR's quote while the connection is ready, else Yahoo's (each Quote's `source`
    says which; a v2 pre-open look on a Yahoo quote is labelled so, and the screen defers
    while the feed is down rather than call "no quote" a verdict)."""

    name = "ibkr+yfinance"

    def __init__(self, primary: IBKRQuotes, backup: QuoteProvider):
        self.primary, self.backup = primary, backup

    @property
    def live(self) -> bool:
        return self.primary.gw.ready

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
    gw = shared_live(cfg)
    return FailoverQuotes(IBKRQuotes(gw, str(cfg.get("backtest.index_ticker", "^AXJO"))), backup)


def session_time(now: datetime) -> time_cls:
    return now.astimezone(SYD).time()


__all__ = [
    "FALLBACK_LABEL", "FailoverFeed", "FailoverQuotes", "HISTORY_TRIES", "IBKRLiveFeed",
    "IBKRQuotes", "LIVE_LABEL", "PAUSED_LABEL", "build_failover", "market_hours",
    "quote_label", "quote_provider", "read_status", "status_path",
]  # fmt: skip
