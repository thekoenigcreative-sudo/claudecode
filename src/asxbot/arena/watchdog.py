"""The watchdog: tells Rick on Telegram when the arena watcher is down, from OUTSIDE it.

Why it exists: at 13:32:51 on 23 Sep 2026 the watcher's console window was closed and the
process died mid-poll. The self-checks run inside the watcher, so they died with it, and
nothing was said until someone read the log hours later.

Run every 5 minutes by its own scheduled task (scripts/schedule_watchdog.ps1) with
pythonw, so it has no window. Each run is one check:

  * Should the watcher be running? On an ASX trading day, from a few minutes after the
    warm-up task starts it (07:30) until its own stop time (19:31, or 20:31 on daylight
    saving; 19:25 until 26 Sep 2026). Outside that, the check does nothing.
  * Is it? It reads the heartbeat the watcher writes every minute (heartbeat.py). The
    watcher is DOWN if there is no heartbeat from a run started today, if that run has
    ended, if its process is gone, or if its heartbeat is more than 5 minutes old. It is
    SILENT if its log has said nothing for more than 5 minutes when it had not announced
    a wait (a sleep, or a model call that may run to its timeout).
  * A heartbeat that is stale while the process still exists is looked at again 90
    seconds later before anything is sent, so a PC waking from sleep is not an outage.
  * Is its cycle moving? (26 Sep 2026.) A watcher that is up and logging can still be stuck
    inside one cycle: on 25 Sep one ran 10:52-11:17, and fills, stops and the 10:30 rule
    waited on it while the watchdog said nothing. The heartbeat carries when the current
    cycle started and when the last one finished; a cycle running more than 6 minutes
    outside any wait it announced (a sleep, a model call) is STUCK. One message when it is
    first seen, one when the cycle finishes.

One Telegram message when an outage is first seen, and one when the watcher is back.
If a message cannot be sent it is tried again on the next run. The outage is kept in
data/arena/watchdog_state.json; transitions and failures go to watchdog.log in the local
logs folder (asxbot.log.logs_dir, off Google Drive), never to asxbot.log, whose silence is
one of the things being measured.

Plain code: it only reads the heartbeat and sends a message. It never starts, stops or
restarts anything. The watcher is only ever started through its scheduled task.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot.arena.heartbeat import read as read_heartbeat
from asxbot.io import write_text_atomic

SYD = ZoneInfo("Australia/Sydney")
SILENT_AFTER = timedelta(minutes=5)
STALL_AFTER = timedelta(minutes=6)  # a cycle this long outside an announced wait is stuck
START_GRACE = timedelta(minutes=3)  # the warm-up task starts at 07:30; python takes a moment
RECHECK_S = 90
TASK = "ASXBot Arena Warmup"
STATE_FILE = "watchdog_state.json"
LOG_FILE = "watchdog.log"


@dataclass
class Verdict:
    expected: bool  # the watcher should be running now
    ok: bool  # it is (always True when not expected)
    problem: str = ""  # down | silent
    detail: str = ""
    pid: int | None = None
    recheck: bool = False  # the process exists; look again before calling it an outage


def _t(value) -> datetime | None:
    if not value:
        return None
    t = datetime.fromisoformat(str(value))
    return t if t.tzinfo else t.replace(tzinfo=SYD)


def _hm(t: datetime | None) -> str:
    return t.astimezone(SYD).strftime("%H:%M:%S") if t is not None else "never"


def expected_window(cfg, now: datetime) -> tuple[datetime, datetime] | None:
    """When the watcher should be up today, or None on a day it should not run at all."""
    from asxbot.announcements.live import is_trading_day
    from asxbot.arena.hours import announcement_window, watcher_stop_time

    day = now.astimezone(SYD).date()
    if day.weekday() >= 5 or not is_trading_day(day):
        return None
    start, _ = announcement_window(cfg, day)
    stop = watcher_stop_time(cfg, day)
    return (
        datetime.combine(day, start, tzinfo=SYD) + START_GRACE,
        datetime.combine(day, stop, tzinfo=SYD),
    )


def assess(hb: dict | None, now: datetime, window, pid_alive) -> Verdict:
    """Is the watcher up? Pure: every input is passed in, so every case is testable."""
    now = now.astimezone(SYD)
    if window is None or not (window[0] <= now < window[1]):
        return Verdict(expected=False, ok=True)

    def down(detail: str, pid=None, recheck=False) -> Verdict:
        return Verdict(True, False, "down", detail, pid, recheck)

    if not hb:
        return down("there is no watcher heartbeat at all: the watcher has not run since "
                    "heartbeats began.")  # fmt: skip
    pid = int(hb.get("pid") or 0) or None
    started = _t(hb.get("started"))
    beat = _t(hb.get("beat"))
    activity = _t(hb.get("last_activity"))
    quiet_until = _t(hb.get("quiet_until"))
    state = str(hb.get("state") or "")

    if started is None or started.date() != now.date():
        last = f" Its last run started {started:%a %d %b %H:%M} and ended {state}." if started \
            else ""  # fmt: skip
        return down(f"the watcher has not started today.{last}")
    if state != "running":
        return down(f"the watcher that started at {_hm(started)} has exited ({state}); "
                    f"its last heartbeat was {_hm(beat)}.", pid)  # fmt: skip
    if pid is None or not pid_alive(pid):
        return down(f"the watcher process (pid {pid}) is gone. It started at {_hm(started)}; "
                    f"its last heartbeat was {_hm(beat)} and its last log line "
                    f"{_hm(activity)}. It exited without a word, so it was killed - a "
                    "closed window, a shutdown, or the task being ended.", pid)  # fmt: skip
    if beat is None or now - beat > SILENT_AFTER:
        return down(f"the watcher process (pid {pid}) exists but its heartbeat stopped at "
                    f"{_hm(beat)}: it is hung or suspended.", pid, recheck=True)  # fmt: skip
    if activity is not None and now - activity > SILENT_AFTER:
        if quiet_until is not None and now <= quiet_until:
            return Verdict(True, True, pid=pid)  # a wait it announced; not over yet
        waited = f" It announced a wait ({hb.get('quiet_why')}) that ran out at " \
                 f"{_hm(quiet_until)}." if quiet_until else ""  # fmt: skip
        return Verdict(True, False, "silent",
                       f"the watcher (pid {pid}) is alive but its log has been silent since "
                       f"{_hm(activity)}.{waited} It is probably stuck.", pid, True)  # fmt: skip
    return Verdict(True, True, pid=pid)


def stuck_cycle(hb: dict | None, now: datetime) -> str:
    """Why the running watcher's current cycle counts as stuck, or "" if it does not.

    Pure. Stuck: the cycle has not finished, no announced wait is running now, and it has
    been busy more than STALL_AFTER since it started or since its last announced wait ended,
    whichever is later - so a cycle of several model calls, each announced, is slow but not
    stuck, and a cycle grinding through the feed for 24 minutes (25 Sep) is stuck."""
    if not hb or str(hb.get("state") or "") != "running":
        return ""
    now = now.astimezone(SYD)
    started = _t(hb.get("cycle_started"))
    if started is None:
        return ""
    finished = _t(hb.get("cycle_finished"))
    if finished is not None and finished >= started:
        return ""
    quiet_until = _t(hb.get("quiet_until"))
    if quiet_until is not None and now <= quiet_until:
        return ""  # inside a wait it announced
    ended = _t(hb.get("wait_ended"))
    busy_since = ended if ended is not None and ended > started else started
    if now - busy_since <= STALL_AFTER:
        return ""
    mins = (now - started).total_seconds() / 60.0
    waited = f", {(now - busy_since).total_seconds() / 60.0:.0f} of them outside any announced " \
             "wait" if busy_since != started else ""  # fmt: skip
    return (f"the watcher (pid {hb.get('pid')}) is running, but its cycle that started at "
            f"{_hm(started)} has not finished after {mins:.0f} minutes{waited}; its last log "
            f"line was {_hm(_t(hb.get('last_activity')))}.")  # fmt: skip


def pid_alive(pid: int) -> bool:
    """True if a process with this id is running."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # access denied: it exists, it is not ours
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


