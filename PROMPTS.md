# Prompt queue — 23 September 2026

Paste-ready prompts, in order. One at a time into Claude Code — never two sessions
on this repo at once, and never while it is still working.

Mark each DONE with the commit hash when its output comes back.

---

## DONE

- **Fix the crashing re-look.** Claude Code caught it itself and shipped the fix in
  `b5cc780`: `decider_packet` now takes the re-look note as a named parameter, with
  a test for that exact failure. Watcher restarted 10:29. Eight Sonnet reads were
  wasted by the broken version before it was found.
- **The three arena changes** (`d74d4ef`): 10-session horizon matching the
  yardstick, the 10:20 re-look, max_tick_pct 1.0 → 3.0.
- **First trade, 10:29:46.** ARN-000002: buy 3,000 A1M at a 0.84 limit, stop 0.755,
  target 0.90, pending fill. $2,520 is 25% of equity; risk to stop $255, 2.55%
  against a 5% cap. Seven re-looks reached the decider, six passed. Note for the
  record: A1M is an acquisition, the worst category in the type split (20 trades,
  −3.21% average, 10% win rate) — written down before the outcome is known.


---

## 1. Commit LEARNINGS.md and start the ETF agent

**Where:** Claude Code
**Status:** NEXT
**Why:** no deadline. The money is in cash, the IBKR account isn't funded, and this
work needs no broker at all.

```
Two things. Commit LEARNINGS.md first, untouched — it's the record of today.

Then start a new piece of work, separate from the arena: a long-term ETF research
and monitoring agent. This is for my own real money, currently sitting in cash. It
NEVER places an order and never will — it researches, monitors and reports, and I
decide and execute. Build it that way structurally, not as a config setting, and do
not connect it to any broker.

Use the existing trader-reader agent for the document work — it already reads PDFs
and cites page numbers, which is the same job. Do not create a new agent unless you
can say why trader-reader can't do it.

Start with the questions, not code. Write docs/ETF.md containing:
1. What "safe" has to mean before anything is built — low fee and diversification,
   or limited drawdown, or both — and what each implies for the design. The ASX 200
   fell 54.5% in the GFC and took until July 2019 to pass its November 2007 peak on
   price, though on a total-return basis with dividends reinvested it recovered by
   September 2013. Broad equity also fell about a third in early 2020. Present the
   options; do not choose for me.
2. The Australian specifics that change the answer: franking credits, the 12-month
   CGT discount, domicile and withholding on US-domiciled versus Australian-domiciled
   funds, distribution timing, CHESS sponsorship versus custodial holding. Facts with
   sources, not recommendations.
3. What an agent can genuinely add over a fixed allocation left alone: rebalancing
   discipline within bands, fee and tracking-difference comparison across equivalent
   products, reading each fund's PDS and annual report for changes to fees, index or
   structure, and flagging drift. Be honest about what it cannot add — for a
   set-and-forget portfolio that may be very little.

Then build only the read-only half:
- a watchlist in config.yaml, dated, with the allocation and rebalance bands I set —
  not values you choose
- a monitor that reports drift against target, fee changes, and anything material in
  a fund's own documents
- a backtest of any allocation I propose against long history, with the same
  survivorship warnings as the equity work
- a monthly report on the existing Telegram path

Do not propose specific funds until docs/ETF.md is written and I've answered
question 1. Do not touch the arena, the watcher, or any frozen parameter.
Commit and confirm.
```

---

## 2. $20,000 account, real-money limits, and the bot's entry timing

**Where:** Claude Code
**Status:** PENDING — decisions already made, reasons included
**Why:** every backtest number was computed at $10,000 and doesn't describe the
account. Brokerage is 0.088% with a $6.60 minimum, which binds under $7,500, so
position count sets the cost drag.

```
Two decisions and a re-run. The account is $20,000, not $10,000.

1. Set capital.starting_aud and arena.starting_aud to 20000, dated 2026-09-23.
   Then re-run the Phase 1 backtest in-sample only at the new capital and tell me
   what changes. Reason it matters: brokerage is 0.088% with a $6.60 minimum, and
   that minimum binds on any position under $7,500, so doubling the capital halves
   the cost drag per trade. Every number in the current reports/phase1.md was
   computed at $10,000 and does not describe this account.

2. The arena runs at the real-money limits: 4 open positions maximum, no leverage.
   At $20,000 that is $5,000 a position and a 0.26% round trip, against 0.53% at 8
   positions. Strategy A's average trade after costs was +0.05% at $10,000. And 3x
   leverage means a 10% adverse move costs 30% of equity in a day whatever the
   account size. Set levels.1.leverage_asx to 1.0 and levels.1.max_open_positions
   to 4, dated, with that reasoning in the comment. Leave Levels 2 and 3 alone.

3. Make the live yardstick bot run the frozen rule it is named after: confirm at the
   day-0 close, enter at the next open, as config.yaml strategy.entry says. It
   currently enters immediately at an intraday limit, which is a different strategy
   with the same name and makes the backtest inapplicable to what is running. Keep
   every frozen parameter — 5% gap, 3x volume, 10-day hold, 8% stop — exactly as
   they are. Only the entry timing changes, to match what was written down and tested.

Tell me what all three do to the accounts currently open and whether anything needs
resetting. Do not touch the holdout.
Commit and confirm.
```

