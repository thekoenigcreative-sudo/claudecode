# Releases: what the scheduled tasks run (25 Sep 2026)

Until 25 Sep every scheduled task ran a launcher straight from this checkout on Google
Drive. A build editing files here changed what the next task start loaded, and on 25 Sep a
build restarted the watcher at 10:52, mid-session. Now:

| Piece | Where | What |
|---|---|---|
| A release | `%LOCALAPPDATA%\asx-bot\releases\<yyyymmdd-hhmmss-sha10>\` | `git archive` of one commit: `src`, `scripts`, `tests`, `config.yaml`, `docs`; `RELEASE.json` says which commit, when, and that the suite passed inside it |
| `CURRENT`, `PREVIOUS` | `releases\` | Pointer files: the release the tasks run, and the one before (`--rollback`) |
| Shims | `%LOCALAPPDATA%\asx-bot\bin\<task>.pyw` | One per task (`asxbot.release.SHIM_TEMPLATE`, written by `deploy.py`). Each resolves `CURRENT` at start, sets `ASXBOT_HOME`, puts the release's `src` first on the path, logs one line to `logs\releases.log`, and runs `CURRENT\scripts\<task>.pyw` |
| `ASXBOT_HOME` | this checkout, `G:\My Drive\asx-bot` | Settings and data never move: `config.yaml` (a `/model` change from the Trader chat edits and commits it here), `.env`, `data\` (books, events, minute cache), `reports\`. `asxbot.config.repo_root` reads the variable |

The tasks: `ASXBot Arena Warmup`, `Arena Watchdog`, `Arena Evening`, `Chat`, `IB Gateway`,
`IB Gateway Supervisor`, `IBKR Preflight`, `Filter Cost`, `IBKR History Fetch` (added 25 Sep evening). `scripts\register_release_tasks.ps1`
points each at its shim (backing up the definition to `data\task_backups\`); only the action
changes.

## Deploying

    C:\venvs\asx-bot\Scripts\python.exe scripts\deploy.py             # HEAD
    C:\venvs\asx-bot\Scripts\python.exe scripts\deploy.py <commit>
    C:\venvs\asx-bot\Scripts\python.exe scripts\deploy.py --status
    C:\venvs\asx-bot\Scripts\python.exe scripts\deploy.py --rollback
    C:\venvs\asx-bot\Scripts\python.exe scripts\deploy.py --test-only <tree-ish>

A deploy exports the commit (never the working tree), runs the whole test suite inside the
export with only the export on the import path, and only then points `CURRENT` at it. It
restarts nothing: each task runs the new release from its next start (the watcher's is 07:30,
or `scripts\watcher.py restart`; the chat's, `schtasks /end` then `/run /tn "ASXBot Chat"`).

## The market-hours lock

`scripts\watcher.py stop|start|restart` and `deploy.py` refuse between 07:25 and 19:25 Sydney
on an ASX trading day unless `--force --reason "..."` (logged to `logs\watcher_control.log`
or `RELEASE.json`), and `watcher.py` refuses whenever any arena account holds a position or
has an order working, forced or not (`asxbot.arena.lock`). The rule is in CLAUDE.md.

## Checking what runs

- `deploy.py --status`: `CURRENT`, `PREVIOUS`, whether HEAD is deployed, the shims present.
- `%LOCALAPPDATA%\asx-bot\logs\releases.log`: every task start, with the release it ran.
- The watcher's first log line and the chat's say which release and which settings folder.
