# CLOUD BRIEF 2 - the rules-only strategy search, run hard on the existing Practice Lab

You are a Claude Code CLOUD session (Anthropic's VM, Linux), on Rick's one-time cloud credit. This repo is
Rick's trade bot (asx-bot). Rick, 26 Sep 2026: "id like it to do everything it can to use up the usage for
the trade bot". A separate cloud session (branch cloud/proper-sim, CLOUD_BRIEF.md) is building the proper
trading simulator and the AI trader - do NOT touch that work or its files. Your job runs alongside it.

## Where you are
- This VM cannot reach Rick's PC, IBKR, Telegram, the ASX website or any live bot; nothing here places an
  order or deploys. Ignore Windows paths, deploy.py, scheduled tasks and market-hours locks in the repo docs.
- DATA: `git fetch origin cloud-data && git checkout origin/cloud-data -- cloud_data`, unzip as its README
  says (1-minute IBKR history per code + ^AXJO; data/announcements, data/prices, data/events, data/universe,
  data/arena). Point the code at the unzipped history on Linux (env var/config) without breaking Windows.
  Never commit data or zips.
- Install the project (pyproject.toml), run the tests, fix only Linux-vs-Windows breakage.
- Branch: `cloud/rules-search`. Commit and push results at least every hour. Open a pull request to main at
  the end with a plain-English summary for Rick.

## The job: find strategies that make money after all costs, rules-only, as many as the credit allows
Use the existing Practice Lab "time machine" (src/asxbot/lab, PRACTICE_LAB.md, WINNER.md) and replay engine.
Rules-only means plain code decides - no model calls - so it can run many ideas cheaply.
1. Yardsticks first, each on the lab's practice split, with honest costs (IBKR ASX 0.08% min AUD 6 per
   order AND the cheapest ASX broker with an API - research which qualify and their fees, with sources;
   spread by price/liquidity tier, at least one tick each way; slippage growing with size vs volume):
   A. Stocks in play, opening-range breakout: rank by opening-window relative volume (the stock's own first
      5-15 min after ITS auction vs its 14-day average for that window), keep only stocks with a catalyst
      (a price-sensitive announcement since the last close, or a 2%+ gap), trade the top 2-3; enter on the
      range break in the opening candle's direction; stop at the other side; hold to the close (trailing
      stop variant); size ~1% risk; skip if expected move < 3x round-trip cost; long only unless shortable.
   B. Announcement drift held 2-10 days, by announcement type (results, guidance up/down, contracts,
      drilling/assays, takeovers) - classify with the headline rules already in the repo, not a model.
   C. Each with a market regime filter (index trend / first-30-minute direction) and the closing-volume
      cap on entries (the replay had 9 positions stuck at the close).
2. Then THE SEARCH LOOP - keep going until the credit is used or the brief is done:
   propose a variant (setup, filter, holding period, exit, sizing, universe) with the reason it might
   work -> test on a practice sample -> confirm promising ones on the check set -> log every idea, its
   result and why it failed in a research log Rick can read (reports/research_log.md). Count every idea
   tried and raise the bar as the count grows; never retune on the sealed period; a finalist gets the
   sealed test ONCE. Widen the search when ideas run dry rather than tweaking the same thing.
3. Judge on WINNER.md's bar (after all costs, 40+ trades, not carried by its best 3 trades, OK on up and
   down days, worst drawdown under 10%, beats the old rules). State the survivorship bias (today's index
   lists on past days) on every report.
4. Write reports/cloud_rules_search_<date>.md: every strategy tried (losers too), how many variants, the
   scoreboard, anything that passed, and what the AI trader (the other session) should try next.
