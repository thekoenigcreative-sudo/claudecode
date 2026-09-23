"""Arena account books. One JSON file per account, atomic writes, no database.

Each playbook has two accounts - `<playbook>__agent` (the AI agent's) and `<playbook>__bot`
(its rule-based yardstick) - so the difference between them is exactly what the agent's
judgment added or lost.

Position quantities are signed: positive is long, negative is short. Equity is marked to
market every day (ARENA.md: "every account is marked to market every day"), and the daily
mark is what the daily loss limit and the scoreboard are both measured against.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot.io import write_text_atomic
from asxbot.log import EventLog, get_logger

log = get_logger("asxbot.arena.accounts")
SYD = ZoneInfo("Australia/Sydney")


@dataclass
class Position:
    ticker: str
    qty: int  # signed: + long, - short
    avg_cost: float
    opened_at: str
    stop: float | None = None
    target: float | None = None  # a take-profit, honoured by the broker for agent accounts
    thesis: str = ""
    opened_by: str = ""  # agent | bot
    model: str = ""  # the model that made the call, for the audit trail
    borrow_accrued: float = 0.0  # short borrow charged so far
    last_borrow_day: str = ""
    hold: str = "intraday"  # intraday | overnight
    hold_reason: str = ""  # required, in writing, to keep a Level 1 position overnight
    hold_asked_on: str = ""  # the day the pre-close sweep last asked about this position
    # The minute the take-profit target is honoured from (checked from the minute after it).
    # A position opened before targets were honoured (2026-09-24) gets this the first time
    # the broker sees it, and its target rests from then like any other. Whether the price
    # was already past the target at that moment is recorded, for the audit trail only.
    target_from: str = ""
    target_past_when_armed: bool = False
    # The last minute bar the stop and target have been checked against. Bars are worked
    # once each, in time order (broker.work); empty means from the entry bar, as before.
    worked_through: str = ""

    @property
    def is_short(self) -> bool:
        return self.qty < 0

    def to_dict(self) -> dict:
        d = asdict(self)
        # Written only when set, so a book saved by this version still loads in a process
        # started before these fields existed (as ArenaOrder does with stop_pct).
        if not d["target_from"]:
            del d["target_from"]
        if not d["target_past_when_armed"]:
            del d["target_past_when_armed"]
        if not d["worked_through"]:
            del d["worked_through"]
        return d



def _position(raw: dict) -> Position:
    """A position from a saved book. 42364dc called target_past_when_armed
    target_exit_at_open (and sold at the first open whatever the price); a book written by
    it still loads, and the flag now means only what its new name says."""
    raw = dict(raw)
    if "target_exit_at_open" in raw:
        raw.setdefault("target_past_when_armed", raw.pop("target_exit_at_open"))
    return Position(**raw)

@dataclass
class ArenaOrder:
    """An order as the arena broker sees it. `pending_fill` until a traded minute exists."""

    order_id: str
    account: str
    ticker: str
    side: str  # buy | sell | short | cover
    qty: int
    limit: float
    # The wall clock when the order was recorded - never the time of the data it was decided
    # on. It fills at the first traded bar that starts strictly after this. Until 2026-09-23
    # (then called decision_at) it was the start of the watcher's cycle, which could be
    # minutes earlier than the decision: ARN-000002 was decided at 10:37:41 after eight
    # minutes of model calls, stamped 10:29:46, and filled at the 10:29 bar.
    decided_at: str
    # When the market data the decision was made on was read. The gap between the two is
    # how stale the decision's picture was; the free feed is itself ~20 minutes behind this.
    data_as_of: str = ""
    # A resting exit (a stop or a take-profit target) fills in the bar that reached its level,
    # which can be before the code notices and records it. What must be strictly before that
    # bar is the moment the level began resting, recorded here. Empty for every other order.
    rests_from: str = ""
    # pending_fill: working, and possibly part-filled already (filled_qty > 0).
    # filled: every share filled. partial: some filled, the rest expired at the end of its
    # session or was cancelled. expired: none filled by the end of its session. cancelled:
    # none filled, cancelled (a stop took over, or the position it was closing is gone).
    status: str = "pending_fill"
    # limit: an order someone placed; a DAY order, working only in its own session.
    # stop / target: an exit the broker raised when a position's stop or take-profit was
    # reached; it keeps working across sessions until the position is out (broker.work).
    order_type: str = "limit"
    filled_qty: int = 0
    avg_price: float | None = None
    commission: float = 0.0
    fill_minute: str | None = None
    fill_basis: str | None = None
    stop: float | None = None
    # For a rule that defines its stop as a distance from the entry ("8% below"): the stop is
    # set at this distance from the price actually filled, replacing `stop`, which is then
    # only the worst case the risk limit was checked against.
    stop_pct: float | None = None
    target: float | None = None
    reason: str = ""
    model: str = ""
    placed_by: str = ""  # agent | bot
    message: str = ""
    realised: float = 0.0  # set on a closing fill, so win rate is computable per trade
    hold: str = "intraday"  # the holding period the decision intended
    # Each bar's share of the fill: {"minute", "qty", "price", "bar_price", "bar_volume"}.
    # No bar fills more than arena.fill.max_volume_share of its traded volume (#25), so a
    # large order in a thin stock fills across several bars. fill_minute is the first.
    fills: list = field(default_factory=list)
    # The last minute bar this order has been worked against; each bar is used once.
    worked_through: str = ""
    # A stop or target exit only: the price the bar that reached the level traded at (the
    # level, or the bar's open if it gapped through). Its slippage is sized from this.
    trigger_price: float | None = None

    @property
    def remaining(self) -> int:
        return max(0, abs(int(self.qty)) - int(self.filled_qty))

    @property
    def working(self) -> bool:
        return self.status == "pending_fill"

    def to_dict(self) -> dict:
        d = asdict(self)
        # Written only when set, so a book saved by this version still loads in a process
        # started before stop_pct existed (the running watcher, on the day it was added).
        if d["stop_pct"] is None:
            del d["stop_pct"]
        if not d["rests_from"]:
            del d["rests_from"]
        for k, blank in (
            ("order_type", "limit"), ("fills", []), ("worked_through", ""), ("trigger_price", None)
        ):  # fmt: skip
            if d[k] == blank:
                del d[k]
        return d


def _order(raw: dict) -> ArenaOrder:
    """An order from a saved book. A book written before 2026-09-24 calls decided_at
    decision_at and has no data_as_of; it still loads, with the old time kept as it was."""
    raw = dict(raw)
    if "decision_at" in raw:
        raw.setdefault("decided_at", raw.pop("decision_at"))
    return ArenaOrder(**raw)


@dataclass
class Mark:
    """One day's mark-to-market snapshot."""

    day: str
    equity: float
    cash: float
    gross_exposure: float
    positions: int
    realised_day: float
    fees_day: float
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Account:
    name: str
    playbook: str
    kind: str  # agent | bot
    level: int
    starting_cash: float
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    orders: dict[str, ArenaOrder] = field(default_factory=dict)
    next_id: int = 1
    realised_pnl: float = 0.0
    fees_paid: float = 0.0
    borrow_paid: float = 0.0
    created: str = ""

    # -- valuation ----------------------------------------------------------
    def market_value(self, prices: dict[str, float]) -> float:
        """Signed value of open positions. Shorts are negative (a liability)."""
        total = 0.0
        for t, p in self.positions.items():
            px = prices.get(t)
            total += p.qty * (px if px is not None else p.avg_cost)
        return total

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.market_value(prices)

    def gross_exposure(self, prices: dict[str, float]) -> float:
        """Sum of |position value|. This is what the leverage cap is measured against."""
        total = 0.0
        for t, p in self.positions.items():
            px = prices.get(t)
            total += abs(p.qty) * (px if px is not None else p.avg_cost)
        return total

    def tickers(self) -> list[str]:
        return sorted(self.positions)

    def closing_qty_working(self, ticker: str) -> int:
        """Shares of `ticker` already working to be sold or covered (unfilled remainders of
        live closing orders, stop and target exits included)."""
        return sum(
            o.remaining for o in self.orders.values()
            if o.working and o.ticker == ticker and o.side in ("sell", "cover")
        )  # fmt: skip


