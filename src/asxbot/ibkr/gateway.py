"""One read-only connection to IB Gateway, and the pacing rules that keep it welcome.

DATA ONLY (see the package docstring). What it asks IB Gateway for:
  * 1-minute TRADES bars (reqHistoricalData), for today and for the prior sessions that
    make "usual volume" - one request per stock, never streamed;
  * a quote (reqMktData, cancelled as soon as it has answered): bid, ask, last and their
    sizes, the day's open (the opening auction's price once the market is open), the
    pre-open auction's indicative price and volume (generic tick 225), and the halt flag;
  * nothing about the account: the connection is opened read-only with no startup fetch
    of orders, executions or account values. (ib_async always asks for positions when it
    connects; this module never reads them.)

IBKR's limits, and how they are kept (TWS API "Historical Data Limitations", read
2026-09-24; interactivebrokers.github.io/tws-api/historical_limitations.html):
  * at most 50 historical requests open at once: `max_concurrent_requests` (8) at a time;
  * bars of 1 minute and larger have no hard pacing limit, only a "soft" slow-down, and too
    much too fast "can lead to throttling and eventual disconnect". The 30-seconds-and-under
    rules are kept anyway: no identical request within 15 s (`min_refetch_s`), and at most
    `max_requests_per_10min` (600) in any ten minutes, `max_requests_per_cycle` (90) a scan;
  * market data lines (100 on a new account) are used ONLY by quotes, one at a time and
    cancelled on answer, capped at `max_quote_lines`. Bars do not hold a line open. So the
    day trader's universe (~250 stocks) never meets the line limit: it is ROTATED through
    historical requests, the stalest stocks first, 90 a scan - each stock refreshed about
    every three minutes, and a stock being decided on is fetched on the spot.
  * a pacing violation (error 162 with "pacing", or 420) halves the per-scan budget for the
    rest of the day and is logged as an ERROR.

Every contract is QUALIFIED before use (reqContractDetails, through ib_async's
qualifyContracts): ib_async refuses a quote for a contract with no conId (the check at 21:32
on 24 Sep 2026 failed exactly so). Each code is qualified once per process and cached with
its conId; a stock must come back as an ASX primary listing in AUD, the index as XJO on ASX.

Market data type (reqMarketDataType) follows the ASX clock: real-time (1) from the pre-open
to the closing auction on a trading day, frozen (2) otherwise - frozen is the last real-time
value, so it still proves the real-time subscription out of hours, where delayed-frozen (4)
would hide a missing one.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as time_cls
from zoneinfo import ZoneInfo

import pandas as pd

from asxbot.log import get_logger

log = get_logger("asxbot.ibkr.gateway")
SYD = ZoneInfo("Australia/Sydney")

# IB message codes this module acts on (TWS API "Message Codes").
LOST = {1100, 2110}  # connectivity between Gateway and IBKR's servers broken
RESTORED = {1101, 1102}
FARM_BROKEN = {2103, 2105, 2157}  # a market data / historical / sec-def farm is down
FARM_OK = {2104, 2106, 2158}
NOT_CONNECTED = {502, 504}
PACING = {420}
NO_PERMISSION = {354, 10089, 10090, 10167, 10168, 10186, 10187}  # delayed / not subscribed
NO_SECURITY = {200}
# reqMarketDataType: 1 real-time, 2 frozen, 3 delayed, 4 delayed-frozen. We ask for 1 in the
# live window below and 2 outside it (live_data_type); the type each quote actually came back
# with is what is checked.
MARKET_DATA_TYPES = {1: "real-time", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}
REAL_TIME_TYPES = {1, 2}
MARKET_OPEN, MARKET_CLOSE = time_cls(10, 0), time_cls(16, 10)
# Real-time is asked for from the pre-open (07:00) to past the closing auction (16:10-16:12).
LIVE_FROM, LIVE_TO = time_cls(7, 0), time_cls(16, 15)
QUALIFY_RETRY_S = 3600.0  # a code IBKR could not qualify is not asked about again for an hour
LINK_CHECK_S = 5.0  # how long Gateway has to answer a time request after a batch timed out


def _trading_day(now: datetime) -> bool:
    from asxbot.announcements.live import is_trading_day

    return now.weekday() < 5 and is_trading_day(now.date())


def market_hours(now: datetime) -> bool:
    """The continuous session, 10:00 to the closing auction, on an ASX trading day."""
    now = now.astimezone(SYD)
    return _trading_day(now) and MARKET_OPEN <= now.time() < MARKET_CLOSE


def live_data_type(now: datetime) -> int:
    """The market data type to ask for: 1 (real-time) from the pre-open to the end of the
    closing auction on a trading day, else 2 (frozen)."""
    now = now.astimezone(SYD)
    return 1 if _trading_day(now) and LIVE_FROM <= now.time() < LIVE_TO else 2


@dataclass
class GatewaySettings:
    host: str = "127.0.0.1"
    port: int = 4001
    client_id: int = 41
    connect_timeout_s: float = 10.0
    request_timeout_s: float = 20.0
    market_data_lines: int = 100
    max_quote_lines: int = 20
    max_requests_per_cycle: int = 90
    max_requests_per_10min: int = 600
    max_concurrent_requests: int = 8
    min_refetch_s: float = 15.0
    reconnect_every_s: float = 300.0
    quote_wait_s: float = 3.0
    generic_ticks: str = "225"
    probe_code: str = "BHP"
    # The persistent connection (ibkr/live.py, 2026-09-25): the heartbeat, what counts as
    # stale, the streaming rotation and the paced history queue.
    heartbeat_s: float = 30.0
    heartbeat_timeout_s: float = 10.0
    heartbeat_misses: int = 2
    max_data_age_s: float = 60.0
    stream_reserve_lines: int = 6
    min_dwell_s: float = 120.0
    poll_per_cycle: int = 30
    history_concurrency: int = 4
    history_min_interval_s: float = 0.25
    reconnect_backoff_max_s: float = 60.0

    def __post_init__(self) -> None:
        if int(self.client_id) == 0:
            # client 0 is bound to orders placed in Gateway by hand; never ours
            raise ValueError("ibkr.client_id must not be 0")
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            # Gateway on this PC only: its API is trusted to 127.0.0.1 (jts.ini TrustedIPs).
            raise ValueError(f"ibkr.host must be this PC (127.0.0.1), got {self.host!r}")


def settings_from_config(cfg) -> GatewaySettings:
    raw = cfg.get("ibkr") or {}
    known = GatewaySettings.__dataclass_fields__
    return GatewaySettings(**{k: v for k, v in raw.items() if k in known})


def _num(x) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or f < 0 else f


def disable_orders(ib) -> list[str]:
    """Replace every order method on the connection and its client with one that raises.

    Belt and braces: nothing here calls them (the no-order test checks the source), and IB
    Gateway's Read-Only API setting would refuse them. Names are matched by pattern, so a new
    order method in a later ib_async is caught too. Returns the names disabled."""
    done = []

    def refuse(name):
        def _refused(*a, **k):
            raise PermissionError(f"{name}: the IBKR data connection never touches orders")

        return _refused

    for obj in (ib, getattr(ib, "client", None)):
        if obj is None:
            continue
        for name in dir(obj):
            low = name.lower()
            if name.startswith("_") or name.endswith("Event"):
                continue  # events are how ib_async reports; replacing them breaks it
            if not callable(getattr(obj, name, None)):
                continue
            if "order" in low or "globalcancel" in low or low.startswith("exercise"):
                try:
                    setattr(obj, name, refuse(name))
                    done.append(name)
                except (AttributeError, TypeError):
                    pass
    return done


class Pacer:
    """At most `per_10min` requests in any ten minutes."""

    def __init__(self, per_10min: int):
        self.per_10min = max(1, int(per_10min))
        self.stamps: deque[float] = deque()

    def room(self, now_s: float) -> int:
        while self.stamps and now_s - self.stamps[0] >= 600:
            self.stamps.popleft()
        return max(0, self.per_10min - len(self.stamps))

    def spend(self, n: int, now_s: float) -> None:
        self.stamps.extend([now_s] * int(n))


@dataclass
class Health:
    connected: bool = False
    server_ok: bool = False  # Gateway's own link to IBKR (codes 1100 / 2110 break it)
    refused: bool = False  # the last connect attempt was refused: Gateway down or logged out
    market_data_type: int | None = None  # of the last quote
    requested_data_type: int | None = None  # what reqMarketDataType last asked for
    last_request_error: str = ""  # the last error Gateway sent about one request
    permission_denied: bool = False  # a request was refused for want of a subscription
    last_error: str = ""
    last_ok_at: str = ""
    pacing_hits: int = 0
    farms_broken: set = field(default_factory=set)
    timeouts_in_row: int = 0  # bars batches in a row that ran out of time with nothing back
    competing_at: str = ""  # 10197: a competing session holds the market data (live.py)

    def to_dict(self) -> dict:
        return {
            "connected": self.connected,
            "server_ok": self.server_ok,
            "refused": self.refused,
            "market_data_type": self.market_data_type,
            "market_data": MARKET_DATA_TYPES.get(self.market_data_type or 0, "unknown"),
            "requested_data_type": self.requested_data_type,
            "requested_data": MARKET_DATA_TYPES.get(self.requested_data_type or 0, "unknown"),
            "last_request_error": self.last_request_error,
            "permission_denied": self.permission_denied,
            "last_error": self.last_error,
            "last_ok_at": self.last_ok_at,
            "pacing_hits": self.pacing_hits,
            "farms_broken": sorted(self.farms_broken),
            "timeouts_in_row": self.timeouts_in_row,
            "competing_at": self.competing_at,
        }


def _make_ib():
    from ib_async import IB

    return IB()


class Gateway:
    """The one connection. Every call is synchronous and never raises for a data problem:
    a failed request is an empty answer plus a note in `health`."""

    def __init__(
        self, settings: GatewaySettings, ib_factory=_make_ib, clock=time.monotonic,
        wall=lambda: datetime.now(SYD),
    ):  # fmt: skip
        self.s = settings
        self.ib_factory = ib_factory
        self.clock = clock
        self.wall = wall
        # code -> qualified contract (conId filled in); kept across reconnects (conIds last)
        self.contracts: dict = {}
        self._unqualified: dict[str, float] = {}  # code -> when IBKR last failed to qualify it
        self.ib = None
        self.health = Health()
        self.pacer = Pacer(settings.max_requests_per_10min)
        self.budget = max(1, int(settings.max_requests_per_cycle))
        self.last_connect_try: float | None = None
        self.unknown_codes: set[str] = set()
        self.unanswered: set[str] = set()  # codes the last bars batch ran out of time for
        self._req_errors: dict[int, tuple[int, str]] = {}
        self._lines = 0

    # -- connection ---------------------------------------------------------
    @property
    def ready(self) -> bool:
        return bool(
            self.ib is not None
            and self.ib.isConnected()
            and self.health.connected
            and self.health.server_ok
        )

    def connect(self) -> bool:
        """Connect if not connected. True when connected and Gateway reaches IBKR."""
        if self.ib is not None and self.ib.isConnected():
            return self.ready
        self.last_connect_try = self.clock()
        pacing_hits = self.health.pacing_hits
        self.health = Health(pacing_hits=pacing_hits)  # what arrives while connecting counts
        ib = self.ib_factory()
        disable_orders(ib)
        ib.errorEvent += self._on_error
        try:
            from ib_async import StartupFetchNONE

            fetch = StartupFetchNONE
        except ImportError:  # the fake in tests
            fetch = 0
        try:
            ib.connect(
                self.s.host, int(self.s.port), clientId=int(self.s.client_id),
                timeout=float(self.s.connect_timeout_s), readonly=True, fetchFields=fetch,
            )  # fmt: skip
        except (OSError, TimeoutError) as e:  # refused, reset, or no answer
            self.health = Health(refused=True, last_error=f"connect refused: {e!r}")
            log.warning(
                "IB Gateway at %s:%s refused the connection (%r): not running or logged out",
                self.s.host, self.s.port, e,
            )  # fmt: skip
            self.ib = None
            return False
        self.ib = ib
        self.health.connected = True
        self.health.refused = False
        # Assume the link is up unless Gateway said otherwise while connecting (its farm and
        # connectivity messages arrive during the handshake and are handled in _on_error).
        if not self.health.last_error.startswith("lost"):
            self.health.server_ok = True
        self._ask_data_type(force=True)
        ib.disconnectedEvent += self._on_disconnect
        log.info(
            "IB Gateway connected at %s:%s (client %s), read-only; link to IBKR %s",
            self.s.host, self.s.port, self.s.client_id,
            "up" if self.health.server_ok else "BROKEN",
        )  # fmt: skip
        return self.ready

    def disconnect(self) -> None:
        self._closing = True
        if self.ib is not None:
            try:
                self.ib.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self.ib = None
        self.health.connected = False
        self._closing = False

    def _on_disconnect(self) -> None:
        self.health.connected = False
        if getattr(self, "_closing", False):
            return  # we closed it
        self.health.last_error = "Gateway closed the API connection"
        log.error("IB Gateway closed the API connection")

    def _on_error(self, req_id, code, msg, contract=None) -> None:
        code = int(code)
        if code in LOST:
            self.health.server_ok = False
            self.health.last_error = f"lost {code}: {msg}"
            log.error("IB Gateway lost its link to IBKR (%s): %s", code, msg)
        elif code in RESTORED:
            self.health.server_ok = True
            log.warning("IB Gateway's link to IBKR restored (%s): %s", code, msg)
        elif code in FARM_BROKEN:
            self.health.farms_broken.add(str(msg).split(":")[-1])
        elif code in FARM_OK:
            self.health.farms_broken.discard(str(msg).split(":")[-1])
            self.health.server_ok = True
        elif code in NOT_CONNECTED:
            self.health.connected = False
            self.health.last_error = f"{code}: {msg}"
        elif code in PACING or (code == 162 and "pacing" in str(msg).lower()):
            self._pacing(f"{code}: {msg}")
        if code in NO_PERMISSION:
            self.health.permission_denied = True
        if int(req_id) > 0:
            self._req_errors[int(req_id)] = (code, str(msg))
            self.health.last_request_error = f"{code}: {msg}"

    def _pacing(self, why: str) -> None:
        self.health.pacing_hits += 1
        self.budget = max(10, self.budget // 2)
        log.error("IBKR pacing violation (%s); %d requests a scan from now on", why, self.budget)

    def _ask_data_type(self, force: bool = False) -> int:
        """Ask for real-time (1) in the live window, frozen (2) outside it; only when it
        changes. Returns the type asked for."""
        want = live_data_type(self.wall())
        if force or self.health.requested_data_type != want:
            self.ib.reqMarketDataType(want)
            self.health.requested_data_type = want
        return want

    # -- contracts ----------------------------------------------------------
    @staticmethod
    def contract(code: str):
        """The UNQUALIFIED contract for a code; use `qualified` / `_qualify` before any
        request."""
        from ib_async import Index, Stock

        if code.startswith("^"):
            if code.upper() != "^AXJO":
                raise ValueError(f"no IBKR index mapped for {code}")
            return Index("XJO", "ASX", "AUD")
        # ASX exchange, not SMART: bars and volumes are the ASX's own, like Yahoo's .AX
        return Stock(code.upper(), "ASX", "AUD", primaryExchange="ASX")

    @staticmethod
    def _accept(code: str, c) -> str | None:
        """Why a qualified contract is not the one meant, or None if it is."""
        if c is None or not int(getattr(c, "conId", 0) or 0):
            return "IBKR returned no single matching contract"
        if getattr(c, "currency", "") != "AUD":
            return f"currency {c.currency!r}, not AUD"
        if code.startswith("^"):
            if (c.secType, c.symbol, c.exchange) != ("IND", "XJO", "ASX"):
                return f"got {c.secType} {c.symbol} on {c.exchange}, not the XJO index on ASX"
        elif (c.secType, getattr(c, "primaryExchange", "")) != ("STK", "ASX"):
            return f"got {c.secType} with primary exchange {c.primaryExchange!r}, not ASX"
        return None

    async def _qualify(self, code: str):
        """The qualified contract for a code (from the cache after the first time), or None.
        Raises ValueError for a code with no IBKR mapping."""
        code = code.upper()
        if code in self.contracts:
            return self.contracts[code]
        failed = self._unqualified.get(code)
        if failed is not None and self.clock() - failed < QUALIFY_RETRY_S:
            return None
        c = self.contract(code)
        try:
            got = await asyncio.wait_for(
                self.ib.qualifyContractsAsync(c), timeout=float(self.s.request_timeout_s)
            )
        except Exception as e:  # noqa: BLE001
            log.warning("IBKR could not qualify %s: %r", code, e)
            return None  # a timeout or a broken link: not cached, tried again next time
        q = got[0] if got else None
        why = self._accept(code, q)
        if why:
            self._unqualified[code] = self.clock()
            log.warning("IBKR contract for %s refused (%s); not asked again for an hour", code, why)
            return None
        self._unqualified.pop(code, None)
        self.contracts[code] = q
        log.debug("IBKR contract %s qualified: conId %s", code, q.conId)
        return q

    def qualified(self, code: str):
        """`_qualify` from synchronous code."""
        if self.ib is None:
            return None
        return self.ib.run(self._qualify(code))

    # -- bars ---------------------------------------------------------------
    def room(self) -> int:
        """Requests allowed right now: the scan budget, within the ten-minute cap."""
        return min(self.budget, self.pacer.room(self.clock()))

    def bars(self, requests: dict[str, tuple[str, datetime | None]]) -> dict[str, pd.DataFrame]:
        """1-minute TRADES bars. `requests` maps code -> (duration, end or None for now).
        At most `room()` are sent (the caller picks which); the rest are left out. Returns
        code -> bars (Sydney index, open/high/low/close/volume); a code with none is left out.
        """
        self.unanswered = set()
        if not requests or not self.ready:
            return {}
        codes = list(requests)[: self.room()]
        if not codes:
            return {}
        self.pacer.spend(len(codes), self.clock())
        sem = asyncio.Semaphore(max(1, min(int(self.s.max_concurrent_requests), 45)))

        async def one(code: str):
            dur, end = requests[code]
            async with sem:
                try:
                    c = await self._qualify(code)
                except ValueError:
                    return code, None
                if c is None:
                    return code, None
                try:
                    got = await self.ib.reqHistoricalDataAsync(
                        c, end or "", dur, "1 min", "TRADES", False, formatDate=2,
                        timeout=float(self.s.request_timeout_s),
                    )  # fmt: skip
                except Exception as e:  # noqa: BLE001
                    log.warning("IBKR bars for %s failed: %r", code, e)
                    return code, None
            return code, got

        # The whole batch is bounded too: a data farm that hangs must not stall the watcher.
        # What came back in time is kept; the rest is cancelled and left for the next cycle.
        # Until 25 Sep 2026 a timeout threw every answer away (07:30: "90 asked, 0 returned")
        # and marked the link down, though Gateway was fine (LEARNINGS #25).
        whole = float(self.s.request_timeout_s) * 2

        async def every():  # gathered inside the running loop, never before it
            tasks = {asyncio.ensure_future(one(c)): c for c in codes}
            done, pending = await asyncio.wait(tasks, timeout=whole)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            got = [t.result() for t in done if not t.cancelled() and t.exception() is None]
            return got, {tasks[t] for t in pending}

        try:
            results, late = self.ib.run(every())
        except Exception as e:  # noqa: BLE001
            self.health.last_error = f"bars batch failed: {e!r}"
            self.health.server_ok = False  # the connection itself broke: treat the link as down
            log.error("IBKR bars batch of %d failed: %r", len(codes), e)
            return {}
        out = {}
        for code, got in results:
            df = bars_frame(got)
            if df is not None and len(df):
                out[code] = df
        if out:
            self.health.last_ok_at = datetime.now(SYD).isoformat(timespec="seconds")
        if not late:
            self.health.timeouts_in_row = 0
            return out
        self.unanswered = set(late)
        self._timed_out(len(codes), len(results), len(late), whole)
        return out

    def _timed_out(self, asked: int, answered: int, late: int, whole: float) -> None:
        """A batch ran out of time. Anything back means the link works: the late ones are
        asked again next cycle. Nothing back: the link is down only if Gateway does not answer
        a time request either, or if it is the second such batch in a row."""
        if answered:
            self.health.timeouts_in_row = 0
            log.warning(
                "IBKR bars batch: %d of %d answered in %.0f s; the other %d are asked again "
                "next cycle", answered, asked, whole, late,
            )  # fmt: skip
            return
        self.health.timeouts_in_row += 1
        answers = self._answers()
        if answers and self.health.timeouts_in_row < 2:
            log.warning(
                "IBKR bars batch of %d: nothing back in %.0f s, but Gateway answers; asked "
                "again next cycle", asked, whole,
            )  # fmt: skip
            return
        why = (f"bars batch of {asked} timed out, {self.health.timeouts_in_row} in a row"
               if answers else f"bars batch of {asked} timed out and Gateway did not answer "
               "a time request")  # fmt: skip
        self.health.last_error = why
        self.health.server_ok = False
        log.error("IBKR %s: link treated as down", why)

    def _answers(self) -> bool:
        """Does Gateway answer at all? Its clock, which needs no data farm."""
        ask = getattr(self.ib, "reqCurrentTimeAsync", None)
        if ask is None:
            return False
        try:
            self.ib.run(asyncio.wait_for(ask(), timeout=LINK_CHECK_S))
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("IB Gateway did not answer a time request: %r", e)
            return False

    # -- quotes -------------------------------------------------------------
    def quote(self, code: str) -> dict | None:
        """One quote, then the line is given back. None if Gateway is not ready, every line
        is in use, or nothing came back in `quote_wait_s`."""
        if not self.ready or self._lines >= min(self.s.max_quote_lines, self.s.market_data_lines):
            return None
        try:
            c = self.qualified(code)
        except ValueError:
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("IBKR contract for %s failed: %r", code, e)
            return None
        if c is None:
            return None
        self._lines += 1
        tk = None
        try:
            self._ask_data_type()
            tk = self.ib.reqMktData(c, "" if code.startswith("^") else self.s.generic_ticks)
            deadline = self.clock() + float(self.s.quote_wait_s)
            while self.clock() < deadline:
                self.ib.sleep(0.2)
                have_px = _num(tk.last) or _num(tk.close)
                have_book = code.startswith("^") or (_num(tk.bid) and _num(tk.ask))
                if have_px and have_book:
                    break
        except Exception as e:  # noqa: BLE001
            log.warning("IBKR quote for %s failed: %r", code, e)
            return None
        finally:
            self._lines -= 1
            try:
                self.ib.cancelMktData(c)
            except Exception:  # noqa: BLE001
                pass
        mdt = getattr(tk, "marketDataType", None)
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
        self.health.last_ok_at = datetime.now(SYD).isoformat(timespec="seconds")
        return q


def bars_frame(got) -> pd.DataFrame | None:
    """ib_async bars -> the frame every feed returns: Sydney-local index, bar START times,
    open/high/low/close/volume. An index's volume (IBKR sends -1 or 0) is 0."""
    if not got:
        return None
    rows = []
    for b in got:
        ts = pd.Timestamp(b.date)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        vol = _num(getattr(b, "volume", 0)) or 0.0
        rows.append((ts.tz_convert(SYD), float(b.open), float(b.high), float(b.low),
                     float(b.close), vol))  # fmt: skip
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("ts", keep="last").set_index("ts").sort_index()
    df.index.name = None
    return df


