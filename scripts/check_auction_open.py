# ruff: noqa: E501 - the report template's table rows are one line each
"""How far Yahoo's daily open can be trusted as the ASX opening auction price (TRACKER #28).

Read-only, apart from the report it writes. For each stock it reads Yahoo's 1-minute bars
(the ~7 days the free feed keeps) and its daily bars, one request pair per stock, paced, and
compares them day by day:

  * does the daily CLOSE equal the 16:10 closing-auction minute bar? The closing auction is
    in the minute feed, so this tests whether the daily bar carries official auction prints;
  * is the daily OPEN on the ASX tick grid, and inside the daily bar's own range?
  * does the open appear in the minute bars at all (inside their range, equal to the first
    traded minute's open), and how far is it from that first traded minute?
  * the auction's estimated volume, the daily volume less the minute volumes: how often is it
    not above zero, so the arena would refuse the auction (minutes.opening_auction)?

Nothing here is independent of Yahoo's vendor: no free independent source could be read (see
the report). Results are a plumbing test - not a go/no-go.

    python scripts/check_auction_open.py [CODES,...] [--out reports/auction_open_check.md]
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import pandas as pd  # noqa: E402

from asxbot.arena.minutes import _on_tick  # noqa: E402
from asxbot.config import repo_root  # noqa: E402

SYD = ZoneInfo("Australia/Sydney")
DEFAULT = (
    "BHP,CBA,A1M,FMG,ANZ,AGL,ALL,BXB,COL,CSL,DUG,EVN,GMG,JBH,MQG,NAB,NST,ORG,QBE,RIO,S32,STO,"
    "TLS,WBC,WDS,WES,WOW,XRO,ZIP,PLS,LYC,MIN,IGO,SFR,PDN,BOE,DRO,NXT,TNE,SGH,VUL,LTR,CXO,IEL,"
    "PNV,IMU,NUF,TUA,CMM,GL1"
)
TOL = 1e-6


def stock_days(code: str) -> list[dict]:
    import yfinance as yf

    t = yf.Ticker(f"{code}.AX")
    m = t.history(period="7d", interval="1m", auto_adjust=False)
    d = t.history(period="15d", interval="1d", auto_adjust=False)
    if not len(m) or not len(d):
        return []
    m = m.rename(columns=str.lower)
    d = d.rename(columns=str.lower)
    m.index = m.index.tz_convert(SYD)
    out = []
    for day, g in m.groupby(m.index.date):
        dr = d[[x.date() == day for x in d.index]]
        traded = g[g["volume"] > 0]
        if not len(dr) or not len(traded):
            continue
        r = dr.iloc[0]
        body = traded[traded.index.time < datetime(2000, 1, 1, 16, 10).time()]
        close_auction = traded[traded.index.time >= datetime(2000, 1, 1, 16, 10).time()]
        out.append({
            "code": code, "day": day, "open": r["open"], "high": r["high"], "low": r["low"],
            "close": r["close"], "volume": r["volume"],
            "first_row": g.index[0].strftime("%H:%M"), "first_row_volume": g["volume"].iloc[0],
            "first_traded": traded.index[0].strftime("%H:%M"),
            "first_traded_open": traded["open"].iloc[0],
            "minute_high": body["high"].max() if len(body) else float("nan"),
            "minute_low": body["low"].min() if len(body) else float("nan"),
            "minute_volume": g["volume"].sum(),
            "closing_auction": close_auction["close"].iloc[0] if len(close_auction) else None,
        })  # fmt: skip
    return out


def report(df: pd.DataFrame, codes: list[str], when: datetime) -> str:
    n = len(df)
    ca = df[df["closing_auction"].notna()]
    close_ok = ((ca["close"] - ca["closing_auction"]).abs() < TOL).sum()
    on_tick = df["open"].map(_on_tick).sum()
    in_range = ((df["open"] >= df["low"] - TOL) & (df["open"] <= df["high"] + TOL)).sum()
    outside = ((df["open"] > df["minute_high"] + TOL) | (df["open"] < df["minute_low"] - TOL))
    eq_first = ((df["open"] - df["first_traded_open"]).abs() < TOL).sum()
    gap = (df["open"] / df["first_traded_open"] - 1).abs() * 100
    worst = df.loc[gap.idxmax()]
    est = df["volume"] - df["minute_volume"]
    zero_first = (df["first_row_volume"] == 0).sum()
    refused = (
        (~df["open"].map(_on_tick))
        | (df["open"] < df["low"] - TOL) | (df["open"] > df["high"] + TOL) | (est <= 0)
    ).sum()  # fmt: skip
    share = (est[est > 0] / df.loc[est > 0, "volume"] * 100).median()
    big = df.assign(gap=gap).sort_values("gap", ascending=False).head(8)
    rows = "\n".join(
        f"| {r.code} | {r.day} | {r.open:.4f} | {r.first_traded_open:.4f} ({r.first_traded}) | "
        f"{r.gap:.2f}% |"
        for r in big.itertuples()
    )
    first_times = ", ".join(
        f"{t} on {k} days" for t, k in df["first_traded"].value_counts().head(4).items()
    )
    return f"""# Yahoo's daily open as the ASX opening auction price (TRACKER #28)

