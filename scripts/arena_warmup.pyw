"""Tactic 1 warm-up with no window to close: the watcher, started hidden.

Started by the Windows scheduled task "ASXBot Arena Warmup" at 07:30 Sydney, weekdays, as

    C:\\venvs\\asx-bot\\Scripts\\pythonw.exe "G:\\My Drive\\asx-bot\\scripts\\arena_warmup.pyw"

(scripts/schedule_watcher_hidden.ps1 points the task here). It does what arena_warmup.ps1
does - run `asxbot arena watch --until auto` from the repo folder and append everything it
prints to arena_warmup.log between the same start and finish lines - without a console
window.

The log is in the local logs folder (%LOCALAPPDATA%/asx-bot/logs), not on Google Drive. At
08:14 on 24 Sep 2026 Drive silently cut off this launcher's long-open handle on
data/arena_warmup.log, and the watcher's on data/logs/asxbot.log, while the watcher went on
trading: nothing reached either log until it was restarted at 08:23. The watcher is told
the same folder (ASXBOT_LOG_DIR) and where its printed output goes (ASXBOT_STDOUT_LOG), so
its log_silent self-check can see this file stop growing. The log is rotated daily
(arena_warmup.log.<date>, 30 kept); the evening routine copies it to data/logs/.

Why: on 23 Sep 2026 the watcher ran in a visible console window, which was closed at
13:32 (exit 0xC000013A, "console closed / Ctrl+C") and took the watcher with it, mid-poll
and without a word. pythonw has no console (checked: the venv's must be CPython's GUI
launcher, not uv's console one - TRACKER #32). The watcher itself is started with
CREATE_NO_WINDOW, so it and everything it runs (the OpenClaw agent calls) share one
console that has no window at all: nothing to close, and nothing flashing up on screen.

Unlike the PowerShell version, output is written line by line as it arrives, not all at
once at the end, so a killed watcher still leaves its last lines in this log. The watcher
runs unbuffered for that (added 23 Sep 2026): Python holds printed output to a pipe in a
buffer, and a killed process loses it - only its logging reached this file as it happened.

Google Drive not mounted yet (26 Sep 2026): the tasks now start in %LOCALAPPDATA%\\asx-bot,
so they can launch before G: is there. Until then this launcher gave up at once when the
settings folder (ASXBOT_HOME, on G:) was missing, and the day had no watcher. Now it waits:
it looks again every 30 seconds until 09:45 Sydney, noting each minute in this log (and
once in task-failures.log), and starts the watcher as normal when the folder appears. Only
at 09:45 - the morning's decisions gone - does it give up. On a day the ASX is shut it does
not wait. Nothing here depends on the folder the task started in: it changes to the
settings folder itself once that exists.
"""

import os
import sys
import time
from datetime import datetime, timedelta
from datetime import time as time_cls
from pathlib import Path
from zoneinfo import ZoneInfo

VENV = Path(r"C:\venvs\asx-bot\Scripts")
# The release this file is part of (an export under %LOCALAPPDATA%\asx-bot\releases, or the
# checkout itself), and REPO: where config.yaml, .env, data/ and reports/ live. The task shim
# (scripts/deploy.py, asxbot.release) sets ASXBOT_HOME; run straight from the checkout the
# two are the same folder. Every child gets both, with PYTHONPATH on the release's src.
RELEASE = Path(__file__).resolve().parents[1]
REPO = Path(os.environ.get("ASXBOT_HOME") or RELEASE)


