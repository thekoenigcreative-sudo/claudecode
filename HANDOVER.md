# Handover — 22 Sep 2026, ~20:40 AEST (home PC, RK-MINI)

Supersedes the 15:25 laptop handover. Read PLAN.md first, then SPEC.md, STRATEGIES.md and
ARENA.md.

## Where things stand

The nine SPEC build stages were already done. Tonight added the **arena** — the fake-money
machinery every tactic reuses — and **tactic 1 (ASX announcements)**, proven end to end.

Broker mode is `sim`. Nothing in this repo can place a real order. The arena refuses to
run at all unless the broker mode is `sim`.

**88 tests passing, ruff clean.**

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

| Agent | Model | Workspace | Telegram |
|---|---|---|---|
| `main` (JARVIS) | `anthropic/claude-sonnet-5` | `~\.openclaw\workspace` | `@JARVIS_Z2G9_bot` (`default`) |
| `trader-reader` | `anthropic/claude-sonnet-5` | `~\.openclaw\workspace-trader-reader` | none |
| `trader-decider` | `anthropic/claude-opus-5` | `~\.openclaw\workspace-trader-decider` | `@rick_asx_trader_bot` (`trader`) |

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

The test trades were then cleared with `asxbot arena reset --yes`, so both accounts start
the warm-up at a clean $10,000.

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

## Announcement archive

Restarted here at 20:11 and running in the background (`--universe asx300`). Progress goes
to `data/history_asx300.err`. Roughly 4–5 hours from 26 codes.

Do not run it on two PCs at once: they share `_progress.json` through Drive.

## Telegram

Paired and working, both ways. Outbound reports go straight through the Bot API (plain
code, so they still arrive if a model call fails); inbound messages to
`@rick_asx_trader_bot` route to `trader-decider`.

One gotcha worth knowing: **OpenClaw now polls that bot, and `getUpdates` is exclusive.**
`asxbot telegram pair` can no longer discover the chat id by asking Telegram, because
OpenClaw has already consumed the updates. Use `asxbot telegram pair --chat-id <id>`.
Sending is unaffected — only `getUpdates` is exclusive, not `sendMessage`.

## Open items

1. Let the ASX 300 archive finish, then `asxbot backtest` and commit the refreshed
   `reports/phase1.md`. Strategy A still rests on few codes.
4. Then the small-universe archive (~12–15 h), and rerun.
5. Strategies C–G (STRATEGIES.md). F and G can run on price data alone.
6. Norgate, then IBKR paper — only then is any result a go/no-go.

## Deliberate deviation from ARENA.md, flagged for Rick

ARENA.md describes trader-decider "calling `place_order`". Instead the decider returns a
DECISION block and **code** calls `arena_place_order` with it. The invariant that matters is
preserved — the limits are plain code, and the agent never decides an order was placed or
filled; only the broker's returned order id does — and it avoids giving an agent shell
access to a trading command. Easy to switch to the agent-calls-CLI form later; that would
need OpenClaw exec approvals configured for the trader agents.
