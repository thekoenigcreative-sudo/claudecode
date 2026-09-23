"""The evening report.

Code assembles the facts and the scoreboard; the agent writes the prose around them
(ARENA.md: "Code calculates it; the agent writes it into the evening Telegram report").
`render_plain` is the fallback the code sends by itself if the agent is unavailable - the
report must still arrive on an evening when a model call fails.

Also here: the "is it doing enough?" block ARENA.md asks for in every report - alerts seen,
alerts acted on, trades placed, P&L against the matching bot, green days vs red days.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from asxbot.alerts import Alerts
from asxbot.announcements.history import status as archive_status
from asxbot.arena.runtime import Arena
from asxbot.arena.scoreboard import Score, agent_vs_bot, format_table, score
from asxbot.data.universe import asx200_provenance
from asxbot.log import EventLog

SYD = ZoneInfo("Australia/Sydney")


def gather(arena: Arena, day: date | None = None) -> dict:
    """Every fact the report is allowed to use. No model involved."""
    cfg = arena.cfg
    day = day or datetime.now(SYD).date()
    iso = day.isoformat()
    ev = EventLog(cfg.data_dir)

    def today_rows(kind: str) -> list[dict]:
        return [r for r in ev.read(kind) if str(r.get("ts", ""))[:10] == iso]

    scores: list[Score] = []
    positions: list[dict] = []
    for pb in arena.playbooks():
        for kind in ("agent", "bot"):
            acct = arena.account(pb, kind)
            arena.broker.accrue_borrow(acct, day)
            prices = arena.broker.prices(acct, day)
            arena.broker.mark_to_market(acct, day)
            scores.append(score(arena.store, acct, prices))
            for t, p in acct.positions.items():
                px = prices.get(t, p.avg_cost)
                positions.append(
                    {
                        "account": acct.name,
                        "ticker": t,
                        "qty": p.qty,
                        "avg_cost": round(p.avg_cost, 4),
                        "last": round(px, 4),
                        "open_pnl": round((px - p.avg_cost) * p.qty, 2),
                        "stop": p.stop,
                        "target": p.target,
                        "opened_at": p.opened_at,
                        "thesis": p.thesis,
                        "by": p.opened_by,
                        "model": p.model,
                    }
                )

    # Only count activity that still belongs to a live account. The event log is
    # append-only, so after `arena reset` it still holds orders from wiped accounts -
    # and a scoreboard saying "no trades" beside a list of fills is worse than useless.
    live: dict[str, set[str]] = {}
    for pb in arena.playbooks():
        for kind in ("agent", "bot"):
            acct = arena.account(pb, kind)
            live[acct.name] = set(acct.orders)

    def belongs(r: dict) -> bool:
        acct_name = r.get("account")
        if acct_name not in live:
            return False
        oid = r.get("order_id")
        return oid is None or oid in live[acct_name]

    working = [
        {"order_id": o.order_id, "account": o.account, "side": o.side, "ticker": o.ticker,
         "order_type": o.order_type, "filled": o.filled_qty, "ordered": abs(o.qty),
         "avg_price": o.avg_price, "message": o.message}  # fmt: skip
        for pb in arena.playbooks()
        for kind in ("agent", "bot")
        for o in arena.account(pb, kind).orders.values()
        if o.working and o.filled_qty
    ]
    orders_today = [r for r in today_rows("arena_orders") if belongs(r)]
    fills_today = [r for r in today_rows("arena_fills") if belongs(r)]
    alerts_today = today_rows("arena_alerts")
    signals_today = today_rows("signals")

    return {
        "day": iso,
        "broker_mode": cfg.broker,
        "data_label": cfg.data_label(),
        "money": "FAKE money (arena). No real order can be placed from here.",
        "scores": [s.to_dict() for s in scores],
        "score_objects": scores,
        "positions": positions,
        "alerts_seen": len(alerts_today),
        "alerts_acted_on": len({r.get("ticker") for r in orders_today if r.get("ticker")}),
        "announcements_seen": len(today_rows("announcements")),
        "signals_checked": len(signals_today),
        "orders_placed": sum(1 for r in orders_today if r.get("outcome") == "accepted"),
        "orders_refused": sum(1 for r in orders_today if r.get("outcome") == "refused"),
        "refusal_reasons": [
            r.get("reason") for r in orders_today if r.get("outcome") == "refused"
        ][:10],
        "fills": len(fills_today),
        "fill_details": [
            {
                "order_id": r.get("order_id"),
                "account": r.get("account"),
                "side": r.get("side"),
                "ticker": r.get("ticker"),
                "qty": r.get("filled_qty"),
                "ordered": abs(int(r.get("qty") or 0)),
                "status": r.get("event"),  # filled | partial (the rest expired or cancelled)
                "bars": len(r.get("fills") or []) or 1,
                "price": r.get("avg_price"),
                "fee": r.get("commission"),
                "realised": r.get("realised"),
                "basis": r.get("fill_basis"),
                "reason": r.get("reason"),
                "model": r.get("model"),
            }
            for r in fills_today
        ],
        "pending_fills": sum(s.pending_fills for s in scores),
        # Orders still working that have part-filled: no bar fills more than a set share of
        # its traded volume (arena.fill.max_volume_share), so a large order in a thin stock
        # fills over several bars, and a stop or target exit keeps working overnight.
        "part_filled_working": working,
        "active_alerts": [
            {"key": k, "message": m.splitlines()[-1]} for k, m in Alerts(cfg.data_dir).active()
        ],
        "archive": archive_status(cfg.data_dir),
        "agent_vs_bot": agent_vs_bot(scores),
        # The list the short rule reads, and its date. Authoritative: the report does not
        # overrule it from memory (TRACKER #9; NUF, 23 Sep).
        "asx200_list": asx200_provenance(
            cfg.data_dir, set(getattr(arena, "short_universe", None) or ()) or None
        ),
        "flags_for_claude": [
            {"ticker": r.get("ticker"), "flag": r.get("flag")} for r in today_rows("arena_flags")
        ],
    }


def render_plain(facts: dict) -> str:
    """The fallback report, written by code. Plain, complete, and honest."""
    lines = [
        f"<b>ASX arena - {facts['day']}</b>",
        f"{facts['money']}",
        f"Data: {facts['data_label']}",
        "",
        "<b>Scoreboard</b>",
        f"<pre>{format_table(facts['score_objects'])}</pre>",
    ]
    if facts["agent_vs_bot"]:
        lines.append("<b>Agent against its yardstick</b>")
        lines += [f"- {x}" for x in facts["agent_vs_bot"]]
        lines.append("")

    lines.append("<b>Today</b>")
    lines.append(
        f"- {facts['announcements_seen']} announcements seen, "
        f"{facts['signals_checked']} checked against the rule"
    )
    lines.append(
        f"- {facts['orders_placed']} orders placed, {facts['orders_refused']} refused by the "
        f"limits, {facts['fills']} filled, {facts['pending_fills']} still waiting for a price"
    )
    for r in facts["refusal_reasons"]:
        lines.append(f"    refused: {r}")
    for f in facts["fill_details"]:
        part = f" of {f['ordered']} (the rest did not fill)" if f.get("status") == "partial" else ""
        lines.append(
            f"- FILL {f['order_id']} {f['side']} {f['qty']}{part} {f['ticker']} @ {f['price']} "
            f"(fee {f['fee']}) [{f['account']}]"
        )
        if f.get("basis"):
            lines.append(f"    price basis: {f['basis']}")
    if not facts["fill_details"]:
        lines.append("- no fills today")
    for w in facts.get("part_filled_working", []):
        lines.append(
            f"- STILL WORKING {w['order_id']} {w['side']} {w['ticker']}: {w['filled']} of "
            f"{w['ordered']} filled so far [{w['account']}]"
        )

    lines.append("")
    lines.append("<b>Open positions</b>")
    if facts["positions"]:
        for p in facts["positions"]:
            lines.append(
                f"- {p['ticker']} {p['qty']:+d} @ {p['avg_cost']} now {p['last']} "
                f"({p['open_pnl']:+,.2f}) stop {p['stop']} [{p['account']}]"
            )
    else:
        lines.append("- none")

    if facts["active_alerts"]:
        lines.append("")
        lines.append("<b>Alerts</b>")
        for a in facts["active_alerts"]:
            lines.append(f"- {a['key']}: {a['message']}")

    lines.append("")
    lines.append(f"Archive: {facts['archive']}")
    lines.append("Written by code (the agent did not write this one).")
    return "\n".join(lines)


def agent_brief(facts: dict) -> str:
    """What the decider agent is given so it can write the report in its own words."""
    import json

    payload = {k: v for k, v in facts.items() if k != "score_objects"}
    return (
        "Write tonight's arena report for Rick, for Telegram.\n\n"
        "Rules:\n"
        "- These numbers are the truth. Do not restate them wrongly and do not invent any.\n"
        "- Say plainly whether the day was good or bad. Honest results over pretty ones.\n"
        "- Cover: what you traded and why, what the limits refused, how you are doing against "
        "your yardstick bot, green days vs red days, and what you will watch next.\n"
        "- If you placed no trades, say why not - that is a real answer.\n"
        "- Index membership comes from FACTS asx200_list, which is authoritative and dated "
        f"(as of {(facts.get('asx200_list') or {}).get('as_of') or 'unknown'}). Never say "
        "from memory that a company is or is not an index member, or that a membership flag "
        "'looks wrong'. If you believe a dated fact is wrong, add one last line starting "
        "'Flag for Claude:', worded as a question to check - never as a fact in the report.\n"
        "- Keep it under 2500 characters. Plain text with simple HTML tags "
        "(<b>, <i>, <pre>) only.\n\n"
        f"FACTS (JSON):\n{json.dumps(payload, indent=2, default=str)}\n"
    )