# -- state, messages, one run ---------------------------------------------------------
def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def down_message(v: Verdict, now: datetime) -> str:
    what = "WATCHER DOWN" if v.problem == "down" else "WATCHER SILENT"
    if v.problem == "down":
        # 26 Sep 2026: until then this said to run the task by hand, and scripts\watcher.py
        # start (the sanctioned way) refused a dead watcher whenever a position was open -
        # exactly when it is needed. A start is now allowed whenever no watcher is running.
        fix = ("To bring it back it is started again through scripts\\watcher.py start, which "
               f"runs its scheduled task ({TASK}). That is allowed at any hour and with "
               "positions open, because no watcher is running; once started it catches up "
               "the missed fills and stops from the minute bars.")  # fmt: skip
    else:
        fix = ("It is still running, so it is not started again. A restart stops it first: "
               "never while a position or order is open, and in market hours only with a "
               "written reason (scripts\\watcher.py restart --force --reason).")  # fmt: skip
    return (
        f"<b>{what}</b> (watchdog, {now:%H:%M})\n{escape(v.detail)}\n"
        "Until it is back nothing is watched, filled, stopped or taken at a target.\n"
        f"{escape(fix)}"
    )


def stuck_message(detail: str, now: datetime) -> str:
    return (
        f"<b>WATCHER CYCLE STUCK</b> (watchdog, {now:%H:%M})\n{escape(detail)}\n"
        "Until the cycle moves on, fills, stops, targets, the 10:30 rule and the flat sweep "
        "wait on it. One more message when it finishes."
    )


