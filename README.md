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
