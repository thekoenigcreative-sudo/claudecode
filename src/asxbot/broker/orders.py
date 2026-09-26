"""place_order: the only path to a broker. Plain code, hard limits, no AI in the loop.

Every call must carry ticker, qty and limit explicitly (so the approval prompt shows exactly
what is being approved) and a proposal id for buys. The limits below are enforced HERE, in
code, and refuse anything outside them:
    max position size, max open positions, max new positions per day, long-only,
    limit orders only, allowed universe, allowed hours, minimum order value,
    and the order must match its proposal (ticker, qty, limit within one tick).
The function returns the broker's OrderResult. Whether an order was placed or filled is
decided only by that result; nothing upstream may assume it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot.broker.base import Broker, OrderResult
from asxbot.config import Config
from asxbot.io import write_text_atomic
from asxbot.log import EventLog, event_day, get_logger

log = get_logger("asxbot.broker.orders")
SYD = ZoneInfo("Australia/Sydney")


class OrderRefused(RuntimeError):
    """A limit check failed. Nothing was sent to the broker."""


@dataclass
class Limits:
    max_position_aud: float
    max_open_positions: int
    max_new_positions_per_day: int
    min_order_aud: float
    allowed_hours: tuple[str, str]
    long_only: bool
    limit_orders_only: bool
    allowed_universe: set[str]
    enforce_hours: bool

    @classmethod
    def from_config(cls, cfg: Config, universe_codes: set[str]) -> Limits:
        lim = cfg.get("limits")
        hrs = lim.get("allowed_hours", {})
        enforce = True
        if cfg.broker == "sim" and not bool(lim.get("enforce_hours_in_sim", False)):
            enforce = False
        return cls(
            max_position_aud=float(lim["max_position_aud"]),
            max_open_positions=int(lim["max_open_positions"]),
            max_new_positions_per_day=int(lim["max_new_positions_per_day"]),
            min_order_aud=float(lim.get("min_order_aud", 500)),
            allowed_hours=(hrs.get("start", "10:00"), hrs.get("end", "16:00")),
            long_only=bool(lim.get("long_only", True)),
            limit_orders_only=bool(lim.get("limit_orders_only", True)),
            allowed_universe=universe_codes,
            enforce_hours=enforce,
        )


def _in_hours(now: datetime, start: str, end: str) -> bool:
    t = now.astimezone(SYD).time()
    return time.fromisoformat(start) <= t <= time.fromisoformat(end)


def _tick(price: float) -> float:
    return 0.001 if price < 0.10 else 0.005 if price < 2.0 else 0.01


def place_order(
    cfg: Config,
    broker: Broker,
    limits: Limits,
    *,
    proposal_id: str | None,
    ticker: str,
    side: str,
    qty: int,
    limit: float,
    now: datetime | None = None,
    order_type: str = "limit",
) -> OrderResult:
    now = now or datetime.now(SYD)
    ticker = ticker.upper().strip()
    events = EventLog(cfg.data_dir)
    req = {
        "proposal_id": proposal_id, "ticker": ticker, "side": side, "qty": qty, "limit": limit,
        "order_type": order_type, "broker_mode": broker.mode,
    }  # fmt: skip

    def refuse(why: str) -> OrderRefused:
        events.append("orders", {**req, "outcome": "refused", "reason": why})
        log.warning("place_order REFUSED %s: %s", req, why)
        return OrderRefused(why)

    # --- hard limits, in code ---------------------------------------------
    if order_type != "limit" and limits.limit_orders_only:
        raise refuse("only limit orders are allowed")
    if side not in ("buy", "sell"):
        raise refuse(f"bad side {side!r}")
    if not isinstance(qty, int) or qty <= 0:
        raise refuse("qty must be a positive whole number")
    if not (limit > 0):
        raise refuse("limit must be positive")
    if limits.enforce_hours and not _in_hours(now, *limits.allowed_hours):
        raise refuse(
            f"outside allowed hours {limits.allowed_hours[0]}-{limits.allowed_hours[1]} Sydney"
        )
    positions = {p.ticker: p for p in broker.positions()}
    value = qty * limit
    if side == "sell":
        if limits.long_only and (ticker not in positions or positions[ticker].qty < qty):
            raise refuse("long-only: cannot sell more than the position held")
    else:
        if ticker not in limits.allowed_universe:
            raise refuse(f"{ticker} is not in the allowed universe")
        if value > limits.max_position_aud + 1e-6:
            raise refuse(
                f"order value {value:.2f} exceeds max position {limits.max_position_aud:.2f}"
            )
        if value < limits.min_order_aud:
            raise refuse(f"order value {value:.2f} below minimum order {limits.min_order_aud:.2f}")
        if ticker in positions:
            raise refuse(f"already holding {ticker}; one position per ticker")
        if len(positions) >= limits.max_open_positions:
            raise refuse(f"already at max open positions ({limits.max_open_positions})")
        today = now.astimezone(SYD).date()
        new_today = _new_positions_today(events, today)
        if new_today >= limits.max_new_positions_per_day:
            raise refuse(
                f"already opened {new_today} positions today "
                f"(max {limits.max_new_positions_per_day})"
            )
        if proposal_id is None:
            raise refuse("a buy needs a proposal id")
        prop = _load_proposal(cfg.data_dir, proposal_id)
        if prop is None:
            raise refuse(f"proposal {proposal_id} not found")
        if prop["status"] not in ("pending", "approved"):
            raise refuse(f"proposal {proposal_id} is {prop['status']}, not pending")
        if prop["ticker"] != ticker:
            raise refuse(f"proposal {proposal_id} is for {prop['ticker']}, not {ticker}")
        if qty > int(prop["qty"]):
            raise refuse(f"qty {qty} exceeds proposal qty {prop['qty']}")
        if abs(limit - float(prop["entry_limit"])) > _tick(limit) + 1e-9:
            raise refuse(
                f"limit {limit} differs from proposal limit {prop['entry_limit']} "
                "by more than a tick"
            )
        if date.fromisoformat(prop["created_at"][:10]) != today:
            raise refuse(
                f"proposal {proposal_id} is from {prop['created_at'][:10]}, not today; "
                "it has expired"
            )

    # --- send to the broker; its answer is the only truth -------------------
    tag = f"{proposal_id or 'manual'} {side} {ticker} {qty}@{limit}"
    res = broker.place_limit_order(ticker, side, qty, limit, tag)
    events.append("orders", {**req, "outcome": "sent", **res.to_dict()})
    if res.filled_qty:
        events.append(
            "fills",
            {"order_id": res.order_id, "ticker": ticker, "side": side, "qty": res.filled_qty,
             "price": res.avg_price, "commission": res.commission, "proposal_id": proposal_id,
             "day": now.astimezone(SYD).date().isoformat()},
        )  # fmt: skip
    if side == "buy" and prop is not None:
        prop["status"] = "placed" if res.status in ("filled", "partial", "open") else "rejected"
        prop["order_id"] = res.order_id
        prop["broker_status"] = res.status
        _save_proposal(cfg.data_dir, prop)
    log.info("place_order -> broker %s: %s %s", res.order_id, res.status, res.message)
    return res


def _new_positions_today(events: EventLog, today: date) -> int:
    n = 0
    for f in events.read("fills"):
        if f.get("side") == "buy" and event_day(f) == today.isoformat():
            n += 1
    return n


def _load_proposal(data_dir: Path, pid: str) -> dict | None:
    p = Path(data_dir) / "proposals" / f"{pid}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _save_proposal(data_dir: Path, prop: dict) -> None:
    p = Path(data_dir) / "proposals" / f"{prop['id']}.json"
    write_text_atomic(json.dumps(prop, indent=2), p)
