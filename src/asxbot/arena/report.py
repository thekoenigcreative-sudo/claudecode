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
from datetime import date, datetime, time
from html import escape
from zoneinfo import ZoneInfo

from asxbot.alerts import Alerts
from asxbot.announcements.history import status as archive_status
from asxbot.arena.runtime import Arena
from asxbot.arena.scoreboard import Score, agent_vs_bot, format_table, round_trips, score
from asxbot.arena.tally import _read as read_day
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
    partial = partial_day(pb, day)
    tail = f" - PARTIAL DAY ({partial})" if partial else ""
    if k > n:
        return f"day {k} (v{pb.version}): past the {n}-day test, checkpoint due{tail}"
    return f"day {k} of {n} (v{pb.version}){tail}"


COVERAGE_FROM = date(2026, 9, 28)  # the first day with a watcher day record (heartbeat.py)
MARKET = (time(10, 0), time(16, 10))


def watcher_coverage(data_dir, day: date) -> dict:
    """How much of the session the watcher covered (26 Sep 2026, review D6), from its day
    record (heartbeat.session_path): {"line", "candidate_partial", "gaps"}. A gap or a
    stalled cycle inside 10:00-16:10 makes the day a CANDIDATE partial day - how such a
    day counts in the 10 is Rick's call (TRACKER #57), so nothing is excluded here."""
    from asxbot.announcements.live import is_trading_day
    from asxbot.arena.heartbeat import read_session

    if not is_trading_day(day):
        return {"line": "", "candidate_partial": False, "gaps": []}
    s = read_session(data_dir, day)
    if s is None:
        if day < COVERAGE_FROM:
            return {"line": "", "candidate_partial": False, "gaps": []}
        return {"line": "Watcher: NO RECORD of it running today - a candidate partial day",
                "candidate_partial": True, "gaps": ["no record"]}  # fmt: skip

    def t(x):
        try:
            v = datetime.fromisoformat(str(x))
        except (TypeError, ValueError):
            return None
        return v if v.tzinfo else v.replace(tzinfo=SYD)

    open_ = datetime.combine(day, MARKET[0], tzinfo=SYD)
    close = datetime.combine(day, MARKET[1], tzinfo=SYD)
    first, last = t(s.get("first_beat")), t(s.get("last_beat"))
    bad = []
    if first is None or first > open_:
        bad.append(f"started {first:%H:%M}" if first else "no start")
    if last is None or last < close:
        bad.append(f"last seen {last:%H:%M}" if last else "no end")
    for kind, spans in (("DOWN", s.get("gaps") or []), ("a cycle stalled", s.get("stalls") or [])):
        for a, b in spans:
            a, b = t(a), t(b)
            if a is None or b is None or b <= open_ or a >= close:
                continue
            mins = (b - a).total_seconds() / 60
            bad.append(f"{kind} {a:%H:%M}-{b:%H:%M} ({mins:.0f} min)")
    span = f"{first:%H:%M}-{last:%H:%M}" if first and last else "?"
    if not bad:
        return {"line": f"Watcher: ran {span}, no gap in market hours",
                "candidate_partial": False, "gaps": []}  # fmt: skip
    return {"line": f"Watcher: ran {span}; in market hours {'; '.join(bad)} - a candidate "
                    "partial day (how it counts is Rick's call)",
            "candidate_partial": True, "gaps": bad}  # fmt: skip


def _coverage(data_dir, day: date) -> dict:
    try:
        return watcher_coverage(data_dir, day)
    except Exception as e:  # noqa: BLE001 - a coverage line must never stop the report
        return {"line": f"Watcher coverage could not be read ({type(e).__name__})",
                "candidate_partial": False, "gaps": []}  # fmt: skip


def partial_day(pb, day: date) -> str:
    """The reason `day` is only a partial test day for this playbook (config
    `partial_days`, dated), or "" for a full day."""
    days = pb.raw.get("partial_days") or {}
    return str(days.get(day.isoformat()) or days.get(day) or "")


