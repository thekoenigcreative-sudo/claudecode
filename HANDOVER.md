# Handover — 22 Sep 2026, ~20:40 AEST (home PC, RK-MINI)

Supersedes the 15:25 laptop handover. Read PLAN.md first, then SPEC.md, STRATEGIES.md and
ARENA.md.

## Where things stand

The nine SPEC build stages were already done. Tonight added the **arena** — the fake-money
machinery every tactic reuses — and **tactic 1 (ASX announcements)**, proven end to end.

Broker mode is `sim`. Nothing in this repo can place a real order. The arena refuses to
run at all unless the broker mode is `sim`.

**105 tests passing, ruff clean.**

## This PC (RK-MINI)

| What | Where |
|---|---|
| Virtual environment | `C:\venvs\asx-bot` (Python 3.12.12, outside Drive) |
| Activate | `$env:VIRTUAL_ENV = "C:\venvs\asx-bot"` |
| Run | `C:\venvs\asx-bot\Scripts\asxbot.exe <command>` |
| Reinstall deps | `uv pip install -e ".[dev]"` (stop the archive first — it locks `asxbot.exe`) |
| Git identity | Rick Koenig / thekoenigcreative@gmail.com, set locally on this repo |

Note: `uv` resolved **pandas 3.0.6** here where the laptop had 2.x. The whole suite passes
on it, so nothing is pinned.

## The arena — what was built

| Piece | Where | What it does |
|---|---|---|
| Ladder and playbooks | `config.yaml` → `arena:` | Levels 1–3, the `asx_announcements` playbook, runaway guards, the fill rule. All dated 2026-09-22. |
| Minute bars and the fill rule | `arena/minutes.py` | Turns a decision time into a fill price. |
| Account books | `arena/accounts.py` | One JSON book per account, signed quantities (shorts are negative), daily marks. |
| Broker | `arena/broker.py` | Deferred fills, limit orders that rest and expire, stops with a gap rule, brokerage, slippage, short borrow, mark-to-market. |
| Hard limits | `arena/orders.py` | Risk per trade, open positions, leverage, daily loss limit, ASX-200-only shorts, no adding to losers, guards. Exits are never blocked. |
| Yardstick bot | `arena/bots/announcement_drift.py` | Strategy A as a live bot. No model use anywhere in that file. |
| The two agents | `arena/agents.py` | Calls OpenClaw, records the model that actually ran. |
| Orchestrator | `arena/watch.py` | Announcement → bot + reader → decider → limits → broker. |
| Scoreboard | `arena/scoreboard.py` | P&L, green/red days, worst day, drawdown, win rate, fees, the ladder's pass/fail test. |
| Report and Telegram | `arena/report.py`, `telegram.py` | Facts from code, prose from the agent, a code-written fallback that sends anyway. |
| Commands | `arena/cli.py` | `asxbot arena …` and `asxbot telegram …` |

### The deferred fill rule (decided with Rick, 22 Sep)

Free quotes are ~20 minutes delayed, so the true price at the moment of a decision is not
knowable when the decision is made. So:

- every order is recorded `pending_fill` with its decision timestamp;
- it fills from the 1-minute bar covering that minute (`close` of that minute);
- a minute with no trade walks **forward** to the next minute that did trade, never back;
- the same walk applies to stops and exits;
- a stop that is gapped through fills at the bar's open, not at the stop price;
- a limit the market never reached rests, then expires at the close.

One consequence, found and fixed tonight: a stop is *chosen* as a distance but *carried* as
a level, so a fill landing far from the delayed quote could leave a long's stop above its
entry. A stop that ends up on the wrong side of its fill is now re-derived at the same
percentage distance from the price actually paid, and the adjustment is logged.

### OpenClaw

Three agents now. **`main` (JARVIS, the editorial agent) still works exactly as before** —
still the default, still Sonnet 5, still on its own bot.

