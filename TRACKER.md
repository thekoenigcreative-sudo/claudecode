# Tracker — ASX bot

Living plan. Reviewed and updated by Claude every time a Claude Code output comes back,
before the next prompt is handed over. Every line here is either verified against a file,
a log or a command output, or labelled as unverified.

**Last reviewed:** 2026-09-23 11:58 AEST
**Visual version:** https://claude.ai/artifact/Pg5TsxMqKkbbwz2aFF3UqZ — republished to the
same link whenever this file changes.

---

## Where we actually are

| Area | State | Verified how |
|---|---|---|
| Announcement collector | Working. Real PDFs since 09:12 | `%PDF` magic on disk |
| Screen | Working; `max_tick_pct` 3.0, turnover floor $250k | config.yaml |
| Reader / decider | Sonnet 5 low / Opus 5 high, confirmed per call | `agent_mismatch` self-check |
| Broker, orders, fills, stops | Sound — read in full | `broker.py`, `orders.py`, `minutes.py` |
| Self-checks | 7 checks every cycle, loud on failure | `selfcheck.py`, live log |
| ASX 200 short universe | Real constituent list, 200 codes, dated | `asxbot universe asx200` |
| Arena position | A1M 3,000 @ 0.8308, stop 0.755 | account file |
| Strategy with an edge | **None** | reports/phase1.md |
| Trustworthy data | **No** — survivorship-biased | yfinance, current listings only |

---

## The gate

**Norgate (or equivalent survivorship-free data).** Every strategy number in this repo is
biased upward until price history includes delisted stocks. The momentum baseline's
24.5% CAGR is that bias in plain sight. Nothing is a go or no-go before this.

Status: **decision for Rick** — a purchase.

---

## Strategy register

Planning discount: published anomalies return ~58% less after publication (McLean &
Pontiff, 97 predictors). Assume under half of any paper's figure.

| ID | Strategy | Evidence | Verdict | Status | Next action |
|---|---|---|---|---|---|
| A | Gap + volume + announcement | Dead on 492 trades, 155 cos; misspecified — tests gap continuation, not drift | **Drop** | Running live as machinery test only | Retire once F/G/C exist |
| B | Volume-confirmed surprise | Loses after costs | **Drop** | Built | None |
| C | Earnings drift, done properly | PEAD confirmed in Australia but weakened; results only positive slice (+0.90%, 181 trades) | **Keep, rebuild** | Not built | Measure surprise vs prior guidance in the archive, 30–60 day hold |
| D | Trading-halt resumptions | None found | Keep, low | Screen now lets reinstatements through? **unverified** | Check screen, then backtest |
| E | Directors buying | Discretionary +4.6% / 200d; non-discretionary −4.7%; larger firms | **Upgrade** | Not built; PDFs now work | Reader classifies 3Y as discretionary on-market; long horizon |
| F | 52-week-high breakout | Tested on ASX: not practical after costs and short limits | **Downgrade** | Not built | Cheap test only; expect failure |
| G | Pullback in strong stock | Not researched yet | Keep, untested | Not built | Research, then test |
| N1 | Placement overhang | No resale limits in AU; capital_raising −1.30%, 34.5% win; decider reasons about it unprompted | **New, promising as a veto** | Not built | As a filter on other strategies first |
| N2 | Index deletion reversal | ASX 200 deletions reverse after announcement (2011); likely decayed | New, low | Not built | Needs rebalance history since 2011 |
| Base | 12-1 momentum | Real anomaly; 24.5% here is survivorship | Baseline only | Built | Rerun on Norgate |

---

## Open defects

| # | Defect | Severity | Where it's queued |
|---|---|---|---|
| 1 | Live bot enters intraday; frozen rule is enter-next-open | High | PROMPTS #2 |
| 2 | Arena sizing ≠ backtest ≠ real-money limits | High | PROMPTS #2 |
| 3 | `target` stored on positions but never acted on — agent believes it set a take-profit | Medium | **Not yet queued** |
| 4 | ASX 300 universe is a market-cap proxy, like the ASX 200 was | Medium | **Not yet queued** |
| 5 | `universe.asx300_source: vas_holdings` never read | Low | **Not yet queued** |
| 6 | Stop can fire in the entry minute | Medium | PROMPTS #4 |
| 7 | Screen rejects reinstatements (kills D) | Medium | PROMPTS #4 — verify if already fixed |
| 8 | Win rate before costs; raw P&L comparison; trades count legs | Medium | PROMPTS #5 |
| 9 | Decider asserts stale world facts confidently (NUF "index member") | Medium | **Not yet queued** |

---

## Milestones

1. Machinery works and checks itself — **done 23 Sep**
2. First honest strategy result — **done 23 Sep** (A dead)
3. Arena measures fairly — sizing, entry rule, scoring — *in progress*
4. Strategy harness with trial counting — *queued*
5. Survivorship-free data — *gate, Rick's decision*
6. C, E, F, G, N1 tested in-sample on clean data
7. Shortlist ≤3, each run once on the 3-year holdout
8. IBKR live data, formal 10-day paper test
9. Real money — only after 7 and 8

ETF track runs separately: research and monitoring only, no broker, Rick executes.

---

## Review log

- **23 Sep 11:55** — Created. Strategy register rewritten after research: A and B dropped,
  E upgraded, F downgraded on direct ASX evidence, N1 and N2 added. Four defects found
  on review and not yet queued (#3, #4, #5, #9).
