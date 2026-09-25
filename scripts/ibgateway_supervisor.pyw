"""The IB Gateway supervisor: one check, no window.

Run at logon and every 2 minutes by the scheduled task "ASXBot IB Gateway Supervisor"
(scripts/register_ibgateway_tasks.ps1) as

    C:\\venvs\\asx-bot\\Scripts\\pythonw.exe "G:\\My Drive\\asx-bot\\scripts\\ibgateway_supervisor.pyw"

See src/asxbot/ibkr/supervisor.py for what it checks and does.
"""

import sys

from asxbot.ibkr.supervisor import main

if __name__ == "__main__":
    sys.exit(main())
