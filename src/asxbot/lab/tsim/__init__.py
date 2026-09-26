"""The proper trading simulator (CLOUD_BRIEF.md, Rick 26 Sep: "i dont feel the simulator is a
proper simulator"). Built alongside the Practice Lab's time machine (lab/sim.py), which stays
for calibration and rules-only variants of the live playbooks.

  market.py   the whole universe playing out minute by minute from the stored IBKR history,
              every announcement released at its real timestamp, nothing after "now"
  costs.py    commission per broker (IBKR and the cheapest API broker), spread by price and
              liquidity tier (at least one tick each way), slippage growing with size/volume
  broker.py   a simulated broker: market, limit, stop, stop-limit, trailing stop, market/limit
              on close; buy, sell, short (borrowable list), modify, cancel; brackets; opening
              and closing auctions; volume-capped partial fills; an account that carries over
  tools.py    what a trader sees: scanner, charts, daily history, the announcement feed, its
              account, orders and journal - anonymised before the models' knowledge cutoff
  alerts.py   the wake-ups a trader sets for itself (price, time, volume, news, scanner)
  engine.py   the clock: bars in, fills out, the trader woken only when something it asked
              for happens; its thinking time moves the clock
  ai.py       the AI trader (Claude Opus 5.5, high effort): free to choose what to look at and
              what to trade; its tokens are counted per simulated day
  rules.py    rules-only traders (the yardsticks, the search's candidates): plain code
  run.py      days in order with one account (continuity), independent runs in parallel
  fidelity.py the check against the live paper test before any result is trusted

Nothing here places an order anywhere: the broker is a simulation over stored history.
"""
