# The trading journal (26 Sep 2026)

Rick, 26 Sep: "do the traders know to write down what works etc". Until then the live agents
logged each decision but never wrote down what worked, what didn't, or why. Now, after the
close each trading day, every book of the frozen 10-day test gets a journal entry. How it
trades is unchanged.

Code: `src/asxbot/arena/journal.py`. Command: `asxbot arena journal [--day YYYY-MM-DD]
[--no-agent] [--again] [--send]`. Tests: `tests/test_journal.py`.

## What gets written, and by whom

| Book | Written by | What |
|---|---|---|
| `asx_daytrader_v1__agent` | the day trader's agent, in ONE call with its own model | every setup it took or skipped and why, how each trade played out, what worked, what didn't, its mistakes, what it would do differently, and one `LESSON:` line |
| `asx_announcements_v2__agent` | announcements v2's agent, in ONE call with its own model | the same, for its pre-open and reaction looks |
| `asx_daytrader_v1__bot`, `asx_announcements_v2__bot` | plain code, no model | the same facts: trades, outcome, R, fees; the setups (or the 10:30 rule's candidates) it took or skipped and why |

Every file starts with the day's facts from plain code: each trade (entry, exit, the initial
stop, the risk in dollars, gross, fees, net, R = net over the initial risk, every exit and
stop move), each setup or look decided (the decision and the agent's own recorded words), and
what the price did afterwards. "After" is hindsight and labelled so: the plain price path from
the decision to 15:50, before costs and without breakeven, half-off or trailing; a bar that
touched both the stop and +1R counts the stop first. Setups nobody decided (too stale, the
agent unavailable, a stock with no data) are counted as the system's facts, never as the
agent's rejections. An agent book also shows its rule bot's trades as the yardstick.

## FROZEN TEST RULE

The journal is written only. During the 10-day test (25 Sep - 8 Oct 2026) it is never fed back
into the agents' prompts or decisions, so the test stays fair:

- nothing in the watcher, the day trader, v2, the agents' calls, the chat or the agents'
  standing instructions reads it (`test_no_trading_code_reads_the_journal` fails the build if
  any of them names it);
- the evening report's three lines are added by code after the agent has written the report;
  the report's agent brief never contains them (a test holds that);
- the agent's journal call is not made through OpenClaw. The trader agents run there with the
  "coding" tools and a workspace whose files later calls load, so a journal written through
  OpenClaw could land in the decider's memory and so in its next decision. The call runs the
  decider's model directly: `claude -p`, the model and effort in config.yaml
  (`arena.agents.models.decider`, `arena.agents.effort.decider`), its standing instructions
  (the live `AGENTS.md`) as the system prompt, no tools, no session kept, no settings or MCP
  servers, in an empty scratch folder that is deleted after. Nothing persists but the text,
  which code writes to the file.

Every journal file says so in its header.

## One call per agent per day, and its usage

- The call is recorded in `data/events/arena_journal_calls.jsonl` before it is made
  (`stage: started`) and after (`stage: done`: the model that ran, effort, tokens in, cached
  and out, the list-price cost - the calls are on Rick's Claude plan, so this is a size, not a
  bill - seconds, and the plan's weekly meter as the CLI reported it). The entry's text is kept
  in the done record too, so a paid call is never lost to a failed file write.
- A book with any record that day is not asked again: an evening task that runs twice asks
  nobody twice. `--again` asks anyway and is logged `again: true`.
- A book the agent made no decision in gets a code-written line and no call.
- These are kept out of `arena_agent_calls` on purpose: a failed journal call is not a trading
  decision, and must not read as "the agent could not be asked" in the report or the
  self-checks.

## When it runs

The evening task (`scripts/arena_evening.pyw`): `arena resolve`, `arena mark`, **`arena
journal`**, `arena report --agent --send`. The report ends with:

    Journal - the day's lessons (written only: no agent reads it during the test)
    - Announcements v2 agent: <its LESSON line>
    - Day trader agent: <its LESSON line>
    - Rule bots: day trader N trades +x after fees (R, R); announcements v2 N trades +y ...

`asxbot arena journal` refuses a day that is not over (before 16:15 Sydney) and does nothing
on a day the ASX is shut. `--send` sends the three lines to the Trader chat on their own.

## Where the Practice Lab reads them

`data/arena/journal/<yyyy-mm-dd>/<account>.md` under `ASXBOT_HOME` (the settings checkout; the
lab's `data/lab` is beside it). Each file ends with a fenced ` ```json journal-data ` block:
`facts`, `entry`, `lesson`, `written_by`, `call`, `locked_test_day`. `journal.load(data_dir,
since, until)` returns them all, oldest first.

They are real lessons from forward days, after the models' knowledge cutoff. A day inside
WINNER.md's sealed LOCKED TEST window (17 Aug - 25 Sep 2026; 25 Sep is its last day) says so in
its header and has `locked_test_day: true`: never use it to tune or screen a variant.
