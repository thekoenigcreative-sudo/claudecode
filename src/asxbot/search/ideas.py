"""Every idea the search will try, REGISTERED BEFORE ANY OF THEM RAN (26 Sep 2026: the commit
that adds this file is the proof - no bar of history was on this machine when it was written).

An idea is a family (search/strategies.py), the parameters it changes from that family's
defaults, the reason it might work, and its parent. Ideas are never edited once registered; a
change is a new idea with its own id. Every idea tried is counted: the check-set bar is
sqrt(2 ln N) over ALL ideas tried so far, winners and losers (WINNER.md's validation bar,
charged on every try rather than only on those validated - the stricter reading).

Waves: 1 = the brief's yardsticks A, B and C; 2 = variants of them, each changing one thing
for a stated reason; 3 = new families, widening rather than tweaking. Proposals beyond these
(from a later session) go in data/search/proposals.json with the same fields and get ids
from 100 up.
"""

from __future__ import annotations

import json
from pathlib import Path

IDEAS: list[dict] = [
    # ---------------- wave 1: yardstick A - stocks in play, opening-range breakout ---------
    {"id": "A01", "family": "orb_inplay", "wave": 1, "parent": None, "params": {},
     "reason": "Yardstick A as the brief gives it: opening-window RVOL ranks the day's "
               "stocks in play; with a catalyst, the first 5-minute range's break in the "
               "opening candle's direction often runs all day (Zarattini & Aziz 2023, US)."},
    {"id": "A02", "family": "orb_inplay", "wave": 1, "parent": "A01", "params": {"window": 10},
     "reason": "A 10-minute range filters the ASX's noisy first minutes after a staggered "
               "open; fewer, better-defined breaks."},
    {"id": "A03", "family": "orb_inplay", "wave": 1, "parent": "A01", "params": {"window": 15},
     "reason": "A 15-minute range: wider stop, fewer false breaks, lower cost per R."},
    {"id": "A04", "family": "orb_inplay", "wave": 1, "parent": "A01", "params": {"exit": "trail"},
     "reason": "The brief's trailing-stop variant: keep the trend days, give back less on "
               "reversals (trail 1R once 1R up)."},
    {"id": "A05", "family": "orb_inplay", "wave": 1, "parent": "A01", "params": {"top_k": 2},
     "reason": "Only the two most in-play stocks: if RVOL rank carries the edge, the top of "
               "the list should be better than the third."},
    # ---------------- wave 1: yardstick B - announcement drift by type ----------------------
    {"id": "B01", "family": "drift", "wave": 1, "parent": None,
     "params": {"buckets": ["results"], "hold": 2},
     "reason": "Post-earnings drift: prices underreact to results, and a strong day-0 "
               "reaction keeps drifting (Bernard & Thomas 1989); 2-day hold."},
    {"id": "B02", "family": "drift", "wave": 1, "parent": "B01",
     "params": {"buckets": ["results"], "hold": 5},
     "reason": "Results drift over a week (LEARNINGS #9: results were the only type with a "
               "pulse - a hypothesis from a slice, so it is tested here out of sample)."},
    {"id": "B03", "family": "drift", "wave": 1, "parent": "B01",
     "params": {"buckets": ["results"], "hold": 10},
     "reason": "Results drift over two weeks: the literature's drift is slow."},
    {"id": "B04", "family": "drift", "wave": 1, "parent": None,
     "params": {"buckets": ["guidance_up"], "hold": 5},
     "reason": "Guidance upgrades are revisions analysts follow over days."},
    {"id": "B05", "family": "drift", "wave": 1, "parent": None,
     "params": {"buckets": ["contract"], "hold": 5},
     "reason": "Contract wins in small/mid caps are digested slowly by thinly-covered names."},
    {"id": "B06", "family": "drift", "wave": 1, "parent": None,
     "params": {"buckets": ["drilling"], "hold": 5},
     "reason": "Drilling/assay news: retail follow-through over days in explorers."},
    {"id": "B07", "family": "drift", "wave": 1, "parent": None,
     "params": {"buckets": ["takeover"], "hold": 10, "react_max": 0.6},
     "reason": "Takeover news: the price sits under the offer and closes the gap as the deal "
               "firms up (merger arbitrage in miniature)."},
    {"id": "B08", "family": "drift", "wave": 1, "parent": None, "params": {"hold": 5},
     "reason": "All the brief's types together (results, guidance up, contracts, drilling, "
               "takeovers): the broadest drift test, most trades."},
    {"id": "B09", "family": "drift", "wave": 1, "parent": None,
     "params": {"buckets": ["guidance_down"], "hold": 5, "direction": "against"},
     "reason": "Guidance downgrades long only: buy the overreaction after a big fall."},
    # ---------------- wave 1: yardstick C - regime filter and closing-volume cap -----------
    {"id": "C01", "family": "orb_inplay", "wave": 1, "parent": "A01",
     "params": {"regime": "index_ma"},
     "reason": "Longs only when the index closed above its 20-session average: breakouts "
               "fail more in a falling market."},
    {"id": "C02", "family": "orb_inplay", "wave": 1, "parent": "A01",
     "params": {"regime": "first30"},
     "reason": "Only in the direction of the index's first 30 minutes (the day's tone); "
               "entries wait until 10:31."},
    {"id": "C03", "family": "orb_inplay", "wave": 1, "parent": "A01", "params": {"close_cap": 0.2},
     "reason": "Closing-volume cap: never hold more than 20% of the usual closing auction, "
               "so the flat-by-close exit is always absorbed (9 stuck positions in the "
               "replay)."},
    {"id": "C04", "family": "orb_inplay", "wave": 1, "parent": "A01",
     "params": {"regime": "index_ma", "close_cap": 0.2},
     "reason": "Both C filters on A together."},
    {"id": "C05", "family": "drift", "wave": 1, "parent": "B08",
     "params": {"hold": 5, "regime": "index_ma"},
     "reason": "Drift with the regime filter: underreaction is continued more readily in a "
               "rising market."},
    {"id": "C06", "family": "drift", "wave": 1, "parent": "B02",
     "params": {"buckets": ["results"], "hold": 5, "regime": "index_ma"},
     "reason": "Results drift with the regime filter."},
    # ---------------- wave 2: one change each, for a reason ---------------------------------
    {"id": "A06", "family": "orb_inplay", "wave": 2, "parent": "A01",
     "params": {"catalyst": "news"},
     "reason": "News-only catalyst: a gap without news may be flow that reverses; news is "
               "information that persists."},
    {"id": "A07", "family": "orb_inplay", "wave": 2, "parent": "A01",
     "params": {"catalyst": "none"},
     "reason": "A control: no catalyst at all. If this does as well, the catalyst adds nothing."},
    {"id": "A08", "family": "orb_inplay", "wave": 2, "parent": "A01", "params": {"min_rvol": 3.0},
     "reason": "Only truly in-play stocks (3x usual opening volume); fewer, stronger days."},
    {"id": "A09", "family": "orb_inplay", "wave": 2, "parent": "A01", "params": {"shorts": True},
     "reason": "The short mirror in the ASX 200 (shortable): down-candle openings with news "
               "break down as well as up."},
    {"id": "A10", "family": "orb_inplay", "wave": 2, "parent": "A01",
     "params": {"min_r_over_cost": 6.0},
     "reason": "Costs eat small ranges: require 1R to be 6x the round trip, not 3x."},
    {"id": "A11", "family": "orb_inplay", "wave": 2, "parent": "A03",
     "params": {"window": 15, "exit": "trail", "trail_r": 2.0, "trail_after_r": 2.0},
     "reason": "Wide range with a loose trail: let the trend days pay for the chop."},
    {"id": "A12", "family": "orb_inplay", "wave": 2, "parent": "A01",
     "params": {"risk_pct": 0.5, "top_k": 5},
     "reason": "Spread the same risk over more names: lower variance, same edge if it is "
               "there (costs rise per trade)."},
    {"id": "B10", "family": "drift", "wave": 2, "parent": "B08",
     "params": {"hold": 5, "entry": "open1"},
     "reason": "Enter at the next open instead of the day-0 auction: avoids paying the "
               "close's spike, sees the overnight."},
    {"id": "B11", "family": "drift", "wave": 2, "parent": "B08",
     "params": {"hold": 5, "stop_pct": 0.08},
     "reason": "An 8% stop on the daily low: cut the reversals of the reaction."},
    {"id": "B12", "family": "drift", "wave": 2, "parent": "B08",
     "params": {"hold": 5, "react_min": 0.06},
     "reason": "Only big reactions (6%+ against the index): stronger news, stronger drift."},
    {"id": "B13", "family": "drift", "wave": 2, "parent": "B08",
     "params": {"hold": 5, "react_min": 0.01, "react_max": 0.03},
     "reason": "Small reactions: underreaction is largest where the market barely moved."},
    {"id": "B14", "family": "drift", "wave": 2, "parent": "B08",
     "params": {"hold": 5, "ps_only": False},
     "reason": "Include non-price-sensitive announcements of the same types (more trades)."},
    {"id": "B15", "family": "drift", "wave": 2, "parent": "B08",
     "params": {"hold": 5, "min_turnover": 5_000_000.0},
     "reason": "Liquid names only: costs and stuck exits fall; does the drift survive?"},
    # ---------------- wave 3: new families (widen, do not tweak) ----------------------------
    {"id": "G01", "family": "gap_fade", "wave": 3, "parent": None, "params": {},
     "reason": "No-news gaps down of 3%+ in liquid names are often flow, not information; "
               "buy the turn at 10:30 with a stop under the low, out at the close."},
    {"id": "G02", "family": "gap_fade", "wave": 3, "parent": "G01", "params": {"gap_min": 0.05},
     "reason": "Only big no-news gaps (5%+): more overshoot to recover."},
    {"id": "G03", "family": "gap_fade", "wave": 3, "parent": "G01", "params": {"entry_at": "11:00"},
     "reason": "Wait until 11:00: the low is more often in by then."},
    {"id": "L01", "family": "late_trend", "wave": 3, "parent": None, "params": {},
     "reason": "Intraday momentum into the close (Gao, Han, Li & Zhou 2018): the day's "
               "strong, heavy-volume movers keep rising into the closing auction."},
    {"id": "L02", "family": "late_trend", "wave": 3, "parent": "L01",
     "params": {"catalyst": "news"},
     "reason": "Only movers with price-sensitive news today."},
    {"id": "L03", "family": "late_trend", "wave": 3, "parent": "L01",
     "params": {"regime": "index_ma"},
     "reason": "Late momentum only in a rising market."},
    {"id": "R01", "family": "reversal", "wave": 3, "parent": None, "params": {},
     "reason": "Short-term reversal: a liquid stock's big no-news drop is often liquidity "
               "demand that is repaid the next day (Lehmann 1990; Nagel 2012)."},
    {"id": "R02", "family": "reversal", "wave": 3, "parent": "R01", "params": {"hold": 3},
     "reason": "The same, held three sessions."},
    {"id": "R03", "family": "reversal", "wave": 3, "parent": "R01", "params": {"drop_min": 0.07},
     "reason": "Only big drops (7%+)."},
    {"id": "R04", "family": "reversal", "wave": 3, "parent": "R01",
     "params": {"regime": "index_ma"},
     "reason": "Reversal only in a rising market (a falling one keeps falling)."},
    {"id": "K01", "family": "breakout20", "wave": 3, "parent": None, "params": {},
     "reason": "A 20-session high on double volume: momentum over the next week."},
    {"id": "K02", "family": "breakout20", "wave": 3, "parent": "K01", "params": {"hold": 10},
     "reason": "The same, held two weeks."},
    {"id": "D01", "family": "day2_orb", "wave": 3, "parent": None, "params": {},
     "reason": "The day after a 5%+ news reaction, the stock is still in play: its opening "
               "range break in the reaction's direction."},
    {"id": "D02", "family": "day2_orb", "wave": 3, "parent": "D01",
     "params": {"react_min": 0.10},
     "reason": "Only after very big reactions (10%+)."},
]


def all_ideas(data_dir: Path | None = None) -> list[dict]:
    """The registered ideas, then any later proposals (data/search/proposals.json)."""
    out = [dict(x) for x in IDEAS]
    if data_dir is not None:
        f = Path(data_dir) / "search" / "proposals.json"
        if f.exists():
            seen = {x["id"] for x in out}
            for x in json.loads(f.read_text(encoding="utf-8")):
                if x["id"] in seen:
                    raise ValueError(f"proposal {x['id']} reuses a registered id")
                for k in ("family", "params", "reason"):
                    if k not in x:
                        raise ValueError(f"proposal {x.get('id')} has no {k}")
                out.append({"wave": 4, "parent": None, **x})
    return out


def complexity(idea: dict) -> int:
    """Changed parameters (WINNER.md criterion 11: at most 6)."""
    return len(idea.get("params") or {})


__all__ = ["IDEAS", "all_ideas", "complexity"]
