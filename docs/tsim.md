# The proper trading simulator, the AI trader and the never-ending strategy search

Built 26 Sep 2026 in a Claude Code cloud session (CLOUD_BRIEF.md). Code: `src/asxbot/lab/tsim/`.
CLI: `asxbot lab sim ...`. It runs inside the Practice Lab's nightly tick (first half of each
tick; `lab/runner._work`), only while the market is closed, with no IBKR connection (stored
history only). **Simulation only: nothing here reaches any broker** (a test fails if tsim ever
imports the real-money path or IBKR).

The Practice Lab's time machine (`lab/sim.py`) stays: it replays the live playbooks through the
live code and is used for calibration (E, the decisions check) and rules-only variants of the
live playbooks. The AI trader and the search run on this simulator.

## 1. The market (`market.py`, `summaries.py`)

- The whole universe (every code in the IBKR 1-minute history, 540 on 26 Sep) and the ASX 200
  index play out on a 373-slot grid, 09:59-16:11 Sydney. A bar is visible only once complete
  and delivered (20 s after its minute ends, as the live feed); announcements only once
  released; daily history only up to the day before (per-code daily summaries built once from
  the same minute history).
- **The opening auction** is IBKR's 09:59 bar: every stock has one (checked on 24 Sep for BHP,
  CBA, FMG, NAB, WBC, S32, ZIP, PLS, MIN, A2M - the staggered open does not show in IBKR's
  stamps, TRACKER #33). **The closing auction** is the 16:10 bar. Both are assumptions about the
  bars, checked by the fidelity run.

## 2. The broker (`broker.py`, `costs.py`)

