# Strategy testing plan
*22 Sep 2026; register rewritten 23 Sep 2026. Extends SPEC.md. Every strategy below gets tested under the rules in this file.*

## Owner decisions
- Test several strategies, not one. Keep what survives, drop the rest.
- ASX website terms are not a reason to drop, narrow or delay any strategy here. The announcement collector stays; collection itself follows SPEC section 4.
- Every strategy uses the same capital ($10,000), max positions (4), costs, slippage model and both universes (ASX 300 and smaller stocks).
- All signals come from public data: prices and published announcements.

## Testing rules (so a lucky result can't pass as a real one)
1. Write each strategy's rules into `config.yaml`, dated, BEFORE running it. No changes after seeing results.
2. Test on the in-sample years only.
3. Shortlist at most the best 3, then run each ONCE on the 3-year holdout.
4. Report every strategy tried, including the losers, and how many variants were tested.
5. yfinance results are labelled "plumbing test — not a go/no-go". The real verdict needs Norgate full history.

## Strategy register

**Planning note.** Published cross-sectional return predictors earn about 26% less out-of-sample and 58% less after publication, and the decline is biggest for the strongest in-sample results (McLean & Pontiff, 97 predictors). Treat any figure from a paper as an upper bound.

Every figure carries its source in brackets. Figures from `reports/phase1.md` are a plumbing test under rule 5: yfinance, ASX 300 proxy universe, in-sample only, 1× slippage unless stated.

| ID | Strategy | Verdict | Data needed | Next step |
|---|---|---|---|---|
| A | Announcement gap | **Dropped** | Archive + prices | Keep running live only as a machinery test |
| B | Volume surprise | **Dropped** | Prices | None |
| C | Earnings drift | **Keep, rebuild** | Archive + prices | Measure surprise against the company's own prior guidance; 30–60 session hold |
| D | Trading-halt resumptions | Keep, low | Archive + prices | Check whether the tradability screen still rejects reinstatements |
| E | Directors buying | **Upgraded** | Archive + 3Y PDFs + prices | Reader classifies each Appendix 3Y; test small and large firms separately; hold measured in months |
| F | 52-week-high breakout | **Downgraded** | Prices | Build only because it is cheap; expect it to fail |
| G | Pullbacks | **Reframed** | Prices + industry classification | Large, liquid stock down against its industry peers over a month, momentum controlled for |
| N1 | Placement overhang | **New** | Archive + prices | Build first as a veto on other strategies |
| N2 | Index deletion reversal | New, low | Index rebalance history + prices | Needs rebalance history, which Norgate supplies |
| S1 | Short after an earnings miss | **Tested: loses** | Archive + prices, ASX 200 | Not tuned. Only a rerun of the same rules on survivorship-free data |
| S2 | Short ahead of placement shares | **Tested: loses** | Archive + prices, ASX 200 | Not tuned. Not worth a holdout run |
| Baseline | 12-1 momentum | Keep as baseline only | Prices | Rerun on survivorship-free data |

### A — announcement gap: DROPPED
492 trades across 155 companies: +0.05% per trade at 1× slippage and −0.33% at 2× (`reports/phase1.md`). The rule is also misspecified: it triggers on a 5% gap and uses the announcement only as a filter, so it tests gap continuation, not drift after surprise. Keep running live only as a machinery test.

### B — volume surprise: DROPPED
Loses after costs, on both universes at 1× and 2× slippage (`reports/phase1.md`).

### C — earnings drift: KEEP, REBUILD
Post-earnings-announcement drift is documented in Australia but has weakened as information spreads faster and costs fall. The only positive slice of A was results announcements: 181 trades, +0.90% average, 53.6% win rate (`reports/phase1.md`, announcement-type split). That is a hypothesis found by slicing, not a finding. Rebuild C to measure the surprise against the company's own prior guidance in the announcement archive, with a 30–60 session hold.

### D — trading-halt resumptions: KEEP, LOW
No evidence found either way. Before building, check whether the tradability screen still rejects reinstatements.