class AccountStore:
    """Loads and saves account books under data/arena/accounts/."""

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "arena"
        self.dir = self.root / "accounts"
        self.marks_dir = self.root / "marks"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.marks_dir.mkdir(parents=True, exist_ok=True)
        self.events = EventLog(data_dir)

    def path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def exists(self, name: str) -> bool:
        return self.path(name).exists()

    def open(
        self, name: str, playbook: str, kind: str, level: int, starting_cash: float
    ) -> Account:
        """Load the account, creating it on first use."""
        p = self.path(name)
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            acct = Account(
                name=raw["name"],
                playbook=raw["playbook"],
                kind=raw["kind"],
                level=int(raw.get("level", level)),
                starting_cash=float(raw["starting_cash"]),
                cash=float(raw["cash"]),
                positions={k: _position(v) for k, v in raw.get("positions", {}).items()},
                orders={k: _order(v) for k, v in raw.get("orders", {}).items()},
                next_id=int(raw.get("next_id", 1)),
                realised_pnl=float(raw.get("realised_pnl", 0.0)),
                fees_paid=float(raw.get("fees_paid", 0.0)),
                borrow_paid=float(raw.get("borrow_paid", 0.0)),
                created=raw.get("created", ""),
            )
            if acct.level != level:
                log.info("account %s level %s -> %s (config)", name, acct.level, level)
                acct.level = level
            return acct
        acct = Account(
            name=name,
            playbook=playbook,
            kind=kind,
            level=level,
            starting_cash=float(starting_cash),
            cash=float(starting_cash),
            created=datetime.now(SYD).isoformat(timespec="seconds"),
        )
        self.save(acct)
        log.info("opened arena account %s with %.2f fake dollars", name, starting_cash)
        self.events.append(
            "arena_accounts",
            {"account": name, "event": "opened", "starting_cash": starting_cash},
        )
        return acct

    def save(self, acct: Account) -> Path:
        raw = {
            "name": acct.name,
            "playbook": acct.playbook,
            "kind": acct.kind,
            "level": acct.level,
            "starting_cash": acct.starting_cash,
            "cash": acct.cash,
            "positions": {k: v.to_dict() for k, v in acct.positions.items()},
            "orders": {k: v.to_dict() for k, v in acct.orders.items()},
            "next_id": acct.next_id,
            "realised_pnl": acct.realised_pnl,
            "fees_paid": acct.fees_paid,
            "borrow_paid": acct.borrow_paid,
            "created": acct.created,
        }
        return write_text_atomic(json.dumps(raw, indent=2), self.path(acct.name))

    def names(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.json"))

    # -- order ids ----------------------------------------------------------
    def next_order_id(self) -> str:
        """The next order id, unique across EVERY account.

        The id is the only proof an order exists, so it cannot be ambiguous. A per-account
        counter gave the agent and its bot both an ARN-000001, which made the event log
        impossible to read back with certainty.
        """
        p = self.root / "next_order_id.json"
        n = 1
        if p.exists():
            try:
                n = int(json.loads(p.read_text(encoding="utf-8"))["next"])
            except (ValueError, KeyError, TypeError):
                n = 1
        # Never reuse an id, even if the counter file was lost with accounts still on disk.
        highest = 0
        for f in self.dir.glob("*.json"):
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            for oid in raw.get("orders", {}):
                digits = oid.rsplit("-", 1)[-1]
                if digits.isdigit():
                    highest = max(highest, int(digits))
        n = max(n, highest + 1)
        write_text_atomic(json.dumps({"next": n + 1}, indent=2), p)
        return f"ARN-{n:06d}"

    # -- daily marks --------------------------------------------------------
    def marks_path(self, name: str) -> Path:
        return self.marks_dir / f"{name}.jsonl"

    def marks(self, name: str) -> list[Mark]:
        p = self.marks_path(name)
        if not p.exists():
            return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(Mark(**json.loads(line)))
        return out

    def write_mark(self, name: str, mark: Mark) -> Mark:
        """Append today's mark, replacing any earlier mark for the same day."""
        existing = [m for m in self.marks(name) if m.day != mark.day]
        existing.append(mark)
        existing.sort(key=lambda m: m.day)
        body = "\n".join(json.dumps(m.to_dict()) for m in existing) + "\n"
        write_text_atomic(body, self.marks_path(name))
        return mark

    def day_start_equity(self, acct: Account, today: date | None = None) -> float:
        """Equity at the start of today: yesterday's closing mark, or the starting cash.

        The daily loss limit is measured against this, so a day that starts after a big
        loss cannot quietly reset the limit to a smaller number mid-session.
        """
        today = today or datetime.now(SYD).date()
        prior = [m for m in self.marks(acct.name) if m.day < today.isoformat()]
        if not prior:
            return acct.starting_cash
        return prior[-1].equity
