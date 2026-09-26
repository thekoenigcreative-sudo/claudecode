# pair2_single vs pair2_team: the same 10 days

Written Sat 26 Sep 2026 17:32. Simulated money on real ASX history (the lab's trading simulator); every figure after brokerage, spread and slippage. Disguised: True / True.

_Survivorship: the universe is the stocks in today's history cache (today's index lists applied to past days) - stocks that later dropped out are missing, which flatters results._

| | pair2_single | pair2_team |
|---|---|---|
| net after costs (IBKR) | $1.44 | $-244.09 |
|   made on the simulated days | $1.43 | $-244.10 |
|   across unsimulated gaps (not trading) | $0.01 | $0.01 |
| closed trades | 19 | 16 |
| win rate | 42% | 31% |
| brokerage | $289.87 | $257.40 |
| max drawdown | 2.06% | 3.04% |
| worst day | $-312.84 | $-158.75 |
| green / red days | 5 / 5 | 3 / 7 |
| t (daily P&L) | 0.00 | -0.51 |
| model calls per day | 37.2 | 178.6 |
| thinking time per day | 297 s | 955 s |
| tokens (all days) | 2,486,575 in (722,424 cached), 227,148 out | 13,836,132 in (153,640 cached), 1,556,123 out |
|   of which Opus | all (single agent) | 2,478,237 in (153,640 cached), 406,917 out |
|   of which Sonnet | - | 11,357,895 in (0 cached), 1,149,206 out |
| API-equivalent cost | $20.28 | $95.37 |

## Day by day

| day | index | pair2_single | pair2_team |
|---|---|---|---|
| 2026-05-22 | +0.41% | $-271.26 | $-131.05 |
| 2026-05-25 | +0.40% | $29.79 | $29.77 |
| 2026-05-26 | -0.39% | $-87.79 | $138.20 |
| 2026-05-27 | +0.69% | $238.71 | $326.70 |
| 2026-05-28 | -1.43% | $379.61 | $-103.16 |
| 2026-05-29 | +1.61% | $43.82 | $-52.77 |
| 2026-06-01 | -0.03% | $-1.33 | $-158.74 |
| 2026-06-02 | -0.06% | $81.87 | $-53.32 |
| 2026-06-03 | +0.70% | $-312.84 | $-98.55 |
| 2026-06-04 | -1.13% | $-99.15 | $-141.18 |

## Plainly

Over 10 days the first did better by $245.53 after costs, on what was made on the simulated days themselves. 10 days is a small sample: this is a first reading, not a verdict (WINNER.md's bar needs 40+ trades on the sealed block and 10+ shadow days).
