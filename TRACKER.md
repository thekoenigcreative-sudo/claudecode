# Tracker — ASX bot

Living plan. Reviewed and updated by Claude every time a Claude Code output comes back,
before the next prompt is handed over. Every line here is either verified against a file,
a log or a command output, or labelled as unverified.

**Last reviewed:** 2026-09-24 ~21:30 AEST (Claude Code, headless: announcements v2 and the day-trader playbook, live from 25 Sep 07:30)
**Visual version:** https://claude.ai/artifact/Pg5TsxMqKkbbwz2aFF3UqZ — republished to the
same link whenever this file changes.

---

## Where we actually are

| Area | State | Verified how |
|---|---|---|
| Announcement collector | Working. Real PDFs since 09:12 | `%PDF` magic on disk |
| Playbooks live | **From 25 Sep 07:30: announcements v2 and the day trader**, one watcher. v1 retired (disabled, values kept). Day 1 of 10 for both is 25 Sep | config.yaml `asx_announcements_v2`, `asx_daytrader` (frozen 2026-09-24, commits 666c17e, 18ca09f); dry check 24 Sep evening |
| Screen | v2: halt and tick (3%) kept; turnover is size-aware (our order < 5% of median daily turnover, so $100k at $5,000); no-quote tickers written to `data/arena/no_quote/`. v1's $250k floor retired with v1 | config.yaml, `arena/reaction_v2.py` |
| Reader / decider | Sonnet 5 medium / Opus 5.5 high (from the evening of 23 Sep; Sonnet 5 low / Opus 5 high before), confirmed per call. From 25 Sep the decider also answers the day trader's setups, 60 s each | `agent_mismatch` self-check |
| Broker, orders, fills, stops | Fills volume-aware since 24 Sep (20% of a bar); bars used only once final; orders before 09:59 fill at the opening auction (Yahoo's daily open, 20% of its estimated volume) from 24 Sep | `broker.work`, `minutes.final_bars`, `minutes.opening_auction`, tests/test_fill_volume.py, tests/test_opening_auction.py |
| Self-checks | 10 checks every cycle, loud on failure (`log_silent` added 24 Sep) | `selfcheck.run_checks`, live log |
| ASX 200 short universe | Real constituent list, 200 codes, dated | `asxbot universe asx200` |
| Arena position | **New books from 25 Sep:** `asx_announcements_v2__agent/__bot`, `asx_daytrader_v1__agent/__bot`, $20,000 each, opened 24 Sep 20:59 (by the self-check run; no order). v1's books below are kept, no longer traded. v1: none open. A1M bought 10:41 (ARN-000002, 0.8358 after the #24 correction, applied 19:40) and sold by its 0.90 target (ARN-000003, re-priced by the #25 correction, applied 20:31: 15:58 bar first, 0.9159, backup `correct_arn000003_20260923_203115`). Agent cash 20,227.09, bot 20,000.00. Both books topped up to $20,000 starting cash at 19:40. No arena order has ever filled at the open | account files read 23 Sep ~21:50 (read-only, hashes unchanged), `data/arena/backups/` |
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
| 20 | No outside check that the watcher is alive | High | **Running** - `watchdog.py` + heartbeat, every 5 min by `ASXBot Arena Watchdog` (`scripts/schedule_watchdog.ps1`). One Telegram alert per outage, one on recovery. Off for part of the evening of 23 Sep (#31, #32) |
| 21 | ~~`.gitignore`'s `data/` also hid `src/asxbot/data` — 8 modules never committed, a clone would not import~~ | — | **Fixed `76c2036`** — pattern is now `/data/` |
| 22 | The 22 Sep marks are in `events/arena_marks.jsonl` but `data/arena/marks/` is empty. Not a bug: `arena reset --yes` at 21:40 22 Sep (the deliberate cleanup in HANDOVER.md) deleted the marks files for test accounts that no longer exist; the event log is append-only and keeps them. No real account ever had a 22 Sep mark. `reset` writes nothing to the event log, so the log reads as if those marks still apply | Low | Optional: have `reset` log an event |
| 23 | The evening task (19:30/20:30: settle, mark, report) also ran in a visible console window; closing it would lose the day's settlement and the report. The watcher's hidden launcher did not flush printed output, so a killed run lost it | High | **Fixed and applied 23 Sep 16:01** - `scripts/arena_evening.pyw`; `scripts/schedule_evening_hidden.ps1` re-pointed the task (read back: only the action changed; backup in `data/task_backups/`). Both launchers now run unbuffered. First real run 19:30 23 Sep, **not yet verified** |
| 24 | **Fills priced from before the decision.** The watcher took one `now` at the top of each cycle and passed it to every order placed in that cycle, as the decision time; the fill rule then took the bar containing that minute. On 23 Sep the cycle that began at 10:29:46 (the watcher's restart) worked eight re-looks, a Sonnet and an Opus call each; A1M's decider replied at 10:37:41 and ARN-000002 was recorded then, stamped 10:29:46, and filled at the close of the 10:29 bar (0.8308) - eight minutes before it existed. Not the data time, as first suspected: the log shows 10:29:46 is the cycle start. Affected every order placed after anything slow in the same cycle (agent, re-looks, pre-close, yardstick after an announcement), and every fill (the decision minute's own bar) | High | **Fixed, this job** - the broker stamps `decided_at` from its own clock when it records the order; the caller's time is kept as `data_as_of`; fills take the first traded bar starting strictly after `decided_at`, pending until the feed holds it; the watcher reads the clock afresh per stage and per announcement; stop/target exits record `rests_from`; a resting limit no longer expires before the delayed feed reaches the close; the yardstick records a miss if its orders reach the broker after 10:00; new self-check `fill_before_order`. 15 tests fail on the old code. Loads at the 07:30 24 Sep start. ARN-000002 re-priced by `scripts/correct_fill_arn000002.py` (written, dry-run only, **not run**): 10:41 bar, 0.8358; cash and realised -15.02 (255.72 -> 240.71). Backtests checked: no equivalent look-ahead (review log) |
| 25 | ~~Fills ignored bar volume: ARN-000003 sold 3,000 A1M in the 15:57 bar, where 1 share traded~~ | — | **Fixed 24 Sep** - no bar fills more than `arena.fill.max_volume_share` (20%, dated in config) of its volume, the rest carries to later bars at their prices. Per account, per ticker, per bar; a stop exit takes the volume first, then a target exit, then the rest by age. A day order part-filled at the end of its session ends `partial` (the rest expires; the position keeps what filled); a stop or target exit keeps working into the next session until the position is out. A stop reached while a target is filling cancels the target's rest. Brokerage minimum once per order per day. Stops, targets and entries in one ticker are worked in one bar-by-bar pass (`broker.work`). ARN-000003 re-pricing: `scripts/correct_fill_arn000003.py`, **applied 23 Sep 20:31** (backup `correct_arn000003_20260923_203115`; the account file shows 15:58, 0.9159) |
| 26 | ~~Possible: the newest bar in the delayed feed may still be forming~~ | — | **Checked and ruled 24 Sep** - no completed bar changed (A1M, BHP, FMG, DUG cache vs fresh fetch; 180 polls of six London/Frankfurt stocks in session). Only the newest row changes: Yahoo's zero-volume placeholder for the minute forming. Rule (`minutes.final_bars`): a bar must have traded, have ended, and not be the newest row of an intraday fetch; `settle_minutes` 0 in config. ASX bars **not** polled during a session - worth a poll on 24 Sep. The two A1M "10:29" prices were two delayed quotes ~1m40s apart (10:15 and 10:17 closes), both stamped with the stale cycle clock (#24), not a revised bar |
| 27 | Yahoo's `^AXJO` daily open equals the prior close on most days (0.98 of days in 2015, 0.93 in 2026), so "gap vs the index" was in effect a raw gap | Medium | **Live fixed 24 Sep; frozen results untouched.** The live yardstick takes the index open from the ^AXJO 10:00 minute bar (config `yardstick.index_open: first_minute_bar`, dated). 14-23 Sep against the cap-weighted opening gap of ~195 members: mean error 0.12 points (10:00 bar), 0.17-0.23 (ETF opens), 0.40 (Yahoo daily). No 10:00 bar = NotReady, then a recorded miss; never a fallback. **Affected, not rerun:** A and B in reports/phase1.md and phase1_20k.md (both use `detect_events`' gap vs index): in the years the index open is the prior close, their trigger was a raw 5% gap - admitting stocks riding a market-wide up-gap, and missing some on down-gap days. The size of the effect is unknown and cannot be measured on free data (Yahoo keeps ~7 days of 1-minute history). **Not affected:** momentum baseline (closes), S1 (close-to-close vs index), S2 (no index), filter_cost (closes), the arena's scoreboard. A's live yardstick and its frozen backtest now measure the index leg differently: compare them with that in mind |
| 28 | ~~1-minute data leaves out the opening auction: the first bar of an ASX day usually shows volume 0 (BHP 15, 16, 21, 23 Sep; CBA 6 of 7 days; A1M's 10:14 resumption) and the 1m volumes sum short of the daily (A1M 9.62m vs 9.73m). So no fill ever used the auction print: an order "at the open" filled at the first traded minute after it~~ | — | **Fixed 24 Sep (approved by Rick 23 Sep)** - every day's bars are led by its opening auction, stamped 09:59, priced at Yahoo's daily open; an order recorded before 09:59:00 joins it (agent pre-open orders, orders after the close or on a weekend, the yardstick's next-open entries), and so does a stop or target the auction price reaches (fills at the auction price). At most `arena.fill.auction_volume_share` (20%, Rick's bar figure reused, **confirmed by Rick 23 Sep 22:20**) of the auction's estimated volume fills there - daily volume less the minute volumes, an upper bound (it also holds off-market trades) - the rest carries into the minute bars. Each auction is read once and kept (`<date>.auction.json`). **Validation** (`reports/auction_open_check.md`, 50 stocks, 348 stock-days, 15-23 Sep): daily close = 16:10 closing-auction bar 348/348; open on tick 348/348; open outside every minute bar's range 20%; open = first traded minute's open only 12%; median gap between them 0.30%, p90 1.16%, max 9.69% (TUA 23 Sep, 2.49 vs 2.27). **Not confirmed against the ASX:** every free independent source refused automated reading; CNBC is the same vendor. **Refused, fallback to the first traded minute, labelled in the fill basis:** no daily bar with an open by 10:52 (the wait: 30 min past open + 22 min delay; nothing in the ticker is worked while it waits), open off-tick, outside the day's range, or daily volume not above the minute volumes (15 of 348 days, e.g. A1M 17 Sep). A failed check on a day the feed still holds is re-read until 10:52, then kept. The yardstick's cut-off moved 10:00 -> 09:59. `opening_auction: first_minute` restores the old rule. **Affected:** every arena fill at the open from 24 Sep (none before: ARN-000002 10:41, ARN-000003 15:58). **Not affected:** every frozen backtest (A, B, S1, S2, momentum, filter_cost) - they already enter and exit at the daily open, so the arena now prices the open as they do |
| 29 | `fast_info.previous_close` moves once the day's first bar appears (A1M 0.80 at 10:23, 0.795 at 10:36), so the re-look packet's "% vs prior close" has no stable base | Low | **Not yet queued** |
| 30 | CMM, NUF and TUA were each re-looked twice on 23 Sep (10:20 and 10:29-10:33); the rule is one each. The 10:29:46 restart is the likely cause. Unverified | Low | **Not yet queued** |
| 31 | ~~`ASXBot Arena Watchdog` task is **Disabled** (last run 19:45 23 Sep, next run N/A). Nothing in tonight's job disabled it. If it stays off, nothing outside the watcher watches the 24 Sep session~~ | — | **Re-enabled 23 Sep 20:36.** Claude disabled it by hand that evening because every run flashed a window (#32); re-enabled once that was fixed. Test run 20:38:02, result 0, state file updated |
| 32 | **Every scheduled launch flashed a window.** uv 0.10.2 made `C:\venvs\asx-bot\Scripts\pythonw.exe` a CONSOLE program, byte for byte the same as its `python.exe`, which then started the base interpreter's console `python.exe`. So the Warmup (07:30), Evening (19:30) and Watchdog (every 5 min) tasks each opened a console window (conhost) titled with the launcher's path: the flash Rick saw every 5 minutes on 23 Sep. `pythonw` was trusted by name (#19, #23) and never checked | High | **Fixed 23 Sep 20:35.** The venv's `pythonw.exe` is now CPython's own GUI venv launcher (`<base>\Lib\venv\scripts\nt\pythonw.exe`, what `python -m venv` installs), which starts the base `pythonw.exe` inside the venv; uv's is kept as `pythonw.exe.uv-console-bak-20260923`. Task command lines unchanged. Proof: a process + window recorder at 20:37 on 23 Sep, with the same probe run through a task each way. The old launcher gave conhost and a visible ConsoleWindowClass window. The new launcher and interpreter are GUI: no conhost, no window. The real Watchdog task was the same (20:38:02, result 0). The watcher's own child (`asxbot.exe`, a console program) still gets a conhost, windowless through CREATE_NO_WINDOW, as before. Watchdog detection was re-tested through the new chain: live gives ok, killed gives one DOWN, restarted gives BACK UP. New self-check `console_launcher`; `scripts/install_gui_launcher.py` puts it right if uv rebuilds the venv. etf-agent's venv had the same launcher and got the same fix |
| 33 | Yahoo's 1-minute bars show no trace of the ASX's staggered open (five groups, 10:00 to ~10:09): the first traded minute is 10:01 on 242 and 10:00 on 82 of 348 stock-days across all groups, S-Z included. Either the bars are not stamped with trade times or the stagger is not what we think. Every minute-bar time in the first ten minutes is uncertain by that much. The auction is stamped 09:59 for every group, so it is not affected | Low | **Not yet queued** - check against a broker's course of sales once IBKR data starts |
| 34 | The opening-auction price is Yahoo's daily open: **checked, not confirmed** against the ASX (#28, `reports/auction_open_check.md`). Accepted by Rick on that basis 23 Sep 22:20 until IBKR live data starts | Medium | **Tied to IBKR live data (milestone 8)** - once it starts, confirm the auction price against a broker trade record (course of sales / IBKR fill) for the same stock-days; until then every auction fill stays labelled "checked, not confirmed" |
| 35 | **The watcher's logs went silent while it ran, twice on 24 Sep.** Google Drive silently cut off the long-open append handles on `data/logs/asxbot.log` and `data/arena_warmup.log`: at 08:14 (noticed; restarted 08:23), and again at 12:14:04 until the watcher stopped at 19:25 (not noticed until the logs were moved that evening). Files written whole or opened per write kept updating, so the heartbeat looked healthy; `errors_logged` read a dead file for 7 hours | High | **Fixed 24 Sep evening** - every log in `%LOCALAPPDATA%\asx-bot\logs` (daily rotation kept), copied to `data/logs/` whole after each evening run; new `log_silent` self-check (a log 5+ min behind the watcher's last line), Telegram once an hour. Old logs copied over and checked byte for byte. First real run 07:30 25 Sep, **not yet verified** |
| 36 | The 07:30 poll logged ERROR "no announcements table with a Headline column found" (07:30:05, 07:31:24 on 24 Sep): before the ASX posts anything the page has no table | Low | **Fixed 24 Sep evening** - an ASX page with no announcement links is zero, at INFO; anything else is still an ERROR. The empty-page fixture is built by hand (no real one was kept); the poller now keeps the first empty page of each day in `data/announcements/pages/` to replace it. First real empty page 07:30 25 Sep, **not yet seen** |
| 37 | **The hourly digest was noise.** On 24 Sep the Trader sent "nothing passed this hour" every hour from 11:33 to 18:35, eight times after the last pass (10:32), and four digests after the 16:10 session summary (16:34, 17:35, 18:35, and 19:25 when the watcher stopped). The quiet-hour digest was meant to show the watcher alive; the watchdog and `log_silent` do that now | Low | **Fixed 24 Sep evening** - a digest only for an hour with a pass, an order, or a screen-out worth reading (no quote, no history, or no trades by 10:30 on a non-halt announcement, listed by name); the last part-hour goes just before the 16:10 summary if it had anything; after the summary no digest that day, forced or not. A test replays 24 Sep's passes and quiet hours: one digest (10:32) and the summary. With 24 Sep's real screen-outs, 5 digests instead of 10 (review log). First real day 25 Sep, **not yet seen** |
| 38 | `live/quotes.py` reads `fi.get("last_volume")`, which returns None on yfinance 1.7.0: every quote's volume is 0 (24 Sep day review). v1's screen and packets used it | Medium | **Not fixed (needs Rick's OK)** - v2 and the day trader never read it: volume comes from minute bars. Still read by v1 (retired) and the real-money scanner (`live/scanner.py`) |
| 39 | Yahoo's 10:00 minute bar holds the opening auction's volume on some stock-days and none on others (171 vs 892, 17-24 Sep) | Medium | **Handled before any run** - every v2 and day-trader volume measure counts from 10:01 (`intraday.VOLUME_FROM`), dated in config.yaml (18ca09f). LEARNINGS #20 |
| 40 | The day trader's scan asks Yahoo for up to 90 stocks a cycle (~5,400 an hour). Yahoo's limit is unknown; a refusal would also stall fills | Medium | **Watch 25 Sep** - a refused or mostly empty batch halves the budget and logs an ERROR (self-check alert). The backfill of 633 stocks x 6 days in 3 minutes on 24 Sep was not refused |
| 41 | The decider is asked about every day-trader setup the agent's book can take (60 s each). The replay found dozens of setups a day | Medium | **Watch plan usage 25 Sep** - calls stop when the book is full (3 open / 6 new) and a setup older than 5 bars (including time spent on earlier calls) is not asked about |
| 42 | On the free feed every decision is ~20 minutes behind the market: the v2 rule bot's "10:31" entry is ~10:52, a setup is entered ~20 minutes after its trigger bar | High (for meaning, not for code) | **By design until IBKR live data** - every report says "delayed data - rehearsal until IBKR live prices"; `arena.intraday_data.provider: ibkr_live` switches it (not connected yet) |
| 43 | v1's yardstick signal OFX (for the 25 Sep open, day review) will not be traded: v1 is retired | Low | Recorded, not a defect |

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

- **24 Sep ~21:30** - Headless build from Rick's brief (24 Sep evening). **Announcements v2**
  (trade the reaction) and **the day trader** frozen in config.yaml before any run (666c17e),
  one pre-run correction (volume from 10:01, #39, 18ca09f), code, tests and a plumbing replay
  (328246a and the next commit). Level 1: $5,000 a position, 3 open, 6 new a day (dated;
  real-money `limits:` unchanged). v1 retired, its books and values kept. Tests: 388 passed,
  1 skipped; ruff clean. Dry check (scratch books, no agent, no order): the scheduled command
  runs v2 + the day trader on the Yahoo delayed feed; 5 stocks with news after 24 Sep's close
  queue for 25 Sep's reaction looks (LKE, MOT, MRE, MXT, PEN); the live batch feed held 20
  stocks in memory and wrote nothing to Drive; nothing runs before the open. Self-checks 10/10
  ok. Scheduled task unchanged (`arena_warmup.pyw` -> `arena watch --until auto`), next run
  25 Sep 07:30. **Not verified until 25 Sep:** Yahoo's tolerance of the scan (#40), decider
  load (#41), the first reaction look and v2 rule-bot day, the end-of-day minute write.

- **24 Sep 20:30** - Headless job, no strategy change. The hourly digest now goes only for
  an hour with a pass, an order or a screen-out worth reading, and never after the 16:10
  session summary (#37). Notification only: the screen, the decider and every threshold are
  unchanged. 24 Sep's real screen-outs replayed through the new rule (`worth_reading`):
  digests at 10:32, 11:33, 12:33 and 15:34, plus a 15:34-16:10 part-hour (JNO) just before
  the summary - 5 instead of 10, and none after 16:10. Three of those four hours had no
  pass; they go because they list screen-outs by name (PCI, LF1, MHC, AMD no trades by
  10:30; AXL, NVQ, CMX, PAR no quote), where before they were only a count. The 7 after
  16:10 (PEN to RCT, all "no trades") would wait for the next trading day's first digest:
  the evening report lists neither passes nor screen-outs, so dropping them would lose
  them. Found on the way, not changed: SGR's "The Star Gold Coast Licence Suspension
  Deferred" was screened out at 15:58 as a halt notice, because its headline
  contains "suspension".

- **24 Sep 19:50** - Headless job. (1) Logs off Google Drive: `asxbot.log`, the warm-up,
  evening and filter-cost launcher logs and `watchdog.log` now live in
  `%LOCALAPPDATA%\asx-bot\logs`, rotated daily and copied to `data/logs/` whole after each
  evening run (#35). Found while doing it: the log went silent a second time, 12:14 to
  19:25, unnoticed. (2) `log_silent` self-check. (3) An empty announcements page is zero,
  not an ERROR (#36). No scheduled task needed re-registering: their actions are unchanged,
  and the scripts they run hold the new paths.

- **23 Sep 22:30** - Headless record-only job, no behaviour change. Rick confirmed on 23 Sep 22:20 AEST, in chat, the #28 defaults: (1) `arena.fill.auction_volume_share` 0.20 confirmed, value unchanged, dated in config.yaml; (2) the auction price from Yahoo's daily open accepted as **checked, not confirmed** until IBKR live data starts, then to be confirmed against a broker trade record (#34, LEARNINGS 18); (3) auction fills keep the 0.10% slippage base, matching the frozen backtest; (4) the two documented simplifications accepted as-is: an order recorded 09:59:00-10:00 misses the auction and fills at the first traded minute, and the staggered open (five groups, 10:00 to ~10:09) is not modelled - every auction is stamped 09:59 (#33).

- **23 Sep 22:00** - Headless job, #28 opening auction (Rick approved 23 Sep). Source: Yahoo's
  daily open, fetched beside the minute bars and read once per stock-day; validated in
  `reports/auction_open_check.md` (above, #28) - checked, **not confirmed** against the ASX:
  Stooq, MarketWatch, Market Index, Google and the FT all refused automated reading, and CNBC
  carries the same vendor's numbers. Intraday sync of daily and minute volumes was polled on
  London and Frankfurt stocks (ASX closed): steady to within a minute's volume, one block
  trade ahead. End to end on real 23 Sep data in a scratch folder: hypothetical 09:30 buys in
  TUA, BHP and A1M filled at 2.49, 61.83 and 0.80 in the auction (A1M's is its 10:14
  reinstatement auction), auction volumes 246,522 / 401,259 / 114,535. 329 passed, 1 skipped,
  three runs; ruff check clean. 21 new or changed tests fail on aab9e55: 18 on behaviour, 3 on
  names only (the `first_minute` switch, the config wiring, and the 09:59:30 order, whose
  fill is unchanged by design). Load check read-only: config, 66 modules, both books load,
  5 file hashes unchanged. No frozen backtest, threshold or holdout touched; the two new
  config values are dated. Found on the way: Yahoo's minute bars show no stagger - the
  first traded minute is 10:00/10:01 for all five open groups (#33). TRACKER's position row
  was stale: ARN-000003's correction was applied at 20:31.

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
