"""What happened today, read back from the arena's own records, for the Trader chat.

Rick asks the chat "how's it going today", "what did it trade", "why did it pass on NWL"
(25 Sep 2026: "i need to be able to just tell it things without commands"). The answers come
from the records the arena writes as it works - the account books, the event log (orders,
fills, decisions, screen verdicts, reaction looks), the v2 rule bot's state and the day
trader's signals - and from nothing else. No model is asked, nothing is guessed, and nothing
here writes: this is the read-only side of arena/tally.py and arena/report.py.

A question about one stock is answered as its story for the day, in time order: the news,
the screen, the reader, the decider, the reaction look, the rule bot, the day trader's
setups, the orders and fills, the pre-close sweep. A stock nothing mentions gets "nothing on
record", which is the honest answer, with what the record does say (in the day trader's
universe or not).
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot.arena.tally import Counts, _read, counts_for

SYD = ZoneInfo("Australia/Sydney")
MAX_FILL_LINES = 20
MAX_STORY_LINES = 40


# --------------------------------------------------------------------------- helpers


def _syd(value) -> datetime | None:
    """A Sydney datetime from an ISO stamp (event ts, a signal's `at`), or None."""
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=SYD)).astimezone(SYD)


def _px(v) -> str:
    """A price the way it was recorded, without trailing zeros: 17.8656, 0.3891, 4.48."""
    if v is None:
        return "?"
    try:
        s = f"{float(v):,.4f}"
    except (TypeError, ValueError):
        return str(v)
    return s.rstrip("0").rstrip(".") if "." in s else s


def _money(v) -> str:
    return f"${float(v):,.2f}"


def _signed(v) -> str:
    return f"{float(v):+,.2f}"


def _yes(v) -> bool:
    return str(v).strip().lower() in ("true", "yes", "1", "y")


def _one_line(text) -> str:
    return " ".join(str(text or "").split())


def _title(pb) -> str:
    """'ASX announcements v2 (trade the reaction)' -> 'ASX announcements v2'."""
    return re.sub(r"\s*\(.*?\)\s*", " ", str(pb.title)).strip()


def _who(placed_by: str, order_type: str = "") -> str:
    if order_type == "stop":
        return "STOP"
    if order_type == "target":
        return "TARGET"
    return {"agent": "AGENT", "bot": "BOT", "code": "CODE"}.get(placed_by, str(placed_by).upper())


def _placer(o: dict) -> str:
    """Who decided an order: the agent, a rule bot, or code (a pre-close sweep, a stop or a
    target on either account: `model` says 'code (...)' and placed_by names the account)."""
    if str(o.get("placed_by") or "code") == "code" or str(o.get("model") or "").startswith("code"):
        return "code"
    return "agent" if o.get("placed_by") == "agent" else "bot"


def _who_filled(r: dict) -> str:
    """Who a fill belongs to: the AGENT's or the BOT's decision, a STOP or TARGET exit, or
    the pre-close SWEEP (code closing a position on either account)."""
    ot = str(r.get("order_type") or "")
    if ot in ("stop", "target"):
        return ot.upper()
    model = str(r.get("model") or "")
    if "sweep" in model:
        return "SWEEP"
    if _placer(r) == "code":
        return "CODE"
    return _who(str(r.get("placed_by") or ""))


def _stop_text(v) -> str:
    return f", stop {_px(v)}" if v is not None else ""


# --------------------------------------------------------------------------- which day


def has_records(data_dir: Path, day: date) -> bool:
    """True if the arena recorded anything for `day`: a day file (cheap) or an event."""
    for folder in ("daytrader", "reaction", "v2bot"):
        if (Path(data_dir) / "arena" / folder / f"{day.isoformat()}.json").exists():
            return True
    for kind in ("arena_alerts", "arena_orders", "arena_decisions", "daytrader_setups",
                 "v2_reaction", "arena_fills"):  # fmt: skip
        if _read(data_dir, kind, day):
            return True
    return False


