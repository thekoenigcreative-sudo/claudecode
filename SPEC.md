# ASX trading assistant — build spec (rev 4)
*22 Sep 2026. Single source of truth for this repo. Supersedes all earlier drafts.*

## 1. What this is
A personal trading assistant for my own ASX account. It finds trade ideas, proposes each one with its reasoning, and I approve every trade on Telegram. It is never fully automatic.

**Build the whole system now.** Orders run against a simulated broker until I open an IBKR account (paper phase) and later authorise live trading. Nothing in this build can place a real order.

## 2. Working rules
- **Honest results over pretty ones.** If a strategy doesn't beat the benchmarks out-of-sample after costs, say so plainly — that's a successful result. No curve-fitting.
- **Build in stages** (section 9). Test each stage before starting the next. Commit after each working stage.
- **Portable from day one:** dependencies in `requirements.txt` (or `pyproject.toml`), settings in `config.yaml`, secrets only in a gitignored `.env` (commit a `.env.example`), bulky data in a gitignored `data/` folder. The repo must move to another Windows PC with a clone plus a copy of `data/`.
- **Log everything:** every announcement seen, signal, proposal, approval, order and fill, with timestamps.
- **Location:** the repo lives in Google Drive (`G:\My Drive\asx-bot`) and syncs between my PCs. Create the Python virtual environment outside Google Drive, one per PC (a synced environment breaks across machines). Don't keep SQLite or other live database files in the repo, because sync can corrupt them; use Parquet or CSV files written atomically. Keep the announcement history to metadata and fetch PDFs only when needed, so Drive storage stays small.

## 3. The strategy
**Post-announcement drift, long-only.** When an ASX company releases a price-sensitive announcement and the market reacts strongly, the move often continues over the following hours to days. The system aims to catch that continuation.

**Event definition (backtest):** a price-sensitive announcement plus a reaction: an up-gap of at least **X%** relative to the ASX 200's move that day, on volume at least **Y×** the stock's 20-day average. **Fix X and Y before looking at any results** and record them in `config.yaml` with the date they were fixed.

**Second strategy (also the live fallback):** the same price/volume event without the announcement filter. Name it honestly: "drift after a volume-confirmed surprise".

**Baseline to beat:** cross-sectional momentum with a 200-day index regime filter.
**Benchmark:** ASX 200 total return (dividends included).

**Entry (daily-data backtest):** announcements released before the 10:00 open enter at that day's open; later ones enter at the next day's open.
**Exit:** rules defined before results — at minimum a time-based exit and a stop-loss.

**Universes — test both, report separately:**
- (a) the ASX 300 (point-in-time membership once Norgate is in)
- (b) smaller stocks outside it

Apply a minimum daily dollar-turnover floor to both, so results only include stocks I could actually trade.

**Live design:** intraday. The collector sees a new price-sensitive announcement, the scanner checks the live price and volume reaction, a proposal reaches me within minutes, I approve on Telegram, and entry happens the same day. The next-open entry above is only a limit of daily data.

## 4. Data
**Prices — swappable provider layer:**
- **Now: `yfinance`** (`.AX` tickers). It lacks delisted stocks, so label every result from it **"plumbing test — not a go/no-go"**.
- **Later: Norgate Data**, Australian Stocks, Platinum tier, via the `norgatedata` package (works with the Norgate Data Updater app on Windows). It adds delisted stocks and point-in-time index membership. Not set up yet: build the adapter behind the same interface so it drops in without rewrites. The free trial has only 2 years of history, so trial results are a dry run, not a go/no-go.

**Announcements — collector for asx.com.au listings.** For each announcement: ticker, release date and time, headline, type, price-sensitive flag, PDF link.
- **History:** build the archive once for both universes (the site holds announcements back to November 2002). Resumable, runs in the background, stores everything in `data/`.
- **Live:** on trading days, check the day's announcement listing about once a minute during announcement hours (7:30am–7:30pm Sydney time). Fetch PDFs only for price-sensitive items in the universes.
- **Collection rules:** low volume, personal research use. Use a descriptive user agent, pause between requests, cache everything, never re-download what's cached, and back off on errors.
- **If the page layout changes:** repair the parser, with tests against saved sample pages.
- **If access is refused:** stop collecting, alert me, and keep the system running on the price/volume fallback. Don't work around refused access.

**Classifying historical announcements:** mechanical only — price-sensitive flag, announcement type, headline keywords, price reaction. **No AI classification of historical announcements**: a model may already know what happened next, which would leak the future into the test. Live, the trader agent may read and summarise announcements for proposals.

