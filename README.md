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
| `data/` | Prices, announcements, events, logs. Gitignored. Copy by hand when moving PCs. |
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

## Commands the trader agent calls

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
| `asxbot announcements poll` | Live poller loop (07:30-19:30 Sydney, trading days). Run it as its own process. |
| `asxbot announcements history --universe asx300` | Resumable archive builder. Run detached; Ctrl-C safe. |
| `asxbot backtest` | Phase 1 backtest to `reports/phase1.md`. |

## OpenClaw trader agent

Set up a **separate** OpenClaw agent for trading. Leave existing agents untouched. Do not install
third-party ClawHub add-ons on it.

1. Create the agent with its own workspace (for example `~/.openclaw/agents/asx-trader/`) and its
   own Telegram bot token. Its workspace instructions should say: "You propose and explain ASX
   trades from `asxbot scan` and `asxbot proposals`. You never decide whether an order was
   approved, placed or filled; only the broker's returned order id and fill count in the
   `place_order` output say that."
2. Give it exec access to exactly these commands, with the venv path:
   `C:\venvs\asx-bot\Scripts\asxbot.exe scan|proposals|positions|reconcile|daily_report|place_order ...`
3. **Exec approvals:** set the agent to *ask on every command* (never "always allow"), and set
   it to *deny* when no approval channel is reachable. `place_order` puts ticker, qty and limit
   in the command line, so the approval prompt on Telegram shows exactly what is being approved.
4. Daily routine the agent runs (each step is a command you approve): `scan` during the session
   when the poller reports a new price-sensitive announcement, `proposals` to show you the
   candidates, `place_order` only after you say yes to a specific proposal id, `daily_report`
   after the close.
5. The agent may read and summarise a proposal's announcement PDF (`data/announcements/pdf/`)
   when explaining a live proposal. It must not classify historical announcements.

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

Each playbook gets two simulated $10,000 accounts: one traded by the agents, one by a
rule-based yardstick bot that runs the same playbook's plain rule with no model at all.
The gap between them is the measure of what the agent's judgment adds.

```powershell
asxbot arena status                 # playbooks, levels, accounts
asxbot arena scoreboard             # P&L, green/red days, drawdown, agent vs bot
asxbot arena positions              # open positions and pending fills
asxbot arena quote --ticker BHP     # the delayed quote and the price reaction
asxbot arena dossier --ticker BHP   # one-page brief on a stock
asxbot arena resolve                # fill pending orders, trigger stops
asxbot arena mark                   # mark every account to market
asxbot arena report --agent --send  # the evening report, on Telegram
asxbot arena watch --until 19:25    # the warm-up watcher (the scheduled task runs this)
asxbot arena reset --yes            # wipe accounts back to their opening balance
```

### Deferred fills

Free quotes are about 20 minutes delayed, so the true price at the moment of a decision is
not knowable when the decision is made. Every arena order is therefore recorded pending
with its decision timestamp and filled later from the 1-minute bar covering that minute. If
the stock did not trade in that minute, the fill walks **forward** to the next minute that
did — never back to an earlier one. Stops and exits follow the same rule, and a stop gapped
through fills at the bar's open rather than at the stop price.

### The two agents

`trader-reader` (Sonnet 5) reads each announcement and writes a quotable summary ending in
a TRADE_WORTHY verdict. Code hands that summary to `trader-decider` (Opus 5), which works a
fixed checklist and returns a DECISION block. Code then calls `arena_place_order`, which
enforces every limit — risk per trade, open positions, leverage, the daily loss limit,
ASX-200-only shorts — and the broker's returned order id is the only proof an order exists.

A malformed reply from either agent is treated as "pass", so a broken answer costs a trade
and never causes one.

### Telegram

The trader bot is separate from any other OpenClaw agent's bot. Its token lives in `.env`.
To link it: message the bot on Telegram, then `asxbot telegram pair`.
