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
    target: float | None = None
    thesis: str = ""
    opened_by: str = ""  # agent | bot
    model: str = ""  # the model that made the call, for the audit trail
    borrow_accrued: float = 0.0  # short borrow charged so far
    last_borrow_day: str = ""

    @property
    def is_short(self) -> bool:
        return self.qty < 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ArenaOrder:
    """An order as the arena broker sees it. `pending_fill` until a traded minute exists."""

    order_id: str
    account: str
    ticker: str
    side: str  # buy | sell | short | cover
    qty: int
    limit: float
    decision_at: str
    status: str = "pending_fill"  # pending_fill | filled | rejected | expired
    filled_qty: int = 0
    avg_price: float | None = None
    commission: float = 0.0
    fill_minute: str | None = None
    fill_basis: str | None = None
    stop: float | None = None
    target: float | None = None
    reason: str = ""
    model: str = ""
    placed_by: str = ""  # agent | bot
    message: str = ""
    realised: float = 0.0  # set on a closing fill, so win rate is computable per trade

    def to_dict(self) -> dict:
        return asdict(self)


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
                positions={k: Position(**v) for k, v in raw.get("positions", {}).items()},
                orders={k: ArenaOrder(**v) for k, v in raw.get("orders", {}).items()},
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
