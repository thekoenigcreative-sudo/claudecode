"""The evening report.

Code assembles the facts and the scoreboard; the agent writes the prose around them
(ARENA.md: "Code calculates it; the agent writes it into the evening Telegram report").
`render_plain` is the fallback the code sends by itself if the agent is unavailable - the
report must still arrive on an evening when a model call fails.

Also here: the "is it doing enough?" block ARENA.md asks for in every report - alerts seen,
alerts acted on, trades placed, P&L against the matching bot, green days vs red days.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from html import escape
from zoneinfo import ZoneInfo

from asxbot.alerts import Alerts
from asxbot.announcements.history import status as archive_status
from asxbot.arena.runtime import Arena
from asxbot.arena.scoreboard import Score, agent_vs_bot, format_table, score
from asxbot.data.universe import asx200_provenance
from asxbot.io import write_text_atomic
from asxbot.log import EventLog

SYD = ZoneInfo("Australia/Sydney")
SENT_FILE = "report_sent.json"


def mark_report_sent(cfg, when: datetime | None = None) -> None:
    """Remember when the evening report was last delivered, so the next one can list what
    changed since (settings_changed). Best effort: a report is never failed over this."""
    at = (when or datetime.now(SYD)).isoformat(timespec="seconds")
    try:
        write_text_atomic(json.dumps({"sent_at": at}), cfg.data_dir / "arena" / SENT_FILE)
    except OSError:
        pass


def last_report_sent(cfg) -> datetime | None:
    try:
        raw = json.loads((cfg.data_dir / "arena" / SENT_FILE).read_text(encoding="utf-8"))
        t = datetime.fromisoformat(str(raw["sent_at"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=SYD)


def settings_changed(cfg, day: date) -> list[dict]:
    """Rick's deliberate model/effort changes (config.yaml arena.agents.history) since the
    previous report, or since the start of `day` when there is none. Each with a `line`:
    'decider model Opus 5.5 -> Sonnet 5 (Rick, 10:15pm)'."""
    from asxbot.arena import settings_history as H
    from asxbot.arena.agents import agent_settings

    start, end = H.report_window(last_report_sent(cfg), day)
    return [{**e, "line": H.describe(e, day)} for e in H.since(agent_settings(cfg), start, end)]


def test_day(pb, day: date) -> str:
    """Where the playbook is in its Level 1 test: "day N of 10 (v2)" (PLAN.md; the clock
    for v2 and the day trader starts 25 Sep 2026)."""
    from asxbot.announcements.live import is_trading_day

    start = pb.raw.get("test_start")
    if not start:
        return ""
    first = date.fromisoformat(str(start))
    n = int(pb.raw.get("test_days", 10))
    if day < first:
        return f"not started: day 1 of {n} (v{pb.version}) is {first:%a %d %b}"
    k, d = 0, first
    while d <= day:
        if d.weekday() < 5 and is_trading_day(d):
            k += 1
        d = date.fromordinal(d.toordinal() + 1)
    if k > n:
        return f"day {k} (v{pb.version}): past the {n}-day test, checkpoint due"
    return f"day {k} of {n} (v{pb.version})"


NO_PRICES = "no usable prices (no previous close on {})"
NOT_RECORDED = "price source not recorded"


def _price_fixes(data_dir, day: date) -> dict[str, str]:
    """Labels established after the fact, from the log, for decisions made before they were
    recorded (data/arena/price_sources/<day>.json: {"<ticker> <stage>": label}). Each entry
    there says what it rests on; nothing here guesses."""
    p = data_dir / "arena" / "price_sources" / f"{day.isoformat()}.json"
    if not p.exists():
        return {}
    return dict(json.loads(p.read_text(encoding="utf-8")).get("labels", {}))


def decision_data(cfg, day: date) -> dict[str, dict[str, int]]:
    """Which prices each of today's decisions was made on, per playbook: every day-trader
    setup, every v2 pre-open look, reaction look and the v2 rule bot record the label of the
    prices they used when they decide (IBKR live, or Yahoo delayed - including a fallback
    when IBKR was down). A look or rule that had no previous close to measure against made
    no decision on prices at all: it is counted as that, not under the feed's label."""
    from asxbot.arena.daytrader import load_state
    from asxbot.arena.reaction_v2 import NO_PREV_CLOSE, load_bot_state, load_queue

    out: dict[str, dict[str, int]] = {}
    fixes = _price_fixes(cfg.data_dir, day)

    def add(key: str, label) -> None:
        if label:
            d = out.setdefault(key, {})
            d[str(label)] = d.get(str(label), 0) + 1

    for sig in load_state(cfg.data_dir, day).get("signals", []):
        add("asx_daytrader", sig.get("data"))
    for rec in EventLog(cfg.data_dir).read("arena_decisions"):
        if rec.get("v2") != "pre_open" or rec.get("stage") != "decider":
            continue
        try:
            at = datetime.fromisoformat(rec["ts"]).astimezone(SYD)
        except (KeyError, ValueError):
            continue
        if at.date() == day:
            add("asx_announcements_v2", rec.get("data")
                or fixes.get(f"{rec.get('ticker')} pre_open") or NOT_RECORDED)  # fmt: skip
    for k, v in load_queue(cfg.data_dir, day).items():
        if not k.startswith("_") and isinstance(v, dict):
            r = v.get("reaction") or {}
            if r.get("why") == NO_PREV_CLOSE:
                add("asx_announcements_v2", NO_PRICES.format(r.get("data_label") or "?"))
            else:
                add("asx_announcements_v2", r.get("data_label"))
    bot = load_bot_state(cfg.data_dir, day)
    measured = [c for c in bot.get("candidates", []) if "signal" in c]
    if measured and all(c.get("why") == NO_PREV_CLOSE for c in measured):
        add("asx_announcements_v2", NO_PRICES.format(bot.get("data") or "?"))
    else:
        add("asx_announcements_v2", bot.get("data"))
    return out


