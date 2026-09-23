# Learnings — 23 September 2026

The first live warm-up day. Written the same day, while the detail is exact.

Read this before trusting any number in this repo, and before assuming any part of
the system does what its name says.

---

## 1. The one that cost the day

Every announcement PDF fetched before 09:12 was not a PDF. `asx.com.au` does not
serve the document at the announcement link — it serves an "Access to this site"
terms page, and the collector saved that HTML under a `.pdf` name. All 43 files
were 4,596 or 4,729 bytes of markup. `pypdf` threw "Stream has ended unexpectedly"
every time.

Two things turned a bug into a wasted morning:

- `get_bytes` returned early whenever the destination file existed, so the first
  bad save became permanent. Every later poll skipped it. That is why it looked
  intermittent and never recovered.
- The failure was logged at WARNING and swallowed. Nothing anywhere said "the
  agents are about to judge a headline with no document".

So from 07:30 to 09:12 the reader and the decider judged headlines and price
charts, at Sonnet and Opus prices, and the log read as though the system was
working. Sixty-five reader calls and thirty-three decider calls were spent on
announcements nobody had read.

**Rule taken from it:** a fetch that returns the wrong content type must fail
loudly, never be cached, and never be handed to a parser. Check the magic bytes.

---

## 2. Rules that don't mean what they say

### The 1% tick limit is a 50-cent price floor

`max_tick_pct: 1.0` reads like a liquidity test. It isn't. ASX ticks are 0.1c
under 10c, 0.5c from 10c to $2, and 1c at $2 and above. Work it through:

| Price band | Tick | Tick as % of price | Result at 1% |
|---|---|---|---|
| under 10c | 0.1c | more than 1% always | rejected |
| 10c – 50c | 0.5c | 1% – 5% | rejected |
| 50c – $2 | 0.5c | 0.25% – 1% | passes |
| above $2 | 1c | under 0.5% | always passes |

Every ASX stock under 50 cents is excluded, and nothing above $2 is ever excluded.
It caused 32 of 47 screen rejections on the first morning, and it removes most of
the universe that actually issues announcements. PTX was rejected at 10:04 — a $6m
raise on a 6.8c stock — six minutes after the market opened.

It is also too strict on its own logic. Strategy A targets a 5% gap and holds ten
days. Demanding the spread cost stay under 1% is demanding it stay under a fifth of
the move being chased.

**Rule taken from it:** when a threshold rejects something, compute what the rule
excludes, not just how often it fired. A count of rejections is not evidence a
filter is working.

### The screen destroys a strategy that hasn't been built yet

`tradability.py` rejects every announcement of type `trading_halt`, and that type
covers reinstatements. Strategy D in STRATEGIES.md is trading-halt resumptions — a
stock reinstated after a halt that opens strongly on heavy volume. The live screen
throws away D's only trigger.

---

## 3. The live bot does not run the strategy that was backtested

`config.yaml` freezes strategy A as `entry: next_open` — confirm at the day-0
close, enter at the next open. The backtest engine does exactly that
(`entry_lag=1`). The live yardstick bot enters immediately, intraday, at a limit
near the current price, on session-adjusted volume.

Two different strategies with the same name. Whatever the backtest says about A,
the live bot isn't doing it, and the agent-versus-bot comparison measures something
else again.

---

## 4. The arena tests a configuration that cannot be traded

| | Arena (Level 1) | Real-money path (`limits:`) | Backtest |
|---|---|---|---|
| Position size | $4,000 (40% cap binds) | $2,500 max | $2,500 (equity/4) |
| Open positions | 8 | 4 | 4 |
| Leverage | 3x | none | none |
| Hours | opens from 07:00 | 10:00–16:00 | daily bars |

Three different systems. Whatever passes the arena cannot be traded live as tested,
and does not describe what the backtest measured. On $10,000 the binding limit in
the arena is `max_position_pct_of_equity: 40%`, so arena positions are 60% larger
than the ones the backtest scored.

Four sizing limits disagree with each other — `risk_per_trade_pct: 5.0` with an 8%
stop implies $6,250, `max_position_pct_of_equity` says 40%, `max_order_value_aud`
says $8,000, and `capital.max_positions: 4` contradicts `max_open_positions: 8`.
Whichever binds first is accidental rather than chosen. The bot's own sizing never
checks `max_order_value_aud` at all.

