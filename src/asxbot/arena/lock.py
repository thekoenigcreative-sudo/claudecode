"""The market-hours lock: when the watcher may NOT be restarted (2026-09-25, Rick's brief).

On 25 Sep a build restarted the watcher at 10:52, mid-session, and re-ran the v2 rule late.
From now on the deploy and restart commands refuse to stop or start the watcher:

  * between 07:25 and 19:25 Sydney on an ASX trading day, unless explicitly forced with a
    written reason (`--force --reason "..."`), which is logged;
  * while any arena account holds a position or has an order working - never, forced or not.

The rule is also in CLAUDE.md so every future job obeys it. Plain code, no model.
"""

from __future__ import annotations

from datetime import datetime
from datetime import time as time_cls
from pathlib import Path
from zoneinfo import ZoneInfo

SYD = ZoneInfo("Australia/Sydney")
LOCK_FROM, LOCK_TO = time_cls(7, 25), time_cls(19, 25)


class Locked(RuntimeError):
    """The restart is refused; the message says why."""


def in_market_lock(now: datetime, is_trading_day=None) -> bool:
    """True inside the locked window on a trading day."""
    if is_trading_day is None:
        from asxbot.announcements.live import is_trading_day as _td

        is_trading_day = _td
    local = now.astimezone(SYD)
    if local.weekday() >= 5 or not is_trading_day(local.date()):
        return False
    return LOCK_FROM <= local.time() < LOCK_TO


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


def check_restart(
    data_dir: Path, now: datetime | None = None, force: bool = False, reason: str = "",
    is_trading_day=None,
) -> str:  # fmt: skip
    """Raise Locked if the watcher may not be restarted now; return a note otherwise."""
    now = now or datetime.now(SYD)
    exposed = open_exposure(data_dir)
    if exposed:
        raise Locked(
            "REFUSED: an arena position or order is open, and the watcher is never restarted "
            "with one open (forced or not): " + "; ".join(exposed)
        )
    if in_market_lock(now, is_trading_day):
        if not (force and reason.strip()):
            raise Locked(
                f"REFUSED: it is {now.astimezone(SYD):%H:%M} Sydney on a trading day; the "
                f"watcher is not restarted between {LOCK_FROM:%H:%M} and {LOCK_TO:%H:%M}. "
                'To override: --force --reason "why" (it is logged).'
            )
        return f"FORCED restart in market hours at {now.astimezone(SYD):%H:%M}: {reason.strip()}"
    return f"clear: {now.astimezone(SYD):%H:%M} Sydney is outside market hours; every book is flat"


__all__ = ["LOCK_FROM", "LOCK_TO", "Locked", "check_restart", "in_market_lock", "open_exposure"]
