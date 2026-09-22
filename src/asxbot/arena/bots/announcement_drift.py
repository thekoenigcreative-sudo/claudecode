"""Yardstick bot for playbook "ASX announcements": strategy A, unchanged.

The rule, frozen 2026-09-22 and already backtested (STRATEGIES.md row A):
    a price-sensitive announcement, plus a gap of at least 5% over the ASX 200 on at
    least 3x the stock's 20-day average volume; long only; 8% stop; exit after 10
    trading days.

No model is used anywhere in this file. It reads the headline's price-sensitive flag and
the price/volume reaction, nothing else - not even the announcement text. That is what
makes it a fair yardstick for what the agent's reading adds.
"""

from __future__ import annotations

import math
from datetime import datetime

from asxbot.announcements.model import Announcement
from asxbot.arena.accounts import Account
from asxbot.arena.bots.base import Bot, BotDecision
from asxbot.live.reaction import measure
from asxbot.live.scanner import round_to_tick, session_fraction


class AnnouncementDriftBot(Bot):
    name = "announcement_drift_A"
    version = "2026-09-22"

    def __init__(self, cfg, playbook, broker, quotes, daily_lookup, universe):
        super().__init__(cfg, playbook, broker)
        self.quotes = quotes
        self.daily_lookup = daily_lookup
        self.universe = universe
        self.gap_pct = float(self.params.get("gap_pct_vs_index", 5.0))
        self.vol_mult = float(self.params.get("volume_multiple", 3.0))
        self.hold_days = int(self.params.get("hold_days", 10))
        self.stop_pct = float(self.params.get("stop_loss_pct", 8.0))
        self.turnover_floor = float(
            (playbook.raw.get("trigger") or {}).get("turnover_floor_aud", 250_000)
        )

    # -- entries -------------------------------------------------------------
    def on_announcement(
        self, acct: Account, a: Announcement, now: datetime
    ) -> tuple[BotDecision | None, str]:
        if not a.price_sensitive:
            return None, "not price sensitive"
        if a.code not in self.universe:
            return None, "not in universe"
        if a.code in acct.positions:
            return None, "already holding"
        q = self.quotes.quote(a.code)
        iq = self.quotes.index_quote()
        if q is None or iq is None:
            return None, "no quote"
        daily = self.daily_lookup(a.code)
        r = measure(q, iq, daily, now, session_fraction)
        if r is None:
            return None, "insufficient daily history"
        if r.median_turnover < self.turnover_floor:
            return None, f"turnover {r.median_turnover:,.0f} below floor"
        ok, why = r.passes(self.gap_pct, self.vol_mult)
        if not ok:
            return None, why

        # Size: the level's risk cap decides how many shares an 8% stop allows.
        equity = acct.equity(self.broker.prices(acct))
        limit = round_to_tick(q.last, up=True)
        stop = round_to_tick(limit * (1 - self.stop_pct / 100.0), up=False)
        risk_each = limit - stop
        if risk_each <= 0:
            return None, "stop is not below the entry"
        risk_budget = equity * self.playbook.level.risk_per_trade_pct / 100.0
        qty = int(math.floor(min(risk_budget / risk_each, equity / limit)))
        max_pct = self.playbook.guidance("max_position_pct_of_equity")
        if max_pct is not None:
            qty = min(qty, int(math.floor(equity * float(max_pct) / 100.0 / limit)))
        if qty <= 0:
            return None, "size rounds to zero"
        return (
            BotDecision(
                ticker=a.code,
                side="buy",
                qty=qty,
                limit=limit,
                stop=stop,
                reason=(
                    f"{self.label}: price-sensitive announcement, {r.move_rel_pct:+.1f}% vs the "
                    f"ASX 200 on {r.vol_mult:.1f}x session-adjusted volume "
                    f"(rule: >= {self.gap_pct}% and >= {self.vol_mult}x). "
                    f"Stop {self.stop_pct}% below, time exit after {self.hold_days} sessions. "
                    "No model was used."
                ),
            ),
            "ok",
        )

    # -- exits ---------------------------------------------------------------
    def manage(self, acct: Account, now: datetime) -> list[BotDecision]:
        """The time exit. The 8% stop is enforced in code by the broker, always on."""
        out = []
        for ticker, pos in acct.positions.items():
            if pos.opened_by != "bot" or pos.qty <= 0:
                continue
            opened = datetime.fromisoformat(pos.opened_at)
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=now.tzinfo)
            sessions = _sessions_between(opened, now)
            if sessions < self.hold_days:
                continue
            px = self.broker.minutes.last_price(ticker) or pos.avg_cost
            out.append(
                BotDecision(
                    ticker=ticker,
                    side="sell",
                    qty=pos.qty,
                    limit=round_to_tick(px * 0.97, up=False),  # marketable, still a limit
                    stop=0.0,
                    reason=(
                        f"{self.label}: time exit after {sessions} sessions "
                        f"(rule: {self.hold_days})"
                    ),
                )
            )
        return out


def _sessions_between(start: datetime, end: datetime) -> int:
    """Weekday count between two datetimes. ASX holidays are not netted out; the effect on
    a 10-session exit is at most a day or two and is recorded in the trade log either way."""
    days = 0
    cur = start.date()
    last = end.date()
    while cur < last:
        cur = cur.fromordinal(cur.toordinal() + 1)
        if cur.weekday() < 5:
            days += 1
    return days
