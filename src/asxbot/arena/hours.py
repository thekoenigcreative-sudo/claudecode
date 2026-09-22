"""Announcement hours, which move with Sydney daylight saving.

ASX announcements are released 07:30-19:30 Sydney, and 07:30-20:30 during daylight saving
(ARENA.md). Sydney daylight saving runs from the first Sunday in October to the first
Sunday in April, so the window changes twice a year.

Working that out in code rather than editing config.yaml on the day means nothing silently
stops collecting on 4 October, and nothing has to be remembered again in April.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from datetime import time as time_cls
from zoneinfo import ZoneInfo

from asxbot.config import Config

SYD = ZoneInfo("Australia/Sydney")


def is_dst(day: date | None = None) -> bool:
    """True if Sydney is on daylight saving on this date."""
    day = day or datetime.now(SYD).date()
    noon = datetime(day.year, day.month, day.day, 12, 0, tzinfo=SYD)
    return noon.dst() != timedelta(0)


def _pick(base: str, dst: str | None, day: date) -> time_cls:
    if dst and is_dst(day):
        return time_cls.fromisoformat(str(dst))
    return time_cls.fromisoformat(str(base))


def announcement_window(cfg: Config, day: date | None = None) -> tuple[time_cls, time_cls]:
    """When announcements are released today."""
    day = day or datetime.now(SYD).date()
    h = cfg.get("collector.hours") or {}
    return (
        _pick(h.get("start", "07:30"), h.get("start_dst"), day),
        _pick(h.get("end", "19:30"), h.get("end_dst"), day),
    )


def order_window(cfg: Config, day: date | None = None) -> tuple[time_cls, time_cls]:
    """When the arena will accept an order. Follows the announcement window."""
    day = day or datetime.now(SYD).date()
    h = (cfg.get("arena.guards") or {}).get("allowed_hours", {})
    return (
        _pick(h.get("start", "07:00"), h.get("start_dst"), day),
        _pick(h.get("end", "19:30"), h.get("end_dst"), day),
    )


def watcher_stop_time(cfg: Config, day: date | None = None, minutes_before: int = 5) -> time_cls:
    """When the day's watcher should exit: just before announcements stop, so the evening
    report has the machine to itself. 19:25 normally, 20:25 on daylight saving.

    Anything released in those last few minutes is picked up by the next morning's
    catch-up and queued for the pre-open, which is what ARENA.md asks for anyway.
    """
    _, end = announcement_window(cfg, day)
    dt = datetime.combine(day or datetime.now(SYD).date(), end) - timedelta(
        minutes=minutes_before
    )
    return dt.time()


def evening_slot(cfg: Config, day: date | None = None) -> time_cls:
    """The half hour the evening report belongs in today: 19:30, or 20:30 on daylight saving.

    The scheduled task fires at both times and the wrong one exits immediately, so the
    changeover needs no attention in October or April.
    """
    day = day or datetime.now(SYD).date()
    ev = cfg.get("arena.telegram") or {}
    return _pick(
        ev.get("evening_report_time", "19:30"), ev.get("evening_report_time_dst"), day
    )


def is_evening_slot(cfg: Config, now: datetime | None = None, tolerance_min: int = 25) -> bool:
    """True if now is close enough to today's evening slot to run the report."""
    now = (now or datetime.now(SYD)).astimezone(SYD)
    slot = evening_slot(cfg, now.date())
    target = datetime.combine(now.date(), slot, tzinfo=SYD)
    return abs((now - target).total_seconds()) <= tolerance_min * 60


def describe(cfg: Config, day: date | None = None) -> str:
    day = day or datetime.now(SYD).date()
    start, end = announcement_window(cfg, day)
    o_start, o_end = order_window(cfg, day)
    return (
        f"{day.isoformat()}: daylight saving {'ON' if is_dst(day) else 'off'}; "
        f"announcements {start:%H:%M}-{end:%H:%M}; orders accepted "
        f"{o_start:%H:%M}-{o_end:%H:%M}; watcher stops "
        f"{watcher_stop_time(cfg, day):%H:%M}; evening report {evening_slot(cfg, day):%H:%M}"
    )
