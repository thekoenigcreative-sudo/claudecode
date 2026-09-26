# Research log - the rules-only strategy search

*RULES-ONLY SEARCH - fake money, plain-code rules, IBKR 1-minute history. Not live, not a verdict on the AI trader.*

*Survivorship bias: the universe is today's index list applied to every past day, so the stocks that later fell out (often after falling) are missing. Results are biased upward; a loss here is a stronger negative than it looks, and a profit is weaker than it looks.*

Rebuilt 2026-09-26 11:06 from data/search/tried.jsonl. Ideas tried: **72**. The check-set bar now stands at t >= 2.92 (sqrt(2 ln N), N = every idea tried).

IBKR 1-minute history for 540 codes and the ASX 200 index, 2026-03-26 to 2026-09-25 (129 sessions): practice (TUNE) 66 sessions, check (VALIDATE) 33, sealed (LOCKED) 30. Announcements in the archive: 405875 (73567 price-sensitive). Stock-days with a closing-auction print: 94%. Frozen rule bots for comparison: replay_ibkr_20260926_1319.json.

## A01 - orb_inplay (defaults)

- Idea #1 (wave 1, parent -), tried 2026-09-26T10:52:43.
- Why it might work: Yardstick A as the brief gives it: opening-window RVOL ranks the day's stocks in play; with a catalyst, the first 5-minute range's break in the opening candle's direction often runs all day (Zarattini & Aziz 2023, US).
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 46 trades, -2,604 after costs, win rate 28.3%, t -2.869, worst day -274, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -2,604

## A02 - orb_inplay (window=10)

- Idea #2 (wave 1, parent A01), tried 2026-09-26T10:52:55.
- Why it might work: A 10-minute range filters the ASX's noisy first minutes after a staggered open; fewer, better-defined breaks.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 52 trades, -1,104 after costs, win rate 40.4%, t -1.294, worst day -269, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -1,104

## A03 - orb_inplay (window=15)

- Idea #3 (wave 1, parent A01), tried 2026-09-26T10:52:56.
- Why it might work: A 15-minute range: wider stop, fewer false breaks, lower cost per R.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 49 trades, -701 after costs, win rate 42.9%, t -0.776, worst day -269, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -701

## A04 - orb_inplay (exit=trail)

- Idea #4 (wave 1, parent A01), tried 2026-09-26T10:52:58.
- Why it might work: The brief's trailing-stop variant: keep the trend days, give back less on reversals (trail 1R once 1R up).
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 46 trades, -2,611 after costs, win rate 30.4%, t -2.865, worst day -274, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -2,611

## A05 - orb_inplay (top_k=2)

- Idea #5 (wave 1, parent A01), tried 2026-09-26T10:52:59.
- Why it might work: Only the two most in-play stocks: if RVOL rank carries the edge, the top of the list should be better than the third.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 33 trades, -2,099 after costs, win rate 24.2%, t -2.778, worst day -274, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -2,099

## B01 - drift (buckets=['results'], hold=2)

- Idea #6 (wave 1, parent -), tried 2026-09-26T10:53:00.
- Why it might work: Post-earnings drift: prices underreact to results, and a strong day-0 reaction keeps drifting (Bernard & Thomas 1989); 2-day hold.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 23 trades, +119 after costs, win rate 43.5%, t 0.102, worst day -774, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - no better than the frozen rule bot (-1,617)

## B02 - drift (buckets=['results'], hold=5)