---

## 5. The agent and its yardstick are not running the same race

Level 1 is intraday: the 15:50 sweep flattens the agent's positions before the
close unless the decider writes a reason to hold. The yardstick bot holds ten
sessions. The comparison the whole arena exists to make is therefore between a
day-trader and a swing-trader.

The agent said so itself, unprompted, at 09:58 on A1M:

> The yardstick bot will likely take this and may well beat me, because its
> 10-session horizon captures M&A drift that an intraday position cannot.

It had read the acquisition correctly — resource per share up 17%, copper acquired
at about $488/t against the company's own $1,028/t, a $70m placement at no discount
— and passed anyway, because its mandate could not hold the position long enough to
capture what it had found.

**Rule taken from it:** when two things are being compared, check that the rules
they operate under are the same before reading any result.

---

## 6. Every event is judged once, at the worst possible moment

An announcement is worked the minute it arrives; its id then goes into
`data/arena/handled` and it is never looked at again (`watch.py`, line 725).

Most ASX announcements arrive before 10:00. At that hour there is no open to gap
from, so the bot cannot measure its trigger, and the decider's most common stated
reason for passing is that the 10:00 auction will price the news before it can act.
Both reasons expire at 10:00. Nothing revisits them.

Both A1M announcements were judged at 09:56 and 09:59 — the only minutes of the day
when neither side could evaluate them. The first genuinely tradeable event of the
day was missed by both.

The same applies to the morning's PDF failures: an announcement passed on because
its document could not be read is closed forever, even though the document is now
on disk.

---

## 7. Measurement faults

**The arena's headline comparison is raw P&L.** `agent_vs_bot` reports
`agent.pnl - bot.pnl` with no adjustment for position size or exposure. A decider
that consistently bets larger wins the comparison without picking better.

**Win rate is measured before costs.** `realised` in the broker is
`(price - avg_cost) * qty`, gross; brokerage accumulates separately in `fees_paid`.
A trade that makes $5 and pays $13.20 in fees counts as a win.

**"Trades" counts closing orders, not round trips.** A position exited in two parts
counts twice, each scored separately, though the docstring says round trips.

**Green days versus red days only counts days the system ran.** Marks exist only
for days the watcher was up, so a loss on a day the machine was off is invisible to
the ladder test that decides whether a playbook stays at Level 1.

**The holdout was one forgotten flag from being spent.** `asxbot backtest`
simulated the last three years unless `--in-sample-only` was passed. The holdout
gets exactly one run, ever, after the shortlist is frozen; a default that spends it
silently is a trap, not a setting.

---

## 8. A stop can fire in the minute the position opened

The fill takes the *close* of the minute bar. The stop scan starts from that same
minute and triggers if the bar's *low* touched the stop level. A bar whose low
dipped 8% and recovered to close at the fill price registers as both the entry and
the stop: two lots of brokerage, an instant loss, no trade anyone would recognise.

`_stop_rescaled_to_fill` guards the case where the fill price itself lands below
the stop, but not this one. It needs an 8% minute range — which is exactly what
announcement days in small caps produce.

Two related constraints, not bugs: minute bars exist for only 7 days on the free
feed, so an outage longer than that makes pending orders unfillable; and marks fall
back to average cost when no minute data exists, so a weekend or a data gap reads
as flat rather than missing.

---

## 9. Strategy A does not survive breadth

The headline result of the day, once the full archive was available.

| Run | Trades | Companies | Win % | Avg trade after costs | Worst drawdown | CAGR |
|---|---:|---:|---:|---:|---:|---:|
| A, 1x slippage | 492 | 155 | 44.3 | +0.05% | −43.7% | −0.5% |
| A, 2x slippage | 492 | 155 | 42.3 | −0.33% | −55.9% | −2.7% |
| Momentum baseline, 1x | 257 | 105 | 50.2 | +11.52% | −51.5% | +24.5% |
| Benchmark (buy and hold) | — | — | — | — | −55.1% | +6.9% |

