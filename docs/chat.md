# The Trader chat

*Added 24 Sep 2026. Code: `src/asxbot/chat.py`. Launcher: `scripts/chat.pyw`. Task:
"ASXBot Chat" (`scripts/register_chat_task.ps1`).*

Rick's Telegram bot for the Trader, **@rick_asx_trader_bot**, is answered by this repo's own
program, `asxbot chat`. Until 24 Sep 2026 OpenClaw's gateway polled the bot as one of its
channels. It now runs on its own so that commands are handled in plain code, like Rick's
other bots. The same bot still sends the evening report, the alerts and the watchdog's
messages. Sending is not affected, because only reading messages (getUpdates) is exclusive.

## What it does

- It answers **only Rick**, in his chat (the paired chat id, `data/arena/telegram_chat.json`).
  Messages and button taps from anyone else are ignored and logged.
- **Plain messages go to the decider** (`trader-decider`) as a conversation, in its own
  session (`agent:trader-decider:rick-chat-<n>`). Rick can ask what it traded and why, how a
  playbook works or how the day went. The decider is told that this is Rick in the chat and
  that it cannot place, change or approve any order from here. The code has no order path
  at all. The arena is fake money.
- A "typing..." indicator shows while the decider is working. Only one answer is worked on
  at a time. `/queue` sets what happens to a message that arrives in the meantime.
- A message sent while the chat was not running, and more than 30 minutes old when it
  starts, is not answered. The chat says so once.

## Plain words (25 Sep 2026)

Rick, 25 Sep: "i need to be able to just tell it things without commands". Every command
below also works from an ordinary sentence, worked out in code (`src/asxbot/plain.py`, no
model reads the words; `tests/test_plain.py` holds some 170 phrasings to their meaning).
Replies never tell Rick to type a command; the commands stay as shortcuts.