- Idea #7 (wave 1, parent B01), tried 2026-09-26T10:53:03.
- Why it might work: Results drift over a week (LEARNINGS #9: results were the only type with a pulse - a hypothesis from a slice, so it is tested here out of sample).
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 18 trades, +987 after costs, win rate 55.6%, t 0.783, worst day -307, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - 18 trades (fewer than 20); no better than the frozen rule bot (-749)

## B03 - drift (buckets=['results'], hold=10)

- Idea #8 (wave 1, parent B01), tried 2026-09-26T10:53:05.
- Why it might work: Results drift over two weeks: the literature's drift is slow.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 13 trades, -391 after costs, win rate 53.8%, t -0.272, worst day -408, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -391; 13 trades (fewer than 20); no better than the frozen rule bot (-2,127)

## B04 - drift (buckets=['guidance_up'], hold=5)

- Idea #9 (wave 1, parent -), tried 2026-09-26T10:53:08.
- Why it might work: Guidance upgrades are revisions analysts follow over days.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 2 trades, -217 after costs, win rate 0.0%, t -0.544, worst day -137, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -217; 2 trades (fewer than 20); no better than the frozen rule bot (-1,953)

## B05 - drift (buckets=['contract'], hold=5)

- Idea #10 (wave 1, parent -), tried 2026-09-26T10:53:08.
- Why it might work: Contract wins in small/mid caps are digested slowly by thinly-covered names.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 21 trades, -296 after costs, win rate 61.9%, t -0.213, worst day -675, frozen bot same days +1,736, stuck at close 1.
- Verdict: **failed practice** - P&L after costs -296; no better than the frozen rule bot (-2,032)

## B06 - drift (buckets=['drilling'], hold=5)

- Idea #11 (wave 1, parent -), tried 2026-09-26T10:53:08.
- Why it might work: Drilling/assay news: retail follow-through over days in explorers.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 11 trades, -1,172 after costs, win rate 45.5%, t -0.763, worst day -935, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -1,172; 11 trades (fewer than 20); no better than the frozen rule bot (-2,908)

## B07 - drift (buckets=['takeover'], hold=10, react_max=0.6)

- Idea #12 (wave 1, parent -), tried 2026-09-26T10:53:09.
- Why it might work: Takeover news: the price sits under the offer and closes the gap as the deal firms up (merger arbitrage in miniature).
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 1 trades, -129 after costs, win rate 0.0%, t -1.678, worst day -57, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -129; 1 trades (fewer than 20); no better than the frozen rule bot (-1,865)

## B08 - drift (hold=5)

- Idea #13 (wave 1, parent -), tried 2026-09-26T10:53:09.
- Why it might work: All the brief's types together (results, guidance up, contracts, drilling, takeovers): the broadest drift test, most trades.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 41 trades, -1,344 after costs, win rate 51.2%, t -0.615, worst day -840, frozen bot same days +1,736, stuck at close 1.
- Verdict: **failed practice** - P&L after costs -1,344; no better than the frozen rule bot (-3,080)

## B09 - drift (buckets=['guidance_down'], hold=5, direction=against)

- Idea #14 (wave 1, parent -), tried 2026-09-26T10:53:13.
- Why it might work: Guidance downgrades long only: buy the overreaction after a big fall.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 1 trades, -612 after costs, win rate 0.0%, t -1.116, worst day -535, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -612; 1 trades (fewer than 20); no better than the frozen rule bot (-2,348)

## C01 - orb_inplay (regime=index_ma)

- Idea #15 (wave 1, parent A01), tried 2026-09-26T10:53:13.
- Why it might work: Longs only when the index closed above its 20-session average: breakouts fail more in a falling market.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 10 trades, -444 after costs, win rate 30.0%, t -0.922, worst day -250, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -444; 10 trades (fewer than 20)

## C02 - orb_inplay (regime=first30)

- Idea #16 (wave 1, parent A01), tried 2026-09-26T10:53:14.
- Why it might work: Only in the direction of the index's first 30 minutes (the day's tone); entries wait until 10:31.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 16 trades, -311 after costs, win rate 37.5%, t -1.102, worst day -137, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -311; 16 trades (fewer than 20)

## C03 - orb_inplay (close_cap=0.2)

- Idea #17 (wave 1, parent A01), tried 2026-09-26T10:53:15.
- Why it might work: Closing-volume cap: never hold more than 20% of the usual closing auction, so the flat-by-close exit is always absorbed (9 stuck positions in the replay).
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 46 trades, -2,604 after costs, win rate 28.3%, t -2.869, worst day -274, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -2,604

## C04 - orb_inplay (regime=index_ma, close_cap=0.2)

