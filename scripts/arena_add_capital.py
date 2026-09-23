"""Add $10,000 to both arena accounts, once. See src/asxbot/arena/capital.py for the rules.

Run from the repo folder, after the evening routine has finished:

    C:\\venvs\\asx-bot\\Scripts\\python.exe scripts\\arena_add_capital.py --dry-run
    C:\\venvs\\asx-bot\\Scripts\\python.exe scripts\\arena_add_capital.py

Refuses, writing nothing, while any arena process is running or before the evening routine
has finished.
"""

import sys

from asxbot.arena.capital import main

if __name__ == "__main__":
    sys.exit(main())
