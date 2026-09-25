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
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot.log import get_logger

log = get_logger("asxbot.arena.tally")
SYD = ZoneInfo("Australia/Sydney")


def _is_test(r: dict) -> bool:
    """Rehearsals never count. A fake announcement is flagged when it is written, and its
    id starts with FAKE, which also covers records written before the flag existed."""
    return bool(r.get("is_test")) or str(r.get("ids_id", "")).upper().startswith("FAKE")


def _read(data_dir: Path, kind: str, day: date) -> list[dict]:
    """Every REAL record of one kind stamped with the given Sydney day."""
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
            if when.date() == day and not _is_test(r):
                r["syd"] = when
                out.append(r)
    return out


OPENING = ("buy", "short")
# A screen record that is not a verdict: v2 defers an announcement while the live feed is
# down and screens it again when the feed is back (watch._handle_v2, LEARNINGS #25).
NOT_A_VERDICT = ("deferred",)


@dataclass
class Counts:
    seen: int = 0
    screened: int = 0
    by_test: dict = field(default_factory=dict)
    read: int = 0
    to_decider: int = 0
    passed: int = 0
    traded: int = 0  # the agent's OPENING orders (26 Sep 2026, D10)
    orders_bot: int = 0  # the rule bot's opening orders
    gate_disagreements: int = 0
    exits: int = 0  # closing orders placed (the flat sweep, pre-close, horizon), both books
    agent_unavailable: int = 0  # agent calls that failed (G1)
    agent_unavailable_first: str = ""  # HH:MM of the first
    agent_unavailable_why: str = ""  # usage limit / no answer in time / error
    deferred: int = 0  # announcements waiting for the feed, not screened out

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

    # A deferral is not a rejection (26 Sep 2026, H13): an announcement deferred while the
    # feed was down and screened later counted as screened out under "deferred".
    rejected = {
        r["ids_id"]: r.get("test") or "?" for r in screened
        if not r.get("ok") and r.get("test") not in NOT_A_VERDICT
    }  # fmt: skip
    deferred = {r["ids_id"] for r in screened if r.get("test") in NOT_A_VERDICT}
    judged = {r["ids_id"] for r in screened if r.get("test") not in NOT_A_VERDICT}
    readers = {r["ids_id"] for r in decisions if r.get("stage") == "reader"}
    # Counted from the same deduplicated list the summary prints, so the header can never
    # say 31 above a list of 33.
    deciders = decider_items(data_dir, day)
    passed = [r for r in deciders if r["action"] != "trade"]
    placed = [o for o in orders if o.get("event") == "submitted"]
    # Openings only (26 Sep 2026, D10): the flat sweep's exits are placed in the agent's and
    # the bot's names and made "traded 4" of a day with 2 entries.
    opened = [o for o in placed if o.get("side") in OPENING]
    fails = [
        r for r in _read(data_dir, "arena_agent_calls", day)
        if r.get("ok") is False and not str(r.get("purpose", "")).startswith("chat")
    ]  # fmt: skip
    kinds = Counter(str(r.get("kind") or "error") for r in fails)
    from asxbot.arena.agents import FAILURE_WORDS

    return Counts(
        seen=len({r["ids_id"] for r in alerts if r.get("ids_id")}),
        screened=len(rejected),
        by_test=dict(Counter(rejected.values())),
        read=len(readers),
        to_decider=len(deciders),
        passed=len(passed),
        traded=len([o for o in opened if o.get("placed_by") == "agent"]),
        orders_bot=len([o for o in opened if o.get("placed_by") == "bot"]),
        gate_disagreements=len(
            [r for r in _read(data_dir, "arena_gate_compare", day) if not r.get("agrees")]
        ),
        exits=len([o for o in placed if o.get("side") not in OPENING]),
        agent_unavailable=len(fails),
        agent_unavailable_first=f"{min(r['syd'] for r in fails):%H:%M}" if fails else "",
        agent_unavailable_why=", ".join(FAILURE_WORDS.get(k, k) for k, _ in kinds.most_common()),
        deferred=len(deferred - judged),
    )


def orders_between(data_dir: Path, since: datetime, until: datetime) -> list[dict]:
    """Every real order placed after `since` and up to `until`, oldest first: agent, bot or
    code (a stop or a target). The hourly digest reads this to know whether its hour had
    a trade in it."""
    since, until = since.astimezone(SYD), until.astimezone(SYD)
    out, day = [], since.date()
    while day <= until.date():
        out += [
            o
            for o in _read(data_dir, "arena_orders", day)
            if o.get("event") == "submitted" and since < o["syd"] <= until
        ]
        day += timedelta(days=1)
    return sorted(out, key=lambda o: o["syd"])


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
        f"reached the decider: {c.to_decider} · bot orders: {c.orders_bot} · "
        f"exits placed: {c.exits}",
    ]
    if c.agent_unavailable:
        out.append(
            f"⚠️ the agent could not be asked {c.agent_unavailable} time"
            f"{'s' if c.agent_unavailable != 1 else ''} from {c.agent_unavailable_first}: "
            f"{escape(c.agent_unavailable_why)}"
        )
    if c.deferred:
        out.append(f"{c.deferred} announcement(s) still waiting for the live feed to screen")
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
    pbs = _playbooks(arena, pb)
    for p in pbs:
        if len(pbs) > 1:
            out.append(f"<i>{escape(p.title)}</i>")
        for b in balances(arena, p):
            held = ", ".join(b["holdings"]) or "no positions"
            pending = f", {b['pending']} pending fill" if b["pending"] else ""
            out.append(
                f"• {b['kind'].upper()} ${b['equity']:,.2f} ({b['pnl']:+,.2f}) — "
                f"{escape(held)}{pending}"
            )
    return "\n".join(out)


def _playbooks(arena, pb) -> list:
    """Every enabled playbook, `pb` first; just `pb` for a test double without playbooks."""
    try:
        others = [p for p in arena.playbooks() if p.key != pb.key]
    except Exception:  # noqa: BLE001
        others = []
    return [pb, *others]


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