| Rick says, for example | What happens |
|---|---|
| "how's it going today", "how did it go", "what happened yesterday", "give me an update" | The day from the records (`src/asxbot/arena/today.py`): the watcher, announcements v2's counts and reaction looks, the rule bot, the day trader's setups, orders, fills, both accounts, open positions. On a day with no records (a weekend) it answers for the last session on record and says so |
| "what did it trade", "any trades today", "show me the fills" | Every fill (timed by its bar), the orders that did not fill, what the limits refused, with the recorded reasons |
| "how much are we up", "what's the P&L" | Each playbook's agent and bot: equity, today's move, the total since the start |
| "why did it pass on NWL", "what happened with hls", "tell me about REG" | That stock's day in time order from the records: news, screen, reader, decider (with its written reason), reaction look, 10:30 rule bot, day-trader setups (what the bot and the agent did), orders, fills, stop moves, the pre-close sweep. Nothing on record says so, and whether the day trader scanned it |
| "show me the positions", "what are we holding" | `/positions` |
| "is it running", "status", "are you there", "watcher up?" | `/status`, with a `Data:` line (the watcher's feed status) and Rick's pause if set |
| "stop", "cancel that", "stop it" | `/stop`: drops the answer being worked on |
| "no new entries today", "stop it for today", "stop trading today", "halt entries", "kill switch", "shut it down", "don't trade today", "don't open any more positions today" | Rick's pause (below): read back with **[Pause new entries] [Cancel]** |
| "resume trading", "start trading again", "allow entries again" | Lifts the pause, after a read-back **[Allow entries again] [Cancel]** |
| "use opus for the decider", "switch the reader to sonnet 5", "decider back to normal" | `/model`, the same strategy change, but READ BACK first (below). No agent named: "For the reader or the decider?", and the next message answers it |
| "make the decider think harder", "reader effort low", "turn the reader's thinking down" | `/think`, read back first. "harder"/"less" step one level along minimal, low, medium, high, xhigh, max from the current level |
| "what model is it on", "how hard is it thinking", and any model sentence with a "?", a negation ("don't switch the decider to sonnet", "the decider stays on opus") or a hedge ("maybe", "should", "tomorrow", "later") | The models and levels, whether a test freezes them, and how to switch them in words. Never a change |
| "is the gateway up", "is ibkr connected", "gateway should be back", "is the data live", "is it on ibkr or yahoo", "are we on live prices" | IB Gateway and the prices from the records, each with its age: the watcher's feed status (`data/arena/live_data.json`: IBKR or Yahoo, connected, real-time, entries allowed or paused and why), the connection doctor's diagnosis, open problem and last episode (`doctor.json`), and the Gateway supervisor's last check - hand-started or the launcher's, whether Rick's login is stored (`supervisor.json`). Nothing is probed from the chat |
| "did the evening report go out", "i didn't get the evening report" | From `data/arena/report_sent.json`, and whether tonight's is due, late or not on record |
| "is the market open", "is today a trading day" | Today's hours, and whether the market is open right now |
| "restart the watcher" | Why it isn't: the market-hours lock (7:25am-7:25pm on a trading day, and while any account holds a position or has an order working), and the watcher's state now |
| "how did we go this week", "last week" | Day by day: each day of the week that has records, one line each, and each account's move over the week from its closing marks |
| "how much did we make", "did we lose money", "is the agent beating the bot", "bot vs agent" | The money lines (each playbook's agent against its rule bot) |
| "start over", "new conversation", "reset the chat" | `/new`, `/reset` |
| "can you make it ...", "from now on ..." | A change request, as before (read back, Build it button) |
| "what changes have I asked for", "undo the last change" | `/changes`, `/undo` |
| "answer them one at a time", "bundle my messages", "what happens if I message you while you're busy" | `/queue` |
| "when does it start", "what are the trading hours" | Today's window from `arena/hours.py` |
| "close the NWL position", "sell everything", "could you sell BHP" | Refused in code: nothing in the chat can place, change or close an order. Only an order verb with something to trade counts: "short answer please", "cover the basics", "add to the list", "exit" and "would you buy PLS" (an opinion) are conversation |
| "help", "what can you do" | `/help` |

Order of precedence in `on_text`: slash commands (a `/model` or `/think` that would change a
setting mid-test is read back first); a reply to a Foreman message (below); a plain yes or
no to a read-back just sent; the answer to a change reader's open question - unless the
message is a clear request (stop, status, today, P&L, feed, pause, resume), which is never
swallowed as that answer; the Foreman's topics (below); plain-word intents; the
change-request check; the decider. A sentence that reads like a change request ("can you
make it show positions first") stays a change request unless it is a setting, the pause or
a watcher restart ("can you make the decider think harder", "can you make it stop trading
today"). Queue modes change only on an explicit instruction ("answer them one at a time",
"bundle my messages", "interrupt mode"): "sorry to interrupt" and "take it one at a time"
are conversation.

Quick spellings are read as meant (26 Sep 2026): "whats", "hows", "wheres", "didnt",
"dont", "isnt", "cant", "wont", "wat", "goin"; and "fuck it", "ffs", "jesus", "ugh", "oh",
"wtf", "c'mon" at the start are dropped.

A stock code is recognised from the ASX directory and ASX 200 list in `data/universe`. In
capitals it counts (not when the whole message is in capitals); in lower case only when it
is not an ordinary word AND it follows on/with/about/in/for/of (or another code: "hls and
reg"), or the day's records name it. So "all" is a word and "ALL" is Aristocrat, and "what
happened today pls" is about today, not Pilbara (PLS). Day names: full names anywhere, the
short forms (mon, wed, fri) only after on/last/this/since/from/for - "c'mon" is not Monday.

Anything else still goes to the decider as a conversation. If it names a stock or talks
about the day's trading (money, the week, the feed and the report included), the message
carries a FACTS ON RECORD block - the same summary and stock stories the chat would have
sent, stamped with the time it was gathered - with the rule to answer from it, to say "not
on record" where it is silent, and that numbers from earlier turns may be stale. On a
trading day with nothing recorded yet, every day answer (and the block) leads with today's
watcher state ("Nothing recorded today (Mon 28 Sep) - Watcher: ...") before the last
session on record.

What the records say, 26 Sep 2026:
- A stock's story says which prices each decision was made on: "[prices: delayed data
  (Yahoo) - IBKR unavailable]" on 25 Sep's 08:21 NWL pass, from the decision's own record
  or `data/arena/price_sources/<day>.json`.
- Open positions are marked from the minute bars already on disk, with the bar's time
  ("last minute bar on disk, 15:59, not a live quote"), or at cost. The chat never fetches
  a price: its arena's minute bars read the cache only.
- A past day never lists the positions held now; it says how many its closing marks held.

## Rick's pause: no new entries today (26 Sep 2026)

"Stop it for today" used to have nothing to stop. Now "no new entries today" (and the
phrasings in the table) reads back: "Stop NEW entries for the rest of today, for both
playbooks and both books? Exits, stops and the 15:50 close keep working." with **[Pause new
entries] [Cancel]** (a typed "yes" or "no" within ten minutes answers it too). The tap writes
`data/arena/pause/<Sydney date>.json` through `asxbot.arena.pause.set_pause` (by "Rick",
with his words) and an `arena_pause` event; the chat then checks the file is there before
it says "Done". The watcher reads it before every new entry, for every playbook and both
books; exits are never paused. The file is dated, so it clears itself the next day;
"resume trading" lifts it sooner after its own read-back. Status and the day's summary say
"New entries paused since 11:05 (Rick)". On a non-trading day or after the close the chat
says nothing more enters today anyway, and sets nothing. "Stop everything" also stops the
answer being worked on; a bare "stop" is still only that.

## Replies to the Foreman (26 Sep 2026)

A reply (Telegram's reply) to a message from the Foreman ("Foreman: ...") or to a change
job's report ("Change #0926-1015 is live: ...") is handed to the Foreman with the text it
answers, as `reply_to` in the handover file: a bare "yes", "ok do it" or "go ahead" means
nothing to the decider on its own. A reply about trading ("sell BHP") or a stock is still
the Trader's.

## One voice: the Foreman (26 Sep 2026)

Rick, 25 Sep 23:50: "everytime i ask it something it says it can't do shit". That night he
told this chat "No keep building I have a full reset", "Yes tell me at 99", "Prioritise the
simulator ahead of fetch", and the decider answered "I can't set that up from here" while
the Foreman read the same messages and acted on them.

The Foreman (`G:\My Drive\foreman`, state in `%USERPROFILE%\.foreman`) is Rick's
orchestrator: it runs the builds on all his bots, one at a time, watches Claude usage and
limits, keeps his priorities, and answers in this chat itself as "Foreman: ...". It reads
every "from Rick:" line of chat.log, so that line now carries the whole message on one line.
Code: `src/asxbot/foreman.py`; tests: `tests/test_foreman_handover.py` (Rick's exact
messages of that night, replayed).

- **Foreman topics are handed over in code and the Trader says nothing.** Builds and the
  build queue, what's being worked on, Claude usage / limits / resets, priorities of
  improvement work, the Foreman - and, within ten minutes of one, a short follow-up that is
  an instruction for it ("yes tell me at 99", "stop at 80%", "carry on", "status"). A
  message that names a stock or is about trading, orders, the watcher, the decider or this
  chat is never a follow-up: "stop trading for today", "sell BHP", "what's my risk per
  trade" and "how did the day trader go" are answered as before.
- **The decider hands over what it can't do.** Its prompt says who the Foreman is and never
  to say "I can't" for something the system could do: it starts its reply with
  `HANDOVER: <what he wants>` and adds at most one short line ("The Foreman's on it - it'll
  answer here."). A reply that says "I can't ... from here" anyway is handed over the same
  way, with those sentences taken out. "I can't tell from the records" (knowing, not doing)
  and anything about orders, real money, the broker or passwords (hard rules) are left
  alone, and a hard rule is never handed over. Since 26 Sep 2026 so is a refusal about how
  the arena trades - stops, targets, risk, limits, size, entries, rules, the strategy or a
  playbook, thresholds, the models, the watcher: those are frozen for the test, so "I can't
  change the stop rules mid-test" is honest and never becomes a queued build. `SILENT` from the decider sends nothing (a
  message that only answers or thanks the Foreman).
- **Handing over** is one UTF-8 JSON file in `%USERPROFILE%\.foreman\inbox`
  (`FOREMAN_HOME` overrides it; every test uses a temporary one), written as
  `<yyyymmdd-hhmmss>-trader-<4 hex>.tmp` and renamed to `.json`:
  `{"bot": "trader", "from": "Rick", "text", "at", "why": "foreman topic" | "can't do: ..." |
  "reply to a Foreman message"}`, plus `"reply_to": <the message he replied to>` when he
  replied to one (below).
  The Foreman answers in this chat within a minute and acts; it skips a handover it has
  already answered from the chat log. chat.log shows `for the Foreman (...)` and `handed to
  the Foreman as <file>`.
- **If the Foreman isn't running** (its `heartbeat.json` more than 5 minutes old), Rick is
  told so in one line, and that his message is waiting in its inbox.
- The decider's next turn is told which messages went to the Foreman since its last answer,
  so its conversation still makes sense.

## Commands

The Trader's own:

| Command | What it does |
|---|---|
| `/start`, `/help` | What this chat is, then every command |
| `/positions` | Open fake-money positions and pending fills, per arena account (the same lines as `asxbot arena positions`) |

The shared ones (botctl, the same names and answers as in Jarvis's chat):

| Command | What it does |
|---|---|
| `/status` | Whether the watcher is running (the watchdog's own verdict from its heartbeat), open positions, today's orders, the agents' models and effort, and anything running |
| `/stop` | Stops the answer being worked on. Its reply is dropped, and messages waiting behind it are dropped too |
| `/queue [steer\|followup\|collect\|interrupt]` | What a message does while an answer is running. Options: `debounce:2s cap:5 drop:old`. `/queue default` resets it |
| `/model [reader\|decider] [name\|number\|default]`, `/models` | Shows the models, or switches one. **This is a strategy change** (see below) |
| `/think [reader\|decider] [level\|default]` | The effort level. **Also a strategy change** |
| `/new`, `/reset` | Starts a fresh conversation with the decider |
| `/compact`, `/steer`, `/tasks`, `/whoami`, `/commands` | As in Jarvis's chat |
| `/change <what>` | Asks for a change to how the Trader works, in Rick's own words |
| `/changes`, `/undo` | Lists recent change requests, or rolls back the last one |

Jarvis's commands that don't fit a bot (`/restart`, `/config`, `/bash`, ...) answer with the
reason. Any other `/command` gets "I don't know that one. Just say what you want in plain
words ...".

## Change requests

When Rick asks for a change, either with `/change ...` or in plain words ("can you make it
..."), the bot reads it back as one line with the buttons **[Build it] [Cancel]**. Nothing
happens until Rick taps Build it. The request then goes to the change dispatcher in
`C:\Users\Richa\.cc-jobs\changes`, which builds, tests and deploys it and reports back in
this chat.

- **Refused in code, before anything is offered:** real-money orders or anything about the
  live broker, IBKR doing anything but read-only data, credentials or tokens, anything to
  do with Jarvis, other bots' code, and shared OpenClaw settings.
- **A safety limit needs a second tap** showing the old and new values. That covers the
  Level 1 risk per trade, open positions, position size, new positions a day, leverage and
  daily loss limit, the arena guards (orders a day, largest and smallest order), the day
  trader's risk per trade, and the real-money `limits:` block.
- A change is built into the code and **never restarts the watcher**. The watcher picks the
  change up at its next 7:30 start. The chat itself is restarted by the go-live step of the
  change.

## /model and /think are strategy changes

A different model or effort level changes every decision the arena makes. So, by Rick's
rule, `/model` and `/think` in this chat do three things:

1. They change the OpenClaw agent (`trader-reader` or `trader-decider`) through
   `openclaw config set`, and validate the result. If anything else in openclaw.json
   changed, the file is put back.
2. They write the new expectation into `config.yaml`: `arena.agents.models.<role>` or
   `arena.agents.effort.<role>`. The self-check and every agent call hold the agents to
   that, re-reading it when it changes, so a running watcher does not raise a false
   mismatch alarm.
3. They add a dated entry to `arena.agents.history`, for example
   `{date: "2026-09-24", time: "22:15", role: decider, setting: model, old: ..., new: ..., by: "Rick, /model in the Trader chat", note: "deliberate change by Rick, not a finding"}`.
   The file is edited as text, so its comments survive. It is then checked to load and to
   differ only in those lines, and committed on its own.

The next evening report says, for example, "Settings changed: decider model Opus 5.5 ->
Sonnet 5 (Rick, 10:15pm)", and the decider is told it must mention the change. The reply
says which report: tonight's (with its time, 7:30pm or 8:30pm in daylight saving) before
tonight's slot on a trading day, otherwise the next one.

It is refused, with the reason, while `config.yaml` has uncommitted edits, or while a
change request is being built in this repo. If recording the change fails, the OpenClaw
setting is put back.

**Read back, and frozen during a test (26 Sep 2026).** A change in plain words is never
applied at once: it is read back ("Switch the decider's model from Opus 5.5 to Sonnet 5?")
with **[Change it] [Cancel]**. While any enabled playbook in `config.yaml` has `status:
test`, the models and thinking levels are frozen for it (`settings_history.frozen_test`:
the test ends on the last of `test_days` ASX sessions from `test_start`; for v2 and the
day trader, Thu 8 Oct). Then every change - `/model` and `/think` included - is read back
as breaking the test ("... would break the 10-day test of ASX announcements v2 and ASX day
trader (Fri 25 Sep to Thu 08 Oct) ... Change it anyway?") with **[Change it anyway]
[Cancel]**; a typed "yes" is not enough, only the button or the words "change it anyway".
Any other path to a change mid-test (for example `/new <model>`) is refused in code. The
history entry says how he asked and that he confirmed: `by: "Rick, plain words in the Trader
chat, confirmed on a read-back; breaks the frozen 10-day test"`. Outside a test `/model`
still changes at once (`by: "Rick, /model in the Trader chat"`).

## Guardrails

- **Refuses to start** (exit 10, logged "REFUSING TO START") while
  `~/.openclaw/openclaw.json` still has `channels.telegram.accounts.trader`, or any account
  that uses this bot's token. **Never add this bot back to OpenClaw as a channel.**
- **One instance per PC:** an OS lock on `%LOCALAPPDATA%\asx-bot\chat\chat.lock`. A second
  instance waits 30 seconds, then exits 11.
- **Telegram 409 Conflict** means another program is polling the bot. The chat exits 12 with
  a clear log line.
- If Telegram can't be reached, the chat waits and tries again: 5 seconds at first, doubling
  up to 5 minutes. It keeps running.
- The token is never logged, only its last four characters. It is removed from any error text.
- Every process goes through `asxbot.proc` (windowless), including botctl's. The chat's
  state (conversation number, Telegram offset, queue settings, change-request settings) is
  kept in `%LOCALAPPDATA%\asx-bot\chat`, off Google Drive.

Exit codes: 0 stopped; 2 config error; 3 Telegram token or chat id problem; 10 OpenClaw
still polls this bot; 11 another chat is running; 12 Telegram 409 Conflict.

## Running it

`scripts/chat.pyw` runs `asxbot chat` hidden and writes its output to
`%LOCALAPPDATA%\asx-bot\logs\chat.log`, rotated daily. After a crash it restarts the chat 60
seconds later, at most 5 times an hour. It does not restart on exit 2, 3 or 10, because a
restart can't fix those. The chat exits when the launcher is gone, so ending the task never
leaves an old copy polling.

A good start looks like this in chat.log:

    === chat launcher starting 2026-09-24 22:30 (pid 1234) ===
    ... INFO asxbot.chat: Trader chat starting on commit 1a2b3c4 (master) (botctl 2026-09-24.1, pid 5678)
    ... INFO asxbot.chat: Running. Polling @rick_asx_trader_bot (token ...abcd) for Rick's chat 8998104023; state in ...

To try a message without Telegram, run `asxbot chat --probe "how was today?" --no-agent`.
Replies are printed, not sent, and Telegram is never polled. A handover to the Foreman is
printed too, never written to its inbox. Leave out `--no-agent` to call the real decider. `--button "chg:b:<id>"` simulates a button tap. A probe of `/model` really
changes the setting.

## Going live

1. Remove the bot from OpenClaw: in `~/.openclaw/openclaw.json`, delete
   `channels.telegram.accounts.trader` and its binding (`agentId: trader-decider`,
   `match.accountId: trader`), then restart the gateway so it stops polling. Ask Rick first,
   because a gateway restart interrupts Jarvis.
2. Register the task once: `powershell -NoProfile -ExecutionPolicy Bypass -File
   "G:\My Drive\asx-bot\scripts\register_chat_task.ps1"`. It prints the task back and the
   undo command.
3. Start it with `schtasks /run /tn "ASXBot Chat"`. Later restarts (for example after a code
   change) are `schtasks /end /tn "ASXBot Chat"` then `schtasks /run /tn "ASXBot Chat"`.
4. Check chat.log for the commit line and "Running.", then send /help from Rick's Telegram.

The watcher is never restarted for any of this. It runs itself from 7:30am into the
evening on trading days and picks up code changes at its next 7:30 start.