**plumbing test - not a go/no-go.** Yahoo data, read {when:%Y-%m-%d %H:%M} Sydney by
`scripts/check_auction_open.py`: {len(codes)} stocks, {n} stock-days (the ~7 days of 1-minute
bars the free feed keeps). Nothing here was used to choose a parameter.

## What was checked, and what it found

| Check | Result | What it says |
|---|---|---|
| Daily close equals the 16:10 closing-auction minute bar | {close_ok} of {len(ca)} | The daily bar carries the official auction prints, at least at the close |
| Daily open on the ASX tick grid | {on_tick} of {n} | A real traded price, not an average or a quote midpoint |
| Daily open inside the daily bar's low-high | {in_range} of {n} | Internally consistent |
| Daily open outside every continuous minute bar's range | {int(outside.sum())} of {n} ({outside.mean():.0%}) | A print the minute feed does not hold, as the auction is |
| Daily open equals the first traded minute's open | {eq_first} of {n} ({eq_first / n:.0%}) | The first traded minute is usually not the open |
| First minute row of the day has no volume | {zero_first} of {n} ({zero_first / n:.0%}) | The auction's volume is not in the minute bars (#28) |
| Daily volume not above the minute volumes (auction volume unmeasurable) | {int((est <= 0).sum())} of {n} | The arena refuses these auctions |
| Would be refused by the arena's checks (tick, range, volume) | {int(refused)} of {n} | Those fill at the first traded minute, labelled |

Distance between the daily open and the first traded minute's open: median
{gap.median():.2f}%, 90th percentile {gap.quantile(0.9):.2f}%, largest {gap.max():.2f}%
({worst.code} {worst.day}: open {worst.open:.4f}, first traded minute {worst.first_traded_open:.4f}).
The arena's slippage base is 0.10%, so the difference between the two rules is larger than
the cost model on most days. Median estimated auction volume, where measurable:
{share:.1f}% of the day's volume.

The largest differences:

| Stock | Day | Daily open | First traded minute open (time) | Difference |
|---|---|---|---|---|
{rows}

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
  {first_times}. The arena stamps every auction 09:59 whatever the group.
- **Auction volume is an estimate and an upper bound**: the daily volume less the minute
  volumes also holds off-market trades reported to the ASX, and on a day the minute feed
  drops its closing auction, that too. Intraday, London and Frankfurt stocks polled six times
  on 23 Sep showed the same difference steady to within a minute's volume, once jumping by a
  block trade the daily bar had before the minute bars.
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="?", default=DEFAULT)
    ap.add_argument("--out", default=str(repo_root() / "reports" / "auction_open_check.md"))
    ap.add_argument("--pause", type=float, default=1.0)
    a = ap.parse_args()
    codes = [c.strip().upper() for c in a.codes.split(",") if c.strip()]
    rows: list[dict] = []
    for c in codes:
        rows.extend(stock_days(c))
        time.sleep(a.pause)
    if not rows:
        print("no data")
        return 1
    df = pd.DataFrame(rows)
    text = report(df, codes, datetime.now(SYD))
    Path(a.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