Market, limit, stop, stop-limit, trailing stop (%), market/limit on close; buy, sell, short
(only stocks on the borrowable list - the ASX 200, the arena's rule), cover; day, GTC and
good-till-time; modify and cancel; brackets (stop, target, trailing stop, one-cancels-other,
reduce-only). The account (cash, positions, orders, trades, marks) carries from day to day.

Fills: only in bars that START after the order reached the broker (the trader's thinking time
included). Marketable orders take the bar's open + half the modelled spread (at least one tick)
+ impact (0.5% x slice / bar volume, capped 2%). Resting limits fill only when the price trades
THROUGH them, at the limit, no spread. A limit that stays marketable after a partial fill keeps
taking the market. Stops trigger when reached; through a gap the reference is the open; a
triggered stop finishes as a market order. No bar fills more than 20% of its volume (all the
account's orders in that stock together). Auctions fill at the auction price with impact but no
spread; no print, no fill. Commission per order on its cumulative filled value (the minimum
once). Short borrow nightly (3% a year).

Spread tiers (full quoted spread, by median daily turnover): >= $50m 0.05%, >= $10m 0.10%,
>= $2m 0.25%, >= $0.5m 0.50%, below 1.00%; half each way, never less than a tick. IBKR's
historical bid/ask was not in the stored history; if it is fetched, it replaces the tiers.

## 3. Brokers (researched 26 Sep 2026)

Every run is scored under every broker in `config.yaml tradesim.brokers` (commission is additive
per order, so the same fills are re-priced). Only one broker could be confirmed to take ASX
share orders through an API a retail trader can use:

| Broker | API places ASX share orders? | ASX commission | Status in the lab |
|---|---|---|---|
| Interactive Brokers AU (TWS API) | **Yes** | Fixed 0.08% min AUD 6 (+GST per the arena's frozen figure; sources conflict on GST); Tiered only cheaper above ~$3m a month | `ibkr` (primary, 0.088% / $6.60) and `ibkr_if_no_gst` (0.08% / $6.00) |
| Tiger Brokers AU (OpenAPI) | Not confirmed: docs list US, HK, CN, SG | 0.025% min $2.50 | `tiger_unconfirmed_api` - a what-if only |
| moomoo AU (OpenAPI) | No: trading covers HK, US, A-shares, SG, JP; ASX "not yet supported" | 0.03% min $3 | not scored |
| Saxo / Totality AU | No: OpenAPI ended for AU accounts 11 Aug 2025 | 0.08% min $3-$14.90 | not scored |
| IG AU (REST API) | CFDs only, not share dealing | - | not scored |
| CMC Invest, Webull AU, Superhero, Stake, CommSec, SelfWealth | No public trading API found | - | not scored |

**Conclusion: the cheapest broker that genuinely supports automated ASX share trading is IBKR
(Fixed).** Every order under $7,500 pays its $6 minimum (0.30% a side on $2,000). The broker
websites refused automated reading from the cloud session, so these figures come from search
results read on 26 Sep 2026; confirm on the pages before relying on them. Sources:
interactivebrokers.com.au/en/pricing/commissions-stocks-asia-pacific.php;
finder.com.au/share-trading/interactive-brokers-share-trading; brokerchooser.com (IBKR, Saxo
reviews); itiger.com/au/commissions; quant.itigerup.com/openapi/en/python/overview/introduction.html;
moomoo.com/au/support/topic6_373; open.moomoo.com/api/overview; forexbrokerz.com (Saxo to
Totality, 11 Aug 2025); labs.ig.com/faq.html; cmcmarkets.com/en-au/stockbroking/products/algorithmic-trading.

## 4. The AI trader (`ai.py`, `tools.py`, `alerts.py`, `prompts/trader.md`)

Claude Opus 5.5, high effort, via `claude -p` directly (never OpenClaw), no tools. Nothing is
pre-selected: it has a scanner (gainers, losers, gaps, relative volume, opening-window relative
volume, turnover, news, range), quotes with estimated bid/ask, charts (1/5/15-minute), daily
history, the announcement feed (and, after the knowledge cutoff, a Sonnet reader's cached
summary where the PDF exists), its account, orders and journal. It answers in JSON: look (up to
3 rounds per wake) or act (orders, alerts, a call-back time, a note). Its real response time is
added to the clock; while it thinks the market moves on.

Wake-ups are its own: price levels, times, relative volume, news for a code or any
price-sensitive announcement (by type), a scanner with its own thresholds, a % move, and its own
fills. Pre-open call at 09:40 (overnight announcements, yesterday's movers, positions carried,
journal lessons); end-of-day call writes the journal (what worked, lessons). Lessons carry only
forward within a run, in date order.

No trading style is hard-coded: no trade counts, risk %, setups or entry rules. The only limits
are the account (cash, leverage 1x, borrowable list), costs, no future data, and a budget guard
(`max_calls_per_day`, 40).

**Measured 26 Sep (15 Jul 2026, one real day):** 31 calls, ~181k Opus tokens (106k cache
writes, 58k cache reads, 16k output), 222 s thinking, 4.2 minutes wall. The brief's estimate was
~200k per day.

Disguise before the knowledge cutoff (1 Jul 2026) - the AI only (plain-code traders see the
market as it was): one alias and one hidden price factor (0.25-4x) per stock for the whole run,
volumes divided by the same factor (so dollar values are real), "Day N" dates with no weekday,
headlines reduced to generic words with no calendar (years, day numbers, months, FY/1H/Q
periods removed). Measured, not assumed: `asxbot lab sim contamination RUN_ID` asks the model
to name the codes and date from the pre-open packet it saw; a day where it names a code among
the aliases or the date within 3 days is contaminated and excluded. First probe (26 Sep, the
first disguise): 2 of 5 days' dates named exactly and 2 stocks named - so the calendar was
stripped and the price factor widened. Second probe: no stock named, 1 of 4 days within 3 days
(reporting-season clues), other guesses years off.

Sampled runs: when the next simulated day is not the next trading session, everything held is
closed at the day's close (auction impact and brokerage paid) and working orders are cancelled,
and the trader is told so in advance - otherwise a position would jump weeks of unsimulated
market. Practice samples are blocks of consecutive sessions.

## 4b. The AI trading team (`team.py`)

Rick, 26 Sep: "build the AI trader as a team of agents working in parallel, not one". Same
simulator, tools, costs and wake-ups as the single AI trader, so the two are measured on the
same days (`asxbot lab sim compare RUN_A RUN_B`). Each wake: READERS (Sonnet 5, one per new
price-sensitive announcement, in parallel) -> SCANNER (Sonnet 5: the plain-code scans and the
readers' notes -> a watchlist of stocks in play, re-run at most every 10 minutes or on news) ->
five SPECIALISTS in parallel (Sonnet 5: opening range, momentum, mean reversion, news/catalyst,
multi-day; each proposes trades in its own style with entry, stop, target, size, confidence) ->
the DECISION-MAKER (Opus 5.5 high: chooses, sizes, manages, sets alerts; the only one that places
orders) -> the RISK MANAGER (Opus 5.5 high: vetoes or cuts every order that adds risk, including
orders left for tomorrow; code enforces it). After the close the decision-maker writes the journal
and the RESEARCHER (Opus 5.5 high) writes lessons for tomorrow and proposes new strategy ideas,
registered in the search (`team-researcher`) to be tested. The clock advances by the slowest agent
of each stage. Budget guard: 18 wakes a day. Measured on its first day: ~170 calls, ~210k Opus
and ~850k Sonnet tokens, 16-18 minutes wall, about 5x the single agent's API-equivalent cost -
almost all of it the specialists re-reading the watchlist each wake.

## 5. Rules-only traders (`rules.py`)

Families: `orb` (A: stocks in play, opening-range breakout), `drift` (announcement drift held
days, plain-code headline classes), `gap_fade`, `vwap_rev`, `hod_mom`. Common: risk-based size,
cost filter (skip if the expected move < 3x the round trip), regime filter (index day / first
30 minutes), closing-volume cap, long only unless borrowable, flat at the closing auction.

## 6. The search (`search.py`, `splits.py`)

Opus proposes ideas from the research log (every idea, result and failure reason). Each is
registered and counted before it runs; exact repeats are refused. Practice: a 20-day sample
that rotates with the idea's number (10 days for the AI trader). Gates: net > 0 after costs,
enough trades, beats the best yardstick and the old rules on the same days, t >= 0.5 sqrt(2 ln N)
(practice) / sqrt(2 ln N) (check). Finalists get the sealed block once (WINNER.md's bar,
t >= 1.65 + 0.5 ln F). The sealed block (initially the lab's locked test, 17 Aug - 25 Sep) is
replaced by the newest 30 trading days after 3 finalists have used it; the old block goes to the
check set. Survivors shadow-trade new days; after 10+ days and 15+ trades in profit, the idea is
written to `data/lab/tsim/winner_pending.json` for the one question to Rick. A rules idea that
reaches the finalist stage is handed to the AI trader as an optional idea (AI spend where it
pays). The log: `reports/tsim_research_log.md`.

## 7. Fidelity (`fidelity.py`)

Every live paper-test order is sent to this broker at its live time; fills and P&L are compared
(`reports/tsim_fidelity_<day>.md`, each live day automatically). The frozen rule bots are also
replayed through the live code and their trades set against the live bots'.

## 8. News coverage (`coverage.py`)

Every day is marked full / partial: the archive holds the ASX 300's announcements; small caps'
only from the live collector's first day (22 Sep). Catalyst strategies are not judged on partial
days. The backfill is the existing collector, plain code, paced and cached:
`asxbot announcements history --universe small` (run on Rick's PC; the cloud session could not
reach asx.com.au).

## Running it on Rick's PC

Nothing to schedule: the Foreman's `asxbot lab tick` now gives tsim the first half of each tick.
By hand (outside market hours): `asxbot lab sim fidelity`, `asxbot lab sim yardsticks`,
`asxbot lab sim e-question`, `asxbot lab sim run --trader ai --from ... --to ...`,
`asxbot lab sim tick`, `asxbot lab sim log`, `asxbot lab sim scoreboard`.
State: `%LOCALAPPDATA%\asx-bot\lab\tsim` (runs, caches, summaries), `data/lab/tsim` (ideas,
splits, yardsticks, contamination, E), `reports/tsim_research_log.md`.
