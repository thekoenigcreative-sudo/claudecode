r"""Re-price ARN-000002 (A1M, 23 Sep) under the fixed fill rule, once. TRACKER.md #24.
See src/asxbot/arena/correction.py for exactly what it changes and when it refuses.

Run from the repo folder, after the watcher has stopped (19:25) and before the $10,000
top-up (scripts/arena_add_capital.py):

    C:\venvs\asx-bot\Scripts\python.exe scripts\correct_fill_arn000002.py --dry-run
    C:\venvs\asx-bot\Scripts\python.exe scripts\correct_fill_arn000002.py

Refuses, writing nothing, while any arena process or scheduled arena task is running, or if
it has already been applied.
"""

import sys

from asxbot.arena.correction import main

if __name__ == "__main__":
    sys.exit(main())
