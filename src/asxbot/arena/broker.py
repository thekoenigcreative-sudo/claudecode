"""The arena broker: fake money, deferred fills, leverage, ASX shorts, daily marks.

This is NOT the real-money path. `asxbot.broker.orders.place_order` (human approval on
every call) is untouched and remains the only route to a real broker.

What this broker does:
  * takes an order and records it `pending_fill`, stamped with its own clock at that moment
    (`decided_at`) and with the time of the data the decision was made on (`data_as_of`);
  * later fills it from the first traded minute bar that starts strictly after `decided_at`
    (see minutes.py), capped at the order's limit - a limit the market never reached rests,
    then expires once the delayed feed has caught up with the close. An order recorded before
    the open joins the opening auction first, at the auction's price (from 2026-09-24,
    TRACKER #28), as do stops and targets the auction price has gone through;
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
from asxbot.arena.minutes import (
    AUCTION_MINUTE,
    MinuteBars,
    NoTradeYet,
    _price_of,
    first_minute_after,
    is_auction,
)
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
        max_volume_share: float = 0.20,
        settle_minutes: int = 0,
        opening_auction: str = "daily_open",
        auction_volume_share: float = 0.20,
        auction_wait_minutes: int = 30,
    ):
        self.store = AccountStore(data_dir)
        self.costs = costs
        self.minutes = minutes
        self.adv_lookup = adv_lookup
        self.short_borrow_pct_annual = float(short_borrow_pct_annual)
        self.resolve_after_minutes = int(resolve_after_minutes)
        self.max_wait_minutes = int(max_wait_minutes)
        self.clock = clock or (lambda: datetime.now(SYD))
        # No minute bar fills more than this share of the shares it traded (#25).
        if not 0.0 < float(max_volume_share) <= 1.0:
            raise ValueError(
                f"arena.fill.max_volume_share must be in (0, 1], got {max_volume_share}"
            )
        self.max_volume_share = float(max_volume_share)
        # Minutes kept back behind the newest row of an intraday fetch (minutes.final_bars).
        self.settle_minutes = int(settle_minutes)
        # The opening auction (#28): `daily_open` fills orders at the open at the auction's
        # price; `first_minute` is the rule before 2026-09-24, the first traded minute after.
        if opening_auction not in ("daily_open", "first_minute"):
            raise ValueError(
                "arena.fill.opening_auction must be daily_open|first_minute, "
                f"got {opening_auction!r}"
            )
        self.opening_auction = opening_auction
        if not 0.0 < float(auction_volume_share) <= 1.0:
            raise ValueError(
                f"arena.fill.auction_volume_share must be in (0, 1], got {auction_volume_share}"
            )
        self.auction_volume_share = float(auction_volume_share)
        self.auction_wait_minutes = int(auction_wait_minutes)
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

    # -- filling: the order book, worked bar by bar ----------------------------
    def resolve_pending(self, acct: Account, now: datetime | None = None) -> list[FillOutcome]:
        """Work the account's orders, stops and targets through the bars the feed now holds.

        The same pass as apply_exits since 2026-09-24 (work): entries, exits, stops and
        targets in one ticker share each bar's volume, so they have to be worked together,
        in time order. The watcher calls both; the second finds nothing new to do.
        """
        return self.work(acct, now)

    def apply_exits(self, acct: Account, now: datetime | None = None) -> list[FillOutcome]:
        """Trigger any stop or take-profit target the minute bars have reached, and fill it.

        The same pass as resolve_pending (see work). Always on, never skipped.

        Both are checked from the minute AFTER the position opened. The entry bar is
        excluded because the entry filled at that bar's close (the default
        `arena.fill.minute_price`), so its high and low happened before the position existed.
        Until 2026-09-23 the stop scan included it: a bar that dipped 8% and recovered to
        close at the fill price registered as both the entry and the stop, for two lots of
        brokerage and an instant loss on a move the position never saw.

        Stop: a long exits when a bar's low reaches it, a short when a bar's high does. From
        then on it is a market order. In the bar that reached it, it fills at the stop, or
        at the bar's open when the bar gapped straight through it - whichever is worse; in
        any later bar it needs (see work), at that bar's price. Then slippage.

        Target, agent accounts only (the yardstick's frozen rule has no target, so a bot
        position's target is never honoured): a resting take-profit, the mirror image of
        the stop. A long's target is reached when a bar's high reaches it, a short's when a
        bar's low does. It fills at the target, or at the bar's open when the price gapped
        through it - whichever is better, as a resting limit fills - then slippage, the same
        as every other arena fill. It stays a limit: a later bar fills the rest only if that
        bar reaches the target too. Until 2026-09-24 the target was stored and never acted
        on, though the decider set it believing it was a take-profit. 42364dc filled targets
        with no slippage, which flattered the agent's account against its yardstick; target
        fills pay it from 2026-09-23.

        The stop always has priority. One bar reaching both: the stop is taken (a minute
        bar does not say which came first, so the arena assumes the worse). A stop reached
        while a target exit is still filling cancels the rest of the target and takes over
        the whole remaining position, and it is filled first from every bar's volume.

        A position opened before targets were honoured has its target honoured from the
        first time this runs on it, and from then on it is checked exactly like any other
        (_arm_target). If the price is already past the target at that moment, nothing
        special happens: the target rests from there. The first bar after it that reaches
        the target fills it - at that bar's open if the open is at or beyond the target,
        otherwise at the target - and if the price has fallen back, the position holds until
        a later bar reaches the target. 42364dc instead sold such a position at the first
        open whatever that open was, which is a market order, not a take-profit.
        """
        return self.work(acct, now)

    def work(self, acct: Account, now: datetime | None = None) -> list[FillOutcome]:
        """Work every live order, stop and target of the account through the minute bars
        that are final by `now`, one bar at a time per ticker. Each bar is worked once.

        Volume (TRACKER #25, 2026-09-24). No bar fills more than `max_volume_share` of the
        shares it traded (arena.fill.max_volume_share, default 20%), rounded down; the rest
        of the order carries to later bars, at their prices. Until 2026-09-24 a bar filled
        any size: A1M's 3,000-share take-profit (ARN-000003) filled in the 15:57 bar, where
        1 share traded. The share is per account and per bar, shared by all of the
        account's orders in that ticker in this order: a stop exit, then a target exit,
        then everything else in the order it was recorded. The agent's account and its
        yardstick's are separate books and do not take volume from each other.

        What happens to an order still part-filled at the close depends on what it is:
          * An order someone placed (the agent, the yardstick, the pre-close sweep, the
            horizon exit, a person) is a DAY limit order, as an ASX order through IBKR is by
            default. It works only in its own session - the one it was recorded in, or the
            next one if it was recorded after the 16:10 closing auction or on a day the ASX
            did not trade. Once that session is over and the delayed feed has caught up with
            its close, the rest expires: `partial` if some of it filled, `expired` if none.
            The position keeps whatever did fill.
          * A stop or target exit is raised by the broker to get a position out, and keeps
            working, across sessions, until the position is out. Leaving a triggered stop
            half-done overnight would leave the position half-protected; a real stop-loss
            order would still be working in the morning.
        When a stop or target exit is raised, every other working order in that ticker is
        cancelled (its fills stand): the exit takes the whole position.

        The opening auction (TRACKER #28, 2026-09-24). Each day's bars are led by its opening
        auction (minutes.opening_auction), stamped 09:59: one price, Yahoo's daily open, and
        an estimated volume. It is worked like a bar. An order recorded before 09:59:00 joins
        it; a stop or target the auction price has reached is raised there and fills at the
        auction price. No more than `auction_volume_share` of the auction's estimated volume
        fills in it, shared in the same order as a bar's; the rest carries into the minute
        bars. With no auction price the arena will trust, the orders that would have joined it
        fill at the first traded minute after it, as before, and their fill basis says so.
        """
        now = (now or datetime.now(SYD)).astimezone(SYD)
        out: list[FillOutcome] = []
        for pos in list(acct.positions.values()):
            if acct.kind == "agent" and pos.target is not None and pos.qty and not pos.target_from:
                # Armed at `now`, so `now` must be fresh: the watcher reads the clock
                # again just before calling this, never reusing its cycle's start time.
                self._arm_target(acct, pos, now)
        tickers = {o.ticker for o in acct.orders.values() if o.working}
        tickers |= {t for t, p in acct.positions.items() if p.qty and self._guarded(acct, p)}
        for ticker in sorted(tickers):
            start = self._work_start(acct, ticker)
            if start is None:
                continue
            wait = self.auction_wait_minutes if self.opening_auction == "daily_open" else None
            for ts, bar in self.minutes.final_bars(
                ticker, start, now, self.settle_minutes, self.resolve_after_minutes, wait
            ):
                if is_auction(bar) and not bar["available"]:
                    self._no_auction(acct, ticker, ts, str(bar["reason"]))
                    continue
                out.extend(self._work_bar(acct, ticker, ts, bar))
        out.extend(self._end_sessions(acct, now))
        self.store.save(acct)
        return out

    @staticmethod
    def _guarded(acct: Account, pos: Position) -> bool:
        return pos.stop is not None or (acct.kind == "agent" and pos.target is not None)

    def _work_start(self, acct: Account, ticker: str) -> datetime | None:
        """The earliest bar anything in this ticker still needs."""
        starts = []
        for o in acct.orders.values():
            if o.working and o.ticker == ticker:
                starts.append(_order_from(o))
        pos = acct.positions.get(ticker)
        if pos is not None and pos.qty and self._guarded(acct, pos):
            starts.append(_minute_after(pos.worked_through or pos.opened_at))
        return min(starts) if starts else None

    def _work_bar(self, acct: Account, ticker: str, ts: datetime, bar) -> list[FillOutcome]:
        out: list[FillOutcome] = []
        minute = ts.isoformat(timespec="minutes")
        # 1. The position's stop, then its target. The stop is always looked at first.
        pos = acct.positions.get(ticker)
        if pos is not None and pos.qty and ts > _aware(pos.worked_through or pos.opened_at):
            pos.worked_through = minute
            long = pos.qty > 0
            live = [o for o in acct.orders.values() if o.working and o.ticker == ticker]
            if (
                pos.stop is not None
                and not any(o.order_type == "stop" for o in live)
                and _reaches(bar, float(pos.stop), "down" if long else "up")
            ):
                out.extend(self._raise_exit(acct, pos, "stop", ts, bar, live))
            elif (
                acct.kind == "agent"
                and pos.target is not None
                and not any(o.order_type in ("stop", "target") for o in live)
                and ts > _aware(_later(pos.opened_at, pos.target_from))
                and _reaches(bar, float(pos.target), "up" if long else "down")
            ):
                out.extend(self._raise_exit(acct, pos, "target", ts, bar, live))

        # 2. The bar's volume, shared: stop exit, target exit, then the rest by age. The
        # opening auction has its own share of its estimated volume.
        share = self.auction_volume_share if is_auction(bar) else self.max_volume_share
        room = int(float(bar["volume"]) * share)
        live = [o for o in acct.orders.values() if o.working and o.ticker == ticker]
        for o in sorted(live, key=_priority):
            if ts < _order_from(o):
                continue
            if o.order_type == "limit" and ts.date() != _session_day(_aware(o.decided_at)):
                continue  # outside its session; _end_sessions expires it
            o.worked_through = minute
            raw = self._bar_price(o, ts, bar)
            if raw is None:
                continue
            px = raw * (1 + self._slip(o)) if o.side in ("buy", "cover") else raw * (
                1 - self._slip(o)
            )
            if o.order_type == "limit" and not _meets(o, px):
                continue
            qty = min(o.remaining, room)
            if o.side in CLOSING_SIDES:
                held = acct.positions.get(ticker)
                have = 0 if held is None else (held.qty if o.side == "sell" else -held.qty)
                if have <= 0:
                    out.append(self._cancel(acct, o, f"no {ticker} position left to close"))
                    continue
                qty = min(qty, have)
            if qty <= 0:
                continue
            out.append(self._fill_slice(acct, o, qty, px, raw, ts, bar))
            room -= qty
        return out

    def _no_auction(self, acct: Account, ticker: str, ts: datetime, reason: str) -> None:
        """No auction price to trust: the orders that would have joined it are told why they
        fill at the first traded minute after it."""
        for o in acct.orders.values():
            if o.working and o.ticker == ticker and not o.fills and ts >= _order_from(o):
                o.open_note = (
                    f"no opening auction price for {ticker} on {ts:%d %b} ({reason}); filled at "
                    "the first traded minute after it instead"
                )

    def _bar_price(self, o: ArenaOrder, ts: datetime, bar) -> float | None:
        """The raw price this order trades at in this bar, before slippage; None if it
        cannot trade in it."""
        if o.order_type == "stop":
            if ts == _aware(o.data_as_of):  # the bar that reached the stop: the gap rule
                return _gap_price(bar, float(o.limit), worse=True, sell=o.side == "sell")
            return _price_of(bar, self.minutes.price_field)  # a market order from then on
        if o.order_type == "target":
            if not _reaches(bar, float(o.limit), "up" if o.side == "sell" else "down"):
                return None
            return _gap_price(bar, float(o.limit), worse=False, sell=o.side == "sell")
        return _price_of(bar, self.minutes.price_field)

    def _slip(self, o: ArenaOrder) -> float:
        """The slippage fraction for this order, from its full size, as every fill used."""
        if o.order_type == "limit":
            return self.costs.slippage_pct(abs(o.qty) * o.limit, self.adv_lookup(o.ticker))
        return self.costs.slippage_pct(abs(o.qty) * float(o.trigger_price), self.adv_lookup(
            o.ticker
        ))

    def _raise_exit(
        self, acct: Account, pos: Position, kind: str, ts: datetime, bar, live: list
    ) -> list[FillOutcome]:
        """A stop or target has been reached in bar `ts`: record the exit that works the
        whole position out, and cancel every other working order in the ticker."""
        long = pos.qty > 0
        side = "sell" if long else "cover"
        level = float(pos.stop if kind == "stop" else pos.target)
        trigger = _gap_price(bar, level, worse=kind == "stop", sell=long)
        cancelled = [
            self._cancel(acct, other, f"the {kind} at {level:.3f} was reached at {ts:%H:%M}")
            for other in live
        ]
        if kind == "stop":
            reason = f"STOP hit at {level:.3f}"
            model = "code (stop, not the agent)"
            rests = pos.opened_at
        else:
            reason = f"TARGET reached at {level:.3f}"
            if pos.target_past_when_armed:
                reason += (
                    f" (it was already past when targets began to be honoured, "
                    f"{pos.target_from}, and rested from there)"
                )
            model = "code (target, not the agent)"
            rests = _later(pos.opened_at, pos.target_from)
        o = ArenaOrder(
            order_id=self.store.next_order_id(), account=acct.name, ticker=pos.ticker,
            side=side, qty=abs(pos.qty), limit=round(level, 4),
            **self._resting_times(ts, rests), order_type=kind, trigger_price=trigger,
            reason=reason, model=model, placed_by="code", hold=pos.hold,
            message=f"{kind} reached in the {ts:%Y-%m-%d %H:%M} bar; working",
        )  # fmt: skip
        acct.orders[o.order_id] = o
        self.events.append("arena_orders", {**o.to_dict(), "event": f"{kind}_reached"})
        log.info(
            "arena %s %s %s %s reached %.4f in the %s bar: %s %d working",
            acct.name, o.order_id, pos.ticker, kind, level, ts.strftime("%H:%M"), side, o.qty,
        )  # fmt: skip
        return cancelled

    def _cancel(self, acct: Account, o: ArenaOrder, why: str) -> FillOutcome:
        o.status = "partial" if o.filled_qty else "cancelled"
        o.message = (
            f"filled {o.filled_qty:,} of {abs(o.qty):,}; the rest cancelled: {why}"
            if o.filled_qty else f"cancelled unfilled: {why}"
        )  # fmt: skip
        self._finish(acct, o)
        return FillOutcome(o.order_id, o.status, o.message)

    def _end_sessions(self, acct: Account, now: datetime) -> list[FillOutcome]:
        """A day order whose session is over, with the delayed feed caught up with its
        close, is done: the rest of it expires."""
        out = []
        for o in list(acct.orders.values()):
            if not o.working or o.order_type != "limit":
                continue
            decided = _aware(o.decided_at)
            day = _session_day(decided)
            close = datetime.combine(day, SESSION_CLOSE, tzinfo=SYD)
            if now < close + timedelta(minutes=self.resolve_after_minutes):
                continue
            if o.filled_qty:
                o.status = "partial"
                o.message = (
                    f"filled {o.filled_qty:,} of {abs(o.qty):,} by the {day:%d %b} close, no bar "
                    f"filling more than {self.max_volume_share:.0%} of its volume; the other "
                    f"{o.remaining:,} expired with the day"
                )
                log.info("arena %s %s part-filled and done: %s", acct.name, o.order_id, o.message)
                self._finish(acct, o)
            else:
                o.status = "expired"
                o.message = (
                    f"limit {o.limit:.3f} was never met between {decided:%H:%M} and the close"
                )
                try:
                    first = self.minutes.fill_at(o.ticker, decided, self.max_wait_minutes)
                    o.message += (
                        f"; first traded minute was {first.minute:%H:%M} at {first.price:.3f}"
                    )
                except NoTradeYet:
                    o.message += "; the stock did not trade after it was recorded"
                self.events.append("arena_orders", {**o.to_dict(), "event": "expired"})
                log.info("arena %s %s expired: %s", acct.name, o.order_id, o.message)
                self._notify("expired", o)
            out.append(FillOutcome(o.order_id, o.status, o.message))
        return out

    def _fill_slice(
        self, acct: Account, o: ArenaOrder, qty: int, price: float, raw: float, ts: datetime,
        bar,
    ) -> FillOutcome:
        """Fill `qty` of the order in bar `ts` at `price` (after slippage) and book it."""
        day = ts.date().isoformat()
        before = sum(f["qty"] * f["price"] for f in o.fills if f["minute"][:10] == day)
        # IBKR charges one commission per order per day, however many executions it takes,
        # so the minimum is paid once a day, not once a bar.
        fee = self.costs.brokerage(before + qty * price) - (
            self.costs.brokerage(before) if before else 0.0
        )
        first_slice = not o.fills
        o.fills.append({
            "minute": ts.isoformat(timespec="minutes"), "qty": int(qty),
            "price": price, "bar_price": raw,
            "bar_volume": float(bar["volume"]),
            **({"auction": True} if is_auction(bar) else {}),
        })  # fmt: skip
        o.filled_qty += int(qty)
        avg = sum(f["qty"] * f["price"] for f in o.fills) / o.filled_qty
        pos = acct.positions.get(o.ticker)
        realised = 0.0
        if o.side in OPENING_SIDES:
            if o.stop_pct is not None:
                d = float(o.stop_pct) / 100.0
                o.stop = round(avg * (1 - d) if o.side == "buy" else avg * (1 + d), 4)
            o = _stop_rescaled_to_fill(o, avg)
            sign = 1 if o.side == "buy" else -1
            if o.side == "buy":
                acct.cash -= qty * price + fee
            else:
                acct.cash += qty * price - fee
            if pos is None:
                acct.positions[o.ticker] = Position(
                    ticker=o.ticker, qty=sign * qty, avg_cost=price,
                    opened_at=ts.isoformat(timespec="minutes"), stop=o.stop, target=o.target,
                    thesis=o.reason, opened_by=o.placed_by, model=o.model,
                    last_borrow_day=ts.date().isoformat(), hold=o.hold,
                    target_from=_target_from(o, ts),
                )  # fmt: skip
            else:
                total = abs(pos.qty) + qty
                pos.avg_cost = (pos.avg_cost * abs(pos.qty) + price * qty) / total
                pos.qty = sign * total
                if o.stop is not None:
                    pos.stop = o.stop
                if first_slice:
                    _move_target(pos, o, ts)
        else:
            if o.side == "sell":
                acct.cash += qty * price - fee
                realised = (price - pos.avg_cost) * qty
                pos.qty -= qty
            else:
                acct.cash -= qty * price + fee
                realised = (pos.avg_cost - price) * qty
                pos.qty += qty
            acct.realised_pnl += realised
            if pos.qty == 0:
                del acct.positions[o.ticker]
        acct.fees_paid += fee
        o.realised = round(o.realised + realised, 2)
        o.avg_price = round(avg, 4)
        o.commission = round(o.commission + fee, 2)
        if first_slice:
            o.fill_minute = ts.isoformat(timespec="minutes")
            o.fill_basis = self._first_basis(o, ts, raw, bar)
        self.events.append(
            "arena_fill_slices",
            {"account": acct.name, "order_id": o.order_id, "ticker": o.ticker, "side": o.side,
             "order_type": o.order_type, **o.fills[-1], "fee": round(fee, 4),
             "filled_qty": o.filled_qty, "qty": abs(o.qty)},
        )  # fmt: skip
        if o.remaining == 0:
            o.status = "filled"
            o.message = "filled"
            self._finish(acct, o)
            return FillOutcome(o.order_id, "filled", o.fill_basis)
        o.message = (
            f"part-filled: {o.filled_qty:,} of {abs(o.qty):,} so far, no bar filling more than "
            f"{self.max_volume_share:.0%} of its volume; the rest is working"
        )
        log.info(
            "arena %s %s part-filled %s %s %d @ %.4f in the %s bar (volume %s): %d of %d",
            acct.name, o.order_id, o.side, o.ticker, qty, price, ts.strftime("%H:%M"),
            f"{float(bar['volume']):,.0f}", o.filled_qty, abs(o.qty),
        )  # fmt: skip
        return FillOutcome(o.order_id, "pending_fill", o.message)

    def _first_basis(self, o: ArenaOrder, ts: datetime, raw: float, bar=None) -> str:
        basis = self._basis(o, ts, raw, bar)
        return f"{basis}; {o.open_note}" if o.open_note else basis

    def _auction_basis(self, ts: datetime, bar) -> str:
        return (
            f"the {ts:%Y-%m-%d} opening auction at {float(bar['close']):.4f} (Yahoo's daily "
            f"open; TRACKER #28), at most {self.auction_volume_share:.0%} of its estimated "
            f"volume {float(bar['volume']):,.0f} (the daily volume less the minute bars', an "
            "upper bound)"
        )

    def _basis(self, o: ArenaOrder, ts: datetime, raw: float, bar=None) -> str:
        if bar is not None and is_auction(bar):
            side = "less" if o.side in ("sell", "short") else "plus"
            if o.order_type in ("stop", "target"):
                return (
                    f"{o.order_type} {o.limit:.3f} reached at {self._auction_basis(ts, bar)}; "
                    f"filled at {raw:.4f}, {side} {self._slip(o) * 100:.3f}% slippage"
                )
            return (
                f"{self._auction_basis(ts, bar)}; recorded at {_aware(o.decided_at):%H:%M:%S}, "
                f"before the auction; {side} {self._slip(o) * 100:.3f}% slippage"
            )
        if o.order_type == "stop":
            reached = _aware(o.data_as_of)
            how = (
                "after the gap rule" if ts == reached
                else f"in the {ts:%H:%M} bar, a market order once the stop was reached"
            )  # fmt: skip
            return (
                f"stop {o.limit:.3f} reached in {_bar_name(reached)}; "
                f"filled at {raw:.4f} {how}, then slippage"
            )
        if o.order_type == "target":
            reached = _aware(o.data_as_of)
            where = (
                f"reached in {_bar_name(ts)}"
                if ts == reached
                else f"reached in {_bar_name(reached)}, first filled in the {ts:%H:%M} bar"
            )
            return (
                f"target {o.limit:.3f} {where}; {raw:.4f} (the target, or the bar's open if it "
                f"gapped through), {'less' if o.side == 'sell' else 'plus'} "
                f"{self._slip(o) * 100:.3f}% slippage"
            )
        return self.fill_basis(o, _aware(o.decided_at), ts, raw, self._slip(o))

    def _finish(self, acct: Account, o: ArenaOrder) -> None:
        """An order is done (filled, partial or cancelled): say so once, in full."""
        if len(o.fills) > 1:
            first, last = (datetime.fromisoformat(o.fills[i]["minute"]) for i in (0, -1))
            fmt = "%H:%M" if first.date() == last.date() else "%d %b %H:%M"
            o.fill_basis += (
                f"; filled over {len(o.fills)} bars, {first:{fmt}} to {last:{fmt}}, no bar "
                f"filling more than {self.max_volume_share:.0%} of its traded volume"
                + (
                    f" (the opening auction, stamped 09:59: {self.auction_volume_share:.0%} "
                    "of its estimated volume)"
                    if any(f.get("auction") for f in o.fills) else ""
                )
                + f"; average {o.avg_price:.4f} after slippage"
            )
        if not o.filled_qty:
            self.events.append("arena_orders", {**o.to_dict(), "event": o.status})
            log.info("arena %s %s %s: %s", acct.name, o.order_id, o.status, o.message)
            return
        self.events.append("arena_fills", {**o.to_dict(), "event": o.status})
        log.info(
            "arena %s %s %s %s %s %d of %d @ %.4f (fee %.2f) - %s",
            acct.name, o.order_id, o.status.upper(), o.side, o.ticker, o.filled_qty,
            abs(o.qty), o.avg_price, o.commission, o.fill_basis,
        )  # fmt: skip
        after = acct.positions.get(o.ticker)
        self._notify("filled", o, after is None, after.stop if after is not None else None)

    def find_fill(
        self, o: ArenaOrder, decided: datetime
    ) -> tuple[tuple[datetime, float, float] | None, float]:
        """((bar minute, fill price, raw bar price) or None, slippage fraction).

        The single-bar rule before 2026-09-24, kept only for scripts/correct_fill_arn000002.py,
        which re-priced one order under it (volume did not bind there: 3,000 shares against
        36,051 traded in the bar). Every live fill goes through work().

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


def _bar_name(ts: datetime) -> str:
    """How a fill's bar is named: the opening auction has its own stamp (AUCTION_MINUTE)."""
    ts = ts.astimezone(SYD)
    if ts.time() == AUCTION_MINUTE:
        return f"the {ts:%Y-%m-%d} opening auction"
    return f"the {ts:%Y-%m-%d %H:%M} minute bar"


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


def _session_day(decided: datetime) -> date:
    """The session a day order works in: the one it was recorded in, or - recorded at or
    after the 16:10 closing auction, at a weekend or on an ASX holiday - the next one."""
    local = decided.astimezone(SYD)
    day = local.date()
    if local.time() >= SESSION_CLOSE:
        day += timedelta(days=1)
    while not _is_session(day):
        day += timedelta(days=1)
    return day


def _is_session(day: date) -> bool:
    if day.weekday() >= 5:
        return False
    try:
        from asxbot.announcements.live import is_trading_day

        return bool(is_trading_day(day))
    except Exception:  # noqa: BLE001 - a calendar that does not reach this far: weekdays
        return True


def _order_from(o: ArenaOrder) -> datetime:
    """The first bar an order may still use: the one after the last it was worked against;
    for a new exit, the bar that reached its level; for a new order, the first bar that
    starts strictly after it was recorded."""
    if o.worked_through:
        return _minute_after(o.worked_through)
    if o.order_type in ("stop", "target"):
        return _aware(o.data_as_of).replace(second=0, microsecond=0)
    return first_minute_after(_aware(o.decided_at))


def _priority(o: ArenaOrder) -> tuple:
    rank = {"stop": 0, "target": 1}.get(o.order_type, 2)
    return (rank, _aware(o.decided_at), o.order_id)


def _reaches(bar, level: float, direction: str) -> bool:
    """direction 'down': the bar's low reached the level; 'up': its high did."""
    if direction == "down":
        return float(bar["low"]) <= level
    return float(bar["high"]) >= level


def _gap_price(bar, level: float, *, worse: bool, sell: bool) -> float:
    """The level itself, or the bar's open when the bar opened through it: the worse of the
    two for a stop, the better for a take-profit (a resting limit fills at the better)."""
    op = float(bar["open"])
    if worse:
        return min(level, op) if sell else max(level, op)
    return max(level, op) if sell else min(level, op)


def _meets(o: ArenaOrder, px: float) -> bool:
    if o.side in ("buy", "cover"):
        return px <= o.limit + 1e-9
    return px >= o.limit - 1e-9


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
