# Yahoo's daily open as the ASX opening auction price (TRACKER #28)

**plumbing test - not a go/no-go.** Yahoo data, read 2026-09-23 21:42 Sydney by
`scripts/check_auction_open.py`: 50 stocks, 348 stock-days (the ~7 days of 1-minute
bars the free feed keeps). Nothing here was used to choose a parameter.

## What was checked, and what it found

| Check | Result | What it says |
|---|---|---|
| Daily close equals the 16:10 closing-auction minute bar | 348 of 348 | The daily bar carries the official auction prints, at least at the close |
| Daily open on the ASX tick grid | 348 of 348 | A real traded price, not an average or a quote midpoint |
| Daily open inside the daily bar's low-high | 348 of 348 | Internally consistent |
| Daily open outside every continuous minute bar's range | 71 of 348 (20%) | A print the minute feed does not hold, as the auction is |
| Daily open equals the first traded minute's open | 42 of 348 (12%) | The first traded minute is usually not the open |
| First minute row of the day has no volume | 260 of 348 (75%) | The auction's volume is not in the minute bars (#28) |
| Daily volume not above the minute volumes (auction volume unmeasurable) | 15 of 348 | The arena refuses these auctions |
| Would be refused by the arena's checks (tick, range, volume) | 15 of 348 | Those fill at the first traded minute, labelled |

Distance between the daily open and the first traded minute's open: median
0.30%, 90th percentile 1.16%, largest 9.69%
(TUA 2026-09-23: open 2.4900, first traded minute 2.2700).
The arena's slippage base is 0.10%, so the difference between the two rules is larger than
the cost model on most days. Median estimated auction volume, where measurable:
4.6% of the day's volume.

The largest differences:

| Stock | Day | Daily open | First traded minute open (time) | Difference |
|---|---|---|---|---|
| TUA | 2026-09-23 | 2.4900 | 2.2700 (10:01) | 9.69% |
| CXO | 2026-09-21 | 0.3650 | 0.3870 (10:01) | 5.68% |
| IMU | 2026-09-21 | 0.0490 | 0.0470 (10:12) | 4.26% |
| NUF | 2026-09-23 | 3.3000 | 3.1700 (10:01) | 4.10% |
| IMU | 2026-09-16 | 0.0470 | 0.0490 (10:03) | 4.08% |
| CXO | 2026-09-15 | 0.3850 | 0.3750 (10:01) | 2.67% |
| A1M | 2026-09-23 | 0.8000 | 0.8200 (10:15) | 2.44% |
| GL1 | 2026-09-17 | 0.6700 | 0.6550 (10:00) | 2.29% |

## What was not checked, and why

- **No source independent of Yahoo's vendor.** Tried on 23 Sep 2026: Stooq (JavaScript
  proof-of-work page), MarketWatch (captcha), Market Index (Cloudflare challenge), Google
  Finance (empty), the FT (sign-in), the ASX's own API (`asx.api.markitdigital.com` header and
  key-statistics give no open; the old `asx.com.au/asx/1/share` endpoint is gone). CNBC's
  daily bars for BHP give the same opens as Yahoo's, to the cent, and the same volumes on
  every completed day (23 Sep, read that evening, differed: 7,354,781 against 7,269,781, as
  the ASX's own API had it), so they are the same vendor, not a second witness. The daily
  open is therefore **checked, not confirmed** against the ASX's record of the auction. A broker's course of sales (IBKR, once live data
  starts) is the way to confirm it.
- **Some days disagree in a way the checks cannot explain.** A1M 17 Sep: the 10:00 minute
  bar traded 99,985 shares at a flat 0.770 and the daily volume equals the minute volume, yet
  the daily open is 0.775. The arena refuses that day's auction (no volume left over for
  it), so its orders would fill at the first traded minute.
- **Stagger.** The ASX opens stocks in five alphabetical groups from 10:00 to about 10:09.
  Yahoo's minute bars do not show it, across all five groups the first traded minute is
  10:01 on 242 days, 10:00 on 82 days, 10:03 on 5 days, 10:02 on 4 days. The arena stamps every auction 09:59 whatever the group.
- **Auction volume is an estimate and an upper bound**: the daily volume less the minute
  volumes also holds off-market trades reported to the ASX, and on a day the minute feed
  drops its closing auction, that too. Intraday, London and Frankfurt stocks polled six times
  on 23 Sep showed the same difference steady to within a minute's volume, once jumping by a
  block trade the daily bar had before the minute bars.