def scorecard_facts(arena: Arena, day: date, scores: list[Score]) -> list[dict]:
    """The daily scorecard (Rick's brief, 25 Sep): per playbook and account, today's P&L
    after fees, the running total, green/red days so far, the worst day and the worst
    peak-to-trough drop, how much of the gross profit the best trade is, and which prices
    today's decisions were made on (playbook_facts carries the per-decision counts).

    Trades are round trips, won after every cost, since 26 Sep 2026 (D4,
    scoreboard.round_trips). Until then closing orders were counted, each less only its own
    brokerage: 25 Sep's day-trader bot read "4 trades, 4 won, 56% from the best trade"; it
    had 3 round trips, 1 won after costs, and 92% of the gross profit from NWL."""
    by_acct = {s.account: s for s in scores}
    out = []
    for pb in arena.playbooks():
        row = {"key": pb.key, "title": pb.title, "test": test_day(pb, day),
               "partial": partial_day(pb, day), "accounts": []}  # fmt: skip
        for kind in ("agent", "bot"):
            acct = arena.account(pb, kind)
            s = by_acct.get(acct.name)
            prices = arena.broker.prices(acct, day)
            start = arena.store.day_start_equity(acct, day)
            equity = acct.equity(prices)
            trips = round_trips(acct)
            closed = [t for t in trips if t.is_closed]
            gains = sorted((t.gross for t in closed if t.gross > 0), reverse=True)
            gross_gain = sum(gains)
            row["accounts"].append({
                "kind": kind, "name": acct.name,
                "today_after_fees": round(equity - start, 2),
                "total_after_fees": round(equity - acct.starting_cash, 2),
                "fees_total": round(acct.fees_paid + acct.borrow_paid, 2),
                "green_days": s.green_days if s else 0, "red_days": s.red_days if s else 0,
                "worst_day_pct": s.worst_day_pct if s else 0.0,
                "max_drawdown_pct": s.max_drawdown_pct if s else 0.0,
                "trades": len(closed), "open_trades": len(trips) - len(closed),
                "wins_after_fees": sum(1 for t in closed if t.net > 0),
                "round_trips": [t.to_dict() for t in closed
                                if (t.closed or "")[:10] == day.isoformat()],
                "best_trade": round(gains[0], 2) if gains else None,
                "best_trade_share_pct": (round(gains[0] / gross_gain * 100, 1)
                                         if gains and gross_gain > 0 else None),
            })  # fmt: skip
        out.append(row)
    return out


def scorecard_lines(facts: dict) -> list[str]:
    """The scorecard as plain lines, for the code-written report and the agent's brief."""
    lines = []
    for row in facts.get("scorecard") or []:
        lines.append(f"<b>{escape(row['title'])}</b> - {escape(row['test'])}")
        if row.get("partial"):
            lines.append(f"  PARTIAL DAY: {escape(row['partial'])}")
        for a in row["accounts"]:
            best = ("-" if a["best_trade_share_pct"] is None
                    else f"{a['best_trade_share_pct']:.0f}% from the best trade "
                         f"({a['best_trade']:+,.2f})")  # fmt: skip
            still = f", {a['open_trades']} still open" if a.get("open_trades") else ""
            lines.append(
                f"  {a['kind'].upper()}: today {a['today_after_fees']:+,.2f} after fees, total "
                f"{a['total_after_fees']:+,.2f} (fees {a['fees_total']:,.2f}); green/red days "
                f"{a['green_days']}/{a['red_days']}; worst day {a['worst_day_pct']:+.2f}%, worst "
                f"drop {a['max_drawdown_pct']:.2f}%; round trips {a['trades']} "
                f"({a['wins_after_fees']} won after all costs{still}); gross profit {best}"
            )
            for t in a.get("round_trips") or []:
                lines.append(
                    f"    {escape(t['ticker'])} {t['side']}: {t['net']:+,.2f} after costs "
                    f"(gross {t['gross']:+,.2f}, fees {t['entry_fees'] + t['exit_fees']:,.2f}"
                    + (f", borrow {t['borrow']:,.2f}" if t["borrow"] else "") + ")"
                )
        pb = next((p for p in facts.get("playbooks", []) if p["key"] == row["key"]), None)
        if pb and pb.get("data_by_decision"):
            lines.append("  prices per decision: " + ", ".join(
                f"{n} on {k}" for k, n in pb["data_by_decision"].items()))
    return lines


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


NO_DECISIONS = "no entry decisions today"


