"""The watchdog: one check of the arena watcher, from outside it. No window.

Run every 5 minutes by the scheduled task "ASXBot Arena Watchdog"
(scripts/schedule_watchdog.ps1) as

    C:\\venvs\\asx-bot\\Scripts\\pythonw.exe "G:\\My Drive\\asx-bot\\scripts\\arena_watchdog.pyw"

See src/asxbot/arena/watchdog.py for what it checks. It never starts or stops anything.
"""

import sys

from asxbot.arena.watchdog import main

if __name__ == "__main__":
    sys.exit(main())
