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
- `data/logs/asxbot.log` — decider passes on TGN (07:52) and AUE (10:36), 23 Sep 2026.
- McLean, R. D. & Pontiff, J. (2016), "Does Academic Research Destroy Stock Return Predictability?", *Journal of Finance* 71(1).
- Foley, S., Kwan, A., McInish, T. & Philip, R. (2016). Director discretion and insider trading profitability. *Pacific-Basin Finance Journal* 39, 28–43. doi:10.1016/j.pacfin.2016.05.005 — E.
- "Director trades, profitability and market efficiency: New evidence" (2023), an Australian study — E, firm size. Authors and journal not yet recorded.
- Bettman, J. L., Sault, S. J. & von Reibnitz, A. H. (2010). The impact of liquidity and transaction costs on the 52-week high momentum strategy in Australia. *Australian Journal of Management* 35(3), 227–244. doi:10.1177/0312896210385282 — F.
- Chai, D. & Do, B. (2016). Co-existence of short-term reversals and momentum in the Australian equity market. *Australian Journal of Management* 41(1), 55–76. doi:10.1177/0312896214535789 — G. Print issue February 2016, online first 10 Oct 2014 (Crossref).
- Schmidt, C., Zhao, R. & Terry, C. S. (2011). Index Effects: Further Evidence for the S&P/ASX 200. SSRN 1914170 — N2.
