"""The 07:20 IBKR pre-flight: `asxbot ibkr check`, and a Trader chat message only if it fails.

Run on weekdays at 07:20 by the scheduled task "ASXBot IBKR Preflight"
(scripts/register_ibgateway_tasks.ps1); it does nothing on days the ASX is shut. The result
goes to ibkr_preflight.log in the local logs folder. See supervisor.preflight.
"""

# 26 Sep 2026: the tasks now start in %LOCALAPPDATA%\asx-bot, not on G:. Nothing here reads
# a relative path, but the settings folder is made the working folder once it exists, so
# nothing ever depends on where the task started.
import os
import sys

_home = os.environ.get("ASXBOT_HOME")
if _home and os.path.isdir(_home):
    os.chdir(_home)

from asxbot.ibkr.supervisor import preflight_main  # noqa: E402 - after the chdir

if __name__ == "__main__":
    sys.exit(preflight_main())
