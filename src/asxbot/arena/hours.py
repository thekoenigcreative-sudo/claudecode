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


STOP_AFTER_END_MIN = 1


def watcher_stop_time(
    cfg: Config, day: date | None = None, minutes_after: int = STOP_AFTER_END_MIN
) -> time_cls:
    """When the day's watcher stops: one minute after announcements end, 19:31 normally and
    20:31 on daylight saving. At its stop it polls the page one last time (arena/watch.py),
    so the last poll is at or after 19:31 and catches everything released up to 19:30.

    Changed 26 Sep 2026 (was 5 minutes BEFORE the end: 19:25 / 20:25). Anything released
    19:25-19:30 was never polled, so it was in no day file: the morning catch-up (which reads
    those files) could not see it, and neither could the day trader's news list or the v2
    rule's candidates. The evening routine, due at 19:30, now waits for the watcher to exit
    before it settles the books (scripts/arena_evening.pyw), so it still has them to itself.
    """
    _, end = announcement_window(cfg, day)
    dt = datetime.combine(day or datetime.now(SYD).date(), end) + timedelta(
        minutes=minutes_after
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


def evening_due(
    cfg: Config, now: datetime | None = None, last_sent: datetime | None = None,
    tolerance_min: int = 25,
) -> tuple[bool, str]:  # fmt: skip
    """Should the evening routine run now? (due, why).

    Due in today's slot (is_evening_slot), as before. And, from 26 Sep 2026, LATE: after the
    slot, the same day before midnight, when today's report has not been sent. The task
    starts a missed run late (StartWhenAvailable: the PC was asleep or off at 19:30), and
    until then that late start was skipped as "not today's evening slot", so the day got no
    settle, no mark and no report. `last_sent` is when a report was last delivered
    (report.last_report_sent); one sent in or after today's slot means today is done, so
    the slot an hour later (the task fires at both 19:30 and 20:30) still does nothing."""
    now = (now or datetime.now(SYD)).astimezone(SYD)
    slot = datetime.combine(now.date(), evening_slot(cfg, now.date()), tzinfo=SYD)
    if is_evening_slot(cfg, now, tolerance_min):
        return True, f"due: today's slot is {slot:%H:%M} Sydney"
    opens = slot - timedelta(minutes=tolerance_min)
    if now < slot:
        return False, f"not due: today's slot is {slot:%H:%M} Sydney, now {now:%H:%M}"
    sent = last_sent.astimezone(SYD) if last_sent is not None else None
    if sent is not None and sent >= opens:
        return False, (f"not due: today's report went at {sent:%H:%M} (slot {slot:%H:%M}), "
                       f"now {now:%H:%M}")  # fmt: skip
    return True, (f"due LATE: today's slot was {slot:%H:%M} Sydney and no report has gone "
                  f"since; running now, {now:%H:%M}")  # fmt: skip


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
