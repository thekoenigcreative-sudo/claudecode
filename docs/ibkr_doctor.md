# The IBKR connection doctor (26 Sep 2026)

Rick, 25 Sep: "a safeguard to stop the IBKR connection dropping is needed too, using an
agentic workflow". Three layers now keep the live prices up, each watching the one below:

| Layer | Where | What it does |
|---|---|---|
| The persistent connection | `src/asxbot/ibkr/live.py`, inside the watcher | One read-only connection for the day: 30 s heartbeat, reconnect with backoff, every stream re-requested after a drop (inside IBKR's real-time bar budget), history paced and cached. |
| **The connection doctor** | `src/asxbot/ibkr/doctor.py`, ticked by the watcher every cycle (`FailoverFeed.check`) | Names the CAUSE of any trouble, takes the recovery that cause needs, checks it worked, escalates when it did not, pauses new entries while the prices cannot be trusted, and tells Rick in plain words. |
| The Gateway supervisor | `src/asxbot/ibkr/supervisor.py`, task "ASXBot IB Gateway Supervisor" every 2 minutes, outside the watcher | Keeps IB Gateway itself running (relaunch through IBC, Rick's phone approval), never while the PC is offline; answers the doctor's restart request; says when Gateway is back. |

## Why plain code, not a model

It is an agent in the plain sense - it observes, decides and acts on its own, in a loop - but
the loop is code. It must keep working when the Claude plan is at its limit (25 Sep 22:41 the
builds exhausted it; the trader agents run on the same plan), it has to answer inside a
minute, and every cause it can name has one right answer. Where a cause is outside its
reach (Rick's phone, his login elsewhere), it tells him and waits.

## Causes and what the doctor does

Each rung has a deadline: if the cause has not cleared by then, the next rung. The episode
closes, "verified", the first tick the diagnosis is healthy again, with what fixed it.

| Cause (`diagnose`) | Evidence | Ladder (wait before the next rung) |
|---|---|---|
| network_down | neither the internet (1.1.1.1, 8.8.8.8, api.telegram.org) nor IBKR's servers (from Gateway's jts.ini) answer a TCP connect | wait 3 min, then tell Rick (sent when the network is back); never a restart |
| gateway_down | no Gateway process, nothing on port 4001 | ask the supervisor to check at once (it relaunches through IBC); the supervisor tells Rick if his phone is needed |
| login_needed | the launcher's Gateway at its login or phone approval; a Gateway process with port 4001 closed | ask the supervisor to check at once; it tells Rick |
| gateway_hung | port open but the API does not answer; or no heartbeat answer for 150 s | reconnect (90 s), the supervisor (4 min), tell Rick |
| client_id_in_use | IBKR error 326 / "already in use" on connect | reconnect under spare client id 44 (90 s), then 47 (90 s), tell Rick |
| watcher_disconnected | Gateway healthy, our socket not | wait (60 s), reconnect (90 s), a spare client id (90 s), tell Rick |
| ibkr_link_down | connected, but Gateway reports 1100/2110 | wait 3 min (Gateway reconnects itself after IBKR's resets), tell Rick; the supervisor restarts Gateway after 10 min |
| competing_session | IBKR 10197 and no data since | wait 1 min, tell Rick to log out of IBKR on the phone/web |
| delayed_data | the 10-minute probe quote came back delayed or refused in market hours | reconnect (asks for real-time again), tell Rick to check the ASX subscription |
| stale_stream | 10:03-16:00: the index's streamed bars older than 2 min, heartbeat fine | re-request the index stream (90 s), reconnect (2 min), a Gateway restart through the supervisor - only after a second witness (IBKR's history holds recent index bars, so only the stream is dead) and only with Rick's login stored (8 min), tell Rick |
| pacing | 3+ pacing violations in 10 min | slow the history queue x2 for 10 min (does not pause entries) |

While any cause but pacing is open, **new entries are paused** (`Doctor.block`, read by
`FailoverFeed.entries_allowed`) for both playbooks and both books; stops, targets, trailing
and the 15:50 close keep working. They resume by themselves when the episode closes.

## What Rick hears

At most two lines an episode, on a trading day 07:00-17:00: one when the doctor needs him or
has run out of rungs ("IBKR prices stopped at 10:05: ... New entries are paused; stops and
exits keep working. Tried: ...") and one when it is fixed ("IBKR prices are back at 10:17
after 12 min ..."). A short blip that clears on its own is recorded, not sent. Gateway
logins and phone approvals are the supervisor's messages. While the doctor is running and has
an episode open, the watcher's own `live_data` self-check does not send its Telegram line.

## Records

- `%LOCALAPPDATA%\asx-bot\ibgateway\doctor.json`: the last diagnosis, the open episode (its
  causes, every action with its result, when Rick was told) and the last 50 episodes.
- `data/events/ibkr_doctor.jsonl`: every episode opened, cause change, action and close.
- `data/arena/live_data.json` carries the doctor's current cause, evidence and episode.
- `%LOCALAPPDATA%\asx-bot\ibgateway\RESTART_REQUEST.json`: the doctor's request to restart
  Gateway (the supervisor answers it within 2 minutes and deletes it; ignored after 15 min).
- `asxbot ibkr doctor` prints all of it and probes the port, the internet and IBKR now.

## Tests

`tests/test_ibkr_doctor.py`: every cause from its evidence; every ladder walked with a moving
clock (escalation, verification, told once, the "back" line, outside Rick's hours, an action
that raises); end-to-end chaos against the fake IB (a dropped socket, a stream that stops, a
client id held by another connection, a competing session, the link to IBKR lost and
restored, Gateway killed and relaunched, pacing); the supervisor's side (no restart while
offline, the restart request honoured or dropped without a stored login, the "back" line).
The real Gateway was tested on 26 Sep with a socket drop and a client-id clash on spare ids
(reports/ibkr_doctor_live_<stamp>.json) - never by killing Gateway, which would need Rick.