The earlier result — 43 trades, 6 companies, 53.5% wins, +4.31% a trade — was six
companies' luck. A loses money after costs, beats neither the benchmark nor the
baseline, and 123 of its 492 trades exit on the stop.

On the small universe A takes no trades at all: the archive covers ASX 300 codes
only, 0 of 1,525 small-cap codes.

**The deeper problem is structural.** Look at `signals.py`: A is the subset of B
events that coincide with an announcement. The trigger is a 5% gap on 3x volume,
and the announcement is a filter applied afterwards. So A never tests whether
announcements produce drift — it tests whether gap-continuation is better when an
announcement caused the gap. Every announcement that produced slow drift instead of
a violent gap is invisible to it.

### Split by announcement type — a hypothesis, not a finding

| Type | Trades | Avg % | Median % | Win % |
|---|---:|---:|---:|---:|
| results | 181 | +0.90 | +0.44 | 53.6 |
| other | 111 | −1.03 | −2.16 | 45.1 |
| exploration | 39 | +0.21 | −7.83 | 28.2 |
| guidance | 38 | −0.67 | −0.70 | 42.1 |
| contract | 31 | +1.39 | −4.96 | 38.7 |
| capital_raising | 29 | −1.30 | −1.36 | 34.5 |
| acquisition | 20 | −3.21 | −2.24 | 10.0 |
| trading_halt | 18 | +1.28 | −1.94 | 44.4 |

Results announcements are the only category with a pulse. **Treat this as a
hypothesis.** It was found by slicing thirteen ways *after* seeing the aggregate
fail, which is exactly the variant-hunting the testing rules exist to stop. If it
is to be tested it goes into config as a dated rule and gets its one holdout run
later, on Norgate data.

---

## 10. No number in this repo is trustworthy yet

`universe.asx300_source: vas_holdings` is current membership only, applied backwards
to 2003. yfinance has no delisted stocks. So every result is biased upward.

The clearest evidence is the baseline: momentum showing a 24.5% CAGR from a
four-stock portfolio is not momentum working, it is survivorship bias in plain
sight. On a universe built from today's 300 largest companies, momentum simply buys
the ones that went on to become large.

This cuts both ways and is worth holding onto: **A losing money on upward-biased
data is a stronger negative than it looks.** And nothing that looks good on this
data can be believed either.

Norgate is the thing that unblocks every other question here.

---

## 11. Process learnings

**A summary is not evidence.** Most of the morning's wrong statements came from
reading Claude Code's reports of its own work instead of opening the files. The
defects in sections 2 through 8 had been sitting in plain text since the day
before; they surfaced in the order files happened to get opened. Reading eleven of
fifty-seven source files produced sixteen faults.

**An agent's account of its own input can be wrong.** Claude Code stated plainly
that two requested items "were not in the message I received". They had been in an
earlier draft, not in the message actually sent — but the claim was asserted with
confidence either way, and was accepted without checking. Check the artefact: the
log, the commit, the file.

**A warning in a log is the story, not a footnote.** Five "could not read the PDF"
lines in one hour were read past for two hours.

**Two Claude Code sessions on one repo interleave.** Repeated "another command ran
in this repository at the same time" notices, and sub-agents finishing mid-run.
One session at a time.

**Don't change a parameter after seeing a result.** Loosening the tick limit at
09:32 because the morning produced no trades would have produced a trade that meant
nothing. When a threshold is changed, it is dated in config with the reason, and
labelled as a deliberate loosening rather than a tuned result.

**But a paper day with no trades teaches nothing either.** Rules that protect
measurement are worth keeping; rules that prevent any observation at all are worth
suspending on fake money. The correction here came from Rick, and it was right.

---

## 12. What the design is actually for

Worth stating because the current build contradicts it.

The one advantage this system has is that it reads a forty-page document in twenty
seconds. That advantage is largest where documents are long, dull and unread:
small-cap filings nobody covers, restructurings, scheme booklets, prospectuses. It
is smallest in liquid ASX 300 names on a same-day horizon, where the market has
already read the document and round-trip costs of 0.5–1% dominate any edge.

The system as built spends that advantage in the place it is worth least: a screen
that admits only stocks above 50 cents, an intraday mandate, and a ten-day
yardstick it is not allowed to match.

