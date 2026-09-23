# Short-side backtest: S1 and S2

_Generated 2026-09-23 14:38. Rules: config.yaml `strategies`, frozen 2026-09-23 and committed before this run (3a936e4)._

## DATA LABEL: **PLUMBING TEST - NOT A GO/NO-GO**

- **IN-SAMPLE ONLY**: 2003-01-01 to 2023-09-22. The holdout (the last 3 years) was not simulated.
- **Survivorship**: the universe is TODAY's ASX 200 (200 codes; 194 have an announcement archive and price history, the rest cannot trigger) applied to every past year. Stocks that left the index or were delisted are missing: the survivors are the ones that did well, which works against a short.
- **Borrow fee: both rates are ASSUMPTIONS.** IBKR's rate for ASX 200 shares could not be sourced; 1% and 5% a year bracket it.
- **S2 is not independent evidence.** Its idea came from slicing this same in-sample data (A's capital-raising slice), so passing here proves little. Only the holdout can.
- Costs: A's brokerage (0.088%, $6.60 min) and slippage, adverse both ways, at 1x and 2x; no interest on short proceeds; dividends paid implicitly (adjusted prices).
- Benchmark (buy and hold, same window): CAGR 6.2%, worst drawdown -55.1%.

## S1: short after an earnings miss

In-sample events: 311 across 112 companies (before slots, the turnover floor at entry and the minimum parcel).

| slippage | borrow (assumed) | trades | companies | per year | made money | avg trade | median trade | avg, index-hedged | median hold | worst trade | 5th pct trade | stops | CAGR | worst drawdown | final $ |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|
| 1x | 1% | 245 | 100 | 11.8 | 50.6% | -0.34% | 0.03% | -0.83% | 10 | -28.1% (WOR 2017-02-21) | -12.7% | 55 | -1.4% | -46.6% | 15,020 |
| 1x | 5% | 245 | 100 | 11.8 | 48.2% | -0.49% | -0.15% | -0.98% | 10 | -28.2% (WOR 2017-02-21) | -12.8% | 55 | -1.8% | -48.9% | 13,703 |
| 2x | 1% | 244 | 100 | 11.8 | 47.5% | -0.57% | -0.21% | -1.06% | 10 | -28.3% (WOR 2017-02-21) | -13.0% | 55 | -2.0% | -51.0% | 13,087 |
| 2x | 5% | 243 | 100 | 11.7 | 46.5% | -0.73% | -0.40% | -1.22% | 10 | -28.4% (WOR 2017-02-21) | -13.1% | 55 | -2.5% | -54.8% | 11,933 |

The tail - five worst trades at 1x slippage, 1% borrow (a short's loss has no ceiling):

| ticker | entry | exit | held | trade | index same window | exit reason | headline |
|---|---|---|---:|---:|---:|---|---|
| WOR | 2017-02-21 | 2017-03-01 | 6 | -28.1% | -1.4% | stop | Half Yearly Report and Accounts |
| WHC | 2016-02-08 | 2016-02-19 | 9 | -22.5% | 0.3% | stop | Appendix 4D and Interim Financial Report |
| PLS | 2017-04-19 | 2017-05-03 | 9 | -21.2% | 1.9% | stop | Bulk Sampling Program Results and Project Update |
| TAH | 2017-02-03 | 2017-02-08 | 3 | -19.5% | -0.4% | stop | Appendix 4D and half year results announcement |
| IMD | 2007-08-17 | 2007-08-21 | 2 | -16.8% | 3.9% | stop | Preliminary Final Report |

## S2: short ahead of placement shares

In-sample events: 445 across 117 companies (before slots, the turnover floor at entry and the minimum parcel).

Cover basis: 107 events by a quotation notice, 338 by the fixed 10 sessions (quotation notices exist only from Dec 2019).

| slippage | borrow (assumed) | trades | companies | per year | made money | avg trade | median trade | avg, index-hedged | median hold | worst trade | 5th pct trade | stops | CAGR | worst drawdown | final $ |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|
| 1x | 1% | 249 | 101 | 12.0 | 43.4% | -1.52% | -1.26% | -1.54% | 10 | -35.4% (PDN 2004-02-27) | -13.6% | 58 | -4.7% | -66.3% | 7,390 |
| 1x | 5% | 249 | 101 | 12.0 | 42.6% | -1.68% | -1.42% | -1.70% | 10 | -35.5% (PDN 2004-02-27) | -13.7% | 58 | -5.2% | -69.4% | 6,683 |
| 2x | 1% | 249 | 101 | 12.0 | 42.6% | -1.72% | -1.53% | -1.74% | 10 | -35.7% (PDN 2004-02-27) | -13.7% | 58 | -5.3% | -70.1% | 6,519 |
| 2x | 5% | 249 | 101 | 12.0 | 41.4% | -1.88% | -1.70% | -1.90% | 10 | -35.7% (PDN 2004-02-27) | -13.8% | 58 | -5.7% | -72.7% | 5,906 |

The tail - five worst trades at 1x slippage, 1% borrow (a short's loss has no ceiling):

| ticker | entry | exit | held | trade | index same window | exit reason | headline |
|---|---|---|---:|---:|---:|---|---|
| PDN | 2004-02-27 | 2004-03-02 | 2 | -35.4% | 1.0% | stop | Placement |
| PDN | 2004-09-24 | 2004-09-29 | 3 | -29.3% | -0.2% | stop | $3 million Placement |
| PLS | 2019-03-27 | 2019-03-29 | 2 | -20.2% | 0.7% | stop | Pilbara Completes Ganfeng Equity Placement |
| PDN | 2011-09-28 | 2011-10-10 | 7 | -20.1% | 4.0% | stop | Proposed Institutional Placement of Shares |
| PDN | 2022-04-01 | 2022-04-11 | 6 | -19.0% | -0.3% | stop | Fully Underwritten $200m Placement - Langer Heinrich Restart |

## Reading this

- *made money*: share of trades with a profit after brokerage, slippage and borrow.
- *avg, index-hedged*: each short's return plus the ASX 200's move over the same window (approximately open to open), i.e. the short with a long index hedge. A short that loses only because the market rose shows up here as roughly flat.
- *worst trade* and *5th pct*: the loss tail, per trade, after all costs.
- Trades per year use the in-sample span from the first priced session.