def data_line(pb, counts: dict[str, int] | None) -> str:
    """The playbook's data label for the day: its frozen label when every decision was on
    Yahoo's delayed feed, otherwise how many were on which prices. With no decisions at all
    it says so (26 Sep 2026, H5): until then a day without decisions printed the frozen
    "delayed data - rehearsal until IBKR live prices", on days that ran on IBKR's prices."""
    from asxbot.arena.intraday import DELAYED_LABEL

    counts = counts or {}
    if not counts:
        return NO_DECISIONS
    if set(counts) == {DELAYED_LABEL}:
        return pb.data_basis or DELAYED_LABEL
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


UNAVAILABLE_OUTCOMES = ("decider_failed", "agent_unavailable")


def unavailable_text(text) -> bool:
    """A recorded answer that was really no answer: the agent could not be asked (a failed
    call, "agent unavailable (<kind>)"), including the day trader's pre-26 Sep wording for it,
    "no answer within 60s (...)", which every failed call got whatever the cause."""
    t = str(text or "").strip().lower()
    return t.startswith(("agent unavailable", "no answer within", "decider failed"))


def _look_unavailable(v: dict) -> bool:
    """A v2 reaction look whose decider call failed: recorded "looked", but nobody looked."""
    out = v.get("outcome") or {}
    if not isinstance(out, dict):
        return False
    return bool(out.get("unavailable")) or unavailable_text(out.get("error"))


