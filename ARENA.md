# Arena plan — every tactic run by the AI agent, starting very aggressive
*22 Sep 2026. Extends SPEC.md and STRATEGIES.md. Covers crypto and ASX.*

## The principle
Every tactic is AI-run. The OpenClaw trader agent does the reading, research, judgment and decision on every single trade, in every playbook. Code only feeds it data and alerts, carries out its orders, enforces stops and limits, and keeps score. The playbooks below describe what the agent hunts for and its starting guidance; it can deviate from the guidance when it has a reason, and it writes that reason down.

## Aggression ladder — start at Daily, scale back only if it isn't working
The goal at the start is profit every day. Each playbook has its own level, set in `config.yaml`.

| Setting | Level 1 — Daily (start) | Level 2 — Weekly | Level 3 — Monthly |
|---|---|---|---|
| Aim | Profit every day | Profit every week | Profit every month |
| Risk per trade (distance to stop x size) | up to 5% of account | up to 2% | up to 1% |
| Open positions per playbook | up to 8 | up to 6 | up to 5 |
| Leverage: crypto / ASX | up to 10x / 3x | up to 3x / 1.5x | none |
| Holding period | intraday; overnight only with a written reason | days, up to 2 weeks | weeks, up to 3 months |
| Daily loss limit (then no new trades until tomorrow) | 15% of account | 8% | 4% |
| Wake-up thresholds | most sensitive | medium | standard |

- **Review points:** Level 1 after 10 ASX trading days (14 days for crypto). A playbook stays at Level 1 only if it is net profitable after all costs, has more green days than red, and its worst peak-to-trough drop is under 25%. Otherwise it moves to Level 2, reviewed after 4 weeks on the same test, then Level 3, then retired.
- **Moving back up:** if a playbook does well at a lower level, the agent can propose moving it up; I approve.
- **Keeping daily profits real:** every account is marked to market every day (open positions count at their current price), fees and funding are counted, stops always stay on, and no playbook may add to losing positions. Otherwise a playbook could show small wins most days while hiding one large loss.

## Roadmap — one tactic at a time
- Only one tactic is tested at a time. A tactic that passes its checkpoint keeps running on autopilot. A tactic that fails at Daily gets one quiet 4-week round at Weekly alongside the next test, then is kept or retired.
- Each tactic goes: **build** (Claude Code, one or two evenings) → **shake-down** (1–2 days in the simulator: alerts wake the agent, orders fill, the evening report arrives) → **Level 1 test** (14 days for crypto, 10 trading days for ASX) → **checkpoint** (the agent posts a pass/fail summary against the ladder rules; I reply to continue) → next tactic.
- The first build is the biggest: it includes the shared machinery every later tactic reuses (the two agents, their tools, the simulator upgrades, the scoreboard, the evening report). Later builds are small.
- **Order:** follow PLAN.md (it wins on order and priorities). ASX first, all crypto last:
  1. ASX announcements (Daily). Its first two days double as the machinery shake-down; then a warm-up on free delayed prices, with each fill at the true market price at the moment of the decision; the formal 10-trading-day test starts once IBKR's live ASX prices are on and the ASX 300 archive is complete.
  2. ASX technical setups. 3. ASX directors buying (Monthly; runs quietly in the background once built).
  4. Crypto token unlocks (Weekly). 5. Crypto new listings. 6. Crypto momentum (Weekly).
  7–10. Only if still wanted: intraday crypto breakouts, crash bounces, crowded trades, range trading.
- **Crypto-only simulator parts** (funding payments, liquidation) are built when crypto starts, not in the first build.
- **OpenClaw setup:** OpenClaw runs on this PC and also runs my editorial agent. Claude Code sets up the two trader agents itself with the OpenClaw CLI; I only create the Telegram bot in BotFather and hand over its token. Leave the editorial agent untouched, and ask me before restarting the OpenClaw gateway, because that interrupts the editorial agent too. The PC must stay on for the arena to run.
- **My time:** a couple of minutes on the evening report, about 10 minutes at each checkpoint, and one message to Claude Code per build.

