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

1. **ASX announcements — Daily.** The first two days double as the shake-down that proves the machinery (the agent wakes, orders fill, stops trigger, reports arrive). Then a warm-up on free, delayed prices while IBKR is being set up, with each fill at the true market price at the moment of the agent's decision. The formal 10-trading-day test starts once IBKR's live ASX prices are switched on.
2. **ASX technical setups** (52-week highs, pullbacks), with the agent checking the news behind each one — days to weeks.
3. **ASX directors buying — Monthly.** It trades rarely and needs almost no attention, so once built it runs quietly in the background and doesn't hold up the queue.
4. **Crypto token unlocks — Weekly.** Fake money; going live needs a futures-venue decision.
5. **Crypto new listings — days to weeks.** Fake money; going live needs a futures-venue decision.
6. **Crypto momentum — Weekly.** Spot, so it can go live on an ordinary exchange.
7–10. **The fast crypto tactics with the weakest evidence,** only if still wanted: intraday breakouts, crash bounces, crowded trades, range trading.

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
- **Now:** none. The ASX warm-up runs on free, delayed prices.
- **IBKR:** for ASX live prices (tactic 1's formal test) and, later, ASX real money.
- **Binance or Kraken:** only when a crypto tactic goes live on spot.
- **A futures venue:** only if a shorting crypto tactic earns real money. Decide then, knowing the offshore risks.
