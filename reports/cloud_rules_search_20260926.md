# Rules-only strategy search - 2026-09-26

*RULES-ONLY SEARCH - fake money, plain-code rules, IBKR 1-minute history. Not live, not a verdict on the AI trader.*

**Survivorship bias: the universe is today's index list applied to every past day, so the stocks that later fell out (often after falling) are missing. Results are biased upward; a loss here is a stronger negative than it looks, and a profit is weaker than it looks.**

## In short

**No strategy passed.** I tried 72 rule ideas in 10 families: the brief's yardsticks A (stocks in play, opening-range breakout), B (announcement drift by type) and C (the regime filter and the closing-volume cap on both), then variants of them, then eight wider families. Plain code made every decision; no model was asked anything. The ideas were written into the code and committed before they ran: waves 1-3 before any real data was on the machine, waves 4-5 after the practice results only. On the practice months (26 Mar-30 Jun), 68 of the 72 lost money after costs, had too few trades, or did not beat the frozen rule bot. Three of the 68 could not run at all (no index-fund history before 11 Sep); they were re-run on an index proxy as X04-X06. The other 4 made money on practice and lost it on the check months (1 Jul-14 Aug): the two 20-day-breakout ideas and the two liquid-name breakout ideas. **Nothing reached the sealed test, so the locked 17 Aug-25 Sep window is still unspent** for a future finalist.

