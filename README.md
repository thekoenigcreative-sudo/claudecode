# asx-bot

Personal ASX trading assistant. Finds post-announcement drift candidates, proposes each trade with
its reasoning, and the human approves every order on Telegram. Never fully automatic.

**SPEC.md is the single source of truth.** Read it before touching anything.

## Setup (Windows, one venv per PC, outside Google Drive)

```powershell
uv venv C:\venvs\asx-bot --python 3.12
C:\venvs\asx-bot\Scripts\activate
uv pip install -e ".[dev]"
copy .env.example .env      # then edit .env by hand
asxbot check
pytest
```

Optional extras, installed only when needed:

```powershell
uv pip install -e ".[ibkr]"     # Phase 2, IB Gateway
uv pip install -e ".[norgate]"  # when the Norgate Data Updater is installed on this PC
```

## Layout

| Path | What |
|---|---|
| `config.yaml` | All settings. Strategy, baseline, cost and universe parameters were frozen on 2026-09-22. |
| `.env` | Secrets and the live-trading confirmation. Gitignored. Template in `.env.example`. |
| `data/` | Prices, announcements, events. Gitignored. Copy by hand when moving PCs. |
| `%LOCALAPPDATA%\asx-bot\logs` | The logs (`asxbot.log`, the launchers' logs, the watchdog's), rotated daily. On a local disk because Google Drive cuts off long-open files (LEARNINGS #19). The evening routine copies them to `data/logs/`, which is a copy, not the live log. |
| `reports/` | Backtest reports (`phase1.md`). |
| `src/asxbot/` | The package. |
| `tests/` | pytest. Parser tests run against saved sample pages. |

## Broker modes

`broker: sim` is the default and the only mode in this build that can run. `paper` needs the IBKR
adapter. `live` refuses to start unless `config.yaml` says `broker: live` **and** `.env` has
`LIVE_TRADING_CONFIRMED=yes`, both set by hand.

## Data honesty

Every result from yfinance is labelled **"plumbing test - not a go/no-go"**. yfinance has no delisted
stocks and no point-in-time index membership. Only Norgate Platinum data counts for a go/no-go.

## Stages

See SPEC.md section 9. Each stage is tested and committed before the next starts.

## Commands (the real-money path, sim only)

All through the `asxbot` CLI in the venv. Every command prints active alerts first.

| Command | What it does |
|---|---|
| `asxbot scan` | Fetch today's announcements once, check price-sensitive ones against the live reaction rule, write proposals to `data/proposals/`. |
| `asxbot proposals [-v] [--all]` | List pending proposals with id, ticker, qty, limit, stop, dollar risk. |
| `asxbot place_order --id P-... --ticker XXX --qty N --limit P` | The ONLY order path. Hard limits in code. Prints the broker's order id and fill. |
| `asxbot place_order --side sell --ticker XXX --qty N --limit P` | Close (part of) an existing position. Long-only: never more than held. |
| `asxbot positions` | Broker cash, positions, open orders. |
| `asxbot reconcile` | Broker positions vs the fills log. Raises an alert on mismatch. |
| `asxbot daily_report` | One-screen summary: alerts, cash, positions, today's counts, reconciliation, archive status. |
| `asxbot dryrun [--unwind]` | Sim-only end-to-end exercise with a fake announcement and fake quote. |
| `asxbot announcements poll` | Standalone poller for testing; never while the watcher runs. |
| `asxbot announcements history --universe asx300` | Resumable archive builder. Run detached; Ctrl-C safe. |
| `asxbot backtest` | Phase 1 backtest to `reports/phase1.md`. |

## The trader agents and the Trader chat

The Trader chat (@rick_asx_trader_bot) is this repo's own receiver, `asxbot chat`
(docs/chat.md). The arena agents are called by code (arena/agents.py). No agent has shell
access to an order command. Real-money `place_order` approvals are not set up.

The two arena agents, `trader-reader` and `trader-decider`, are their own OpenClaw agents
(docs/agents/). Leave the other OpenClaw agents untouched, install no ClawHub add-ons on
these, and never add the trader bot back to OpenClaw as a channel. No agent classifies
historical announcements.

Broker mode stays `sim` until you change `config.yaml` yourself. `paper` needs IB Gateway and
`uv pip install -e ".[ibkr]"`. `live` additionally needs `LIVE_TRADING_CONFIRMED=yes` in `.env`.

## Alerts

`data/alerts/*.flag` files are raised by code and shown at the top of every command. The two that
matter: `collector_access_refused` (asx.com.au refused us: collection has stopped, the system
runs on price/volume signals only; do not work around it) and `reconcile_mismatch` (broker and
log disagree). Clear by hand with `asxbot alerts-clear <key>` once you understand the cause.

## The arena (fake money)

Every tactic is run by the AI agent in a fake-money arena before any real money is
considered. See ARENA.md for the design and PLAN.md for the order tactics are built in.

Each playbook gets two simulated $20,000 accounts ($10,000 until 23 Sep 2026): one traded by the agents, one by a
rule-based yardstick bot that runs the same playbook's plain rule with no model at all.
The gap between them is the measure of what the agent's judgment adds.

```powershell
asxbot arena status                 # playbooks, levels, accounts
asxbot arena scoreboard             # P&L, green/red days, drawdown, agent vs bot
asxbot arena positions              # open positions and pending fills
asxbot arena quote --ticker BHP     # the quote (IBKR real-time; Yahoo's delayed one while Gateway is down) and the price reaction
asxbot arena dossier --ticker BHP   # one-page brief on a stock
asxbot arena resolve                # fill pending orders, trigger stops
asxbot arena mark                   # mark every account to market
asxbot arena report --agent --send  # the evening report, on Telegram
asxbot arena watch --until auto     # the watcher: only through its scheduled task, never from a terminal (CLAUDE.md)
asxbot arena reset --yes            # wipe accounts back to their opening balance
```

### Deferred fills

Decided on 22 Sep, when the arena ran on free quotes about 20 minutes delayed, so the true
price at the moment of a decision was not knowable when the decision was made. Since 25 Sep
decisions use IBKR real-time prices (docs/ibkr_live_data.md); the rule stands. Every arena
order is recorded pending,
stamped with the time it was recorded (`decided_at`) and the time its data was read
(`data_as_of`), and filled later from the first 1-minute bar that starts strictly after
`decided_at`. If the stock did not trade in that minute, the fill walks **forward** to the
next minute that did — never back to an earlier one. Stops and exits follow the same rule, and a stop gapped
through fills at the bar's open rather than at the stop price.

Since 2026-09-24 fills also respect volume (`arena.fill.max_volume_share`, 20%): no bar fills
more than that share of the shares it traded, and the rest carries to later bars at their
prices. An order someone placed is a day order - whatever is unfilled at the end of its
session expires (`partial`), and the position keeps what filled. A stop or target exit keeps
working into the next session until the position is out. The stop always gets a bar's volume
first. A bar is used only once it is final (it has ended and is not the newest row of an
intraday fetch). The full rule is in `ArenaBroker.work` (`arena/broker.py`).

Since 2026-09-24 an order recorded before 09:59 fills at the **opening auction**, not at the
first traded minute (the minute feed leaves the auction out, TRACKER #28). The auction is
priced at Yahoo's daily open, checked but not confirmed against the ASX
(`reports/auction_open_check.md`, rerun with `scripts/check_auction_open.py`), and no more than
`arena.fill.auction_volume_share` (20%) of its estimated volume fills there, the rest in the
minute bars. Stops and targets the auction gaps through fill at the auction price. A day with
no auction price the arena trusts fills at the first traded minute, and the fill says so.
`arena.fill.opening_auction: first_minute` restores the old rule.

### The two agents

`trader-reader` (Sonnet 5) reads each announcement and writes a quotable summary ending in
a TRADE_WORTHY verdict. Code hands that summary to `trader-decider` (Opus 5.5), which works a
fixed checklist and returns a DECISION block. Code then calls `arena_place_order`, which
enforces every limit — risk per trade, open positions, leverage, the daily loss limit,
ASX-200-only shorts — and the broker's returned order id is the only proof an order exists.

A malformed reply from either agent is treated as "pass", so a broken answer costs a trade
and never causes one.

### Telegram

The trader bot (@rick_asx_trader_bot) is separate from any other bot. Its token lives in
`.env`. To link it: message the bot on Telegram, then `asxbot telegram pair --chat-id <id>`
only; without --chat-id it competes with the chat for the bot's messages.
