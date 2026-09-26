"""The evening routine with no window to close: settle, mark, report - started hidden.

Started by the Windows scheduled task "ASXBot Arena Evening" at 19:30 and 20:30 Sydney,
weekdays, as

    C:\\venvs\\asx-bot\\Scripts\\pythonw.exe "G:\\My Drive\\asx-bot\\scripts\\arena_evening.pyw"

(scripts/schedule_evening_hidden.ps1 points the task here). It does what arena_evening.ps1
does, in the same order and with the same log lines in arena_evening.log:

  * `asxbot arena evening-due` first; for the wrong slot today (daylight saving moves the
    report an hour) it logs "skipped <time>: not today's evening slot" and stops;
  * otherwise a blank line, "=== evening report <yyyy-mm-dd HH:MM> ===", then for each of
    `arena resolve`, `arena mark`, `arena journal` (since 26 Sep 2026: the day's trading
    journal, whose three lines the report carries) and `arena report --agent --send` a
    "--- asxbot <args> ---" header followed by everything the step printed. A failed step
    does not stop the next one, as before.

Why: on 23 Sep 2026 the watcher, which ran in a visible console window, died when that
window was closed at 13:32. The evening task ran the same way; closing its window would
lose the day's settlement and the report. pythonw has no console (the venv's must be
CPython's GUI launcher, not uv's console one - TRACKER #32), and each step runs
under CREATE_NO_WINDOW, so it and the OpenClaw call the report makes share one console
that has no window at all.

Two differences from the PowerShell version, both deliberate:
  * output is written line by line as it arrives (and the steps run unbuffered), not all
    at once when a step returns, so a killed run still leaves its last lines;
  * because of that, a line after a step's header no longer means the step returned. Each
    step is followed by "--- exit <code> ---" when it has; arena/capital.py reads that.

The logs are in the local logs folder (%LOCALAPPDATA%/asx-bot/logs), not on Google Drive:
at 08:14 on 24 Sep 2026 Drive silently cut off the watcher's long-open log handles
(asxbot.log.logs_dir says more). arena_evening.log is rotated daily (30 kept). When the
routine has run, every file in that folder is copied to data/logs/ whole - nothing on Drive
is held open - so the record is still in the repo's data folder for the other PC.

The exit code is what the PowerShell version gave the task: 1 if the repo is not there
or asxbot cannot be started, else 0 whatever the steps returned (their codes are in the
log). The task restarts a failed run up to three times, and a restart must never send a
second report.

The watcher now stops at 19:31 (20:31), a minute after announcements end, with one last
poll (26 Sep 2026; it stopped at 19:25). So before settling, this waits - up to 15
minutes, with a line in the log - for a running watcher to finish, and the books are still
settled by one process at a time.
"""

import os
import sys
import time
from datetime import datetime
from pathlib import Path

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
ASXBOT = [str(VENV / "asxbot.exe")]


