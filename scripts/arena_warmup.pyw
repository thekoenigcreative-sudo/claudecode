"""Tactic 1 warm-up with no window to close: the watcher, started hidden.

Started by the Windows scheduled task "ASXBot Arena Warmup" at 07:30 Sydney, weekdays, as

    C:\\venvs\\asx-bot\\Scripts\\pythonw.exe "G:\\My Drive\\asx-bot\\scripts\\arena_warmup.pyw"

(scripts/schedule_watcher_hidden.ps1 points the task here). It does what arena_warmup.ps1
does - run `asxbot arena watch --until auto` from the repo folder and append everything it
prints to data/arena_warmup.log between the same start and finish lines - without a
console window.

Why: on 23 Sep 2026 the watcher ran in a visible console window, which was closed at
13:32 (exit 0xC000013A, "console closed / Ctrl+C") and took the watcher with it, mid-poll
and without a word. pythonw has no console. The watcher itself is started with
CREATE_NO_WINDOW, so it and everything it runs (the OpenClaw agent calls) share one
console that has no window at all: nothing to close, and nothing flashing up on screen.

Unlike the PowerShell version, output is written line by line as it arrives, not all at
once at the end, so a killed watcher still leaves its last lines in this log.
"""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

VENV = Path(r"C:\venvs\asx-bot\Scripts")
REPO = Path(__file__).resolve().parents[1]
LOG = REPO / "data" / "arena_warmup.log"
FAILURES = Path(r"C:\venvs\asx-bot\task-failures.log")
CREATE_NO_WINDOW = 0x08000000


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def write(log, line: str) -> None:
    try:
        log.write(line + "\n")
    except OSError:
        pass


def main() -> int:
    if not (REPO / "config.yaml").exists():
        # G: is a Google Drive mount that exists only inside Rick's logged-in session.
        FAILURES.parent.mkdir(parents=True, exist_ok=True)
        with open(FAILURES, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp()}  ABORTED (warm-up): {REPO} is not available. Google Drive "
                     "is not mounted, which usually means nobody is logged in.\n")  # fmt: skip
        return 1
    os.chdir(REPO)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    with open(LOG, "a", encoding="utf-8", buffering=1) as log:
        log.write(f"\n=== warm-up starting {stamp()} (hidden) ===\n")
        try:
            proc = subprocess.Popen(
                [str(VENV / "asxbot.exe"), "arena", "watch", "--until", "auto"],
                cwd=REPO, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW,
            )  # fmt: skip
        except OSError as e:
            log.write(f"could not start the watcher: {e}\n")
            log.write(f"=== warm-up finished {stamp()} (exit 1) ===\n")
            return 1
        for raw in proc.stdout:
            # Keep draining even if a write fails (Drive can hold the file for a moment):
            # a launcher that stopped reading would block the watcher on a full pipe.
            write(log, raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        code = proc.wait()
        write(log, f"=== warm-up finished {stamp()} (exit {code}) ===")
    return code


if __name__ == "__main__":
    sys.exit(main())
