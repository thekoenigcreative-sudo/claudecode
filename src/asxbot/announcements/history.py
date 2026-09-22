"""History archive: every announcement for every code in both universes, 2002 to now.

One request per (code, year). Resumable: data/announcements/history/_progress.json records
which (code, year) pages are done. Completed past years are never refetched; the current
year is refetched when older than `current_year_max_age_days`. Output: one Parquet per code
in data/announcements/history/<CODE>.parquet (metadata only, no PDFs).

Runs in the foreground of its own process; start it with
    asxbot announcements history --universe all
and stop it with Ctrl-C at any time. It picks up where it left off.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from asxbot.alerts import ACCESS_REFUSED, Alerts
from asxbot.announcements.http import AccessRefused, PacedClient
from asxbot.announcements.model import to_frame
from asxbot.announcements.parser import ParseError, parse_company
from asxbot.io import safe_stem, write_parquet_atomic, write_text_atomic
from asxbot.log import get_logger

log = get_logger("asxbot.announcements.history")

FIRST_YEAR = 2002
URL = (
    "https://www.asx.com.au/asx/v2/statistics/announcements.do"
    "?by=asxCode&asxCode={code}&timeframe=Y&year={year}"
)


class HistoryArchive:
    def __init__(self, data_dir: Path, client: PacedClient, alerts: Alerts):
        self.dir = Path(data_dir) / "announcements" / "history"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.alerts = alerts
        self._progress_path = self.dir / "_progress.json"
        self.progress: dict[str, str] = self._load_progress()

    def _load_progress(self) -> dict[str, str]:
        if self._progress_path.exists():
            try:
                return json.loads(self._progress_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log.warning("corrupt progress file; starting over (cache makes this cheap)")
        return {}

    def _save_progress(self) -> None:
        write_text_atomic(json.dumps(self.progress, sort_keys=True), self._progress_path)

    @staticmethod
    def _key(code: str, year: int) -> str:
        return f"{code}:{year}"

    def done(self, code: str, year: int, current_year_max_age_days: int) -> bool:
        s = self.progress.get(self._key(code, year))
        if not s:
            return False
        if year < date.today().year:
            return True
        return (date.today() - date.fromisoformat(s)).days <= current_year_max_age_days

    def path(self, code: str) -> Path:
        return self.dir / f"{safe_stem(code)}.parquet"

    def load(self, code: str) -> pd.DataFrame:
        p = self.path(code)
        return pd.read_parquet(p) if p.exists() else to_frame([])

    def load_all(self, codes: list[str] | None = None) -> pd.DataFrame:
        files = [self.path(c) for c in codes] if codes else sorted(self.dir.glob("*.parquet"))
        frames = [pd.read_parquet(p) for p in files if p.exists() and not p.stem.startswith("_")]
        if not frames:
            return to_frame([])
        return pd.concat(frames, ignore_index=True).sort_values(["released_at", "code"])

    def years_for(self, code: str, listing_date: date | None) -> list[int]:
        first = FIRST_YEAR
        if listing_date is not None and listing_date.year > first:
            first = listing_date.year
        return list(range(first, date.today().year + 1))

    def run(
        self,
        codes: list[str],
        listing_dates: dict[str, date | None] | None = None,
        current_year_max_age_days: int = 1,
        max_requests: int | None = None,
    ) -> dict[str, int]:
        """Fetch what's missing. Returns counts. Raises AccessRefused after alerting."""
        if self.alerts.is_active(ACCESS_REFUSED):
            raise AccessRefused("access-refused alert is active; clear it by hand to resume")
        listing_dates = listing_dates or {}
        stats = {"codes": 0, "pages": 0, "rows": 0, "skipped": 0}
        try:
            for code in codes:
                years = [
                    y
                    for y in self.years_for(code, listing_dates.get(code))
                    if not self.done(code, y, current_year_max_age_days)
                ]
                if not years:
                    stats["skipped"] += 1
                    continue
                existing = self.load(code)
                new_frames = [existing] if len(existing) else []
                for year in years:
                    if max_requests is not None and self.client.requests_made >= max_requests:
                        log.info("request budget reached; stopping (resumable)")
                        self._flush(code, new_frames)
                        return stats
                    url = URL.format(code=code, year=year)
                    html = self.client.get(url, use_cache=year < date.today().year)
                    try:
                        items = parse_company(html, code)
                    except ParseError as e:
                        self.alerts.raise_alert(
                            "collector_parse_error", f"{code} {year}: {e}. Fix parser, add fixture."
                        )
                        raise
                    stats["pages"] += 1
                    stats["rows"] += len(items)
                    new_frames.append(to_frame(items))
                    self.progress[self._key(code, year)] = date.today().isoformat()
                self._flush(code, new_frames)
                stats["codes"] += 1
                log.info(
                    "history %s: %d years fetched, %d rows total", code, len(years), stats["rows"]
                )
        except AccessRefused as e:
            self.alerts.raise_alert(
                ACCESS_REFUSED, f"{e}. Collector stopped; price/volume fallback."
            )
            self._save_progress()
            raise
        self._save_progress()
        return stats

    def _flush(self, code: str, frames: list[pd.DataFrame]) -> None:
        frames = [f for f in frames if len(f)]
        if frames:
            df = pd.concat(frames, ignore_index=True)
            df = df.drop_duplicates("ids_id").sort_values("released_at").reset_index(drop=True)
            write_parquet_atomic(df, self.path(code))
        self._save_progress()


def status(data_dir: Path) -> dict:
    d = Path(data_dir) / "announcements" / "history"
    prog = d / "_progress.json"
    n_pages = len(json.loads(prog.read_text(encoding="utf-8"))) if prog.exists() else 0
    files = [p for p in d.glob("*.parquet")] if d.exists() else []
    return {"codes_with_data": len(files), "pages_done": n_pages, "updated": datetime.now()}
