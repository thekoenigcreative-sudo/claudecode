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
from asxbot.io import safe_stem, write_parquet_atomic
from asxbot.log import EventLog, get_logger

log = get_logger("asxbot.announcements.live")

TODAY_URL = "https://www.asx.com.au/asx/v2/statistics/todayAnns.do"
SYD = ZoneInfo("Australia/Sydney")


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
            self.alerts.raise_alert("collector_parse_error", f"today page: {e}")
            raise
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

    def _fetch_pdf(self, a: Announcement) -> Path | None:
        dest = (
            self.pdf_dir
            / a.released_at.strftime("%Y-%m-%d")
            / f"{safe_stem(a.code)}_{a.ids_id}.pdf"
        )
        try:
            return self.client.get_bytes(a.pdf_url, dest)
        except AccessRefused:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("pdf fetch failed for %s %s: %s", a.code, a.ids_id, e)
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