---

## 3. Build strategies F and G

**Where:** Claude Code
**Status:** PENDING
**Why:** strategy A is dead on 492 trades, and it structurally cannot trade daily —
its trigger fires a few times a week across the whole ASX 300. F and G scan the
market and have candidates every session. STRATEGIES.md said build them first and
they were never built. Price data only: no archive, no PDFs, no model calls.

```
Strategy A is dead on the full archive, and it structurally cannot trade daily —
its trigger fires a few times a week across the whole ASX 300, while Level 1's aim
is profit every day. Build F and G, which scan the market and have candidates every
session. STRATEGIES.md says build them first and they were never built.

Before writing any strategy code, write both rules into config.yaml under a new
`strategies:` section, dated 2026-09-23, with every parameter explicit — and do not
change them after seeing any result:
F  52-week-high breakout: new 52-week high on close, volume >= N x its 20-day
   average, same turnover floor, long only, fixed hold, stop as % below entry.
G  Pullback in a strong stock: close above the 200-day average, a drop of X% over
   the last K sessions, buy the next open, exit on a bounce of Y% or after Z
   sessions, stop as % below entry.
Choose sensible starting values yourself, write them down with your reasoning as a
comment, and tell me what you picked before running anything.

Then backtest both on the ASX 300 price data, in-sample only, at 1x and 2x
slippage, same capital, same position count and costs as A, using the existing
engine. Report trades, distinct companies, trades per year, win rate, average trade
after costs, median hold, worst drawdown, against the momentum baseline and the
benchmark. Tell me how many days a year each one would have had at least one
candidate — that is the number I care about.
If either loses, say so plainly rather than tuning it.
Do not touch the live watcher, the frozen A parameters, or the holdout.
Commit and confirm.
```

---

## 4. Remaining structural faults

**Where:** Claude Code
**Status:** PENDING

```
Three defects found by reading the source. Fix all three.
1. arena/tradability.py rejects every trading_halt announcement, which includes
   reinstatements. Strategy D in STRATEGIES.md is trading-halt resumptions, so the
   live screen destroys its only trigger. Reject halts and suspensions, let
   reinstatements through, and say in the docstring why.
2. A stop can fire in the minute the position opened. The fill takes the close of the
   minute bar; first_trigger scans from that same minute and triggers on the bar's
   low. A bar whose low dipped 8% and recovered to close at the fill price registers
   as both entry and stop — two lots of brokerage, an instant loss, no trade anyone
   would recognise. Exclude the entry bar from the stop scan, and add a test.
3. guards.min_order_aud is commented "ASX minimum marketable parcel". The $500
   minimum applies to an initial purchase in a company, not every order. Fix the
   comment; leave the value.
Also: the bot's own sizing in announcement_drift.py caps on risk budget, equity and
max_position_pct_of_equity but never on guards.max_order_value_aud, so on a larger
account it would submit orders the broker refuses. Add that cap.
Commit and confirm.
```

---

## 5. Measurement faults

**Where:** Claude Code
**Status:** PENDING — the arena's headline numbers are wrong until these are fixed

```
Four faults in how the arena scores itself. Fix all four.
1. agent_vs_bot reports raw agent.pnl - bot.pnl with no adjustment for position size
   or exposure, so a decider that simply bets larger wins the comparison the whole
   arena exists to make. Add a like-for-like measure — per-dollar-exposed return, or
   both accounts sized identically — and report it alongside the raw figure.
2. Win rate is measured before costs. `realised` in the broker is
   (price - avg_cost) * qty, gross, while brokerage accumulates in fees_paid. A trade
   that makes $5 and pays $13.20 in fees counts as a win. Net the costs.
3. "Trades" counts closing orders, not round trips. A position exited in two parts
   counts twice. Group by position.
4. Green days versus red days counts only days the watcher ran, so a loss on a day
   the machine was off is invisible to the ladder test. Either mark every session or
   say plainly in the report how many sessions were missed.
Commit and confirm.
```

---

## 6. Tonight, after the close

**Where:** Claude Code
**Status:** PENDING — do not start during market hours, it competes with the watcher

```
The market is closed. Start the small-universe announcement archive now — it takes
12-15 hours and should run overnight. Tell me the command you used and how to check
progress in the morning.
```

---

## 7. Norgate — the thing that unblocks everything

**Where:** decision for me, not a prompt yet
**Status:** BLOCKED on me

Nothing in this repo is a go or no-go until the price data includes delisted stocks.
`universe.asx300_source: vas_holdings` is today's membership applied backwards to
2003, and yfinance has no delistings. The momentum baseline's 24.5% CAGR is that
bias in plain sight.

Every strategy result — A's failure included — is provisional until this is fixed.

---

## Standing rules for these prompts

- One Claude Code session at a time on this repo, and never paste while it's working.
- Send me the full output before the next prompt goes in.
- Never change a threshold after seeing a result. If it must change, it is dated in
  config with the reason and labelled a deliberate loosening, not a finding.
- Nothing goes live on anyone's say-so — only on in-sample results I have seen.
