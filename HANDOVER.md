# Handover — 22 Sep 2026, ~15:25 AEST (laptop → home PC)

## What is finished

All nine build stages in SPEC.md section 9 are built, tested and committed on `master`
(7 commits, 63 tests passing, ruff clean). Broker mode is `sim`. Nothing can place a real order.

| Stage | Where | Notes |
|---|---|---|
| 1 Skeleton | `config.yaml`, `src/asxbot/config.py`, `log.py`, `io.py` | Capital $10,000, 4 positions. Strategy/baseline/costs/universe parameters frozen 2026-09-22. Live mode refused without both keys. |
| 2 Data layer | `src/asxbot/data/` | yfinance adapter, Norgate adapter (raises until `norgatedata` + Updater are installed), Parquet store, universes, benchmark. |
| 3 Collector | `src/asxbot/announcements/` | Paced (3 s), cached, stops and alerts on refusal. Parser fixtures in `tests/fixtures/`. |
| 4 Backtester | `src/asxbot/backtest/`, `reports/phase1.md` | Strategy A (announcement + reaction), strategy B (reaction only), momentum baseline, benchmark, 1x/2x slippage, IS/OOS. |
| 5 Scanner | `src/asxbot/live/` | Announcement + live reaction → proposal JSON in `data/proposals/`. |
| 6 Orders | `src/asxbot/broker/orders.py`, `sim.py`, `reconcile.py` | `place_order` enforces every limit in code; sim broker issues `SIM-nnnnnn` ids; reconciliation alerts on mismatch. |
| 7 OpenClaw | `asxbot` CLI, README "OpenClaw trader agent" | Commands: `scan`, `proposals`, `place_order`, `positions`, `reconcile`, `daily_report`. Agent not yet created in OpenClaw. |
| 8 Dry run | `asxbot dryrun` | Ran clean in sim today: proposal P-20260922-001 → SIM-000001 filled → reconcile clean → unwind SIM-000002 → clean. |
| 9 IBKR | `src/asxbot/broker/ibkr.py` | `ib_async` adapter behind the broker interface. Refuses unless `paper`/`live`; not installed (`uv pip install -e ".[ibkr]"` when needed). |