def end_of(day) -> datetime:
    """The end of a Sydney day, as the end time of a request for the sessions before it."""
    return datetime.combine(day, datetime.min.time(), tzinfo=SYD)


def seconds_since(ts: datetime, now: datetime, pad_s: int = 180) -> str:
    """A duration covering `ts` to now plus a margin, as IBKR wants it ("900 S")."""
    secs = int((now - ts).total_seconds()) + int(pad_s)
    return f"{max(300, min(secs, 86_400))} S"


_SHARED: dict = {}


def shared(cfg) -> Gateway:
    """The process's one Gateway connection (fixed client id), built on first use."""
    s = settings_from_config(cfg)
    key = (s.host, s.port, s.client_id)
    gw = _SHARED.get(key)
    if gw is None:
        gw = _SHARED[key] = Gateway(s)
    return gw


def maybe_reconnect(gw: Gateway) -> bool:
    """Try again, but not more often than `reconnect_every_s`."""
    if gw.ready:
        return True
    last = gw.last_connect_try
    if last is not None and gw.clock() - last < float(gw.s.reconnect_every_s):
        return False
    if gw.ib is not None and not gw.ib.isConnected():
        gw.disconnect()
    if gw.ib is not None and gw.ib.isConnected():
        gw.last_connect_try = gw.clock()
        try:
            gw.ib.sleep(0.2)  # let Gateway's "link restored" messages in
        except Exception:  # noqa: BLE001
            pass
        if gw.ready:
            return True
        # Still connected, still not ready. Gateway sends "restored" (1101/1102) only for a
        # link IT reported lost; when WE marked the link down (a bars batch that timed out
        # or came back mostly empty) nothing from Gateway will ever clear the mark, and the
        # watcher stayed on Yahoo all day (25 Sep 2026, from 10:17, after the first batch
        # on a freshly logged-in Gateway timed out). So start a fresh connection: its
        # handshake says whether the link is up (Gateway repeats 2110 if it is not).
        gw.disconnect()
    return gw.connect()


__all__ = [
    "Gateway", "GatewaySettings", "Health", "Pacer", "bars_frame", "disable_orders",
    "end_of", "live_data_type", "market_hours", "maybe_reconnect", "seconds_since",
    "settings_from_config", "shared",
]  # fmt: skip
