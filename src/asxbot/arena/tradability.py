"""Can this stock be traded at all? A plain-code screen, run BEFORE any model call.

Most price-sensitive announcements come from companies the arena could never trade at a
sensible size: a few thousand dollars a day of turnover, a half-cent tick on a two-cent
share, or a stock that is halted. Reading those with Sonnet and judging them with Opus
costs money and teaches nothing, because the answer is always "cannot trade it".

So four tests, all arithmetic, no model:

  1. HALTED OR SUSPENDED - the announcement itself is a halt/suspension/reinstatement, or
     the market has been open a while and the stock has not traded at all today.
  2. NO LIVE QUOTE - nothing to price an entry or a stop against.
  3. TICK TOO COARSE - one ASX tick is worth more than 1% of the price, so the spread
     alone eats the edge.
  4. TOO THIN - median 20-day dollar turnover below `universe.turnover_floor_aud`.

A rejection here is logged (events: `arena_screened`) and NOT alerted: these are the
uninteresting majority, and the point is to stop them quietly before they cost anything.

The yardstick bot is deliberately left alone. Its rule was frozen on 2026-09-22 with its
own turnover floor, and changing what it sees mid-warm-up would break the comparison the
whole arena exists to make.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from asxbot.announcements.model import Announcement
from asxbot.live.quotes import median_turnover_20d
from asxbot.live.scanner import asx_tick
from asxbot.log import get_logger

log = get_logger("asxbot.arena.tradability")
SYD = ZoneInfo("Australia/Sydney")

MAX_TICK_PCT = 1.0  # one tick worth more than this much of the price is untradeable
NO_TRADE_BY = time(10, 30)  # by this time a live stock has traded; a halted one has not


@dataclass(frozen=True)
class Screen:
    ok: bool
    why: str
    test: str = ""  # which test rejected it, for counting later
    turnover: float | None = None  # the arithmetic behind the verdict, kept for the record
    tick_pct: float | None = None

    def __bool__(self) -> bool:
        return self.ok


def tick_pct(price: float) -> float:
    return asx_tick(price) / price * 100.0 if price > 0 else 100.0


def screen(
    a: Announcement,
    quote,
    daily,
    floor_aud: float,
    now: datetime | None = None,
    max_tick_pct: float = MAX_TICK_PCT,
) -> Screen:
    """Decide in plain code whether this is worth a model's time. No network, no model."""
    now = (now or datetime.now(SYD)).astimezone(SYD)

    if a.type == "trading_halt":
        return Screen(False, f"the announcement is a halt or suspension ({a.headline})", "halted")

    if quote is None:
        return Screen(False, "no live quote, so an entry and a stop cannot be priced", "no_quote")

    if now.time() >= NO_TRADE_BY and quote.volume_today <= 0:
        return Screen(
            False,
            f"no trades at all by {now:%H:%M}, which usually means halted or suspended",
            "halted",
        )

    tp = tick_pct(quote.last)
    if tp > max_tick_pct:
        return Screen(
            False,
            f"one tick ({asx_tick(quote.last):.3f}) is {tp:.2f}% of the {quote.last:.3f} price, "
            f"above the {max_tick_pct}% limit - the spread alone would eat the edge",
            "tick",
            tick_pct=tp,
        )

    turnover = median_turnover_20d(daily)
    if turnover is None:
        return Screen(
            False, "not enough daily history to measure turnover", "no_history"
        )
    if turnover < floor_aud:
        return Screen(
            False,
            f"median 20-day turnover ${turnover:,.0f} is below the ${floor_aud:,.0f} floor",
            "turnover",
            turnover=turnover,
            tick_pct=tp,
        )
    return Screen(
        True,
        f"tradeable: ${turnover:,.0f} turnover, tick {tp:.2f}% of price",
        turnover=turnover,
        tick_pct=tp,
    )


def limits_for(cfg, pb) -> tuple[float, float]:
    """The playbook's turnover floor and tick limit, falling back to the universe's."""
    trigger = pb.raw.get("trigger") or {}
    default_floor = cfg.get("universe.turnover_floor_aud", 250_000)
    floor = float(trigger.get("turnover_floor_aud", default_floor))
    return floor, float(trigger.get("max_tick_pct", MAX_TICK_PCT))
