r"""Re-price ARN-000003 (A1M's 3,000-share take-profit, 23 Sep) under the volume-aware fill
rule, once. TRACKER.md #25. See src/asxbot/arena/correction_arn000003.py for exactly what it
changes and when it refuses.

Run from the repo folder, with the watcher stopped and after scripts\correct_fill_arn000002.py
has been applied:

    C:\venvs\asx-bot\Scripts\python.exe scripts\correct_fill_arn000003.py --dry-run
    C:\venvs\asx-bot\Scripts\python.exe scripts\correct_fill_arn000003.py

Refuses, writing nothing, while any arena process or scheduled arena task is running, or if
it has already been applied.
"""

import sys

from asxbot.arena.correction_arn000003 import main

if __name__ == "__main__":
    sys.exit(main())