- Idea #18 (wave 1, parent A01), tried 2026-09-26T10:53:17.
- Why it might work: Both C filters on A together.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 10 trades, -444 after costs, win rate 30.0%, t -0.922, worst day -250, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -444; 10 trades (fewer than 20)

## C05 - drift (hold=5, regime=index_ma)

- Idea #19 (wave 1, parent B08), tried 2026-09-26T10:53:18.
- Why it might work: Drift with the regime filter: underreaction is continued more readily in a rising market.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 15 trades, -183 after costs, win rate 60.0%, t -0.138, worst day -768, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -183; 15 trades (fewer than 20); no better than the frozen rule bot (-1,919)

## C06 - drift (buckets=['results'], hold=5, regime=index_ma)

- Idea #20 (wave 1, parent B02), tried 2026-09-26T10:53:22.
- Why it might work: Results drift with the regime filter.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 1 trades, +51 after costs, win rate 100.0%, t 0.271, worst day -78, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - 1 trades (fewer than 20); no better than the frozen rule bot (-1,685)

## A06 - orb_inplay (catalyst=news)

- Idea #21 (wave 2, parent A01), tried 2026-09-26T10:53:24.
- Why it might work: News-only catalyst: a gap without news may be flow that reverses; news is information that persists.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 41 trades, -2,060 after costs, win rate 26.8%, t -2.305, worst day -397, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -2,060

## A07 - orb_inplay (catalyst=none)

- Idea #22 (wave 2, parent A01), tried 2026-09-26T10:53:25.
- Why it might work: A control: no catalyst at all. If this does as well, the catalyst adds nothing.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 34 trades, -1,133 after costs, win rate 32.4%, t -1.36, worst day -350, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -1,133

## A08 - orb_inplay (min_rvol=3.0)

- Idea #23 (wave 2, parent A01), tried 2026-09-26T10:53:27.
- Why it might work: Only truly in-play stocks (3x usual opening volume); fewer, stronger days.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 44 trades, -2,137 after costs, win rate 29.5%, t -2.473, worst day -274, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -2,137

## A09 - orb_inplay (shorts=True)

- Idea #24 (wave 2, parent A01), tried 2026-09-26T10:53:28.
- Why it might work: The short mirror in the ASX 200 (shortable): down-candle openings with news break down as well as up.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 91 trades, -2,595 after costs, win rate 37.4%, t -1.817, worst day -401, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -2,595

## A10 - orb_inplay (min_r_over_cost=6.0)

- Idea #25 (wave 2, parent A01), tried 2026-09-26T10:53:29.
- Why it might work: Costs eat small ranges: require 1R to be 6x the round trip, not 3x.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 19 trades, -1,238 after costs, win rate 26.3%, t -2.221, worst day -250, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -1,238; 19 trades (fewer than 20)

## A11 - orb_inplay (window=15, exit=trail, trail_r=2.0, trail_after_r=2.0)

- Idea #26 (wave 2, parent A03), tried 2026-09-26T10:53:31.
- Why it might work: Wide range with a loose trail: let the trend days pay for the chop.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 49 trades, -737 after costs, win rate 42.9%, t -0.838, worst day -269, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -737

## A12 - orb_inplay (risk_pct=0.5, top_k=5)

- Idea #27 (wave 2, parent A01), tried 2026-09-26T10:53:32.
- Why it might work: Spread the same risk over more names: lower variance, same edge if it is there (costs rise per trade).
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 67 trades, -1,657 after costs, win rate 31.3%, t -2.069, worst day -317, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -1,657

## B10 - drift (hold=5, entry=open1)

- Idea #28 (wave 2, parent B08), tried 2026-09-26T10:53:33.
- Why it might work: Enter at the next open instead of the day-0 auction: avoids paying the close's spike, sees the overnight.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 41 trades, -2,125 after costs, win rate 43.9%, t -0.819, worst day -1,084, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -2,125; no better than the frozen rule bot (-3,861)

## B11 - drift (hold=5, stop_pct=0.08)

