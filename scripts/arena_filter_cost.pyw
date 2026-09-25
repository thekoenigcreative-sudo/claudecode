"""The weekly filter-cost report with no window: `asxbot arena filter-cost --weeks 8 --send`.

Started by the scheduled task "ASXBot Filter Cost" (Sundays 18:00 Sydney) through its shim
(%LOCALAPPDATA%\\asx-bot\\bin\\arena_filter_cost.pyw), which runs this file from the deployed
release with ASXBOT_HOME set to the checkout. Until 25 Sep 2026 the task ran
scripts/arena_filter_cost.ps1 straight from the checkout on Google Drive.

This REPORTS. It changes nothing. No threshold moves because of a number in this report; a
rule that looks expensive earns a dated change in config.yaml, made deliberately. The log
goes to arena_filter_cost.log in the local logs folder (%LOCALAPPDATA%\\asx-bot\\logs).
"""

import os
import sys
from datetime import datetime
from pathlib import Path

VENV = Path(r"C:\venvs\asx-bot\Scripts")
RELEASE = Path(__file__).resolve().parents[1]
HOME = Path(os.environ.get("ASXBOT_HOME") or RELEASE)
FAILURES = Path(r"C:\venvs\asx-bot\task-failures.log")


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
            fh.write(f"{stamp()}  ABORTED (filter cost): {HOME} is not available. Google Drive "
                     "is not mounted, which usually means nobody is logged in.\n")  # fmt: skip
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

    with open(log_dir / "arena_filter_cost.log", "a", encoding="utf-8", buffering=1) as log:
        log.write(f"\n=== filter cost starting {stamp()} from {RELEASE.name} ===\n")
        try:
            child = hidden.popen(
                [str(VENV / "asxbot.exe"), "arena", "filter-cost", "--weeks", "8", "--send"],
                cwd=HOME, env=env, stdin=hidden.DEVNULL, stdout=hidden.PIPE,
                stderr=hidden.STDOUT,
            )  # fmt: skip
        except OSError as e:
            log.write(f"could not start asxbot: {e}\n")
            log.write(f"=== filter cost finished {stamp()} (exit 1) ===\n")
            return 1
        for raw in child.stdout:
            log.write(raw.decode("utf-8", errors="replace").rstrip("\r\n") + "\n")
        code = child.wait()
        log.write(f"=== filter cost finished {stamp()} (exit {code}) ===\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
