"""The arena broker: fake money, deferred fills, leverage, ASX shorts, daily marks.

This is NOT the real-money path. `asxbot.broker.orders.place_order` (human approval on
every call) is untouched and remains the only route to a real broker.

What this broker does:
  * takes an order and records it `pending_fill`, stamped with its own clock at that moment
    (`decided_at`) and with the time of the data the decision was made on (`data_as_of`);
  * later fills it from the first traded minute bar that starts strictly after `decided_at`
    (see minutes.py), capped at the order's limit - a limit the market never reached rests,
    then expires once the delayed feed has caught up with the close;
  * enforces stops from the minute bars, with the gap rule, and - for the agent's accounts -
    take-profit targets the same way, mirrored, as resting limits;
  * charges IBKR brokerage both ways, adverse slippage on every fill (it stands in for the
    spread), and a daily borrow cost on short positions;
  * marks every account to market daily, which is what the daily loss limit and the
    scoreboard are measured against.

The order id it returns is the only proof an order exists. Nothing upstream - and no agent -
may decide an order was placed or filled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot.arena.accounts import Account, AccountStore, ArenaOrder, Mark, Position
from asxbot.arena.minutes import MinuteBars, NoTradeYet, first_minute_after
from asxbot.backtest.costs import CostModel
from asxbot.log import EventLog, get_logger

log = get_logger("asxbot.arena.broker")
SYD = ZoneInfo("Australia/Sydney")
SESSION_CLOSE = time(16, 10)

OPENING_SIDES = ("buy", "short")
CLOSING_SIDES = ("sell", "cover")


@dataclass
class FillOutcome:
    order_id: str
    status: str
    detail: str


class ArenaBroker:
    def __init__(
        self,
        data_dir: Path,
        costs: CostModel,
        minutes: MinuteBars,
        adv_lookup,  # callable(ticker) -> average daily dollar turnover or None
        short_borrow_pct_annual: float = 3.0,
        resolve_after_minutes: int = 22,
        max_wait_minutes: int = 390,
        clock=None,  # callable() -> aware datetime; the wall clock unless a test sets one
    ):
        self.store = AccountStore(data_dir)
        self.costs = costs
        self.minutes = minutes
        self.adv_lookup = adv_lookup
        self.short_borrow_pct_annual = float(short_borrow_pct_annual)
        self.resolve_after_minutes = int(resolve_after_minutes)
        self.max_wait_minutes = int(max_wait_minutes)
        self.clock = clock or (lambda: datetime.now(SYD))
        self.events = EventLog(data_dir)
        self.notifier = None  # set by build_arena; alerts are best effort (notify.py)

    def _notify(self, method: str, *args) -> None:
        if self.notifier is not None:
            getattr(self.notifier, method)(*args)

    # -- placing ------------------------------------------------------------
    def submit(
        self,
        acct: Account,
        *,
        ticker: str,
        side: str,
        qty: int,
        limit: float,
        data_as_of: datetime | None = None,
        stop: float | None = None,
        target: float | None = None,
        reason: str = "",
        model: str = "",
        placed_by: str = "",
        hold: str = "intraday",
        stop_pct: float | None = None,
    ) -> ArenaOrder:
        """Record an order. It is NOT filled here - fills happen in resolve_pending().

        The decision time is this broker's clock now, as the order is recorded. No caller
        can supply it: a caller's `now` is the time its picture of the market was taken,
        which may be minutes old by the time an agent has finished deciding, and it is kept
        as `data_as_of` so the gap is visible.
        """
        decided = self.clock().astimezone(SYD)
        as_of = (data_as_of or decided).astimezone(SYD)
        oid = self.store.next_order_id()
        o = ArenaOrder(
            order_id=oid,
            account=acct.name,
            ticker=ticker.upper(),
            side=side,
            qty=int(qty),
            limit=float(limit),
            decided_at=decided.isoformat(timespec="seconds"),
            data_as_of=as_of.isoformat(timespec="seconds"),
            stop=stop,
            stop_pct=stop_pct,
            target=target,
            reason=reason,
            model=model,
            placed_by=placed_by,
            hold=hold,
            message="recorded; waiting for the first traded minute after the decision",
        )
        acct.orders[oid] = o
        self.store.save(acct)
        self.events.append("arena_orders", {**o.to_dict(), "event": "submitted"})
        log.info(
            "arena %s %s %s %s %d @ %.3f (decided %s, on data read at %s, %s earlier) "
            "-> pending fill",
            acct.name, oid, side, o.ticker, o.qty, o.limit, o.decided_at, o.data_as_of,
            _gap(as_of, decided),
        )  # fmt: skip
        return o

    # -- filling ------------------------------------------------------------
    def resolve_pending(self, acct: Account, now: datetime | None = None) -> list[FillOutcome]:
        """Fill every pending order the free feed has now caught up with."""
        now = now or datetime.now(SYD)
        out: list[FillOutcome] = []
        for o in list(acct.orders.values()):
            if o.status != "pending_fill":
                continue
            decided = _aware(o.decided_at)
            if (now - decided) < timedelta(minutes=self.resolve_after_minutes):
                continue  # the delayed feed has not caught up with that minute yet
            out.append(self._resolve_one(acct, o, now))
        self.store.save(acct)
        return out

    def _resolve_one(self, acct: Account, o: ArenaOrder, now: datetime) -> FillOutcome:
        decided = _aware(o.decided_at)
        try:
            first = self.minutes.fill_at(o.ticker, decided, self.max_wait_minutes)
        except NoTradeYet as e:
            return FillOutcome(o.order_id, "pending_fill", str(e))
        chosen, slip = self.find_fill(o, decided)

        if chosen is None:
            # Did the session it was decided in already finish, and has the delayed feed
            # caught up with its close? Then it can never fill.
            if _session_over(decided, now, self.resolve_after_minutes):
                o.status = "expired"
                o.message = (
                    f"limit {o.limit:.3f} was never met between {decided:%H:%M} and the close; "
                    f"first traded minute was {first.minute:%H:%M} at {first.price:.3f}"
                )
                self.events.append("arena_orders", {**o.to_dict(), "event": "expired"})
                log.info("arena %s %s expired: %s", acct.name, o.order_id, o.message)
                self._notify("expired", o)
                return FillOutcome(o.order_id, "expired", o.message)
            return FillOutcome(o.order_id, "pending_fill", "limit not met yet; still resting")

        ts, px, raw = chosen
        return self._apply_fill(acct, o, px, ts, self.fill_basis(o, decided, ts, raw, slip))

    def find_fill(
        self, o: ArenaOrder, decided: datetime
    ) -> tuple[tuple[datetime, float, float] | None, float]:
        """((bar minute, fill price, raw bar price) or None, slippage fraction).

        Walks traded minutes from the first one that starts strictly after the decision. The
        first whose price, after slippage, meets the order's limit is the fill; the walk
        stops at the decision session's close. None means the limit has not been met in the
        bars the feed holds so far.
        """
        adv = self.adv_lookup(o.ticker)
        slip = self.costs.slippage_pct(abs(o.qty) * o.limit, adv)
        for ts, _bar, raw in self.minutes.traded_minutes(o.ticker, first_minute_after(decided)):
            px = raw * (1 + slip) if o.side in ("buy", "cover") else raw * (1 - slip)
            if o.side in ("buy", "cover") and px <= o.limit + 1e-9:
                return (ts, px, raw), slip
            if o.side in ("sell", "short") and px >= o.limit - 1e-9:
                return (ts, px, raw), slip
            if ts.date() > decided.date() or (ts.time() >= SESSION_CLOSE):
                break
        return None, slip

    def fill_basis(
        self, o: ArenaOrder, decided: datetime, ts: datetime, raw: float, slip: float
    ) -> str:
        return (
            f"{self.minutes.price_field} of the {ts:%Y-%m-%d %H:%M} minute bar, the first "
            f"traded bar after the decision at {decided:%H:%M:%S} that met the limit, at "
            f"{raw:.4f}, {'plus' if o.side in ('buy', 'cover') else 'less'} "
            f"{slip * 100:.3f}% slippage"
        )

    def _apply_fill(
        self, acct: Account, o: ArenaOrder, price: float, minute: datetime, basis: str
    ) -> FillOutcome:
        qty = abs(int(o.qty))
        value = qty * price
        fee = self.costs.brokerage(value)
        pos = acct.positions.get(o.ticker)
        realised = 0.0
        if o.stop_pct is not None and o.side in OPENING_SIDES:
            d = float(o.stop_pct) / 100.0
            o.stop = round(price * (1 - d) if o.side == "buy" else price * (1 + d), 4)
        o = _stop_rescaled_to_fill(o, price)

        if o.side == "buy":
            acct.cash -= value + fee
            if pos is None:
                acct.positions[o.ticker] = Position(
                    ticker=o.ticker, qty=qty, avg_cost=price,
                    opened_at=minute.isoformat(timespec="minutes"), stop=o.stop, target=o.target,
                    thesis=o.reason, opened_by=o.placed_by, model=o.model,
                    last_borrow_day=minute.date().isoformat(), hold=o.hold,
                    target_from=_target_from(o, minute),
                )  # fmt: skip
            else:
                total = pos.qty + qty
                pos.avg_cost = (pos.avg_cost * pos.qty + price * qty) / total
                pos.qty = total
                if o.stop is not None:
                    pos.stop = o.stop
                _move_target(pos, o, minute)
        elif o.side == "short":
            acct.cash += value - fee
            if pos is None:
                acct.positions[o.ticker] = Position(
                    ticker=o.ticker, qty=-qty, avg_cost=price,
                    opened_at=minute.isoformat(timespec="minutes"), stop=o.stop, target=o.target,
                    thesis=o.reason, opened_by=o.placed_by, model=o.model,
                    last_borrow_day=minute.date().isoformat(), hold=o.hold,
                    target_from=_target_from(o, minute),
                )  # fmt: skip
            else:
                total = pos.qty - qty  # more negative
                pos.avg_cost = (pos.avg_cost * abs(pos.qty) + price * qty) / abs(total)
                pos.qty = total
                if o.stop is not None:
                    pos.stop = o.stop
                _move_target(pos, o, minute)
        elif o.side == "sell":
            if pos is None or pos.qty < qty:
                o.status = "rejected"
                o.message = "no long position of that size to sell"
                return FillOutcome(o.order_id, "rejected", o.message)
            acct.cash += value - fee
            realised = (price - pos.avg_cost) * qty
            acct.realised_pnl += realised
            pos.qty -= qty
            if pos.qty == 0:
                del acct.positions[o.ticker]
        elif o.side == "cover":
            if pos is None or pos.qty > -qty:
                o.status = "rejected"
                o.message = "no short position of that size to cover"
                return FillOutcome(o.order_id, "rejected", o.message)
            acct.cash -= value + fee
            realised = (pos.avg_cost - price) * qty
            acct.realised_pnl += realised
            pos.qty += qty
            if pos.qty == 0:
                del acct.positions[o.ticker]
        else:
            o.status = "rejected"
            o.message = f"bad side {o.side!r}"
            return FillOutcome(o.order_id, "rejected", o.message)

        acct.fees_paid += fee
        o.realised = round(realised, 2)
        o.status = "filled"
        o.filled_qty = qty
        o.avg_price = round(price, 4)
        o.commission = round(fee, 2)
        o.fill_minute = minute.isoformat(timespec="minutes")
        o.fill_basis = basis
        o.message = "filled"
        self.events.append("arena_fills", {**o.to_dict(), "event": "filled"})
        log.info(
            "arena %s %s FILLED %s %s %d @ %.4f (fee %.2f) - %s",
            acct.name, o.order_id, o.side, o.ticker, qty, price, fee, basis,
        )  # fmt: skip
        after = acct.positions.get(o.ticker)
        self._notify("filled", o, after is None, after.stop if after is not None else None)
        return FillOutcome(o.order_id, "filled", basis)

    # -- stops and targets ----------------------------------------------------
    def apply_exits(self, acct: Account, now: datetime | None = None) -> list[FillOutcome]:
        """Trigger any stop or take-profit target the minute bars have already reached.

        Always on, never skipped. Both are checked from the minute AFTER the position opened.
        The entry bar is excluded because the entry filled at that bar's close (the default
        `arena.fill.minute_price`), so its high and low happened before the position existed.
        Until 2026-09-23 the stop scan included it: a bar that dipped 8% and recovered to
        close at the fill price registered as both the entry and the stop, for two lots of
        brokerage and an instant loss on a move the position never saw.

        Stop: a long exits when a bar's low reaches it, a short when a bar's high does. The
        fill is the stop, or the bar's open when the bar gapped straight through it -
        whichever is worse - then slippage.

        Target, agent accounts only (the yardstick's frozen rule has no target, so a bot
        position's target is never honoured): a resting take-profit, the mirror image of
        the stop. A long exits when a bar's high reaches it, a short when a bar's low does.
        The fill is the target, or the bar's open when the price gapped through it -
        whichever is better, as a resting limit fills - then slippage, the same as every
        other arena fill. Until 2026-09-24 the target was stored and never acted on, though
        the decider set it believing it was a take-profit. 42364dc filled targets with no
        slippage, which flattered the agent's account against its yardstick; target fills pay
        it from 2026-09-23.

        One bar reaching both: the stop is taken. A minute bar does not say which came
        first, so the arena assumes the worse.

        A position opened before targets were honoured has its target honoured from the
        first time this runs on it, and from then on it is checked exactly like any other
        (_arm_target). If the price is already past the target at that moment, nothing
        special happens: the target rests from there. The first bar after it that reaches
        the target fills it - at that bar's open if the open is at or beyond the target,
        otherwise at the target - and if the price has fallen back, the position holds until
        a later bar reaches the target. 42364dc instead sold such a position at the first
        open whatever that open was, which is a market order, not a take-profit.
        """
        now = now or datetime.now(SYD)
        out: list[FillOutcome] = []
        armed = False
        for ticker, pos in list(acct.positions.items()):
            if pos.qty == 0:
                continue
            since = _minute_after(pos.opened_at)  # opened_at is the entry bar's minute
            long = pos.qty > 0
            stop_hit = None
            if pos.stop is not None:
                stop_hit = self.minutes.first_trigger(
                    ticker, since, float(pos.stop), "down" if long else "up", until=now.date()
                )
            target_hit = None
            if acct.kind == "agent" and pos.target is not None:
                if not pos.target_from:
                    # Armed at `now`, so `now` must be fresh: the watcher reads the clock
                    # again just before calling this, never reusing its cycle's start time.
                    self._arm_target(acct, pos, now)
                    armed = True
                t_since = max(since, _minute_after(pos.target_from))
                target_hit = self.minutes.first_trigger(
                    ticker, t_since, float(pos.target), "up" if long else "down",
                    until=now.date(),
                )  # fmt: skip
            if stop_hit is not None and (target_hit is None or stop_hit[0] <= target_hit[0]):
                out.append(self._exit_at_stop(acct, ticker, pos, *stop_hit))
            elif target_hit is not None:
                out.append(self._exit_at_target(acct, ticker, pos, *target_hit))
        if out or armed:
            self.store.save(acct)
        return out

    def _exit_at_stop(
        self, acct: Account, ticker: str, pos: Position, ts: datetime, raw: float
    ) -> FillOutcome:
        adv = self.adv_lookup(ticker)
        slip = self.costs.slippage_pct(abs(pos.qty) * raw, adv)
        side = "sell" if pos.qty > 0 else "cover"
        px = raw * (1 - slip) if side == "sell" else raw * (1 + slip)
        oid = self.store.next_order_id()
        o = ArenaOrder(
            order_id=oid, account=acct.name, ticker=ticker, side=side, qty=abs(pos.qty),
            limit=round(px, 4), **self._resting_times(ts, pos.opened_at),
            reason=f"STOP hit at {pos.stop:.3f}", model="code (stop, not the agent)",
            placed_by="code",
        )  # fmt: skip
        acct.orders[oid] = o
        basis = (
            f"stop {pos.stop:.3f} reached in the {ts:%Y-%m-%d %H:%M} minute bar; "
            f"filled at {raw:.4f} after the gap rule, then slippage"
        )
        return self._apply_fill(acct, o, px, ts, basis)

    def _exit_at_target(
        self, acct: Account, ticker: str, pos: Position, ts: datetime, raw: float
    ) -> FillOutcome:
        adv = self.adv_lookup(ticker)
        slip = self.costs.slippage_pct(abs(pos.qty) * raw, adv)
        side = "sell" if pos.qty > 0 else "cover"
        px = raw * (1 - slip) if side == "sell" else raw * (1 + slip)
        reason = f"TARGET reached at {pos.target:.3f}"
        if pos.target_past_when_armed:
            reason += (
                f" (it was already past when targets began to be honoured, {pos.target_from},"
                " and rested from there)"
            )
        basis = (
            f"target {pos.target:.3f} reached in the {ts:%Y-%m-%d %H:%M} minute bar; "
            f"{raw:.4f} (the target, or the bar's open if it gapped through), "
            f"{'less' if side == 'sell' else 'plus'} {slip * 100:.3f}% slippage"
        )
        oid = self.store.next_order_id()
        o = ArenaOrder(
            order_id=oid, account=acct.name, ticker=ticker, side=side, qty=abs(pos.qty),
            limit=round(px, 4),
            **self._resting_times(ts, _later(pos.opened_at, pos.target_from)),
            reason=reason, model="code (target, not the agent)", placed_by="code",
        )  # fmt: skip
        acct.orders[oid] = o
        return self._apply_fill(acct, o, px, ts, basis)

    def _resting_times(self, trigger: datetime, rests_from: str) -> dict:
        """The three times on a stop or target exit. The level had been resting since
        `rests_from` (the entry minute, or the minute the target was armed) and the bar that
        reached it starts after that; the code records the exit when it notices it."""
        return {
            "decided_at": self.clock().astimezone(SYD).isoformat(timespec="seconds"),
            "data_as_of": trigger.astimezone(SYD).isoformat(timespec="seconds"),
            "rests_from": _aware(rests_from).isoformat(timespec="minutes"),
        }

    def _arm_target(self, acct: Account, pos: Position, now: datetime) -> None:
        """Start honouring the target of a position opened before targets were honoured.

        From the minute after this one it rests like any other target. Exiting at an
        earlier bar, when nothing was acting on it, would be a trade in the past. Whether
        the last traded price was already past it is recorded, for the audit trail only.
        """
        at = now.astimezone(SYD).replace(second=0, microsecond=0)
        target = float(pos.target)
        px = self.minutes.price_at(pos.ticker, at)
        pos.target_from = at.isoformat(timespec="minutes")
        pos.target_past_when_armed = px is not None and (
            px >= target if pos.qty > 0 else px <= target
        )
        if px is None:
            log.warning(
                "arena %s %s: target %.4f honoured from %s; no recent price is known, so "
                "it is checked from here like any other",
                acct.name, pos.ticker, target, pos.target_from,
            )  # fmt: skip
        else:
            log.info(
                "arena %s %s: target %.4f honoured from %s; last price %.4f%s",
                acct.name, pos.ticker, target, pos.target_from, px,
                " is already past it; the target rests from here, and the next bar to reach "
                "it fills it (at its open if that open is at or beyond the target)"
                if pos.target_past_when_armed else "",
            )  # fmt: skip
        self.events.append(
            "arena_orders",
            {
                "account": acct.name, "ticker": pos.ticker, "event": "target_armed",
                "target": target, "target_from": pos.target_from, "last_price": px,
                "past_when_armed": pos.target_past_when_armed,
            },
        )  # fmt: skip

    # -- carrying costs and marks -------------------------------------------
    def accrue_borrow(self, acct: Account, today: date | None = None) -> float:
        """Charge the short borrow for each calendar day a short was held."""
        today = today or datetime.now(SYD).date()
        charged = 0.0
        for pos in acct.positions.values():
            if pos.qty >= 0:
                continue
            last = date.fromisoformat(pos.last_borrow_day) if pos.last_borrow_day else today
            days = (today - last).days
            if days <= 0:
                continue
            px = self.minutes.last_price(pos.ticker) or pos.avg_cost
            fee = abs(pos.qty) * px * (self.short_borrow_pct_annual / 100.0) * days / 365.0
            pos.borrow_accrued += fee
            pos.last_borrow_day = today.isoformat()
            acct.cash -= fee
            acct.borrow_paid += fee
            charged += fee
        if charged:
            self.store.save(acct)
            log.info("arena %s short borrow charged %.2f", acct.name, charged)
        return charged

    def prices(self, acct: Account, day: date | None = None) -> dict[str, float]:
        out: dict[str, float] = {}
        for t, pos in acct.positions.items():
            px = self.minutes.last_price(t, day)
            out[t] = px if px is not None else pos.avg_cost
        return out

    def mark_to_market(self, acct: Account, day: date | None = None, note: str = "") -> Mark:
        """Write today's mark. Open positions count at their current price."""
        day = day or datetime.now(SYD).date()
        prices = self.prices(acct, day)
        prior = [m for m in self.store.marks(acct.name) if m.day < day.isoformat()]
        prev_equity = prior[-1].equity if prior else acct.starting_cash
        prev_realised = sum(m.realised_day for m in prior)
        prev_fees = sum(m.fees_day for m in prior)
        equity = acct.equity(prices)
        mark = Mark(
            day=day.isoformat(),
            equity=round(equity, 2),
            cash=round(acct.cash, 2),
            gross_exposure=round(acct.gross_exposure(prices), 2),
            positions=len(acct.positions),
            realised_day=round(acct.realised_pnl - prev_realised, 2),
            fees_day=round(acct.fees_paid + acct.borrow_paid - prev_fees, 2),
            note=note or f"prev equity {prev_equity:,.2f}",
        )
        self.store.write_mark(acct.name, mark)
        self.events.append("arena_marks", {"account": acct.name, **mark.to_dict()})
        return mark

    def day_loss_pct(self, acct: Account, now: datetime | None = None) -> float:
        """Today's mark-to-market move as a percentage of the day's starting equity.

        Negative is a loss. This is what the level's daily loss limit is measured against.
        """
        now = now or datetime.now(SYD)
        start = self.store.day_start_equity(acct, now.date())
        if start <= 0:
            return 0.0
        return (acct.equity(self.prices(acct, now.date())) - start) / start * 100.0


def _stop_rescaled_to_fill(o: ArenaOrder, price: float) -> ArenaOrder:
    """Keep a stop on the correct side of the price it actually filled at.

    A stop is chosen as a DISTANCE from the intended entry ("8% below"), but it is carried
    as a level. With deferred fills the true price can differ from the delayed quote the
    level was computed against, and a long could fill below its own stop - which would open
    a position and stop it out in the same minute for two lots of brokerage and no trade.

    So when a fill lands on the wrong side of its own stop, the stop is re-derived at the
    same proportional distance from the price that was actually paid. The risk the order
    was checked against is unchanged in percentage terms, and the position gets the stop
    its author meant.
    """
    if o.stop is None or o.side not in OPENING_SIDES or not o.limit:
        return o
    wrong_side = (o.side == "buy" and price <= o.stop) or (o.side == "short" and price >= o.stop)
    if not wrong_side:
        return o
    distance = abs(o.limit - o.stop) / o.limit
    new_stop = price * (1 - distance) if o.side == "buy" else price * (1 + distance)
    log.warning(
        "arena %s %s filled at %.4f, on the wrong side of its stop %.4f; the stop is "
        "re-derived at the same %.2f%% distance from the fill: %.4f",
        o.account, o.order_id, price, o.stop, distance * 100, new_stop,
    )  # fmt: skip
    o.message = (
        f"{o.message}; stop moved from {o.stop:.4f} to {new_stop:.4f} to stay "
        f"{distance * 100:.2f}% from the actual fill"
    ).strip("; ")
    o.stop = round(new_stop, 4)
    return o


def _minute_after(iso_minute: str) -> datetime:
    """The minute after a stored minute: where a stop or target check begins."""
    t = datetime.fromisoformat(iso_minute)
    if t.tzinfo is None:
        t = t.replace(tzinfo=SYD)
    return t + timedelta(minutes=1)


def _target_from(o: ArenaOrder, minute: datetime) -> str:
    """A new position's target is honoured from its entry minute (checked from the next)."""
    return minute.isoformat(timespec="minutes") if o.target is not None else ""


def _move_target(pos: Position, o: ArenaOrder, minute: datetime) -> None:
    """An add that names a target moves the position's target, as one naming a stop does."""
    if o.target is None:
        return
    pos.target = o.target
    pos.target_from = minute.isoformat(timespec="minutes")
    pos.target_past_when_armed = False


def _session_over(decided: datetime, now: datetime, feed_delay_minutes: int = 0) -> bool:
    """True once the ASX session the order was decided in has finished AND the delayed feed
    has had time to show its last minutes. Until 2026-09-23 this ignored the delay, so a
    resting limit could expire at 16:11 with the feed still twenty minutes short of the
    close - bars that might have filled it not yet visible."""
    if now.date() > decided.date():
        return True
    close = datetime.combine(decided.date(), SESSION_CLOSE, tzinfo=SYD)
    return now >= close + timedelta(minutes=feed_delay_minutes)


def _aware(iso: str) -> datetime:
    t = datetime.fromisoformat(iso)
    return t if t.tzinfo is not None else t.replace(tzinfo=SYD)


def _later(a: str, b: str) -> str:
    """The later of two stored minutes; an empty one is ignored."""
    if not b:
        return a
    return a if _aware(a) >= _aware(b) else b


def _gap(earlier: datetime, later: datetime) -> str:
    secs = int((later - earlier).total_seconds())
    sign = "-" if secs < 0 else ""
    secs = abs(secs)
    return f"{sign}{secs // 60}m{secs % 60:02d}s"