| Agent | Model | Effort | Workspace | Telegram |
|---|---|---|---|---|
| `main` (JARVIS) | `anthropic/claude-sonnet-5` | (default) | `~\.openclaw\workspace` | `@JARVIS_Z2G9_bot` (`default`) |
| `trader-reader` | `anthropic/claude-sonnet-5` | `low` | `~\.openclaw\workspace-trader-reader` | none |
| `trader-decider` | `anthropic/claude-opus-5` | `high` | `~\.openclaw\workspace-trader-decider` | `@rick_asx_trader_bot` (`trader`) |

Effort is set per agent, 23 Sep, and needed no gateway restart:

```
openclaw config set agents.list.1.thinkingDefault low     # trader-reader
openclaw config set agents.list.2.thinkingDefault high    # trader-decider
```

Confirmed in effect on a live call each: `meta.requestShaping.thinking` came back `low`
for the reader and `high` for the decider. Every arena call now records that value in
`events/arena_agent_calls.jsonl`, so a level that stops applying is visible rather than
assumed. Config backup: `openclaw.json.bak-20260923-pre-thinking`.

**The reader now has two gates.** Its contract ends with `TRADE_WORTHY` and then
`CAN_SIZE_AND_EXIT` ("enough here to size a position and exit it at sensible cost?"), and
code calls the decider only when both are YES. Either line missing or unclear counts as
NO. This is the second look after the plain-code screen, on what got through it.

`channels.telegram` was migrated from a single `botToken` to a two-account form
(`accounts.default` and `accounts.trader`), with explicit bindings so each bot routes to
its own agent. The gateway was restarted on 22 Sep with Rick's approval; both channels
probe as connected. Config backup: `openclaw.json.bak-20260922-pre-telegram-trader`.

Each agent has its own `AGENTS.md` operating manual, mirrored into `docs/agents/`.

Models are set on the agent, never per call (ARENA.md warns the per-spawn override is
silently ignored). Every call records `executionTrace.winnerModel` — what OpenClaw reports
it actually ran — and logs a MODEL MISMATCH warning if it differs. Both matched tonight.

## Tonight's proof

`asxbot arena fake-announcement` ran one fake price-sensitive announcement through the
whole chain. The fill came from the true 14:30 minute bar rather than the delayed quote.
The reader quoted every figure with its page and wrote "not stated" instead of inventing a
counterparty. **Both agents caught the prompt injection planted in the fake announcement**,
named it, and ignored it. The decider passed, correctly: a contract worth 0.4% of revenue
cannot explain a 9% move.

A second run proved the agent's TRADE path on a real small cap (DUG Technology, $232m):
a fake but genuinely material contract, released at 12:55 with the decision at 13:00, so
the delayed quote still showed the pre-announcement price. The yardstick bot declined -
no price reaction, so the plain rule could not act - while the agent read the document,
sized to 2% risk on 57% confidence, and bought. The fill landed at the 13:04 minute bar,
four minutes later, because DUG did not trade in between: the walk-forward rule on a
genuinely illiquid stock. The stop was armed at 1.58 and correctly did not fire (the low
after entry was 1.65). The pre-close sweep then closed it.

Both runs were then cleared with `asxbot arena reset --yes`, so both accounts start the
warm-up at a clean $10,000.

## What is scheduled

| Task | When | What |
|---|---|---|
| `ASXBot Arena Warmup` | 07:30 Mon–Fri | `asxbot arena watch --until auto` (stops 19:25, or 20:25 on daylight saving) |
| `ASXBot Arena Evening` | 19:30 **and** 20:30 Mon–Fri | the wrong slot for today exits immediately; the right one resolves fills → marks to market → agent writes the report → Telegram |

Daylight saving is handled in code (`arena/hours.py`), so nothing needs changing on
4 October or in April. `asxbot arena hours` prints the window for any date.

