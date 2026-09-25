"""Live poller: on trading days, fetch today's announcements about once a minute during
announcement hours (07:30-19:30 Sydney), record every new item in the event log, save the
day's table to Parquet, and hand price-sensitive in-universe items to the scanner.

PDFs are fetched only for price-sensitive announcements in the universes.

In the watcher (26 Sep 2026) PDF fetching is bounded, because the watcher is single-threaded
and nothing else happens while it waits on asx.com.au: a poll spends at most `pdf_budget_s`
fetching the new PDFs, stops fetching for the cycle at the first network failure, and puts
every PDF it did not get on a retry list. `retry_pdfs`, called by the watcher every cycle,
tries them again - 2, 5, 10, 20 and 40 minutes after each failure - and records the give-up
as a failure event. The list is rebuilt from the day's file on start, so a restart loses
nothing. (LEARNINGS #16: a retry a comment promises must exist; this one has a test.)
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from asxbot.alerts import ACCESS_REFUSED, Alerts
from asxbot.announcements.http import AccessRefused, PacedClient
from asxbot.announcements.model import Announcement, to_frame
from asxbot.announcements.parser import ParseError, parse_today
from asxbot.io import safe_stem, write_parquet_atomic, write_text_atomic
from asxbot.log import EventLog, get_logger

log = get_logger("asxbot.announcements.live")

TODAY_URL = "https://www.asx.com.au/asx/v2/statistics/todayAnns.do"
SYD = ZoneInfo("Australia/Sydney")
PAGES_KEPT_PER_DAY = 5
PARSE_ERROR = "collector_parse_error"
PDF_RETRY_AFTER_MIN = (2, 5, 10, 20, 40)  # after each failed try; then it is given up
# A failure of this kind says asx.com.au is not answering: the rest of the cycle's PDFs wait.
OUTAGE_REASONS = ("timeout", "network error", "server error", "Timeout", "ConnectionError")


def pdf_path(data_dir: Path, a: Announcement) -> Path:
    """Where an announcement's PDF is kept: one place, for the poller and the reader."""
    return (
        Path(data_dir) / "announcements" / "pdf" / a.released_at.strftime("%Y-%m-%d")
        / f"{safe_stem(a.code)}_{a.ids_id}.pdf"
    )  # fmt: skip


def is_trading_day(d: date) -> bool:
    import exchange_calendars as xc

    cal = xc.get_calendar("XASX")
    return cal.is_session(pd.Timestamp(d))


def in_hours(now: datetime, start: str, end: str) -> bool:
    s = dtime.fromisoformat(start)
    e = dtime.fromisoformat(end)
    return s <= now.astimezone(SYD).time() <= e


