"""The simulated broker: a trader's own orders against the market, with realistic fills.

Order types: market, limit, stop (stop-market), stop_limit, trailing_stop (by percent),
moc / loc (market or limit on close: the closing auction). Sides: buy, sell, short, cover
(short = a sell that opens or adds to a short; only stocks on the borrowable list). Time in
force: day (expires after the closing auction) or gtc (carries to the next day). Orders can be
modified (price, stop, quantity) and cancelled. An entry can carry a bracket (`attach`: a stop,
a target and/or a trailing stop), placed as reduce-only, one-cancels-other exits as the entry
fills.

How fills work - never better than the bar allows:
  * an order is worked only in bars that START after it reached the broker (`submitted_at`,
    which includes the trader's thinking time);
  * a marketable order takes the bar's OPEN (the first trade after it arrived) plus half the
    modelled spread (at least a tick) plus impact growing with its slice of the bar's volume;
  * a resting limit fills only when the price trades THROUGH it (a buy needs a low strictly
    below its limit), at its limit, with no spread (it is the passive side);
  * a stop triggers when the bar reaches it; if the bar opened through it (a gap), the
    reference is the open, not the stop;
  * no bar fills more than `max_volume_share` (20%, the arena's figure) of the volume it traded,
    across all the account's orders in that stock; the rest carries to later bars;
  * the opening auction (the stock's first bar, market.py) fills orders that arrived before
    it at the auction price, no spread, capped at the same share of its volume; the closing
    auction (16:10) fills moc/loc orders, market orders sent in the pre-close (16:00-16:10)
    and resting limits the auction price crosses. No print, no fill.
  * commission (the primary broker's) per order on its cumulative filled value; short borrow
    nightly.
The broker returns fills and order states. A trader never decides an order filled.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime

from asxbot.lab.tsim.costs import CostModel, round_to_tick, tick_size
from asxbot.lab.tsim.market import (
    CLOSE_AUCTION_SLOT,
    CONTINUOUS_END_SLOT,
    SLOTS,
    Market,
    slot_time,
)

TYPES = ("market", "limit", "stop", "stop_limit", "trailing_stop", "moc", "loc")
SIDES = ("buy", "sell", "short", "cover")


class OrderRejected(ValueError):
    pass


@dataclass
class Order:
    id: str
    code: str
    side: str  # buy | sell (short/cover normalised to sell/buy; `opens_short` recorded)
    qty: int
    type: str
    submitted_at: str
    limit: float | None = None
    stop: float | None = None
    trail_pct: float | None = None
    tif: str = "day"
    status: str = "working"  # working | filled | cancelled | expired | rejected
    filled: int = 0
    value: float = 0.0  # cumulative filled value (AUD)
    avg_price: float | None = None
    commission: float = 0.0
    triggered: bool = False
    resting: bool = False  # has been through its first eligible bar without filling
    high_water: float | None = None  # trailing stops: best price since placed
    attach: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    parent: str | None = None
    oca: str | None = None
    reduce_only: bool = False
    by: str = ""
    why: str = ""
    fills: list = field(default_factory=list)
    closed_at: str = ""
    note: str = ""
    good_till: str = ""  # an ISO time: the order stops working at this moment (good-till-time)

    @property
    def remaining(self) -> int:
        return max(0, int(self.qty) - int(self.filled))

    @property
    def working(self) -> bool:
        return self.status == "working"

    @property
    def buy(self) -> bool:
        return self.side == "buy"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Position:
    code: str
    qty: int  # signed: + long, - short
    avg: float
    opened_at: str
    trade_id: str


@dataclass
class Trade:
    """One round trip: from flat to flat in one stock."""

    id: str
    code: str
    direction: str  # long | short
    opened_at: str
    closed_at: str = ""
    gross: float = 0.0  # realised price P&L
    fees: float = 0.0  # primary broker's commission on its orders' share
    borrow: float = 0.0
    max_qty: int = 0
    entry_value: float = 0.0
    orders: dict = field(default_factory=dict)  # order id -> filled value inside this trade
    by: str = ""

    @property
    def net(self) -> float:
        return self.gross - self.fees - self.borrow


@dataclass
class Account:
    name: str
    start_cash: float
    cash: float
    leverage: float = 1.0
    positions: dict = field(default_factory=dict)
    orders: dict = field(default_factory=dict)  # every order this run (id -> Order)
    trades: list = field(default_factory=list)  # closed Trades
    open_trades: dict = field(default_factory=dict)  # code -> Trade
    fees: float = 0.0
    borrow: float = 0.0
    next_id: int = 1
    marks: list = field(default_factory=list)  # [{"day", "equity", "cash", ...}]

    def equity(self, price_of) -> float:
        return self.cash + sum(p.qty * (price_of(c) or p.avg) for c, p in self.positions.items())

    def gross(self, price_of) -> float:
        return sum(abs(p.qty) * (price_of(c) or p.avg) for c, p in self.positions.items())

    def working(self, code: str | None = None) -> list[Order]:
        return [o for o in self.orders.values() if o.working and (code is None or o.code == code)]

    # -- persistence (continuity from one simulated day to the next) --------
    def to_dict(self) -> dict:
        return {
            "name": self.name, "start_cash": self.start_cash, "cash": self.cash,
            "leverage": self.leverage, "fees": self.fees, "borrow": self.borrow,
            "next_id": self.next_id, "marks": self.marks,
            "positions": {c: asdict(p) for c, p in self.positions.items()},
            "orders": {i: o.to_dict() for i, o in self.orders.items()},
            "trades": [asdict(t) for t in self.trades],
            "open_trades": {c: asdict(t) for c, t in self.open_trades.items()},
        }  # fmt: skip

    @classmethod
    def from_dict(cls, d: dict) -> Account:
        a = cls(d["name"], float(d["start_cash"]), float(d["cash"]), float(d.get("leverage", 1)))
        a.fees, a.borrow, a.next_id = float(d["fees"]), float(d["borrow"]), int(d["next_id"])
        a.marks = list(d.get("marks") or [])
        a.positions = {c: Position(**p) for c, p in (d.get("positions") or {}).items()}
        a.orders = {i: Order(**o) for i, o in (d.get("orders") or {}).items()}
        a.trades = [Trade(**t) for t in d.get("trades") or []]
        a.open_trades = {c: Trade(**t) for c, t in (d.get("open_trades") or {}).items()}
        return a


@dataclass
class Fill:
    order_id: str
    code: str
    side: str
    qty: int
    price: float
    at: str
    basis: str
    position_after: int


class SimBroker:
    def __init__(
        self,
        account: Account,
        costs: CostModel,
        max_volume_share: float = 0.20,
        shortable: set[str] | None = None,
    ):
        self.acct = account
        self.costs = costs
        self.max_volume_share = float(max_volume_share)
        self.shortable = {c.upper() for c in (shortable or set())}
        self.market: Market | None = None
        self.events: list[dict] = []  # fills and order changes since the trader last looked

    # ------------------------------------------------------------------ orders
    def _new_id(self) -> str:
        i = self.acct.next_id
        self.acct.next_id += 1
        return f"O{i:05d}"

    def price(self, code: str) -> float | None:
        return self.market.last(code) if self.market else None

    def place(
        self,
        code: str,
        side: str,
        qty: int,
        type: str = "market",  # noqa: A002
        *,
        at: datetime,
        limit: float | None = None,
        stop: float | None = None,
        trail_pct: float | None = None,
        tif: str = "day",
        attach: dict | None = None,
        reduce_only: bool = False,
        oca: str | None = None,
        parent: str | None = None,
        by: str = "",
        why: str = "",
        good_till: datetime | str | None = None,
    ) -> Order:
        code = code.upper()
        side = side.lower()
        type = type.lower()  # noqa: A001
        if side not in SIDES:
            raise OrderRejected(f"side must be one of {SIDES}")
        if type not in TYPES:
            raise OrderRejected(f"type must be one of {TYPES}")
        if tif not in ("day", "gtc"):
            raise OrderRejected("tif must be day or gtc")
        qty = int(qty)
        if qty <= 0:
            raise OrderRejected("quantity must be a positive whole number of shares")
        m = self.market
        if m is None or (code not in m.bars and m.prev_close(code) is None):
            raise OrderRejected(f"{code}: no such stock in the simulated market today")
        if type in ("limit", "stop_limit", "loc") and not (limit and limit > 0):
            raise OrderRejected(f"a {type} order needs a limit price")
        if type in ("stop", "stop_limit") and not (stop and stop > 0):
            raise OrderRejected(f"a {type} order needs a stop price")
        if type == "trailing_stop" and not (trail_pct and 0 < trail_pct < 50):
            raise OrderRejected("a trailing stop needs trail_pct between 0 and 50")
        buy = side in ("buy", "cover")
        pos = self.acct.positions.get(code)
        held = pos.qty if pos else 0
        pending_sells = sum(o.remaining for o in self.acct.working(code) if not o.buy
                            and not o.reduce_only)  # fmt: skip
        if not buy and not reduce_only:
            after = held - pending_sells - qty
            if after < 0 and code not in self.shortable:
                raise OrderRejected(
                    f"{code} is not on the borrowable list: a sell may not take the position "
                    f"below zero (held {held}, other sells working {pending_sells})"
                )
        ref = self.price(code) or (limit or stop or 0)
        opens = (buy and held >= 0) or (not buy and held <= 0)
        if opens and not reduce_only and ref > 0:
            eq = self.acct.equity(self.price)
            working_open = sum(
                o.remaining * (o.limit or o.stop or self.price(o.code) or 0)
                for o in self.acct.working()
                if not o.reduce_only
            )
            if self.acct.gross(self.price) + working_open + qty * ref > eq * self.acct.leverage:
                raise OrderRejected(
                    f"not enough buying power: equity ${eq:,.0f} x leverage "
                    f"{self.acct.leverage:g}, positions and working orders already "
                    f"${self.acct.gross(self.price) + working_open:,.0f}"
                )
        o = Order(
            id=self._new_id(), code=code, side="buy" if buy else "sell", qty=qty, type=type,
            submitted_at=at.isoformat(timespec="seconds"),
            limit=round_to_tick(limit, up=not buy) if limit else None,
            stop=round_to_tick(stop, up=buy) if stop else None,
            trail_pct=trail_pct, tif=tif, attach=dict(attach or {}), reduce_only=reduce_only,
            oca=oca, parent=parent, by=by, why=why[:300],
            good_till=(good_till.isoformat(timespec="seconds") if hasattr(good_till, "isoformat")
                       else str(good_till or "")),
        )  # fmt: skip
        if type == "trailing_stop":
            o.high_water = ref
        self.acct.orders[o.id] = o
        self._event("placed", o, at)
        return o

    def modify(self, order_id: str, at: datetime, **changes) -> Order:
        o = self.acct.orders.get(order_id)
        if o is None or not o.working:
            raise OrderRejected(f"{order_id} is not a working order")
        for k in ("limit", "stop", "trail_pct"):
            if changes.get(k) is not None:
                v = float(changes[k])
                if k != "trail_pct":
                    v = round_to_tick(v, up=(k == "stop") == o.buy)
                setattr(o, k, v)
        if changes.get("qty") is not None:
            q = int(changes["qty"])
            if q < o.filled or q <= 0:
                raise OrderRejected(f"quantity {q} is below what has filled ({o.filled})")
            o.qty = q
        # A modified order loses its place: it is worked again only from bars after `at`,
        # and a limit that was resting must again trade through.
        o.submitted_at = at.isoformat(timespec="seconds")
        self._event("modified", o, at)
        if o.remaining == 0:
            self._close(o, "filled", at)
        return o

    def cancel(self, order_id: str, at: datetime, why: str = "cancelled") -> Order:
        o = self.acct.orders.get(order_id)
        if o is None or not o.working:
            raise OrderRejected(f"{order_id} is not a working order")
        # A cancel reaches the market with the trader's latency too: bars that started before
        # `at` have already been worked (the engine works bars before applying actions).
        self._close(o, "cancelled", at, why)
        return o

    def _close(self, o: Order, status: str, at, why: str = "") -> None:
        if o.filled > 0 and status in ("cancelled", "expired"):
            status = status + "_partial"
        o.status = status
        o.closed_at = at.isoformat(timespec="seconds") if hasattr(at, "isoformat") else str(at)
        o.note = why or o.note
        self._event(status, o, at)
        if status.startswith("cancel") or status.startswith("expire"):
            for cid in o.children:
                c = self.acct.orders.get(cid)
                if c and c.working and c.filled == 0 and self._held(c.code) == 0:
                    self._close(c, "cancelled", at, "its entry did not fill")

    def _event(self, kind: str, o: Order, at) -> None:
        self.events.append({
            "kind": kind, "order": o.id, "code": o.code, "side": o.side, "type": o.type,
            "qty": o.qty, "filled": o.filled, "avg_price": o.avg_price,
            "at": at.isoformat(timespec="seconds") if hasattr(at, "isoformat") else str(at),
            "note": o.note,
        })  # fmt: skip

    def _held(self, code: str) -> int:
        p = self.acct.positions.get(code)
        return p.qty if p else 0

    # ------------------------------------------------------------------ the bars
    def work_slot(self, slot: int) -> list[Fill]:
        """Work every working order against the bar in `slot` (called once per slot, in
        order, after the bar is complete)."""
        m = self.market
        fills: list[Fill] = []
        start = slot_time(m.day, slot)
        by_code: dict[str, list[Order]] = {}
        for o in self.acct.working():
            if o.good_till and datetime.fromisoformat(o.good_till) <= start:
                self._close(o, "expired", start, "good-till time reached")
                continue
            if datetime.fromisoformat(o.submitted_at) < start:
                by_code.setdefault(o.code, []).append(o)
        for code, orders in by_code.items():
            b = m.bars.get(code)
            if b is None or b.v[slot] <= 0:
                continue
            fills += self._work_bar(code, orders, slot, b)
        if slot == CLOSE_AUCTION_SLOT:
            self._expire_day_orders(slot_time(m.day, SLOTS))
        return fills

    def _work_bar(self, code, orders, slot, b) -> list[Fill]:
        o_, h, l, c, v = b.o[slot], b.h[slot], b.l[slot], b.c[slot], b.v[slot]  # noqa: E741
        cap = math.floor(self.max_volume_share * v)
        used = 0
        fills = []
        auction_open = slot == b.open_slot
        auction_close = slot == CLOSE_AUCTION_SLOT
        start = slot_time(self.market.day, slot)
        turnover = self.market.turnover(code)
        # exits first (stops, then brackets' targets are limits like any other), then entries
        orders = sorted(orders, key=lambda o: (0 if o.type in ("stop", "trailing_stop") else
                                               1 if o.reduce_only else 2, o.id))  # fmt: skip
        for o in orders:
            if not o.working or used >= cap:
                continue
            if o.reduce_only:
                held = self._held(code)
                room = held if not o.buy else -held
                if room <= 0:
                    self._close(o, "cancelled", start, "no position left to close")
                    continue
                o.qty = min(o.qty, o.filled + room)
            price, basis = self._price_for(o, slot, o_, h, l, c, v, turnover, auction_open,
                                           auction_close)  # fmt: skip
            if o.type == "trailing_stop" and o.high_water is not None:
                # updated only after this bar's trigger check (conservative)
                o.high_water = max(o.high_water, h) if not o.buy else min(o.high_water, l)
            if o.type in ("limit", "stop_limit") and o.limit and (
                    o.type == "limit" or o.triggered):  # fmt: skip
                # after this bar: is it still marketable at the bar's close? If not, it rests
                # in the book and from now on fills only when the price trades through it.
                half = self.costs.half_spread(c, turnover)
                o.resting = not ((o.buy and c + half <= o.limit) or
                                 (not o.buy and c - half >= o.limit))  # fmt: skip
            else:
                o.resting = True
            if price is None:
                continue
            qty = min(o.remaining, cap - used)
            if qty <= 0:
                continue
            aggressive = basis in ("market", "stop", "marketable")
            is_auction = auction_open or auction_close
            if aggressive:
                price = self.costs.aggressive_price(price, o.buy, qty, v, turnover, is_auction)
                if o.type in ("limit", "stop_limit", "loc") and o.limit:
                    if (o.buy and price > o.limit) or (not o.buy and price < o.limit):
                        price = o.limit
            elif is_auction:
                price = self.costs.aggressive_price(price, o.buy, qty, v, turnover, True)
                if o.limit and ((o.buy and price > o.limit) or (not o.buy and price < o.limit)):
                    price = o.limit
            used += qty
            fills.append(self._fill(o, qty, round(price, 6), start, basis + (
                " (opening auction)" if auction_open else " (closing auction)"
                if auction_close else "")))  # fmt: skip
        return fills

    def _price_for(self, o, slot, op, h, l, c, v, turnover, auction_open, auction_close):  # noqa: E741
        """The reference price this bar gives the order, and why; (None, "") if no fill."""
        t = o.type
        continuous = slot < CONTINUOUS_END_SLOT
        if auction_close:
            ap = c
            if t in ("moc", "market"):
                return ap, "market"
            if t in ("loc", "limit") or (t == "stop_limit" and o.triggered):
                if (o.buy and ap <= o.limit) or (not o.buy and ap >= o.limit):
                    return ap, "auction-limit"
                return None, ""
            if t in ("stop", "trailing_stop", "stop_limit"):
                lvl = self._stop_level(o)
                if lvl is not None and ((o.buy and ap >= lvl) or (not o.buy and ap <= lvl)):
                    o.triggered = True
                    if t == "stop_limit":
                        ok = (o.buy and ap <= o.limit) or (not o.buy and ap >= o.limit)
                        return (ap, "auction-limit") if ok else (None, "")
                    return ap, "stop"
            return None, ""
        if not continuous:
            return None, ""  # the pre-close: nothing trades until the closing auction
        if t in ("moc", "loc"):
            return None, ""
        if auction_open and not o.resting:
            ap = op  # the auction price
            if t == "market":
                return ap, "market"
            if t == "limit":
                if (o.buy and ap <= o.limit) or (not o.buy and ap >= o.limit):
                    return ap, "auction-limit"
            # stops that rested overnight and the auction went through
        if t == "market":
            return op, "market"
        if t in ("stop", "trailing_stop") and o.triggered:
            return op, "stop"  # a triggered stop is a market order until it is done
        if t in ("stop", "trailing_stop") or (t == "stop_limit" and not o.triggered):
            lvl = self._stop_level(o)
            if lvl is None:
                return None, ""
            if o.buy:
                hit = op >= lvl or h >= lvl
                ref = max(op, lvl)
            else:
                hit = op <= lvl or l <= lvl
                ref = min(op, lvl)
            if not hit:
                return None, ""
            o.triggered = True
            if t != "stop_limit":
                return ref, "stop"
            # a stop-limit becomes a limit at the trigger: marketable if the trigger price is
            # within its limit, else it rests from the next bar
            if (o.buy and ref <= o.limit) or (not o.buy and ref >= o.limit):
                return ref, "marketable"
            return None, ""
        if t in ("limit", "stop_limit"):
            lim = o.limit
            if not o.resting:
                # marketable when it arrives (or still marketable after a partial fill): it
                # takes the market, never better than the bar's open plus half the spread
                half = self.costs.half_spread(op, turnover)
                if (o.buy and op + half <= lim) or (not o.buy and op - half >= lim):
                    return op, "marketable"
            # resting in the book: only a trade THROUGH the limit fills it, at the limit
            if (o.buy and l < lim) or (not o.buy and h > lim):
                return lim, "limit"
            return None, ""
        return None, ""

    def _stop_level(self, o: Order) -> float | None:
        if o.type == "trailing_stop":
            if o.high_water is None or not o.trail_pct:
                return None
            f = o.trail_pct / 100.0
            return o.high_water * (1 + f) if o.buy else o.high_water * (1 - f)
        return o.stop

    # ------------------------------------------------------------------ fills
    def _fill(self, o: Order, qty: int, price: float, at: datetime, basis: str) -> Fill:
        a = self.acct
        code = o.code
        signed = qty if o.buy else -qty
        value = qty * price
        o.avg_price = (((o.avg_price or 0) * o.filled) + value) / (o.filled + qty)
        o.filled += qty
        o.value += value
        fee_total = self.costs.broker.commission(o.value)
        fee = round(fee_total - o.commission, 4)
        o.commission = fee_total
        a.cash -= signed * price + fee
        a.fees += fee
        o.fills.append({"at": at.isoformat(timespec="seconds"), "qty": qty,
                        "price": round(price, 6), "basis": basis})  # fmt: skip
        pos = a.positions.get(code)
        trade = a.open_trades.get(code)
        if pos is None or pos.qty == 0:
            trade = Trade(id=f"T{len(a.trades) + len(a.open_trades) + 1:05d}", code=code,
                          direction="long" if signed > 0 else "short",
                          opened_at=at.isoformat(timespec="seconds"), by=o.by)  # fmt: skip
            a.open_trades[code] = trade
            pos = Position(code, 0, 0.0, trade.opened_at, trade.id)
            a.positions[code] = pos
        trade.orders[o.id] = trade.orders.get(o.id, 0.0) + value
        trade.fees += fee
        if pos.qty == 0 or (pos.qty > 0) == (signed > 0):
            new = pos.qty + signed
            pos.avg = (pos.avg * abs(pos.qty) + price * qty) / abs(new)
            pos.qty = new
            trade.entry_value += value
        else:
            closing = min(qty, abs(pos.qty))
            direction = 1 if pos.qty > 0 else -1
            trade.gross += (price - pos.avg) * closing * direction
            pos.qty += signed
            rest = qty - closing
            if pos.qty == 0 or rest > 0:
                trade.closed_at = at.isoformat(timespec="seconds")
                a.trades.append(a.open_trades.pop(code))
                del a.positions[code]
                if rest > 0:  # flipped through zero: the rest opens a new trade
                    t2 = Trade(id=f"T{len(a.trades) + len(a.open_trades) + 1:05d}", code=code,
                               direction="long" if signed > 0 else "short",
                               opened_at=trade.closed_at, by=o.by)  # fmt: skip
                    a.open_trades[code] = t2
                    a.positions[code] = Position(code, signed // qty * rest, price,
                                                 t2.opened_at, t2.id)  # fmt: skip
                    t2.entry_value += rest * price
        held = a.positions[code].qty if code in a.positions else 0
        if a.positions.get(code) is not None:
            tr = a.open_trades[code]
            tr.max_qty = max(tr.max_qty, abs(held))
        fill = Fill(o.id, code, o.side, qty, round(price, 6), at.isoformat(timespec="seconds"),
                    basis, held)  # fmt: skip
        self.events.append({"kind": "fill", **asdict(fill)})
        if o.remaining == 0:
            self._close(o, "filled", at)
        self._after_fill(o, qty, at)
        return fill

    def _after_fill(self, o: Order, qty: int, at: datetime) -> None:
        a = self.acct
        # one-cancels-other: the siblings shrink by what filled
        if o.oca:
            for s in a.working(o.code):
                if s.oca == o.oca and s.id != o.id:
                    s.qty = max(s.filled, s.qty - qty)
                    if s.remaining == 0:
                        self._close(s, "cancelled", at, f"one-cancels-other: {o.id} filled")
        # a bracket's exits follow the entry as it fills
        if o.attach and not o.reduce_only:
            exit_side = "sell" if o.buy else "buy"
            oca = f"B{o.id}"
            want = {k: o.attach.get(k) for k in ("stop", "target", "trail_pct")}
            have = {a.orders[c].type: a.orders[c] for c in o.children if c in a.orders}
            specs = []
            if want["stop"]:
                specs.append(("stop", {"stop": float(want["stop"])}))
            if want["trail_pct"]:
                specs.append(("trailing_stop", {"trail_pct": float(want["trail_pct"])}))
            if want["target"]:
                specs.append(("limit", {"limit": float(want["target"])}))
            for typ, kw in specs:
                child = have.get(typ)
                if child is not None and child.working:
                    child.qty += qty
                    continue
                c = self.place(o.code, exit_side, qty, typ, at=at, reduce_only=True, oca=oca,
                               parent=o.id, tif=str(o.attach.get("tif", "gtc")), by=o.by,
                               why=f"bracket of {o.id}", **kw)  # fmt: skip
                o.children.append(c.id)

    def _expire_day_orders(self, at: datetime) -> None:
        for o in self.acct.working():
            if o.tif == "day":
                self._close(o, "expired", at, "day order: the session ended")

    # ------------------------------------------------------------------ the night
    def overnight(self, nights: int, close_of) -> float:
        """Charge short borrow for `nights` calendar nights on each short's closing value."""
        total = 0.0
        for code, p in self.acct.positions.items():
            if p.qty < 0:
                px = close_of(code) or p.avg
                fee = abs(p.qty) * px * self.costs.borrow_pct_annual / 100.0 * nights / 365.0
                total += fee
                t = self.acct.open_trades.get(code)
                if t is not None:
                    t.borrow += fee
        self.acct.cash -= total
        self.acct.borrow += total
        return round(total, 4)


def shares_for_value(value: float, price: float) -> int:
    """Whole shares for a dollar amount at a price (rounded down)."""
    if not price or price <= 0:
        return 0
    return int(value // price)


__all__ = [
    "Account",
    "Fill",
    "Order",
    "OrderRejected",
    "Position",
    "SimBroker",
    "Trade",
    "shares_for_value",
    "tick_size",
]
