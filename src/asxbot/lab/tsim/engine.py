"""The clock: one simulated trading day for one trader.

  pre-open (default 09:40)  the trader is woken with the overnight news, its positions and
                            orders carried from yesterday, and its journal; orders it sends now
                            join the opening auction
  each minute 10:00-16:11   the bar that just finished is delivered (20 s after it closed);
                            the broker works every order against it; plain code checks the
                            trader's alerts; if anything the trader asked for happened (or it
                            is a rules trader that looks every minute) it is woken
  after the close (16:20)   the trader's end-of-day call (the AI writes its journal)
  overnight                 short borrow is charged, the account is marked at the close

The trader's thinking time moves the clock: what it decides at T after thinking L seconds
reaches the broker at T+L, and fills only from bars that start after that. While it thinks,
the market keeps moving and its resting orders keep working; anything that happens meanwhile
wakes it once it is free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from asxbot.lab.tsim.alerts import AlertBook
from asxbot.lab.tsim.broker import SimBroker
from asxbot.lab.tsim.market import FEED_LAG, SLOTS, SYD, Market, slot_time
from asxbot.lab.tsim.tools import TraderView


@dataclass
class Decision:
    actions: list = field(default_factory=list)  # order actions (tools.TraderView.do)
    alerts: list = field(default_factory=list)  # alert specs to add
    clear_alerts: list = field(default_factory=list)
    next_wake: str | None = None  # "HH:MM"
    note: str = ""
    latency_s: float = 0.0
    usage: dict = field(default_factory=dict)  # tokens and cost of this wake
    calls: int = 0
    journal: str = ""  # end of day only


class Trader:
    """A trader: rules (plain code) or the AI. Subclasses implement `wake`."""

    name = "trader"
    every_minute = False  # rules traders look at every bar; the AI only when woken
    kind = "rules"

    def pre_open(self, view: TraderView, ctx: dict) -> Decision | None:
        return None

    def wake(self, view: TraderView, reasons: list[dict], ctx: dict) -> Decision | None:
        return None

    def after_close(self, view: TraderView, ctx: dict) -> Decision | None:
        return None


@dataclass
class DayResult:
    day: str
    wakes: int = 0
    calls: int = 0
    usage: dict = field(default_factory=dict)
    think_s: float = 0.0
    fills: int = 0
    rejected: list = field(default_factory=list)
    equity_close: float = 0.0
    pnl: float = 0.0
    notes: list = field(default_factory=list)
    journal: str = ""
    wall_s: float = 0.0


def _add_usage(total: dict, u: dict) -> None:
    for k, v in (u or {}).items():
        if isinstance(v, (int, float)):
            total[k] = round(total.get(k, 0) + v, 6)


def run_day(
    market: Market,
    broker: SimBroker,
    trader: Trader,
    alerts: AlertBook,
    view: TraderView,
    *,
    preopen_at: time = time(9, 40),
    after_close_at: time = time(16, 20),
    nights: int = 1,
    ctx: dict | None = None,
) -> DayResult:
    import time as wall

    t0 = wall.monotonic()
    day = market.day
    ctx = dict(ctx or {})
    res = DayResult(day=day.isoformat())
    broker.market = market
    alerts.new_day()
    start_equity = broker.acct.equity(lambda c: market.prev_close(c))
    pending: list[tuple[datetime, Decision]] = []
    busy_until = datetime.combine(day, time(0, 0), tzinfo=SYD)
    queued: list[dict] = []

    def apply(at: datetime, d: Decision) -> None:
        saved = market.now
        market.now = at
        for a in d.actions:
            r = view.do(a, at, trader.name)
            if not r.get("ok"):
                res.rejected.append(r)
                queued.append({"type": "rejected", **r})
        for spec in d.alerts:
            r = alerts.add(spec, view.anon)
            if not r.get("ok"):
                queued.append({"type": "alert_refused", **r})
        alerts.clear(d.clear_alerts)
        if d.next_wake:
            alerts.add({"type": "time", "at": d.next_wake, "note": "call-back"}, view.anon)
        if d.note:
            res.notes.append(f"{at:%H:%M:%S} {d.note[:500]}")
        market.now = saved

    def call(fn, *args) -> None:
        nonlocal busy_until
        d = fn(view, *args)
        res.wakes += 1
        if d is None:
            return
        res.calls += d.calls
        res.think_s += d.latency_s
        _add_usage(res.usage, d.usage)
        at = market.now + timedelta(seconds=max(0.0, d.latency_s))
        if trader.kind == "rules":
            at = market.now + timedelta(seconds=1)  # plain code: a second
        busy_until = at
        if d.journal:
            res.journal = d.journal
        pending.append((at, d))

    # -- pre-open
    market.now = datetime.combine(day, preopen_at, tzinfo=SYD)
    alerts.mark_news_seen(view) if ctx.get("news_seen_at_preopen", True) else None
    call(trader.pre_open, {**ctx, "phase": "pre_open"})

    for slot in range(SLOTS):
        bar_start = slot_time(day, slot)
        delivered = slot_time(day, slot + 1) + FEED_LAG
        # decisions that reached the broker before this bar was delivered are in the book
        for at, d in sorted([p for p in pending if p[0] <= delivered], key=lambda p: p[0]):
            apply(at, d)
        pending[:] = [p for p in pending if p[0] > delivered]
        market.now = max(delivered, bar_start)
        fills = broker.work_slot(slot)
        res.fills += len(fills)
        reasons = alerts.check(view, slot)
        evs = broker.events
        broker.events = []
        for e in evs:
            if e["kind"] in ("fill", "cancelled", "cancelled_partial", "expired",
                             "expired_partial", "filled"):  # fmt: skip
                queued.append({"type": "order", **e})
        queued.extend(reasons)
        if market.now < busy_until:
            continue
        if queued or trader.every_minute:
            why = queued[:]
            queued.clear()
            call(trader.wake, why, {**ctx, "phase": "session", "slot": slot})

    # anything still thinking at the close lands after it
    end = datetime.combine(day, after_close_at, tzinfo=SYD)
    for at, d in sorted(pending, key=lambda p: p[0]):
        apply(at, d)
    pending.clear()
    market.now = end
    broker.events = []
    call(trader.after_close, {**ctx, "phase": "after_close"})
    for at, d in pending:
        apply(max(at, end), d)
    pending.clear()
    # day orders left (placed after the close) expire; the night
    broker._expire_day_orders(end)
    broker.events = []
    borrow = broker.overnight(nights, lambda c: market.last(c))
    eq = broker.acct.equity(market.last)
    broker.acct.marks.append({"day": day.isoformat(), "equity": round(eq, 2),
                              "cash": round(broker.acct.cash, 2), "borrow": borrow,
                              "positions": len(broker.acct.positions)})  # fmt: skip
    res.equity_close = round(eq, 2)
    res.pnl = round(eq - start_equity, 2)
    res.wall_s = round(wall.monotonic() - t0, 2)
    return res