def data_line(pb, counts: dict[str, int] | None) -> str:
    """The playbook's data label for the day: its frozen label when every decision was on
    Yahoo's delayed feed (or there were none), otherwise how many were on which prices."""
    from asxbot.arena.intraday import DELAYED_LABEL

    counts = counts or {}
    if not counts or set(counts) == {DELAYED_LABEL}:
        return pb.data_basis or ""
    return "prices per decision: " + ", ".join(f"{n} on {k}" for k, n in counts.items())


def playbook_facts(arena: Arena, day: date) -> list[dict]:
    try:
        used = decision_data(arena.cfg, day)
    except Exception:  # noqa: BLE001 - a label must never stop the report
        used = {}
    return [
        {
            "key": pb.key, "title": pb.title, "version": pb.version, "level": pb.level.number,
            "test": test_day(pb, day), "data": data_line(pb, used.get(pb.key)),
            "data_by_decision": used.get(pb.key, {}),
            "accounts": [pb.agent_account, pb.bot_account],
        }
        for pb in arena.playbooks()
    ]  # fmt: skip


def v2_facts(cfg, day: date) -> dict:
    """Announcements v2 today: the reaction looks and the rule bot, from their files."""
    from asxbot.arena.reaction_v2 import load_bot_state, load_queue

    q = {k: v for k, v in load_queue(cfg.data_dir, day).items() if not k.startswith("_")}
    by: dict[str, int] = {}
    for v in q.values():
        by[v.get("status", "?")] = by.get(v.get("status", "?"), 0) + 1
    bot = load_bot_state(cfg.data_dir, day)
    return {
        "stocks_with_news_queued": len(q),
        "reaction_looks_by_outcome": by,
        "looked": [
            {"ticker": k, "outcome": v.get("outcome"), "reaction": v.get("reaction")}
            for k, v in q.items() if v.get("status") == "looked"
        ],
        "rule_bot": {
            "status": bot.get("status"), "why": bot.get("why"),
            "signals": [c for c in bot.get("candidates", []) if c.get("signal")],
            "orders": bot.get("orders", []),
        },
    }  # fmt: skip