This PC's clock is AUS Eastern, identical to Sydney. Nothing trades before
`arena.playbooks.asx_announcements.warmup_start` (2026-09-23 07:30) — the watcher waits and
says so. At 15:50 the pre-close sweep asks the decider about each open position and closes
it unless a reason to hold is written.

**The PC must stay on AND Rick must stay logged in.** `G:\My Drive` is a Google Drive mount
that exists only inside his session, so signing out removes the repo from the machine's
view entirely. Locking the screen is fine. Sleep and hibernate are already disabled.

## The terms gate (23 Sep — needs Rick's decision, not the code's)

asx.com.au does not serve an announcement PDF at its link. It serves an "Access to this
site" page whose hidden `pdfURL` field holds the real document on announcements.asx.com.au,
behind an "Agree and proceed" button. The collector now follows that field, which does by
code what a person does by clicking.

What the page asks for is a **use** condition, not a technical one. In its own words:
announcements are free "for investors' private and personal use"; a "distinction is drawn
however where use is for a 'commercial' as opposed to 'private or personal' purpose", and
commercial use needs "the express written authority of ASX". Its examples of professional
or commercial use include accessing the information "in connection with any trade or
business", aggregating and redistributing it, and access by exchange participants or people
employed by a bank, fund or asset manager in connection with that employment.

Fake money in a personal account reads as private and personal use. What would change that:
trading anyone else's money, using it in connection with a business, redistributing the
announcements or anything derived from them, or selling the output. Note the page's own
link to `asx.com.au/legal/general_conditions.htm` no longer resolves to a conditions page —
it redirects to the ASX home page — so the interstitial text above is the operative wording
available. If this ever stops being personal, the honest route is to email info@asx.com.au
for written authority rather than to keep clicking through.

## Announcement archive

Restarted here at 20:11 and running in the background (`--universe asx300`). Progress goes
to `data/history_asx300.err`. Roughly 4–5 hours from 26 codes.

Do not run it on two PCs at once: they share `_progress.json` through Drive.

## Telegram

**23 Sep, first warm-up morning:** 59 announcements in 40 minutes, 60 Sonnet reads and 29
Opus calls, nearly all on microcaps that could never be traded. A plain-code tradability
screen (`arena/tradability.py`) now runs BEFORE any model call and rejects: a halt or
suspension, a stock that has not traded by 10:30, no live quote, a tick worth more than
`trigger.max_tick_pct` (1%) of the price, and median 20-day turnover below
`trigger.turnover_floor_aud`. Replayed over that morning it stops 52 of the 59 (88%) and
24 of the 27 Opus calls. Rejections are logged (`events/arena_screened.jsonl`), never
alerted. The yardstick bot is deliberately untouched, so the comparison stays honest.

Paired and working, both ways. Besides the evening report, the arena now sends **instant
alerts** (`arena/notify.py`): each trade decided (agent or bot) and each refused by the limits,
each fill with its price and stop, every stop that fires and every close with its result, and
passes are NOT instant. The digest goes out **every hour the watcher is up**, quiet hour or
not (a silent hour and a dead watcher must not look alike), headed by a counts line: seen,
screened by test, read, passed, traded. At **16:10** one end-of-session message gives the
day's totals, everything that reached the decider with its reason, and both balances. State
lives in `data/arena/pass_digest.json`; counts are read back from the event log
(`arena/tally.py`) rather than kept twice. Send either by hand with `asxbot arena digest`
(`--summary`, `--print-only`, `--again`). Off switch: `arena.alerts.telegram: false`.

Every announcement that clears the plain-code screen and reaches the reader now also writes
an `arena_gate_compare` record: what the arithmetic said (turnover, tick) against what the
reader's `CAN_SIZE_AND_EXIT` said. Agreement logs at INFO, a disagreement at WARNING, and
the count appears in the 16:10 message - so whether that gate ever overrules the numbers is
a question the log answers rather than a hunch.
Delivery is best effort and never blocks an order, fill or stop. Outbound reports go straight through the Bot API (plain
code, so they still arrive if a model call fails); inbound messages to
`@rick_asx_trader_bot` route to `trader-decider`.

