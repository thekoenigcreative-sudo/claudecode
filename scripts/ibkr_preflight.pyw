"""The 07:20 IBKR pre-flight: `asxbot ibkr check`, and a Trader chat message only if it fails.

Run on weekdays at 07:20 by the scheduled task "ASXBot IBKR Preflight"
(scripts/register_ibgateway_tasks.ps1); it does nothing on days the ASX is shut. The result
goes to ibkr_preflight.log in the local logs folder. See supervisor.preflight.
"""

import sys

from asxbot.ibkr.supervisor import preflight_main

if __name__ == "__main__":
    sys.exit(preflight_main())
