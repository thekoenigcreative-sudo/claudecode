# Research log: the never-ending strategy search

Updated Sat 26 Sep 2026 11:42. Ideas tried: **0**. Practice bar now t >= 0.59; check bar t >= 1.18; sealed block 2026-08-17 to 2026-09-25 (used by 0 of 3 finalists). Lab usage this week: 0.0% of the weekly allowance, 78 calls.

_Survivorship: the universe is the stocks in today's history cache (today's index lists applied to past days) - stocks that later dropped out are missing, which flatters results._

All money is simulated on stored history; every figure is after brokerage, spread and slippage (IBKR's fees; other brokers in each run's scores).

## Yardsticks (practice window, rules only)

| yardstick | days | trades | net | t | old rules same days |
|---|---|---|---|---|---|
| A: stocks in play ORB | 66 | 0 | $0 | 0.00 | $-4,607 |
| A trail: ORB, 3% trailing stop | 66 | 0 | $0 | 0.00 | $-4,607 |
| A+D regime: ORB, index direction | 66 | 0 | $0 | 0.00 | $-4,607 |
| A+D first30: ORB, first-30-min direction | 66 | 0 | $0 | 0.00 | $-4,607 |
| A+D closing volume cap 20% | 66 | 0 | $0 | 0.00 | $-4,607 |
| C rules: announcement drift 5 days | 66 | 0 | $0 | 0.00 | $-4,607 |
| C rules + D regime: drift, index direction | 66 | 0 | $0 | 0.00 | $-4,607 |

## Every idea

| id | what | why it might work | practice | check | sealed | verdict |
|---|---|---|---|---|---|---|
