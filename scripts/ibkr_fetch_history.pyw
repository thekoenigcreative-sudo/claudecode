"""IBKR 1-minute history for the replay, fetched with no window to close:
`scripts/ibkr_fetch_history.py --months 6` (newest first, paced, resumable, cached in
%LOCALAPPDATA%\\asx-bot\\ibkr\\history - the same cache the live feed reads its prior sessions
from, so each morning's pre-open history is mostly on disk already).

Started by the scheduled task "ASXBot IBKR History Fetch" (daily 17:30 Sydney) through its
shim (%LOCALAPPDATA%\\asx-bot\\bin\\ibkr_fetch_history.pyw), which runs this file from the
deployed release with ASXBOT_HOME set to the checkout. The fetch itself refuses to start
between 07:00 and 17:00 on a trading day and stops on its own at 07:00 on one (the watcher's
live feed has IBKR to itself in market hours); on a weekend it runs until the window is on
disk. Once the six months are there, a run tops up the newest sessions and ends in minutes.

Why a task and not a shell (25 Sep 2026): six months of the ASX 300 is about 3,000 requests
and IBKR answers a two-week request in ~12 s, so the fetch outlives any Claude Code session,
and a process tied to a window dies with it (LEARNINGS #14). The log goes to
ibkr_history_fetch.log in the local logs folder (%LOCALAPPDATA%\\asx-bot\\logs).
"""

import os
import sys
from datetime import datetime
from pathlib import Path

VENV = Path(r"C:\venvs\asx-bot\Scripts")
RELEASE = Path(__file__).resolve().parents[1]
HOME = Path(os.environ.get("ASXBOT_HOME") or RELEASE)
FAILURES = Path(r"C:\venvs\asx-bot\task-failures.log")
MONTHS = "6"


def logs_dir() -> Path:
    override = os.environ.get("ASXBOT_LOG_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "asx-bot" / "logs"


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def main() -> int:
    if not (HOME / "config.yaml").exists():
        FAILURES.parent.mkdir(parents=True, exist_ok=True)
        with open(FAILURES, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp()}  ABORTED (history fetch): {HOME} is not available. "
                     "Google Drive is not mounted: nobody is logged in.\n")  # fmt: skip
        return 1
    os.chdir(HOME)
    env = {
        **os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
        "ASXBOT_HOME": str(HOME), "ASXBOT_RELEASE": str(RELEASE),
        "PYTHONPATH": str(RELEASE / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }  # fmt: skip
    log_dir = logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    from asxbot import proc as hidden

    with open(log_dir / "ibkr_history_fetch.log", "a", encoding="utf-8", buffering=1) as log:
        log.write(f"\n=== history fetch starting {stamp()} from {RELEASE.name} ===\n")
        try:
            child = hidden.popen(
                [str(VENV / "python.exe"), str(RELEASE / "scripts" / "ibkr_fetch_history.py"),
                 "--months", MONTHS],
                cwd=HOME, env=env, stdin=hidden.DEVNULL, stdout=hidden.PIPE,
                stderr=hidden.STDOUT,
            )  # fmt: skip
        except OSError as e:
            log.write(f"could not start the fetch: {e}\n")
            log.write(f"=== history fetch finished {stamp()} (exit 1) ===\n")
            return 1
        for raw in child.stdout:
            log.write(raw.decode("utf-8", errors="replace").rstrip("\r\n") + "\n")
        code = child.wait()
        log.write(f"=== history fetch finished {stamp()} (exit {code}) ===\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