- Idea #29 (wave 2, parent B08), tried 2026-09-26T10:53:37.
- Why it might work: An 8% stop on the daily low: cut the reversals of the reaction.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 44 trades, -1,373 after costs, win rate 47.7%, t -0.699, worst day -758, frozen bot same days +1,736, stuck at close 1.
- Verdict: **failed practice** - P&L after costs -1,373; no better than the frozen rule bot (-3,109)

## B12 - drift (hold=5, react_min=0.06)

- Idea #30 (wave 2, parent B08), tried 2026-09-26T10:53:41.
- Why it might work: Only big reactions (6%+ against the index): stronger news, stronger drift.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 25 trades, +488 after costs, win rate 56.0%, t 0.261, worst day -586, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - no better than the frozen rule bot (-1,248)

## B13 - drift (hold=5, react_min=0.01, react_max=0.03)

- Idea #31 (wave 2, parent B08), tried 2026-09-26T10:53:45.
- Why it might work: Small reactions: underreaction is largest where the market barely moved.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 28 trades, -1,495 after costs, win rate 42.9%, t -0.718, worst day -1,005, frozen bot same days +1,736, stuck at close 1.
- Verdict: **failed practice** - P&L after costs -1,495; no better than the frozen rule bot (-3,231)

## B14 - drift (hold=5, ps_only=False)

- Idea #32 (wave 2, parent B08), tried 2026-09-26T10:53:49.
- Why it might work: Include non-price-sensitive announcements of the same types (more trades).
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 45 trades, +501 after costs, win rate 51.1%, t 0.228, worst day -768, frozen bot same days +1,736, stuck at close 1.
- Verdict: **failed practice** - no better than the frozen rule bot (-1,235)

## B15 - drift (hold=5, min_turnover=5000000.0)

- Idea #33 (wave 2, parent B08), tried 2026-09-26T10:53:56.
- Why it might work: Liquid names only: costs and stuck exits fall; does the drift survive?
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 30 trades, -1,735 after costs, win rate 43.3%, t -0.994, worst day -897, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -1,735; no better than the frozen rule bot (-3,471)

## G01 - gap_fade (defaults)

- Idea #34 (wave 3, parent -), tried 2026-09-26T10:54:00.
- Why it might work: No-news gaps down of 3%+ in liquid names are often flow, not information; buy the turn at 10:30 with a stop under the low, out at the close.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 72 trades, -2,397 after costs, win rate 31.9%, t -2.536, worst day -413, frozen bot same days -4,607, stuck at close 1.
- Verdict: **failed practice** - P&L after costs -2,397

## G02 - gap_fade (gap_min=0.05)

- Idea #35 (wave 3, parent G01), tried 2026-09-26T10:54:02.
- Why it might work: Only big no-news gaps (5%+): more overshoot to recover.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 20 trades, -130 after costs, win rate 40.0%, t -0.284, worst day -139, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -130

## G03 - gap_fade (entry_at=11:00)

- Idea #36 (wave 3, parent G01), tried 2026-09-26T10:54:03.
- Why it might work: Wait until 11:00: the low is more often in by then.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 78 trades, -2,536 after costs, win rate 30.8%, t -2.706, worst day -411, frozen bot same days -4,607, stuck at close 1.
- Verdict: **failed practice** - P&L after costs -2,536

## L01 - late_trend (defaults)

- Idea #37 (wave 3, parent -), tried 2026-09-26T10:54:04.
- Why it might work: Intraday momentum into the close (Gao, Han, Li & Zhou 2018): the day's strong, heavy-volume movers keep rising into the closing auction.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 130 trades, -3,803 after costs, win rate 22.3%, t -6.214, worst day -242, frozen bot same days -4,607, stuck at close 1.
- Verdict: **failed practice** - P&L after costs -3,803

## L02 - late_trend (catalyst=news)

- Idea #38 (wave 3, parent L01), tried 2026-09-26T10:54:05.
- Why it might work: Only movers with price-sensitive news today.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 51 trades, -1,631 after costs, win rate 19.6%, t -3.692, worst day -193, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -1,631