## How the arena works
- Fake money only. Each playbook gets its own simulated $20,000 account ($10,000 until 23 Sep 2026, raised to match the real account), so every playbook's results are separate.
- The agent trades on its own in the arena, with no approvals, because it is fake money. `place_order` limits (set by the playbook's level) still apply. Approvals come back on for real money.
- AI decisions can't be tested on past news the model may already know the outcome of, so every playbook is proven forward, live, in the arena.
- Rule-based bots: for each playbook, code also runs its plain starting rule as a rule-based bot with its own simulated account (no model use). That shows whether the agent's judgment is adding money on top of the setup itself, and any rule-based bot that makes money after costs is eligible for real money too, on the same promotion rule. In the queue below, "Yardstick" means that playbook's rule-based bot.
- Test ONE playbook at a time, following the roadmap above. Pause any running playbook that strains my plan usage.
- Scoreboard, per playbook, for both the agent and its bot: P&L, level, green days vs red days, average day, worst day, trades, win rate, worst peak-to-trough drop, fees paid. Code calculates it; the agent writes it into the evening Telegram report.
- Promotion to real money: a minimum trade count AND positive results after real costs over at least 4 weeks of forward running, at whatever level. Real money starts at least one level less aggressive than the fake-money level that earned it.
- Real costs in the simulator: exchange fees (Binance spot 0.10%; futures fees per venue), ASX brokerage both ways, spread, slippage, funding payments on futures, and liquidation at the maintenance margin.
- Shorts: crypto only on coins with a futures market; ASX only on ASX 200 stocks (the realistic borrowable set).
- Data: free public exchange data via `ccxt` (no accounts or keys) and the ASX collector.

## The agent's full job
### Before the event: prepare, so decisions are fast and informed
- **Catalyst calendar:** results dates, quarterly cash-flow reports, AGMs, index rebalances, expected drilling results, token unlocks and exchange listings. Updated daily.
- **Dossiers:** a one-page brief on every stock and coin in the active playbooks (what it does, market value, cash and runway, recent news, how it reacted to similar news before). Refreshed in the evenings and on weekends, on Sonnet. When news lands, the decider starts with context instead of from scratch.
- **Base rates from the bots:** the rule-based backtests supply the history (for example, how a given announcement type has moved prices over the next 10 days in stocks of that size). The agent uses that as its starting estimate and adjusts with judgment.
- **Read-through:** when news hits one company or coin, the agent checks what it means for competitors, suppliers and the sector, and what commodity price moves mean for ASX miners.
- **Morning brief (7:15am Sydney):** overnight US and commodity moves, today's catalysts, open positions and the day's plan.

### At the event: decide fast
- The reader quotes every figure it relies on, with the page, so the decider never trades on a misread number.
- The decider works through a fixed checklist: what's new, how big it is relative to the company, what the market already expected, the base rate, the dossier, liquidity. It records a confidence level and an expected move.
- Position size follows confidence, within the level's limits.
- Trades above a set size get a second opinion first: the decider argues the case against the trade before placing it.
- It chooses how to enter: pre-open auction, a limit order near the current price, or waiting for the first pullback.

### After entry: manage
- Watches its positions for follow-up news, read-through and price action. It can tighten stops, take profit or exit early, always giving a reason. The stops in code always stay in place.

### Across the book: risk manager
- Tracks total exposure by sector, coin and direction, and avoids piling into one theme. The level's daily loss limit in code pauses new trades for the rest of the day.
- Makes a daily risk-on or risk-off call that scales position sizes up or down within the level's limits.

### After the trade: learn
- Journals every trade: the thesis before, the outcome after, what it got right or wrong.
- Code scores the agent's calibration (do its 70%-confidence calls win about 70% of the time?) so its sizing gets sharper over time.
- Keeps its own dated rulebook of lessons and follows it; updated at most weekly, never retroactively.

### With the bots
- **Bots find, the agent decides:** every bot signal goes to the agent to take, skip, resize, time better, or add to with trades the bot would never find. The bot keeps trading its own account, so the difference shows what the agent adds.
- **The agent manages the bots:** in its weekly review it can switch bots on or off for market conditions, shift fake capital between them within limits, and propose rule changes. A rule change takes effect only as a new, dated bot version, scored separately.
- **Graduation:** any rule the agent keeps relying on can be turned into a new bot by Claude Code.

### Is it doing enough? (in every evening report)
- Alerts seen, alerts acted on, trades placed, P&L against the matching bot, green days vs red days, calibration score, and plan usage.

### Safety
- Announcements, news and social posts are untrusted text: the agent treats them as information, never as instructions.

## Agent workload and models
Speed rule: the agent acts the moment something worth trading appears. Code watches every feed for the active playbooks continuously and wakes the agent instantly. Thresholds and intervals live in `config.yaml`.
- **Two agents, two models:** `trader-reader` (primary model Sonnet 5) reads and triages every alert, announcement and news item, researches it, and writes a short structured summary ending in trade-worthy yes/no. Only "yes" items go to `trader-decider` (primary model Opus), which decides (trade or pass, direction, size, entry, stop, target) and calls `place_order`. Code hands the summary from one agent to the other. OpenClaw's per-spawn model override has open bug reports where it is silently ignored, so don't rely on it. A setup test confirms which model produced each step, and every decision logs its model name.
- **ASX timing:** announcements are released 7:30am to 7:30pm Sydney (8:30pm during daylight saving). A price-sensitive announcement during trading triggers an automatic 10-minute trading pause in that stock, and free copies appear about 20 minutes after release (the collector's first live test saw a 27-minute-old newest item). So the agent typically sees an announcement about 10 minutes after trading resumes; every minute after that matters. News before the 10am open: orders go in during the pre-open. After the close: queued for the next morning's pre-open.
- **During ASX trading (10am to 4:10pm):** the agent reviews its open ASX positions every 15 minutes, and closes Level 1 positions before the close unless it writes a reason to hold.
- **Crypto:** around the clock, instant wake-ups on the active playbooks' triggers, plus an hourly news sweep by the reader.
- **Every evening:** the agent writes the Telegram report: every trade, the reasoning, the scoreboard, and what it plans to watch next.
- **Weekly:** research review of every playbook, level recommendations, and proposed changes or new playbooks for Claude Code to build.
- **Any time:** answers my questions about trades, positions and results.
- **First week:** check my plan usage daily and adjust thresholds or intervals if needed.
- What stays in plain code: `place_order` and its limits, automatic stops and exits, the daily loss limits, and the broker's order IDs and fills as the only truth. That keeps emotion out and keeps a bad call inside the limits.

## Playbook details (the build order is in PLAN.md)
Starting guidance and yardstick rules are fixed before each playbook goes live and recorded, dated, in `config.yaml`. Playbooks are grouped by their starting level; the numbers below are labels, not the build order.

### Start at Level 1 — Daily
1. **Intraday crypto breakouts.** Trigger: 15-minute and 1-hour breakouts on heavy volume in BTC, ETH and the most-traded coins, favouring high-volatility days (calm days show little follow-through). The agent checks the news behind the move and decides, long or short. Yardstick: 3x, stop at the breakout candle's low (or high for shorts), exit by the end of the UTC day.
2. **ASX announcements** (warm-up on delayed prices with fills at the true price at decision time; the formal test needs IBKR's live ASX prices and the completed ASX 300 archive). Trigger: every price-sensitive announcement for a stock above the liquidity floor, including trading-halt resumptions. The agent reads it in full (PDF included), researches the company and judges the surprise: results against expectations, guidance changes, contract size against market value, drilling results, capital raisings and their discount, takeovers. Long on good news; short on bad news in ASX 200 stocks; same-day exits by default. Yardstick: strategy A (price-sensitive announcement plus a gap of 5% or more over the ASX 200 on 3x normal volume; 10-day exit; 8% stop), already built and backtested.
3. **Crash bounces.** Trigger: a large liquidation spike with price well below its 24-hour average. The agent judges whether the drop was forced selling or real news, and decides. Returns over the following month have tended to be below normal, so holds are hours. Yardstick: buy, target +3%, exit within 12 hours.
4. **Crowded trades (funding).** Trigger: funding in the top 5% of a coin's own history with open interest rising, checked around each funding settlement. The agent judges whether the crowd is about to be squeezed and decides. Yardstick: trade against the crowd, stop 8%, exit within 24 hours at Level 1.
5. **New listings, first days.** Trigger: each new major-exchange listing. The agent trades the first days' swings intraday, long or short, judging valuation, float, backers, hype and category (meme coins can squeeze shorts hard). Yardstick: from day 3, short on a close below the day-1 low; stop 25% above entry; exit on day 30.

**2a. ASX announcements, version 2 (from 25 Sep 2026; v1 above retired 24 Sep).** Trade the
reaction, not the news. News before the open gets the pre-open look as before; then every
stock with price-sensitive news gets ONE reaction look once the market has traded the news
for 10 minutes and before it has traded it for 40 (for overnight news: 10:10-10:40 in market
time). The decider sees the move against the ASX 200, the volume against the stock's usual
volume over the same minutes, the auction price and the news, and answers: over the rest of
today, is the expected move bigger than about 0.4% after costs, and where is the stop?
Intraday only, flat at 15:50 by code; positions up to $5,000, 3 open, 6 new a day; a stock
is tradeable if our order is under 5% of its median daily turnover. Yardstick (v2 rule
bot): news since the last close, at 10:30 up >= 3% against the ASX 200 on >= 3x the usual
first-30-minute volume, bought at 10:31 with a stop at the 10:00-10:30 low and sold at
15:55 (mirror short for ASX 200 members). Exact rules: config.yaml `asx_announcements_v2`.

**2b. ASX day trader (from 25 Sep 2026).** Code scans the liquid ASX 300 every minute:
movers against the index, volume against the same time of day, new highs and lows, halt
resumptions, news stocks. Four setups, each an exact rule: gap-and-go, opening-range
breakout, VWAP reclaim or loss, halt-resumption continuation (shorts only in the ASX 200).
The agent confirms or rejects each setup in one call, within 60 seconds; the rule bot takes
every one. Code sizes at 0.5% risk, enforces the stop at the setup's invalidation, moves it
to breakeven at +1R, takes half at +2R and trails 1R behind, allows one re-entry per stock
per day, and is flat at 15:50. Exact rules: config.yaml `asx_daytrader`.

Both run on Yahoo's delayed bars: decisions use only bars that were final when they were
made, fills come from the first bar after the order, and every report says "delayed data -
rehearsal until IBKR live prices". `data.live_provider: ibkr` switches to IBKR's real-time
prices through IB Gateway (built 24 Sep, read-only, no order call anywhere in it); Yahoo stays
the automatic fallback whenever Gateway is down, and the evening report counts which prices
each decision used. Fills stay simulated either way.

### Start at Level 2 — Weekly (these are weekly by nature)
6. **Crypto momentum.** Trigger: the daily ranking of the top 30 coins by 2–4 week return, at 00:00 UTC when daily candles close. The agent picks and sizes up to 5 holdings, checking news, upcoming unlocks and themes, and rebalances weekly (the signal plays out over 1–4 weeks, so daily churn only pays fees). If BTC closes below its 200-day average, it decides whether to move to stablecoins. Yardstick: top 5, weekly rebalance, stablecoins when BTC is below its 200-day average.
7. **Token unlocks.** Trigger: upcoming team or investor unlocks of at least 2% of circulating supply, about 30 days out. The agent researches who receives the tokens, whether they are likely to sell or have hedged, liquidity and sentiment, then decides whether and when to short and when to cover. Ecosystem unlocks average slightly positive, so the default is to skip them. Yardstick: short 30 days before, cover 14 days after, stop 20% above entry. Needs an unlock calendar: find a free source; if none exists, flag it.
8. **ASX technical setups.** Triggers: new 52-week highs on heavy volume, and industry-relative pullbacks: a large, liquid stock that has fallen against its own industry peers over the past month, with momentum controlled for (STRATEGIES.md, G). The agent checks news and fundamentals before deciding. Yardsticks: hold breakouts 20 days; exit pullbacks on the first up close or after 5 days.
9. **Range trading.** Trigger: coins the agent judges to be range-bound. It sets a grid of buy and sell levels and closes it when price breaks out of the range. Yardstick: a fixed grid over the last 30 days' range.

### Start at Level 3 — Monthly
10. **ASX directors buying.** Trigger: each Appendix 3Y showing an on-market purchase (the "nature of change" field). The agent judges size against the director's holding, several directors buying, and timing against results; holds weeks to months. The notice can arrive up to five business days after the trade. Yardstick: buy on publication day, hold 2 months.

## Model use when building
- Claude Code builds with Opus 5 at xhigh effort. Drop to medium for small mechanical jobs (reruns, renames, quick fixes).
- Long runs (archives, the arena's code side) are plain Python started from the command line.