def logs_dir() -> Path:
    """asxbot.log.logs_dir(), worked out here because asxbot lives on G: (a test holds the
    two to the same answer)."""
    override = os.environ.get("ASXBOT_LOG_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "asx-bot" / "logs"


LOG_DIR = logs_dir()
LOG = LOG_DIR / "arena_evening.log"
MIRROR = REPO / "data" / "logs"
FAILURES = Path(r"C:\venvs\asx-bot\task-failures.log")
STEPS = (
    ("arena", "resolve"),
    ("arena", "mark"),
    # The trading journal (26 Sep 2026): after the books are settled and marked, before the
    # report, which carries its three lines (arena/journal.py; one model call per agent book).
    ("arena", "journal"),
    ("arena", "report", "--agent", "--send"),
)


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def failure(text: str) -> None:
    """A line in the local-disk failures log, for when the repo's own log is out of reach."""
    try:
        FAILURES.parent.mkdir(parents=True, exist_ok=True)
        with open(FAILURES, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp()}  {text}\n")
    except OSError:
        pass


def write(log, line: str) -> None:
    try:
        log.write(line + "\n")
    except OSError:
        pass


def start(args, env: dict):
    # Imported here, after the check that the repo is mounted: asxbot lives on G:.
    from asxbot import proc as hidden

    return hidden.popen(
        [*ASXBOT, *args], cwd=REPO, env=env, stdin=hidden.DEVNULL,
        stdout=hidden.PIPE, stderr=hidden.STDOUT,
    )  # fmt: skip


def run_step(log, args, env: dict) -> int:
    write(log, f"--- asxbot {' '.join(args)} ---")
    try:
        proc = start(args, env)
    except OSError as e:
        write(log, f"could not start asxbot: {e}")
        write(log, "--- exit 1 ---")
        return 1
    for raw in proc.stdout:
        # Keep draining even if a write fails: a launcher that stopped reading would block
        # the step on a full pipe.
        write(log, raw.decode("utf-8", errors="replace").rstrip("\r\n"))
    code = proc.wait()
    write(log, f"--- exit {code} ---")
    return code


WATCHER_WAIT_S = 15 * 60
WATCHER_POLL_S = 10


def watcher_still_running() -> str:
    """What the heartbeat says if the day's watcher is still running, else ""."""
    try:
        from asxbot.arena.lock import watcher_running

        running, said = watcher_running(REPO / "data")
        return said if running else ""
    except Exception:  # noqa: BLE001 - cannot tell: do not hold the evening up
        return ""


def wait_for_watcher(log, sleep=time.sleep, limit_s: float = WATCHER_WAIT_S) -> bool:
    """Wait for a running watcher to exit (its last poll is at 19:31). True if it has."""
    said = watcher_still_running()
    if not said:
        return True
    write(log, f"waiting for the watcher to stop before settling: {said}")
    waited = 0.0
    while said and waited < limit_s:
        sleep(WATCHER_POLL_S)
        waited += WATCHER_POLL_S
        said = watcher_still_running()
    write(log, f"the watcher has stopped ({waited:.0f} s)" if not said else
          f"the watcher is still running after {limit_s / 60:.0f} min; settling anyway")
    return not said


def open_log():
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            from asxbot.log import rotate_daily

            rotate_daily(LOG)
        except OSError:
            pass  # a log that could not be rotated is still a log; carry on appending
        return open(LOG, "a", encoding="utf-8", buffering=1)
    except OSError as e:
        # Settling the day matters more than its log: carry on, and say so somewhere.
        failure(f"evening: could not open {LOG} ({e}); running without it")
        return open(os.devnull, "w", encoding="utf-8")


def main() -> int:
    if not (REPO / "config.yaml").exists():
        # G: is a Google Drive mount that exists only inside Rick's logged-in session.
        failure(f"ABORTED (evening): {REPO} is not available. Google Drive is not mounted, "
                "which usually means nobody is logged in.")  # fmt: skip
        return 1
    os.chdir(REPO)
    env = release_env({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
                       "ASXBOT_LOG_DIR": str(LOG_DIR)})  # fmt: skip
    now = stamp()
    with open_log() as log:
        try:
            due = start(("arena", "evening-due"), env)
        except OSError as e:
            write(log, f"could not run asxbot arena evening-due at {now}: {e}")
            return 1
        due.communicate()
        if due.returncode != 0:
            write(log, f"skipped {now}: not today's evening slot")
            return 0
        write(log, "")
        write(log, f"=== evening report {now} ===")
        wait_for_watcher(log)
        for args in STEPS:
            run_step(log, args, env)
    mirror()
    return 0


def mirror() -> None:
    """Copy the day's logs to data/logs/ on Drive, whole, after this log is closed."""
    try:
        from asxbot.log import mirror_logs

        mirror_logs(LOG_DIR, MIRROR)
    except OSError as e:
        failure(f"evening: could not copy the logs from {LOG_DIR} to {MIRROR} ({e})")


if __name__ == "__main__":
    sys.exit(main())