class LivePoller:
    def __init__(
        self,
        data_dir: Path,
        client: PacedClient,
        alerts: Alerts,
        universe_codes: set[str],
        on_sensitive: Callable[[Announcement], None] | None = None,
        fetch_pdfs: bool = True,
        pdf_budget_s: float | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.dir = self.data_dir / "announcements" / "live"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.pdf_dir = self.data_dir / "announcements" / "pdf"
        self.client = client
        self.alerts = alerts
        self.universe = universe_codes
        self.on_sensitive = on_sensitive
        self.fetch_pdfs = fetch_pdfs
        # Seconds a poll (or a retry pass) may spend fetching PDFs; None: no limit (the
        # stand-alone collector). The watcher sets one (module docstring).
        self.pdf_budget_s = pdf_budget_s
        # ids_id -> {"a": Announcement, "tries": failed tries so far, "next": datetime}
        self.pdf_retry: dict[str, dict] = {}
        self._outage_at: datetime | None = None
        self.events = EventLog(data_dir)
        self.seen: set[str] = set()
        self._load_seen(date.today())

    def _day_path(self, d: date) -> Path:
        return self.dir / f"{d.isoformat()}.parquet"

    def _load_seen(self, d: date) -> None:
        p = self._day_path(d)
        if p.exists():
            try:
                self.seen = set(pd.read_parquet(p)["ids_id"].astype(str))
            except Exception as e:  # noqa: BLE001 - 26 Sep 2026: this ended the watcher's start
                # The page is read again in full; the watcher's own handled list still stops
                # an announcement being worked twice.
                log.error("today's announcement file %s could not be read (%s); every item on "
                          "the page is seen afresh", p.name, e)  # fmt: skip

    def poll_once(self, now: datetime | None = None) -> list[Announcement]:
        """One fetch. Returns the NEW announcements since the last poll."""
        if self.alerts.is_active(ACCESS_REFUSED):
            raise AccessRefused("access-refused alert active; clear it by hand to resume")
        now = now or datetime.now(SYD)
        try:
            html = self.client.get(TODAY_URL, use_cache=False)
        except AccessRefused as e:
            self.alerts.raise_alert(ACCESS_REFUSED, f"{e}. Poller stopped; price/volume fallback.")
            raise
        try:
            items = parse_today(html)
        except ParseError as e:
            kept = self._keep_page(html, "unparsed", now)
            # Dated in the message itself: the evening report shows only the message, so an
            # old flag read as tonight's news (26 Sep 2026).
            local = now.astimezone(SYD)
            self.alerts.raise_alert(PARSE_ERROR, f"today page at {local:%a %d %b %H:%M}: {e}; "
                                                 f"{kept}")  # fmt: skip
            raise
        if self.alerts.is_active(PARSE_ERROR):
            # A page that parses ends the fault. Until 26 Sep 2026 nothing cleared this flag,
            # so 24 Sep's parse error was reported as persisting every evening after it.
            try:
                self.alerts.clear(PARSE_ERROR)
                log.info("today page parses again; the collector parse-error alert is cleared")
            except OSError as e:
                log.warning("could not clear the collector parse-error alert yet: %s", e)
        if not items:
            # Before the ASX posts anything (07:30 on 24 Sep) the page has no announcements
            # table. That is zero, not a fault; parse_today tells it from a changed layout.
            kept = self._keep_page(html, "empty", now)
            log.info("today page: the ASX has posted no announcements yet; %s", kept)
        new = [a for a in items if a.ids_id not in self.seen]
        if new:
            # The day file first, then "seen" (26 Sep 2026: the other way round, a moment of
            # Google Drive refusing the write marked the new announcements seen and raised,
            # so they were never handed on and never polled as new again).
            write_parquet_atomic(to_frame(items), self._day_path(now.date()))
            self.seen.update(a.ids_id for a in new)
            self._begin_pass(now)
            for a in new:
                rec = a.to_dict()
                rec["in_universe"] = a.code in self.universe
                self._record("announcements", rec)
                if a.price_sensitive and a.code in self.universe:
                    if self.fetch_pdfs:
                        held = self._hold_back(now, budget=True)
                        if held:
                            self._queue_retry(a, now, tried=False, why=held)
                        else:
                            self.fetch_pdf(a, stage="poll", now=now)
                    if self.on_sensitive:
                        self.on_sensitive(a)
        log.info("poll: %d on page, %d new, %d seen today", len(items), len(new), len(self.seen))
        return new

    def _keep_page(self, html: str, kind: str, now: datetime) -> str:
        """Keep the page as evidence: the first empty one of the day, and the first
        PAGES_KEPT_PER_DAY pages that could not be parsed. Never read back by the poller.

        On 24 Sep neither shape was kept, so the empty page's test fixture had to be built
        by hand. Returns where it went, for the log line.
        """
        d = self.data_dir / "announcements" / "pages"
        day = now.astimezone(SYD).strftime("%Y-%m-%d")
        if kind == "empty":
            p = d / f"todayAnns_empty_{day}.html"
            if p.exists():
                return f"first empty page of the day already kept in {p.name}"
        else:
            if len(list(d.glob(f"todayAnns_{kind}_{day}_*.html"))) >= PAGES_KEPT_PER_DAY:
                return f"page not kept ({PAGES_KEPT_PER_DAY} already kept today)"
            p = d / f"todayAnns_{kind}_{day}_{now.astimezone(SYD):%H%M%S}.html"
        try:
            write_text_atomic(html, p)
        except OSError as e:
            return f"page could not be kept: {e}"
        return f"page kept as {p}"

    def _fetch_pdf(self, a: Announcement) -> Path | None:
        return self.fetch_pdf(a, stage="poll")

    def fetch_pdf(
        self, a: Announcement, stage: str = "poll", now: datetime | None = None
    ) -> Path | None:
        """Fetch the announcement's PDF unless a real one is already on disk. None, with an
        `announcement_pdf_failures` event saying why, if it cannot be had.

        Called when an announcement is first seen, and again (since 2026-09-24) when one
        reaches the reader with no document on disk: until then a PDF missed the first time
        was never fetched again. On 23 Sep 44 announcements released 07:37-09:08 had terms
        pages saved as PDFs; the 09:12 fix deleted them and nothing fetched them again, so
        CMM, NUF and TUA (its FY26 Appendix 4E) were re-looked at 10:20-10:33 on headlines.

        From 26 Sep 2026 a failure puts it on the retry list (`retry_pdfs`), and the reader's
        own try is skipped while that retry is not yet due, or while asx.com.au has not been
        answering this cycle: the same PDF is not waited on twice in one cycle.
        """
        now = now or datetime.now(SYD)
        dest = pdf_path(self.data_dir, a)
        if stage == "reader":
            item = self.pdf_retry.get(a.ids_id)
            held = self._hold_back(now, budget=False)
            if held or (item is not None and now < item["next"]):
                when = f"{item['next'].astimezone(SYD):%H:%M}" if item else "next cycle"
                log.info("PDF for %s %s not fetched again for the reader: %s; retried at %s",
                         a.code, a.ids_id, held or f"it failed ({item['why']})", when)  # fmt: skip
                if item is None:
                    self._queue_retry(a, now, tried=False, why=held)
                return None
        try:
            got = self.client.get_bytes(a.pdf_url, dest)
        except AccessRefused as e:
            self.alerts.raise_alert(ACCESS_REFUSED, f"{e}. Poller stopped; price/volume fallback.")
            raise
        except Exception as e:  # noqa: BLE001
            # Say it plainly and record it. On 23 Sep this failed silently for every
            # announcement of the day, and the agents judged headlines without anyone
            # noticing that no document had ever arrived.
            reason = getattr(e, "reason", None) or type(e).__name__
            first = a.ids_id not in self.pdf_retry or stage != "retry"
            (log.error if first else log.warning)(
                "PDF FETCH FAILED for %s %s (%s): %s - the agents will see no document; "
                "retried on a later cycle", a.code, a.ids_id, reason, e,
            )  # fmt: skip
            self._record(
                "announcement_pdf_failures",
                {"code": a.code, "ids_id": a.ids_id, "url": a.pdf_url, "error": str(e),
                 "reason": reason, "stage": stage},  # fmt: skip
            )
            if any(str(reason).startswith(r) for r in OUTAGE_REASONS):
                self._pass_outage, self._outage_at = str(reason), now
            self._queue_retry(a, now, tried=True, why=str(reason))
            return None
        if self.pdf_retry.pop(a.ids_id, None) is not None:
            log.info("PDF for %s %s fetched on a retry (%s)", a.code, a.ids_id, stage)
        return got

    def _record(self, kind: str, rec: dict) -> None:
        """An event-log line (Google Drive). Never raises: the record of an announcement
        must not stop it being handed on (26 Sep 2026)."""
        try:
            self.events.append(kind, rec)
        except OSError as e:
            log.warning("could not write the %s event (%s): %s", kind, e, rec.get("ids_id"))

    # -- the retry list (26 Sep 2026) ----------------------------------------------------
    _pass_started: float = 0.0
    _pass_budget: float | None = None
    _pass_outage: str = ""
    _outage_at: datetime | None = None
    _seeded: date | None = None
    OUTAGE_HOLDS_S = 60.0  # an outage seen this long ago still holds back the next pass

    def _outage_holds(self, now: datetime) -> bool:
        if not self._pass_outage or self._outage_at is None:
            return False
        return abs((now - self._outage_at).total_seconds()) <= self.OUTAGE_HOLDS_S

    def _begin_pass(self, now: datetime, budget_s: float | None = None) -> None:
        self._pass_started = time.monotonic()
        self._pass_budget = self.pdf_budget_s if budget_s is None else budget_s
        if not self._outage_holds(now):
            self._pass_outage = ""  # the poll's outage, seconds ago, still holds the retries

    def _hold_back(self, now: datetime, budget: bool = True) -> str:
        """Why a PDF is not fetched now but left for a later cycle, or "". Only in the
        watcher (a budget is set): the stand-alone collector fetches as it always has."""
        if self.pdf_budget_s is None:
            return ""
        if self._outage_holds(now):
            return f"asx.com.au is not answering this cycle ({self._pass_outage})"
        spent = time.monotonic() - self._pass_started
        limit = self.pdf_budget_s if self._pass_budget is None else self._pass_budget
        if budget and spent >= limit:
            return f"this cycle's {limit:.0f} s for PDFs is spent"
        return ""

    def _queue_retry(
        self, a: Announcement, now: datetime, tried: bool, why: str, wait_s: float = 60.0
    ) -> None:
        """Put a PDF on the retry list. `tried`: a fetch failed (the next try follows the
        PDF_RETRY_AFTER_MIN ladder); otherwise it was held back, and waits `wait_s`."""
        item = self.pdf_retry.get(a.ids_id) or {"a": a, "tries": 0, "why": ""}
        wait = wait_s / 60.0
        if tried:
            item["tries"] += 1
            if item["tries"] > len(PDF_RETRY_AFTER_MIN):
                self.pdf_retry.pop(a.ids_id, None)
                log.error("PDF for %s %s given up after %d tries (%s); the agents see no "
                          "document for it", a.code, a.ids_id, item["tries"], why)  # fmt: skip
                self._record(
                    "announcement_pdf_failures",
                    {"code": a.code, "ids_id": a.ids_id, "url": a.pdf_url, "stage": "retry",
                     "reason": f"given up after {item['tries']} tries: {why}"},  # fmt: skip
                )
                return
            wait = PDF_RETRY_AFTER_MIN[item["tries"] - 1]
        item.update(next=now + timedelta(minutes=wait), why=why or item.get("why", ""))
        self.pdf_retry[a.ids_id] = item

    def _seed_retry(self, now: datetime) -> None:
        """On the first retry pass of a day: every price-sensitive, in-universe announcement
        in today's file whose PDF is not on disk joins the list. A restart loses nothing."""
        day = now.astimezone(SYD).date()
        if self._seeded == day:
            return
        self._seeded = day
        p = self._day_path(day)
        if not p.exists():
            return
        from asxbot.announcements.http import _is_pdf, head_bytes

        df = pd.read_parquet(p)
        for r in df.itertuples():
            if not bool(r.price_sensitive) or r.code not in self.universe:
                continue
            a = Announcement(
                str(r.code), r.released_at.to_pydatetime(), str(r.headline), True,
                str(r.ids_id), str(r.pdf_url), getattr(r, "pages", None), getattr(r, "size", None),
            )  # fmt: skip
            if a.ids_id in self.pdf_retry:
                continue
            dest = pdf_path(self.data_dir, a)
            try:
                ok = dest.exists() and _is_pdf(head_bytes(dest))
            except OSError:
                ok = False
            if not ok:
                self._queue_retry(a, now, tried=False, why="not on disk at start", wait_s=0)

    def retry_pdfs(self, now: datetime | None = None, budget_s: float | None = None) -> list[str]:
        """Fetch again the PDFs whose retry is due, within `budget_s` (default the poll's
        budget). Called by the watcher every cycle. Returns the ids fetched."""
        if not self.fetch_pdfs or self.alerts.is_active(ACCESS_REFUSED):
            return []
        now = now or datetime.now(SYD)
        self._seed_retry(now)
        self._begin_pass(now, budget_s)
        got = []
        for ids, item in sorted(self.pdf_retry.items(), key=lambda kv: kv[1]["next"]):
            if item["next"] > now:
                break  # sorted: nothing after this one is due either
            if self._hold_back(now, budget=True):
                break  # the rest wait for the next cycle
            if self.fetch_pdf(item["a"], stage="retry", now=now) is not None:
                got.append(ids)
        return got

    def run(self, interval_s: float, hours: tuple[str, str], once: bool = False) -> None:
        while True:
            now = datetime.now(SYD)
            if not is_trading_day(now.date()):
                log.info("not a trading day; sleeping 30 min")
                if once:
                    return
                time.sleep(1800)
                continue
            if not in_hours(now, *hours):
                log.info("outside announcement hours %s-%s; sleeping 5 min", *hours)
                if once:
                    return
                time.sleep(300)
                continue
            if now.date() != getattr(self, "_day", now.date()):
                self.seen = set()
            self._day = now.date()
            self.poll_once(now)
            if once:
                return
            time.sleep(interval_s)