def daytrader_facts(cfg, day: date) -> dict:
    from asxbot.arena.daytrader import load_state

    st = load_state(cfg.data_dir, day)
    sig = st.get("signals", [])
    by: dict[str, int] = {}
    for x in sig:
        by[x["setup"]] = by.get(x["setup"], 0) + 1
    agent = [x.get("agent") or {} for x in sig]
    return {
        "universe": len(st.get("universe", [])),
        "setups_found": len(sig),
        "by_setup": by,
        "bot_orders": sum(1 for x in sig if (x.get("bot") or {}).get("order_id")),
        "agent_took": sum(1 for a in agent if a.get("order_id")),
        "agent_rejected": sum(1 for a in agent if "rejected" in a),
        "agent_not_asked": sum(1 for a in agent if a.get("skipped")),
        "rejections": [a.get("rejected") for a in agent if "rejected" in a][:8],
    }


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

    keys = {pb.key for pb in arena.playbooks()}
    try:
        changed = settings_changed(cfg, day)
    except Exception:  # noqa: BLE001 - a malformed history entry must never stop the report
        changed = [{"line": "(config.yaml arena.agents.history could not be read)"}]
    return {
        "day": iso,
        # Deliberate changes Rick made to the agents' model or effort (/model, /think in the
        # Trader chat) since the previous report. Every one must be mentioned.
        "settings_changed": changed,
        "playbooks": playbook_facts(arena, day),
        "announcements_v2_today": v2_facts(cfg, day) if "asx_announcements_v2" in keys else None,
        "daytrader_today": daytrader_facts(cfg, day) if "asx_daytrader" in keys else None,
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
    ]
    changed = facts.get("settings_changed") or []
    if changed:
        lines.append(
            "Settings changed: " + "; ".join(escape(str(c.get("line", ""))) for c in changed)
        )
    lines.append("")
    for p in facts.get("playbooks", []):
        lines.append(f"<b>{p['title']}</b>: {p['test']}" + (f" - {p['data']}" if p["data"] else ""))
    dt = facts.get("daytrader_today")
    if dt:
        lines.append(
            f"- day trader: {dt['setups_found']} setups found in {dt['universe']} stocks "
            f"({', '.join(f'{k} {v}' for k, v in dt['by_setup'].items()) or 'none'}); "
            f"bot orders {dt['bot_orders']}; agent took {dt['agent_took']}, rejected "
            f"{dt['agent_rejected']}"
        )
    v2 = facts.get("announcements_v2_today")
    if v2:
        lines.append(
            f"- announcements v2: {v2['stocks_with_news_queued']} stocks with news; reaction "
            f"looks {v2['reaction_looks_by_outcome'] or 'none'}; rule bot "
            f"{v2['rule_bot']['status'] or 'did not run'}, "
            f"{len(v2['rule_bot']['signals'])} signal(s)"
        )
    lines += [
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
    payload = {k: v for k, v in facts.items() if k != "score_objects"}
    changed = [str(c.get("line", "")) for c in facts.get("settings_changed") or []]
    settings_rule = (
        "- Rick deliberately changed the agents' settings since the last report (FACTS "
        "settings_changed). You MUST mention every one near the top, on one line starting "
        "'Settings changed:', as Rick's deliberate change, not a finding: "
        + "; ".join(changed)
        + ".\n"
        if changed
        else ""
    )
    return (
        "Write tonight's arena report for Rick, for Telegram.\n\n"
        "Rules:\n"
        "- These numbers are the truth. Do not restate them wrongly and do not invent any.\n"
        "- Say plainly whether the day was good or bad. Honest results over pretty ones.\n"
        "- Cover: what you traded and why, what the limits refused, how you are doing against "
        "your yardstick bot, green days vs red days, and what you will watch next.\n"
        "- If you placed no trades, say why not - that is a real answer.\n"
        f"{settings_rule}"
        "- Index membership comes from FACTS asx200_list, which is authoritative and dated "
        f"(as of {(facts.get('asx200_list') or {}).get('as_of') or 'unknown'}). Never say "
        "from memory that a company is or is not an index member, or that a membership flag "
        "'looks wrong'. If you believe a dated fact is wrong, add one last line starting "
        "'Flag for Claude:', worded as a question to check - never as a fact in the report.\n"
        "- There are playbooks in FACTS playbooks. Report each separately, each against its "
        "own yardstick bot. Head each with its title and its `test` value word for word "
        "(for example 'day 1 of 10 (v2)'), and its `data` label word for word - 'delayed data "
        "- rehearsal until IBKR live prices' - so no one mistakes a rehearsal for a result.\n"
        "- For the day trader say how many setups the scan found, how many you took and "
        "rejected, and what the rule bot did (FACTS daytrader_today). For announcements v2 "
        "say what the reaction looks and the rule bot did (FACTS announcements_v2_today).\n"
        "- Keep it under 3000 characters. Plain text with simple HTML tags "
        "(<b>, <i>, <pre>) only.\n\n"
        f"FACTS (JSON):\n{json.dumps(payload, indent=2, default=str)}\n"
    )
