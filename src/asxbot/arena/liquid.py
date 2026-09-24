"""The size-aware liquidity rule, in one place (announcements v2 and the day trader).

v1 screened a stock out below a fixed $250,000 of median daily turnover, whatever we meant
to buy. From 2026-09-24 (config.yaml, both playbooks) a stock is tradeable if OUR ORDER is
under a set share (5%) of its median daily dollar turnover over the last 20 sessions. The
screen asks it at the largest order the level allows ($5,000, so $100,000 a day), and
arena_place_order asks it again of the actual order.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from asxbot.live.quotes import median_turnover_20d
from asxbot.live.scanner import asx_tick


@dataclass(frozen=True)
class SizeRule:
    share: float  # max order / median daily dollar turnover
    window: int = 20

    def floor(self, order_aud: float) -> float:
        """The median turnover a stock needs for an order of this size."""
        return float(order_aud) / self.share

    def max_order(self, turnover: float | None) -> float:
        return 0.0 if not turnover else float(turnover) * self.share


def size_rule(pb) -> SizeRule | None:
    """The playbook's size-aware rule, or None for a playbook without one (v1)."""
    conf = pb.raw.get("screen") or pb.raw.get("universe") or {}
    share = conf.get("max_order_share_of_turnover")
    if share is None:
        return None
    return SizeRule(float(share), int(conf.get("turnover_window_days", 20)))


def median_turnover(daily: pd.DataFrame | None, window: int = 20) -> float | None:
    return median_turnover_20d(daily, window)


def liquid_universe(
    codes: list[str], daily_of, rule: SizeRule, order_aud: float, max_tick_pct: float
) -> tuple[list[str], dict[str, dict]]:
    """The codes an order of `order_aud` may trade: median turnover at least order/share,
    and one tick at most `max_tick_pct` of the last close. Returns (codes, why for each)."""
    ok, why = [], {}
    need = rule.floor(order_aud)
    for c in codes:
        d = daily_of(c)
        t = median_turnover(d, rule.window)
        if t is None:
            why[c] = {"ok": False, "why": "no daily history"}
            continue
        last = float(d["close"].iloc[-1])
        tick = asx_tick(last) / last * 100 if last > 0 else 100.0
        if t < need:
            why[c] = {"ok": False, "why": f"turnover {t:,.0f} < {need:,.0f}", "turnover": t}
        elif tick > max_tick_pct:
            why[c] = {"ok": False, "why": f"tick {tick:.2f}% > {max_tick_pct}%", "turnover": t}
        else:
            ok.append(c)
            why[c] = {"ok": True, "turnover": t, "tick_pct": tick, "last_close": last}
    return sorted(ok), why
