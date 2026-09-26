# side_high vs side_xhigh: the same 10 days

Written Sat 26 Sep 2026 14:23. Simulated money on real ASX history (the lab's trading simulator); every figure after brokerage, spread and slippage. Disguised: True / True.

_Survivorship: the universe is the stocks in today's history cache (today's index lists applied to past days) - stocks that later dropped out are missing, which flatters results._

| | side_high | side_xhigh |
|---|---|---|
| net after costs (IBKR) | $462.94 | $-980.38 |
|   made on the simulated days | $-406.86 | $-980.40 |
|   across unsimulated gaps (not trading) | $869.80 | $0.02 |
| closed trades | 8 | 17 |
| win rate | 25% | 24% |
| brokerage | $123.45 | $257.39 |
| max drawdown | 2.27% | 4.90% |
| worst day | $-269.32 | $-517.24 |
| green / red days | 2 / 5 | 1 / 8 |
| t (daily P&L) | 0.37 | -1.92 |
| model calls per day | 25.6 | 34.9 |
| thinking time per day | 212 s | 544 s |
| tokens (all days) | 1,634,674 in (499,094 cached), 162,063 out | 2,413,050 in (675,816 cached), 442,996 out |
|   of which Opus | all (single agent) | all (single agent) |
|   of which Sonnet | - | - |
| API-equivalent cost | $13.38 | $24.36 |

## Day by day

| day | index | side_high | side_xhigh |
|---|---|---|---|
| 2026-04-08 | +2.56% | $-54.49 | $-517.24 |
| 2026-04-10 | -0.14% | $-149.81 | $-0.97 |
| 2026-04-22 | -1.18% | $0.00 | $30.88 |
| 2026-05-01 | +0.74% | $0.00 | $-59.25 |
| 2026-05-04 | -0.38% | $-86.71 | $-44.26 |
| 2026-05-05 | -0.19% | $-133.65 | $-4.67 |
| 2026-05-15 | -0.12% | $0.00 | $-97.37 |
| 2026-05-25 | +0.40% | $44.56 | $-203.19 |
| 2026-06-16 | +0.04% | $64.43 | $-84.33 |
| 2026-06-25 | -0.68% | $-91.19 | $0.00 |

## Plainly

Over 10 days the first did better by $573.54 after costs, on what was made on the simulated days themselves. 10 days is a small sample: this is a first reading, not a verdict (WINNER.md's bar needs 40+ trades on the sealed block and 10+ shadow days).