A through G are all the same shape — a price-and-volume pattern held for days. That
is a narrow place to look, and it is a crowded one.

---

## 13. What is actually sound

Not everything is broken, and it matters to know which parts to trust.

`orders.py` and `broker.py` are well built. Exits are never blocked; adding to a
losing position is refused; leverage, risk-per-trade and the daily loss limit are
enforced in plain code before anything reaches the broker; the deferred fill walks
forward only, never back. `_stop_rescaled_to_fill` is a genuinely careful piece of
work — when a fill lands on the wrong side of its own stop, the stop is re-derived
at the same percentage distance rather than stopping the position out in the minute
it opened for two lots of brokerage.

`accounts.py` is clean: atomic writes, globally unique order ids, and a daily-loss
baseline taken from the last mark before today so a bad day cannot quietly reset
the limit mid-session.

The backtest orchestration is honest. It warns when archive coverage is below 95%,
labels yfinance results as a plumbing test rather than a go/no-go, separates
in-sample from holdout, and — after a fix today — says "NO TRADES, nothing to
compare" instead of claiming a zero-trade run beat a losing baseline.

And the day's headline result is the best evidence the machinery works: it was
asked whether the flagship strategy made money, and it said no.

---

## 14. The watcher died and nothing inside it could say so

At 13:32:51 on 23 Sep the watcher's log stopped mid-poll: a routine "0 new" line, no
error after it. The warm-up task's last result was 0xC000013A (console closed / Ctrl+C).
The watcher had been running in a visible console window, and that window was closed,
almost certainly by accident while other terminals were being opened. The seven
self-checks run inside the watcher, so they died with it. The hourly digest stopped
too, but a missing message is easy to miss. Nothing was polled or alerted after
13:32, and the pre-close sweep never ran, so A1M went overnight without it. Stops
catch up from the minute bars when the watcher restarts, but only after the fact.
Nobody noticed until someone read the log.

Two lessons. A check that runs inside the process it checks cannot report that
process's death, so liveness has to be watched from outside (watchdog.py, fed by
heartbeat.py). And a long-running process should not own a window someone can close.
The warm-up now has a pythonw launcher with no console (scripts/arena_warmup.pyw).
The evening routine (settle, mark, report) ran in a window the same way, and got the
same launcher the same day (scripts/arena_evening.pyw).

Testing that launcher by killing it mid-step showed the watcher's launcher had only half
its promise. It said output was written "line by line, so a killed watcher still leaves
its last lines". That was true of logging, which goes to stderr. It was not true of
anything printed: Python buffers stdout to a pipe in 8 KB blocks, and a killed process
loses the buffer. Both launchers now run their child with PYTHONUNBUFFERED=1. The test
that caught it kills a real process and reads what it left. Removing that one setting
makes the test fail. Streaming also broke a quiet assumption elsewhere: the $10,000 top-up
(capital.py) took any line after the report step's header to mean the report had
returned, which only held while PowerShell wrote everything at the end. Each step now
closes with an exit line, and that is what capital.py reads.

Found the same afternoon, while testing a fix from a clean checkout: `.gitignore` held
`data/`, which matches every folder named data, so `src/asxbot/data` (eight modules the
backtest and the arena import) had never been committed. The code existed only on
Google Drive. An ignore rule hides mistakes as well as files. `git status` showed
nothing because git was told not to look.

---

## Standing rules

1. Read the file. A summary, a commit message or a passing test count is not
   evidence that something behaves as claimed.
2. When a rule rejects something, compute what it excludes, not just how often it
   fired.
3. A fetch that returns the wrong content type fails loudly and is never cached.
4. Before comparing two things, check they operate under the same rules.
5. Never change a threshold after seeing a result. If it must change, date it in
   config with the reason and label it as a loosening, not a finding.
6. A result found by slicing after the aggregate failed is a hypothesis. It earns
   its holdout run, not a conclusion.
7. Judge an event when it can actually be judged, not when it happens to arrive.
8. One Claude Code session per repository at a time.
9. Nothing can report its own death. Watch long-running processes from outside, and
   never run them in a window that can be closed.
9. On fake money, an observation is worth more than a rule that prevents all
   observation.
10. Nothing on yfinance data is a go or a no-go. Norgate first.
