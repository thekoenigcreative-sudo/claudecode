# side_high vs side_B: the same 10 days

Written Sat 26 Sep 2026 14:57. Simulated money on real ASX history (the lab's trading simulator); every figure after brokerage, spread and slippage. Disguised: True / True.

_Survivorship: the universe is the stocks in today's history cache (today's index lists applied to past days) - stocks that later dropped out are missing, which flatters results._

| | side_high | side_B |
|---|---|---|
| net after costs (IBKR) | $462.94 | $-348.99 |
|   made on the simulated days | $-406.86 | $-348.98 |
|   across unsimulated gaps (not trading) | $869.80 | $-0.01 |
| closed trades | 8 | 11 |
| win rate | 25% | 18% |
| brokerage | $123.45 | $187.02 |
| max drawdown | 2.27% | 2.22% |
| worst day | $-269.32 | $-178.80 |
| green / red days | 2 / 5 | 1 / 6 |
| t (daily P&L) | 0.37 | -1.48 |
| model calls per day | 25.6 | 19.6 |
| thinking time per day | 212 s | 170 s |
| tokens (all days) | 1,634,674 in (499,094 cached), 162,063 out | 1,315,942 in (408,525 cached), 137,245 out |
|   of which Opus | all (single agent) | all (single agent) |
|   of which Sonnet | - | - |
| API-equivalent cost | $13.38 | $10.85 |

## Day by day

| day | index | side_high | side_B |
|---|---|---|---|
| 2026-04-08 | +2.56% | $-54.49 | $0.00 |
| 2026-04-10 | -0.14% | $-149.81 | $95.48 |
| 2026-04-22 | -1.18% | $0.00 | $-45.77 |
| 2026-05-01 | +0.74% | $0.00 | $0.00 |
| 2026-05-04 | -0.38% | $-86.71 | $-6.64 |
| 2026-05-05 | -0.19% | $-133.65 | $-73.70 |
| 2026-05-15 | -0.12% | $0.00 | $-178.80 |
| 2026-05-25 | +0.40% | $44.56 | $0.00 |
| 2026-06-16 | +0.04% | $64.43 | $-27.58 |
| 2026-06-25 | -0.68% | $-91.19 | $-111.97 |

## Plainly

Over 10 days the second did better by $57.88 after costs, on what was made on the simulated days themselves. 10 days is a small sample: this is a first reading, not a verdict (WINNER.md's bar needs 40+ trades on the sealed block and 10+ shadow days).