One gotcha worth knowing: **OpenClaw now polls that bot, and `getUpdates` is exclusive.**
`asxbot telegram pair` can no longer discover the chat id by asking Telegram, because
OpenClaw has already consumed the updates. Use `asxbot telegram pair --chat-id <id>`.
Sending is unaffected — only `getUpdates` is exclusive, not `sendMessage`.

## Phase 1 rerun on the finished archive (23 Sep)

The ASX 300 archive finished at 00:13: 300/300 codes, 415,751 rows, 2002-01-02 to
2026-09-22. The 25 "skipped" codes in the run stats are not failures - they are the codes
(360 through ASB, alphabetically first) whose every year was already fetched in the earlier
run, so there was nothing left to request.

`asxbot backtest --in-sample-only` (new flag; stops every run at the holdout start, and
changes no frozen parameter) on both universes at 1x and 2x slippage:

**Strategy A does not survive the breadth.** On asx300 it went from 43 trades over 6
companies to 492 over 155, and the edge went with it: win rate 53.5% -> 44.3%, average
trade +4.31% -> +0.05% at 1x and -0.33% at 2x, CAGR -0.5% and -2.7% against a 6.9%
benchmark, worst drawdown -43.7%. It loses money after costs and does not beat buy-and-hold.
The old result was six companies' luck.

On the small universe A takes no trades at all: the archive covers ASX 300 codes only.
The report now says NO TRADES rather than claiming a zero-trade run beat a losing baseline.

Not a go/no-go either way: yfinance has no delisted stocks, and the momentum baseline's
24.5% CAGR is survivorship bias in plain sight. Norgate first.

## Three changes to get the arena trading (23 Sep, all dated in config.yaml)

1. **Same race as the yardstick.** The playbook now states `holding: days` and
   `hold_sessions: 10`, matching the bot's frozen 10-session rule. The level stays
   intraday; only this playbook overrides it. The 15:50 sweep no longer runs for a
   multi-day playbook, and even under an intraday one it leaves alone a position opened
   with a written multi-day thesis. A new `horizon_exit` closes agent positions at 10
   sessions, so "not intraday" cannot quietly become "held forever".
2. **The 10:20 re-look.** An announcement judged before the open was judged without the
   reaction. Each pre-open pass now gets ONE second look at 10:20 with the opening move and
   volume visible, logged as a re-look (`relook: true` on every record), still facing the
   screen and both reader gates. The yardstick is NOT re-run: its rule is frozen.
   Re-looks done are tracked in `data/arena/relooked/<date>.json`.
3. **max_tick_pct 1.0 -> 3.0.** At 1% this was arithmetically a 50c price floor and bound
   nothing above $2; it caused 32 of 47 screen rejections. A deliberate, dated loosening to
   get the arena trading, not a tuned result.

Frozen strategy A parameters, the holdout and the yardstick's rule were not touched.

## Open items

1. Let the ASX 300 archive finish, then `asxbot backtest` and commit the refreshed
   `reports/phase1.md`. Strategy A still rests on few codes.
2. Then the small-universe archive (~12–15 h), and rerun.
3. Strategies C–G (STRATEGIES.md). F and G can run on price data alone.
4. Norgate, then IBKR paper — only then is any result a go/no-go.

## Deliberate deviation from ARENA.md, flagged for Rick

ARENA.md describes trader-decider "calling `place_order`". Instead the decider returns a
DECISION block and **code** calls `arena_place_order` with it. The invariant that matters is
preserved — the limits are plain code, and the agent never decides an order was placed or
filled; only the broker's returned order id does — and it avoids giving an agent shell
access to a trading command. Easy to switch to the agent-calls-CLI form later; that would
need OpenClaw exec approvals configured for the trader agents.
