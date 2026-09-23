"""The evening routine with no window to close: settle, mark, report - started hidden.

Started by the Windows scheduled task "ASXBot Arena Evening" at 19:30 and 20:30 Sydney,
weekdays, as

    C:\\venvs\\asx-bot\\Scripts\\pythonw.exe "G:\\My Drive\\asx-bot\\scripts\\arena_evening.pyw"

(scripts/schedule_evening_hidden.ps1 points the task here). It does what arena_evening.ps1
does, in the same order and with the same log lines in data/arena_evening.log:

  * `asxbot arena evening-due` first; for the wrong slot today (daylight saving moves the
    report an hour) it logs "skipped <time>: not today's evening slot" and stops;
  * otherwise a blank line, "=== evening report <yyyy-mm-dd HH:MM> ===", then for each of
    `arena resolve`, `arena mark` and `arena report --agent --send` a
    "--- asxbot <args> ---" header followed by everything the step printed. A failed step
    does not stop the next one, as before.

Why: on 23 Sep 2026 the watcher, which ran in a visible console window, died when that
window was closed at 13:32. The evening task ran the same way; closing its window would
lose the day's settlement and the report. pythonw has no console, and each step runs
under CREATE_NO_WINDOW, so it and the OpenClaw call the report makes share one console
that has no window at all.

Two differences from the PowerShell version, both deliberate:
  * output is written line by line as it arrives (and the steps run unbuffered), not all
    at once when a step returns, so a killed run still leaves its last lines;
  * because of that, a line after a step's header no longer means the step returned. Each
    step is followed by "--- exit <code> ---" when it has; arena/capital.py reads that.

The exit code is what the PowerShell version gave the task: 1 if the repo is not there
or asxbot cannot be started, else 0 whatever the steps returned (their codes are in the
log). The task restarts a failed run up to three times, and a restart must never send a
second report.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

VENV = Path(r"C:\venvs\asx-bot\Scripts")
ASXBOT = [str(VENV / "asxbot.exe")]
REPO = Path(__file__).resolve().parents[1]
LOG = REPO / "data" / "arena_evening.log"
FAILURES = Path(r"C:\venvs\asx-bot\task-failures.log")
STEPS = (
    ("arena", "resolve"),
    ("arena", "mark"),
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
        # Keep draining even if a write fails (Drive can hold the file for a moment): a
        # launcher that stopped reading would block the step on a full pipe.
        write(log, raw.decode("utf-8", errors="replace").rstrip("\r\n"))
    code = proc.wait()
    write(log, f"--- exit {code} ---")
    return code


def open_log():
    try:
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
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
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
        for args in STEPS:
            run_step(log, args, env)
    return 0


if __name__ == "__main__":
    sys.exit(main())
