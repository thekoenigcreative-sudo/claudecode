"""Rick's "no new entries today" switch (26 Sep 2026).

Rick, 25 Sep: "stop it for today" - and there was nothing to stop: the chat's /stop only
drops the answer it is working on, and the watcher is never stopped in market hours or with a
position open. This is the switch: a dated file, data/arena/pause/<Sydney date>.json, written
by the chat (or by hand) and read by the watcher before every NEW entry, for every playbook
and BOTH books (the agent's and the rule bot's alike, so the comparison stays fair). Exits -
stops, targets, trailing, the flat sweep, closes - are never paused. The file is dated, so
it clears itself the next day; "resume" deletes it.

Infrastructure, not a rule change: no frozen parameter moves (the same kind of control as
the live feed's entry pause and the daily loss limit). Every pause and resume is an
`arena_pause` event, and the evening report and the chat's day summary say when entries were
paused and why.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot.io import write_text_atomic

SYD = ZoneInfo("Australia/Sydney")


def pause_path(data_dir: Path, day: date) -> Path:
    return Path(data_dir) / "arena" / "pause" / f"{day.isoformat()}.json"


def read_pause(data_dir: Path, day: date) -> dict:
    p = pause_path(data_dir, day)
    if not p.exists():
        return {}
    try:
        body = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # An unreadable pause file is still a pause: someone asked for one.
        return {"at": None, "by": "?", "why": "pause file unreadable"}
    return body if isinstance(body, dict) else {}


def paused(data_dir: Path, now: datetime) -> tuple[bool, str]:
    """(entries paused today, why) - for the watcher's entry checks."""
    day = now.astimezone(SYD).date()
    body = read_pause(data_dir, day)
    if not body:
        return False, ""
    at = str(body.get("at") or "")[11:16]
    words = str(body.get("words") or body.get("why") or "")
    why = f"entries paused for today by {body.get('by') or 'Rick'}" + (f" at {at}" if at else "")
    if words:
        why += f" ({words[:80]})"
    return True, why


def set_pause(data_dir: Path, now: datetime, by: str, words: str = "", events=None) -> Path:
    now = now.astimezone(SYD)
    p = pause_path(data_dir, now.date())
    p.parent.mkdir(parents=True, exist_ok=True)
    body = {"at": now.isoformat(timespec="seconds"), "by": by, "words": words[:300]}
    write_text_atomic(json.dumps(body, indent=2), p)
    if events is not None:
        events.append("arena_pause", {"event": "paused", **body})
    return p


def clear_pause(data_dir: Path, now: datetime, by: str, events=None) -> bool:
    now = now.astimezone(SYD)
    p = pause_path(data_dir, now.date())
    if not p.exists():
        return False
    body = read_pause(data_dir, now.date())
    p.unlink()
    if events is not None:
        events.append("arena_pause", {"event": "resumed", "at": now.isoformat(timespec="seconds"),
                                      "by": by, "paused_at": body.get("at")})  # fmt: skip
    return True


__all__ = ["clear_pause", "pause_path", "paused", "read_pause", "set_pause"]
