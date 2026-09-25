"""The scoreboard. Code calculates it; the agent only writes it into the evening report.

Per account (ARENA.md): P&L, level, green days vs red days, average day, worst day, trades,
win rate, worst peak-to-trough drop, fees paid. Plus the checkpoint test for the ladder:
a playbook stays at Level 1 only if it is net profitable after all costs, has more green
days than red, and its worst peak-to-trough drop is under 25%.

Everything here is derived from the daily marks and the filled orders, so the agent cannot
flatter its own record: it does not write these numbers.

Trades are ROUND TRIPS since 26 Sep 2026 (D4, LEARNINGS #7): one position's life, from the
first share bought (or shorted) to the last sold (or covered), however many orders and bars
it took. A trade is won only if it made money after every cost: net = realised - the entry
orders' brokerage - the exit orders' brokerage - short borrow. Until then each closing order
was a trade and only its own brokerage was taken off: on 25 Sep the day trader's rule bot
showed 4 trades, all won; it had 3 round trips and 1 winner after costs (NWL +131.40, CWY
-6.33, DOW -7.42).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from asxbot.arena.accounts import Account, AccountStore, ArenaOrder

MAX_DRAWDOWN_TO_STAY = 25.0  # per cent, ARENA.md review rule
_OPENING = ("buy", "short")


@dataclass
class RoundTrip:
    """One position's life in one ticker, from flat to flat."""

    ticker: str
    side: str  # long | short
    opened: str  # the first fill's minute
    closed: str = ""  # the last exit fill's minute; "" while still open
    qty: int = 0  # the most held at once
    entry_value: float = 0.0
    gross: float = 0.0  # realised, before any cost
    entry_fees: float = 0.0
    exit_fees: float = 0.0
    borrow: float = 0.0
    orders: list = field(default_factory=list)
    held: int = 0  # signed shares still held (0 once closed)
    _avg: float = 0.0

    @property
    def is_closed(self) -> bool:
        return bool(self.closed) and self.held == 0

    @property
    def net(self) -> float:
        return self.gross - self.entry_fees - self.exit_fees - self.borrow

    def to_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        d["net"] = round(self.net, 2)
        for k in ("entry_value", "gross", "entry_fees", "exit_fees", "borrow"):
            d[k] = round(d[k], 2)
        return d


def _slices(o: ArenaOrder) -> list[dict]:
    """The order's fill slices, each with its share of the order's brokerage. An order
    filled before slices were recorded (2026-09-24) is one slice at its average price."""
    fills = list(o.fills or [])
    if not fills and o.filled_qty and o.avg_price is not None:
        fills = [{"minute": o.fill_minute or o.decided_at, "qty": int(o.filled_qty),
                  "price": float(o.avg_price)}]  # fmt: skip
    value = sum(abs(int(f["qty"])) * float(f["price"]) for f in fills) or 1.0
    return [
        {**f, "fee": float(o.commission) * abs(int(f["qty"])) * float(f["price"]) / value}
        for f in fills
    ]


def round_trips(acct: Account) -> list[RoundTrip]:
    """Every position life in the account, oldest first, rebuilt from the fill slices in the
    order the broker booked them (by bar, then stop exit, target exit, the rest by age).
    The realised figure is the broker's own arithmetic: each exit slice against the average
    cost of the entry slices before it."""
    rank = {"stop": 0, "target": 1}
    rows = []
    for o in acct.orders.values():
        if not o.filled_qty:
            continue
        for f in _slices(o):
            rows.append((str(f["minute"]), rank.get(o.order_type, 2), o.decided_at, o.order_id,
                         o, f))  # fmt: skip
    rows.sort(key=lambda r: r[:4])
    live: dict[str, RoundTrip] = {}
    done: list[RoundTrip] = []
    for minute, _, _, _, o, f in rows:
        q, px, fee = abs(int(f["qty"])), float(f["price"]), float(f["fee"])
        t = live.get(o.ticker)
        if o.side in _OPENING:
            if t is None:
                t = live[o.ticker] = RoundTrip(
                    o.ticker, "long" if o.side == "buy" else "short", minute
                )
            total = abs(t.held) + q
            t._avg = (t._avg * abs(t.held) + px * q) / total
            t.held = total if o.side == "buy" else -total
            t.qty = max(t.qty, total)
            t.entry_value += q * px
            t.entry_fees += fee
        else:
            if t is None:
                continue  # an exit with no entry on the books (a reset account)
            q = min(q, abs(t.held))
            t.gross += (px - t._avg) * q if o.side == "sell" else (t._avg - px) * q
            t.exit_fees += fee
            t.held += -q if o.side == "sell" else q
            t.closed = minute
            if t.held == 0:
                t.borrow += float(getattr(o, "borrow", 0.0) or 0.0)
                done.append(live.pop(o.ticker))
        if o.order_id not in t.orders:
            t.orders.append(o.order_id)
    for tk, t in live.items():
        t.closed = ""
        pos = acct.positions.get(tk)
        if pos is not None:
            t.borrow += float(pos.borrow_accrued)
        done.append(t)
    return sorted(done, key=lambda t: t.opened)


def closed_trips(acct: Account) -> list[RoundTrip]:
    return [t for t in round_trips(acct) if t.is_closed]


