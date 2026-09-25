"""The watchdog: one check of the arena watcher, from outside it. No window.

Run every 5 minutes by the scheduled task "ASXBot Arena Watchdog"
(scripts/schedule_watchdog.ps1) as

    C:\\venvs\\asx-bot\\Scripts\\pythonw.exe "G:\\My Drive\\asx-bot\\scripts\\arena_watchdog.pyw"

See src/asxbot/arena/watchdog.py for what it checks. It never starts or stops anything.
"""

# 26 Sep 2026: the tasks now start in %LOCALAPPDATA%\asx-bot, not on G:. Nothing here reads
# a relative path, but the settings folder is made the working folder once it exists, so
# nothing ever depends on where the task started.
import os
import sys

_home = os.environ.get("ASXBOT_HOME")
if _home and os.path.isdir(_home):
    os.chdir(_home)

from asxbot.arena.watchdog import main  # noqa: E402 - after the chdir

if __name__ == "__main__":
    sys.exit(main())