Both strategies named in the spec are implemented (A "post-announcement drift", B "drift after a
volume-confirmed surprise"). No other strategies were requested in this session; if you have
others in mind, they are not started.

## Where the announcement archive is up to

- ASX 300 archive stopped cleanly at 15:22 (no `.tmp` files, `_progress.json` valid).
- Progress: **26 of 300 codes** complete (alphabetically `360` … `ASB`, plus `BHP`), 575 pages,
  45,106 announcement rows in `data/announcements/history/`.
- Every fetched page is also cached under `data/announcements/cache/`, so a restart never
  re-downloads what it already has.
- Small-universe archive (1,525 codes): **not started**.
- Estimated remaining: ASX 300 ≈ 4–5 h at the 3 s pace; small ≈ 12–15 h. Run them one at a time.

## Pending

1. Let the ASX 300 archive finish, then `asxbot backtest` and commit the refreshed
   `reports/phase1.md`. Strategy A currently rests on 6 archived codes (43 trades); the report
   says so in its warning block.
2. Start the small-universe archive after that; rerun the backtest again.
3. Create the OpenClaw trader agent (own workspace, own Telegram bot, ask-on-every-command
   approvals). Steps are in README.md.
4. Norgate: install the Data Updater, `uv pip install -e ".[norgate]"`, set
   `data.provider: norgate` in `config.yaml`, refetch, rerun. Only then is any result a go/no-go.
5. Phase 2 (IBKR paper): account, IB Gateway, `uv pip install -e ".[ibkr]"`, `broker: paper`.

## Caveats baked into today's report (all labelled in the report itself)

- Universe (a) is a **proxy**: today's top 300 by market cap from the ASX directory. No public
  point-in-time ASX 300 list exists; Norgate provides it later. Override with
  `data/universe/asx300_manual.csv` (header `code`) if you have a list.
- Benchmark is STW.AX adjusted close from 2008, spliced onto the ^AXJO price index before that.
- The momentum baseline shows ~28% CAGR on universe (a). That is survivorship bias, not skill.
- Strategy B loses after costs on both universes. That is a real result, not a bug.
- yfinance quotes are ~20 min delayed; the live scanner is plumbing only until IBKR data.
- Sim broker state (`data/broker_sim/state.json`) holds the two dry-run orders. Delete that file
  and `data/proposals/P-20260922-001.json` for a clean slate; they are not in git.

## Before shutting the laptop

Wait until Google Drive shows the `asx-bot` folder fully synced. `data/` is gitignored but
lives inside the Drive folder, so the price cache (1,825 Parquet files), announcement cache,
archive progress and event logs all travel via Drive, not git. If Drive is mid-sync when the
laptop closes, the home PC will see a partial `data/`.

## Set up and resume on the home PC (Windows)

Do not start the archive on two PCs at once: they share `_progress.json` through Drive.

```powershell
# 0. Wait for Drive to finish syncing G:\My Drive\asx-bot on the home PC.
cd "G:\My Drive\asx-bot"
git log --oneline -3          # expect 5eb3690 "Stages 7 and 8 ..." at the top (plus this handover commit)

# 1. Python 3.12 and uv (once per PC). Python 3.14 is the default here and is NOT used.
winget install astral-sh.uv   # or: pip install uv
winget install Python.Python.3.12

# 2. Virtual environment OUTSIDE Drive, one per PC
uv venv C:\venvs\asx-bot --python 3.12
$env:VIRTUAL_ENV = "C:\venvs\asx-bot"
uv pip install -e ".[dev]"

# 3. Secrets file (there is no .env yet; nothing in it is needed for sim)
Copy-Item .env.example .env

# 4. Verify
C:\venvs\asx-bot\Scripts\asxbot.exe check            # broker=sim, capital 10000, 4 positions
C:\venvs\asx-bot\Scripts\python.exe -m pytest -q      # 63 passed, 1 skipped
C:\venvs\asx-bot\Scripts\asxbot.exe data-status       # 1825 tickers cached
C:\venvs\asx-bot\Scripts\asxbot.exe announcements status   # codes_with_data 26, pages_done 575

# 5. Resume the ASX 300 archive, detached (survives closing the terminal; Ctrl-C safe; resumable)
Start-Process -FilePath "C:\venvs\asx-bot\Scripts\asxbot.exe" `
  -ArgumentList "announcements","history","--universe","asx300" `
  -WorkingDirectory "G:\My Drive\asx-bot" `
  -RedirectStandardOutput "G:\My Drive\asx-bot\data\history_asx300.log" `
  -RedirectStandardError  "G:\My Drive\asx-bot\data\history_asx300.err" `
  -WindowStyle Hidden
Get-Content "G:\My Drive\asx-bot\data\history_asx300.err" -Tail 3   # progress lines go to .err

# 6. When it finishes (log line "history archive: {...}"), refresh the report and commit
C:\venvs\asx-bot\Scripts\asxbot.exe backtest
git add reports/phase1.md; git commit -m "Refresh phase1 report with full ASX 300 archive"

# 7. Then the small universe, same command with --universe small
```

If the archive stops with `data/alerts/collector_parse_error.flag`: read the flag (it names the
code and year), open the cached page, extend `src/asxbot/announcements/parser.py`, add a trimmed
fixture under `tests/fixtures/` with a test, `asxbot alerts-clear collector_parse_error`, relaunch.
If it stops with `collector_access_refused.flag`: do not work around it; the system keeps running
on price/volume signals (strategy B) until you decide otherwise.

Log lines from the CLI go to stderr and to `data/logs/asxbot.log` (daily rotation).