### E — directors buying: UPGRADED
Discretionary director purchases showed a +4.6% cumulative abnormal return over 200 trading days; non-discretionary purchases showed −4.7% (Foley, Kwan, McInish & Philip 2016). That study classified over 60,000 director transactions as discretionary or non-discretionary using the trading motive the insider gave — the same classification the reader would do on each Appendix 3Y.

The evidence on firm size conflicts. Foley et al. find director purchases in small firms earn higher returns, and that returns are higher for larger trades and strongest for purchases. A separate 2023 Australian study ("Director trades, profitability and market efficiency: New evidence") finds insiders profit mainly in larger firms. Test E on small and large firms separately.

The reader should classify each Appendix 3Y as a discretionary on-market purchase versus a plan, dividend reinvestment or option exercise. Hold measured in months, not 10 sessions.

### F — 52-week-high breakout: DOWNGRADED
Tested on the ASX over 1996–2008 and found not of practical use once short-sale restrictions and transaction costs are counted (Bettman, Sault & von Reibnitz 2010). Build only because it is cheap; expect it to fail.

### G — pullbacks: REFRAMED
In Australia, over a one-month horizon, small stocks tend to reverse while large stocks tend to trend, and the small-stock reversals are driven by illiquidity, so costs consume them (Chai & Do 2016). What holds up is narrower: large stocks show intra-industry reversals once price momentum is controlled for. Rebuild G as a large, liquid stock that has fallen against its own industry peers over the past month, with momentum controlled for. Plain "buy any dip in an uptrend" is dropped.

### N1 — placement overhang: NEW
Australia puts no resale restriction on placement shares, so holders can sell immediately. Capital-raising announcements scored a −1.30% average and a 34.5% win rate in A, on 29 trades (`reports/phase1.md`, announcement-type split). The decider reasoned about quotation-date supply unprompted on TGN and AUE on 23 Sep (`data/logs/asxbot.log`). Build first as a VETO on other strategies: do not open a long when new placement shares quote inside the hold window.

### N2 — index deletion reversal: NEW, LOW
S&P/ASX 200 deletions showed negative returns on announcement that began to reverse afterwards (Schmidt, Zhao & Terry 2011). Long-only friendly and scheduled, but likely decayed. Needs rebalance history, which Norgate supplies.

### S1 and S2 — the short side: TESTED, BOTH LOSE
Rules written into `config.yaml` (`strategies`) and committed before any run (`3a936e4`), the code committed before its first run too (`d009048`); one variant each, not tuned since. Tested in-sample only, 2003-01-01 to 2023-09-22, inside today's ASX 200 only, the arena's short universe: shares outside it usually cannot be borrowed. Figures below are from `reports/shorts_phase1.md`, across its four cost settings: 1× and 2× slippage, and borrow at 1% and 5% a year. **Both borrow rates are assumptions**; IBKR's rate for ASX 200 shares could not be sourced. Benchmark over the same window: +6.2% a year, worst drawdown −55.1%.

**S1 — short after an earnings miss.** A price-sensitive "results" announcement whose reaction session closes at least 5% below the ASX 200 on 3× volume; short at the next open, cover after 10 sessions, 8% stop tested on the close. A's numbers, sign reversed.

- 245 trades across 100 companies, 11.8 a year (311 events; the rest found the book full).
- Made money after costs on 50.6% of trades at the cheapest setting, 46.5% at the dearest.
- Average trade −0.34% to −0.73%; median hold 10 sessions; 55 stops.
- Hedged with a long ASX 200 position over the same window it does worse, −0.83% to −1.22%: the loss is not the market rising.
- Worst drawdown −46.6% to −54.8%; −1.4% to −2.5% a year.
- The tail: worst trade −28.1% (WOR, Feb 2017, stopped, and the next open gapped through the stop); 5th percentile −12.7%.