def release_env(extra: dict) -> dict:
    return {
        **os.environ, "ASXBOT_HOME": str(REPO), "ASXBOT_RELEASE": str(RELEASE),
        "PYTHONPATH": str(RELEASE / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
        **extra,
    }  # fmt: skip


def logs_dir() -> Path:
    """asxbot.log.logs_dir(), worked out here because asxbot lives on G: (a test holds the
    two to the same answer)."""
    override = os.environ.get("ASXBOT_LOG_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "asx-bot" / "logs"


LOG_DIR = logs_dir()
LOG = LOG_DIR / "arena_warmup.log"
FAILURES = Path(r"C:\venvs\asx-bot\task-failures.log")
SYD = ZoneInfo("Australia/Sydney")
WAIT_EVERY_S = 30
WAIT_UNTIL = time_cls(9, 45)  # Sydney: after this the morning's decisions have gone


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def write(log, line: str) -> None:
    try:
        log.write(line + "\n")
    except OSError:
        pass


def failure(text: str) -> None:
    """A line in the local-disk failures log (the settings folder may be out of reach)."""
    try:
        FAILURES.parent.mkdir(parents=True, exist_ok=True)
        with open(FAILURES, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp()}  {text}\n")
    except OSError:
        pass


def home_ready() -> bool:
    return (REPO / "config.yaml").exists()


def sydney_now() -> datetime:
    return datetime.now(SYD)


def trading_day(day) -> bool:
    """Is the ASX open on `day`? True when it cannot be told (then it is worth waiting)."""
    try:
        from asxbot.announcements.live import is_trading_day

        return bool(is_trading_day(day))
    except Exception:  # noqa: BLE001 - the release's calendar unreadable: assume it is
        return day.weekday() < 5


def wait_for_home(sleep=time.sleep, clock=sydney_now, is_trading_day=trading_day) -> bool:
    """Wait for the settings folder (ASXBOT_HOME on Google Drive) until 09:45 Sydney on a
    trading day, looking every 30 seconds. True once it is there; False if it never came,
    or the day is not one worth waiting on. Each minute of waiting is a line in the warm-up
    log, and the wait is noted once in task-failures.log."""
    if home_ready():
        return True
    now = clock()
    deadline = datetime.combine(now.date(), WAIT_UNTIL, tzinfo=SYD)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8", buffering=1) as log:
        if now >= deadline or not is_trading_day(now.date()):
            why = "after 09:45" if now >= deadline else "not an ASX trading day"
            write(log, f"=== warm-up {stamp()}: {REPO} is not available ({why}); not waiting ===")
            failure(f"ABORTED (warm-up): {REPO} is not available ({why}). Google Drive is not "
                    "mounted, which usually means nobody is logged in.")  # fmt: skip
            return False
        write(log, f"=== warm-up {stamp()}: {REPO} is not available yet (Google Drive not "
                   "mounted?); waiting for it until 09:45 ===")  # fmt: skip
        failure(f"WAITING (warm-up): {REPO} is not available at {now:%H:%M}; the warm-up looks "
                "again every 30 s until 09:45 and starts the watcher when it appears.")  # fmt: skip
        noted = now
        while True:
            sleep(WAIT_EVERY_S)
            if home_ready():
                write(log, f"{clock():%H:%M:%S} {REPO} is available; starting the watcher")
                return True
            now = clock()
            if now >= deadline:
                write(log, f"=== warm-up gave up {stamp()}: {REPO} still not available at "
                           "09:45 ===")  # fmt: skip
                failure(f"ABORTED (warm-up): {REPO} was still not available at 09:45; no "
                        "watcher today until it is started by hand (scripts\\watcher.py start).")
                return False
            if now - noted >= timedelta(minutes=1):
                write(log, f"{now:%H:%M:%S} still waiting for {REPO}")
                noted = now


def rotate() -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        from asxbot.log import rotate_daily

        rotate_daily(LOG)
    except (OSError, ImportError):
        pass  # a log that could not be rotated is still a log; carry on appending


def main() -> int:
    rotate()  # before any wait, so a wait's lines land in today's log
    if not wait_for_home():
        return 1
    os.chdir(REPO)
    env = release_env({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
                       "ASXBOT_LOG_DIR": str(LOG_DIR), "ASXBOT_STDOUT_LOG": str(LOG)})  # fmt: skip
    with open(LOG, "a", encoding="utf-8", buffering=1) as log:
        log.write(f"\n=== warm-up starting {stamp()} (hidden) from {RELEASE.name}, settings in "
                  f"{REPO} ===\n")  # fmt: skip
        try:
            from asxbot import proc as hidden

            proc = hidden.popen(
                [str(VENV / "asxbot.exe"), "arena", "watch", "--until", "auto"],
                cwd=REPO, env=env, stdin=hidden.DEVNULL, stdout=hidden.PIPE,
                stderr=hidden.STDOUT,
            )  # fmt: skip
        except OSError as e:
            log.write(f"could not start the watcher: {e}\n")
            log.write(f"=== warm-up finished {stamp()} (exit 1) ===\n")
            return 1
        for raw in proc.stdout:
            # Keep draining even if a write fails: a launcher that stopped reading would
            # block the watcher on a full pipe.
            write(log, raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        code = proc.wait()
        write(log, f"=== warm-up finished {stamp()} (exit {code}) ===")
    return code


if __name__ == "__main__":
    sys.exit(main())
