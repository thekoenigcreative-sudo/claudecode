# ai_base_practice10 vs team_practice10: the same 10 days

Written Sat 26 Sep 2026 14:39. Simulated money on real ASX history (the lab's trading simulator); every figure after brokerage, spread and slippage. Disguised: True / True.

_Survivorship: the universe is the stocks in today's history cache (today's index lists applied to past days) - stocks that later dropped out are missing, which flatters results._

| | ai_base_practice10 | team_practice10 |
|---|---|---|
| net after costs (IBKR) | $-554.29 | $408.26 |
|   made on the simulated days | $-554.28 | $121.23 |
|   across unsimulated gaps (not trading) | $-0.01 | $287.03 |
| closed trades | 14 | 19 |
| win rate | 21% | 32% |
| brokerage | $217.33 | $290.40 |
| max drawdown | 3.41% | 1.72% |
| worst day | $-150.43 | $-214.01 |
| green / red days | 1 / 7 | 4 / 6 |
| t (daily P&L) | -2.11 | 0.60 |
| model calls per day | 34.5 | 166.7 |
| thinking time per day | 265 s | 870 s |
| tokens (all days) | 2,200,522 in (669,990 cached), 195,819 out | 13,316,846 in (137,775 cached), 1,449,392 out |
|   of which Opus | all (single agent) | 2,215,238 in (137,775 cached), 333,954 out |
|   of which Sonnet | - | 11,101,608 in (0 cached), 1,115,438 out |
| API-equivalent cost | $17.59 | $90.14 |

## Day by day

| day | index | ai_base_practice10 | team_practice10 |
|---|---|---|---|
| 2026-04-02 | -1.06% | $126.80 | $-214.01 |
| 2026-04-20 | +0.07% | $-24.59 | $184.30 |
| 2026-04-21 | -0.04% | $-111.33 | $-82.04 |
| 2026-04-27 | -0.23% | $-115.84 | $-122.51 |
| 2026-04-30 | -0.24% | $-80.65 | $-81.71 |
| 2026-05-04 | -0.38% | $-69.64 | $-12.90 |
| 2026-05-06 | +1.30% | $-128.60 | $154.44 |
| 2026-05-27 | +0.69% | $-150.43 | $80.71 |
| 2026-05-29 | +1.61% | $0.00 | $308.72 |
| 2026-06-16 | +0.04% | $0.00 | $-93.77 |

## Plainly

Over 10 days the second did better by $675.51 after costs, on what was made on the simulated days themselves. 10 days is a small sample: this is a first reading, not a verdict (WINNER.md's bar needs 40+ trades on the sealed block and 10+ shadow days).