## 5. Costs and sizing
- **Starting capital and max concurrent positions:** set in `config.yaml` (values given in my opening message).
- **Position size:** capital ÷ max positions, equal weight, unless a different rule is justified before seeing results.
- **Brokerage:** IBKR Australia fixed pricing — **0.088% of trade value, $6.60 minimum per order, GST included** — on both buy and sell.
- **Slippage:** scaled to liquidity. Rerun every backtest at **2× slippage**.

## 6. Testing safeguards (mandatory)
1. **Survivorship:** results only count on data that includes delisted stocks (Norgate Platinum). yfinance = plumbing test.
2. **Look-ahead:** act only on information available at the time — the announcement's release time, then the next tradeable price.
3. **Overfitting:** parameters fixed before results. Hold out the most recent 3 years untouched until parameters are frozen; walk-forward on the rest. This needs full history — on yfinance or the 2-year Norgate trial, report a dry run only. Flag any result that rests on a handful of trades.

## 7. Order handling rule (applies to simulated, paper and live orders)
An assistant once reported an action it never took, so the AI never has the final say on orders:
- A separate **OpenClaw "trader" agent** (its own workspace and its own Telegram bot) runs the scans, writes proposals, explains them and answers my questions. Leave existing OpenClaw agents untouched.
- **Orders go only through `place_order`** — plain code, not the agent. It enforces the limits in code and refuses anything outside them: max position size, max open positions, max new positions per day, long-only, limit orders only, allowed universe, allowed hours.
- **Every `place_order` call needs my approval** through OpenClaw's exec approvals: ask on every call, deny if no approval channel is reachable, and never set the trader agent to run commands without asking. Put ticker, quantity and limit price in the command itself so the approval prompt shows exactly what I'm approving.
- `place_order` returns the broker's order ID. **The agent never decides an order was approved, placed or filled** — only the broker's returned order ID and fill count.
- A daily reconciliation compares broker positions with the log and alerts me to any mismatch.
- **Broker modes:** `sim` (default in this build), `paper` (IBKR paper, Phase 2), `live` (Phase 3). Live mode requires both `broker: live` in `config.yaml` and `LIVE_TRADING_CONFIRMED=yes` in `.env`, both set by me; otherwise the code refuses to start in live mode.
- Don't install third-party ClawHub add-ons on the trader agent.

## 8. Tech stack
Python 3.11+, `venv` or `uv`, `ruff`, `pytest`. Data: `yfinance` now, `norgatedata` later. Engine: `vectorbt` or plain pandas — **not `backtesting.py`** (built for one instrument at a time). Broker adapter: `ib_async` (not `ib_insync`). Telegram: through the OpenClaw trader agent's own bot. Windows-first (Norgate runs on Windows).

## 9. Build order
1. **Skeleton:** project layout, `config.yaml`, `.env.example`, logging, `README.md`, test setup.
2. **Data layer:** price provider interface, yfinance adapter, Norgate adapter (ready to enable), universe builder with the turnover floor, ASX 200 total-return benchmark series.
3. **Announcements collector:** history archive (resumable, background) and live poller, with parser tests on saved sample pages.
4. **Backtester and report:** both strategies plus the baseline, costs, the 2× slippage rerun, both universes → `reports/phase1.md`.
5. **Live scanner:** turns a new price-sensitive announcement plus its price reaction into a proposal (ID, ticker, entry limit, stop, size, dollar risk, reasoning, announcement link), saved to the log.
6. **`place_order` and the simulated broker:** limits in code, realistic simulated fills including slippage, order IDs, positions, reconciliation.
7. **OpenClaw integration:** command-line scripts the trader agent calls (`scan`, `proposals`, `place_order --id --ticker --qty --limit`, `positions`, `daily_report`), plus a README section on setting up the trader agent and its approval settings.
8. **End-to-end dry run in sim:** collector → scanner → proposal → my approval → `place_order` → simulated fill → log → reconciliation.
9. **IBKR adapter** (`ib_async`, IB Gateway) behind the same broker interface, disabled until I have an account; paper mode first.

## 10. Report — `reports/phase1.md`
Per strategy, per universe, at 1× and 2× slippage: trade count, win rate, average trade, CAGR, max drawdown, turnover, exposure, results by year, in-sample vs out-of-sample, against the baseline and the benchmark. Flag small samples. End with an honest go/no-go — or "plumbing test only" when the data is yfinance or the trial.

## 11. After this build
- **Phase 2 — paper:** IBKR account and paper account, IB Gateway, IBKR "ASX Total" real-time data ($25/month, needs at least US$500 in the account), broker mode `paper`, trader agent live on Telegram. Go live after about 30 paper trades whose fills and results track the backtest.
- **Phase 3 — live:** small real capital; scale only on performance.

## 12. Before writing any code
Restate the plan in your own words, list the packages you'll install and why, and flag anything you think is wrong, risky or likely to overfit. Then wait for my go.
