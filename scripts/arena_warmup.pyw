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
"""

import os
import sys
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
    env = release_env({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
                       "ASXBOT_LOG_DIR": str(LOG_DIR), "ASXBOT_STDOUT_LOG": str(LOG)})  # fmt: skip
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        from asxbot.log import rotate_daily

        rotate_daily(LOG)
    except OSError:
        pass  # a log that could not be rotated is still a log; carry on appending
    with open(LOG, "a", encoding="utf-8", buffering=1) as log:
        log.write(f"\n=== warm-up starting {stamp()} (hidden) from {RELEASE.name}, settings in "
                  f"{REPO} ===\n")  # fmt: skip
        try:
            # Imported here, after the check that the repo is mounted: asxbot lives on G:.
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