@dataclass
class Score:
    account: str
    playbook: str
    kind: str  # agent | bot
    level: int
    starting: float
    equity: float
    pnl: float
    pnl_pct: float
    days: int
    green_days: int
    red_days: int
    flat_days: int
    avg_day_pct: float
    worst_day_pct: float
    best_day_pct: float
    max_drawdown_pct: float
    trades: int  # closed round trips
    wins: int
    win_rate_pct: float
    open_positions: int
    fees_paid: float
    borrow_paid: float
    orders_placed: int
    orders_filled: int
    orders_expired: int
    pending_fills: int
    # Part-filled and done: the rest expired with its session or was cancelled (#25).
    orders_partial: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    # -- the ladder's review test ------------------------------------------
    def passes_level_test(self) -> tuple[bool, list[str]]:
        """ARENA.md: net profitable after costs, more green days than red, drawdown < 25%."""
        reasons = []
        if self.pnl <= 0:
            reasons.append(f"not profitable after costs ({self.pnl:+,.2f})")
        if self.green_days <= self.red_days:
            reasons.append(f"green days {self.green_days} do not beat red days {self.red_days}")
        if self.max_drawdown_pct >= MAX_DRAWDOWN_TO_STAY:
            reasons.append(
                f"worst peak-to-trough drop {self.max_drawdown_pct:.1f}% is not under "
                f"{MAX_DRAWDOWN_TO_STAY}%"
            )
        return (not reasons), reasons


def score(store: AccountStore, acct: Account, prices: dict[str, float]) -> Score:
    marks = store.marks(acct.name)
    equity = acct.equity(prices)

    # Daily returns from the equity curve, starting at the opening balance.
    curve = [acct.starting_cash] + [m.equity for m in marks]
    rets = []
    for prev, cur in zip(curve[:-1], curve[1:], strict=True):
        rets.append((cur / prev - 1) * 100.0 if prev else 0.0)

    green = sum(1 for r in rets if r > 0)
    red = sum(1 for r in rets if r < 0)
    flat = sum(1 for r in rets if r == 0)

    peak = curve[0]
    max_dd = 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            max_dd = max(max_dd, (peak - v) / peak * 100.0)

    # Round trips, won after every cost (26 Sep 2026, D4); not closing orders before costs.
    closed = closed_trips(acct)
    wins = sum(1 for t in closed if t.net > 0)

    return Score(
        account=acct.name,
        playbook=acct.playbook,
        kind=acct.kind,
        level=acct.level,
        starting=round(acct.starting_cash, 2),
        equity=round(equity, 2),
        pnl=round(equity - acct.starting_cash, 2),
        pnl_pct=round((equity / acct.starting_cash - 1) * 100.0, 2) if acct.starting_cash else 0.0,
        days=len(rets),
        green_days=green,
        red_days=red,
        flat_days=flat,
        avg_day_pct=round(sum(rets) / len(rets), 3) if rets else 0.0,
        worst_day_pct=round(min(rets), 2) if rets else 0.0,
        best_day_pct=round(max(rets), 2) if rets else 0.0,
        max_drawdown_pct=round(max_dd, 2),
        trades=len(closed),
        wins=wins,
        win_rate_pct=round(wins / len(closed) * 100.0, 1) if closed else 0.0,
        open_positions=len(acct.positions),
        fees_paid=round(acct.fees_paid, 2),
        borrow_paid=round(acct.borrow_paid, 2),
        orders_placed=len(acct.orders),
        orders_filled=sum(1 for o in acct.orders.values() if o.status == "filled"),
        orders_partial=sum(1 for o in acct.orders.values() if o.status == "partial"),
        orders_expired=sum(1 for o in acct.orders.values() if o.status == "expired"),
        pending_fills=sum(1 for o in acct.orders.values() if o.status == "pending_fill"),
    )


def format_table(scores: list[Score]) -> str:
    """A compact scoreboard, agent account against its bot, for the evening report."""
    if not scores:
        return "no arena accounts yet"
    head = (
        f"{'account':28s} {'lvl':>3s} {'equity':>10s} {'P&L':>9s} {'%':>7s} "
        f"{'G/R':>7s} {'worst':>7s} {'ddown':>7s} {'trades':>6s} {'win%':>6s} {'fees':>8s}"
    )
    lines = [head, "-" * len(head)]
    for s in scores:
        lines.append(
            f"{s.account:28s} {s.level:>3d} {s.equity:>10,.2f} {s.pnl:>+9,.2f} "
            f"{s.pnl_pct:>+7.2f} {f'{s.green_days}/{s.red_days}':>7s} "
            f"{s.worst_day_pct:>+7.2f} {s.max_drawdown_pct:>7.2f} {s.trades:>6d} "
            f"{s.win_rate_pct:>6.1f} {s.fees_paid:>8,.2f}"
        )
    return "\n".join(lines)


def agent_vs_bot(scores: list[Score]) -> list[str]:
    """One line per playbook: what the agent's judgment added on top of the plain rule."""
    by_playbook: dict[str, dict[str, Score]] = {}
    for s in scores:
        by_playbook.setdefault(s.playbook, {})[s.kind] = s
    out = []
    for pb, pair in sorted(by_playbook.items()):
        a, b = pair.get("agent"), pair.get("bot")
        if a is None or b is None:
            continue
        diff = a.pnl - b.pnl
        verdict = "ahead of" if diff > 0 else "behind" if diff < 0 else "level with"
        out.append(
            f"{pb}: agent {a.pnl:+,.2f} vs yardstick bot {b.pnl:+,.2f} - "
            f"the agent is {verdict} the plain rule by {abs(diff):,.2f} "
            f"over {max(a.days, b.days)} day(s)"
        )
    return out
