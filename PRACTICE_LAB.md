# The Practice Lab: constant practice for the day trader

Rick, 25 Sep 2026: "I want to go all in on getting the day trading system perfected by
constantly practising" ... "particularly how to do the constant simulation until we have a
winner". Built 26 Sep 2026 on the replay engine (`arena/replay_ibkr.py`, asx-replay /
asx-review). Winner criteria: WINNER.md (written before the search). Code: `src/asxbot/lab/`.
CLI: `asxbot lab ...`. Results outside Google Drive in `%LOCALAPPDATA%\asx-bot\lab`; the
small registry and scoreboard in `data/lab/` (the settings checkout, not git).

## 1. The time machine (point-in-time simulator)

`lab/sim.py` runs one trading day for one VARIANT through the live code, faster than real
time: the day's IBKR 1-minute bars from the history cache, the ASX announcements from the
archive, a clock that steps to 20 s past each minute (when the live feed has closed the minute
before), a scratch arena and book per day ($20,000), and the live broker (costs, slippage,
fills capped at 20% of a bar's volume, stops before targets, breakeven/half/trail, flat by
15:50). Every decision - the rule bot's and the agent's - sees only the bars and announcements
that existed at that simulated moment (`MarketView` cuts the bars at the clock;
announcements are clipped to `released_at` before the clock; daily prices end the day before).

The **agent** is in the loop: for each day-trader setup the lab builds the SAME packet the live
trader-decider gets (`daytrader.agent_packet`) and asks Claude directly (`claude -p`, the
decider's model and effort from config.yaml, its standing instructions from
`docs/agents/trader-decider.AGENTS.md` as the system prompt, no tools) - never through
OpenClaw, so the live agents' sessions and the gateway are untouched. The answer's latency is
added to the simulated clock (the fill is the first bar after the answer; over 60 s is a
rejection, as live). Answers are cached by (packet, model, prompt), so a re-run costs nothing.
Announcements v2's agent (reader + decider, which read the PDF) is phase 2; v2's rule bot is
in the lab from day one.

Rule-bot days cost no Claude usage and run in parallel worker processes. Agent days do, so
they are budgeted (`lab.agent_calls_per_batch`) and stop, like every build, when weekly Claude
usage reaches Rick's stop (70%, read from the Foreman's usage feed).

## 2. Contamination control

The models may know what happened to ASX stocks before their knowledge cutoff (taken as
2026-07-01). CLAUDE.md: no AI classification of historical announcements. So:
- **Scoring is on post-cutoff days only** (VALIDATE, LOCKED TEST, SHADOW).
- **Before the cutoff (TUNE) the agent sees an ANONYMISED packet** (`lab/anon.py`): the stock
  code becomes a stable alias for that day (`S7F3A`), every price is multiplied by a hidden
  per-stock-per-day factor (percentages, R multiples and volumes unchanged), the date becomes
  "Day N" (the weekday and time stay), announcement headlines become their type and
  price-sensitive flag. The agent's tighter stop is scaled back before use.
- **Measured, not assumed**: `asxbot lab contamination` runs the agent raw and anonymised on
  the same sample of VALIDATE days (after the cutoff: any difference is the anonymisation's
  own effect, A) and of TUNE days (before: difference R = knowledge + anonymisation). The
  estimate of what the model knows is C = R - A, with a bootstrap interval over days, and the
  share of setups where raw and anonymised decisions agree. Raw pre-cutoff answers exist only
  for this measurement and never score a variant. The report states the result plainly.

## 3. Walk-forward, the locked test, overfitting

Windows (WINNER.md): TUNE (Mar-Jun), VALIDATE (Jul-mid Aug), LOCKED TEST (mid Aug-25 Sep,
sealed in code: `lab/splits.py` refuses a locked-test run unless the variant is a final
candidate, and records every run in `locked_runs.jsonl`), SHADOW (forward days).
- Every variant is registered before it runs (`data/lab/variants/<id>.json`: parent, changed
  parameters, prompt text, who proposed it and why, when) and never edited; results are kept
  per window. The count of variants tried (N) raises the validation bar (sqrt(2 ln N)).
- Complexity is counted (changed parameters + prompt characters / 100) and ranks simpler
  variants first; above 6 parameters or 600 prompt characters a variant is refused.
- What a variant may change is a whitelist (`lab/variants.py`): setup thresholds and windows,
  entry slack and good-for time, the uneconomic filter's multiple, trade management (breakeven,
  half-off, trail), scan window and signal age, the last entry time, universe filters, risk
  per trade BELOW the playbook's cap, and the agent's added instructions. Never the cost or
  fill model, the level's limits, the flat-by-close rule, shorts outside the ASX 200, or
  anything that touches a frozen live playbook.
- Every result, winners and losers, is in the lab report (STRATEGIES.md).

## 4. The research loop

- After each simulated batch and each live trading day, `lab/research.py` writes a research
  packet (`%LOCALAPPDATA%\asx-bot\lab\research\<stamp>\packet.md` + data): the scoreboard, the
  variants tried and their results, the day's or batch's losing trades with their bars, the
  agent's rejected setups and what happened next, and the rules above.
- The Foreman queues ONE research session (Claude Code, Opus 5.5 xhigh, in Rick's build queue,
  working in that folder, no repo edits) that diagnoses the losers and proposes up to 5 variants
  as `proposals.json` (schema in the packet). `asxbot lab ingest` validates them against the
  whitelist and registers them; the batch runner screens them on TUNE, validates survivors,
  and promotes validated ones to SHADOW.
- **Shadow** (`lab/shadow.py`, phase 1): each evening after the day's IBKR bars are in (the 17:30
  history fetch), every promoted variant trades that day - a day nobody has seen - through the
  same engine at the live decision times; its book and scores are kept per variant
  (`data/lab/shadow/`). This is forward paper trading with no effect on the watcher or the
  frozen 10-day test. Phase 2 (after the frozen test ends, 8 Oct): the same variants as dated
  shadow playbooks inside the watcher itself (needs per-playbook day-trader state).
- **Final and winner**: after 10 shadow days in profit, one locked-test run; if WINNER.md holds,
  ONE Trader message to Rick with the evidence and "trial it with real money?".

## 5. When it runs

The Foreman starts `asxbot lab tick` (hidden, from the deployed release) whenever the market is
closed - weekday evenings from 19:35 to 06:45 and all weekend - and never two at once. Each tick
does the next unit of work: the evening shadow day, pending screens/validations, the
contamination sample, a final locked run. It never runs 06:45-19:35 on a trading day (the
watcher's time), needs no IBKR connection (the history cache only) and stops at Rick's weekly
usage stop for agent calls.

## 6. Reports

- `asxbot lab report` -> `reports/lab_<stamp>.md` + `data/lab/scoreboard.json`: every variant,
  its screen/validate/shadow/locked results, N tried, the contamination measurement.
- The evening report gains a Practice Lab scoreboard section (top variants in shadow against the
  frozen bots, variants tried, the leader's distance from WINNER.md).
- A weekly summary on Sunday evening (Trader chat, via the Foreman).