def pick_day(data_dir: Path, today: date, back: int = 7) -> tuple[date, str]:
    """The day a question about "today" is answered for: today if the arena has recorded
    anything today, otherwise the most recent day (within `back`) that it did. The label
    says which, so a Saturday's "how did it go" is answered for Friday and says so."""
    if has_records(data_dir, today):
        return today, "today"
    d = today
    for _ in range(back):
        d -= timedelta(days=1)
        if has_records(data_dir, d):
            return d, f"the last session on record, {d:%a %d %b}"
    return today, "today"


def named_day(text: str, today: date) -> date | None:
    """A day Rick named in his words: 'yesterday', 'on Tuesday', 'last Friday'; None if he
    did not name one (so the question is about today)."""
    t = text.lower()
    if re.search(r"\byesterday\b", t):
        d = today - timedelta(days=1)
        while d.weekday() > 4:  # over a weekend, "yesterday" means Friday
            d -= timedelta(days=1)
        return d
    m = re.search(r"\b(?:on |last )?(monday|tuesday|wednesday|thursday|friday|mon|tue|tues|"
                  r"wed|thu|thur|thurs|fri)\b", t)  # fmt: skip
    if m:
        wd = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4}[m.group(1)[:3]]
        d = today
        while d.weekday() != wd:
            d -= timedelta(days=1)
        if d == today:
            # "Friday" said on a Friday is today; "last Friday" is a week ago
            return d - timedelta(days=7) if re.search(r"\blast\b", t) else None
        return d
    return None


# --------------------------------------------------------------------------- codes


_codes_cache: dict[str, tuple[float, set[str]]] = {}


