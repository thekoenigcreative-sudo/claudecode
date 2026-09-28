# The AI trading team's live paper book (28 Sep 2026)

Rick, 26 Sep: "even then i'll want to actually test the agent team like the other trader on a
live environment". The team built and tested in the simulator on 26 Sep (docs/tsim.md 4b:
announcement readers, a market scanner, five strategy specialists, a risk manager with a veto,
the Opus 5.5 decision-maker, a researcher after the close) now trades live on paper, in its OWN
A$20,000 book, beside the frozen 10-day test. First live day: **Wed 30 Sep 2026**
(config.yaml `arena.team.start`).

Code: `src/asxbot/arena/team/` (and `src/asxbot/lab/tsim/live.py`, the simulator's side).
Settings: config.yaml `arena.team`. Book: `data/arena/team/`.

## What it is, and what it is not

- **The same team as the simulator's.** `lab/tsim/team.py`'s TeamTrader, unchanged: the same
  prompts, stages and models (decisions and the risk manager on Opus 5.5 at high; the scanner
  and the five specialists on Sonnet 5 medium; one reader per announcement on Sonnet 5 low;
  the researcher on Opus 5.5 high), called with `claude -p` directly (never OpenClaw, never
  the frozen agents' sessions). Its own lessons carry from day to day, in date order, as in a
  simulated run - it is not frozen.
- **Its own book, with the simulator's broker.** Orders go to the simulator's broker
  (`lab/tsim/broker.py`: market, limit, stop, stop-limit, trailing, on-close, brackets,
  shorts in the ASX 200 only, 1x leverage) filling against the LIVE bars, with the simulator's
  costs: IBKR's 0.088% min $6.60 a side, half the modelled spread each way, impact, no bar
  filling more than 20% of what it traded, short borrow 3% a year. There is no IBKR paper
  account (the Gateway is read-only on the live account), so "the paper account" is this
  sub-book; nothing here can place a real order (tests/test_team_live.py).
- **It never touches the frozen test.** Its book is not an arena playbook: the frozen books'
  rules, limits, accounts, agents, prompts and reports are unchanged. It reads the bars the
  watcher's feed already holds and asks IBKR for nothing (no stream, poll or quote is added),
  so the frozen books' data is what it would have been without it. Every model call runs on
  the team's own thread: the frozen books never wait for it. Its results are added to the
  evening report by code AFTER the frozen agent has written, so no frozen agent sees them.

## The day (arena/team/session.py)

The simulator's clock rules (lab/tsim/engine.py) on the real clock, in one thread inside the
watcher (started by `runner.tick` every cycle from 09:00 on a trading day; restarted after two
minutes if it dies, at most six times a day, resuming the day from the book):

- 09:40 pre-open: the team is woken with the overnight news, its positions and orders carried
  from yesterday, and its lessons. Orders sent now join the opening auction.
- Each minute, once its bar is delivered (20 s after it closes): decisions that reached the
  broker are applied through the hard limits, the broker works every order against the bar,
  plain code checks the team's own alerts (price levels, times, relative volume, news,
  scanner thresholds, moves, its fills), and the team is woken only if something it asked
  for happened and it is not still thinking. Its orders fill only from bars that START after
  the moment its call returned.
- A stock the feed has no streaming line for arrives with its next poll (~6 minutes): its
  minutes are worked when they come in, at the prices they had (`deferred`), up to 8 minutes
  late; before the closing auction the session waits for them.
- 16:20 after the close: the decision-maker writes its notes and lessons (and may leave GTC
  orders, reviewed by the risk manager), the researcher writes lessons and new ideas for the
  lab's search. Day orders expire; borrow is charged; the book is marked.
- It may hold overnight (its multi-day specialist), always through the decision-maker's
  after-close call; a position is never carried unmanaged (below).

## Hard limits, in code (arena/team/limits.py)

Exits, cancels and stop changes always pass. Anything that ADDS exposure is refused (and the
team told why) when:

| Limit | Setting |
|---|---|
| Kill switch | `asxbot arena team kill --why "..."`: working orders cancelled, positions closed at market at the next minute, the team asked nothing until `asxbot arena team unkill` |
| Daily loss limit | equity down 15% from the day's start (Level 1's figure): no new position until tomorrow |
| Rick's pause | "stop it for today" / "kill switch" / "no new entries today" in the Trader chat (arena/pause.py): every book, the team included |
| Live feed | down, delayed or stale; or the stock's own bars stale (a streamed stock over the feed's age limit, a polled one over 180 s) - read-only checks |
| Closing-volume cap | a position is cut to 20% of the stock's usual closing-auction volume (14 sessions), so it can always be sold in one closing auction |

The **usage stop**: no model call once the plan's weekly usage reaches 70% (`usage_stop`,
Rick's stop for every bot), nor while the Foreman has Claude paused (claude_pause.json). The
team goes quiet; its resting stops and brackets keep working in code; anything it still holds
is sold in the closing auction by code rather than carried unmanaged. The team's calls are
never counted in the lab's weekly share.

## Evening (scripts/arena_evening.pyw)

1. `asxbot arena journal` also writes the team's file beside the frozen books'
   (`data/arena/journal/<day>/asx_team.md`): its own after-close entry and the day's facts,
   no extra model call.
2. `asxbot arena team evening` - calibration check A (below).
3. `asxbot arena report` - after the frozen agent's text and the journal's lines, code adds
   **"AI team"**: today's P&L after all costs, round trips, orders, wakes, calls and tokens,
   what the limits refused, its lesson, and ONE TABLE with the team beside every frozen book
   (the day trader's agent from 25 Sep, the v2 agent, both rule bots) on the same measures:
   today, total, green/red days, round trips won after costs, worst drop, fees.
4. `asxbot arena team calibrate --rerun` - calibration check B (model calls; its line is in
   the next evening's report).

## Calibration: each live day in the simulator (arena/team/calibrate.py)

- **A. The same decisions** (plain code, free): the team's own order actions, exactly as they
  reached the live broker and when, are sent to the simulator's broker over the stored history
  (IBKR's 1-minute history fetched at 17:30) from the same start-of-day book. Differences are
  the simulator's market against the live feed's: orders filled the same, the median fill
  difference in basis points, P&L live vs simulated. On identical bars it reproduces the book
  exactly (a test holds it to that).
- **B. The same team** (model calls, at most 3% of the week, stops at the usage stop): the
  simulated team re-runs the whole day from the same start-of-day book and lessons. Its trades
  (stock and side), wakes and P&L against the live day. The models do not answer the same
  way twice, so B measures the simulator and the team's own variance together; A isolates the
  market.

Results: `data/arena/team/calibration/<day>.json`, `reports/team_calibration_<day>.md`.

## Commands

    asxbot arena team status                    # the book, its thread, the kill switch
    asxbot arena team kill --why "..."          # KILL SWITCH (and unkill)
    asxbot arena team replay --day 2026-09-25   # the dry run: a recorded day, a scratch book
    asxbot arena team evening [--day D]         # check A
    asxbot arena team calibrate --rerun [--day D]

The book: `data/arena/team/state.json` (the account, alerts, the team's day state; saved every
minute that changed it), `start/<day>.json` (the book as the day began), `days/<day>.json` (the
day's record), `actions/<day>.jsonl` (every order action, as sent and as the limits left it),
`calls/<day>.jsonl` (every model call, prompt and reply), `status.json` (the thread, each
minute), `kill.json`, `calibration/`, and the team's own lessons in `journal/` (the
simulator's format). The watcher's market-hours lock counts its positions and working orders
(arena/lock.py).