**S2 — short ahead of placement shares.** A price-sensitive equity placement; short at the first open after it; cover 5 sessions after the quotation notice (Appendix 2A) when one follows within 10 sessions, else after 10 sessions; 8% stop. Quotation notices exist only from December 2019, so 107 of 445 events were covered by one and the rest by the fixed 10 sessions.

- 249 trades across 101 companies, 12.0 a year. Of the 196 events not traded, 153 were below the turnover floor at entry.
- Made money on 43.4% to 41.4% of trades.
- Average trade −1.52% to −1.88%, about the same hedged (−1.54% to −1.90%); median hold 10 sessions; 58 stops.
- Worst drawdown −66.3% to −72.7%; −4.7% to −5.7% a year.
- The tail: worst trade −35.4% (PDN, Feb 2004); 5th percentile −13.6%. PDN, a small uranium explorer in 2004 and an ASX 200 member today, is four of the five worst trades.

**What this does and does not show.**
- Survivorship runs against these results: the universe is today's ASX 200, the companies that went on to grow, applied to every past year. That is PDN. So the losses are biased against shorting, and neither a loss nor a pass here is conclusive. The remedy is survivorship-free data with point-in-time index membership, run on the same frozen rules, not a new parameter.
- S2's idea came from slicing this same in-sample data (A's capital-raising slice), so it could not have been independent evidence even had it passed. Having lost, it does not earn a holdout run under rule 3.
- For N1: S2 is N1's thesis turned into a short. Inside the ASX 200, placement supply did not push prices down over the following 10 sessions on average. That weakens the veto's premise for large stocks, though it does not test the veto itself, which applies to longs across both universes.

### Baseline — momentum: KEEP AS BASELINE ONLY
Its 24.5% CAGR is survivorship bias (`reports/phase1.md`): on a universe of today's largest companies, momentum buys the stocks that went on to become large.

## Order of work
1. Strategy harness with trial counting.
2. Survivorship-free data: Norgate Platinum (three-week free trial available).
3. Then C, E, N1 (as a veto), G and F, in that order.
4. Shortlist at most three.
5. One holdout run each.

## Sources
- `reports/phase1.md` — Phase 1 backtest, generated 23 Sep 2026 09:51. yfinance, no delisted stocks; ASX 300 proxy universe; in-sample to 22 Sep 2023.
- `reports/shorts_phase1.md` — S1 and S2, generated 23 Sep 2026 14:38. yfinance, no delisted stocks; today's ASX 200 (194 of 200 with an archive and prices); in-sample 2003-01-01 to 22 Sep 2023; borrow at 1% and 5% a year, both assumed. Rules: `config.yaml` `strategies`, commit `3a936e4`.
- `data/logs/asxbot.log` — decider passes on TGN (07:52) and AUE (10:36), 23 Sep 2026.
- McLean, R. D. & Pontiff, J. (2016), "Does Academic Research Destroy Stock Return Predictability?", *Journal of Finance* 71(1).
- Foley, S., Kwan, A., McInish, T. & Philip, R. (2016). Director discretion and insider trading profitability. *Pacific-Basin Finance Journal* 39, 28–43. doi:10.1016/j.pacfin.2016.05.005 — E.
- "Director trades, profitability and market efficiency: New evidence" (2023), an Australian study — E, firm size. Authors and journal not yet recorded.
- Bettman, J. L., Sault, S. J. & von Reibnitz, A. H. (2010). The impact of liquidity and transaction costs on the 52-week high momentum strategy in Australia. *Australian Journal of Management* 35(3), 227–244. doi:10.1177/0312896210385282 — F.
- Chai, D. & Do, B. (2016). Co-existence of short-term reversals and momentum in the Australian equity market. *Australian Journal of Management* 41(1), 55–76. doi:10.1177/0312896214535789 — G. Print issue February 2016, online first 10 Oct 2014 (Crossref).
- Schmidt, C., Zhao, R. & Terry, C. S. (2011). Index Effects: Further Evidence for the S&P/ASX 200. SSRN 1914170 — N2.