**Why: costs, not direction.** On a $20,000 book with $4,000-5,000 positions, a same-day trade costs about 0.85% of the money traded, round trip (IBKR's $6.60 minimum each way is 0.3% on its own, plus at least a tick of spread each way). The intraday ideas' raw moves, before costs, were between -1.2% and +0.6% a trade. None of them had an edge to lose. Multi-day holds moved more (20-day breakouts +2.7% to +5% a trade on practice), but that did not survive into July-August. The best near-miss, B12 (buy after an announcement moves a stock 6% or more, hold a week), made money in both windows but is noise: its t-statistic was 0.26 and 0.35 against a bar of about 2.9, and it loses money without its best three trades in both. The cheapest broker with an API (Tiger Brokers AU, about $5.50 an order) saves about $1 an order. It turns 17 practice results positive, mostly by small amounts, and changes no verdict.

**Read every number with the survivorship warning.** Today's index lists were applied to every past day, which flatters every result. Losing on that data is a stronger negative than it looks.

## The data

IBKR 1-minute history for 540 codes and the ASX 200 index, 2026-03-26 to 2026-09-25 (129 sessions): practice (TUNE) 66 sessions, check (VALIDATE) 33, sealed (LOCKED) 30. Announcements in the archive: 405875 (73567 price-sensitive). Stock-days with a closing-auction print: 94%. Frozen rule bots for comparison: replay_ibkr_20260926_1319.json.

## Checks on the engine and the data

- **No look-ahead, on random walks.** Every family was run with its filters loosened on three synthetic markets of 80 random-walk stocks each, where nothing can be predicted by construction. Pooled across the seeds, every family's raw P&L was within 1.03 standard errors of zero. One single-seed result of +2.9 standard errors (drift, 9 trades) was traced trade by trade: the entries and exits were correct, and it disappeared when pooled.
- **The prices are the real ones.** Daily closes built from the minute bars equal Yahoo's closes to the cent for stocks without a dividend in the window. The constant gaps for BHP, CBA and A2M are Yahoo's dividend adjustment. Minute volumes are in shares and sum to 91-98% of Yahoo's daily volume.
- **The auctions.** The opening auction price is the OPEN of the 09:59 bar: it matched Yahoo's official open on 1,174 of 1,174 stock-days (12 codes, dividend adjustment removed). The bar's close did not (it holds some early continuous trades). This was found and fixed before any idea ran. The closing auction prints in the 16:10 bar; 94% of stock-days have one.
- **What the closing-volume cap excluded: nothing.** C03 (yardstick A with the cap) is identical to A01, trade for trade. At $5,000 in the three most in-play names, no position came near 20% of the usual closing auction. Across all 2,499 practice trades, 9 were stuck at the close (sold at the next open and counted). The replay's stuck positions came from thinner names than these rules pick.
- **The cheapest-broker column can be worse than IBKR's** for ideas with a minimum-R-over-costs filter (A01, A04, C01, C03, C04, A10, A12, G03, G04). Cheaper costs let more marginal setups through, so the trades differ.
- **Re-runs, stated.** The first real-data pass was re-run from scratch to add the before-costs column, with the same ideas and rules. Three results (B06, R03, R04) moved by rounding when the daily table went from 32-bit to 64-bit numbers; no verdict changed, and the first pass's log is kept (data/search/tried_pass1_without_cost_split.jsonl). X01-X03 could not run (no index-fund history before 11 Sep). They were re-registered as X04-X06 on an index proxy and are counted twice in N.
- **The sealed window was never opened.** No idea became a finalist, so nothing asked for the locked test and the lab's locked-run log was never written.

## Costs used (fixed before any idea ran)

- Brokerage, IBKR fixed: 0.088% of value, min A$6.60 per order. Source: interactivebrokers.com.au commissions (ASX fixed: 0.08%, min AUD 6, + GST); config.yaml costs.brokerage_pct / brokerage_min_aud
- Brokerage, Tiger Brokers AU: 0.0275% of value, min A$5.50 per order. Source: itiger.com/au/commissions (via search summaries, 26 Sep 2026): A$5 flat per order up to A$20,000, then 0.025%; GST treatment not confirmed, so 10% GST is ADDED here (A$5.50 / 0.0275%) to stay on the dear side. Its Open API SDK lists Australian stocks; ASX order placement by API for a retail AU account is inferred, not proven. Saxo/Totality closed its API to AU accounts (Aug 2025); moomoo's API is US/HK only; Webull AU's API is unconfirmed for ASX.
- Spread: a half-spread paid on every fill, by the stock's median daily turnover (20 sessions before): >= $50m: 0.03%, >= $10m: 0.06%, >= $2m: 0.15%, >= $0m: 0.35% - and never less than one full tick each way.
- Impact: 0.5 x daily volatility x sqrt(order value / daily turnover), capped at 2% a side. Fills capped at 20% of each minute's volume and 20% of the closing auction; what the auction cannot take is sold at the next open and counted as stuck.
- Every idea that reached the check set was also run with spread and impact doubled, and with the cheapest API broker's brokerage.

## Scoreboard

Ideas tried: **72** across 10 families (breakout20, day2_orb, drift, gap_fade, index_timing, late_trend, orb_inplay, reversal, rotation, vwap_fade). Reached the check set: 4. Finalists: 0. Given the sealed test: 0. Passed it: **0**.

Practice = TUNE (26 Mar-30 Jun), check = VALIDATE (1 Jul-14 Aug), sealed = LOCKED (17 Aug-25 Sep). Raw = the price move before any cost (what the idea would make if trading were free), per trade as a share of the money traded, and in dollars.

| Idea | Family | Changes | Practice trades | Raw move / trade | Practice raw $ | Practice P&L (IBKR) | Practice P&L (cheapest API broker) | vs frozen bot | Check trades | Check P&L | Check t | Sealed P&L | Verdict |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| A01 | orb_inplay | defaults | 46 | -0.61% | -1,089 | -2,604 | -2,615 | +2,003 | - | - | - | - | failed practice |
| A02 | orb_inplay | window=10 | 52 | +0.28% | +564 | -1,104 | -990 | +3,503 | - | - | - | - | failed practice |
| A03 | orb_inplay | window=15 | 49 | +0.46% | +808 | -701 | -612 | +3,906 | - | - | - | - | failed practice |
| A04 | orb_inplay | exit=trail | 46 | -0.62% | -1,097 | -2,611 | -2,622 | +1,996 | - | - | - | - | failed practice |
| A05 | orb_inplay | top_k=2 | 33 | -0.86% | -1,049 | -2,099 | -2,045 | +2,509 | - | - | - | - | failed practice |
| B01 | drift | buckets=['results'], hold=2 | 23 | +0.95% | +874 | +119 | +169 | -1,617 | - | - | - | - | failed practice |
| B02 | drift | buckets=['results'], hold=5 | 18 | +2.16% | +1,554 | +987 | +1,027 | -749 | - | - | - | - | failed practice |
| B03 | drift | buckets=['results'], hold=10 | 13 | +0.05% | +26 | -391 | -362 | -2,127 | - | - | - | - | failed practice |
| B04 | drift | buckets=['guidance_up'], hold=5 | 2 | -1.90% | -152 | -217 | -212 | -1,953 | - | - | - | - | failed practice |
| B05 | drift | buckets=['contract'], hold=5 | 21 | +0.61% | +490 | -296 | -250 | -2,032 | - | - | - | - | failed practice |
| B06 | drift | buckets=['drilling'], hold=5 | 11 | -1.09% | -478 | -1,172 | -1,148 | -2,908 | - | - | - | - | failed practice |
| B07 | drift | buckets=['takeover'], hold=10, react_max=0.6 | 1 | -2.44% | -98 | -129 | -127 | -1,865 | - | - | - | - | failed practice |
| B08 | drift | hold=5 | 41 | +0.26% | +417 | -1,344 | -1,254 | -3,080 | - | - | - | - | failed practice |
| B09 | drift | buckets=['guidance_down'], hold=5, direction=against | 1 | -14.29% | -571 | -612 | -609 | -2,348 | - | - | - | - | failed practice |
| C01 | orb_inplay | regime=index_ma | 10 | -0.34% | -117 | -444 | -534 | +4,163 | - | - | - | - | failed practice |
| C02 | orb_inplay | regime=first30 | 16 | +0.29% | +188 | -311 | -294 | +4,296 | - | - | - | - | failed practice |
| C03 | orb_inplay | close_cap=0.2 | 46 | -0.61% | -1,089 | -2,604 | -2,615 | +2,003 | - | - | - | - | failed practice |
| C04 | orb_inplay | regime=index_ma, close_cap=0.2 | 10 | -0.34% | -117 | -444 | -534 | +4,163 | - | - | - | - | failed practice |
| C05 | drift | hold=5, regime=index_ma | 15 | +0.80% | +465 | -183 | -150 | -1,919 | - | - | - | - | failed practice |
| C06 | drift | buckets=['results'], hold=5, regime=index_ma | 1 | +1.76% | +70 | +51 | +53 | -1,685 | - | - | - | - | failed practice |
| A06 | orb_inplay | catalyst=news | 41 | -0.43% | -708 | -2,060 | -2,045 | +2,547 | - | - | - | - | failed practice |
| A07 | orb_inplay | catalyst=none | 34 | -0.04% | -49 | -1,133 | -1,058 | +3,475 | - | - | - | - | failed practice |
| A08 | orb_inplay | min_rvol=3.0 | 44 | -0.40% | -689 | -2,137 | -2,077 | +2,470 | - | - | - | - | failed practice |
| A09 | orb_inplay | shorts=True | 91 | +0.03% | +113 | -2,595 | -2,507 | +2,013 | - | - | - | - | failed practice |
| A10 | orb_inplay | min_r_over_cost=6.0 | 19 | -1.15% | -775 | -1,238 | -1,418 | +3,369 | - | - | - | - | failed practice |
| A11 | orb_inplay | window=15, exit=trail, trail_r=2.0, trail_after_r=2.0 | 49 | +0.44% | +772 | -737 | -648 | +3,870 | - | - | - | - | failed practice |
| A12 | orb_inplay | risk_pct=0.5, top_k=5 | 67 | -0.10% | -176 | -1,657 | -1,916 | +2,950 | - | - | - | - | failed practice |
| B10 | drift | hold=5, entry=open1 | 41 | -0.23% | -368 | -2,125 | -2,035 | -3,861 | - | - | - | - | failed practice |
| B11 | drift | hold=5, stop_pct=0.08 | 44 | +0.27% | +463 | -1,373 | -1,277 | -3,109 | - | - | - | - | failed practice |
| B12 | drift | hold=5, react_min=0.06 | 25 | +1.67% | +1,646 | +488 | +543 | -1,248 | - | - | - | - | failed practice |
| B13 | drift | hold=5, react_min=0.01, react_max=0.03 | 28 | -0.33% | -374 | -1,495 | -1,433 | -3,231 | - | - | - | - | failed practice |
| B14 | drift | hold=5, ps_only=False | 45 | +1.31% | +2,328 | +501 | +600 | -1,235 | - | - | - | - | failed practice |
| B15 | drift | hold=5, min_turnover=5000000.0 | 30 | -0.55% | -662 | -1,735 | -1,669 | -3,471 | - | - | - | - | failed practice |
| G01 | gap_fade | defaults | 72 | +0.02% | +68 | -2,397 | -2,297 | +2,210 | - | - | - | - | failed practice |
| G02 | gap_fade | gap_min=0.05 | 20 | +0.58% | +513 | -130 | -43 | +4,477 | - | - | - | - | failed practice |
| G03 | gap_fade | entry_at=11:00 | 78 | +0.02% | +71 | -2,536 | -2,678 | +2,071 | - | - | - | - | failed practice |
| L01 | late_trend | defaults | 130 | +0.22% | +1,362 | -3,803 | -3,517 | +804 | - | - | - | - | failed practice |
| L02 | late_trend | catalyst=news | 51 | +0.17% | +412 | -1,631 | -1,519 | +2,976 | - | - | - | - | failed practice |
| L03 | late_trend | regime=index_ma | 47 | +0.30% | +662 | -1,161 | -1,058 | +3,446 | - | - | - | - | failed practice |
| R01 | reversal | defaults | 228 | -0.48% | -4,386 | -12,989 | -12,487 | -14,725 | - | - | - | - | failed practice |
| R02 | reversal | hold=3 | 91 | +0.38% | +1,392 | -1,899 | -1,699 | -3,635 | - | - | - | - | failed practice |
| R03 | reversal | drop_min=0.07 | 75 | -0.96% | -2,881 | -6,070 | -5,905 | -7,806 | - | - | - | - | failed practice |
| R04 | reversal | regime=index_ma | 90 | -0.09% | -330 | -3,585 | -3,387 | -5,321 | - | - | - | - | failed practice |
| K01 | breakout20 | defaults | 35 | +2.67% | +3,730 | +2,679 | +2,756 | +943 | 24 | -2,031 | -1.52 | - | failed check |
| K02 | breakout20 | hold=10 | 22 | +4.97% | +4,358 | +3,701 | +3,749 | +1,965 | 19 | -2,122 | -1.62 | - | failed check |
| D01 | day2_orb | defaults | 9 | +0.04% | +16 | -312 | -293 | -2,048 | - | - | - | - | failed practice |
| D02 | day2_orb | react_min=0.1 | 5 | +1.16% | +277 | +42 | +53 | -1,694 | - | - | - | - | failed practice |
| A13 | orb_inplay | window=15, max_value=10000.0, min_adv=50000000.0 | 38 | +0.40% | +1,237 | +224 | +395 | +4,831 | 24 | -477 | -0.81 | - | failed check |
| A14 | orb_inplay | window=15, exit=trail, trail_r=2.0, trail_after_r=2.0, max_value=10000.0, min_adv=50000000.0 | 38 | +0.38% | +1,176 | +163 | +334 | +4,771 | 24 | -477 | -0.81 | - | failed check |
| L04 | late_trend | max_value=10000.0, min_adv=50000000.0 | 7 | +0.19% | +135 | -84 | -38 | +4,523 | - | - | - | - | failed practice |
| G04 | gap_fade | gap_min=0.05, max_value=10000.0 | 21 | +0.59% | +831 | -62 | -93 | +4,545 | - | - | - | - | failed practice |
| B16 | drift | buckets=['results', 'guidance_up'], entry=open0, hold=5 | 22 | -0.87% | -761 | -1,538 | -1,490 | -3,274 | - | - | - | - | failed practice |
| B17 | drift | entry=open0, hold=1 | 46 | -0.05% | -83 | -1,909 | -1,808 | -3,645 | - | - | - | - | failed practice |
| B18 | drift | buckets=['results'], hold=5, react_min=0.06, per_position=6000.0 | 9 | +0.88% | +476 | +73 | +93 | -1,663 | - | - | - | - | failed practice |
| B19 | drift | hold=5, ps_only=False, react_min=0.06 | 30 | +1.32% | +1,559 | +128 | +194 | -1,608 | - | - | - | - | failed practice |
| K03 | breakout20 | regime=index_ma | 22 | +1.53% | +1,345 | +671 | +719 | -1,065 | - | - | - | - | failed practice |
| K04 | breakout20 | vol_mult=3.0, hold=10 | 16 | +3.37% | +2,146 | +1,657 | +1,692 | -79 | - | - | - | - | failed practice |
| W01 | rotation | side=winners, lookback=5, every=5 | 47 | +0.86% | +1,916 | +429 | +532 | -1,307 | - | - | - | - | failed practice |
| W02 | rotation | side=losers, lookback=5, every=5 | 47 | +0.01% | +21 | -1,592 | -1,489 | -3,328 | - | - | - | - | failed practice |
| W03 | rotation | side=winners, lookback=20, every=10 | 19 | -0.09% | -80 | -671 | -629 | -2,406 | - | - | - | - | failed practice |
| W04 | rotation | side=losers, lookback=20, every=10 | 20 | -2.31% | -2,188 | -2,957 | -2,913 | -4,693 | - | - | - | - | failed practice |
| W05 | rotation | side=winners, lookback=5, every=5, regime=index_ma | 16 | +0.89% | +674 | +206 | +241 | -1,530 | - | - | - | - | failed practice |
| X01 | index_timing | defaults | 0 | - | +0 | +0 | - | +0 | - | - | - | - | not run - no data |
| X02 | index_timing | signal=first30, threshold=0.003 | 0 | - | +0 | +0 | - | +0 | - | - | - | - | not run - no data |
| X03 | index_timing | direction=against, threshold=0.008 | 0 | - | +0 | +0 | - | +0 | - | - | - | - | not run - no data |
| V01 | vwap_fade | defaults | 72 | -0.04% | -292 | -2,940 | -2,474 | +1,668 | - | - | - | - | failed practice |
| V02 | vwap_fade | dev=0.025, vs_index=0.02 | 13 | +0.18% | +237 | -265 | -179 | +4,343 | - | - | - | - | failed practice |
| B20 | drift | buckets=['results'], hold=5, per_position=10000.0, max_positions=2 | 10 | -2.22% | -2,213 | -2,842 | -2,778 | -4,578 | - | - | - | - | failed practice |
| K05 | breakout20 | per_position=6600.0, max_positions=3, min_turnover=10000000.0 | 11 | +4.43% | +3,215 | +2,874 | +2,899 | +1,138 | - | - | - | - | failed practice |
| X04 | index_timing | etf=INDEX_PROXY | 15 | +0.02% | +28 | -286 | -220 | +4,368 | - | - | - | - | failed practice |
| X05 | index_timing | etf=INDEX_PROXY, signal=first30, threshold=0.003 | 6 | +0.08% | +42 | -85 | -58 | +4,522 | - | - | - | - | failed practice |
| X06 | index_timing | etf=INDEX_PROXY, direction=against, threshold=0.008 | 8 | +0.01% | +6 | -160 | -126 | +4,465 | - | - | - | - | failed practice |

## What passed

Nothing passed. No strategy in this search has shown an edge after costs that survived the practice split, the check set and the sealed test.

## Near-misses on the check set (information only - they stay failed)

These made money on practice with 20+ trades but were stopped only because they did not beat the frozen rule bot on the same days. Their check-set result is shown so the next session knows whether the edge held; it cannot promote them.

| Idea | Check trades | Check P&L | Check t | Raw $ |
|---|---:|---:|---:|---:|
| B01 | 36 | -66 | -0.042 | +1,308 |
| B12 | 20 | +921 | 0.349 | +1,734 |
| B14 | 31 | -686 | -0.233 | +298 |
| B19 | 20 | +921 | 0.349 | +1,734 |
| K03 | 23 | -2,118 | -1.592 | -1,307 |
| W01 | 28 | -2,038 | -1.089 | -1,141 |

## The frozen rule bots on the same windows (the bar to beat)

- day trader v1, tune: 234 trades, -4,607 after costs (the arena's cost model).
- day trader v1, validate: 139 trades, -3,206 after costs (the arena's cost model).
- announcements v2, tune: 85 trades, +1,736 after costs (the arena's cost model).
- announcements v2, validate: 48 trades, -248 after costs (the arena's cost model).

## For the AI trader (the other cloud session)

1. **Size decides whether an intraday idea can win.** At $4,000-5,000 a position the round trip is about 0.85%. At $10,000 in the ~28 names turning over $50m+ a day it is about 0.3%. An intraday setup the AI takes needs an expected move clearly above that, and 1R under about 1% of the price is not worth a call. The rules found no raw edge in "stocks in play" selection by volume and catalyst alone (yardstick A's raw move: -0.6% to +0.5% a trade). The agent's judgment has to supply the edge; the setup list will not.
2. **Where the raw moves were biggest: multi-day holds after big news reactions (6%+) and 20-day breakouts.** Neither held from April-June into July-August. The announcement type that carried the best near-miss flipped between windows (contracts +$963 on practice, -$1,570 on check). That is where an AI reader could plausibly add something a headline rule cannot: telling a contract that matters (its size against the company) from one that does not, and results against expectations. Test it only on days after the model's cutoff (VALIDATE onward), anonymised before, as PRACTICE_LAB.md says.
3. **The frozen rule bots are a noisy bar.** Announcements v2 made +$1,736 on the practice months (carried by three trades, per the replay report) and -$248 on the check months. Day trader v1 lost -$4,607 and -$3,206. "Beats the frozen bot" on practice mostly measured v2's luck.
4. **Data facts the simulator should use:** the opening auction is the OPEN of the 09:59 bar (it matched the official open on 1,174 of 1,174 stock-days; the bar also holds early continuous trades). The closing auction prints in the 16:10 bar. The staggered open does not show in the IBKR bars (every group trades from 10:00). STW and VAS history starts on 11 Sep only, so fetch index-fund history before trying any index idea for real.
5. **Keep the sealed window for a real finalist.** It is still unspent. Every idea tried here is counted (N = 72), so the check-set bar now stands at t >= 2.92 for anything proposed next.

## Every idea, in full

See reports/research_log.md.
