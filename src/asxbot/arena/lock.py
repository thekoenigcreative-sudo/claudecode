"""The market-hours lock: when the watcher may NOT be restarted (2026-09-25, Rick's brief).

On 25 Sep a build restarted the watcher at 10:52, mid-session, and re-ran the v2 rule late.
From now on the deploy and restart commands refuse to stop or start the watcher:

  * between 07:25 and the watcher's own stop time for the day (19:31, or 20:31 on daylight
    saving) on an ASX trading day, unless explicitly forced with a written reason
    (`--force --reason "..."`), which is logged;
  * while any arena account holds a position or has an order working - never, forced or not.

Until 26 Sep 2026 the lock ended at 19:25 whatever the day, while from 4 Oct (daylight
saving) the watcher runs to 20:25 and later: a deploy or restart at 19:40 would have been
waved through with the watcher still polling. The end is now the day's stop time
(arena.hours.watcher_stop_time), so the two can never drift apart again.

A watcher that is NOT running is a different case (26 Sep 2026): `scripts/watcher.py start`
may start it at any hour and with positions open, because the positions' stops, targets
and the 15:50 sweep only run while a watcher does (`watcher_running`).

The rule is also in CLAUDE.md so every future job obeys it. Plain code, no model.
"""

from __future__ import annotations

from datetime import date, datetime
from datetime import time as time_cls
from pathlib import Path
from zoneinfo import ZoneInfo

SYD = ZoneInfo("Australia/Sydney")
LOCK_FROM = time_cls(7, 25)
LOCK_TO = time_cls(19, 25)  # a fallback only: the end is the day's watcher stop time


class Locked(RuntimeError):
    """The restart is refused; the message says why."""


def lock_end(day: date, cfg=None) -> time_cls:
    """When the lock ends on `day`: the watcher's stop time that day."""
    try:
        from asxbot.arena.hours import watcher_stop_time

        if cfg is None:
            from asxbot.config import load_config

            cfg = load_config()
        return watcher_stop_time(cfg, day)
    except Exception:  # noqa: BLE001 - a lock that cannot read config still locks
        # The later of the two ends, so an unreadable config never shortens the lock.
        from asxbot.arena.hours import is_dst

        return time_cls(20, 31) if is_dst(day) else time_cls(19, 31)


def in_market_lock(now: datetime, is_trading_day=None, cfg=None) -> bool:
    """True inside the locked window on a trading day."""
    if is_trading_day is None:
        from asxbot.announcements.live import is_trading_day as _td

        is_trading_day = _td
    local = now.astimezone(SYD)
    if local.weekday() >= 5 or not is_trading_day(local.date()):
        return False
    return LOCK_FROM <= local.time() < lock_end(local.date(), cfg)


def open_exposure(data_dir: Path) -> list[str]:
    """Every arena account with a position or a working order, as 'account: what' lines.
    Reads the books only."""
    import json

    out = []
    root = Path(data_dir) / "arena" / "accounts"
    for p in sorted(root.glob("*.json")) if root.exists() else []:
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out.append(f"{p.stem}: unreadable book ({p.name})")
            continue
        held = [f"{t} {v.get('qty'):+d}" for t, v in (raw.get("positions") or {}).items()]
        working = [
            f"{oid} {o.get('side')} {o.get('ticker')}"
            for oid, o in (raw.get("orders") or {}).items()
            if o.get("status") == "pending_fill"
        ]
        if held or working:
            out.append(f"{raw.get('name', p.stem)}: " + ", ".join(held + working))
    return out


def watcher_running(data_dir: Path, pid_alive=None) -> tuple[bool, str]:
    """(a watcher is running now, what the heartbeat says). Running means its heartbeat says
    "running" and that process still exists; anything else - no heartbeat, a run that ended
    (stopped, crashed, interrupted), or a pid that is gone - is not running."""
    from asxbot.arena.heartbeat import read

    if pid_alive is None:
        from asxbot.arena.watchdog import pid_alive
    hb = read(Path(data_dir)) or {}
    if not hb:
        return False, "no watcher heartbeat at all"
    state = str(hb.get("state") or "")
    pid = int(hb.get("pid") or 0) or None
    said = f"pid {pid}, state {state or '?'}, started {hb.get('started')}, beat {hb.get('beat')}"
    if state != "running":
        return False, f"the last watcher ended ({said})"
    if pid is None or not pid_alive(pid):
        return False, f"the heartbeat says running but the process is gone ({said})"
    return True, f"a watcher is running ({said})"


def check_restart(
    data_dir: Path, now: datetime | None = None, force: bool = False, reason: str = "",
    is_trading_day=None, cfg=None,
) -> str:  # fmt: skip
    """Raise Locked if the watcher may not be restarted now; return a note otherwise."""
    now = now or datetime.now(SYD)
    exposed = open_exposure(data_dir)
    if exposed:
        raise Locked(
            "REFUSED: an arena position or order is open, and the watcher is never restarted "
            "with one open (forced or not): " + "; ".join(exposed)
        )
    if in_market_lock(now, is_trading_day, cfg):
        if not (force and reason.strip()):
            end = lock_end(now.astimezone(SYD).date(), cfg)
            raise Locked(
                f"REFUSED: it is {now.astimezone(SYD):%H:%M} Sydney on a trading day; the "
                f"watcher is not restarted between {LOCK_FROM:%H:%M} and {end:%H:%M}. "
                'To override: --force --reason "why" (it is logged).'
            )
        return f"FORCED restart in market hours at {now.astimezone(SYD):%H:%M}: {reason.strip()}"
    return f"clear: {now.astimezone(SYD):%H:%M} Sydney is outside market hours; every book is flat"


def check_start(data_dir: Path, pid_alive=None) -> str:
    """Raise Locked if a watcher may not be STARTED now; return a note otherwise.

    26 Sep 2026: `start` used to go through check_restart, so with the watcher dead and a
    position open it was refused - and the stops, targets and the 15:50 sweep that position
    needed only run inside a watcher. Starting a watcher that is not running interrupts
    nothing, so neither lock applies to it. Starting a second one beside a running watcher
    is refused (the watcher also refuses to run twice: arena/watch.py's single-instance lock).
    """
    running, said = watcher_running(data_dir, pid_alive)
    if running:
        raise Locked(f"REFUSED: {said}. A second watcher is never started beside it; "
                     "to replace it use restart (market-hours lock and open positions apply).")
    exposed = open_exposure(data_dir)
    note = f"clear to start: no watcher is running ({said})"
    if exposed:
        note += "; positions/orders open, and their stops need a watcher: " + "; ".join(exposed)
    return note


__all__ = [
    "LOCK_FROM", "LOCK_TO", "Locked", "check_restart", "check_start", "in_market_lock",
    "lock_end", "open_exposure", "watcher_running",
]  # fmt: skip
