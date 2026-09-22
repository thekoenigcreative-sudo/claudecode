"""The live price/volume reaction test, in one place.

The scanner (which writes proposals for the real-money path) and the arena's rule-based
yardstick bot both measure the same thing. Keeping the arithmetic here means the frozen
rule cannot quietly drift apart in two copies.

Fixed 2026-09-22 (config.yaml `strategy`):
    move_rel = (last / prev_close - 1) - (index_last / index_prev_close - 1), in per cent
    vol_mult = volume_today / (avg_volume_20d * max(0.25, session_fraction))
    eligible = median dollar turnover over the last 20 sessions >= the floor

The session fraction matters because volume accumulates through the day: three times the
20-day average by 10:30am is a far bigger surprise than the same multiple at 4pm.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from asxbot.live.quotes import Quote, avg_volume_20d, median_turnover_20d


@dataclass(frozen=True)
class Reaction:
    move_rel_pct: float
    vol_mult: float
    median_turnover: float
    avg_volume: float
    session_fraction: float

    def passes(self, gap_pct: float, vol_mult: float) -> tuple[bool, str]:
        if self.move_rel_pct < gap_pct:
            return False, f"move {self.move_rel_pct:+.1f}% vs index below {gap_pct}%"
        if self.vol_mult < vol_mult:
            return False, f"volume {self.vol_mult:.1f}x (session-adjusted) below {vol_mult}x"
        return True, "ok"


def measure(
    quote: Quote, index_quote: Quote, daily, now: datetime, session_fraction_fn
) -> Reaction | None:
    """None when the daily history is too short to judge the reaction."""
    avg_vol = avg_volume_20d(daily)
    med_turn = median_turnover_20d(daily)
    if avg_vol is None or med_turn is None:
        return None
    move = (quote.last / quote.prev_close - 1) * 100
    idx_move = (index_quote.last / index_quote.prev_close - 1) * 100
    frac = max(0.25, session_fraction_fn(now))
    return Reaction(
        move_rel_pct=move - idx_move,
        vol_mult=quote.volume_today / (avg_vol * frac) if avg_vol > 0 else 0.0,
        median_turnover=med_turn,
        avg_volume=avg_vol,
        session_fraction=frac,
    )
