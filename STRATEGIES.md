# Strategy testing plan
*22 Sep 2026. Extends SPEC.md. Every strategy below gets tested under the rules in this file.*

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

## Strategies
| ID | Strategy | Rule idea | Data needed | Status |
|---|---|---|---|---|
| A | Announcement drift | Price-sensitive announcement + gap ≥5% vs ASX 200 on ≥3× 20-day volume; confirm at close, enter next open; 10-day exit, 8% stop | Archive + prices | Built; rerun once the archive is complete |
| B | Volume-confirmed surprise | Same as A without the announcement filter | Prices | Built; loses after costs (plumbing test) |
| C | Announcement drift by type | A split by the headline classifier: results, guidance upgrades, contract wins, drilling/assay results, takeover-related | Archive + prices | To build |
| D | Trading-halt resumptions | Stock reinstated after a halt that opens up strongly on heavy volume | Archive + prices | To build |
| E | Directors buying | Appendix 3Y notices showing a director's on-market purchase (fetch and parse those PDFs only) | Archive + PDFs + prices | To build |
| F | 52-week-high breakouts | New 52-week high on heavy volume, fixed holding period | Prices | To build; can run now |
| G | Pullbacks in strong stocks | Sharp short-term drop in a stock above its 200-day average; sell on the bounce or after a few days | Prices | To build; can run now |
| Baseline | Momentum | 12-1 month return, top 4, monthly rebalance, 200-day regime filter | Prices | Built; inflated by survivorship on yfinance |

## Order of work
1. Build and run F and G now (price data only).
2. Build C, D and E; run them once the ASX 300 archive has finished. Start the small-universe archive after that.
3. Rerun A with the full archive.
4. Shortlist at most 3 → run once on the holdout → report.
5. Real verdict: rerun the shortlist on Norgate full history.

## Results so far (plumbing test, 22 Sep 2026)
- B loses after costs on both universes, at 1× and 2× slippage.
- A: 43 trades from only 6 archived companies. Too few to judge.
- Baseline: an implausible 28% CAGR, caused by testing on today's top 300 companies (survivorship).
