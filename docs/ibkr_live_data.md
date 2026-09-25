# IBKR live data: one connection, streamed bars, the rotation (25 Sep 2026)

Rick, 25 Sep: "i want this fucking engine fixed, i don't want drop outs of the god damn
api". On 25 Sep the watcher opened a connection around each batch of 90 historical
requests, a batch timed out at 07:30 and at 10:17, the feed fell back to Yahoo and nothing
brought it back. This is what replaced it (`src/asxbot/ibkr/live.py`, `ibkr/feed.py`).

## The connection

- **One connection for the watcher's lifetime**, opened read-only (client 41) on its own
  thread and asyncio loop. Orders are impossible three ways: the connection is read-only,
  every order method is replaced with one that raises, and `tests/test_ibkr_no_orders.py`
  fails the build if an order call appears in the package.
- **Heartbeat**: `reqCurrentTime` every 30 s (`ibkr.heartbeat_s`). Two answers missed in a
  row, a socket error, ib_async's own "no message for 2 minutes", or IBKR saying the link is
  lost (1100, 2110) drops the connection; it reconnects with backoff 1, 2, 4 ... 60 s until
  Gateway answers again, however long that takes.
- **Every subscription lives in a registry** and is re-requested after every reconnect and
  after "connectivity restored, data lost" (1101). The minutes the stream missed come from
  one small history request per stock ("today since the last minute held").
- **Codes acted on**: 1100 lost, 1101/1102 restored, 2103/2105/2157 farm down, 2104/2106/2158
  farm up, 2110 Gateway's link broken, 101 line limit, 162/420 pacing, 354/10089/10167/10168
  no subscription, 200 no such security, 502/504 not connected, 10197 competing session.

## Streaming instead of polling

Bars are **5-second real-time bars** (`reqRealTimeBars`, TRADES) aggregated into 1-minute
bars in code, stamped with the minute they start. A minute closes when a bar for a later
minute arrives, or 12 s after it ends if nothing later has come. Only closed minutes are
handed to the playbooks, so a decision never sees a forming bar. A quote (`reqMktData`:
bid/ask/last/sizes, open, previous close, halt flag, auction indicative price and volume)
is taken on demand and its line given back at once.

### The market data line limit

IBKR allows a set number of simultaneous subscriptions (100 on a plain account). The code
starts from `ibkr.market_data_lines` (100) and **learns the real number from IBKR**: error
101 ("max number of tickers") names the request that did not fit, that subscription is
dropped, and the limit becomes the number that did fit. `stream_reserve_lines` (6) are kept
back for on-demand quotes.

### The rotation

The playbooks need ~280 stocks (the day trader's liquid universe) plus the index and the
day's news stocks, at 1-minute cadence where it matters. With ~94 usable lines:

| Tier | Who | Cadence | How |
|---|---|---|---|
| 0 | the ASX 200 index (XJO) | every minute | always streaming |
| 1 | every stock with a position or an order working in any arena book; every stock queued for a v2 reaction look; every stock with price-sensitive news since the last close | every minute | pinned by the watcher each cycle (`watch.pinned_codes`); streaming while pinned |
| 2 | the rest of the day trader's universe, ranked by an **interest score** the scan hands back each cycle (`daytrader.interest_score`: |move vs index| in 1% units + relative volume in 1.5x units + 1.5 for a new day high/low + 2 for a halt resumption + 1 for news), then by how long since it was last refreshed | every minute while it holds a line | fills the remaining lines; a slot is held at least `min_dwell_s` (120 s) so nothing churns |
| polled | whatever has no line | every ~6 minutes (`poll_per_cycle` 30 a cycle, stalest first, never the same stock inside 2 minutes) | one small history request each ("today since the last minute held") through the paced queue |

What this means for the setups: gap-and-go, the opening-range breakout and the VWAP
reclaim fire on stocks that are already moving against the index, trading heavily, or at
their day's high or low - exactly what the interest score streams. A quiet stock that
breaks out from nowhere is seen at its next poll; if that is more than
`max_signal_age_bars` (5) bars after the trigger, the setup is recorded as stale and not
taken, as before. The rotation's numbers go to `events/ibkr_rotation.jsonl` every 10
minutes and to `data/arena/live_data.json` every cycle.

## History before the open

From 07:30 the feed queues one request per stock for the prior sessions ("6 D" of 1-minute
bars, ending at midnight) through the **paced queue**: 4 in flight, at least 0.25 s apart,
never more than `max_requests_per_10min` (600) in ten minutes, retried three times with
backoff, and a pacing violation (162 "pacing", 420) pauses the queue for 30 s. Answers are
cached on local disk (`%LOCALAPPDATA%\asx-bot\ibkr\history\<code>\<day>.parquet`), so a
restart fetches nothing twice. At **09:55** (`daytrader.history_deadline_report`) the
stocks still without history are named in the log, told to Rick in one line, written to
`data/arena/daytrader/<day>.json` and `events/ibkr_history.jsonl`, and **left out of the
day's scan** rather than stalling it.

## Never trade on stale data

`FailoverFeed.entries_allowed(now, codes)` answers "no", and why, when:

- the connection is down or the heartbeat is stale (`ready` is False);
- IBKR is sending delayed data rather than real-time (the probe quote every 10 minutes);
- in market hours, the index's streamed bars or a named stock's are older than
  `max_data_age_s` (60 s).

On "no": the day trader does not scan (nothing can trigger; a trigger that happened
meanwhile is stale by the time the feed is back), v2's reaction looks wait (a window that
closes meanwhile is recorded as missed, never quiet), the v2 rule bot waits (its 11:15
deadline records the miss with the reason), a pre-open look is skipped rather than made on
Yahoo, and an announcement that arrives with no quote is deferred and screened again each
cycle until the feed is back. The log says `paused: live feed down (...)` once per reason
and `resumed` when it is back; `data/arena/live_data.json` carries `entries: paused`; the
`live_data` self-check shouts in market hours.

**Exits are never paused.** The broker works stops, targets and trailing from the best bars
it holds: the live minutes as they complete (`arena/minutes.py` live overlay, so a stop
fires in the minute it is reached rather than ~20 minutes later), and Yahoo's delayed bars
behind them only while the stream is down. Yahoo is never the source of an entry decision.

## The supervisor

IB Gateway itself is kept up by the supervisor task (docs/ibgateway.md). Its state is read
by the `gateway_supervisor` self-check: one Telegram line when Gateway needs Rick (the
phone approval or a login on the PC), one when it is back.

## Proof

- `tests/test_ibkr_live.py`: against a fake IB that misbehaves on demand - refused
  connections, missed heartbeats, a dropped socket, 1100 then 1101, a pacing violation,
  history that never comes, the line limit, stale bars, the pause and the resume.
- `scripts/ibkr_live_chaos.py`: the same against the live Gateway (client 45, read-only,
  frozen data): connect and hold, subscribe, a paced history burst, an unknown code, the
  socket dropped from our side and the connection back with every subscription
  re-requested, a frozen quote. Its record is `reports/ibkr_chaos_<stamp>.json`.