def unstuck_message(stall: dict, now: datetime, hb: dict | None) -> str:
    started = _t(stall.get("cycle_started"))
    finished = _t((hb or {}).get("cycle_finished"))
    unsent = "" if stall.get("alerted_at") else " (the stuck alert could not be sent)"
    return (
        f"<b>WATCHER CYCLE MOVING AGAIN</b> (watchdog, {now:%H:%M})\n"
        f"The cycle that started at {_hm(started)} finished"
        + (f" at {_hm(finished)}" if finished is not None else "")
        + f"; it was first seen stuck at {_hm(_t(stall.get('since')))}{unsent}."
    )


def up_message(v: Verdict, outage: dict, now: datetime, hb: dict | None) -> str:
    since = _t(outage.get("since"))
    started = _t((hb or {}).get("started"))
    unsent = "" if outage.get("alerted_at") else " (the down alert could not be sent)"
    return (
        f"<b>WATCHER BACK UP</b> (watchdog, {now:%H:%M})\n"
        f"Running again as pid {v.pid}, started {_hm(started)}. It was first seen "
        f"{escape(outage.get('problem', 'down'))} at {_hm(since)}{unsent}."
    )


def run_once(cfg, now: datetime, send, hb_reader=None, alive=pid_alive, sleep=time.sleep) -> str:
    """One check. `send(text) -> bool` delivers a message. Returns what happened."""
    data_dir = Path(cfg.data_dir)
    state_path = data_dir / "arena" / STATE_FILE
    state = load_state(state_path)
    outage = state.get("outage")
    reader = hb_reader or (lambda: read_heartbeat(data_dir))
    window = expected_window(cfg, now)

    hb = reader()
    v = assess(hb, now, window, alive)
    if not v.ok and v.recheck:
        sleep(RECHECK_S)
        now = now + timedelta(seconds=RECHECK_S)
        hb = reader()
        v = assess(hb, now, window, alive)

    result = "not expected to run" if not v.expected else "ok"
    if v.expected and not v.ok:
        if not outage:
            outage = {"since": now.isoformat(timespec="seconds"), "problem": v.problem,
                      "detail": v.detail, "alerted_at": None}  # fmt: skip
        if not outage.get("alerted_at"):
            if send(down_message(v, now)):
                outage["alerted_at"] = now.isoformat(timespec="seconds")
                result = f"{v.problem}: alert sent"
            else:
                result = f"{v.problem}: alert NOT sent, will retry"
            _log(cfg.logs_dir, f"{v.problem.upper()}: {v.detail} [{result}]")
        else:
            result = f"{v.problem}: already reported at {outage['alerted_at']}"
    elif v.expected and outage:
        if send(up_message(v, outage, now, hb)):
            _log(cfg.logs_dir, f"BACK UP: pid {v.pid}; outage since {outage.get('since')} closed")
            outage = None
            result = "back up: message sent"
        else:
            result = "back up: message NOT sent, will retry"
            _log(cfg.logs_dir, "BACK UP, but the message could not be sent; will retry")

    stall, stall_note = _check_stall(cfg, state.get("stall"), v, hb, now, send)
    if stall_note:
        result = f"{result}; {stall_note}"
    state = {"last_check": now.isoformat(timespec="seconds"), "last_result": result,
             "outage": outage, "stall": stall}  # fmt: skip
    write_text_atomic(json.dumps(state, indent=2), state_path)
    return result


