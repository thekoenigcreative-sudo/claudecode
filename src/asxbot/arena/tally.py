"""What happened today, counted from the event log.

The event log is already the arena's record of everything - announcements alerted, screen
verdicts, reader and decider stages, orders and fills - so the counts are read back from
it rather than kept in a second place that could drift from it. Counting is cheap: these
are a few thousand short JSON lines a day.

Used by the hourly Telegram digest and the 16:10 end-of-session message (notify.py). The
evening report (report.py) is separate and unchanged.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot.log import get_logger

log = get_logger("asxbot.arena.tally")
SYD = ZoneInfo("Australia/Sydney")


def _read(data_dir: Path, kind: str, day: date) -> list[dict]:
    """Every record of one kind stamped with the given Sydney day."""
    p = Path(data_dir) / "events" / f"{kind}.jsonl"
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                when = datetime.fromisoformat(r["ts"]).astimezone(SYD)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if when.date() == day:
                r["syd"] = when
                out.append(r)
    return out


@dataclass
class Counts:
    seen: int = 0
    screened: int = 0
    by_test: dict = field(default_factory=dict)
    read: int = 0
    to_decider: int = 0
    passed: int = 0
    traded: int = 0
    orders_bot: int = 0
    gate_disagreements: int = 0

    def line(self) -> str:
        """The one-line summary that heads every digest."""
        split = ", ".join(f"{t} {n}" for t, n in sorted(self.by_test.items())) or "none"
        return (
            f"seen {self.seen} · screened {self.screened} ({split}) · "
            f"read {self.read} · passed {self.passed} · traded {self.traded}"
        )


def counts_for(data_dir: Path, day: date | None = None) -> Counts:
    day = day or datetime.now(SYD).date()
    alerts = _read(data_dir, "arena_alerts", day)
    screened = _read(data_dir, "arena_screened", day)
    decisions = _read(data_dir, "arena_decisions", day)
    orders = _read(data_dir, "arena_orders", day)

    rejected = {r["ids_id"]: r.get("test") or "?" for r in screened if not r.get("ok")}
    readers = {r["ids_id"] for r in decisions if r.get("stage") == "reader"}
    # Counted from the same deduplicated list the summary prints, so the header can never
    # say 31 above a list of 33.
    deciders = decider_items(data_dir, day)
    passed = [r for r in deciders if r["action"] != "trade"]
    placed = [o for o in orders if o.get("event") == "submitted"]
    return Counts(
        seen=len({r["ids_id"] for r in alerts if r.get("ids_id")}),
        screened=len(rejected),
        by_test=dict(Counter(rejected.values())),
        read=len(readers),
        to_decider=len(deciders),
        passed=len(passed),
        traded=len([o for o in placed if o.get("placed_by") == "agent"]),
        orders_bot=len([o for o in placed if o.get("placed_by") == "bot"]),
        gate_disagreements=len(
            [r for r in _read(data_dir, "arena_gate_compare", day) if not r.get("agrees")]
        ),
    )


def decider_items(data_dir: Path, day: date | None = None) -> list[dict]:
    """Everything that reached the decider today: what it decided, and why.

    One announcement can be recorded twice - a watcher restart re-working it, say - so the
    list is keyed by announcement and the later record wins.
    """
    day = day or datetime.now(SYD).date()
    seen: dict = {}
    for r in _read(data_dir, "arena_decisions", day):
        if r.get("stage") != "decider":
            continue
        d = r.get("decision") or {}
        action = str(d.get("action", "pass"))
        key = r.get("ids_id") or f"{r.get('ticker')}@{r['syd']:%H:%M}"
        seen[key] = (
            {
                "at": r["syd"].strftime("%H:%M"),
                "ticker": r.get("ticker", "?"),
                "action": action,
                "why": str(d.get("why", "")),
                "side": d.get("side"),
                "qty": d.get("qty"),
                "confidence_pct": d.get("confidence_pct"),
            }
        )
    return sorted(seen.values(), key=lambda i: i["at"])


def session_summary_text(arena, pb, now: datetime | None = None) -> str:
    """The 16:10 message: the day's totals, what reached the decider, both balances."""
    from html import escape

    now = (now or datetime.now(SYD)).astimezone(SYD)
    data_dir = arena.cfg.data_dir
    c = counts_for(data_dir, now.date())
    out = [
        f"🔔 <b>SESSION DONE {now:%a %d %b}</b> (ASX closed 16:10)",
        escape(c.line()),
        f"reached the decider: {c.to_decider} · bot orders: {c.orders_bot}",
    ]
    if c.gate_disagreements:
        out.append(
            f"reader's CAN_SIZE_AND_EXIT disagreed with the arithmetic {c.gate_disagreements}×"
        )

    items = decider_items(data_dir, now.date())
    out.append("")
    if items:
        out.append(f"<b>Reached the decider ({len(items)})</b>")
        for i in items:
            verdict = "TRADED" if i["action"] == "trade" else "passed"
            size = (
                f" {i['side']} {i['qty']:,}" if i["action"] == "trade" and i.get("qty") else ""
            )
            conf = f", {i['confidence_pct']}% confidence" if i.get("confidence_pct") else ""
            out.append(
                f"• {i['at']} <b>{escape(i['ticker'])}</b> {verdict}{size}{conf} — "
                f"<i>{escape(' '.join(str(i['why']).split()))}</i>"
            )
    else:
        out.append("<i>nothing reached the decider today</i>")

    out.append("")
    out.append("<b>Accounts</b>")
    for b in balances(arena, pb):
        held = ", ".join(b["holdings"]) or "no positions"
        pending = f", {b['pending']} pending fill" if b["pending"] else ""
        out.append(
            f"• {b['kind'].upper()} ${b['equity']:,.2f} ({b['pnl']:+,.2f}) — "
            f"{escape(held)}{pending}"
        )
    return "\n".join(out)


def balances(arena, pb) -> list[dict]:
    """Both accounts, marked at the prices the broker can see right now."""
    out = []
    for kind in ("agent", "bot"):
        acct = arena.account(pb, kind)
        prices = arena.broker.prices(acct)
        equity = acct.equity(prices)
        out.append(
            {
                "kind": kind,
                "name": acct.name,
                "equity": equity,
                "cash": acct.cash,
                "pnl": equity - acct.starting_cash,
                "positions": len(acct.positions),
                "holdings": [
                    f"{t} {p.qty:+,}@{p.avg_cost:.3f}" for t, p in acct.positions.items()
                ],
                "pending": len(
                    [o for o in acct.orders.values() if o.status == "pending_fill"]
                ),
            }
        )
    return out
