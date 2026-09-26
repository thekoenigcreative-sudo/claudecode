# Full review of the ASX bot - 26 Sep 2026

*Rick, 25 Sep ~23:15: "they all need a full review in claude code". This is that review:
every way the bot can trade on bad data, miss trades, crash, mislead Rick, or break the
frozen 10-day test; rigid chat paths; dead code - and what was done about each.*

**How it was done.** Nine reviewers, each on one part of the system (live data, the day
trader, announcements v2, the broker and reports, the watcher and operations, the Trader
chat, the agents and the test's integrity, dead code and docs, and the 25 Sep runtime logs),
read the code against every brief since 22 Sep and proved each finding - with a snippet on
made-up input, the 25 Sep records, or the installed library's source. Six fixers, each owning
different files, then fixed them, each fix with a test that fails on the old code; I checked,
integrated and committed them and ran the whole suite on each committed tree (not the working
copy). The rules of the two playbooks in the frozen test (config.yaml `asx_announcements_v2`,
`asx_daytrader`) and the AI models were NOT changed: a fix that would change what a rule does
is listed under "Needs Rick's OK" instead. IDs: A live data, B day trader, C v2, D broker and
reports, E watcher and operations, F chat, G agents and the test, H dead code and docs,
I runtime.

## The ten that mattered most

| # | What could go wrong | Found | Status |
|---|---|---|---|
| 1 | **The IBKR bars had holes all day.** Today's bars kept history only before the first streamed minute; every later answer (the catch-up after a reconnect, the polls of a stock rotated out of streaming) was thrown away. Stops inside a hole were never seen; the scanner read a hole as a halt and fired a halt-resumption trade on a stock that never stopped (reproduced) | A1, B1, D1 | **Fixed** (8628940, 9fc46ec): merged minute by minute; a gap counts as a halt only if the feed was watching |
| 2 | **Monday's streaming would hit IBKR's pacing.** ~94 real-time bar subscriptions at once and all of them again on every reconnect, against IBKR's ~60 new requests per 10 minutes; a refused stream still counted as "streaming" and was never polled | A2 | **Fixed** (8628940): at most 50 per 10 minutes, index and positions first, the rest polled; refused and dead streams go to polling |
| 3 | **The flat-by-close rule could fail.** A part-filled half-off (or a target reached after the 15:50 sweep started) switched the sweep off; a leftover under ~$515 was refused every cycle; positions could stay overnight | B2, D2, D3 | **Fixed** (ac28761): the sweep cancels resting targets and sells the whole remainder; exits are never blocked by the order-size guards |
| 4 | **The agents run on Rick's Claude plan, and every call re-sent ~600k tokens.** Every watcher call resumed one conversation per agent since 22 Sep (~593k tokens per day-trader call, ~879k for the reader, near its context limit). A usage-limit failure was recorded as the agent "rejecting" | G1, G2, G8 | **Fixed** (ac28761): a fresh session per call (config `arena.agents.fresh_session_per_call`, dated 26 Sep - infrastructure, not a rule; verified with a real call: its own session, Opus 5.5, 14 s); a failed call is "agent unavailable", counted and said plainly in the report, the 16:10 summary and a self-check line, never "rejected" |
| 5 | **A remark in the chat could change a frozen model.** "dont switch the decider to sonnet" switched it; "sonnet for the reader?" switched it; "less thinking more trading" raised the effort | F1, G3 | **Fixed** (185f176): only an instruction, always read back with buttons; during the test every change is read back as breaking it and needs [Change it anyway] |
| 6 | **The agent was told its prices were 20 minutes late.** Every day-trader packet on day 1 said "delayed data ... ~20 minutes behind"; the bars were a median 1.6 minutes old, and the decider reasoned from it ("this entry is late") | G5, I1, H4 | **Fixed** (9fc46ec, ac28761, 67a4a64): every packet's data line is built from the feed's label and the bar's real age; the agents' standing instructions corrected in the live workspaces (dated, facts only; backups in data/agent_backups) |
| 7 | **From 5 Oct (day 7 of the test) the 07:30 start would have been 08:30.** Every scheduled trigger was written with a +10:00 offset, which Task Scheduler fires at a fixed UTC time - daylight saving moves it an hour | E1 | **Fixed** 26 Sep on the live tasks (backups data/task_backups/dst_fix_20260926); the task scripts now write local times |
| 8 | **v2 recorded data failures as verdicts.** One stock with no previous close became "no signal" and closed the rule bot's day; a stock the feed was not watching was "halted"; a missing usual volume was "quiet" | C1, C2, C3, I11 | **Fixed** (9fc46ec): missing data waits; "missed: no data" when the window closes |
| 9 | **No "no new entries today" switch.** "stop it for today" only stopped the chat's own answer | F4 | **Built** (8628940, ac28761, 185f176): "stop it for today" -> a read-back -> new entries stop for the day, every playbook and both books, enforced at the feed and again in `arena_place_order`; exits never paused; "resume trading" lifts it |
| 10 | **Nothing noticed a stalled cycle.** On 25 Sep one cycle ran 24 minutes (10:52-11:17) while logging busily: no fills, no polls, no alert. A slow asx.com.au could stall the watcher ~30 minutes per PDF | I4, E5, C5 | **Fixed** (ac28761): the heartbeat records cycles and the watchdog alerts on one over 6 minutes; the watcher's HTTP is 15 s with one retry, failed PDFs retried on later cycles; fills, stops, the 10:30 bot and the sweep run between the slow stages |

## Trading on bad data

| ID | Finding | Status |
|---|---|---|
| A1/D1 | History kept only before the first streamed minute: holes all day (above) | Fixed 8628940 |
| A3 | ib_async reuses one Ticker per contract: a second quote of a stock returned the first one's bid/ask/last/type at once - v2 screens, the pre-open packet and the 10-minute real-time probe were wrong after the first quote | Fixed 8628940 (the cached ticker is forgotten before and after) |
| A5 | The v2 pre-open look could decide on a Yahoo quote (`FailoverQuotes` falls back silently) | Fixed ac28761 (needs IBKR's own real-time quote) |
| A6/B8 | No per-stock freshness check before an entry: a polled stock's bars could be 6+ minutes old and count as fresh | Fixed: a polled stock's bars must be < 3 min old for an entry (8628940), checked before the bot, before the agent and before placing (9fc46ec) |
| A9 | The "today" window was fixed when a request was queued; a request that waited left a morning hole | Fixed 8628940 |
| A11/D7 | On IBKR days the opening-auction volume estimate ("Yahoo's daily volume less the minutes") came out <= 0 on 289 of 322 stock-days, so the auction was refused and pre-open orders filled at the 10:00 minute | Fixed 8628940: the volume is IBKR's 09:59 auction bar; the price stays Yahoo's daily open as Rick approved (#28, #34) |
| B4 | Prices formatted to 4 significant figures in the packet: "108.8 vs 108.8" for 108.85 | Fixed 9fc46ec |
| B7 | The day trader's limit and 1R came from the trigger bar's close, not the last visible price (the rule) | Fixed 9fc46ec |
| C6 | The v2 bot's limit was always 1.02 x the 10:29 close even when late | Fixed 9fc46ec (last visible price at order time; the lag recorded) |
| C11 | v2's 10:30 measure could not be reproduced from any file (HLS's IBKR history was held in memory only; on Yahoo it was 1.85x, recorded 4.5x) | Fixed 9fc46ec: each candidate records its inputs |
| C14 | For news during the session the index base is taken at the stock's first trade after the news, not at the news | Open, low |
| D13 | A position that did not trade that day was marked at cost | Fixed (last traded close, labelled) |
| B6 | A later part-fill of an entry reset a stop management had already moved to breakeven | Fixed |
| G7 | The v2 decision could name a different stock than the one asked about; "BUY" rounded the wrong way; an unreadable target crashed the order | Fixed 9fc46ec |
| D12 | The bid-ask spread is not modelled: every fill pays 0.10% slippage, while v2 allows a tick of up to 3% of the price | **Needs Rick's OK** |

## Missing trades

| ID | Finding | Status |
|---|---|---|
| A2 | Real-time bar pacing (above) | Fixed 8628940 |
| A4 | Tier 1 of the rotation sorted by name and unbounded: 120 news codes left a held stock unstreamed | Fixed 8628940 (priority order, capped), ac28761 (the watcher's ordered list) |
| A7 | Only the day trader's scan subscribed or polled: a v2 position opened before 10:00 or after 15:45 was never streamed | Fixed 8628940, ac28761 (the rotation runs every cycle 09:50-16:12) |
| A8 | An index stream that never delivered paused every entry all day | Fixed 8628940 (the index polled every minute when quiet) + the doctor's stale-stream ladder |
| A10 | Empty prior-session answers (timeouts) gave a stock up for the day; a farm hiccup marked a code unknown for an hour | Fixed 8628940 |
| A12 | The 09:55 history report was skipped if no cycle fell in 09:55-10:00 | Fixed 9fc46ec |
| A16 | 16:00-16:10 (closing auction) was called "stale" | Fixed 8628940 |
| B3/G4 | A setup exactly at the 5-bar age limit went to the bot and never to the agent (NWL and DOW on day 1 - the bot's best trade) | Fixed 9fc46ec (one test for both books) |
| B5 | The agent's packet missed last evening's news | Fixed 9fc46ec |
| B9 | A 60-75 s answer was accepted (the rule: no answer within 60 s is a rejection) | Fixed 9fc46ec |
| C1, C2, C3 | v2 data failures recorded as verdicts (above) | Fixed 9fc46ec |
| C4 | "Pause in Trading" notices pass the halt screen, use the stock's one look, and the real news joins a dead queue item | **Needs Rick's OK** (a screen interpretation) |
| C7 | The v2 bot's order is refused, not sized down, when (1.02 x last - the 10:00-10:29 low) exceeds 20% - the biggest movers | **Needs Rick's OK** |
| C8 | Announcements screened out on arrival were re-screened every cycle, verdicts flipping (35 at 10:52 on 25 Sep) | Fixed 9fc46ec |
| C9 | A previous close across a long halt is unavailable (a reinstatement cannot be measured) | **Needs Rick's OK** |
| C10, C13, C15 | "wait" written as "missed"; a look stuck "looking" after a crash; model calls after the 15:40 cut-off | Fixed 9fc46ec |
| D8 | An expired day-trader entry held a slot for 22 more minutes (Yahoo's old delay) | Fixed ac28761: `arena.fill.live_expiry_minutes: 1` (dated) |
| B13 | An opening-range break with fewer than 5 earlier bars returns nothing and kills that side for the day | **Needs Rick's OK** (keep or drop) |

## Crashing or stalling

| ID | Finding | Status |
|---|---|---|
| A14 | `status()` iterated dicts the connection thread was changing | Fixed 8628940 |
| A15 | 502/504 left the feed not-ready forever; a dead connection thread never restarted | Fixed 8628940 |
| B12 | An exception after orders were placed lost the day trader's state; the next cycle asked the agent again | Fixed 9fc46ec |
| E6 | An agent timeout was not a hard stop (only cmd.exe was killed; node held the pipes) | Fixed ac28761 (the process tree is killed) |
| E2 | A Google Drive write failure in the main loop (the digest, the handled list, `Notifier.send`) ended the watcher for the day | Fixed ac28761 |
| E3 | The lock refused to START a dead watcher while a position was open - no stops, no 15:50 sweep | Fixed ac28761: a watcher that is down may always be started (CLAUDE.md) |
| E4 | Tasks could not launch with Google Drive not mounted (their working folder was on G:); the warm-up aborted at once | Fixed: tasks start in %LOCALAPPDATA%\asx-bot; the warm-up waits for Drive until 09:45. **Rick**: Windows has a reboot pending (updates paused to 27 Oct) - better done this weekend under his control |
| E5/C5 | asx.com.au retries could stall the watcher ~30 minutes per PDF | Fixed ac28761 |
| E7 | Deploys pruned releases a running task was using | Fixed ac28761 |
| E8/I5 | Every process shared asxbot.log; other processes' errors alerted as the watcher's (the 18:57 chaos test reached Rick) | Fixed ac28761 (arena_watch.log) |
| E9 | The PDF self-check read 250 MB (growing to ~1 GB) every cycle | Fixed ac28761 |
| E10 | Nothing stopped a second watcher (double trading) | Fixed ac28761 (a lock file) |
| E11, E12, E14, C12 | The lock ended at 19:25 in daylight saving; test lines in the real failures log; a late evening run skipped; announcements 19:25-19:30 never seen | Fixed ac28761 (the watcher stops at 19:31 after one last poll) |
| I4 | A stalled cycle is invisible to the watchdog | Fixed ac28761 |

## Misleading Rick

| ID | Finding | Status |
|---|---|---|
| D4 | Trade counts, win rates and the best-trade share counted closing legs, not round trips: the DT bot's 25 Sep "4 trades, 100% wins" was 3 round trips and 1 net winner | Fixed (round trips after both fees and borrow) |
| D5 | The evening report dated events by UTC day: 470 of 837 announcements counted; from 5 Oct 10:00-11:00 would vanish | Fixed |
| D10, D11 | The 16:10 summary counted flat-sweep exits as trades; the report had no count for stale setups | Fixed |
| D15 | Fills did not say which feed priced them | Fixed (each slice records its feed) |
| H5 | "delayed data - rehearsal until IBKR live prices" printed on any day with no decisions | Fixed ("no entry decisions today") |
| I5, I6, I8, I12 | Other processes' errors alerted as the watcher's; a stale collector alert repeated every evening; "log in again ... on Yahoo prices" for any outage; the same error sent twice | Fixed ac28761 |
| H7, H8, H14-H17 | README/HANDOVER/ARENA/PLAN/SPEC said Yahoo prices, OpenClaw on the Trader bot, 8 positions and 3x leverage, a 15-minute agent review, a standalone poller | Fixed (docs corrected) |

## Breaking the frozen 10-day test

| ID | Finding | Status |
|---|---|---|
| E1 | Daylight saving would move every task an hour from day 7 | Fixed 26 Sep on the live tasks |
| G1, G2 | The agent's side going dark at a usage limit, recorded as rejections; one ever-growing conversation as an input | Fixed (above) |
| F1, G3 | A chat remark changing a frozen model | Fixed 185f176 |
| G6 | Both trader agents are full Claude Code sessions with a shell and no permission prompts, reading third-party PDFs (prompt injection could run `asxbot arena place-order`) | **Needs Rick's OK** - an OpenClaw config change; nothing was changed there (openclaw.json is shared with Jarvis) |
| G9, I10 | Day 1 is not like for like: v2's bot result is entirely the 10:53 re-run on delayed bars; the agent's NWL/HLS looks were lost to the data failure | **Needs Rick's OK**: show totals with and without partial days; decide whether partial days extend the test |
| G10 | Decisions did not record the code that made them | Fixed (every order records its release) |
| D6 | A day the watcher was down is counted as a flat test day | Recorded (7908470): the heartbeat keeps a day record and the evening report names gaps and stalled cycles in market hours as a CANDIDATE partial day; nothing is excluded - **how such days count is Rick's call** |
| B11 | The replay differs from live in small ways (bars one minute later; the flat sweep's price) | Fixed 5a21ce3: the clock steps to 20 s past each minute as live sees bars |
| H1, H2, H3 | `arena preclose` could run v1's hold-or-close on the live v2 book; `fake-announcement` wrote into the live test; `arena reset` could delete the test books; `place-order --by bot` | Fixed ac28761 (refused during the test; logged) |
| #49 | The 25 Sep "uneconomic" filter also changes the rule bot's record from day 2 | Still **needs Rick's OK** (TRACKER #49) |

## The IBKR connection safeguard (Rick's second ask)

Built as `src/asxbot/ibkr/doctor.py` (docs/ibkr_doctor.md, commit 8628940): an agent that
runs inside the watcher every cycle - it observes the connection, Gateway and the network,
names one of eleven causes from evidence, takes that cause's recovery ladder (re-request,
reconnect, a spare client id, the supervisor, a Gateway restart only after a second witness
and only with Rick's login stored, tell Rick), verifies each rung by a deadline, pauses new
entries while the prices cannot be trusted, and tells Rick at most once an episode and once
when fixed. Plain code, so it works when the Claude plan is at its limit. The supervisor no
longer restarts Gateway while the PC is offline and now says when Gateway is back. Proven by
46 tests (every cause, every ladder, chaos end to end against the fake IB) and for real
against the live Gateway (reports/ibkr_doctor_live_20260926_0257.json: socket drop, reconnect,
client-id clash - all passed).

## Rigid chat paths (F)

| ID | Finding | Status |
|---|---|---|
| F1 | Model/effort changes from negations, questions, musings; any <= 6-word message an instruction | Fixed 185f176 |
| F2 | Honest refusals ("I can't change the stop rules mid-test") handed to the Foreman and queued as builds | Fixed 185f176 on the chat's side; **the Foreman's own parser (F3) is the Foreman's repo** - reported, not changed |
| F4 | No real "no new entries today"; Rick's ways of asking became change requests or went to the decider | Fixed (the pause switch) |
| F5 | Ordinary words read as stock codes: "pls", "wow", "bet", "fri", "min" | Fixed 185f176 |
| F6 | "whats running", "hows it goin", "fuck it stop everything" fell through | Fixed 185f176 |
| F7 | Money questions reached the decider with no records; stale numbers from earlier turns | Fixed 185f176 (facts stamped "as of") |
| F8 | Nothing answered "is ibkr connected" / "gateway should be back" from the records (the decider answered about OpenClaw's gateway on 25 Sep) | Fixed 185f176 (from live_data.json, the doctor and the supervisor) |
| F9 | A decision's price source not shown; the chat fetched Yahoo from its poll thread | Fixed 185f176 |
| F10, F11 | A watcher-down day reported as the previous session; "c'mon" read as Monday; past days showed today's positions | Fixed 185f176 |
| F12 | Misroutes ("short answer please" refused as an order; "sorry to interrupt" switched queue mode; report and market-open questions to the decider) | Fixed 185f176 |
| F13 | "yes" to a Foreman message went to the decider without context | Fixed 185f176 |
| F14 | The Trader's change-request go-live note (in .cc-jobs) still says "your commit is the deploy" - a built change is never deployed | **Open** - outside this repo; the Foreman's |
| F15 | "stop" swallowed as the answer to a pending question for 20 minutes | Fixed; slow intents on the poll thread remain (disk reads only now) |
| F16 | Replies naming commands; "tonight's report" at 19:00 | Fixed in the chat; botctl's own lines need its master changed (reported) |

## Dead code

Deleted (H11, H12, H21): the one-off fill corrections and the $10k top-up (applied 23 Sep),
the Yahoo-era plumbing replay (superseded by replay_ibkr), four legacy .ps1 launchers.
Dangerous leftovers fenced (H1-H3, watcher fixer). Kept on purpose: the backtests (they
reproduce reports/phase1*.md), the sim real-money path (SPEC stages, sim only), vendored
botctl. The v1 announcement flow inside watch.py (H10) stays until the test ends: removing
it mid-test is a bigger risk than leaving it.

## Needs Rick's OK

1. **C4**: treat "Pause in Trading" notices as halts (they use up a stock's one look).
2. **C7**: size the v2 rule bot's order down instead of refusing it when its risk is too big.
3. **C9**: a previous close across long halts (so a reinstatement can be measured).
4. **D12**: model the bid-ask spread (every fill pays 0.10% now; v2 allows 3% ticks).
5. **G6**: take the shell away from the trader agents in OpenClaw (prompt injection).
6. **G9/I10, D6**: how partial days (day 1) and watcher-down days count in the 10.
7. **B7 part**: skip a day-trader setup whose stop was touched after its trigger bar.
8. **B8 part / B13**: setup age in market minutes, not bars; the 5-bar minimum for a
   breakout.
9. **H13**: the weekly filter-cost report still scores a 10-session horizon (v1's).
10. **#49** (from 25 Sep): the uneconomic filter also changes the rule bot's record.
11. **Stuck at the close** (the replay, below): 9 positions in 123 days (6 v2, 3 day trader)
    were in stocks too thin to sell by 16:00 at 20% of each bar. Recommended: cap an entry
    at what the stock's usual closing minutes can absorb. Not changed.

## The replay (Rick's third ask) - 26 Sep afternoon

**REPLAY, not live: the frozen rule bots only, no agent.** reports/replay_20260926.md (what
to read) and reports/replay_ibkr_20260926_1319.md/.json (the engine's report, run from an
export of 980b753 with 16 workers, 98 minutes).

- **History**: IBKR 1-minute bars for the ASX 300 and the index, 26 Mar - 25 Sep (129
  sessions): 38,229 stock-days held, 276 known empty, 324 missing (283 of 301 codes
  complete; EF2, AXQ, FDC and LGF have bars only from later in the window), plus the live
  collector's small-cap news stocks for 11 - 25 Sep (2,351 stock-days). The first five
  sessions are the lookback only; 123 sessions replayed (7 Apr - 25 Sep), none with a gap.
  The scheduled fetch finished at 12:52 (reports/ibkr_history_fetch_20260926_1252.json).
- **Day trader v1: not working.** 490 trades, 30% winners, average -0.53R, -$10,197 after
  $6,620 fees, 30 green days and 93 red, worst drawdown 51% of the $20,000 book; it lost on
  up-market days (-$4,047) and down-market days (-$6,150), and every setup lost
  (opening-range breakout -$6,298 on 330, gap-and-go -$2,806 on 119, VWAP reclaim -$1,007
  on 40).
- **Announcements v2 (10:30 rule): unclear.** 187 trades, 47% winners, average -0.04R,
  +$527 after $2,475 fees, 48 green days, 46 red, 29 without a trade, worst drawdown 9.5%;
  the best three trades made 212% of the profit. +$2,243 on up-market days, -$1,717 on down
  days.
- **Stuck at the close**: 9 positions (above, item 11) - an engine issue, recommended, not
  changed. The day's P&L marks them at the last price.
- **Not in it**: the agent's judgment; small-cap news before 22 Sep (the archive holds the
  ASX 300 only); the survivorship of today's index lists; the opening auction for past days.
  Every stock is watched every minute in the replay; live, quiet stocks are polled.
- The verdict rules (`replay_ibkr.verdict`) were fixed before the run. Nothing in the frozen
  test changed. Told to Rick on Telegram (trader) at 15:00.

## Found after the review (26 Sep afternoon)

- **The real-money path's "positions opened today" limit counted the UTC day.** The event
  log stamps fills in UTC; `place_order` compared that date with the Sydney date. From 4 Oct
  (daylight saving) a buy filled 10:00-11:00 Sydney is the previous day in UTC, so the
  per-day limit would have let one more through (sim mode today; the arena books count
  their own orders by Sydney time and were not affected). Found when the deploy's suite
  failed on Saturday afternoon: the test had passed at 04:23 only because Saturday 04:23
  Sydney is still Friday in UTC. **Fixed 17c430d**: a fill carries its Sydney day; the test
  fails on the old code. No other event-log reader compares a UTC date.
- **The history fetch wrote its report inside the release folder** (the scheduled task runs
  a release): fixed 980b753, the report copied to reports/.

## Also open (not fixed, low)

C14 (the index base for news during the session), B10 (whether IBKR's live stream puts the
auction in the 10:00 bar - check on Monday; the day trader's volume tests start at 10:01, so
it cannot reach them), D16 (small wording in the 16:10 summary), G12 (the
model-mismatch self-check's blind spots), A17 (tidy-ups in live.py), H10 (the v1 flow in
watch.py, after the test), the Foreman's parser (F3) and go-live note (F14).

D14 (the streamed bars' volume units, never seen live) is now CHECKED automatically from
Monday (aa6bfd0): the same complete minutes from the stream and from IBKR's history are
compared; the same units are recorded, a factor of 100 or 10 is corrected, anything else is a
MISMATCH - both said loudly by the live_data self-check.

## Commits

8628940 (the connection doctor and the live feed), 9fc46ec (day trader and v2), ac28761
(watcher, books, reports, agents), 185f176 (the chat), 67a4a64 (docs, dead code, the agents'
facts), 5a21ce3 (the replay), aa6bfd0 (streamed volume units), 7908470 (the watcher's day
record), 410741a (a second history fetch from the oldest end), 9e72fad (the news stocks'
history), 980b753 (reports to ASXBOT_HOME), 3949ad5 (the replay's reports), 17c430d (the
per-day limit's Sydney day). Deployed as release 20260926-150638-17c430d7f5: 1073 tests pass
inside the export, 3 skipped. The chat was restarted at 03:49 on Saturday onto the 5a21ce3
release (the chat's code has not changed since); every other task picks up the newest
release at its next start - the history fetch at 17:30 today, the watcher at 07:30 Monday.

## Monday 07:30

Deployable, checked dry on Saturday 26 Sep at 03:51 and again at 15:11 from the deployed
release (now 20260926-150638-17c430d7f5) against a scratch settings folder - no real books,
no Telegram, no agent, no order: config (IBKR live, fresh agent sessions, live order
expiry), the arena (1,825-stock universe, 200 shorts), both playbooks, the IBKR failover
feed connected on the watcher's client id 41, the connection doctor ticking "healthy",
entries allowed, a BHP quote (frozen, as out of hours), and all 13 self-checks ok. The supervisor and the pre-flight
tasks have run from the new release (releases.log); the chat was restarted onto it. The
warm-up task fires at 07:30 local time (the daylight-saving fix), waits for Google Drive if
it is not mounted, and starts `asxbot arena watch --until auto` from this release.

Not provable before Monday: live 5-second bars arriving minute by minute, the real-time bar
budget against IBKR's live pacing, and the auction bar's place in the live stream (B10, D14).
The doctor and the self-checks watch those; the first 30 minutes of Monday are the test.

Rick, before Monday: (1) your IBKR login is stored now, but the Gateway running is still the
one started by hand on 25 Sep - if it dies, the supervisor relaunches it through IBC and your
phone gets the approval request; (2) Windows has a reboot pending (updates paused to 27 Oct):
doing it yourself this weekend is safer than an automatic one in the week.