def known_codes(data_dir: Path) -> set[str]:
    """Every ASX code the chat can recognise in a sentence: the ASX directory and the ASX 200
    list (data/universe, cached per file change) plus anything in the account books."""
    out: set[str] = set()
    udir = Path(data_dir) / "universe"
    for name in ("asx_directory.csv", "asx200_members.csv"):
        p = udir / name
        try:
            stamp = p.stat().st_mtime
        except OSError:
            continue
        key = str(p)
        hit = _codes_cache.get(key)
        if hit and hit[0] == stamp:
            out |= hit[1]
            continue
        codes: set[str] = set()
        try:
            with open(p, encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    code = str(row.get("code") or "").strip().upper()
                    if code:
                        codes.add(code)
        except (OSError, csv.Error):
            continue
        _codes_cache[key] = (stamp, codes)
        out |= codes
    for p in (Path(data_dir) / "arena" / "accounts").glob("*.json"):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out |= {str(t).upper() for t in raw.get("positions") or {}}
        out |= {str(o.get("ticker")).upper() for o in (raw.get("orders") or {}).values()
                if o.get("ticker")}  # fmt: skip
    return out


# --------------------------------------------------------------------------- the day


@dataclass
class DayFacts:
    day: date
    label: str  # "today" or "the last session on record, Fri 25 Sep"
    counts: Counts
    reaction_by_status: dict = field(default_factory=dict)
    rule_bot: dict = field(default_factory=dict)
    daytrader: dict = field(default_factory=dict)
    orders: list = field(default_factory=list)  # submitted (broker) records
    refused: list = field(default_factory=list)
    fills: list = field(default_factory=list)
    accounts: list = field(default_factory=list)  # per playbook
    labels: dict = field(default_factory=dict)  # account name -> "ASX day trader bot"


def _labels(arena) -> dict[str, str]:
    out = {}
    for pb in arena.playbooks(only_enabled=False):
        for kind in ("agent", "bot"):
            name = pb.agent_account if kind == "agent" else pb.bot_account
            out[name] = f"{_title(pb)} {kind}"
    return out


def account_facts(arena, day: date, today: bool = True) -> list[dict]:
    """Every enabled playbook's two accounts: equity, cash, the day's move (against the
    day's starting equity, as the daily loss limit is), the running total, fees, positions
    and pending fills. Today is marked at the prices the broker can see now; a past day is
    read from that day's closing mark, and says so when there is none."""
    out = []
    for pb in arena.playbooks():
        row = {"key": pb.key, "title": _title(pb), "accounts": []}
        for kind in ("agent", "bot"):
            acct = arena.account(pb, kind)
            prices = arena.broker.prices(acct)
            equity = acct.equity(prices)
            start = arena.store.day_start_equity(acct, day)
            day_move: float | None = equity - start
            if not today:
                mark = next((m for m in arena.store.marks(acct.name)
                             if m.day == day.isoformat()), None)  # fmt: skip
                if mark is None:
                    day_move = None
                else:
                    equity, day_move = mark.equity, mark.equity - start
            positions = []
            for t, p in acct.positions.items():
                px = prices.get(t, p.avg_cost)
                positions.append({
                    "ticker": t, "qty": p.qty, "avg_cost": p.avg_cost, "last": px,
                    "open_pnl": (px - p.avg_cost) * p.qty, "stop": p.stop, "target": p.target,
                    "opened_at": p.opened_at, "by": p.opened_by,
                })  # fmt: skip
            row["accounts"].append({
                "kind": kind, "name": acct.name, "equity": equity, "cash": acct.cash,
                "today": day_move, "total": equity - acct.starting_cash,
                "fees": acct.fees_paid + acct.borrow_paid, "positions": positions,
                "pending": sum(1 for o in acct.orders.values() if o.status == "pending_fill"),
            })  # fmt: skip
        out.append(row)
    return out


def gather(arena, day: date, label: str = "today") -> DayFacts:
    """Every fact the chat may use about `day`. Read-only."""
    from asxbot.arena.daytrader import load_state
    from asxbot.arena.reaction_v2 import load_bot_state, load_queue

    data_dir = arena.cfg.data_dir
    queue = {k: v for k, v in load_queue(data_dir, day).items()
             if not k.startswith("_") and isinstance(v, dict)}  # fmt: skip
    by_status: dict[str, int] = {}
    for v in queue.values():
        s = str(v.get("status") or "?")
        by_status[s] = by_status.get(s, 0) + 1
    bot = load_bot_state(data_dir, day)
    dt = load_state(data_dir, day)
    dt_ran = (Path(data_dir) / "arena" / "daytrader" / f"{day.isoformat()}.json").exists()
    sigs = dt.get("signals") or []
    by_setup: dict[str, int] = {}
    for s in sigs:
        by_setup[str(s.get("setup"))] = by_setup.get(str(s.get("setup")), 0) + 1
    agent = [s.get("agent") or {} for s in sigs]
    orders = _read(data_dir, "arena_orders", day)
    return DayFacts(
        day=day, label=label, counts=counts_for(data_dir, day),
        reaction_by_status=by_status,
        rule_bot={"status": bot.get("status"), "why": bot.get("why"),
                  "orders": bot.get("orders") or [],
                  "signals": [c for c in bot.get("candidates") or [] if c.get("signal")]},
        daytrader={
            "ran": dt_ran,
            "universe": len(dt.get("universe") or []),
            "setups": len(sigs), "by_setup": by_setup,
            "agent_took": [s["ticker"] for s in sigs if (s.get("agent") or {}).get("order_id")],
            "agent_rejected": sum(1 for a in agent if "rejected" in a),
            "agent_not_asked": sum(1 for a in agent if a.get("skipped")),
            "bot_took": [s["ticker"] for s in sigs if (s.get("bot") or {}).get("order_id")],
            "stale": sum(1 for s in sigs if str(s.get("skipped") or "").startswith("stale")),
            "uneconomic": sum(
                1 for s in sigs
                if str(s.get("skipped") or "").startswith("uneconomic")
                or any(str((s.get(k) or {}).get("skipped") or "").startswith("uneconomic")
                       for k in ("bot", "agent"))),
        },
        orders=[r for r in orders if r.get("event") == "submitted"],
        refused=[r for r in orders if r.get("outcome") == "refused"],
        fills=_read(data_dir, "arena_fills", day),
        accounts=account_facts(arena, day, today=(label == "today")),
        labels=_labels(arena),
    )  # fmt: skip


# --------------------------------------------------------------------------- lines


def when_text(day: date, label: str) -> str:
    """'Fri 25 Sep, today' / 'Fri 25 Sep, the last session on record, Fri 25 Sep' /
    'Wed 24 Sep' (a day Rick named)."""
    if label == "today":
        return f"{day:%a %d %b}, today"
    return f"{day:%a %d %b}" + (f", {label}" if label else "")


def header(f: DayFacts) -> str:
    return f"{when_text(f.day, f.label)} - the arena, FAKE money."


def fill_time(r: dict) -> datetime | None:
    """When a fill happened in the market: its first bar (fill_minute), not the later minute
    the broker wrote it down (the evening resolve records a 15:50 fill at 16:14)."""
    return _syd(r.get("fill_minute")) or r.get("syd") or _syd(r.get("ts"))


def fill_line(r: dict, labels: dict) -> str:
    t = fill_time(r)
    when = f"{t:%H:%M} " if t else ""
    who = _who_filled(r)
    qty = int(r.get("filled_qty") or 0)
    ordered = abs(int(r.get("qty") or 0))
    amount = f"{qty:,}"
    if r.get("event") == "partial" or (ordered and qty < ordered):
        amount = f"{qty:,} of {ordered:,} (the rest did not fill)"
    line = (f"{when}{who} {r.get('side')} {amount} {r.get('ticker')} @ {_px(r.get('avg_price'))}"
            f" (fee {float(r.get('commission') or 0):.2f})")  # fmt: skip
    realised = float(r.get("realised") or 0)
    if realised:
        line += f", result {_signed(realised)} before fees"
    label = labels.get(str(r.get("account")), str(r.get("account")))
    return f"{line} [{label}]"


def fills_lines(f: DayFacts, limit: int = MAX_FILL_LINES) -> list[str]:
    fills = sorted(f.fills, key=lambda r: fill_time(r) or datetime.min.replace(tzinfo=SYD))
    out = [fill_line(r, f.labels) for r in fills[:limit]]
    if len(fills) > limit:
        out.append(f"... and {len(fills) - limit} more fills")
    return out


def accounts_lines(f: DayFacts) -> list[str]:
    out = []
    for row in f.accounts:
        parts = []
        for a in row["accounts"]:
            extra = ""
            if a["positions"]:
                extra = ", holding " + ", ".join(
                    f"{p['ticker']} {p['qty']:+,}" for p in a["positions"])
            if a["pending"]:
                extra += f", {a['pending']} fill pending"
            if a["today"] is None:
                move = "no mark for that day"
            else:
                move = f"{_signed(a['today'])} {'today' if f.label == 'today' else 'on the day'}"
            parts.append(f"{a['kind'].upper()} {_money(a['equity'])} ({move}, "
                         f"{_signed(a['total'])} since the start{extra})")  # fmt: skip
        out.append(f"{row['title']}: " + "; ".join(parts))
    return out or ["no playbook is enabled"]


def positions_lines(f: DayFacts) -> list[str]:
    out = []
    for row in f.accounts:
        for a in row["accounts"]:
            for p in a["positions"]:
                out.append(
                    f"{p['ticker']} {p['qty']:+,} @ {_px(p['avg_cost'])}, now {_px(p['last'])} "
                    f"({_signed(p['open_pnl'])}), stop {_px(p['stop'])}"
                    + (f", target {_px(p['target'])}" if p.get("target") is not None else "")
                    + f" [{row['title']} {a['kind']}]"
                )
    return out


def summary_lines(f: DayFacts, arena, watcher: str | None = None) -> list[str]:
    """'How's it going today': the day in a dozen plain lines, every number from a record."""
    c = f.counts
    lines = [header(f)]
    if watcher:
        lines.append(watcher)
    keys = {pb.key for pb in arena.playbooks()}
    if "asx_announcements_v2" in keys:
        split = ", ".join(f"{t} {n}" for t, n in sorted(c.by_test.items())) or "none"
        looks = ", ".join(f"{n} {s}" for s, n in sorted(f.reaction_by_status.items())) or "none"
        rb = f.rule_bot
        rb_orders = rb.get("orders") or []
        rb_text = (f"{len(rb_orders)} order{'s' if len(rb_orders) != 1 else ''}"
                   + (" (" + ", ".join(f"{o.get('ticker')} {o.get('side')} "
                                       f"{int(o.get('qty') or 0):,}" for o in rb_orders) + ")"
                      if rb_orders else ""))  # fmt: skip
        if not rb.get("status"):
            rb_text = "did not run"
        elif rb.get("status") != "done":
            rb_text = f"{rb.get('status')}" + (f" ({rb.get('why')})" if rb.get("why") else "")
        traded = c.to_decider - c.passed
        lines.append(
            f"Announcements v2: {c.seen} seen, {c.screened} screened out ({split}), {c.read} "
            f"read, {c.to_decider} reached the decider ({traded} traded, {c.passed} passed); "
            f"reaction looks: {looks}; 10:30 rule bot: {rb_text}."
        )
    if "asx_daytrader" in keys and not f.daytrader.get("ran"):
        lines.append("Day trader: no record for the day.")
    elif "asx_daytrader" in keys:
        d = f.daytrader
        by = ", ".join(f"{k} {v}" for k, v in d["by_setup"].items()) or "none"
        took = f"{len(d['agent_took'])}" + (f" ({', '.join(d['agent_took'])})" if d["agent_took"]
                                              else "")  # fmt: skip
        bot_took = f"{len(d['bot_took'])}" + (f" ({', '.join(d['bot_took'])})" if d["bot_took"]
                                                else "")  # fmt: skip
        lines.append(
            f"Day trader: {d['setups']} setups in {d['universe']} stocks ({by}); agent took "
            f"{took}, rejected {d['agent_rejected']}, not asked {d['agent_not_asked']}; rule bot "
            f"took {bot_took}; {d['stale']} stale by the time they were seen, {d['uneconomic']} "
            f"filtered as uneconomic."
        )
    by_who = {"agent": 0, "bot": 0, "code": 0}
    for o in f.orders:
        by_who[_placer(o)] += 1
    lines.append(
        f"Orders: {len(f.orders)} placed ({by_who['agent']} by the agent, {by_who['bot']} by "
        f"the rule bots, {by_who['code']} by code: sweeps, stops and targets), "
        f"{len(f.refused)} refused by the limits; {len(f.fills)} fills."
    )
    for r in f.refused[:5]:
        lines.append(f"  refused: {r.get('side')} {r.get('ticker')} - {_one_line(r.get('reason'))}")
    if f.fills:
        lines.append("Fills:")
        lines += ["  " + x for x in fills_lines(f)]
    lines.append("Accounts:")
    lines += ["  " + x for x in accounts_lines(f)]
    pos = positions_lines(f)
    lines.append("Open positions:" if pos else "Open positions: none.")
    lines += ["  " + x for x in pos]
    return lines


def trades_lines(f: DayFacts, arena) -> list[str]:
    """'What did it trade': every fill, then the orders that did not fill, then what the
    limits refused - with the recorded reason for each."""
    lines = [header(f)]
    if f.fills:
        lines.append(f"Fills ({len(f.fills)}):")
        lines += ["  " + x for x in fills_lines(f, limit=30)]
    else:
        lines.append("No fills.")
    unfilled = []
    for pb in arena.playbooks():
        for kind in ("agent", "bot"):
            acct = arena.account(pb, kind)
            for o in acct.orders.values():
                t = _syd(o.decided_at)
                if not t or t.date() != f.day or o.status in ("filled", "partial"):
                    continue
                unfilled.append((t, o, f"{_title(pb)} {kind}"))
    if unfilled:
        lines.append("Orders that did not fill:")
        for t, o, label in sorted(unfilled, key=lambda x: x[0]):
            lines.append(f"  {t:%H:%M} {o.order_id} {o.side} {abs(int(o.qty)):,} {o.ticker} @ "
                         f"{_px(o.limit)}: {o.status}"
                         + (f" - {_one_line(o.message)}" if o.message else "")
                         + f" [{label}]")  # fmt: skip
    if f.refused:
        lines.append(f"Refused by the limits ({len(f.refused)}):")
        for r in f.refused:
            t = r.get("syd")
            lines.append(f"  {t:%H:%M} " if t else "  ")
            lines[-1] += (f"{_who(str(r.get('placed_by') or ''))} {r.get('side')} "
                          f"{r.get('qty')} {r.get('ticker')} - {_one_line(r.get('reason'))}")
    if not f.fills and not unfilled and not f.refused:
        lines.append("No orders were placed and nothing was refused.")
    lines.append("Accounts:")
    lines += ["  " + x for x in accounts_lines(f)]
    return lines


def money_lines(f: DayFacts) -> list[str]:
    lines = [header(f), *accounts_lines(f)]
    pos = positions_lines(f)
    if pos:
        lines.append("Open positions:")
        lines += ["  " + x for x in pos]
    else:
        lines.append("Open positions: none.")
    return lines


# --------------------------------------------------------------------------- one stock


def _look(rec: dict) -> str:
    v2 = str(rec.get("v2") or "")
    if v2 == "pre_open":
        return " (pre-open look)"
    if v2 == "reaction":
        return " (reaction look)"
    if rec.get("relook"):
        return " (re-look)"
    return ""


def ticker_story(arena, day: date, ticker: str) -> list[str]:
    """One stock's day from the records, in time order. Empty if nothing names it."""
    from asxbot.arena.daytrader import load_state
    from asxbot.arena.reaction_v2 import load_bot_state

    data_dir = arena.cfg.data_dir
    code = ticker.upper()
    items: list[tuple[datetime, int, str]] = []
    n = 0

    def add(t: datetime | None, text: str) -> None:
        nonlocal n
        n += 1
        items.append((t or datetime.combine(day, datetime.min.time(), SYD), n, text))

    seen_news: set[str] = set()
    for r in _read(data_dir, "arena_alerts", day):
        if r.get("ticker") != code:
            continue
        seen_news.add(str(r.get("ids_id")))
        ps = " (price-sensitive)" if r.get("price_sensitive") else ""
        add(_syd(r.get("released_at")) or r["syd"], f"news: {_one_line(r.get('headline'))}{ps}")
    for r in _read(data_dir, "arena_screened", day):
        if r.get("ticker") != code:
            continue
        if str(r.get("ids_id")) not in seen_news and r.get("headline"):
            seen_news.add(str(r.get("ids_id")))
            add(r["syd"], f"news: {_one_line(r.get('headline'))}")
        if r.get("ok"):
            add(r["syd"], f"screen: {_one_line(r.get('why'))}")
        else:
            add(r["syd"], f"screened out ({r.get('test')}): {_one_line(r.get('why'))}")
    for r in _read(data_dir, "arena_decisions", day):
        if r.get("ticker") != code:
            continue
        stage = r.get("stage")
        if stage in ("reader", "relook") and "trade_worthy" in r:
            tw = "YES" if _yes(r.get("trade_worthy")) else "NO"
            cs = "YES" if _yes(r.get("can_size_and_exit")) else "NO"
            add(r["syd"], f"reader: trade-worthy {tw} ({_one_line(r.get('why'))}); can size and "
                          f"exit {cs} ({_one_line(r.get('can_size_why'))})")  # fmt: skip
        elif stage == "decider":
            if not r.get("decision"):  # no decision: paused on the feed, or the call failed
                what = ("not asked - paused" if r.get("outcome") == "paused"
                        else str(r.get("outcome") or "no decision"))  # fmt: skip
                add(r["syd"], f"decider{_look(r)}: {what}, {_one_line(r.get('why'))}")
                continue
            d = r.get("decision") or {}
            action = str(d.get("action") or "pass").lower()
            if action == "trade":
                conf = f", confidence {d.get('confidence_pct')}%" if d.get("confidence_pct") else ""
                add(r["syd"], f"decider{_look(r)}: TRADE {d.get('side')} {d.get('qty')} @ "
                              f"{_px(d.get('limit'))}, stop {_px(d.get('stop'))}{conf} - "
                              f"{_one_line(d.get('why'))}")  # fmt: skip
            else:
                add(r["syd"], f"decider{_look(r)}: {action.upper()} - {_one_line(d.get('why'))}")
            if d.get("flag_for_claude"):
                add(r["syd"], f"  (the decider flagged for Claude: "
                              f"{_one_line(d.get('flag_for_claude'))})")  # fmt: skip
        elif stage == "preclose":
            add(r["syd"], f"pre-close sweep: {r.get('action')} - {_one_line(r.get('reason'))}")
    last_status = None
    for r in _read(data_dir, "v2_reaction", day):
        if r.get("ticker") != code:
            continue
        status = str(r.get("status") or "?")
        line = f"reaction look: {status}"
        out = r.get("outcome") or {}
        rx = r.get("reaction") or {}
        if out.get("decision"):
            line += (f" - decider {str(out.get('decision')).upper()}: "
                     f"{_one_line(out.get('why'))}")  # fmt: skip
            if rx.get("available"):
                line += (f" [move {rx.get('move_pct')}% vs index {rx.get('index_move_pct')}%, "
                         f"volume {rx.get('volume_vs_usual_same_minutes')}x usual]")
        elif r.get("why"):
            line += f" - {_one_line(r.get('why'))}"
        if line != last_status:
            add(r["syd"], line)
            last_status = line
    bot = load_bot_state(data_dir, day)  # the rule bot's final state for the day
    at = _syd(bot.get("decided_at"))
    for c in bot.get("candidates") or []:
        if c.get("ticker") != code:
            continue
        if c.get("screened"):
            add(at, f"10:30 rule bot: screened out ({c.get('screened')})")
        elif c.get("signal"):
            hit = next((o for o in bot.get("orders") or [] if o.get("ticker") == code), None)
            extra = (f"; ordered {hit.get('order_id')} {hit.get('side')} "
                     f"{int(hit.get('qty') or 0):,} @ {_px(hit.get('limit'))}, stop "
                     f"{_px(hit.get('stop'))}" if hit else "")  # fmt: skip
            add(at, f"10:30 rule bot: signal - {_one_line(c.get('why'))}{extra}")
        else:
            add(at, f"10:30 rule bot: no signal - {_one_line(c.get('why'))}")
    dt = load_state(data_dir, day)
    for s in dt.get("signals") or []:
        if s.get("ticker") != code:
            continue
        t = _syd(s.get("at"))
        line = f"day trader setup, {s.get('setup')} {s.get('side')}: {_one_line(s.get('why'))}"
        if s.get("skipped"):
            line += f"; not offered - {_one_line(s.get('skipped'))}"
        b = s.get("bot") or {}
        if b.get("order_id"):
            line += (f"; rule bot ordered {b['order_id']}, {int(b.get('qty') or 0):,} @ "
                     f"{_px(b.get('limit'))}, stop {_px(b.get('stop'))}")  # fmt: skip
        elif b.get("skipped"):
            line += f"; rule bot skipped - {_one_line(b.get('skipped'))}"
        a = s.get("agent") or {}
        if a.get("order_id"):
            line += (f"; agent took it, {a['order_id']}, {int(a.get('qty') or 0):,} @ "
                     f"{_px(a.get('limit'))}, stop {_px(a.get('stop'))}")  # fmt: skip
        elif a.get("rejected"):
            line += f"; agent REJECTED - {_one_line(a.get('rejected'))}"
        elif a.get("skipped"):
            line += f"; agent not asked - {_one_line(a.get('skipped'))}"
        add(t, line)
    labels = _labels(arena)
    for r in _read(data_dir, "arena_orders", day):
        if r.get("ticker") != code:
            continue
        label = labels.get(str(r.get("account")), str(r.get("account") or ""))
        ev = r.get("event")
        if r.get("outcome") == "refused":
            add(r["syd"], f"REFUSED by the limits: {_who(str(r.get('placed_by') or ''))} "
                          f"{r.get('side')} {r.get('qty')} - {_one_line(r.get('reason'))} "
                          f"[{label}]")  # fmt: skip
        elif ev == "submitted":
            who = _who(str(r.get("placed_by") or ""))
            model = str(r.get("model") or "")
            if _placer(r) == "code":  # "code (pre-close sweep: flat)" -> CODE (pre-close ...)
                who = "CODE" + (model[4:] if model.startswith("code (") else "")
            add(r["syd"], f"order {r.get('order_id')}: {who} {r.get('side')} "
                          f"{abs(int(r.get('qty') or 0)):,} @ {_px(r.get('limit'))}"
                          f"{_stop_text(r.get('stop'))} - {_one_line(r.get('reason'))} "
                          f"[{label}]")  # fmt: skip
        elif ev == "stop_moved":
            add(_syd(r.get("bar")) or r["syd"],
                f"stop moved {_px(r.get('from'))} -> {_px(r.get('to'))} [{label}]")
        elif ev in ("stop_reached", "target_reached", "half_reached"):
            add(_syd(r.get("bar")) or _syd(r.get("data_as_of")) or r["syd"],
                f"{ev.replace('_', ' ')}: {_one_line(r.get('message'))} [{label}]")
        elif ev == "cancelled":
            add(r["syd"], f"order {r.get('order_id')} cancelled: {_one_line(r.get('message'))} "
                          f"[{label}]")  # fmt: skip
    for r in _read(data_dir, "arena_fills", day):
        if r.get("ticker") == code:
            add(fill_time(r), "filled: " + fill_line(r, labels).split(" ", 1)[1])
    items.sort(key=lambda x: (x[0], x[1]))
    lines = [f"{t:%H:%M} {text}" if not text.startswith("  ") else text for t, _, text in items]
    if len(lines) > MAX_STORY_LINES:
        lines = lines[:MAX_STORY_LINES] + [f"... and {len(lines) - MAX_STORY_LINES} more lines"]
    return lines


def ticker_text(arena, day: date, ticker: str, label: str = "today") -> str:
    """The answer to 'why did it pass on NWL' - or the honest 'nothing on record'."""
    from asxbot.arena.daytrader import load_state

    code = ticker.upper()
    when = when_text(day, label)
    lines = ticker_story(arena, day, code)
    if lines:
        return f"{code} on {when}, from the arena's records:\n" + "\n".join(lines)
    universe = set(load_state(arena.cfg.data_dir, day).get("universe") or [])
    if code in universe:
        scan = "the day trader scanned it and found no setup"
    elif universe:
        scan = "it is not in the day trader's universe"
    else:
        scan = "the day trader did not scan that day"
    return (f"Nothing on record for {code} on {when}: no announcement of its reached the "
            f"arena (so the screen, the reader and the decider never saw it), {scan}, and no "
            f"order names it.")  # fmt: skip


# --------------------------------------------------------------------------- for the agent


def facts_for_agent(arena, day: date, label: str, tickers: list[str],
                    watcher: str | None = None) -> str:  # fmt: skip
    """The block the decider is given with a question about the day's trading, so that its
    answer rests on the records and not on its memory of the day."""
    f = gather(arena, day, label)
    lines = summary_lines(f, arena, watcher)
    for code in tickers[:4]:
        story = ticker_story(arena, day, code)
        lines.append("")
        lines.append(f"{code.upper()}:")
        lines += ["  " + x for x in story] if story else ["  nothing on record"]
    return "\n".join(lines)
