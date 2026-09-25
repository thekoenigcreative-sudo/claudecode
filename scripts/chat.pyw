"""The Trader chat with no window: `asxbot chat`, started hidden and kept running.

Started at logon by the Windows scheduled task "ASXBot Chat"
(scripts/register_chat_task.ps1) as

    C:\\venvs\\asx-bot\\Scripts\\pythonw.exe "G:\\My Drive\\asx-bot\\scripts\\chat.pyw"

It runs `asxbot chat` (src/asxbot/chat.py: Rick's Telegram chat with the Trader) under
CREATE_NO_WINDOW and writes everything it prints to chat.log in the local logs folder
(%LOCALAPPDATA%/asx-bot/logs, off Google Drive - asxbot.log.logs_dir says why), line by
line as it arrives. chat.log is rotated when the day changes (chat.log.<date>, 30 kept).

What the log shows at a start, in order:
    === chat launcher starting <yyyy-mm-dd HH:MM> (pid N) ===
    ... Trader chat starting on commit <sha> (<branch>, botctl <version>, pid N)
    ... Running. Polling @<bot> (token ...<last 4>) for Rick's chat <id>; state in <folder>

If the chat stops by itself it is started again after 60 seconds, at most 5 times an hour,
except when a restart cannot help (the exit code says so; chat.py lists them):
    2  config error                     3  Telegram token or chat id problem
    10 OpenClaw still polls this bot (remove its channel account first)
Exit 11 (another chat holds the lock) and 12 (Telegram 409: another program polls the bot)
are retried: at a task restart the old chat is still finishing its last seconds.

The chat is told this launcher's pid (ASXBOT_CHAT_PARENT) and exits when it is gone, so
ending the task never leaves an orphan polling Telegram with old code.

Exit code: 1 if the repo is not there; 0 if the chat stopped normally; otherwise the chat's
last exit code (a stop code, or the one it gave up on).
"""

import os
import sys
import time
from datetime import date, datetime
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
ASXBOT = [str(VENV / "asxbot.exe"), "chat"]
RESTART_WAIT_S = 60
MAX_RESTARTS_PER_HOUR = 5
STOP_CODES = {
    2: "config error",
    3: "Telegram token or chat id problem",
    10: "OpenClaw still polls this bot",
}


def logs_dir() -> Path:
    """asxbot.log.logs_dir(), worked out here because asxbot lives on G: (a test holds the
    two to the same answer)."""
    override = os.environ.get("ASXBOT_LOG_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "asx-bot" / "logs"


LOG_DIR = logs_dir()
LOG = LOG_DIR / "chat.log"
FAILURES = Path(r"C:\venvs\asx-bot\task-failures.log")


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


class DailyLog:
    """chat.log, reopened (and the old day's file rotated) when the date changes: the chat
    runs for days, and one file held open for ever would never rotate."""

    def __init__(self):
        self.fh = None
        self.day = None
        self._open()

    def _open(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            from asxbot.log import rotate_daily

            rotate_daily(LOG)
        except OSError:
            pass  # a log that could not be rotated is still a log; carry on appending
        self.fh = open(LOG, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        self.day = date.today()

    def write(self, line: str) -> None:
        try:
            if date.today() != self.day:
                self.fh.close()
                self._open()
            self.fh.write(line + "\n")
        except OSError:
            pass

    def close(self) -> None:
        if self.fh is not None:
            self.fh.close()


def run_once(log: DailyLog, env: dict) -> int:
    # Imported here, after the check that the repo is mounted: asxbot lives on G:.
    from asxbot import proc as hidden

    try:
        child = hidden.popen(
            ASXBOT, cwd=REPO, env=env, stdin=hidden.DEVNULL, stdout=hidden.PIPE,
            stderr=hidden.STDOUT,
        )  # fmt: skip
    except OSError as e:
        log.write(f"could not start the chat: {e}")
        return 1
    for raw in child.stdout:
        # Keep draining even if a write fails: a launcher that stopped reading would block
        # the chat on a full pipe.
        log.write(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
    return child.wait()


def main() -> int:
    if not (REPO / "config.yaml").exists():
        # G: is a Google Drive mount that exists only inside Rick's logged-in session.
        FAILURES.parent.mkdir(parents=True, exist_ok=True)
        with open(FAILURES, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp()}  ABORTED (chat): {REPO} is not available. Google Drive is "
                     "not mounted, which usually means nobody is logged in.\n")  # fmt: skip
        return 1
    os.chdir(REPO)
    env = release_env({
        "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
        "ASXBOT_LOG_DIR": str(LOG_DIR), "ASXBOT_CHAT_PARENT": str(os.getpid()),
    })  # fmt: skip
    log = DailyLog()
    log.write(f"\n=== chat launcher starting {stamp()} (pid {os.getpid()}) from {RELEASE.name}, "
              f"settings in {REPO} ===")  # fmt: skip
    restarts: list[float] = []
    try:
        while True:
            code = run_once(log, env)
            log.write(f"=== chat exited {code} at {stamp()} ===")
            if code == 0:
                return 0
            if code in STOP_CODES:
                log.write(f"not restarting: {STOP_CODES[code]}. Fix that, then restart the "
                          'task: schtasks /run /tn "ASXBot Chat"')  # fmt: skip
                return code
            now = time.time()
            restarts = [t for t in restarts if now - t < 3600]
            if len(restarts) >= MAX_RESTARTS_PER_HOUR:
                log.write(f"gave up: {MAX_RESTARTS_PER_HOUR} restarts in the last hour. "
                          'Restart the task by hand: schtasks /run /tn "ASXBot Chat"')  # fmt: skip
                return code
            restarts.append(now)
            log.write(f"restarting in {RESTART_WAIT_S}s ({len(restarts)} of "
                      f"{MAX_RESTARTS_PER_HOUR} this hour)")  # fmt: skip
            time.sleep(RESTART_WAIT_S)
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
