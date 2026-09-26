# WINNER: when the Practice Lab may ask Rick "trial it with real money?"

Written 26 Sep 2026, BEFORE the search started (no variant had been run in the lab; the git
commit that adds this file is the proof). Changing any number here after a result has been
seen is forbidden (CLAUDE.md "Verification"); a change is a new dated section below, with the
reason, and it applies only to candidates first run after it. The code copy is
`src/asxbot/lab/winner.py` (`CRITERIA`); a test fails if the two disagree.

A **candidate** is one variant's book of one playbook (the rule bot's book or the agent's
book of the day trader, or of announcements v2), run by the Practice Lab through the live
code: the same scanner, limits, costs, slippage, volume-capped fills, stops, trailing and the
flat-by-close sweep (PRACTICE_LAB.md). Money is fake at every step; nothing real happens
without Rick's "yes".

## The data windows (fixed)

| Window | Sessions | Used for |
|---|---|---|
| TUNE | 2026-03-26 to 2026-06-30 | proposing and screening variants (before the models' knowledge cutoff: the agent sees it ANONYMISED only) |
| VALIDATE | 2026-07-01 to 2026-08-14 | the gate to shadow trading (after the cutoff: seen as it was) |
| LOCKED TEST | 2026-08-17 to 2026-09-25 | sealed: run ONCE per final candidate, never for tuning or screening |
| SHADOW | each trading day from the day after promotion | forward trading on days nobody has seen |

The models' knowledge cutoff is taken as 2026-07-01 (Opus 5.5: June 2026). The primary score
is always on data after it.

## All of these, or it is not a winner

1. **Positive after costs on the locked test**: P&L after brokerage and slippage > $0 on the
   locked test (the $20,000 day books chained), in its single locked run.
2. **Positive after costs in shadow trading**: at least **10 shadow trading days** and at
   least **15 shadow trades**, P&L after costs > $0.
3. **Enough trades to matter**: at least **40 trades** in the locked test.
4. **Not carried by its top few trades**: the locked-test P&L without its best **3** trades is
   still > $0, and those 3 trades are at most **50%** of its gross profit. The same holds in
   shadow if it has 30 or more shadow trades.
5. **Holds in up and down market days**: in the locked test, P&L >= $0 on the days the ASX 200
   rose AND >= $0 on the days it fell, with at least 5 days of each.
6. **Beats the rule-bot baseline**: its P&L exceeds the frozen rule bot of the same playbook
   (day trader v1 / announcements v2 as frozen 2026-09-24) over the same locked-test days AND
   over the same shadow days.
7. **Reasonable worst drawdown**: worst peak-to-trough drawdown on the chained locked-test
   curve at most **10%** of $20,000, and no single day worse than **-3%** ($-600), in the
   locked test and in shadow.
8. **More green days than red** in the locked test.
9. **Not luck from trying many things**: the locked-test daily-P&L t-statistic is at least
   **1.65 + 0.5 x ln(F)**, where F is the number of final candidates ever run on the locked
   test (the first: 1.65). The validation gate below already charges for every variant tried.
10. **Not knowledge of the past**: all its locked-test and shadow days are after the cutoff
    (automatic), and for an agent candidate, anonymising its validation days did not flip the
    sign of its validation P&L (PRACTICE_LAB.md "Contamination").
11. **Simple enough**: at most **6** changed parameters and at most **600** characters added
    to the agent's instructions.

## The gates before that (so the locked test is seen rarely)

- **Screen (TUNE)**: P&L after costs > $0, at least 20 trades, and better than the frozen rule
  bot on the same days. Pass -> run on VALIDATE.
- **Validate (VALIDATE)**: P&L after costs > $0, at least 20 trades, beats the frozen rule bot,
  worst day >= -3%, and a daily-P&L t-statistic of at least **sqrt(2 x ln(N))** (N = every
  variant ever validated, so each extra try raises the bar; at least 1.0). Pass -> promoted to
  SHADOW automatically from the next trading day (paper only).
- **Final**: after 10 shadow days with shadow P&L > $0, the candidate is frozen and gets its one
  locked-test run. If criteria 1-11 all hold, Rick gets ONE message with the evidence and the
  question "trial it with real money?". A candidate that fails the locked test is retired for
  good and listed in the report.

Every variant ever tried is counted and listed with its results, winners and losers
(STRATEGIES.md). A real-money trial, if Rick says yes, starts one ladder level below the
level that earned it (ARENA.md) and is a separate build with his approval on every order.
