# IB Gateway: kept up by a supervisor (25 Sep 2026)

IB Gateway (10.50, `%LOCALAPPDATA%\Programs\ibgateway`) feeds the watcher live ASX prices
(`data.live_provider: ibkr`, src/asxbot/ibkr). It is logged in to Rick's LIVE account with
the API read-only. On 25 Sep it died at 08:58:29 with the Claude desktop app that had started
it (an automatic app update; LEARNINGS #24). Since then it runs only like this:

| Piece | What it does |
|---|---|
| IBC 3.24.2 in `C:\IBC` | IbcAlpha/IBC, from its GitHub release `IBCWin-3.24.2.zip`, sha256 `ba8e95f6…fdd892` (checked against the release's published digest before unpacking). Starts Gateway, fills in the login, handles Gateway's dialogs. |
| `scripts/ibc/config.ini` | IBC's settings; copied to `C:\IBC\asxbot\config.ini` at each start. Live; `ReadOnlyApi=yes`; `AcceptIncomingConnectionAction=reject`; `AllowBlindTrading=no`; `ExistingSessionDetectedAction=primary` (a phone or web login does not knock Gateway off for good); auto-restart 23:45; no logoff, no closedown; IBC's command server off. **No login in it, ever.** |
| Task "ASXBot IB Gateway" → `scripts/ibgateway.pyw` | The launcher. Runs IBC's Java command (`ibkr/ibc.py`, the same command as IBC's StartIBC.bat, but not through cmd.exe), no console, restarts it as IBC's script would: the 23:45 auto-restart (no new login), a login window that never came, an unanswered phone approval (at most 3 in a row). Renames `ibgateway.exe` to `ibgateway1.exe` as IBC does, so Gateway's own restarter cannot start a second Gateway outside IBC. |
| Task "ASXBot IB Gateway Supervisor" → `scripts/ibgateway_supervisor.pyw` | Every 2 minutes and at logon: port 4001 open? API connection (client 43) says the link to IBKR is up? If not, it restarts Gateway through the task above (rules in `ibkr/supervisor.py`) and tells Rick on the Trader chat when his phone (or, with no login stored, the PC) is needed: once, and once more after 10 minutes. |
| Task "ASXBot IBKR Preflight" → `scripts/ibkr_preflight.pyw` | 07:20 Monday-Friday (ASX holidays skipped): `asxbot ibkr check`; a Trader chat message only if it fails. |
| `ibkr_login_setup.cmd` (repo root) | Rick, once: double-click, type the IBKR username and password (the password hidden). Stored in Windows Credential Manager (DPAPI, his Windows account, this PC only). `--check` / `--forget`. |

Logs, all in `%LOCALAPPDATA%\asx-bot\logs`: `ibgateway.log` (IBC's own output, login blanked),
`ibgateway_supervisor.log` (one line per check), `ibkr_preflight.log`. State:
`%LOCALAPPDATA%\asx-bot\ibgateway\launcher.json` and `supervisor.json`.

## What cannot be automated

- **The phone approval.** Every full login to a live IBKR account ends in an IBKR Mobile
  (IB Key) approval. IBKR requires it; nothing can skip it. Gateway's 23:45 auto-restart
  needs none (until IBKR's weekly full re-authentication), so a normal week costs one
  approval. A restart that needs one only starts on ASX trading days 06:45-21:00, so the
  phone is not pinged at night; after 3 unanswered approvals the supervisor waits 30 minutes.
- **The login itself, until Rick runs `ibkr_login_setup.cmd`.** Until then the supervisor
  still starts Gateway, which opens its login window, and asks Rick on the Trader chat to
  log in on the PC. A hand-started Gateway waiting at its login window is left alone.

## Where the login goes

Credential Manager → the launcher, at each start → IBC's two program arguments (IBC takes it
no other way except plain text in its config file, which is not used). So while Gateway runs,
the username and password are in the Java process's command line, readable by Rick's own
Windows account and administrators - as with IBC's own scripts. They are never written to a
file, a log, a message or a printed command (`ibc.redacted`, `ibc.scrub`; tested).

## By hand

- Is it healthy? `C:\venvs\asx-bot\Scripts\asxbot.exe ibkr check` (client 42, read-only).
- Stop Gateway on purpose: create an empty file `%LOCALAPPDATA%\asx-bot\ibgateway\PAUSE`
  (the supervisor then does nothing), then end the task: `schtasks /end /tn "ASXBot IB Gateway"`.
  Delete PAUSE to resume; the next check starts it.
- Never start Gateway (or IBC) from a terminal or a Claude session. Start it only through
  `schtasks /run /tn "ASXBot IB Gateway"` - or leave it to the supervisor.
- Re-create the tasks: `scripts\register_ibgateway_tasks.ps1` (it leaves a running launcher
  alone), then `scripts\register_release_tasks.ps1`, so they run the deployed release and not
  this checkout.

## Deviations from the brief, and why

- A Gateway that answers but is cut off from IBKR gets 5 checks (10 min), not 2, before a
  restart: Gateway reconnects by itself after IBKR's resets, and a restart costs Rick a
  phone approval.
- "Nothing running" restarts at once (not after 2 checks): there is nothing to wait for.
- Automatic logins (which ping the phone) wait for an ASX trading day, 06:45-21:00.
