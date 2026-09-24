"""Live poller: on trading days, fetch today's announcements about once a minute during
announcement hours (07:30-19:30 Sydney), record every new item in the event log, save the
day's table to Parquet, and hand price-sensitive in-universe items to the scanner.

PDFs are fetched only for price-sensitive announcements in the universes.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date, datetime
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
        self.events = EventLog(data_dir)
        self.seen: set[str] = set()
        self._load_seen(date.today())

    def _day_path(self, d: date) -> Path:
        return self.dir / f"{d.isoformat()}.parquet"

    def _load_seen(self, d: date) -> None:
        p = self._day_path(d)
        if p.exists():
            self.seen = set(pd.read_parquet(p)["ids_id"].astype(str))

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
            self.alerts.raise_alert("collector_parse_error", f"today page: {e}; {kept}")
            raise
        if not items:
            # Before the ASX posts anything (07:30 on 24 Sep) the page has no announcements
            # table. That is zero, not a fault; parse_today tells it from a changed layout.
            kept = self._keep_page(html, "empty", now)
            log.info("today page: the ASX has posted no announcements yet; %s", kept)
        new = [a for a in items if a.ids_id not in self.seen]
        if new:
            self.seen.update(a.ids_id for a in new)
            write_parquet_atomic(to_frame(items), self._day_path(now.date()))
            for a in new:
                rec = a.to_dict()
                rec["in_universe"] = a.code in self.universe
                self.events.append("announcements", rec)
                if a.price_sensitive and a.code in self.universe:
                    if self.fetch_pdfs:
                        self._fetch_pdf(a)
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

    def fetch_pdf(self, a: Announcement, stage: str = "poll") -> Path | None:
        """Fetch the announcement's PDF unless a real one is already on disk. None, with an
        `announcement_pdf_failures` event saying why, if it cannot be had.

        Called when an announcement is first seen, and again (since 2026-09-24) when one
        reaches the reader with no document on disk: until then a PDF missed the first time
        was never fetched again. On 23 Sep 44 announcements released 07:37-09:08 had terms
        pages saved as PDFs; the 09:12 fix deleted them and nothing fetched them again, so
        CMM, NUF and TUA (its FY26 Appendix 4E) were re-looked at 10:20-10:33 on headlines.
        """
        dest = pdf_path(self.data_dir, a)
        try:
            return self.client.get_bytes(a.pdf_url, dest)
        except AccessRefused as e:
            self.alerts.raise_alert(ACCESS_REFUSED, f"{e}. Poller stopped; price/volume fallback.")
            raise
        except Exception as e:  # noqa: BLE001
            # Say it plainly and record it. On 23 Sep this failed silently for every
            # announcement of the day, and the agents judged headlines without anyone
            # noticing that no document had ever arrived.
            reason = getattr(e, "reason", None) or type(e).__name__
            log.error("PDF FETCH FAILED for %s %s (%s): %s - the agents will see no document",
                      a.code, a.ids_id, reason, e)  # fmt: skip
            self.events.append(
                "announcement_pdf_failures",
                {"code": a.code, "ids_id": a.ids_id, "url": a.pdf_url, "error": str(e),
                 "reason": reason, "stage": stage},  # fmt: skip
            )
            return None

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
