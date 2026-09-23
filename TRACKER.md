# Tracker — ASX bot

Living plan. Reviewed and updated by Claude every time a Claude Code output comes back,
before the next prompt is handed over. Every line here is either verified against a file,
a log or a command output, or labelled as unverified.

**Last reviewed:** 2026-09-23 20:45 AEST (Claude Code, fill-realism job, headless)
**Visual version:** https://claude.ai/artifact/Pg5TsxMqKkbbwz2aFF3UqZ — republished to the
same link whenever this file changes.

---

## Where we actually are

| Area | State | Verified how |
|---|---|---|
| Announcement collector | Working. Real PDFs since 09:12 | `%PDF` magic on disk |
| Screen | Working; `max_tick_pct` 3.0, turnover floor $250k | config.yaml |
| Reader / decider | Sonnet 5 medium / Opus 5.5 high (from the evening of 23 Sep; Sonnet 5 low / Opus 5 high before), confirmed per call | `agent_mismatch` self-check |
| Broker, orders, fills, stops | Fills volume-aware since 24 Sep (20% of a bar); bars used only once final | `broker.work`, `minutes.final_bars`, tests/test_fill_volume.py |
| Self-checks | 7 checks every cycle, loud on failure | `selfcheck.py`, live log |
| ASX 200 short universe | Real constituent list, 200 codes, dated | `asxbot universe asx200` |
| Arena position | None open. A1M bought 10:41 (ARN-000002, 0.8358 after the #24 correction, applied 19:40) and sold by its 0.90 target (ARN-000003). ARN-000003 is still booked at 0.9161 in the 1-share 15:57 bar; `scripts/correct_fill_arn000003.py` (dry-run only) re-prices it over 15:58-16:10 at 0.9159, cash and realised -0.42 (240.71 -> 240.29). Both books topped up to $20,000 starting cash at 19:40 | account files, tonight.log, dry-run |
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
| E | Directors buying | Foley et al. 2016: discretionary +4.6% / 200d, non-discretionary −4.7%, strongest for purchases and larger trades. **Firm size conflicts**: Foley finds small firms earn more; a 2023 study finds larger firms. Foley classified 60,000 trades by the director's stated motive — what the reader would do | **Upgrade** | Not built; PDFs now work | Reader classifies 3Y by stated motive; test small and large firms separately; long horizon |
| F | 52-week-high breakout | Bettman, Sault & von Reibnitz 2010: not practical on ASX after costs and short limits | **Downgrade** | Not built | Cheap test only; expect failure |
| G | Industry-relative pullback | Chai & Do: in AU small stocks reverse (illiquidity), large stocks trend; large stocks reverse within their industry once momentum is controlled | **Reframed** | Not built | Large liquid stock down vs industry peers over a month |
| N1 | Placement overhang | No resale limits in AU; capital_raising −1.30%, 34.5% win; decider reasons about it unprompted | **New, promising as a veto** | Not built | As a filter on other strategies first |
| N2 | Index deletion reversal | ASX 200 deletions reverse after announcement (2011); likely decayed | New, low | Not built | Needs rebalance history since 2011 |
| N3 | Merger arbitrage, cash takeovers | Mitchell & Pulvino 2001: ~4%/yr excess after costs; failed-deal losses far exceed gains; wider spreads earn more despite more failures. US evidence | **New** | Not built | Reader judges completion odds from scheme booklet; decider did this on GL1 23 Sep |
| N4 | Buyback with stated motive | Ikenberry et al. 1995: +12.1% over 4 yrs, value stocks +45.3%, glamour none; later study: none after costs. AU: undervaluation-motivated buybacks +1.25% on the day | New, contested | Not built | Reader classifies the stated motive |
| N5 | June tax-loss rebound | Brown, Ferguson & Sherry 2010: June tax-loss selling of losers, July rebound, small miners targeted (1994–2007). Proposed CGT changes may weaken it | New, annual | Not built | Next window June 2027 |
| N6 | Short-interest veto | Short-selling risk predicts lower AU returns outside the top 200 only (limits to arbitrage). ASIC data free daily, T+4 | New, as veto | Not built | Filter on small-cap longs |
| Base | 12-1 momentum | Real anomaly; 24.5% here is survivorship | Baseline only | Built | Rerun on Norgate |

---

## Open defects

| # | Defect | Severity | Where it's queued |
|---|---|---|---|
| 1 | ~~Live bot enters intraday; frozen rule is enter-next-open~~ | — | **Fixed `a7665ac`** — confirms at close, enters next open before 10:00; loads 07:30 24 Sep |
| 2 | ~~Arena sizing ≠ backtest ≠ real-money limits~~ | — | **Fixed `150297c`** — $20k, 4 positions, 1.0x, $5k max position |
| 3 | ~~`target` stored on positions but never acted on — agent believes it set a take-profit~~ | — | **Fixed `42364dc` + this job** — a resting take-profit, agent accounts only, less slippage like every fill. 42364dc's rule for a position already past its target (sell at the first open whatever it is) was a spec error, corrected: it sells at an open only if that open is at or beyond the target, otherwise holds until a bar reaches it. A1M (target 0.90): sells at the 24 Sep open only if that open is ≥ 0.90 |
| 4 | ASX 300 universe is a market-cap proxy, like the ASX 200 was | Medium | **Not yet queued** |
| 5 | `universe.asx300_source: vas_holdings` never read | Low | **Not yet queued** |
| 6 | Stop can fire in the entry minute | Medium | PROMPTS #4 |
| 7 | Screen rejects reinstatements (kills D) | Medium | PROMPTS #4 — verify if already fixed |
| 8 | Win rate before costs; raw P&L comparison; trades count legs | Medium | PROMPTS #5 |
| 9 | ~~Decider asserts stale world facts confidently (NUF "index member")~~ | — | **Fixed 24 Sep** - the decider packet carries an INDEX MEMBERSHIP block (the list, its source and `as_of` date, IN/NOT in), and says it is authoritative and not to be overruled from memory; a doubt goes in `flag_for_claude` (logged, `arena_flags` events, in the report facts), never in the reasoning. The report brief carries the same rule and the date. Decider AGENTS.md updated, repo and live copies (identical but for line endings). NUF is correctly outside the list (as_of 2026-09-23) |
| 10 | ~~Decider can't see its own pending orders; duplicate opening order allowed~~ | — | **Fixed `22b2f1a`**; loads at 07:30 24 Sep restart |
| 11 | ~~STRATEGIES.md E firm-size error~~ | — | **Fixed `6bfbb8d`** |
| 12 | ~~ARENA.md setup 8 old G~~ | — | **Fixed `6bfbb8d`** |
| 13 | ~~Pending orders don't count toward max open positions or the leverage cap~~ | — | **Fixed `a7665ac`** |
| 14 | STRATEGIES.md order of work still puts survivorship-free data second; decided 23 Sep to defer it until a strategy survives free data. N3–N6 not yet in STRATEGIES.md | Low | **Not yet queued** |
| 15 | Setup 8 pullback exit ("first up close or 5 days") was written for 3-day dips; may not suit a one-month reversal signal. Decide when G's rule is frozen, not before | Low | When G is built |
| 16 | Yahoo's ^AXJO series was missing 22 Sep 2026 on 23 Sep. The yardstick now needs the index's daily bar each morning; a late bar means a missed day (logged, never traded late). Watch 24 Sep 07:30 | Medium | Watch, then decide |
| 17 | Not yet confirmed what the $20k change did to the open arena accounts (A1M) — the session's own report wasn't reviewed | Medium | Check before 07:30 24 Sep |
| 18 | ~~Stop scan looked only five calendar days from entry, so a stop went dead on any position held past the first week (the yardstick holds ten sessions)~~ | — | **Found and fixed `42364dc`** — scans through today |
| 19 | **The watcher died at 13:32:51 on 23 Sep** and nothing said so. Log stops mid-poll, no error; task result 0xC000013A (console closed / Ctrl+C). It ran in a visible console window, almost certainly closed by accident while other terminals were opened. The self-checks run inside the watcher, so they died with it. No polling, alerts or pre-close sweep after 13:32 (A1M went overnight without the sweep) | High | Fixed in code, **waiting on Rick** — hidden launcher `scripts/arena_warmup.pyw`; `scripts/schedule_watcher_hidden.ps1` re-points the task, not yet run. Until then 07:30 still starts it in a visible window |
| 20 | No outside check that the watcher is alive | High | Built, **waiting on Rick** — `watchdog.py` + heartbeat, run every 5 min by `ASXBot Arena Watchdog` (`scripts/schedule_watchdog.ps1`, not yet run). One Telegram alert per outage, one on recovery |
| 21 | ~~`.gitignore`'s `data/` also hid `src/asxbot/data` — 8 modules never committed, a clone would not import~~ | — | **Fixed `76c2036`** — pattern is now `/data/` |
| 22 | The 22 Sep marks are in `events/arena_marks.jsonl` but `data/arena/marks/` is empty. Not a bug: `arena reset --yes` at 21:40 22 Sep (the deliberate cleanup in HANDOVER.md) deleted the marks files for test accounts that no longer exist; the event log is append-only and keeps them. No real account ever had a 22 Sep mark. `reset` writes nothing to the event log, so the log reads as if those marks still apply | Low | Optional: have `reset` log an event |
| 23 | The evening task (19:30/20:30: settle, mark, report) also ran in a visible console window; closing it would lose the day's settlement and the report. The watcher's hidden launcher did not flush printed output, so a killed run lost it | High | **Fixed and applied 23 Sep 16:01** - `scripts/arena_evening.pyw`; `scripts/schedule_evening_hidden.ps1` re-pointed the task (read back: only the action changed; backup in `data/task_backups/`). Both launchers now run unbuffered. First real run 19:30 23 Sep, **not yet verified** |
| 24 | **Fills priced from before the decision.** The watcher took one `now` at the top of each cycle and passed it to every order placed in that cycle, as the decision time; the fill rule then took the bar containing that minute. On 23 Sep the cycle that began at 10:29:46 (the watcher's restart) worked eight re-looks, a Sonnet and an Opus call each; A1M's decider replied at 10:37:41 and ARN-000002 was recorded then, stamped 10:29:46, and filled at the close of the 10:29 bar (0.8308) - eight minutes before it existed. Not the data time, as first suspected: the log shows 10:29:46 is the cycle start. Affected every order placed after anything slow in the same cycle (agent, re-looks, pre-close, yardstick after an announcement), and every fill (the decision minute's own bar) | High | **Fixed, this job** - the broker stamps `decided_at` from its own clock when it records the order; the caller's time is kept as `data_as_of`; fills take the first traded bar starting strictly after `decided_at`, pending until the feed holds it; the watcher reads the clock afresh per stage and per announcement; stop/target exits record `rests_from`; a resting limit no longer expires before the delayed feed reaches the close; the yardstick records a miss if its orders reach the broker after 10:00; new self-check `fill_before_order`. 15 tests fail on the old code. Loads at the 07:30 24 Sep start. ARN-000002 re-priced by `scripts/correct_fill_arn000002.py` (written, dry-run only, **not run**): 10:41 bar, 0.8358; cash and realised -15.02 (255.72 -> 240.71). Backtests checked: no equivalent look-ahead (review log) |
| 25 | ~~Fills ignored bar volume: ARN-000003 sold 3,000 A1M in the 15:57 bar, where 1 share traded~~ | — | **Fixed 24 Sep** - no bar fills more than `arena.fill.max_volume_share` (20%, dated in config) of its volume, the rest carries to later bars at their prices. Per account, per ticker, per bar; a stop exit takes the volume first, then a target exit, then the rest by age. A day order part-filled at the end of its session ends `partial` (the rest expires; the position keeps what filled); a stop or target exit keeps working into the next session until the position is out. A stop reached while a target is filling cancels the target's rest. Brokerage minimum once per order per day. Stops, targets and entries in one ticker are worked in one bar-by-bar pass (`broker.work`). ARN-000003 re-pricing: `scripts/correct_fill_arn000003.py`, dry-run only, **Claude runs it before 07:30** |
| 26 | ~~Possible: the newest bar in the delayed feed may still be forming~~ | — | **Checked and ruled 24 Sep** - no completed bar changed (A1M, BHP, FMG, DUG cache vs fresh fetch; 180 polls of six London/Frankfurt stocks in session). Only the newest row changes: Yahoo's zero-volume placeholder for the minute forming. Rule (`minutes.final_bars`): a bar must have traded, have ended, and not be the newest row of an intraday fetch; `settle_minutes` 0 in config. ASX bars **not** polled during a session - worth a poll on 24 Sep. The two A1M "10:29" prices were two delayed quotes ~1m40s apart (10:15 and 10:17 closes), both stamped with the stale cycle clock (#24), not a revised bar |
| 27 | Yahoo's `^AXJO` daily open equals the prior close on most days (0.98 of days in 2015, 0.93 in 2026), so "gap vs the index" was in effect a raw gap | Medium | **Live fixed 24 Sep; frozen results untouched.** The live yardstick takes the index open from the ^AXJO 10:00 minute bar (config `yardstick.index_open: first_minute_bar`, dated). 14-23 Sep against the cap-weighted opening gap of ~195 members: mean error 0.12 points (10:00 bar), 0.17-0.23 (ETF opens), 0.40 (Yahoo daily). No 10:00 bar = NotReady, then a recorded miss; never a fallback. **Affected, not rerun:** A and B in reports/phase1.md and phase1_20k.md (both use `detect_events`' gap vs index): in the years the index open is the prior close, their trigger was a raw 5% gap - admitting stocks riding a market-wide up-gap, and missing some on down-gap days. The size of the effect is unknown and cannot be measured on free data (Yahoo keeps ~7 days of 1-minute history). **Not affected:** momentum baseline (closes), S1 (close-to-close vs index), S2 (no index), filter_cost (closes), the arena's scoreboard. A's live yardstick and its frozen backtest now measure the index leg differently: compare them with that in mind |
| 28 | 1-minute data leaves out the opening auction: the first bar of an ASX day usually shows volume 0 (BHP 15, 16, 21, 23 Sep; CBA 6 of 7 days; A1M's 10:14 resumption) and the 1m volumes sum short of the daily (A1M 9.62m vs 9.73m). So no fill ever uses the auction print: an order "at the open" fills at the first traded minute after it, at that bar's close and within 20% of its volume. Permanent in the feed, not a revision | Medium | **Not yet queued**; a decision on how to price auction fills |
| 29 | `fast_info.previous_close` moves once the day's first bar appears (A1M 0.80 at 10:23, 0.795 at 10:36), so the re-look packet's "% vs prior close" has no stable base | Low | **Not yet queued** |
| 30 | CMM, NUF and TUA were each re-looked twice on 23 Sep (10:20 and 10:29-10:33); the rule is one each. The 10:29:46 restart is the likely cause. Unverified | Low | **Not yet queued** |
| 31 | `ASXBot Arena Watchdog` task is **Disabled** (last run 19:45 23 Sep, next run N/A). Nothing in tonight's job disabled it. If it stays off, nothing outside the watcher watches the 24 Sep session | High | **Rick** - re-enable, or say why it is off |

---

## Milestones

1. Machinery works and checks itself — **done 23 Sep**
2. First honest strategy result — **done 23 Sep** (A dead)
3. Arena measures fairly — sizing and entry rule **done 23 Sep**; scoring (PROMPTS #5) still to do
4. Strategy harness with trial counting — *queued*
5. Survivorship-free data — *gate, Rick's decision*
6. C, E, F, G, N1 tested in-sample on clean data
7. Shortlist ≤3, each run once on the 3-year holdout
8. IBKR live data, formal 10-day paper test
9. Real money — only after 7 and 8

ETF track runs separately: research and monitoring only, no broker, Rick executes.

---

## Review log

- **23 Sep 20:45** - Headless job after the evening routine. **Tonight's steps, checked in
  tonight.log and the files:** evening report 19:30 (all three steps exit 0, sent); ARN-000002
  correction applied 19:40 (backup `correct_arn000002_20260923_194005`); $10,000 added to both
  books 19:40 (backup `add_capital_20260923_194010`). Account files read back: agent cash
  20,227.51, bot 20,000.00, both starting 20,000.00, marks restated. The scout-browser step
  (bet-bot, not ASX) FAILED its verify, 4 of 4 checks. **This job:** fills are volume-aware
  (#25); the bar-finality rule (#26); the live yardstick's index open (#27); dated membership
  in the decider's and the report's packets (#9); PDFs missed once are fetched before the
  reader, and every failure carries a reason in the self-check (#8: of 23 Sep's 93
  price-sensitive in-universe announcements, 49 had a real PDF; the new code fetched the
  other 44 of 44 into a scratch folder in 88 requests, no failures - so no fetch ever failed,
  the 44 were deleted at 09:12 and never fetched again); the session summary once a day (the
  hourly digest was erasing its marker; sent 16:10, 16:56, 17:56, 18:57); a late watcher
  start is a WARNING with its reason, not an ERROR; every child process through
  `asxbot.proc` (CREATE_NO_WINDOW), with a scan test; the flaky launcher test fixed (kill
  order). `scripts/correct_fill_arn000003.py` dry-run against the live books (hashes
  unchanged): 15:58 7, 15:59 445, 16:00 1,432, 16:10 1,116 shares, 0.9161 -> 0.9159, cash and
  realised -0.42. Not run. 299 passed x3, ruff check clean; load check (config, 66 modules,
  both books) read-only, hashes unchanged. On ceefbfe: 17 of the 19 volume tests fail on
  behaviour, and the other two pass by design (separate accounts; the stop measured from the
  average fill). The subprocess scan fails. The #4, #5, #7, #8 and #27 tests fail, as does
  every ARN-000003 script test (the module did not exist). One new test passes on ceefbfe
  by design (the decision parser passes `flag_for_claude` through). New: #28-#31.
- **23 Sep 17:00** - Fill-timing look-ahead (#24) found from ARN-000002 and fixed for every order path; 251 tests pass, 15 of the new or changed ones fail on the old code. `correct_fill_arn000002.py` written and dry-run against the live books (read-only, hashes unchanged): it would set the 10:41 bar, 0.8358, cash and realised -15.02. **Not run** - it refuses while the watcher is up. Tonight, in this order: watcher stops 19:25 -> evening routine -> correction -> `arena_add_capital.py` (tested to work after it). Tonight's evening report and Telegram will show the uncorrected 255.72; the correction restates the day's mark. Backtests checked for the same flaw (read-only, sub-agent, key lines re-read by hand): none. A, B, S1 enter at the next open after the signal is complete (`engine.py:84`, entry_lag=1); announcements at or after 10:00 go to the next session (`signals.py:115`); momentum ranks on closes to t-22 and trades the open. Labelled upper bounds (A same-day, S2 entry_lag=0) rely on release times: releases with no time are parsed as 00:00 (`parser.py:41`), which would put them pre-open - reported as 2,863 archive rows from 2002-03 and no trade using one (sub-agent's count, not re-verified). Also found, and checked by hand on `data/prices/yfinance/^AXJO.parquet`: the index's open equals the prior close on 45-98% of days in 2012-20 and 89-93% in 2024-26, so A's gap "vs index" is mostly a raw gap in those years (#27). New: #25 (fill size ignores bar volume - A1M's target filled 3,000 on a 1-share bar), #26, #27.
- **23 Sep 15:50** — Watcher death at 13:32 diagnosed (#19): closed console window,
  0xC000013A. Built a windowless launcher, a heartbeat inside the watcher and a watchdog
  outside it (#20); both scheduled-task changes are written as scripts and **not run**,
  waiting on Rick. #3 corrected: a target is a resting take-profit that pays slippage;
  a position past its target sells at an open only if that open reaches it. The marks
  folder is empty because of the 22 Sep reset, not a bug (#22). Found and fixed
  `.gitignore` hiding `src/asxbot/data` (#21). 225 tests pass.

- **23 Sep 14:50** — Short side tested (`d009048` rules committed first, `c5f3711`). Both
  lose in-sample inside today's ASX 200: S1 earnings-miss short avg −0.34% to −0.73% a
  trade; S2 placement short avg −1.52% to −1.88%, worst trade −35%. Survivorship runs
  against shorts (today's ASX 200 is the set that grew), so not conclusive — the remedy is
  point-in-time membership, not tuning. S2 does not earn a holdout run. From now on Claude
  runs Claude Code jobs headless (`C:\Users\Richa\.cc-jobs`), Telegram ping on finish.
- **23 Sep 14:25** — $20k job landed (`150297c`, `a7665ac`, `f516bc3`; 166 pass). At
  $20k A improves (1x: avg +0.37%, CAGR +1.4%; 2x: +0.08%, −0.3%) but still earns about a
  fifth of the 6.9% benchmark on survivorship-inflated data — still dropped. Yardstick
  now runs the frozen rule; pending orders count toward limits. New: #16 (Yahoo index
  bar missing), #17 (open accounts after the change, unchecked). Short-side strategies
  (S1 earnings miss, S2 placement) sent for spec and in-sample test.
- **23 Sep 12:45** — `22b2f1a` pending-orders fix (162 tests pass; both new tests fail on
  old code) and `6bfbb8d` docs. Running watcher keeps old code; `ASXBot Arena Warmup`
  restarts it 07:30 24 Sep, so no mid-session restart with A1M open. Claude Code flagged
  three gaps, logged as #13–#15. N3–N6 researched and added to the register above.
- **23 Sep 12:26** — STRATEGIES.md rewritten and committed (`2407016`). Chasing its
  missing citations found an error I introduced: E's "stronger in larger firms" merged
  two studies that disagree. Foley et al. 2016 (the source of the +4.6%/−4.7%) finds
  small-firm purchases earn more. Added defects #10–#12. G now researched. Norgate
  deferred: biased data only inflates results, so it can kill strategies for free; clean
  data is only needed to confirm a winner, and the three-week trial may cover that.
- **23 Sep 11:55** — Created. Strategy register rewritten after research: A and B dropped,
  E upgraded, F downgraded on direct ASX evidence, N1 and N2 added. Four defects found
  on review and not yet queued (#3, #4, #5, #9).
