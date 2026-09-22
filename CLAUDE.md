# ASX trading assistant — read this first

Personal trading assistant for my own ASX account. **PLAN.md is the plan in plain English and sets the build order — read it first.** **SPEC.md is the single source of truth — read it fully before any work.** **STRATEGIES.md holds the plain-rule versions, now used as yardsticks for the ARENA.md playbooks.** **ARENA.md is the fake-money arena in technical detail: every tactic is run by the AI agent, one at a time, in PLAN.md's order.**

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
