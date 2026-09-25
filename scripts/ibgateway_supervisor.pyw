"""The IB Gateway supervisor: one check, no window.

Run at logon and every 2 minutes by the scheduled task "ASXBot IB Gateway Supervisor"
(scripts/register_ibgateway_tasks.ps1) as

    C:\\venvs\\asx-bot\\Scripts\\pythonw.exe "G:\\My Drive\\asx-bot\\scripts\\ibgateway_supervisor.pyw"

See src/asxbot/ibkr/supervisor.py for what it checks and does.
"""

# 26 Sep 2026: the tasks now start in %LOCALAPPDATA%\asx-bot, not on G:. Nothing here reads
# a relative path, but the settings folder is made the working folder once it exists, so
# nothing ever depends on where the task started.
import os
import sys

_home = os.environ.get("ASXBOT_HOME")
if _home and os.path.isdir(_home):
    os.chdir(_home)

from asxbot.ibkr.supervisor import main  # noqa: E402 - after the chdir

if __name__ == "__main__":
    sys.exit(main())
