"""The AI trading team's own paper book, live beside the frozen 10-day test (28 Sep 2026).

Rick, 26 Sep: "even then i'll want to actually test the agent team like the other trader on a
live environment". The team (announcement readers, a market scanner, five strategy
specialists and the researcher on Sonnet 5 / Opus 5.5; the decision-maker and a risk manager
with a veto on Opus 5.5 at high) was built and tested in the simulator (lab/tsim/team.py).
This package runs that same team on live IBKR 1-minute bars and the live announcement feed,
in its own A$20,000 paper book, inside the watcher (docs/team.md):

  market.py   the simulator's minute grid, filled from the bars the watcher already fetches
              (no extra IBKR request: the frozen books' data is untouched)
  limits.py   plain-code hard limits: kill switch, daily loss limit, Rick's pause, stale
              feed, the closing-volume cap; the team's weekly Claude usage stop
  session.py  the day, minute by minute - the simulator's clock rules on a live clock (or on
              a virtual one for the dry run and the calibration replays)
  runner.py   the watcher's side: one thread, started and watched every cycle
  score.py    the book scored like every arena book, side by side, for the evening report
  calibrate.py each live day replayed in the simulator: how closely it matched

Nothing here can place a real order: the team's broker is the simulator's (lab/tsim/broker.py),
filling against real bars with the simulator's costs. The frozen books, their rules and their
agents are never read or changed by any of it.
"""
