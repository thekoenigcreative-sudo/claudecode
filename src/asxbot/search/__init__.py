"""The rules-only strategy search (CLOUD_BRIEF_SEARCH.md, 26 Sep 2026).

Plain code decides every entry and exit - no model is asked anything - so many ideas can be
run cheaply on the Practice Lab's windows (lab/splits.py: TUNE is the practice split, VALIDATE
the check set, the LOCKED TEST sealed and run once per finalist) and judged on WINNER.md.

Unlike the lab's time machine (lab/sim.py, which replays the live playbooks minute by minute
through the arena broker), this engine works on whole days of 1-minute bars at once, so a
setup the live playbooks do not have (stocks in play, multi-day announcement drift) can be
tried without touching any frozen playbook. It keeps the replay's timing and fill rules:
a decision sees only bars that had closed; an order fills on a later bar, never the one it
was decided in; fills are capped at a share of each bar's volume; stops fill at the stop or
worse; costs are charged both ways (search/costs.py).

    python -m asxbot.search run   --history <dir> [--data <dir>]
    python -m asxbot.search report
"""

LABEL = (
    "RULES-ONLY SEARCH - fake money, plain-code rules, IBKR 1-minute history. "
    "Not live, not a verdict on the AI trader."
)
SURVIVORSHIP = (
    "Survivorship bias: the universe is today's index list applied to every past day, so the "
    "stocks that later fell out (often after falling) are missing. Results are biased upward; "
    "a loss here is a stronger negative than it looks, and a profit is weaker than it looks."
)
