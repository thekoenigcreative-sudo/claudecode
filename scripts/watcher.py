"""Stop, start or restart the arena watcher - only through its scheduled task, and only when
the market-hours lock allows it (asxbot.arena.lock; Rick's brief, 25 Sep 2026).

    C:\\venvs\\asx-bot\\Scripts\\python.exe scripts\\watcher.py status
    C:\\venvs\\asx-bot\\Scripts\\python.exe scripts\\watcher.py stop
    C:\\venvs\\asx-bot\\Scripts\\python.exe scripts\\watcher.py start
    C:\\venvs\\asx-bot\\Scripts\\python.exe scripts\\watcher.py restart [--force --reason "..."]

stop and restart are refused between 07:25 and the watcher's stop time (19:31, or 20:31 on
daylight saving) on an ASX trading day unless forced with a written reason (logged), and
refused whenever any arena account holds a position or has an order working - forced or not.

start is different (26 Sep 2026): when no watcher is running - its heartbeat says it ended,
or its process is gone - it is allowed at any hour and with positions open, because those
positions' stops, targets and the 15:50 sweep only run inside a watcher. Until then start
went through the same locks, so a dead watcher with a position open could not be started
the sanctioned way at all. With a watcher running, start is refused (never two at once).

A watcher started here dies with nothing: the task "ASXBot Arena Warmup" starts it hidden
(scripts/arena_warmup.pyw), as CLAUDE.md requires.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from asxbot import proc  # noqa: E402
from asxbot.arena.lock import Locked, check_restart, check_start  # noqa: E402

SYD = ZoneInfo("Australia/Sydney")
TASK = "ASXBot Arena Warmup"


def data_dir() -> Path:
    """The data folder the watcher writes its heartbeat and books to: config's, under
    ASXBOT_HOME when set (a release), else this checkout's."""
    try:
        from asxbot.config import load_config

        return load_config().data_dir
    except Exception:  # noqa: BLE001 - fall back to the plain layout
        return Path(os.environ.get("ASXBOT_HOME") or REPO) / "data"


def schtasks(*args: str) -> tuple[int, str]:
    r = proc.run(["schtasks", *args], capture_output=True, text=True, encoding="utf-8",
                 errors="replace", check=False)  # fmt: skip
    return r.returncode, (r.stdout or r.stderr or "").strip()


def task_status() -> str:
    rc, out = schtasks("/query", "/tn", TASK, "/fo", "LIST", "/v")
    if rc != 0:
        return f"task {TASK}: {out}"
    keep = ("Status", "Last Run Time", "Last Result", "Next Run Time", "Task To Run")
    lines = [ln.strip() for ln in out.splitlines() if ln.strip().startswith(keep)]
    return "\n".join(lines)


def heartbeat() -> str:
    from asxbot.arena.heartbeat import read

    hb = read(data_dir()) or {}
    return (f"heartbeat: pid {hb.get('pid')} state {hb.get('state')} started {hb.get('started')} "
            f"beat {hb.get('beat')}")  # fmt: skip


def log_line(text: str) -> None:
    from asxbot.log import logs_dir

    d = logs_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "watcher_control.log", "a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now(SYD):%Y-%m-%d %H:%M:%S} {text}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=["status", "stop", "start", "restart"])
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--reason", default="")
    args = ap.parse_args()
    if args.action == "status":
        print(task_status())
        print(heartbeat())
        return 0
    try:
        if args.action == "start":
            note = check_start(data_dir())
        else:
            note = check_restart(data_dir(), force=args.force, reason=args.reason)
    except Locked as e:
        print(e)
        log_line(f"{args.action} {e}")
        return 3
    print(note)
    log_line(f"{args.action}: {note}")
    if args.action in ("stop", "restart"):
        rc, out = schtasks("/end", "/tn", TASK)
        print(f"schtasks /end: {out}")
        log_line(f"/end -> {rc}: {out}")
        import time

        time.sleep(5)
    if args.action in ("start", "restart"):
        rc, out = schtasks("/run", "/tn", TASK)
        print(f"schtasks /run: {out}")
        log_line(f"/run -> {rc}: {out}")
        if rc != 0:
            return 1
    print(task_status())
    return 0


if __name__ == "__main__":
    sys.exit(main())