## L03 - late_trend (regime=index_ma)

- Idea #39 (wave 3, parent L01), tried 2026-09-26T10:54:07.
- Why it might work: Late momentum only in a rising market.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 47 trades, -1,161 after costs, win rate 29.8%, t -2.726, worst day -190, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -1,161

## R01 - reversal (defaults)

- Idea #40 (wave 3, parent -), tried 2026-09-26T10:54:08.
- Why it might work: Short-term reversal: a liquid stock's big no-news drop is often liquidity demand that is repaid the next day (Lehmann 1990; Nagel 2012).
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 228 trades, -12,989 after costs, win rate 32.0%, t -4.103, worst day -1,025, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -12,989; no better than the frozen rule bot (-14,725)

## R02 - reversal (hold=3)

- Idea #41 (wave 3, parent R01), tried 2026-09-26T10:54:09.
- Why it might work: The same, held three sessions.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 91 trades, -1,899 after costs, win rate 46.2%, t -0.576, worst day -1,241, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -1,899; no better than the frozen rule bot (-3,635)

## R03 - reversal (drop_min=0.07)

- Idea #42 (wave 3, parent R01), tried 2026-09-26T10:54:09.
- Why it might work: Only big drops (7%+).
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 75 trades, -6,070 after costs, win rate 30.7%, t -3.336, worst day -740, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -6,070; no better than the frozen rule bot (-7,806)

## R04 - reversal (regime=index_ma)

- Idea #43 (wave 3, parent R01), tried 2026-09-26T10:54:10.
- Why it might work: Reversal only in a rising market (a falling one keeps falling).
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 90 trades, -3,585 after costs, win rate 37.8%, t -1.583, worst day -1,025, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -3,585; no better than the frozen rule bot (-5,321)

## K01 - breakout20 (defaults)

- Idea #44 (wave 3, parent -), tried 2026-09-26T10:54:10.
- Why it might work: A 20-session high on double volume: momentum over the next week.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 35 trades, +2,679 after costs, win rate 57.1%, t 1.229, worst day -498, frozen bot same days +1,736, stuck at close 0.
- Check (VALIDATE): 24 trades, -2,031, t -1.521 against a bar of 2.751; 2x spread/impact -2,555; cheapest API broker -1,978.
- Verdict: **failed check** - P&L after costs -2,031; no better than the frozen rule bot (-1,782); worst day -806 (below -3%); t-statistic -1.52 below the bar 2.75 for 44 variants tried; loses with spread and impact doubled (-2,555); carried by its best 3 trades (without them -2,857)

## K02 - breakout20 (hold=10)

- Idea #45 (wave 3, parent K01), tried 2026-09-26T10:54:10.
- Why it might work: The same, held two weeks.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 22 trades, +3,701 after costs, win rate 63.6%, t 1.92, worst day -532, frozen bot same days +1,736, stuck at close 0.
- Check (VALIDATE): 19 trades, -2,122, t -1.616 against a bar of 2.759; 2x spread/impact -2,586; cheapest API broker -2,080.
- Verdict: **failed check** - P&L after costs -2,122; 19 trades (fewer than 20); no better than the frozen rule bot (-1,874); worst day -764 (below -3%); t-statistic -1.62 below the bar 2.76 for 45 variants tried; loses with spread and impact doubled (-2,586); carried by its best 3 trades (without them -3,193)

## D01 - day2_orb (defaults)

- Idea #46 (wave 3, parent -), tried 2026-09-26T10:54:11.
- Why it might work: The day after a 5%+ news reaction, the stock is still in play: its opening range break in the reaction's direction.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 9 trades, -312 after costs, win rate 44.4%, t -0.743, worst day -237, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -312; 9 trades (fewer than 20); no better than the frozen rule bot (-2,048)

## D02 - day2_orb (react_min=0.1)

- Idea #47 (wave 3, parent D01), tried 2026-09-26T10:54:12.
- Why it might work: Only after very big reactions (10%+).
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 5 trades, +42 after costs, win rate 60.0%, t 0.122, worst day -237, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - 5 trades (fewer than 20); no better than the frozen rule bot (-1,694)

