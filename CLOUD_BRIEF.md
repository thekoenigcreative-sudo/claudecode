# CLOUD BRIEF - the proper trading simulator, the AI trader and the never-ending strategy search

You are a Claude Code CLOUD session (Anthropic's VM, Linux), running on Rick's one-time cloud credit. This repo
is Rick's trade bot (asx-bot). Rick, 26 Sep 2026: "look all the trade stuff can continue if we run it in the
cloud". His builds on his own PC are held to save his weekly usage; this work runs here instead.

## Where you are, and what that changes
- This VM cannot reach Rick's PC, IBKR, Telegram or any live bot. Nothing you do here places an order,
  deploys, or touches the live 10-day paper test. Ignore any instruction below about Windows paths,
  deploy.py, scheduled tasks, IB Gateway or market-hours locks - they are for builds on his PC.
  Where the brief below says `G:\My Drive\asx-bot`, it means this repo.
- THE DATA is on this repo's `cloud-data` branch (see its README.md). First:
  `git fetch origin cloud-data && git checkout origin/cloud-data -- cloud_data`, then unzip:
  - `cloud_data/ibkr_*.zip` -> the 1-minute IBKR history (one folder per code, plus `^AXJO`, the ASX 200
    index). On Rick's PC it lives at `%LOCALAPPDATA%\asx-bot\ibkr\history`; find how the code locates it
    (src/asxbot paths/config) and point it at your unzipped copy on Linux (env var or config), without
    changing how it works on Windows.
  - `announcements.zip`, `prices_*.zip`, `events_universe_arena.zip` -> under `data/` as in the README.
  Do NOT commit the unzipped data or the zips to your branch (data/ is git-ignored; keep cloud_data/ out too).
- Install the project (pyproject.toml) with pip/uv; run the test suite first to see what's green on Linux,
  and fix anything that only fails because of Windows-only assumptions (keep Windows working).
- Work on a branch named `cloud/proper-sim`. Commit and push often (at least every hour of work), so
  nothing is lost if the session ends. When done, open a pull request to `main` with a plain-English
  summary for Rick: what's built, what ran, what the results are, what's left. A short build on his PC
  will bring it home and check it against the live paper days.
- Decisions inside the simulator are made by Claude Opus 5.5 at high effort, as the brief says. Use the
  lab's existing agent call (src/asxbot/lab/agentcall.py). If model calls aren't possible from inside this
  VM, build and test everything with the agent stubbed or cached, run the rules-only parts for real, and
  say so plainly in the PR - the AI-trader runs then happen on Rick's PC.
- Keep going until the brief is done or the credit runs out. Commit before any long run.

## The brief (written for his PC; apply it here with the changes above)


You are running headless on Opus 5.5 at xhigh, started by Rick's build queue. Work in G:\My Drive\asx-bot.
Read CLAUDE.md, ARENA.md, STRATEGIES.md, TRACKER.md, reports/replay_20260926.md and the Practice Lab's
design first. This is simulation and research only: nothing here places any order anywhere. Leave the frozen 10-day live paper
test exactly as it is. Do NOT end your session until the experiments below are set up in the Practice Lab,
running, and producing results.

FIRST, BEFORE ANY EXPERIMENT:
- A PROPER SIMULATOR FOR THE AI TRADER (Rick, 26 Sep: "i dont feel the simulator is a proper simulator").
  The Practice Lab built today (branch practice-lab, "the time machine") replays a day through the live
  code with the agent answering take-or-skip on setups the RULES find. Keep it for calibration and for
  rules-only variants, but the AI trader needs a real trading simulator, built alongside it:
  1. A market that plays out minute by minute from the stored history - prices and volume for the whole
     universe, and every announcement released at the timestamp it was actually published. The AI sees
     only what existed up to the simulated "now"; the clock keeps moving while it thinks (its real
     response time is added).
  2. A simulated broker: the AI places its own orders (market, limit, stop, stop-limit; buy, sell, and
     short where borrow exists), modifies and cancels them, and sees its positions, orders, cash and
     P&L. Fills are realistic: never better than the bar allows, limit orders fill only if price trades
     through them, the size filled is capped by the volume that actually traded, spread and slippage
     are charged, opening and closing auctions are handled, and IBKR ASX commission applies.
  3. The AI gets a trader's tools inside the simulation: a scanner (movers, gaps, unusual volume), any
     stock's chart and depth of history up to now, the announcement feed, its positions and orders, and
     its journal. It decides what to look at and what to trade - nothing is pre-selected by rules.
  4. Continuity: its account, open positions, orders and journal carry over from one simulated day to
     the next, like a real account.
  5. Fidelity check: run it on the days the live paper test traded and compare with what really
     happened; report the match before trusting any result.
  6. Usage-aware by design: the AI is not called every minute. Like a trader with alerts, it sets its
     own wake-ups (price levels, times, volume alerts, "tell me if X announces") and is also woken by
     the scanner, new announcements and its own fills; plain code does the watching in between. Measure
     tokens per simulated day on the first runs and keep it inside the lab's budget.
  The strategy search loop, the AI trader and all AI experiments below run on THIS simulator.
- Check the Practice Lab is live in asx-bot (the practice-lab-golive build merged it and its nightly runs
  are scheduled). If not, finish that first, following that build's brief and DESIGN.md.
- Make it fast: it took about 12 minutes per simulated day on its first run (15 Jul). Target 2 minutes or
  less - mechanical work in plain code, days run in parallel where they don't depend on each other,
  cached reading - before scaling any experiment.
- It must never interfere with the live 10-day paper test: run lab work only outside ASX market hours
  (after 16:30 until 07:00 on trading days, and weekends); make no IBKR connection from the lab (it uses
  the stored history only); never restart the live bot or break the market-hours lock; and its Claude
  usage never comes out of the live bots' share.
- CALIBRATE BEFORE TRUSTING (Rick doubts the sim's accuracy and whether its usage is worth it - decided
  26 Sep): before any large AI run, replay the days the live 10-day paper test actually traded (25 Sep,
  then each new day as it happens) with the same agent, and compare the lab's decisions, fills and
  P&L with what really happened live. Report the match plainly. If they differ materially, find out why
  and fix the lab (fills, timing, spreads, data) before scaling. Keep doing this every live day: the
  live test is the ground truth the lab is checked against.
- SPEND AI WHERE IT PAYS: most strategy ideas are first tested RULES-ONLY (plain code, almost no Claude
  usage), so the search loop can try many ideas cheaply; the AI trader's judgment (Opus 5.5) is run
  only on ideas that already look promising rules-only, on the calibration days, and on finalists.
- FILL THE NEWS GAP: the announcement archive only has small-cap announcements from 22 Sep, so the lab
  would trade stocks in play without the news that put them in play. Collect the missing announcements
  (small caps back to the start of the price history) with the existing collector - plain code, no AI -
  and mark any day without full news coverage so catalyst strategies aren't judged on it.

GOAL (Rick, 26 Sep): "there are everyday traders not at firms who survive solely doing this, we have the
ability to match that". And, correcting Claude's first version of this brief: "just because i want that
doesn't make that the goal, i'm just saying the ai can DEFINITELY do that, and thats the minimum it should
be doing, i don't want you restricting it too much either".
SO: what a good retail day trader does is THE FLOOR, NOT THE CEILING. The AI trader must at least be able
to do everything a good independent trader does - prepare before the open from the announcements, gaps
and overseas moves; find the stocks in play; plan each trade with where it's wrong; take or skip
setups with judgment; manage risk; cut losers and let winners run; keep a journal and learn from it.
Beyond that the AI is FREE: it chooses its own strategies, setups, holding periods (minutes to days),
how many trades, position sizes and risk per trade, and it can invent, combine and change approaches as
it learns. Do not hard-code a trading style, trade counts, fixed risk percentages or entry rules into
the AI trader. Anything numeric it uses is its own choice, which the lab tunes from results.
The ONLY fixed constraints, because without them the results mean nothing: simulation only (nothing
places any order anywhere); no peeking at the future (the sealed test period and the no-hindsight rules under HONEST TESTING); honest costs on every trade (commission, spread, slippage); and the WINNER bar below
decides what reaches Rick.
The lab improves the AI trader night after night. The rules-only strategies below are yardsticks and
idea sources - never limits on the AI.
THE STRATEGY SEARCH NEVER STOPS (Rick, 26 Sep: "we need a day trading bot that keeps coming up with
strategies until it finds a profitable one"). Build it as a loop that runs every night and weekend inside
the usage budget:
 1. The AI (Opus 5.5) proposes the next strategy idea - new setups, filters, holding periods, exits,
    sizing, or a change to the AI trader's own approach - written down with the reason it might work,
    drawn from the research log, its journals, what has failed before and known market effects.
 2. It is tested on a practice sample; promising ones are confirmed on the check set; failures are
    logged with why they failed, so ideas are never repeated blindly.
 3. Survivors go to the sealed test once, then 10+ days of shadow trading on new live days.
 4. Repeat. It never stops on its own: after a winner it keeps looking for better or for a second
    uncorrelated one; when ideas run dry it widens the search (other holding periods, other signals,
    the announcement flow, sector moves) rather than tweaking the same thing.
 GUARD AGAINST LUCK: testing many ideas means some will look good by chance. Keep a count of every idea
 tried, require the check-set confirmation before the sealed test, never retune on the sealed period,
 and demand more from an idea the more ideas have been tried before it (a stricter bar as the count
 grows). The sealed period wears out with use: count how many finalists have been scored on it, and
 once several have, seal a fresh block of the newest trading days (the history keeps growing) and
 retire the old one to the check set. Keep a research log Rick can read: each idea, result, and verdict.
MODELS AND USAGE (decided 26 Sep from a usage estimate - Claude usage is a real limit, shared with the
live bots and Rick's work assistant). Estimated need per simulated day if built well: ~200k Opus tokens
+ ~100k Sonnet tokens; one full 6-month pass ~25M Opus tokens - a nightly full pass would exhaust the
weekly allowance. So:
1. Every trading DECISION is made by Claude Opus 5.5 at HIGH effort - in the lab and later live, the
   same. Never Sonnet, never Fable, never max. Run one side test of Opus xhigh vs high for decisions
   on the same 10 days; switch only if xhigh clearly makes more after costs, including AI cost.
2. Plain code does all mechanical work (prices, volume, relative volume, triggers, stops, costs) with
   no model calls; the AI is called only at decision points: the pre-open plan, a plan triggering or a
   position needing a call, and the post-close journal.
3. READING is done by Claude Sonnet 5 (reports only, never decides): each announcement is read ONCE and
   its structured summary cached, so replaying days costs almost nothing in reading.
4. Develop on 20-day samples; run the full 6 months only for versions already winning on samples; run
   the sealed test period once, for a finalist.
5. Budget: the lab gets 15% of the weekly Claude allowance (keep a running tally per night, stop for the
   night at its share, never touch the bots' 30% reserve; the Foreman's usage rules apply). Measure the
   real tokens per simulated day on the first night and tune sample sizes to fit. Report usage in the
   weekly scoreboard.
The replay of the frozen day-trader rules lost $10,197 on
$20k over 123 days (30% winners, -0.53R average, $6,620 fees; still about -$3,600 before fees). That design
is retired as a benchmark only. Yardsticks and idea sources, built as rules-only versions:

A. STOCKS IN PLAY, OPENING RANGE BREAKOUT (rules only). Each morning rank the universe by opening-window
   relative volume (volume in the stock's first 5-15 minutes after ITS OWN opening auction, vs its 14-day
   average for that window); keep only stocks with a catalyst - a price-sensitive announcement since the
   last close, or a gap of 2% or more; trade the top 2-3 a day at most. Enter on the break of the
   opening range in the direction of the opening candle; stop at the other side of the range; NO fixed
   profit target - hold to the close (test a trailing stop as a variant). Size by risk: about 1% of the
   account per trade, so positions are large enough that minimum commissions are a small slice. Skip a
   setup if the expected move is under 3x its full round-trip cost. Long only unless the stock is on
   IBKR's shortable list with borrow. Costs: IBKR ASX commission, the bid-ask spread and realistic
   slippage - not commission alone.
B. THE SAME, WITH THE AI CHOOSING: the AI reads each in-play stock's announcement (disguised as set out
   under HONEST TESTING) and decides which to take and with what conviction.
   Compare directly with A.
C. ANNOUNCEMENT DRIFT, HELD 2-10 DAYS: the AI classifies each price-sensitive announcement (results,
   guidance up/down, contracts, drilling/assays, takeovers) and trades the drift over days, fewer trades,
   same cost model.
D. VARIANTS worth testing for each: a market regime filter (index trend / first-30-minute direction), and
   capping every entry at what the stock's usual closing volume can absorb (the replay had 9 positions
   stuck at the close).
E. THE CURRENT DAY TRADER AS IT RUNS IN THE 10-DAY TEST - its rules plus its AI agent choosing which setups
   to take - over the same days. Rick's question: the replay only ran the rules; does the AI's choosing
   rescue those setups? Answer it early and plainly.

HONEST TESTING - without these the results can't be trusted:
- Splits: a practice set (to develop and tune on), a check set (to confirm improvements), and the sealed
  test period (use the lab's existing one; if none, seal the last 30 trading days up to 25 Sep). Rotate
  practice samples so nothing is tuned on the same days over and over. Journal lessons learned on
  practice days never carry into a check or sealed run; within one run, lessons only come from days
  already simulated, in date order.
- No hindsight: disguise tickers AND company names, project and product names in announcement text, and
  shift or hide calendar dates, for every day inside the decision model's knowledge range; before
  trusting results, check the model can't name the real company or date from what it's shown, and treat
  any day where it can as contaminated.
- Spread: 1-minute bars have no bid/ask, so cost each trade's spread honestly - IBKR's historical
  bid/ask data where available, otherwise a conservative spread by price and liquidity tier (at least
  one tick each way) - plus slippage that grows with trade size relative to the stock's volume.
- Brokerage: fees were $6,620 of the old rules' $10,197 loss (IBKR ASX: 0.08%, minimum AUD 6 per order).
  Make the commission model a setting per broker and score every strategy under BOTH IBKR's fees and the
  cheapest ASX broker that supports automated trading through an API (research which brokers qualify and
  their current ASX fees, with sources; record them in the lab's docs). Report both, so the choice of
  broker is made on evidence.
- Survivorship: today's index lists applied to past days flatter results (stocks that later dropped out
  are missing). Use point-in-time lists if they can be obtained; otherwise state the bias on every
  report.

Judge every one on the lab's existing WINNER bar (after all costs, sealed test period, 10+ days of shadow
trading, 40+ trades, not carried by its best 3 trades, OK on up and down days, beats the best rules-only
yardstick above AND the old rules, worst drawdown under 10%). Report to Rick in the Trader chat in plain
words: what the AI trader is trying now, and once a week a short scoreboard of the AI trader against the
yardsticks and the old rules (with E's answer in the first one). Only a winner that passes goes to Rick,
and he decides what happens next. When this build is done, send Rick one plain message in the Trader chat
saying what is now running and when his first scoreboard comes.
