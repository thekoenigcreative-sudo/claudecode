# ASX trading assistant — read this first

Personal trading assistant for my own ASX account. **PLAN.md is the plan in plain English and sets the build order — read it first.** **SPEC.md is the single source of truth — read it fully before any work.** **STRATEGIES.md holds the plain-rule versions, now used as yardsticks for the ARENA.md playbooks.** **ARENA.md is the fake-money arena in technical detail: every tactic is run by the AI agent, one at a time, in PLAN.md's order.**

**LEARNINGS.md is the record of what has actually broken here — read it before trusting any number in this repo, or assuming any component does what its name says.**

Rules for every session:
- Honest results over pretty ones. No curve-fitting. If there's no edge after costs, say so plainly.
- Broker mode is `sim` unless I change it myself. Live mode needs `broker: live` in `config.yaml` AND `LIVE_TRADING_CONFIRMED=yes` in `.env`.
- Orders only through `place_order`: plain code, hard limits. Real money: my approval on every call. Fake-money arena (ARENA.md): the AI agent trades on its own within the same limits. The agent never decides an order was approved, placed or filled — only the broker's returned order ID and fill count.
- Label every yfinance result "plumbing test — not a go/no-go".
- No AI classification of historical announcements.
- Announcement collector: low volume, cached, paced. If access is refused, stop and fall back to price/volume signals.
- Secrets only in `.env` (gitignored). Bulky data only in `data/` (gitignored).
- Test each stage before the next; commit after each working stage.
- The repo is in Google Drive: keep the virtual environment outside Drive (one per PC) and no SQLite or other live database files inside the repo.
- Never start IB Gateway or IBC from a Claude Code shell or any terminal: on 25 Sep a Claude desktop update at 08:58 killed the Gateway a Claude session had started. It runs only under the task "ASXBot IB Gateway", started by the "ASXBot IB Gateway Supervisor" task (docs/ibgateway.md). Never read, type or store Rick's IBKR password; he stores it himself with ibkr_login_setup.cmd.
- Never start the watcher (`asxbot arena watch`) from a Claude Code shell or any terminal that might be closed. Always start it through its scheduled task: `schtasks /run /tn "ASXBot Arena Warmup"`. A watcher tied to a window dies with that window. On 23 Sep a window was closed at 13:32, the watcher went with it, and nothing reported it.

## The Trader chat (@rick_asx_trader_bot)

- Rick's Telegram chat with the Trader is this repo's own receiver, `asxbot chat` (src/asxbot/chat.py; docs/chat.md). It runs hidden under the scheduled task "ASXBot Chat" (scripts/chat.pyw, logging to %LOCALAPPDATA%\asx-bot\logs\chat.log) and answers only Rick. Restart it with `schtasks /end /tn "ASXBot Chat"` then `schtasks /run /tn "ASXBot Chat"`; never run `asxbot chat` from a terminal (a closed window kills it). `asxbot chat --probe "text" [--no-agent]` tries one message without Telegram.
- Never add this bot back to OpenClaw as a channel (openclaw.json `channels.telegram.accounts.trader`). Two programs polling one bot take each other's messages; the chat refuses to start while that account exists.
- Commands are handled in code by src/asxbot/botctl.py, a VERBATIM copy of C:\Users\Richa\.cc-jobs\changes\botctl.py (shared by all Rick's bots). Never edit the copy; change the master and re-copy it (`python sync_botctl.py trader` there). Its process hook is pointed at asxbot.proc by chat.py.
- `/model` and `/think` in the chat are STRATEGY CHANGES: they change the OpenClaw agent and write the new expectation into config.yaml (`arena.agents.models` / `arena.agents.effort`) with a dated entry in `arena.agents.history`, committed on its own (src/asxbot/arena/settings_history.py). The evening report lists them. What the agents are expected to run on lives in config.yaml; watch.py's READER_MODEL/DECIDER_MODEL are fallbacks only.
- Nothing in the chat can place, change or approve an order.

## Verification

- Check the artefact, not your own account of it. A commit message, a passing test count and a summary of what you changed are not evidence the thing works. Before saying something works, look at the file it wrote, the log line it produced, or the response it got.
- A fetch that returns the wrong content type fails loudly and is never cached. Check the magic bytes, never hand an unverified body to a parser, and never let an existing file short-circuit that check.
- When a rule rejects something, compute what it excludes, not just how often it fired. A count of rejections is not evidence a filter is working.
- When you find a defect, audit the rest of that file and its neighbours in the same pass. Do not deliver defects one at a time across sessions.
- Never change a threshold, parameter or rule after seeing a result. If one must change, date it in `config.yaml` with the reason and label it a deliberate change, not a finding.
- A result found by slicing the data after the aggregate failed is a hypothesis, not a conclusion. It earns a holdout run, nothing more.
- Before comparing two things, check they operate under the same rules.
- When you are waiting on something you shipped, verify it completed rather than assuming it did.
- If you cannot verify a claim, say so plainly instead of stating it.