## A13 - orb_inplay (window=15, max_value=10000.0, min_adv=50000000.0)

- Idea #48 (wave 4, parent A03), tried 2026-09-26T10:56:01.
- Why it might work: A03 moved +0.46% a trade raw but paid ~0.85%: $10,000 positions in $50m+ turnover names cut the round trip to roughly 0.3%.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 38 trades, +224 after costs, win rate 44.7%, t 0.189, worst day -301, frozen bot same days -4,607, stuck at close 0.
- Check (VALIDATE): 24 trades, -477, t -0.81 against a bar of 2.783; 2x spread/impact -511; cheapest API broker -530.
- Verdict: **failed check** - P&L after costs -477; t-statistic -0.81 below the bar 2.78 for 48 variants tried; loses with spread and impact doubled (-511); carried by its best 3 trades (without them -1,039)

## A14 - orb_inplay (window=15, exit=trail, trail_r=2.0, trail_after_r=2.0, max_value=10000.0, min_adv=50000000.0)

- Idea #49 (wave 4, parent A11), tried 2026-09-26T10:56:16.
- Why it might work: A11 (the loose trail) in liquid names at $10,000: the same cost argument.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 38 trades, +163 after costs, win rate 44.7%, t 0.143, worst day -301, frozen bot same days -4,607, stuck at close 0.
- Check (VALIDATE): 24 trades, -477, t -0.81 against a bar of 2.79; 2x spread/impact -511; cheapest API broker -530.
- Verdict: **failed check** - P&L after costs -477; t-statistic -0.81 below the bar 2.79 for 49 variants tried; loses with spread and impact doubled (-511); carried by its best 3 trades (without them -1,039)

## L04 - late_trend (max_value=10000.0, min_adv=50000000.0)

- Idea #50 (wave 4, parent L01), tried 2026-09-26T10:56:19.
- Why it might work: Late momentum moved +0.22% raw on 130 trades; in liquid names at $10,000 the round trip is ~0.25%, so the question is whether the move holds there.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 7 trades, -84 after costs, win rate 42.9%, t -0.44, worst day -158, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -84; 7 trades (fewer than 20)

## G04 - gap_fade (gap_min=0.05, max_value=10000.0)

- Idea #51 (wave 4, parent G02), tried 2026-09-26T10:56:20.
- Why it might work: Big no-news gap fades moved +0.58% raw; larger positions dilute the brokerage.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 21 trades, -62 after costs, win rate 52.4%, t -0.078, worst day -235, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -62

## B16 - drift (buckets=['results', 'guidance_up'], entry=open0, hold=5)

- Idea #52 (wave 4, parent B02), tried 2026-09-26T10:56:21.
- Why it might work: Pre-open results and upgrades bought in day 0's opening auction when the auction gaps up 3-25%: the day-0 continuation is part of the drift that the close0 entry gave away.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 22 trades, -1,538 after costs, win rate 36.4%, t -0.852, worst day -785, frozen bot same days +1,736, stuck at close 1.
- Verdict: **failed practice** - P&L after costs -1,538; no better than the frozen rule bot (-3,274)

## B17 - drift (entry=open0, hold=1)

- Idea #53 (wave 4, parent B08), tried 2026-09-26T10:56:24.
- Why it might work: Every brief type, bought in the opening auction on a 3-25% gap, sold at the next day's close: the short end of the drift.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 46 trades, -1,909 after costs, win rate 37.0%, t -0.863, worst day -1,032, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -1,909; no better than the frozen rule bot (-3,645)

## B18 - drift (buckets=['results'], hold=5, react_min=0.06, per_position=6000.0)

- Idea #54 (wave 4, parent B02), tried 2026-09-26T10:56:28.
- Why it might work: Results with big reactions (B02 and B12 moved ~2% raw) at a size that dilutes the minimum brokerage.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 9 trades, +73 after costs, win rate 55.6%, t 0.049, worst day -482, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - 9 trades (fewer than 20); no better than the frozen rule bot (-1,663)