def _check_stall(cfg, stall: dict | None, v: Verdict, hb, now: datetime, send):
    """The stuck-cycle alert: once when a cycle is first seen stuck, once when it moves.
    Returns (the stall state to keep, a note for the result). Only for a watcher that is
    up: a down or silent watcher has its own message, which says more."""
    if not (v.expected and v.ok):
        return None, ""
    detail = stuck_cycle(hb, now)
    cycle = str((hb or {}).get("cycle_started") or "")
    if detail:
        if not stall or stall.get("cycle_started") != cycle:
            stall = {"cycle_started": cycle, "since": now.isoformat(timespec="seconds"),
                     "detail": detail, "alerted_at": None}  # fmt: skip
        if stall.get("alerted_at"):
            return stall, f"cycle stuck: already reported at {stall['alerted_at']}"
        if send(stuck_message(detail, now)):
            stall["alerted_at"] = now.isoformat(timespec="seconds")
            _log(cfg.logs_dir, f"STUCK: {detail} [alert sent]")
            return stall, "cycle stuck: alert sent"
        _log(cfg.logs_dir, f"STUCK: {detail} [alert NOT sent, will retry]")
        return stall, "cycle stuck: alert NOT sent, will retry"
    if stall:
        if send(unstuck_message(stall, now, hb)):
            _log(cfg.logs_dir, f"CYCLE MOVING AGAIN: the {stall.get('cycle_started')} cycle")
            return None, "cycle moving again: message sent"
        return stall, "cycle moving again: message NOT sent, will retry"
    return None, ""


def _log(log_dir: Path, text: str) -> None:
    p = Path(log_dir) / LOG_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now(SYD):%Y-%m-%d %H:%M:%S} {text}\n")


def main() -> int:
    from asxbot.arena.notify import Notifier
    from asxbot.config import load_config

    cfg = load_config()
    # Notifier.send never raises and records every message in events/arena_notify.jsonl.
    # Enabled regardless of arena.alerts.telegram: an outage alert is not an arena alert.
    notifier = Notifier(cfg, enabled=True)
    try:
        run_once(cfg, datetime.now(SYD), notifier.send)
    except Exception as e:  # noqa: BLE001 - leave a trace; pythonw has nowhere to print
        _log(cfg.logs_dir, f"the watchdog itself failed: {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