def v2_facts(cfg, day: date) -> dict:
    """Announcements v2 today: the reaction looks and the rule bot, from their files.

    A look whose decider call failed is counted as `agent unavailable`, not as a look (26 Sep
    2026, G1): its queue status says "looked", but the agent never saw it."""
    from asxbot.arena.reaction_v2 import load_bot_state, load_queue

    q = {k: v for k, v in load_queue(cfg.data_dir, day).items() if not k.startswith("_")}
    by: dict[str, int] = {}
    for v in q.values():
        status = v.get("status", "?")
        if status == "looked" and _look_unavailable(v):
            status = "agent unavailable"
        by[status] = by.get(status, 0) + 1
    bot = load_bot_state(cfg.data_dir, day)
    pre_open_failed = [
        r for r in read_day(cfg.data_dir, "arena_decisions", day)
        if r.get("v2") == "pre_open" and r.get("outcome") in UNAVAILABLE_OUTCOMES
    ]  # fmt: skip
    return {
        "stocks_with_news_queued": len(q),
        "reaction_looks_by_outcome": by,
        "looks_agent_unavailable": by.get("agent unavailable", 0),
        "pre_open_agent_unavailable": len(pre_open_failed),
        "looked": [
            {"ticker": k, "outcome": v.get("outcome"), "reaction": v.get("reaction")}
            for k, v in q.items() if v.get("status") == "looked" and not _look_unavailable(v)
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

    def uneconomic(x: dict) -> bool:
        return any(str((x.get(k) or {}).get("skipped", "")).startswith("uneconomic")
                   for k in ("bot", "agent"))  # fmt: skip

    def unavailable(a: dict) -> bool:
        # The day trader's record of a failed call: {"unavailable": kind, ...} from 26 Sep
        # 2026; before that a "rejection" reading "no answer within 60s (...)".
        return bool(a.get("unavailable")) or unavailable_text(a.get("rejected"))

    rejected = [a for a in agent if "rejected" in a and not unavailable(a)]
    return {
        "universe": len(st.get("universe", [])),
        "setups_found": len(sig),
        "by_setup": by,
        "bot_orders": sum(1 for x in sig if (x.get("bot") or {}).get("order_id")),
        "agent_took": sum(1 for a in agent if a.get("order_id")),
        # The agent's own rejections only: a call that failed is not a rejection (G1).
        "agent_rejected": len(rejected),
        "agent_unavailable": sum(1 for a in agent if unavailable(a)),
        "agent_not_asked": sum(1 for a in agent if a.get("skipped")),
        # Too old to be offered to anyone by the scan (26 Sep 2026, D11: 28 of 67 on 25 Sep,
        # uncounted), and those that went stale while the agent was busy with earlier calls.
        "stale": sum(1 for x in sig if str(x.get("skipped") or "").startswith("stale")),
        "agent_stale_by_its_turn": sum(
            1 for a in agent if str(a.get("skipped") or "").startswith("stale")),
        "after_last_entry": sum(1 for x in sig if "last entry" in str(x.get("skipped") or "")),
        # Filtered by the scanner before anyone was asked: 1R at the largest size the rules
        # allow below 2x the round-trip cost (Rick's brief, 25 Sep; daytrader.economic).
        "uneconomic": sum(1 for x in sig if uneconomic(x)),
        "history": st.get("history"),
        "rejections": [a.get("rejected") for a in rejected][:8],
    }


def agent_unavailable_facts(cfg, day: date) -> dict:
    """Every agent call that failed on `day` (arena_agent_calls records with ok False, written
    for every call since 26 Sep 2026, G1): how many, from when, and why. The Trader chat's
    calls are counted apart: they are not trading decisions."""
    from asxbot.arena.agents import FAILURE_WORDS

    fails = [r for r in read_day(cfg.data_dir, "arena_agent_calls", day) if r.get("ok") is False]
    chat = [r for r in fails if str(r.get("purpose", "")).startswith("chat")]
    trading = [r for r in fails if not str(r.get("purpose", "")).startswith("chat")]
    kinds: dict[str, int] = {}
    for r in trading:
        k = str(r.get("kind") or "error")
        kinds[k] = kinds.get(k, 0) + 1
    out = {"calls_failed": len(trading), "by_kind": kinds, "chat_calls_failed": len(chat),
           "first": None, "last": None, "line": ""}  # fmt: skip
    if trading:
        times = sorted(r["syd"] for r in trading)
        out["first"], out["last"] = f"{times[0]:%H:%M}", f"{times[-1]:%H:%M}"
        why = ", ".join(
            f"{FAILURE_WORDS.get(k, k)}" + (f" {n}" if len(kinds) > 1 else "")
            for k, n in sorted(kinds.items(), key=lambda kv: -kv[1])
        )
        span = (f"from {out['first']}" if out["first"] == out["last"]
                else f"from {out['first']} to {out['last']}")  # fmt: skip
        out["line"] = (
            f"the agent could not be asked {len(trading)} time"
            f"{'s' if len(trading) != 1 else ''} {span}: {why}"
        )
        out["purposes"] = sorted({str(r.get("purpose", "")) for r in trading})[:10]
    return out


def gather(arena: Arena, day: date | None = None) -> dict:
    """Every fact the report is allowed to use. No model involved."""
    cfg = arena.cfg
    day = day or datetime.now(SYD).date()
    iso = day.isoformat()

    def today_rows(kind: str) -> list[dict]:
        # The Sydney day, not the UTC date string of the record (26 Sep 2026, D5): event
        # times are UTC, so comparing their first ten characters dropped everything before
        # 10:00 Sydney (11:00 from 5 Oct): 470 of 837 announcements on 25 Sep, and every
        # pre-open order. Rehearsal records are left out, as in the day's other counts.
        return read_day(cfg.data_dir, kind, day)

    scores: list[Score] = []
    positions: list[dict] = []
    for pb in arena.playbooks():
        for kind in ("agent", "bot"):
            acct = arena.account(pb, kind)
            arena.broker.accrue_borrow(acct, day)
            marks = arena.broker.price_marks(acct, day)
            prices = {t: px for t, (px, _) in marks.items()}
            arena.broker.mark_to_market(acct, day)
            scores.append(score(arena.store, acct, prices))
            for t, p in acct.positions.items():
                px, priced = marks.get(t, (p.avg_cost, "cost (no traded price known)"))
                positions.append(
                    {
                        "account": acct.name,
                        "ticker": t,
                        "qty": p.qty,
                        "avg_cost": round(p.avg_cost, 4),
                        "last": round(px, 4),
                        # What `last` is (26 Sep 2026, D13): the day's last trade, the last
                        # traded price before the day, or cost when no price is known.
                        "priced": priced,
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
    try:
        unavailable = agent_unavailable_facts(cfg, day)
    except Exception:  # noqa: BLE001 - a count must never stop the report
        unavailable = {"calls_failed": 0, "line": "(the failed agent calls could not be read)"}
    return {
        "day": iso,
        # Agent calls that failed today (usage limit, timeout, error): said plainly, first,
        # because every "rejected" or "looked" after the first one may be a call that never
        # happened (26 Sep 2026, G1).
        "agent_unavailable": unavailable,
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
        # The daily scorecard (25 Sep): P&L after fees, green/red days, worst drop, the
        # best trade's share, prices per decision, and any partial day with its reason.
        "scorecard": scorecard_facts(arena, day, scores),
        # How much of the session the watcher covered (26 Sep 2026, D6).
        "watcher_coverage": _coverage(cfg.data_dir, day),
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
    down = facts.get("agent_unavailable") or {}
    if down.get("line"):
        lines.append(f"<b>AGENT UNAVAILABLE</b>: {escape(down['line'])}")
    lines.append("")
    for p in facts.get("playbooks", []):
        lines.append(f"<b>{p['title']}</b>: {p['test']}" + (f" - {p['data']}" if p["data"] else ""))
    dt = facts.get("daytrader_today")
    if dt:
        lines.append(
            f"- day trader: {dt['setups_found']} setups found in {dt['universe']} stocks "
            f"({', '.join(f'{k} {v}' for k, v in dt['by_setup'].items()) or 'none'}); "
            f"{dt.get('stale', 0)} too stale to offer; bot orders {dt['bot_orders']}; agent "
            f"took {dt['agent_took']}, rejected {dt['agent_rejected']}"
            + (f", could not be asked {dt['agent_unavailable']}"
               if dt.get("agent_unavailable") else "")  # fmt: skip
        )
    v2 = facts.get("announcements_v2_today")
    if v2:
        lines.append(
            f"- announcements v2: {v2['stocks_with_news_queued']} stocks with news; reaction "
            f"looks {v2['reaction_looks_by_outcome'] or 'none'}; rule bot "
            f"{v2['rule_bot']['status'] or 'did not run'}, "
            f"{len(v2['rule_bot']['signals'])} signal(s)"
            + (f"; pre-open looks the agent could not be asked: "
               f"{v2['pre_open_agent_unavailable']}"
               if v2.get("pre_open_agent_unavailable") else "")  # fmt: skip
        )
    cov = facts.get("watcher_coverage") or {}
    lines += ["", "<b>Scorecard</b>", *scorecard_lines(facts)]
    if cov.get("line"):
        lines.append(("  NOT A FULL DAY - " if cov.get("candidate_partial") else "  ")
                     + escape(cov["line"]))  # fmt: skip
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
            priced = p.get("priced") or ""
            lines.append(
                f"- {p['ticker']} {p['qty']:+d} @ {p['avg_cost']} now {p['last']} "
                f"({p['open_pnl']:+,.2f}) stop {p['stop']} [{p['account']}]"
                + (f" - {escape(priced)}" if priced and priced != "last trade that day" else "")
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
    down = (facts.get("agent_unavailable") or {}).get("line") or ""
    down_rule = (
        "- The agents could not be asked at times today (FACTS agent_unavailable). Say so "
        f"plainly near the top, in these words: '{down}'. Those setups and looks were never "
        "decided: do not call them rejections or passes.\n"
        if down
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
        f"{down_rule}"
        "- Index membership comes from FACTS asx200_list, which is authoritative and dated "
        f"(as of {(facts.get('asx200_list') or {}).get('as_of') or 'unknown'}). Never say "
        "from memory that a company is or is not an index member, or that a membership flag "
        "'looks wrong'. If you believe a dated fact is wrong, add one last line starting "
        "'Flag for Claude:', worded as a question to check - never as a fact in the report.\n"
        "- There are playbooks in FACTS playbooks. Report each separately, each against its "
        "own yardstick bot. Head each with its title and its `test` value word for word "
        "(for example 'day 1 of 10 (v2)'), and its `data` value word for word, whatever it "
        "says: it names the prices the day's decisions were made on.\n"
        "- For the day trader say how many setups the scan found, how many were too stale to "
        "offer, how many you took and rejected, how many you could not be asked about, and "
        "what the rule bot did (FACTS daytrader_today). For announcements v2 say what the "
        "reaction looks and the rule bot did (FACTS announcements_v2_today).\n"
        "- Trades are round trips after every cost (FACTS scorecard: trades, wins_after_fees, "
        "round_trips): never count a closing order as a trade.\n"
        "- Include the SCORECARD for each playbook, from FACTS scorecard, as its own short "
        "block: today's P&L after fees and the total for the agent and the bot, green/red "
        "days, the worst drop, how much of the profit is the best trade, and the prices per "
        "decision. If a playbook's `partial` is set, say the day is a PARTIAL DAY and give "
        "the reason word for word.\n"
        "- Keep it under 3000 characters. Plain text with simple HTML tags "
        "(<b>, <i>, <pre>) only.\n\n"
        f"FACTS (JSON):\n{json.dumps(payload, indent=2, default=str)}\n"
    )
