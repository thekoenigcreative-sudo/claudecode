"""One persistent, self-healing IB Gateway connection for the watcher's lifetime (25 Sep 2026).

Rick, 25 Sep: "i want this fucking engine fixed, i don't want drop outs of the god damn api".
That morning the watcher connected and disconnected around batches of 90 historical
requests, a batch timed out at 07:30 and again at 10:17, the feed fell back to Yahoo, and
nothing brought it back. This module replaces that with:

  * ONE connection, opened read-only when the watcher starts and kept for its lifetime, run
    by its own thread on its own asyncio loop (ib_async is asyncio; the watcher is not);
  * a HEARTBEAT: reqCurrentTime every `heartbeat_s` (30 s). Two answers missed in a row, a
    socket error, a "connectivity lost" message (1100, 2110), or ib_async's own timeout (no
    message of any kind for two minutes) drops the connection and reconnects with backoff
    (1, 2, 4 ... 60 s) until it is back - whatever Gateway is doing;
  * STREAMING instead of polling: 5-second real-time bars (reqRealTimeBars) aggregated into
    1-minute bars here, and quotes (reqMktData) on demand. Every subscription is kept in a
    registry and re-requested after every reconnect, and after "connectivity restored, data
    lost" (1101). The minutes the stream missed are filled from one small historical
    request per stock;
  * the account's MARKET DATA LINE limit found from IBKR itself: error 101 ("max number of
    tickers") is the limit, remembered for the day; until IBKR says otherwise the default
    (config `market_data_lines`, 100) is used. The feed (ibkr/feed.py) rotates the scanner's
    universe inside it;
  * HISTORY through a paced queue: a few requests in flight at once, a minimum gap between
    them, retries with backoff, a pacing violation (162 "pacing" / 420) pausing the queue -
    never a batch of 90 at once. Prior sessions go to a local-disk cache
    (%LOCALAPPDATA%\\asx-bot\\ibkr\\history) so a restart does not fetch them twice;
  * STALENESS, measured: when the last bar or tick for a stock, or the index, or the last
    heartbeat answer is older than `max_data_age_s` in market hours, `entries_allowed`
    says no and why - the playbooks make no new entry on it (ibkr/feed.py, watch.py).

IBKR message codes acted on: 1100 connectivity lost; 1101 restored, data lost -> everything
re-subscribed; 1102 restored, data kept; 2103/2105/2157 a farm broken, 2104/2106/2158 back;
2110 Gateway's own link to IBKR broken; 101 the line limit; 162 with "pacing" / 420 a pacing
violation; 354/10089/10167/10168/10186/10187 no subscription; 200 no such security; 502/504
not connected; 10197 no data during a competing session (a phone or web login took the
data): treated as the feed being down until data flows again.

DATA ONLY: read-only connection, order methods replaced (gateway.disable_orders), and
tests/test_ibkr_no_orders.py fails the build if an order call appears here.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as time_cls
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from asxbot.ibkr.gateway import (
    FARM_BROKEN,
    FARM_OK,
    LOST,
    NO_PERMISSION,
    NO_SECURITY,
    NOT_CONNECTED,
    PACING,
    QUALIFY_RETRY_S,
    REAL_TIME_TYPES,
    RESTORED,
    Gateway,
    GatewaySettings,
    Health,
    Pacer,
    _num,
    bars_frame,
    disable_orders,
    end_of,
    live_data_type,
    market_hours,
)
from asxbot.log import get_logger

log = get_logger("asxbot.ibkr.live")
SYD = ZoneInfo("Australia/Sydney")

MAX_TICKERS = {101}  # "Max number of tickers has been reached"
COMPETING = {10197}  # no market data during a competing live session
CLIENT_ID_IN_USE = {326}  # "Unable to connect as the client id is already in use"
SESSION_START = time_cls(9, 55)  # today's bars are fetched from here (the auction is 09:59)
CLOSE = time_cls(16, 12)
GRACE_S = 12.0  # a minute is closed this long after it ends if no later bar has arrived
RT_BAR_S = 5
STARTUP_S = 10  # a stock just subscribed is not called stale until this long has passed
# IBKR's rule for real-time bars (reviewed 26 Sep 2026): a subscription counts against the
# market data lines AND the pacing for small bars - no more than 60 new real-time bar
# requests in any ten minutes. The first version asked for ~94 at once, every reconnect
# asked for all of them again and the rotation churned more; nothing counted them. Now at
# most RT_BUDGET in any RT_WINDOW_S, the index and the pinned stocks first; the rest are
# polled with small history requests until there is room.
RT_BUDGET = 50
RT_WINDOW_S = 600.0
RT_REFUSED_S = 600.0  # a stream IBKR refused (or that never delivered) is polled this long
DEAD_STREAM_S = 60.0  # subscribed this long in the session with no bar while the index has
STREAM_HOURS = (time_cls(10, 0), time_cls(16, 0))  # continuous trading: bars expected
DEAD_FROM = time_cls(10, 15)  # dead streams are judged from here (after the staggered open)
RT_REFUSALS = {162, 321, 322, 354, 366, 420, 10089, 10090, 10167, 10168, 10186, 10187, 200}


@dataclass
class Sub:
    code: str
    kind: str  # bars | quote
    handle: object = None
    since: float = 0.0  # clock() when first subscribed
    requested: float = 0.0  # clock() of the current request (this connection)
    last_at: float | None = None  # clock() of the last bar/tick
    contract: object = None


@dataclass
class HistJob:
    code: str
    duration: str
    end: datetime | None
    kind: str  # today | prior | catchup
    day: date | None = None
    tries: int = 0
    submitted: float = 0.0


@dataclass
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class MinuteAggregator:
    """5-second real-time bars into 1-minute bars, stamped with the minute they start.

    A minute is closed when a bar for a later minute arrives, or by `flush` once the minute
    has been over for GRACE_S with nothing later seen. A bar for a minute already closed is
    dropped (logged once per stock per day): the closed bar is what was handed out."""

    def __init__(self, code: str, scale: float = 1.0):
        self.code = code
        self.scale = scale
        self.bucket: datetime | None = None
        self.o = self.h = self.l = self.c = 0.0
        self.v = 0.0
        self.count = 0
        self.complete: list[Bar] = []
        self.dropped = 0
        self.closed_through: datetime | None = None
        # Minutes the stream saw only part of: the first after a (re)subscription that did
        # not start on the minute. History wins for these (live.py `_today_frame`).
        self.partial: set[datetime] = set()
        self.fresh = True

    def restart(self) -> None:
        """The stream stopped (a cancel, a lost connection): the minute being built is
        incomplete and is dropped; the next bar starts a fresh stretch."""
        self.bucket = None
        self.fresh = True

    def add(self, ts: datetime, o: float, h: float, lo: float, c: float, v: float) -> Bar | None:
        ts = ts.astimezone(SYD)
        b = ts.replace(second=0, microsecond=0)
        done = None
        if self.closed_through is not None and b <= self.closed_through:
            self.dropped += 1
            return None
        if self.bucket is not None and b > self.bucket:
            done = self._close()
        if self.bucket is None or b > self.bucket:
            if self.fresh and ts.second >= RT_BAR_S:
                self.partial.add(b)
            self.fresh = False
            self.bucket, self.o, self.h, self.l, self.c = b, o, h, lo, c
            self.v, self.count = max(0.0, v) * self.scale, 1
        elif b == self.bucket:
            self.h, self.l, self.c = max(self.h, h), min(self.l, lo), c
            self.v += max(0.0, v) * self.scale
            self.count += 1
        else:
            self.dropped += 1
        return done

    def flush(self, now: datetime) -> Bar | None:
        if self.bucket is None:
            return None
        if now.astimezone(SYD) >= self.bucket + timedelta(minutes=1, seconds=GRACE_S):
            return self._close()
        return None

    def _close(self) -> Bar:
        bar = Bar(self.bucket, self.o, self.h, self.l, self.c, self.v)
        self.complete.append(bar)
        self.closed_through = self.bucket
        self.bucket = None
        return bar

    def frame(self, day: date) -> pd.DataFrame | None:
        rows = [b for b in self.complete if b.ts.date() == day]
        if not rows:
            return None
        df = pd.DataFrame(
            [(b.ts, b.open, b.high, b.low, b.close, b.volume) for b in rows],
            columns=["ts", "open", "high", "low", "close", "volume"],
        ).drop_duplicates("ts", keep="last").set_index("ts").sort_index()
        df.index.name = None
        return df


def _make_ib():
    from ib_async import IB

    return IB()


class LiveGateway:
    """The one connection, on its own thread. Every public method is safe to call from the
    watcher's thread and never raises for a data problem."""

    def __init__(
        self, settings: GatewaySettings, ib_factory=_make_ib, clock=time.monotonic,
        wall=lambda: datetime.now(SYD), history_dir: Path | None = None,
    ):  # fmt: skip
        self.s = settings
        self.ib_factory = ib_factory
        self.clock = clock
        self.wall = wall
        self.history_dir = history_dir
        self.lock = threading.RLock()
        self.health = Health()
        self.ib = None
        self.contracts: dict = {}
        self._unqualified: dict[str, float] = {}
        self.unknown_codes: set[str] = set()
        self.subs: dict[str, Sub] = {}
        self.quotes: dict[str, Sub] = {}
        self.aggs: dict[str, MinuteAggregator] = {}
        self.volume_scale = 1.0
        self.today_hist: dict[tuple[str, date], tuple[pd.DataFrame, datetime]] = {}
        self.hist: dict[tuple[str, date], pd.DataFrame] = {}
        self.hist_failed: dict[tuple[str, date], int] = {}
        self.hist_given_up: set[tuple[str, date]] = set()
        self.hist_pending: set[tuple[str, str]] = set()  # (code, kind) queued or in flight
        self.line_limit = int(settings.market_data_lines)
        self.line_limit_from_ibkr = False
        self.pacer = Pacer(settings.max_requests_per_10min)
        self.pacing_until = 0.0
        self.heartbeat_at: float | None = None
        self.heartbeat_misses = 0
        self.last_data_at: float | None = None
        self.connected_since: datetime | None = None
        self.reconnects = 0
        self.connect_failures = 0
        self.last_connect_error = ""
        self.events: deque = deque(maxlen=300)
        self.requests_made = 0
        self.history_answered = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lost: asyncio.Event | None = None
        self._need_resub = False
        self._closing = False
        self._hist_q: asyncio.Queue | None = None
        self._req_codes: dict[int, str] = {}
        self._dead_reqs: set[int] = set()  # requests IBKR refused for want of a line (101)
        self._last_hist_at = 0.0
        self._started = threading.Event()
        # What the connection doctor (ibkr/doctor.py) reads and the hooks it pulls
        # (26 Sep 2026): when the socket went, a client-id clash (326), a competing session
        # (10197) with no data since, recent pacing violations, and a slowed history queue.
        self.disconnected_since: float | None = self.clock()
        self.clash_at: float | None = None
        self.competing_since: float | None = None
        self.pacing_times: deque = deque(maxlen=50)
        self.history_slow_until = 0.0
        self.history_slow_factor = 1.0
        self._kick = threading.Event()
        # the real-time bar budget and the streams IBKR refused (26 Sep 2026, RT_BUDGET)
        self.rt_stamps: deque = deque()
        self.rt_refused: dict[str, float] = {}
        self._rt_budget_noted = 0.0
        # what the feed actually watched, for MarketView.covered: per code, [start, end]
        # wall-clock spans of an unbroken stream (end None while it runs) and of today's
        # history answers (IBKR's history holds every traded minute of the span it answers)
        self.stream_spans: dict[str, list[list]] = {}
        self.hist_spans: dict[str, list[tuple]] = {}
        self.given_up_at: dict[tuple[str, date], float] = {}

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> LiveGateway:
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, name="ibkr-live", daemon=True)
        self._thread.start()
        self._started.wait(5)
        return self

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._wake)
        if self._thread is not None:
            self._thread.join(timeout)

    def _wake(self) -> None:
        if self._lost is not None:
            self._lost.set()

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except Exception as e:  # noqa: BLE001
            log.exception("the IBKR live thread died: %s", e)
            self._note(f"thread died: {e!r}")
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001
                pass
            loop.close()
            self._loop = None

    def _note(self, what: str) -> None:
        self.events.append((self.wall().isoformat(timespec="seconds"), what))

    # -- the loop -------------------------------------------------------------
    async def _main(self) -> None:
        self._hist_q = asyncio.Queue()
        self._lost = asyncio.Event()
        workers = [
            asyncio.create_task(self._history_worker(i))
            for i in range(max(1, int(self.s.history_concurrency)))
        ]
        flusher = asyncio.create_task(self._flusher())
        self._started.set()
        backoff = 1.0
        try:
            while not self._stop.is_set():
                if await self._connect_once():
                    backoff = 1.0
                    await self._session()
                    await self._teardown()
                    if not self._stop.is_set():
                        self.reconnects += 1
                        self._note("reconnecting")
                else:
                    self.connect_failures += 1
                await self._sleep_or_stop(backoff)
                backoff = min(backoff * 2, float(self.s.reconnect_backoff_max_s))
        finally:
            for t in [*workers, flusher]:
                t.cancel()
            await asyncio.gather(*workers, flusher, return_exceptions=True)
            await self._teardown()

    async def _sleep_or_stop(self, seconds: float) -> None:
        end = self.clock() + seconds
        while not self._stop.is_set() and self.clock() < end:
            if self._kick.is_set():  # a reconnect was asked for: try now, not after backoff
                self._kick.clear()
                return
            await asyncio.sleep(min(0.2, max(0.0, end - self.clock())))

    async def _connect_once(self) -> bool:
        ib = self.ib_factory()
        disable_orders(ib)
        ib.errorEvent += self._on_error
        ib.disconnectedEvent += self._on_disconnect
        if hasattr(ib, "timeoutEvent"):
            ib.timeoutEvent += self._on_timeout
        pacing_hits = self.health.pacing_hits
        self.health = Health(pacing_hits=pacing_hits)
        self._lost.clear()
        self._closing = False
        try:
            from ib_async import StartupFetchNONE

            fetch = StartupFetchNONE
        except ImportError:  # the fake in tests
            fetch = 0
        try:
            await ib.connectAsync(
                self.s.host, int(self.s.port), clientId=int(self.s.client_id),
                timeout=float(self.s.connect_timeout_s), readonly=True, fetchFields=fetch,
            )  # fmt: skip
        except Exception as e:  # noqa: BLE001 - refused, reset, timeout, handshake
            self.health = Health(refused=True, last_error=f"connect refused: {e!r}",
                                 pacing_hits=pacing_hits)  # fmt: skip
            if self.last_connect_error != str(e):
                log.warning("IB Gateway at %s:%s refused the connection (%r): not running or "
                            "logged out; trying again with backoff",
                            self.s.host, self.s.port, e)  # fmt: skip
            self.last_connect_error = str(e)
            self._note(f"connect failed: {e!r}")
            try:
                ib.disconnect()
            except Exception:  # noqa: BLE001
                pass
            return False
        self.ib = ib
        self.last_connect_error = ""
        self.disconnected_since = None
        self.clash_at = None
        self.competing_since = None  # a new session: the competing one is not assumed
        self.health.competing_at = ""
        self.health.connected = True
        self.health.refused = False
        if not self.health.last_error.startswith("lost"):
            self.health.server_ok = True
        try:
            if hasattr(ib, "setTimeout"):
                ib.setTimeout(float(self.s.heartbeat_s) * 4)
            ib.reqMarketDataType(live_data_type(self.wall()))
            self.health.requested_data_type = live_data_type(self.wall())
        except Exception as e:  # noqa: BLE001
            log.warning("IBKR: could not set the market data type: %r", e)
        self.heartbeat_at = self.clock()
        self.heartbeat_misses = 0
        self.connected_since = self.wall()
        log.info(
            "IB Gateway connected at %s:%s (client %s), read-only, persistent; link to IBKR %s",
            self.s.host, self.s.port, self.s.client_id, "up" if self.health.server_ok else "BROKEN",
        )  # fmt: skip
        self._note("connected")
        await self._resubscribe_all("connected")
        return True

    async def _session(self) -> None:
        """Heartbeat until the connection is lost."""
        ib = self.ib
        while not self._stop.is_set():
            waiter = asyncio.create_task(self._lost.wait())
            sleeper = asyncio.create_task(asyncio.sleep(float(self.s.heartbeat_s)))
            done, pending = await asyncio.wait(
                {waiter, sleeper}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
            if self._lost.is_set():
                self._note("connection lost")
                return
            if not ib.isConnected():
                self.health.last_error = self.health.last_error or "socket closed"
                self._note("socket closed")
                return
            if self._need_resub:
                self._need_resub = False
                await self._resubscribe_all("data lost (1101)")
            try:
                await asyncio.wait_for(ib.reqCurrentTimeAsync(), float(self.s.heartbeat_timeout_s))
                self.heartbeat_at = self.clock()
                self.heartbeat_misses = 0
                self.health.last_ok_at = self.wall().isoformat(timespec="seconds")
            except Exception as e:  # noqa: BLE001
                self.heartbeat_misses += 1
                log.warning("IBKR heartbeat %d of %d missed: %r", self.heartbeat_misses,
                            int(self.s.heartbeat_misses), e)  # fmt: skip
                if self.heartbeat_misses >= int(self.s.heartbeat_misses):
                    self.health.last_error = f"heartbeat: {self.heartbeat_misses} answers missed"
                    log.error("IBKR heartbeat missed %d times; reconnecting", self.heartbeat_misses)
                    self._note("heartbeat lost")
                    return
            want = live_data_type(self.wall())
            if self.health.requested_data_type != want:
                try:
                    ib.reqMarketDataType(want)
                    self.health.requested_data_type = want
                except Exception:  # noqa: BLE001
                    pass

    async def _teardown(self) -> None:
        ib, self.ib = self.ib, None
        self.health.connected = False
        self.heartbeat_at = None
        if self.disconnected_since is None:
            self.disconnected_since = self.clock()
        if ib is None:
            return
        self._closing = True
        try:
            ib.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self._closing = False
        for sub in [*self.subs.values(), *self.quotes.values()]:
            sub.handle = None  # re-requested on the next connection
        with self.lock:
            for code, agg in self.aggs.items():
                agg.restart()
                self._end_span(code)

    # -- callbacks (loop thread) ------------------------------------------------
    def _on_disconnect(self) -> None:
        self.health.connected = False
        if self._closing:
            return
        self.health.last_error = "Gateway closed the API connection"
        log.error("IB Gateway closed the API connection; reconnecting")
        if self._lost is not None:
            self._lost.set()

    def _on_timeout(self, idle: float = 0.0) -> None:
        self.health.last_error = f"no message from Gateway for {idle:.0f}s"
        log.error("IBKR: no message of any kind for %.0f s; reconnecting", idle)
        if self._lost is not None:
            self._lost.set()

    def _on_error(self, req_id, code, msg, contract=None) -> None:
        code = int(code)
        text = str(msg)
        if code in LOST:
            self.health.server_ok = False
            self.health.last_error = f"lost {code}: {text}"
            log.error("IB Gateway lost its link to IBKR (%s): %s", code, text)
            self._note(f"link lost ({code})")
        elif code in RESTORED:
            self.health.server_ok = True
            log.warning("IB Gateway's link to IBKR restored (%s): %s", code, text)
            self._note(f"link restored ({code})")
            if code == 1101:
                self._need_resub = True
                if self._lost is not None:
                    pass  # the session loop picks _need_resub up at its next beat
        elif code in FARM_BROKEN:
            self.health.farms_broken.add(text.split(":")[-1])
        elif code in FARM_OK:
            self.health.farms_broken.discard(text.split(":")[-1])
            self.health.server_ok = True
        elif code in NOT_CONNECTED:
            # 502/504: Gateway says we are not connected. Until 26 Sep this only marked the
            # feed not ready while heartbeats went on, so nothing reconnected: reconnect.
            self.health.connected = False
            self.health.last_error = f"{code}: {text}"
            if self._lost is not None:
                self._lost.set()
        elif code in PACING or (code == 162 and "pacing" in text.lower()):
            self.health.pacing_hits += 1
            self.pacing_times.append(self.clock())
            self.pacing_until = self.clock() + 30.0
            log.error("IBKR pacing violation (%s: %s); history paused for 30 s", code, text)
            self._note("pacing violation")
        elif code in MAX_TICKERS:
            self._dead_reqs.add(int(req_id))
            dead = self._req_codes.get(int(req_id))
            if dead in self.subs and self.subs[dead].handle is not None:
                self.subs.pop(dead)
            live = len([s for s in self.subs.values() if s.handle is not None]) + len(
                [q for q in self.quotes.values() if q.handle is not None]
            )
            self.line_limit = max(1, live)
            self.line_limit_from_ibkr = True
            log.error("IBKR: the market data line limit is reached (%s refused); %d "
                      "subscriptions fit, so the limit is %d from now on",
                      dead or f"request {req_id}", live, self.line_limit)  # fmt: skip
            self._note(f"line limit {self.line_limit}")
        elif code in COMPETING:
            self.health.last_error = f"{code}: {text}"
            if self.competing_since is None:
                self.competing_since = self.clock()
                self.health.competing_at = self.wall().isoformat(timespec="seconds")
            log.error("IBKR: no market data during a competing session (%s); data is paused "
                      "until it flows again", text)  # fmt: skip
            self._note("competing session")
        if code in CLIENT_ID_IN_USE or (int(req_id) < 0 and "already in use" in text.lower()):
            self.clash_at = self.clock()
            log.error("IBKR: client id %s is already in use (%s)", self.s.client_id, text)
            self._note(f"client id {self.s.client_id} in use")
        if code in NO_PERMISSION:
            self.health.permission_denied = True
        if code in NO_SECURITY:
            bad = self._req_codes.get(int(req_id))
            if bad:
                self.unknown_codes.add(bad)
                self._unqualified[bad] = self.clock()
        streamed = self._req_codes.get(int(req_id)) if int(req_id) > 0 else None
        if streamed and code in RT_REFUSALS and code not in MAX_TICKERS:
            # A real-time bar request IBKR refused (pacing, no permission, no security...):
            # until 26 Sep its handle stayed set, so the stock counted as streaming and was
            # never polled. Drop it to polling for a while.
            self._drop_stream(streamed, f"{code}: {text[:80]}")
        if int(req_id) > 0:
            self.health.last_request_error = f"{code}: {text}"

    def _drop_stream(self, code: str, why: str) -> None:
        """Loop thread: stop treating `code` as streaming; it is polled instead."""
        sub = self.subs.pop(code, None)
        self.rt_refused[code] = self.clock()
        if sub is not None and sub.handle is not None and self.ib is not None:
            try:
                self.ib.cancelRealTimeBars(sub.handle)
            except Exception:  # noqa: BLE001
                pass
        with self.lock:
            agg = self.aggs.get(code)
            if agg is not None:
                agg.restart()
            self._end_span(code)
        log.warning("IBKR: %s is polled instead of streamed for %d min (%s)", code,
                    int(RT_REFUSED_S // 60), why)  # fmt: skip
        self._note(f"stream dropped {code}")

    # -- contracts --------------------------------------------------------------
    async def _qualify(self, code: str):
        code = code.upper()
        if code in self.contracts:
            return self.contracts[code]
        failed = self._unqualified.get(code)
        if failed is not None and self.clock() - failed < QUALIFY_RETRY_S:
            return None
        if self.ib is None:
            return None
        try:
            c = Gateway.contract(code)
        except ValueError:
            return None
        try:
            got = await asyncio.wait_for(
                self.ib.qualifyContractsAsync(c), timeout=float(self.s.request_timeout_s)
            )
        except Exception as e:  # noqa: BLE001
            log.warning("IBKR could not qualify %s: %r", code, e)
            return None
        q = got[0] if got else None
        why = Gateway._accept(code, q)
        if why:
            if self.health.farms_broken or not self.health.server_ok:
                # A sec-def farm hiccup is not an answer (26 Sep 2026): asked again in a
                # minute, and the stock is not called unknown for the day.
                self._unqualified[code] = self.clock() - QUALIFY_RETRY_S + 60.0
                log.warning("IBKR could not qualify %s while a farm is broken (%s); asking "
                            "again in a minute", code, why)  # fmt: skip
                return None
            self._unqualified[code] = self.clock()
            self.unknown_codes.add(code)
            log.warning("IBKR contract for %s refused (%s); not asked again for an hour", code, why)
            return None
        self._unqualified.pop(code, None)
        self.unknown_codes.discard(code)
        self.contracts[code] = q
        return q

    # -- subscriptions -------------------------------------------------------
    async def _resubscribe_all(self, why: str, codes=None) -> None:
        """Every stream again (after a reconnect or 1101), inside the real-time bar budget
        and in the order they were subscribed (the index and the pinned first); a stream
        that does not fit is dropped to polling and the rotation brings it back. `codes`:
        only these (the connection doctor re-requesting the index)."""
        if self.ib is None:
            return
        n = 0
        wanted = None if codes is None else {c.upper() for c in codes}
        for code, sub in list(self.subs.items()):
            if wanted is not None and code not in wanted:
                continue
            if await self._subscribe_bars(sub):
                n += 1
                self._queue_catchup(code)
            else:
                self.subs.pop(code, None)
        for sub in list(self.quotes.values()):
            if await self._subscribe_quote(sub):
                n += 1
        if self.subs or self.quotes:
            log.info("IBKR: %d subscription(s) requested again (%s)", n, why)
            self._note(f"resubscribed {n} ({why})")

    async def _subscribe_bars(self, sub: Sub) -> bool:
        ib = self.ib
        if ib is None:
            return False
        if sub.handle is not None:
            try:
                ib.cancelRealTimeBars(sub.handle)
            except Exception:  # noqa: BLE001
                pass
            sub.handle = None
        with self.lock:
            agg = self.aggs.get(sub.code)
            if agg is not None:
                agg.restart()
            self._end_span(sub.code)
        c = await self._qualify(sub.code)
        if c is None:
            return False
        if self._rt_room() <= 0:
            if self.clock() - self._rt_budget_noted > 60:
                self._rt_budget_noted = self.clock()
                log.info("IBKR: the real-time bar budget (%d in 10 min) is spent; %s and any "
                         "others are polled until there is room", RT_BUDGET, sub.code)  # fmt: skip
            return False
        try:
            handle = ib.reqRealTimeBars(c, RT_BAR_S, "TRADES", False)
        except Exception as e:  # noqa: BLE001
            log.warning("IBKR real-time bars for %s could not be requested: %r", sub.code, e)
            return False
        self.rt_stamps.append(self.clock())
        code = sub.code

        def on_update(bars, has_new, _code=code):
            self._on_rt_bar(_code, bars, has_new)

        req_id = int(getattr(handle, "reqId", 0) or 0)
        self._req_codes[req_id] = code
        if req_id in self._dead_reqs:  # refused for want of a line before we got here
            self.subs.pop(code, None)
            return False
        handle.updateEvent += on_update
        sub.handle = handle
        sub.contract = c
        sub.requested = self.clock()
        sub.last_at = None
        return True

    def _rt_room(self) -> int:
        now = self.clock()
        while self.rt_stamps and now - self.rt_stamps[0] >= RT_WINDOW_S:
            self.rt_stamps.popleft()
        return RT_BUDGET - len(self.rt_stamps)

    async def _unsubscribe_bars(self, code: str) -> None:
        sub = self.subs.pop(code, None)
        with self.lock:
            agg = self.aggs.get(code)
            if agg is not None:
                agg.restart()
            self._end_span(code)
        if sub is None or sub.handle is None or self.ib is None:
            return
        try:
            self.ib.cancelRealTimeBars(sub.handle)
        except Exception:  # noqa: BLE001
            pass

    async def _subscribe_quote(self, sub: Sub) -> bool:
        ib = self.ib
        if ib is None:
            return False
        if sub.handle is not None and sub.contract is not None:
            try:  # re-requested on a live connection (the doctor): cancel the old one first
                ib.cancelMktData(sub.contract)
            except Exception:  # noqa: BLE001
                pass
            sub.handle = None
        c = await self._qualify(sub.code)
        if c is None:
            return False
        try:
            tk = ib.reqMktData(c, "" if sub.code.startswith("^") else self.s.generic_ticks)
        except Exception as e:  # noqa: BLE001
            log.warning("IBKR quote stream for %s could not be requested: %r", sub.code, e)
            return False
        sub.handle, sub.contract, sub.requested = tk, c, self.clock()
        return True

    async def _set_streaming(self, wanted: list[str]) -> None:
        now = self.clock()
        self._drop_dead_streams(wanted[:1])
        refused = {c for c, t in self.rt_refused.items() if now - t < RT_REFUSED_S}
        wanted = [c for c in dict.fromkeys(c.upper() for c in wanted) if c not in refused]
        room = max(0, self.line_limit - int(self.s.stream_reserve_lines) - len(self.quotes))
        keep = wanted[:room]
        for code in list(self.subs):
            if code not in keep:
                sub = self.subs[code]
                if now - sub.since < float(self.s.min_dwell_s):
                    continue  # not churned: it stays until it has had its dwell
                await self._unsubscribe_bars(code)
        for code in keep:
            sub = self.subs.get(code)
            if sub is not None and sub.handle is not None:
                continue
            if sub is not None:  # its request failed earlier (the budget): try again
                if not await self._subscribe_bars(sub):
                    if self._rt_room() <= 0:
                        break
                    self.subs.pop(code, None)
                else:
                    self._queue_catchup(code)
                continue
            if self._rt_room() <= 0:
                break
            room = max(0, self.line_limit - int(self.s.stream_reserve_lines) - len(self.quotes))
            if len(self.subs) >= room:
                break
            sub = Sub(code, "bars", since=now)
            self.subs[code] = sub
            if await self._subscribe_bars(sub):
                self._queue_catchup(code)
            else:
                self.subs.pop(code, None)

    def _drop_dead_streams(self, index_first) -> None:
        """In the continuous session, a stream subscribed for DEAD_STREAM_S with not one bar
        while the index's stream delivers is dead (a refusal that came back unmapped, a
        contract with no real-time bars): it is dropped to polling (26 Sep 2026)."""
        now_wall = self.wall()
        # Not before 10:15: the ASX opens in five groups from 10:00 to ~10:09 (S-Z last), and a
        # stock that has not traded yet is not a dead stream.
        if not (market_hours(now_wall) and DEAD_FROM <= now_wall.time() < STREAM_HOURS[1]):
            return
        index = index_first[0].upper() if index_first else None
        isub = self.subs.get(index) if index else None
        if isub is None or isub.last_at is None or self.clock() - isub.last_at > 30:
            return  # without a live index there is no telling a dead stream from a quiet one
        now = self.clock()
        for code, sub in list(self.subs.items()):
            if code == index or sub.handle is None or sub.last_at is not None:
                continue
            if now - sub.requested >= DEAD_STREAM_S:
                self._drop_stream(code, f"no bar in {int(now - sub.requested)}s of streaming")

    def _begin_span(self, code: str, at: datetime) -> None:
        spans = self.stream_spans.setdefault(code, [])
        if not spans or spans[-1][1] is not None:
            spans.append([at, None])

    def _end_span(self, code: str) -> None:
        spans = self.stream_spans.get(code)
        if spans and spans[-1][1] is None:
            spans[-1][1] = self.wall()

    def covered(self, code: str, start: datetime, end: datetime) -> bool:
        """Did this connection watch `code` over [start, end]: inside one unbroken stream,
        or one of today's history answers (which hold every traded minute of their span)?
        A gap in the bars there is then real - the stock did not trade."""
        code = code.upper()
        start, end = start.astimezone(SYD), end.astimezone(SYD)
        now = self.wall()
        with self.lock:
            spans = [(a, b or now) for a, b in self.stream_spans.get(code, [])]
            spans += list(self.hist_spans.get(code, []))
        if not spans:
            return False
        spans.sort()
        slack = timedelta(minutes=1)
        merged = [list(spans[0])]
        for a, b in spans[1:]:
            if a <= merged[-1][1] + slack:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        return any(a <= start + slack and end <= b + slack for a, b in merged)

    def _queue_catchup(self, code: str) -> None:
        """Today's bars up to now, so the stream's minutes join the ones before them."""
        now = self.wall()
        if now.time() < SESSION_START or now.weekday() >= 5:
            return
        self.queue_history(code, "today", now.date())

    # -- real-time bars ----------------------------------------------------------
    def _on_rt_bar(self, code: str, bars, has_new: bool) -> None:
        if not bars:
            return
        b = bars[-1]
        try:
            ts = (b.time if isinstance(b.time, datetime)
                  else datetime.fromtimestamp(float(b.time), SYD))  # fmt: skip
        except (TypeError, ValueError):
            return
        agg = self.aggs.get(code)
        if agg is None:
            agg = self.aggs[code] = MinuteAggregator(code, self.volume_scale)
        with self.lock:
            if agg.fresh:
                # the stream's watch starts with its first complete minute
                first = ts.astimezone(SYD).replace(second=0, microsecond=0)
                self._begin_span(code, first if ts.astimezone(SYD).second < RT_BAR_S
                                 else first + timedelta(minutes=1))  # fmt: skip
            agg.add(ts, float(b.open_), float(b.high), float(b.low), float(b.close),
                    float(getattr(b, "volume", 0.0) or 0.0))  # fmt: skip
            sub = self.subs.get(code)
            if sub is not None:
                sub.last_at = self.clock()
            self.last_data_at = self.clock()
            if self.competing_since is not None:  # data flows again: the session is ours
                self.competing_since = None
                self.health.competing_at = ""
            self.health.last_ok_at = self.wall().isoformat(timespec="seconds")
        if len(bars) > 4:
            del bars[:-2]  # ib_async keeps every bar; two are enough

    async def _flusher(self) -> None:
        while True:
            await asyncio.sleep(RT_BAR_S)
            now = self.wall()
            with self.lock:
                for agg in self.aggs.values():
                    agg.flush(now)

    # -- history ----------------------------------------------------------------
    def queue_history(self, code: str, kind: str, day: date, sessions: int = 0) -> bool:
        """Queue one small historical request (thread-safe). kind: today (today's bars from
        09:55, or from the last minute held) | prior (the `sessions` sessions before `day`,
        one request, cached on disk). Returns False if already queued or given up on."""
        key = (code.upper(), kind)
        if key in self.hist_pending:
            return False
        if kind == "prior" and (code.upper(), day) in self.hist_given_up:
            return False
        loop = self._loop
        if loop is None or self._hist_q is None:
            return False
        if kind == "today":
            # The window is worked out when the request is SENT (`_today_duration`), not
            # when it is queued: a job that waited behind pacing asked for too short a
            # window and left a hole in the morning (26 Sep 2026).
            job = HistJob(code.upper(), "", None, "today", day)
        else:
            job = HistJob(code.upper(), f"{int(sessions) + 1} D", end_of(day), "prior", day)
        self.hist_pending.add(key)
        job.submitted = self.clock()
        loop.call_soon_threadsafe(self._hist_q.put_nowait, job)
        return True

    async def _history_worker(self, n: int) -> None:
        while True:
            job: HistJob = await self._hist_q.get()
            try:
                await self._run_history(job)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("history job %s %s failed: %s", job.code, job.kind, e)
                self.hist_pending.discard((job.code, job.kind))
            finally:
                self._hist_q.task_done()

    async def _wait_pacing(self) -> None:
        while True:
            now = self.clock()
            slow = self.history_slow_factor if now < self.history_slow_until else 1.0
            gap = self._last_hist_at + float(self.s.history_min_interval_s) * slow - now
            wait = max(self.pacing_until - now, gap)
            if self.pacer.room(now) <= 0:
                wait = max(wait, 5.0)
            if wait <= 0:
                self._last_hist_at = self.clock()
                self.pacer.spend(1, self._last_hist_at)
                return
            await asyncio.sleep(min(wait, 5.0))

    async def _run_history(self, job: HistJob) -> None:
        key = (job.code, job.kind)
        while self.ib is None or not self.health.server_ok:
            if self._stop.is_set():
                self.hist_pending.discard(key)
                return
            await asyncio.sleep(1.0)
        await self._wait_pacing()
        since = None
        if job.kind == "today":
            since, job.duration = self._today_duration(job.code, job.day)
        got, err = await self._history(job.code, job.duration, job.end)
        if got is None and err is not None:
            job.tries += 1
            if job.tries < 3 and not self._stop.is_set():
                await asyncio.sleep(2.0 * job.tries)
                self._hist_q.put_nowait(job)  # tried again after a pause
                return
            self.hist_pending.discard(key)
            if job.kind == "prior" and job.day is not None:
                n = self._failed(job.code, job.day)
                if n >= 3:
                    self.hist_given_up.add((job.code, job.day))
                    self.given_up_at[(job.code, job.day)] = self.clock()
                    log.warning("IBKR history for %s: given up after %d failed attempts (%s)",
                                job.code, n, err)  # fmt: skip
            return
        self.hist_pending.discard(key)
        self.history_answered += 1
        df = bars_frame(got) if got else None
        if df is not None and len(df) and self.competing_since is not None:
            self.competing_since = None  # data answered: the session is ours again
            self.health.competing_at = ""
        if job.kind == "today":
            fetched = self.wall()
            if df is not None and len(df):
                part = df[[d == job.day for d in df.index.date]]
                with self.lock:
                    old = self.today_hist.get((job.code, job.day))
                    if old is not None and len(old[0]):
                        # merged, the new answer winning where both hold a minute: an old
                        # minute the new window did not reach is kept (26 Sep 2026)
                        part = pd.concat([old[0], part])
                        part = part[~part.index.duplicated(keep="last")].sort_index()
                    self.today_hist[(job.code, job.day)] = (part, fetched)
            if since is not None and got is not None:
                # an answered span (bars or none): every traded minute in it is held. The
                # minute forming at the answer is not final, so the span ends before it.
                end = fetched.replace(second=0, microsecond=0) - timedelta(minutes=1)
                if end > since:
                    with self.lock:
                        self.hist_spans.setdefault(job.code, []).append((since, end))
            return
        # prior sessions: split by day, keep in memory, write to the local cache
        if df is None or not len(df):
            n = self._failed(job.code, job.day)
            if n >= 3:
                self.hist_given_up.add((job.code, job.day))
                self.given_up_at[(job.code, job.day)] = self.clock()
            return
        with self.lock:
            for d in sorted({x for x in df.index.date if x < job.day}):
                part = df[[x == d for x in df.index.date]]
                if len(part):
                    self.hist[(job.code, d)] = part
                    self._write_cache(job.code, d, part)
            self.hist_failed.pop((job.code, job.day), None)
            self.hist[(job.code, job.day)] = self.hist.get((job.code, job.day), pd.DataFrame())
            self.hist_done_marker(job.code, job.day)

    def _today_duration(self, code: str, day: date) -> tuple[datetime, str]:
        """(since, IBKR duration) for today's bars of `code`, from the last minute held."""
        have = self._today_frame(code, day)
        since = (have.index.max().to_pydatetime() if have is not None and len(have)
                 else datetime.combine(day, SESSION_START, tzinfo=SYD))  # fmt: skip
        secs = int((self.wall() - since).total_seconds()) + 180
        return since.astimezone(SYD), f"{max(300, min(secs, 86_400))} S"

    def retry_given_up(self, after_s: float = 900.0) -> int:
        """Prior sessions given up on are asked for again after `after_s`: an empty answer
        is often a timeout (ib_async returns [] on one), not "no history" (26 Sep 2026)."""
        now = self.clock()
        back = [k for k, t in self.given_up_at.items() if now - t >= after_s]
        for k in back:
            self.given_up_at.pop(k, None)
            self.hist_given_up.discard(k)
            self.hist_failed.pop(k, None)
        return len(back)

    def _failed(self, code: str, day: date) -> int:
        n = self.hist_failed[(code, day)] = self.hist_failed.get((code, day), 0) + 1
        return n

    def hist_done_marker(self, code: str, day: date) -> None:
        self.hist.setdefault(("_done", day), set()).add(code)

    def history_done(self, code: str, day: date) -> bool:
        return code.upper() in self.hist.get(("_done", day), set())

    async def _history(self, code: str, duration: str, end: datetime | None):
        """One historical request. Returns (bars, None) or (None, error text)."""
        if self.ib is None:
            return None, "not connected"
        c = await self._qualify(code)
        if c is None:
            return None, f"no IBKR contract for {code}"
        self.requests_made += 1
        try:
            got = await self.ib.reqHistoricalDataAsync(
                c, end or "", duration, "1 min", "TRADES", False, formatDate=2,
                timeout=float(self.s.request_timeout_s),
            )  # fmt: skip
        except Exception as e:  # noqa: BLE001
            log.warning("IBKR bars for %s (%s) failed: %r", code, duration, e)
            return None, repr(e)
        return list(got or []), None

    def history_sync(self, code: str, duration: str, end: datetime | None = None,
                     timeout: float | None = None) -> pd.DataFrame | None:  # fmt: skip
        """A blocking historical request from another thread (the check, the replay's
        fetcher, a one-off need). Paced like the rest."""
        loop = self._loop
        if loop is None:
            return None

        async def run():
            await self._wait_pacing()
            got, _ = await self._history(code, duration, end)
            return bars_frame(got) if got else None

        fut = asyncio.run_coroutine_threadsafe(run(), loop)
        try:
            return fut.result(timeout or float(self.s.request_timeout_s) * 2 + 30)
        except Exception as e:  # noqa: BLE001
            log.warning("IBKR history_sync %s %s: %r", code, duration, e)
            return None

    def _cache_path(self, code: str, day: date) -> Path | None:
        """<history_dir>/<code>/<day>.parquet; a code that is a Windows device name (PRN)
        gets the suffix every cache here uses (asxbot.io.safe_stem) - the plain name cannot
        be a folder, and on the night of 25 Sep it killed a fetch worker."""
        if self.history_dir is None:
            return None
        from asxbot.io import safe_stem

        return self.history_dir / safe_stem(code.upper()) / f"{day.isoformat()}.parquet"

    def _write_cache(self, code: str, day: date, df: pd.DataFrame) -> None:
        p = self._cache_path(code, day)
        if p is None:
            return
        try:
            from asxbot.io import write_parquet_atomic

            write_parquet_atomic(df, p)
        except OSError as e:
            log.warning("could not cache IBKR history for %s %s: %s", code, day, e)

    def cached_history(self, code: str, day: date) -> pd.DataFrame | None:
        """A prior session's bars: memory, then the local-disk cache."""
        key = (code.upper(), day)
        with self.lock:
            df = self.hist.get(key)
        if df is not None and len(df):
            return df
        p = self._cache_path(code, day)
        if p is not None and p.exists():
            try:
                df = pd.read_parquet(p)
                if df.index.tz is None:
                    df.index = df.index.tz_localize(SYD)
                with self.lock:
                    self.hist[key] = df
                return df
            except Exception as e:  # noqa: BLE001
                log.warning("unreadable IBKR history cache %s: %s", p, e)
        return None

    # -- what the feed reads ------------------------------------------------------
    def _today_frame(self, code: str, day: date) -> pd.DataFrame | None:
        """Today's minutes: history (its newest, possibly forming, minute dropped) as the
        base, the stream's completed minutes over it, minute by minute. A minute the stream
        saw only part of (the first after a (re)subscription) gives way to history's.

        Until 26 Sep 2026 history was kept only BEFORE the first streamed minute, so every
        answer after it - the catch-up after a reconnect, the polls of a stock rotated out
        of streaming - was thrown away and a hole stayed in the bars for the day: stops
        inside it were never seen, and the scanner read the hole as a halt."""
        with self.lock:
            hist = self.today_hist.get((code, day))
            agg = self.aggs.get(code)
            live = agg.frame(day) if agg is not None else None
            partial = set(agg.partial) if agg is not None else set()
        base = None
        if hist is not None:
            df, fetched = hist
            if len(df):
                if fetched.time() < CLOSE and fetched.date() == day:
                    forming = fetched.replace(second=0, microsecond=0) - timedelta(minutes=1)
                    df = df[df.index < forming]
                base = df
        if live is not None and len(live):
            if base is not None and len(base):
                held = set(base.index)
                live = live[[not (ts in partial and ts in held) for ts in live.index]]
                out = pd.concat([base, live])
            else:
                out = live
        else:
            out = base
        if out is None or not len(out):
            return None
        out = out.sort_index()
        return out[~out.index.duplicated(keep="last")]

    def bars_today(self, code: str, day: date) -> pd.DataFrame | None:
        return self._today_frame(code.upper(), day)

    def today_fetched_at(self, code: str, day: date) -> datetime | None:
        """When today's bars of a stock without a line were last answered by IBKR."""
        with self.lock:
            hist = self.today_hist.get((code.upper(), day))
        return None if hist is None else hist[1]

    def streaming(self, code: str) -> bool:
        sub = self.subs.get(code.upper())
        return sub is not None and sub.handle is not None

    def set_streaming(self, codes: list[str]) -> None:
        """Ask for these to stream (in priority order), inside the line limit; the rest of
        the current subscriptions are released once they have had their dwell. Non-blocking."""
        loop = self._loop
        if loop is None or self.ib is None:
            return
        fut = asyncio.run_coroutine_threadsafe(self._set_streaming(list(codes)), loop)
        try:
            fut.result(timeout=10.0)
        except Exception as e:  # noqa: BLE001 - the rotation is finished next cycle
            log.warning("IBKR: the streaming rotation did not finish in time: %r", e)

    def data_age_s(self, code: str | None = None) -> float | None:
        """Seconds since the last bar/tick for `code` (or for anything, with None)."""
        if code is None:
            at = self.last_data_at
        else:
            sub = self.subs.get(code.upper())
            at = sub.last_at if sub is not None else None
            if sub is not None and at is None:
                return None if self.clock() - sub.since < STARTUP_S else self.clock() - sub.since
        return None if at is None else self.clock() - at

    def heartbeat_age_s(self) -> float | None:
        return None if self.heartbeat_at is None else self.clock() - self.heartbeat_at

    @property
    def ready(self) -> bool:
        ib = self.ib
        if ib is None or not self.health.connected or not self.health.server_ok:
            return False
        age = self.heartbeat_age_s()
        limit = float(self.s.heartbeat_s) * int(self.s.heartbeat_misses) + float(
            self.s.heartbeat_timeout_s
        )
        return age is not None and age <= limit

    def entries_allowed(
        self, now: datetime, codes: list[str] = (), index: str = "^AXJO"
    ) -> tuple[bool, str]:
        """May a playbook make a NEW entry on this feed right now? Not when the connection is
        down, when the heartbeat is stale, or - in market hours - when the index's or the
        named stocks' streamed data is older than `max_data_age_s`."""
        if not self.ready:
            return False, f"live feed down: {self.health.last_error or 'IB Gateway not ready'}"
        if self.competing:
            return False, ("live feed down: IBKR sends no market data while another session "
                           "(the phone app or the website) holds it (10197)")  # fmt: skip
        max_age = float(self.s.max_data_age_s)
        # Judged 10:00-16:00: in the closing auction (16:00-16:10) nothing trades, so every
        # stream falls silent and "stale" would be a false alarm (26 Sep 2026).
        if market_hours(now) and now.astimezone(SYD).time() < STREAM_HOURS[1]:
            for code in (index, *codes):
                if not self.streaming(code):
                    continue
                age = self.data_age_s(code)
                if age is not None and age > max_age:
                    what = "the index" if code == index else code
                    return False, f"stale: no bar from IBKR for {what} for {age:.0f}s"
        return True, ""

    # -- quotes --------------------------------------------------------------
    async def _quote(self, code: str, wait_s: float) -> dict | None:
        ib = self.ib
        if ib is None:
            return None
        c = await self._qualify(code)
        if c is None:
            return None
        streamed = self.quotes.get(code.upper())
        tk = streamed.handle if streamed is not None and streamed.handle is not None else None
        own = tk is None
        if own:
            # ib_async keeps one Ticker per contract and hands the SAME object back on the
            # next reqMktData, old values and all, so a second quote of a stock returned the
            # first one's bid/ask/last/type at once (26 Sep 2026). Forget it first.
            self._forget_ticker(ib, c)
            try:
                tk = ib.reqMktData(c, "" if code.startswith("^") else self.s.generic_ticks)
            except Exception as e:  # noqa: BLE001
                log.warning("IBKR quote for %s failed: %r", code, e)
                return None
        try:
            deadline = self.clock() + float(wait_s)
            while self.clock() < deadline:
                have_px = _num(tk.last) or _num(tk.close)
                have_book = code.startswith("^") or (_num(tk.bid) and _num(tk.ask))
                if have_px and (have_book or not market_hours(self.wall())):
                    break
                await asyncio.sleep(0.2)
        finally:
            if own:
                try:
                    ib.cancelMktData(c)
                except Exception:  # noqa: BLE001
                    pass
        mdt = getattr(tk, "marketDataType", None)
        if own:
            self._forget_ticker(ib, c)
        q = {
            "code": code,
            "bid": _num(tk.bid), "bid_size": _num(tk.bidSize),
            "ask": _num(tk.ask), "ask_size": _num(tk.askSize),
            "last": _num(tk.last), "last_size": _num(tk.lastSize),
            "open": _num(tk.open), "prev_close": _num(tk.close), "volume": _num(tk.volume),
            "halted": _num(getattr(tk, "halted", None)),
            "auction_price": _num(getattr(tk, "auctionPrice", None)),
            "auction_volume": _num(getattr(tk, "auctionVolume", None)),
            "market_data_type": int(mdt) if mdt else None,
            "time": str(getattr(tk, "time", "") or ""),
        }  # fmt: skip
        if q["last"] is None and q["prev_close"] is None:
            return None
        self.health.market_data_type = q["market_data_type"]
        if q["market_data_type"] in REAL_TIME_TYPES:
            self.health.permission_denied = False
            if self.competing_since is not None:  # a real-time answer: the data is ours
                self.competing_since = None
                self.health.competing_at = ""
        self.last_data_at = self.clock()
        self.health.last_ok_at = self.wall().isoformat(timespec="seconds")
        return q

    @staticmethod
    def _forget_ticker(ib, contract) -> None:
        tickers = getattr(getattr(ib, "wrapper", None), "tickers", None)
        if isinstance(tickers, dict):
            try:
                tickers.pop(hash(contract), None)
            except TypeError:
                pass

    def quote(self, code: str, wait_s: float | None = None) -> dict | None:
        """One quote (bid/ask/last/sizes, open, previous close, halt flag, the auction's
        indicative price and volume), blocking up to `quote_wait_s`. None if not ready."""
        loop = self._loop
        if loop is None or not self.ready:
            return None
        wait = float(self.s.quote_wait_s if wait_s is None else wait_s)
        fut = asyncio.run_coroutine_threadsafe(self._quote(code.upper(), wait), loop)
        try:
            return fut.result(wait + 10)
        except Exception as e:  # noqa: BLE001
            log.warning("IBKR quote for %s: %r", code, e)
            return None

    # -- status ---------------------------------------------------------------
    def status(self) -> dict:
        """Never raises: the loop thread changes these dicts while this reads them."""
        for _ in range(3):
            try:
                return self._status()
            except RuntimeError:  # "changed size during iteration"
                time.sleep(0.01)
        return {**self.health.to_dict(), "ready": self.ready, "status": "unreadable"}

    def _status(self) -> dict:
        subs = [c for c, s in list(self.subs.items()) if s.handle is not None]
        now = self.clock()
        with_bar = sum(1 for s in list(self.subs.values())
                       if s.last_at is not None and now - s.last_at <= 60)  # fmt: skip
        hb = self.heartbeat_age_s()
        data = self.data_age_s()
        return {
            **self.health.to_dict(),
            "ready": self.ready,
            "heartbeat_age_s": None if hb is None else round(hb, 1),
            "last_data_age_s": None if data is None else round(data, 1),
            "connected_since": (self.connected_since.isoformat(timespec="seconds")
                                if self.connected_since else None),  # fmt: skip
            "reconnects": self.reconnects,
            "connect_failures": self.connect_failures,
            "streaming": len(subs),
            "streams_with_a_bar_in_60s": with_bar,
            "rt_requests_10min": RT_BUDGET - self._rt_room(),
            "polled_not_streamed": sorted(c for c, t in list(self.rt_refused.items())
                                          if now - t < RT_REFUSED_S)[:50],  # fmt: skip
            "streaming_codes": sorted(subs)[:400],
            "quote_streams": len(self.quotes),
            "line_limit": self.line_limit,
            "line_limit_from_ibkr": self.line_limit_from_ibkr,
            "history_queued": self._hist_q.qsize() if self._hist_q is not None else 0,
            "history_answered": self.history_answered,
            "requests_made": self.requests_made,
            "given_up": sorted(f"{c} {d}" for c, d in self.hist_given_up)[:50],
            "unknown_codes": sorted(self.unknown_codes)[:50],
            "volume_scale": self.volume_scale,
            "client_id": self.s.client_id,
            "competing": self.competing,
            "client_id_clash": self.clash_recent(),
            "pacing_recent": self.pacing_recent(),
            "disconnected_for_s": round(self.disconnected_for_s(), 1),
            "recent": list(self.events)[-12:],
        }

    # -- recovery hooks (the connection doctor, ibkr/doctor.py) --------------------
    @property
    def competing(self) -> bool:
        """IBKR said 10197 (a competing session holds the market data) and no bar has
        arrived since."""
        return self.competing_since is not None

    def disconnected_for_s(self) -> float:
        return 0.0 if self.disconnected_since is None else self.clock() - self.disconnected_since

    def clash_recent(self, within_s: float = 300.0) -> bool:
        return self.clash_at is not None and self.clock() - self.clash_at <= within_s

    def pacing_recent(self, within_s: float = 600.0) -> int:
        now = self.clock()
        return sum(1 for t in self.pacing_times if now - t <= within_s)

    def request_reconnect(self, why: str) -> bool:
        """Drop the socket and connect again (backoff 1 s): every subscription is
        re-requested and the missed minutes fetched (the same path as a lost connection)."""
        loop = self._loop
        if loop is None:
            return False

        def go():
            log.warning("IBKR: reconnecting on request (%s)", why)
            self._note(f"reconnect asked ({why})")
            if self._lost is not None:
                self._lost.set()

        self._kick.set()  # if it is between tries, the next try starts now
        loop.call_soon_threadsafe(go)
        return True

    def request_resubscribe(self, why: str, codes=None) -> bool:
        """Cancel and re-request streams on the live connection (all, or `codes`), inside
        the real-time bar budget. Does not wait: the watcher's thread is not held up."""
        loop = self._loop
        if loop is None or self.ib is None:
            return False
        fut = asyncio.run_coroutine_threadsafe(self._resubscribe_all(why, codes), loop)
        fut.add_done_callback(lambda f: f.exception() and log.warning(
            "IBKR: re-requesting the streams failed: %r", f.exception()))  # fmt: skip
        return True

    def ensure_alive(self) -> bool:
        """Restart the connection's thread if it died (an exception out of its loop)."""
        if self._stop.is_set():
            return False
        if self._thread is None or not self._thread.is_alive():
            log.error("IBKR: the live connection's thread was not running; starting it again")
            self._note("thread restarted")
            self._thread = None
            self.start()
            return True
        return False

    def use_client_id(self, client_id: int, why: str) -> bool:
        """Connect again under another client id (a stale connection holds ours)."""
        import dataclasses

        old = self.s.client_id
        self.s = dataclasses.replace(self.s, client_id=int(client_id))
        log.warning("IBKR: client id %s -> %s (%s)", old, client_id, why)
        self._note(f"client id {old} -> {client_id}")
        return self.request_reconnect(f"client id {client_id}: {why}")

    def slow_history(self, factor: float, seconds: float) -> None:
        """Space history requests `factor` times further apart for `seconds` (pacing)."""
        self.history_slow_factor = max(1.0, float(factor))
        self.history_slow_until = self.clock() + float(seconds)
        self._note(f"history slowed x{factor:g} for {seconds:.0f}s")

    # -- test and chaos hooks -----------------------------------------------------
    def simulate_socket_drop(self) -> bool:
        """Close the socket from our side, as a dropped network would: the disconnect event
        fires and the connection is rebuilt. For the chaos tests only."""
        loop, ib = self._loop, self.ib
        if loop is None or ib is None:
            return False

        def drop():
            try:
                conn = ib.client.conn
                if conn.transport is not None:
                    conn.transport.close()
                    return
            except Exception:  # noqa: BLE001
                pass
            self._on_disconnect()

        loop.call_soon_threadsafe(drop)
        return True

    def wait_ready(self, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.ready:
                return True
            time.sleep(0.05)
        return self.ready


_LIVE: dict = {}


def shared_live(cfg) -> LiveGateway:
    """The process's one persistent connection (fixed client id), started on first use."""
    from asxbot.ibkr.gateway import settings_from_config

    s = settings_from_config(cfg)
    key = (s.host, s.port, s.client_id)
    gw = _LIVE.get(key)
    if gw is None:
        from asxbot.localdir import asx_local

        gw = _LIVE[key] = LiveGateway(s, history_dir=asx_local() / "ibkr" / "history")
        gw.start()
    return gw


__all__ = [
    "Bar", "HistJob", "LiveGateway", "MinuteAggregator", "Sub", "shared_live",
]  # fmt: skip
