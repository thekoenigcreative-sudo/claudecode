"""Yardstick bot for playbook "ASX announcements": strategy A, unchanged.

The rule, frozen 2026-09-22 and already backtested (STRATEGIES.md row A, config.yaml
`strategy`):
    a price-sensitive announcement whose reaction session t - the release day if released
    before 10:00, else the next session - opens at least 5% further above its previous close
    than the ASX 200 did, on at least 3x the stock's 20-day average volume, above the
    turnover floor. CONFIRMED at the close of t (t's volume is not known before then),
    ENTERED at the open of t+1; long only; 8% stop; exit after 10 trading days.

Until 2026-09-23 this bot entered the moment an announcement arrived, at an intraday limit,
on a move measured from the live price. That was a different strategy with the same name,
and it made the backtest inapplicable to what was running. It now runs the backtest's own
signal code (backtest/signals.py) on the completed daily bar of t, before the open of t+1,
so its orders are priced from that open. Every frozen parameter is as it was.

No model is used anywhere in this file. It reads the headline's price-sensitive flag and
daily prices, nothing else - not even the announcement text. That is what makes it a fair
yardstick for what the agent's reading adds.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from asxbot.announcements.model import Announcement
from asxbot.arena.accounts import Account
from asxbot.arena.bots.base import Bot, BotDecision
from asxbot.arena.broker import OPENING_SIDES
from asxbot.backtest.signals import announcement_events, build_panels, detect_events
from asxbot.live.scanner import round_to_tick

SYD = ZoneInfo("Australia/Sydney")

# The arena takes limit orders only, and the rule buys at the open whatever it is. So the
# limit sits this far above t's close, where it almost never binds: of 559 in-sample A events
# (asx300, to 2023-09-22), 0.9% opened more than 10% above the previous close, against 13.2%
# more than 3%. Measured on 2026-09-23 on opening prices, not on returns. A narrower cap
# would drop mostly the strongest continuations, which biases the yardstick; this one only
# makes it slightly smaller, because size is set from the limit, the worst case.
ENTRY_CAP_PCT = 10.0


class NotReady(RuntimeError):
    """The completed daily bar for the session being confirmed is not available yet."""


class AnnouncementDriftBot(Bot):
    name = "announcement_drift_A"
    version = "2026-09-23"  # the 2026-09-22 rule, entered at the next open as it was written

    def __init__(self, cfg, playbook, broker, universe, daily_fresh):
        super().__init__(cfg, playbook, broker)
        self.universe = universe
        self.daily_fresh = daily_fresh  # callable(tickers, start) -> {ticker: daily frame}
        self.gap_pct = float(self.params.get("gap_pct_vs_index", 5.0))
        self.vol_mult = float(self.params.get("volume_multiple", 3.0))
        self.hold_days = int(self.params.get("hold_days", 10))
        self.stop_pct = float(self.params.get("stop_loss_pct", 8.0))
        self.turnover_floor = float(
            (playbook.raw.get("trigger") or {}).get("turnover_floor_aud", 250_000)
        )
        self.vol_window = int(cfg.get("strategy.volume_window_days", 20))
        self.turnover_window = int(cfg.get("universe.turnover_window_days", 20))
        self.index_ticker = str(cfg.get("backtest.index_ticker", "^AXJO"))
        # Where the index's open comes from (TRACKER #27): `first_minute_bar`, the open of the
        # index's 10:00 minute bar, or `daily`, Yahoo's daily open - which equals the previous
        # close on most days, making the gap "vs the index" a raw gap.
        self.index_open_source = str(self.params.get("index_open", "daily"))

    # -- entries -------------------------------------------------------------
    def on_announcement(
        self, acct: Account, a: Announcement, now: datetime
    ) -> tuple[BotDecision | None, str]:
        """Nothing is decided when an announcement lands: the rule needs its reaction
        session's volume, which is not known until that session closes. entries() does it."""
        if not a.price_sensitive:
            return None, "not price sensitive"
        if a.code not in self.universe:
            return None, "not in universe"
        return None, (
            "noted: the frozen rule confirms at the close of the reaction session and enters "
            "at the next open"
        )

    def entries(
        self, acct: Account, ann: pd.DataFrame, session: date, now: datetime
    ) -> tuple[list[BotDecision], str]:
        """The orders for the open that follows `session`, the last completed session.

        `ann` holds the announcements the arena saw, shaped like the backtest's archive.
        Events are confirmed on `session`'s completed daily bar by the backtest's own code
        and returned highest volume multiple first, the backtest's priority when there are
        more events than free slots. Raises NotReady if that bar is not published yet.
        """
        if ann.empty:
            return [], "no announcements seen"
        codes = sorted(set(ann["code"]) & set(self.universe))
        if not codes:
            return [], "no announcements in the universe"
        today = now.astimezone(SYD).date()
        frames = self.daily_fresh(
            [self.index_ticker, *codes], session - timedelta(days=90)
        )  # fmt: skip

        def completed(df: pd.DataFrame | None) -> pd.DataFrame:
            if df is None or not len(df):
                return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            return df[df.index.date < today]

        idx = completed(frames.pop(self.index_ticker, None))
        if idx.empty or idx.index[-1].date() != session:
            raise NotReady(f"the ASX 200 has no completed daily bar for {session} yet")
        frames = {c: completed(f) for c, f in frames.items()}
        frames = {c: f for c, f in frames.items() if len(f) > self.turnover_window + 5}
        if not frames:
            return [], "no daily history for the announcing stocks"
        # The gap is measured against the index's open on `session` and its close the session
        # before. Yahoo's index series does drop days (22 Sep 2026, fetched the next day); the
        # backtest's code would then find no event at all, silently. Refuse, and say so.
        days = idx.index
        for f in frames.values():
            days = days.union(f.index)
        need = days[days <= pd.Timestamp(session)][-2:]
        missing = [d.date().isoformat() for d in need if d not in idx.index]
        if missing:
            raise NotReady(f"the ASX 200's daily series is missing {', '.join(missing)}")

        if self.index_open_source == "first_minute_bar":
            real = self.broker.minutes.index_open(self.index_ticker, session)
            if real is None:
                # No silent fallback to the daily open: that is a different rule, and one
                # day of it would be mixed into the record unseen.
                raise NotReady(
                    f"the ASX 200 has no 10:00 minute bar for {session} (its real open, "
                    "TRACKER #27)"
                )
            idx = idx.copy()
            idx.loc[idx.index[-1], "open"] = real
        panels = build_panels(
            frames, idx["open"], idx["close"], self.turnover_floor, self.turnover_window
        )
        ev_b = detect_events(panels, self.gap_pct, self.vol_mult, self.vol_window)
        # Today is a session too: an announcement released after 10:00 on `session` reacts
        # today, and must not be mapped back onto `session` for want of a later date.
        sessions = panels.dates.union(pd.DatetimeIndex([pd.Timestamp(today)]))
        ev_a = announcement_events(ev_b, ann, sessions)
        ev_a = ev_a[ev_a["date"] == pd.Timestamp(session)]
        if ev_a.empty:
            return [], f"no A event confirmed on {session}"

        held = set(acct.positions)
        waiting = {
            o.ticker for o in acct.orders.values()
            if o.status == "pending_fill" and o.side in OPENING_SIDES
        }  # fmt: skip
        equity = acct.equity(self.broker.prices(acct))
        out, notes = [], []
        for r in ev_a.itertuples():
            if r.ticker in held or r.ticker in waiting:
                notes.append(f"{r.ticker} already held")
                continue
            close = float(panels.close.at[r.date, r.ticker])
            limit = round_to_tick(close * (1 + ENTRY_CAP_PCT / 100.0), up=True)
            stop = round_to_tick(limit * (1 - self.stop_pct / 100.0), up=False)
            qty = self._size(equity, limit, stop)
            if qty <= 0:
                notes.append(f"{r.ticker} size rounds to zero")
                continue
            out.append(
                BotDecision(
                    ticker=r.ticker,
                    side="buy",
                    qty=qty,
                    limit=limit,
                    stop=stop,
                    stop_pct=self.stop_pct,
                    reason=(
                        f"{self.label}: price-sensitive announcement reacting on {session}, "
                        f"opened {r.gap_rel:+.1f}% vs the ASX 200 on {r.vol_mult:.1f}x the "
                        f"20-day volume (rule: >= {self.gap_pct}% and >= {self.vol_mult}x), "
                        f"confirmed at that close. Entered at the next open, limit "
                        f"{ENTRY_CAP_PCT:.0f}% over the close; stop {self.stop_pct}% below the "
                        f"fill; time exit after {self.hold_days} sessions. No model was used."
                    ),
                )
            )
        why = f"{len(ev_a)} A event(s) confirmed on {session}" + (
            f" ({'; '.join(notes)})" if notes else ""
        )
        return out, why

    def _size(self, equity: float, limit: float, stop: float) -> int:
        """The level's risk cap decides how many shares an 8% stop allows.

        Then the smallest of: equity, the playbook's max position share of equity, and the
        per-order guard `arena.guards.max_order_value_aud`, which arena_place_order enforces
        on the order's limit value. Without the guard here, an account whose 40% share grew
        past $8,000 would size an order the broker refuses outright - a missed trade, not a
        smaller one.
        """
        risk_each = limit - stop
        if risk_each <= 0:
            return 0
        risk_budget = equity * self.playbook.level.risk_per_trade_pct / 100.0
        qty = int(math.floor(min(risk_budget / risk_each, equity / limit)))
        max_pct = self.playbook.guidance("max_position_pct_of_equity")
        if max_pct is not None:
            qty = min(qty, int(math.floor(equity * float(max_pct) / 100.0 / limit)))
        max_order = float((self.cfg.get("arena.guards") or {}).get("max_order_value_aud", 8000))
        qty = min(qty, int(math.floor(max_order / limit + 1e-9)))
        return max(qty, 0)

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
