# How we're doing this
*22 Sep 2026. The plan in plain English. ARENA.md has the technical detail; where the two differ, this file wins on order and priorities.*

## The goal
An AI agent (OpenClaw) does the trading work — reading, researching and deciding — while stops and limits are enforced in code, so emotion never enters. We test on fake money first, starting very aggressive (aim: profit every day), scale back to weekly or monthly if that doesn't hold up, and only then move to real money, one level less aggressive than the level that earned it.

## Where the AI has a real edge (why ASX announcements come first)
- An AI agent's strength is reading and judging documents quickly and at scale. It is not speed: professional firms win anything decided in milliseconds.
- The ASX publishes every company announcement as a document and flags the price-sensitive ones, and research has found the market under-reacts to some of them, with part of the move arriving later. Hundreds of small companies get little analyst attention and are too small for big funds to trade.
- Lots of documents, a documented delayed reaction, and small stocks the big funds can't touch: that is the best match for an AI reader. It is also where this project started.

## Is crypto worth it?
**As the main event: no.**
- The fast end (intraday breakouts, crash bounces, listing pops) is crowded with bots and has the weakest evidence.
- Most crypto tactics with good evidence need shorting through futures. For an ordinary Australian that means offshore platforms with no Australian licence and no recourse: Binance offers Australians no futures, licensed local crypto futures are for wholesale investors only, and licensed retail leverage is capped at 2:1.
- Crypto news is fast social chatter rather than formal documents, so the AI's reading edge is smaller.

**Tested last.**
- Two crypto tactics have solid evidence and suit the agent's research: token unlocks and new-listing fades. They run on fake money; taking them live would need a decision about a futures venue.
- Crypto momentum can go live on an ordinary exchange (spot, no futures).
- The crypto-only parts of the simulator (funding payments, liquidation) are built when crypto starts, not before.

## The order — one tactic at a time, ASX first
Each tactic is ranked on three things: how strong the evidence is, how much the AI's reading and research adds, and whether it can actually be traded live from Australia.

1. **ASX announcements — Daily.** The first two days double as the shake-down that proves the machinery (the agent wakes, orders fill, stops trigger, reports arrive). Then a warm-up on free, delayed prices while IBKR was being set up (23-24 Sep), with each fill at the true market price at the moment of the agent's decision. The formal 10-trading-day tests run on IBKR's live ASX prices, switched on for 25 Sep (see below: v1 was retired and replaced by two playbooks).
2. **ASX technical setups** (52-week highs, pullbacks), with the agent checking the news behind each one — days to weeks.
3. **ASX directors buying — Monthly.** It trades rarely and needs almost no attention, so once built it runs quietly in the background and doesn't hold up the queue.
4. **Crypto token unlocks — Weekly.** Fake money; going live needs a futures-venue decision.
5. **Crypto new listings — days to weeks.** Fake money; going live needs a futures-venue decision.
6. **Crypto momentum — Weekly.** Spot, so it can go live on an ordinary exchange.
7–10. **The fast crypto tactics with the weakest evidence,** only if still wanted: intraday breakouts, crash bounces, crowded trades, range trading.

**24 Sep 2026 (Rick's brief): two Daily tests from 25 Sep.** The arena aims for profit
every day, and the ladder stands: Daily is tested over 10 ASX trading days and only a failed
test steps down to Weekly. Version 1 of the announcements tactic barely traded in two days,
which is not a test, so it is retired and two playbooks start their 10-day Daily test on
25 Sep, side by side (an exception to "one at a time", by Rick's decision):
- **Announcements v2 — trade the reaction.** The agent still gets a pre-open look, then
  decides on each stock with news once the market has traded it for 10-30 minutes: "over the
  rest of today, is the move bigger than about 0.4% after costs, and where is the stop?"
  Intraday only; $5,000 positions; a liquidity rule that scales with our order size.
- **The day trader.** Code scans the liquid market every minute for four setups (gap-and-go,
  opening-range breakout, VWAP reclaim, halt resumption); the agent confirms or rejects
  each; code sizes (0.5% risk), manages (breakeven, half off, trailing stop) and is flat by
  the close.
Both run on IBKR's real-time prices (switched on for 25 Sep); when the live feed is down
no new entry is decided. Rules: config.yaml, frozen 2026-09-24 before they ran.

## Each tactic's cycle
build (one or two evenings) → shake-down (1–2 days) → test (Daily: 10 ASX trading days or 14 crypto days; Weekly: 4 weeks; Monthly: 3 months) → checkpoint → next tactic.
- A tactic that passes keeps running on autopilot.
- A tactic that fails at Daily gets one 4-week round at Weekly while the next tactic is tested, then it is kept or retired.

## Honest expectations
- Profit every single day is the hardest target in trading. Starting there is fine on fake money; the ladder moves a tactic to weekly if daily doesn't hold up.
- Every account is valued at current prices every day, all costs count, stops stay on, and nothing adds to losing trades, so the green days are real.
- Real money only after 4 weeks of forward profit after costs, starting one level less aggressive.

## Rick's part
- **Once:** create the trader bot in Telegram (@BotFather, `/newbot`) and give Claude Code the token. Apply for IBKR (Margin account, Australian stocks), deposit at least US$500 so live prices can be switched on, and subscribe to ASX Total ($25 a month).
- **Daily:** a glance at the evening Telegram report.
- **At each checkpoint:** about 10 minutes, then one message to Claude Code to start the next build.

## Accounts, and when each is needed
- **Now:** IBKR (live account, ASX Total, read-only API): the arena's prices since 25 Sep.
- **IBKR, later:** ASX real money.
- **Binance or Kraken:** only when a crypto tactic goes live on spot.
- **A futures venue:** only if a shorting crypto tactic earns real money. Decide then, knowing the offshore risks.
