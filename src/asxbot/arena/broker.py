"""The arena broker: fake money, deferred fills, leverage, ASX shorts, daily marks.

This is NOT the real-money path. `asxbot.broker.orders.place_order` (human approval on
every call) is untouched and remains the only route to a real broker.

What this broker does:
  * takes an order and records it `pending_fill` with the decision timestamp;
  * later fills it from the minute bar covering that minute (see minutes.py), capped at
    the order's limit - a limit the market never reached rests, then expires at the close;
  * enforces stops from the minute bars, with the gap rule;
  * charges IBKR brokerage both ways, adverse slippage (which stands in for the spread),
    and a daily borrow cost on short positions;
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
from asxbot.arena.minutes import MinuteBars, NoTradeYet
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
    ):
        self.store = AccountStore(data_dir)
        self.costs = costs
        self.minutes = minutes
        self.adv_lookup = adv_lookup
        self.short_borrow_pct_annual = float(short_borrow_pct_annual)
        self.resolve_after_minutes = int(resolve_after_minutes)
        self.max_wait_minutes = int(max_wait_minutes)
        self.events = EventLog(data_dir)

    # -- placing ------------------------------------------------------------
    def submit(
        self,
        acct: Account,
        *,
        ticker: str,
        side: str,
        qty: int,
        limit: float,
        decision_at: datetime,
        stop: float | None = None,
        target: float | None = None,
        reason: str = "",
        model: str = "",
        placed_by: str = "",
    ) -> ArenaOrder:
        """Record an order. It is NOT filled here - fills happen in resolve_pending()."""
        oid = f"ARN-{acct.next_id:06d}"
        acct.next_id += 1
        o = ArenaOrder(
            order_id=oid,
            account=acct.name,
            ticker=ticker.upper(),
            side=side,
            qty=int(qty),
            limit=float(limit),
            decision_at=decision_at.astimezone(SYD).isoformat(timespec="seconds"),
            stop=stop,
            target=target,
            reason=reason,
            model=model,
            placed_by=placed_by,
            message="recorded; waiting for the true minute price at the decision time",
        )
        acct.orders[oid] = o
        self.store.save(acct)
        self.events.append("arena_orders", {**o.to_dict(), "event": "submitted"})
        log.info(
            "arena %s %s %s %s %d @ %.3f (decision %s) -> pending fill",
            acct.name,
            oid,
            side,
            o.ticker,
            o.qty,
            o.limit,
            o.decision_at,
        )
        return o

    # -- filling ------------------------------------------------------------
    def resolve_pending(self, acct: Account, now: datetime | None = None) -> list[FillOutcome]:
        """Fill every pending order the free feed has now caught up with."""
        now = now or datetime.now(SYD)
        out: list[FillOutcome] = []
        for o in list(acct.orders.values()):
            if o.status != "pending_fill":
                continue
            decided = datetime.fromisoformat(o.decision_at)
            if (now - decided) < timedelta(minutes=self.resolve_after_minutes):
                continue  # the delayed feed has not caught up with that minute yet
            out.append(self._resolve_one(acct, o, now))
        self.store.save(acct)
        return out

    def _resolve_one(self, acct: Account, o: ArenaOrder, now: datetime) -> FillOutcome:
        decided = datetime.fromisoformat(o.decision_at)
        try:
            first = self.minutes.fill_at(o.ticker, decided, self.max_wait_minutes)
        except NoTradeYet as e:
            return FillOutcome(o.order_id, "pending_fill", str(e))
        adv = self.adv_lookup(o.ticker)
        slip = self.costs.slippage_pct(abs(o.qty) * o.limit, adv)

        # Walk traded minutes from the decision minute. The first one whose price meets the
        # order's limit is the fill. A limit the market never reached rests, then expires.
        chosen = None
        for ts, _bar, raw in self.minutes.traded_minutes(o.ticker, decided):
            px = raw * (1 + slip) if o.side in ("buy", "cover") else raw * (1 - slip)
            if o.side in ("buy", "cover") and px <= o.limit + 1e-9:
                chosen = (ts, px, raw)
                break
            if o.side in ("sell", "short") and px >= o.limit - 1e-9:
                chosen = (ts, px, raw)
                break
            if ts.date() > decided.date() or (ts.time() >= SESSION_CLOSE):
                break

        if chosen is None:
            # Did the session it was decided in already finish? Then it can never fill.
            if _session_over(decided, now):
                o.status = "expired"
                o.message = (
                    f"limit {o.limit:.3f} was never met between {decided:%H:%M} and the close; "
                    f"first traded minute was {first.minute:%H:%M} at {first.price:.3f}"
                )
                self.events.append("arena_orders", {**o.to_dict(), "event": "expired"})
                log.info("arena %s %s expired: %s", acct.name, o.order_id, o.message)
                return FillOutcome(o.order_id, "expired", o.message)
            return FillOutcome(o.order_id, "pending_fill", "limit not met yet; still resting")

        ts, px, raw = chosen
        waited = int((ts - decided.replace(second=0, microsecond=0)).total_seconds() // 60)
        basis = (
            f"{self.minutes.price_field} of the {ts:%Y-%m-%d %H:%M} minute bar "
            f"(+{waited} min after the decision) at {raw:.4f}, "
            f"{'plus' if o.side in ('buy', 'cover') else 'less'} {slip * 100:.3f}% slippage"
        )
        return self._apply_fill(acct, o, px, ts, basis)

    def _apply_fill(
        self, acct: Account, o: ArenaOrder, price: float, minute: datetime, basis: str
    ) -> FillOutcome:
        qty = abs(int(o.qty))
        value = qty * price
        fee = self.costs.brokerage(value)
        pos = acct.positions.get(o.ticker)
        realised = 0.0

        if o.side == "buy":
            acct.cash -= value + fee
            if pos is None:
                acct.positions[o.ticker] = Position(
                    ticker=o.ticker, qty=qty, avg_cost=price,
                    opened_at=minute.isoformat(timespec="minutes"), stop=o.stop, target=o.target,
                    thesis=o.reason, opened_by=o.placed_by, model=o.model,
                    last_borrow_day=minute.date().isoformat(),
                )  # fmt: skip
            else:
                total = pos.qty + qty
                pos.avg_cost = (pos.avg_cost * pos.qty + price * qty) / total
                pos.qty = total
                if o.stop is not None:
                    pos.stop = o.stop
        elif o.side == "short":
            acct.cash += value - fee
            if pos is None:
                acct.positions[o.ticker] = Position(
                    ticker=o.ticker, qty=-qty, avg_cost=price,
                    opened_at=minute.isoformat(timespec="minutes"), stop=o.stop, target=o.target,
                    thesis=o.reason, opened_by=o.placed_by, model=o.model,
                    last_borrow_day=minute.date().isoformat(),
                )  # fmt: skip
            else:
                total = pos.qty - qty  # more negative
                pos.avg_cost = (pos.avg_cost * abs(pos.qty) + price * qty) / abs(total)
                pos.qty = total
                if o.stop is not None:
                    pos.stop = o.stop
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
        return FillOutcome(o.order_id, "filled", basis)

    # -- stops --------------------------------------------------------------
    def apply_stops(self, acct: Account, now: datetime | None = None) -> list[FillOutcome]:
        """Trigger any stop the minute bars have already reached. Always on, never skipped.

        A stop is checked from the minute the position opened. The fill is the stop price,
        or the bar's open when the bar gapped straight through it - whichever is worse.
        """
        now = now or datetime.now(SYD)
        out: list[FillOutcome] = []
        for ticker, pos in list(acct.positions.items()):
            if pos.stop is None or pos.qty == 0:
                continue
            since = datetime.fromisoformat(pos.opened_at)
            if since.tzinfo is None:
                since = since.replace(tzinfo=SYD)
            direction = "down" if pos.qty > 0 else "up"
            hit = self.minutes.first_trigger(ticker, since, float(pos.stop), direction)
            if hit is None:
                continue
            ts, raw = hit
            adv = self.adv_lookup(ticker)
            slip = self.costs.slippage_pct(abs(pos.qty) * raw, adv)
            side = "sell" if pos.qty > 0 else "cover"
            px = raw * (1 - slip) if side == "sell" else raw * (1 + slip)
            oid = f"ARN-{acct.next_id:06d}"
            acct.next_id += 1
            o = ArenaOrder(
                order_id=oid, account=acct.name, ticker=ticker, side=side, qty=abs(pos.qty),
                limit=round(px, 4), decision_at=ts.isoformat(timespec="seconds"),
                reason=f"STOP hit at {pos.stop:.3f}", model="code (stop, not the agent)",
                placed_by="code",
            )  # fmt: skip
            acct.orders[oid] = o
            basis = (
                f"stop {pos.stop:.3f} reached in the {ts:%Y-%m-%d %H:%M} minute bar; "
                f"filled at {raw:.4f} after the gap rule, then slippage"
            )
            out.append(self._apply_fill(acct, o, px, ts, basis))
        if out:
            self.store.save(acct)
        return out

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


def _session_over(decided: datetime, now: datetime) -> bool:
    """True once the ASX session the order was decided in has finished."""
    if now.date() > decided.date():
        return True
    return now.date() == decided.date() and now.time() > SESSION_CLOSE