## B19 - drift (hold=5, ps_only=False, react_min=0.06)

- Idea #55 (wave 4, parent B14), tried 2026-09-26T10:56:30.
- Why it might work: B14 (all announcements of the types) moved +1.3% raw; with only big reactions (B12's filter) the move per trade should be larger.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 30 trades, +128 after costs, win rate 56.7%, t 0.054, worst day -1,081, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - no better than the frozen rule bot (-1,608)

## K03 - breakout20 (regime=index_ma)

- Idea #56 (wave 4, parent K01), tried 2026-09-26T10:56:38.
- Why it might work: Breakouts made +2.7% raw a trade in practice but lost on the check set, a momentum idea that may need a rising market: only when the index is above its 20-session average. (Proposed from practice, judged on check.)
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 22 trades, +671 after costs, win rate 54.5%, t 0.412, worst day -555, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - no better than the frozen rule bot (-1,065)

## K04 - breakout20 (vol_mult=3.0, hold=10)

- Idea #57 (wave 4, parent K02), tried 2026-09-26T10:56:38.
- Why it might work: Only breakouts on triple volume: stronger demand, held two weeks.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 16 trades, +1,657 after costs, win rate 50.0%, t 1.248, worst day -434, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - 16 trades (fewer than 20); no better than the frozen rule bot (-79)

## W01 - rotation (side=winners, lookback=5, every=5)

- Idea #58 (wave 4, parent -), tried 2026-09-26T10:56:38.
- Why it might work: Weekly rotation into the four strongest liquid names of the week: short-horizon industry/stock momentum, traded once a week in $10m+ names.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 47 trades, +429 after costs, win rate 40.4%, t 0.136, worst day -941, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - no better than the frozen rule bot (-1,307)

## W02 - rotation (side=losers, lookback=5, every=5)

- Idea #59 (wave 4, parent -), tried 2026-09-26T10:56:38.
- Why it might work: The opposite: weekly reversal (Lehmann 1990) - the week's biggest liquid losers bounce.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 47 trades, -1,592 after costs, win rate 36.2%, t -0.377, worst day -1,147, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -1,592; no better than the frozen rule bot (-3,328)

## W03 - rotation (side=winners, lookback=20, every=10)

- Idea #60 (wave 4, parent W01), tried 2026-09-26T10:56:38.
- Why it might work: Monthly momentum, held a fortnight.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 19 trades, -671 after costs, win rate 47.4%, t -0.211, worst day -915, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -671; 19 trades (fewer than 20); no better than the frozen rule bot (-2,406)

## W04 - rotation (side=losers, lookback=20, every=10)

- Idea #61 (wave 4, parent W02), tried 2026-09-26T10:56:38.
- Why it might work: Monthly losers, held a fortnight (medium-term reversal).
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 20 trades, -2,957 after costs, win rate 30.0%, t -0.867, worst day -1,170, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -2,957; no better than the frozen rule bot (-4,693)

## W05 - rotation (side=winners, lookback=5, every=5, regime=index_ma)

- Idea #62 (wave 4, parent W01), tried 2026-09-26T10:56:38.
- Why it might work: Weekly winners only while the index is above its 20-session average.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 16 trades, +206 after costs, win rate 43.8%, t 0.107, worst day -817, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - 16 trades (fewer than 20); no better than the frozen rule bot (-1,530)

## X01 - index_timing (defaults)

- Idea #63 (wave 5, parent -), tried 2026-09-26T10:58:05.
- Why it might work: Intraday index momentum (Gao et al. 2018): after a 0.5%+ index rise by 15:30, the last half-hour tends to follow; in STW the round trip is ~0.3%.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 0 trades, +0 after costs, win rate None%, t 0.0, worst day +0, frozen bot same days +0, stuck at close 0.
- Verdict: **not run - no data** - no practice session had the data this idea trades

## X02 - index_timing (signal=first30, threshold=0.003)

- Idea #64 (wave 5, parent X01), tried 2026-09-26T10:58:17.
- Why it might work: The first half-hour's direction (0.3%+) held to the close in the index fund.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 0 trades, +0 after costs, win rate None%, t 0.0, worst day +0, frozen bot same days +0, stuck at close 0.
- Verdict: **not run - no data** - no practice session had the data this idea trades

## X03 - index_timing (direction=against, threshold=0.008)

- Idea #65 (wave 5, parent X01), tried 2026-09-26T10:58:17.
- Why it might work: The opposite: after a 0.8%+ index FALL by 15:30, buy the fund for the late-day bounce (end-of-day rebalancing flows).
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 0 trades, +0 after costs, win rate None%, t 0.0, worst day +0, frozen bot same days +0, stuck at close 0.
- Verdict: **not run - no data** - no practice session had the data this idea trades

## V01 - vwap_fade (defaults)

- Idea #66 (wave 5, parent -), tried 2026-09-26T10:58:18.
- Why it might work: Large caps stretched 1.5% under VWAP and 1% under the index without news are usually liquidity trades, not information: buy for the move back to VWAP.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 72 trades, -2,940 after costs, win rate 34.7%, t -3.128, worst day -421, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -2,940

## V02 - vwap_fade (dev=0.025, vs_index=0.02)

- Idea #67 (wave 5, parent V01), tried 2026-09-26T10:58:20.
- Why it might work: Only bigger stretches (2.5% under VWAP, 2% under the index).
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 13 trades, -265 after costs, win rate 38.5%, t -0.649, worst day -153, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -265; 13 trades (fewer than 20)

## B20 - drift (buckets=['results'], hold=5, per_position=10000.0, max_positions=2)

- Idea #68 (wave 5, parent B02), tried 2026-09-26T10:58:21.
- Why it might work: B02 (results, a week) moved +2.2% raw a trade: two $10,000 slots halve the brokerage per dollar.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 10 trades, -2,842 after costs, win rate 30.0%, t -1.234, worst day -921, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -2,842; 10 trades (fewer than 20); no better than the frozen rule bot (-4,578)

## K05 - breakout20 (per_position=6600.0, max_positions=3, min_turnover=10000000.0)

- Idea #69 (wave 5, parent K01), tried 2026-09-26T10:58:24.
- Why it might work: Breakouts in $10m+ names at a third of the book each: cheaper to trade, and a different slice of the momentum question K01 failed on the check set.
- Compared with: the frozen v2 rule bot on the same days.
- Practice (TUNE): 11 trades, +2,874 after costs, win rate 81.8%, t 1.432, worst day -414, frozen bot same days +1,736, stuck at close 0.
- Verdict: **failed practice** - 11 trades (fewer than 20)

## X04 - index_timing (etf=INDEX_PROXY)

- Idea #70 (wave 5, parent X01), tried 2026-09-26T10:59:31.
- Why it might work: X01 on the index proxy: intraday index momentum into the close.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 15 trades, -286 after costs, win rate 13.3%, t -3.152, worst day -51, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -286; 15 trades (fewer than 20)

## X05 - index_timing (etf=INDEX_PROXY, signal=first30, threshold=0.003)

- Idea #71 (wave 5, parent X02), tried 2026-09-26T10:59:43.
- Why it might work: X02 on the index proxy: the first half-hour's direction held to the close.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 6 trades, -85 after costs, win rate 66.7%, t -0.781, worst day -92, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -85; 6 trades (fewer than 20)

## X06 - index_timing (etf=INDEX_PROXY, direction=against, threshold=0.008)

- Idea #72 (wave 5, parent X03), tried 2026-09-26T10:59:44.
- Why it might work: X03 on the index proxy: the late-day bounce after a 0.8%+ fall.
- Compared with: the frozen daytrader rule bot on the same days.
- Practice (TUNE): 8 trades, -160 after costs, win rate 0.0%, t -2.775, worst day -37, frozen bot same days -4,607, stuck at close 0.
- Verdict: **failed practice** - P&L after costs -160; 8 trades (fewer than 20)
