"""The end-of-day trading journal (Rick, 26 Sep 2026: "do the traders know to write down what
works etc").

After the close each trading day, one file per book per day:
`data/arena/journal/<yyyy-mm-dd>/<account>.md`.

- The AGENT's books (the day trader's, announcements v2's): plain code gathers the day's facts
  - every setup or look the agent was asked about, what it decided and why (its own recorded
  words), how each trade played out (entry, exit, R, fees, net), and what the price did after
  each setup - and the agent writes its journal from them in ONE model call: what it took or
  skipped and why, what worked, what didn't, its mistakes, what it would do differently, and
  one LESSON line.
- The RULE BOTS' books: the same facts from plain code, no model call.

FROZEN TEST RULE. The journal is written only. During the 10-day test nothing that trades
reads it: not the watcher's packets, not the agents' standing instructions or OpenClaw
workspace, not the evening report's agent brief (the report's three journal lines are added by
code after the agent has written its part). tests/test_journal.py holds the trading modules to
that. Every file says so in its header.

Why the call is not made through OpenClaw. The trader agents run in OpenClaw with the "coding"
tools and a workspace whose files later calls load: a journal written there could land in the
decider's memory, and so in its next decision. So the journal call runs the decider's model
directly (`claude -p`, as the Practice Lab does): the decider's model and effort from
config.yaml, its standing instructions (its AGENTS.md) as the system prompt, no tools, no
session kept, no settings or MCP servers, in an empty scratch folder. Nothing persists but the
text it returns, which code writes here.

One call per agent per day. The call is recorded in events/arena_journal_calls.jsonl before it
is made ("started") and after ("done": model, effort, tokens, the list-price cost, seconds, the
plan's weekly meter). A book with any record that day is not asked again unless `again` (the
CLI's --again, which is logged the same way). A book the agent made no decision in gets a
code-written entry and no call: there is nothing to reflect on.

The Practice Lab reads the journals with `load()`: real lessons from forward days. A day inside
WINNER.md's sealed LOCKED TEST window says so in its header and its data.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time as _time
from datetime import date, datetime, time, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot import proc
from asxbot.arena.minutes import first_minute_after
from asxbot.log import EventLog, get_logger

log = get_logger("asxbot.arena.journal")
SYD = ZoneInfo("Australia/Sydney")

CALLS = "arena_journal_calls"  # the event log of journal calls (not arena_agent_calls: these
# are not trading decisions, and a failed one must not read as "the agent could not be asked")
FLAT_AT = time(15, 50)  # both live playbooks are flat by code at 15:50
LESSON_MAX = 220
WHY_MAX = 320
# WINNER.md (written 26 Sep 2026, before the Practice Lab's search; its numbers never change):
# the sealed LOCKED TEST window. A journal of a day inside it must not tune or screen a variant.
LOCKED_TEST = (date(2026, 8, 17), date(2026, 9, 25))
SHORT = {"asx_daytrader": "Day trader", "asx_announcements_v2": "Announcements v2"}
OPENING = ("buy", "short")
DATA_FENCE = "```json journal-data"


def _clip(text, n: int = WHY_MAX) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 3].rstrip() + "..."


# --------------------------------------------------------------------------
# where
# --------------------------------------------------------------------------
def journal_dir(data_dir, day: date) -> Path:
    return Path(data_dir) / "arena" / "journal" / day.isoformat()


def journal_path(data_dir, day: date, account: str) -> Path:
    return journal_dir(data_dir, day) / f"{account}.md"


def short_name(pb) -> str:
    return SHORT.get(pb.key, pb.title)


def test_window(pb) -> tuple[date, date] | None:
    """The playbook's test: its first day and its last trading day (config test_start,
    test_days), or None when it has no test."""
    from asxbot.announcements.live import is_trading_day

    start = pb.raw.get("test_start")
    if not start:
        return None
    first = date.fromisoformat(str(start))
    n, k, d, last = int(pb.raw.get("test_days", 10)), 0, first, first
    while k < n:
        if d.weekday() < 5 and is_trading_day(d):
            k, last = k + 1, d
        d += timedelta(days=1)
    return first, last


def frozen_rule(pb) -> str:
    w = test_window(pb)
    span = f" ({w[0]:%a %d %b} - {w[1]:%a %d %b %Y})" if w else ""
    n = int(pb.raw.get("test_days", 10))
    return (
        f"FROZEN TEST RULE: this journal is written only. During the {n}-day test{span} it is "
        "never fed back into the agents' prompts or decisions - not the watcher's packets, not "
        "the agents' standing instructions or memory, not the evening report's agent brief - "
        "so the test stays fair. It is a record for Rick and for the Practice Lab."
    )


def lab_note(day: date) -> str:
    if LOCKED_TEST[0] <= day <= LOCKED_TEST[1]:
        return (f"Practice Lab: {day:%d %b %Y} is inside WINNER.md's sealed LOCKED TEST window "
                f"({LOCKED_TEST[0]:%d %b} - {LOCKED_TEST[1]:%d %b %Y}): never use this day to "
                "tune or screen a variant.")  # fmt: skip
    return "Practice Lab: a forward day after the models' knowledge cutoff - a real lesson."


# --------------------------------------------------------------------------
# what the price did (plain code, hindsight, labelled as such)
# --------------------------------------------------------------------------
def _traded(df, day: date, start: datetime, need_volume: bool = True):
    """The bars from `start` to before 15:50; a stock's only where it traded (a price nobody
    dealt at touches nothing). The index carries no volume, so it keeps every bar."""
    end = datetime.combine(day, FLAT_AT, tzinfo=SYD)
    w = df[(df.index >= start) & (df.index < end)]
    return w[w["volume"] > 0] if need_volume and "volume" in w else w


def _bars(minutes, code: str, day: date):
    try:
        df = minutes.cached(code, day)
    except Exception:  # noqa: BLE001 - a missing or unreadable cache is "no bars", said so
        return None
    return df if df is not None and len(df) else None


def price_path(df, day: date, start: datetime, side: str, ref: float, stop) -> dict | None:
    """What the price did from `start` to 15:50 for a setup entered at `ref` with `stop`:
    the first minute the stop was touched, the first minutes +1R and +2R were reached, and
    where it stood at 15:50 in R. The plain price path before costs, with no trade management
    (breakeven, half off, trailing); a bar that touched both the stop and +1R counts the stop
    first. None when there are no bars or no usable stop."""
    if df is None or stop is None or not ref:
        return None
    sgn = 1 if side in ("buy", "long") else -1
    risk = sgn * (float(ref) - float(stop))
    if risk <= 0:
        return None
    w = _traded(df, day, start)
    if not len(w):
        return None
    hit = r1 = r2 = None
    fav = adv = 0.0
    for ts, bar in w.iterrows():
        hi, lo = float(bar["high"]), float(bar["low"])
        f = sgn * ((hi if sgn > 0 else lo) - ref) / risk
        a = sgn * ((lo if sgn > 0 else hi) - ref) / risk
        fav, adv = max(fav, f), min(adv, a)
        m = f"{ts:%H:%M}"
        if hit is None and a <= -1.0:
            hit = m
        if r1 is None and f >= 1.0:
            r1 = m
        if r2 is None and f >= 2.0:
            r2 = m
    end = float(w["close"].iloc[-1])
    end_r = sgn * (end - ref) / risk
    if hit and (r1 is None or hit <= r1):
        first = "stop"
    elif r1:
        first = "+1R"
    else:
        first = "neither"
    parts = []
    if first == "stop":
        parts.append(f"stop touched {hit} before +1R")
    elif first == "+1R":
        parts.append(f"+1R at {r1} before the stop" + (f", +2R {r2}" if r2 else ""))
        if hit:
            parts.append(f"stop touched later ({hit})")
    else:
        parts.append("neither the stop nor +1R by 15:50")
    parts.append(f"at 15:50 {end_r:+.1f}R")
    return {
        "from": f"{w.index[0]:%H:%M}", "first": first, "stop_touched": hit,
        "plus_1r": r1, "plus_2r": r2, "r_at_1550": round(end_r, 2),
        "best_r": round(fav, 2), "worst_r": round(adv, 2),
        "line": "; ".join(parts) + " (price path before costs, no trade management)",
    }  # fmt: skip


def move_after(df, day: date, start: datetime, need_volume: bool = True) -> dict | None:
    """The move from the first traded minute at or after `start` to 15:50, in %: the close,
    the highest and the lowest point on the way."""
    if df is None:
        return None
    w = _traded(df, day, start, need_volume)
    if not len(w):
        return None
    ref = float(w["close"].iloc[0])
    if not ref:
        return None

    def pct(x):
        return round((float(x) - ref) / ref * 100, 2)

    return {"from": f"{w.index[0]:%H:%M}", "ref": ref, "to_1550_pct": pct(w["close"].iloc[-1]),
            "high_pct": pct(w["high"].max()), "low_pct": pct(w["low"].min())}  # fmt: skip


def _index_move(idx, day: date, start: datetime) -> dict | None:
    """The ASX 200 over the same minutes as a stock's move (no volume on an index bar)."""
    return move_after(idx, day, start, need_volume=False)


def _move_line(m: dict | None, idx: dict | None) -> str:
    if not m:
        return "no bars after it"
    s = (f"from {m['from']} ({m['ref']:g}) to 15:50 {m['to_1550_pct']:+.2f}% "
         f"(high {m['high_pct']:+.2f}%, low {m['low_pct']:+.2f}%)")  # fmt: skip
    if idx:
        s += f"; ASX 200 {idx['to_1550_pct']:+.2f}% over the same minutes"
    return s


# --------------------------------------------------------------------------
# the book's trades (plain code: trades, outcome, R, fees)
# --------------------------------------------------------------------------
def _hhmm(s) -> str:
    try:
        return f"{datetime.fromisoformat(str(s)).astimezone(SYD):%H:%M}"
    except (TypeError, ValueError):
        return str(s or "")[11:16]


def _stop_moves(data_dir, day: date, account: str, ticker: str) -> list[str]:
    from asxbot.arena.tally import _read

    out = []
    for r in _read(data_dir, "arena_orders", day):
        if (r.get("event") == "stop_moved" and r.get("account") == account
                and r.get("ticker") == ticker):  # fmt: skip
            out.append(f"code moved the stop {r.get('from')} -> {float(r.get('to')):.4g} at "
                       f"{_hhmm(r.get('bar'))}, +{float(r.get('r_gained') or 0):.2f}R in hand "
                       "(trade management by code, not the agent)")  # fmt: skip
    return out


def _slippage(orders) -> tuple[float, bool]:
    """Dollars the fills gave up to slippage (the broker's adverse slippage on every fill,
    inside the fill prices): each slice's |price - bar price| x shares. (total, known): known
    is False when a slice has no bar price on record (filled before 24 Sep)."""
    total, known = 0.0, True
    for o in orders:
        for f in o.fills or []:
            if f.get("bar_price") is None:
                known = False
                continue
            total += abs(float(f["price"]) - float(f["bar_price"])) * abs(int(f["qty"]))
    return total, known


def book_trades(acct, day: date, data_dir, limit_by: str = "") -> list[dict]:
    """Every round trip opened or closed on `day`: entry, exits, the initial stop, R and
    every cost. R is the net result over the initial risk (entry to the entry order's stop,
    times the shares). `limit_by` says who set the entry's limit and size."""
    from asxbot.arena.scoreboard import round_trips

    iso = day.isoformat()
    out = []
    for t in round_trips(acct):
        if (t.opened or "")[:10] != iso and (t.closed or "")[:10] != iso:
            continue
        orders = [acct.orders[o] for o in t.orders if o in acct.orders]
        entry = next((o for o in orders if o.side in OPENING), None)
        exits = [o for o in orders if o.side not in OPENING and o.filled_qty]
        avg_in = t.entry_value / t.qty if t.qty else None
        got = sum(int(o.filled_qty) for o in exits)
        avg_out = (sum(float(o.avg_price or 0) * int(o.filled_qty) for o in exits) / got
                   if got else None)  # fmt: skip
        stop = getattr(entry, "stop", None) if entry else None
        risk = None
        if stop is not None and avg_in:
            sgn = 1 if t.side == "long" else -1
            per = sgn * (avg_in - float(stop))
            risk = per * t.qty if per > 0 else None
        fees = t.entry_fees + t.exit_fees
        slip, slip_known = _slippage(orders)
        out.append({
            "ticker": t.ticker, "side": t.side, "qty": t.qty,
            "opened": _hhmm(t.opened), "closed": _hhmm(t.closed) if t.closed else "",
            "still_open": not t.is_closed,
            "entry": round(avg_in, 4) if avg_in else None,
            "exit": round(avg_out, 4) if avg_out else None,
            "initial_stop": stop, "risk_aud": round(risk, 2) if risk else None,
            "gross": round(t.gross, 2), "fees": round(fees, 2), "borrow": round(t.borrow, 2),
            "slippage": round(slip, 2), "slippage_known": slip_known,
            "costs_all_in": round(fees + slip + t.borrow, 2),
            "net": round(t.net, 2),
            "r_net": round(t.net / risk, 2) if risk else None,
            "r_gross": round(t.gross / risk, 2) if risk else None,
            "entry_order": entry.order_id if entry else None,
            "entry_limit": entry.limit if entry else None, "limit_by": limit_by,
            "decided": _hhmm(entry.decided_at) if entry else "",
            "entry_reason": _clip(entry.reason if entry else ""),
            "model": entry.model if entry else "",
            "exits": [f"{o.order_id} {o.side} {o.filled_qty} @ {o.avg_price} at "
                      f"{_hhmm(o.fill_minute)}: {o.reason[:90]}" for o in exits],
            "management": _stop_moves(data_dir, day, acct.name, t.ticker),
        })  # fmt: skip
    return out


def book_result(store, acct, day: date, trades: list[dict]) -> dict:
    """Today's result: the day's mark against the day before (after every cost) when the
    evening has marked the book, else the closed trades' net; fees; wins after costs."""
    closed = [t for t in trades if not t["still_open"]]
    out = {
        "round_trips_closed": len(closed),
        "wins_after_fees": sum(1 for t in closed if t["net"] > 0),
        "net_closed": round(sum(t["net"] for t in closed), 2),
        "fees": round(sum(t["fees"] for t in trades), 2),
        "still_open": [t["ticker"] for t in trades if t["still_open"]],
        "today_after_fees": None,
    }
    try:
        mark = next((m for m in store.marks(acct.name) if m.day == day.isoformat()), None)
        if mark is not None:
            out["today_after_fees"] = round(mark.equity - store.day_start_equity(acct, day), 2)
    except Exception:  # noqa: BLE001 - a mark that cannot be read leaves the trades' net
        pass
    return out


# --------------------------------------------------------------------------
# the day trader's setups
# --------------------------------------------------------------------------
def _unavailable(a: dict) -> bool:
    from asxbot.arena.report import unavailable_text

    return bool(a.get("unavailable")) or unavailable_text(a.get("rejected"))


def _agent_why(acct, oid: str) -> str:
    o = acct.orders.get(oid)
    why = o.reason if o is not None else ""
    return _clip(re.sub(r"^\[[a-z_]+\]\s*agent confirmed:\s*", "", why))


def daytrader_setups(data_dir, day: date, kind: str, acct, minutes) -> dict:
    """Every setup the scan found, from this book's side: taken or skipped by it, and why;
    the rest counted by why nobody decided them (stale, uneconomic, the agent unavailable)."""
    from asxbot.arena.daytrader import load_state

    decided, not_asked = [], {}
    for x in load_state(data_dir, day).get("signals", []):
        a, b = x.get("agent") or {}, x.get("bot") or {}
        item = {"at": _hhmm(x.get("at")), "ticker": x.get("ticker"), "setup": x.get("setup"),
                "side": x.get("side"), "last": x.get("last"),
                "stop": round(float(x["stop"]), 4) if x.get("stop") is not None else None,
                "rvol": round(float(x.get("rvol") or 0), 1),
                "scanner": _clip(x.get("why"), 200)}  # fmt: skip
        seconds = 0.0
        if kind == "agent":
            if a.get("order_id"):
                item |= {"decision": "took", "why": _agent_why(acct, a["order_id"]),
                         "order": a["order_id"]}  # fmt: skip
                seconds = float(a.get("seconds") or 0)
            elif _unavailable(a):
                reason = f"agent unavailable ({a.get('unavailable') or 'no answer'})"
                not_asked[reason] = not_asked.get(reason, 0) + 1
                continue
            elif "rejected" in a:
                item |= {"decision": "skipped", "why": _clip(a["rejected"])}
                seconds = float(a.get("seconds") or 0)
            else:
                reason = str(x.get("skipped") or a.get("skipped") or b.get("skipped")
                             or "not offered")  # fmt: skip
                reason = re.sub(r"\d+(\.\d+)? (bars|minutes)", "N \\2", reason)[:80]
                not_asked[reason] = not_asked.get(reason, 0) + 1
                continue
        else:
            if b.get("order_id"):
                item |= {"decision": "took", "why": "the rule took it", "order": b["order_id"]}
            elif b.get("skipped"):
                item |= {"decision": "skipped", "why": _clip(b["skipped"])}
            else:
                reason = re.sub(r"\d+(\.\d+)? (bars|minutes)", "N \\2",
                                str(x.get("skipped") or "not offered"))[:80]  # fmt: skip
                not_asked[reason] = not_asked.get(reason, 0) + 1
                continue
        try:
            at = datetime.fromisoformat(str(x.get("at"))).astimezone(SYD)
            start = first_minute_after(at + timedelta(seconds=seconds))
            item["after"] = price_path(_bars(minutes, str(x.get("ticker")), day), day, start,
                                       str(x.get("side")), float(x.get("last") or 0),
                                       x.get("stop"))  # fmt: skip
        except (TypeError, ValueError):
            item["after"] = None
        decided.append(item)
    return {"setups_decided": decided, "setups_not_decided": not_asked}


# --------------------------------------------------------------------------
# announcements v2's looks
# --------------------------------------------------------------------------
def _decision(d) -> dict:
    if isinstance(d, dict):
        return d
    if isinstance(d, str):
        import ast

        try:
            v = ast.literal_eval(d)
            return v if isinstance(v, dict) else {}
        except (ValueError, SyntaxError):
            return {}
    return {}


def v2_looks(data_dir, day: date, minutes) -> dict:
    """The agent's side of announcements v2: its pre-open looks and its reaction looks, what
    it decided and why, and each stock's move after; stocks never looked at, with why."""
    from asxbot.arena.reaction_v2 import load_queue
    from asxbot.arena.report import UNAVAILABLE_OUTCOMES, _look_unavailable
    from asxbot.arena.tally import _read

    q = {k: v for k, v in load_queue(data_dir, day).items() if not k.startswith("_")}
    idx = _bars(minutes, "^AXJO", day)
    open_at = datetime.combine(day, time(10, 0), tzinfo=SYD)
    pre_by: dict = {}  # one announcement worked twice (a watcher restart): the later record wins
    looks, never = [], []
    for r in _read(data_dir, "arena_decisions", day):
        if r.get("v2") != "pre_open" or r.get("stage") != "decider":
            continue
        tk = str(r.get("ticker"))
        key = r.get("ids_id") or tk
        if r.get("outcome") in UNAVAILABLE_OUTCOMES:
            pre_by[key] = {"at": f"{r['syd']:%H:%M}", "ticker": tk,
                           "decision": "agent unavailable"}  # fmt: skip
            continue
        d = _decision(r.get("decision"))
        df = _bars(minutes, tk, day)
        pre_by[key] = ({
            "at": f"{r['syd']:%H:%M}", "ticker": tk,
            "headlines": (q.get(tk) or {}).get("headlines", [])[:3],
            "decision": str(d.get("action", "pass")), "side": d.get("side"),
            "why": _clip(d.get("why", "")),
            "after": _move_line(move_after(df, day, open_at), _index_move(idx, day, open_at)),
        })  # fmt: skip
    pre = sorted(pre_by.values(), key=lambda p: p["at"])
    for tk, v in q.items():
        status = str(v.get("status", "?"))
        react = v.get("reaction") or {}
        if status == "looked" and not _look_unavailable(v):
            out = v.get("outcome") or {}
            bar = str(react.get("as_of_bar") or "10:10")
            try:
                hh, mm = (int(p) for p in bar.split(":")[:2])
                start = datetime.combine(day, time(hh, mm), tzinfo=SYD) + timedelta(minutes=1)
            except ValueError:
                start = datetime.combine(day, time(10, 10), tzinfo=SYD)
            looks.append({
                "ticker": tk, "headlines": v.get("headlines", [])[:3],
                "decision": str(out.get("decision") or out.get("action") or "?"),
                "why": _clip(out.get("why")),
                "seen": {k: react.get(k) for k in (
                    "as_of_bar", "move_vs_index_pct", "volume_vs_usual_same_minutes",
                    "data_label") if react.get(k) is not None},
                "after": _move_line(move_after(_bars(minutes, tk, day), day, start),
                                    _index_move(idx, day, start)),
            })  # fmt: skip
        else:
            why = ("agent unavailable" if status == "looked"
                   else str(v.get("why") or react.get("why") or status))  # fmt: skip
            start = datetime.combine(day, time(10, 10), tzinfo=SYD)
            never.append({
                "ticker": tk, "status": status, "why": why[:160],
                "headlines": v.get("headlines", [])[:2],
                "after_10_10": _move_line(move_after(_bars(minutes, tk, day), day, start),
                                          _index_move(idx, day, start)),
            })  # fmt: skip
    return {"pre_open_looks": pre, "reaction_looks": looks, "never_looked": never}


def v2_rule_bot(data_dir, day: date) -> dict:
    from asxbot.arena.reaction_v2 import load_bot_state

    b = load_bot_state(data_dir, day)
    cands = b.get("candidates", [])
    return {
        "status": b.get("status"), "why": b.get("why"), "decided_at": _hhmm(b.get("decided_at")),
        "data": b.get("data"),
        "signals": [{"ticker": c.get("ticker"), "why": c.get("why")} for c in cands
                    if c.get("signal")],
        "no_signal": [{"ticker": c.get("ticker"), "why": c.get("why")} for c in cands
                      if "signal" in c and not c.get("signal")],
        "screened_out": sum(1 for c in cands if "screened" in c),
    }  # fmt: skip


# --------------------------------------------------------------------------
# one book's facts
# --------------------------------------------------------------------------
def limit_setter(pb, kind: str) -> str:
    """Who set a book's entry limit, size and stop - so a journal never takes the credit or
    the blame for code's work (25 Sep: the agent called the day trader's 1% limit its error)."""
    if kind == "bot":
        return "set by the frozen rule, as was the size"
    if pb.key == "asx_daytrader":
        slack = (pb.raw.get("entry") or {}).get("limit_slack_pct", 1.0)
        return (f"set by code: the frozen rule's {slack}% through the last price, as were the "
                "size and the stop; the agent only confirmed")
    return "the agent's own limit, size and stop, within the limits"


def book_facts(cfg, arena, pb, kind: str, day: date, minutes=None) -> dict:
    from asxbot.arena.minutes import MinuteBars
    from asxbot.arena.report import partial_day, test_day

    minutes = minutes or MinuteBars(cfg.data_dir)
    acct = arena.account(pb, kind)
    trades = book_trades(acct, day, cfg.data_dir, limit_setter(pb, kind))
    f = {
        "day": day.isoformat(), "weekday": f"{day:%a}", "account": acct.name,
        "playbook": pb.key, "title": pb.title, "kind": kind, "test": test_day(pb, day),
        "partial": partial_day(pb, day), "trades": trades,
        "result": book_result(arena.store, acct, day, trades),
    }  # fmt: skip
    if pb.key == "asx_daytrader":
        f |= daytrader_setups(cfg.data_dir, day, kind, acct, minutes)
        f["decisions"] = len(f["setups_decided"])
    elif pb.key.startswith("asx_announcements_v2"):
        if kind == "agent":
            f |= v2_looks(cfg.data_dir, day, minutes)
            f["decisions"] = (
                sum(1 for p in f["pre_open_looks"] if p["decision"] != "agent unavailable")
                + len(f["reaction_looks"])
            )
        else:
            f["rule"] = v2_rule_bot(cfg.data_dir, day)
            f["decisions"] = len(f["rule"]["signals"]) + len(f["rule"]["no_signal"])
    else:
        f["decisions"] = len(trades)
    if kind == "agent":
        f["decisions"] = max(f["decisions"], len(trades))
        bot = arena.account(pb, "bot")
        bt = book_trades(bot, day, cfg.data_dir, limit_setter(pb, "bot"))
        keep = ("ticker", "side", "opened", "closed", "entry", "exit", "net", "r_net",
                "entry_reason")
        f["yardstick"] = {"account": bot.name, "result": book_result(arena.store, bot, day, bt),
                          "trades": [{k: t[k] for k in keep} for t in bt]}  # fmt: skip
    return f


# --------------------------------------------------------------------------
# the agent's entry: the brief, and the one call
# --------------------------------------------------------------------------
def agent_brief(facts: dict, pb) -> str:
    """What the agent is asked to write, and the day's records it writes from."""
    return (
        f"Write your trading journal for today, {facts['weekday']} {facts['day']}, for your own "
        f"book {facts['account']}: {facts['title']}, {facts['test']}.\n\n"
        "This is NOT a trading decision: write no DECISION block, and place nothing. You are "
        "writing after the close, from the records below (FACTS). They are the truth: do not "
        "restate a number wrongly and do not invent one. Only the broker's order ids and "
        "fills say what was traded.\n\n"
        "Write, in plain English, under these headings:\n"
        "SETUPS - every setup or look you were asked about: took or skipped, and why, one line "
        "each (near-identical skips may share a line; say how many).\n"
        "TRADES - how each trade you took played out: entry, exit, R and net after fees. Then "
        "the skips whose price path afterwards (the `after` lines) tells you something, and how "
        "you did against your rule bot (the yardstick lines).\n"
        "WHAT WORKED\n"
        "WHAT DIDN'T\n"
        "MISTAKES - your own, judged against your standing instructions and what you could see "
        "at the time. If you see none, say so; do not invent any.\n"
        "DIFFERENTLY - what you would do differently next time.\n"
        "Last line: LESSON: <the day's single most useful lesson, one sentence under 200 "
        "characters>\n\n"
        "Be honest. A skip that would have lost money was a good skip. A skip that would have "
        "won may still have been right on what you could see then - say which, and why. `after` "
        "is hindsight: the plain price path before costs and without trade management. Costs "
        "are brokerage (fees) plus slippage, which is inside the fill prices and shown as its "
        "own figure on each trade. Stop moves (breakeven at +1R, the trail) and the 15:50 "
        "close-out are code's trade management, not your decisions; each trade says who set "
        "its limit and size - judge yourself only on what was yours. Setups and "
        "stocks listed as nobody's decision were never yours to decide (too stale, no data, or "
        "you could not be asked): mention them only as the system's facts. Under 450 words.\n\n"
        "This journal is written only: during the test it is never shown to you or any agent "
        "when deciding. It is for Rick and for the Practice Lab.\n\n"
        "FACTS (plain code, from the day's records):\n" + "\n".join(facts_lines(facts))
        + "\n"
    )


def claude_exe() -> str:
    """The Claude Code CLI the decider's model is run through (as the Practice Lab finds it)."""
    if os.environ.get("ASXBOT_JOURNAL_CLAUDE"):
        return os.environ["ASXBOT_JOURNAL_CLAUDE"]
    exe = (Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "@anthropic-ai"
           / "claude-code" / "bin" / "claude.exe")  # fmt: skip
    return str(exe) if exe.exists() else (shutil.which("claude") or "claude")


def standing_instructions(cfg) -> tuple[str, str]:
    """(text, where): the decider's AGENTS.md - the live OpenClaw workspace's copy, which is
    what the agent runs on, else the repo's mirror (docs/agents)."""
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    here = Path(__file__).resolve().parents[3]
    for p in (home / ".openclaw" / "workspace-trader-decider" / "AGENTS.md",
              here / "docs" / "agents" / "trader-decider.AGENTS.md",
              Path(cfg.root) / "docs" / "agents" / "trader-decider.AGENTS.md"):  # fmt: skip
        try:
            return p.read_text(encoding="utf-8"), str(p)
        except OSError:
            continue
    return "", "none found"


SYSTEM_TAIL = (
    "\n\n---\n# Tonight: your end-of-day trading journal\n"
    "After the close you write a short journal of your own trading day from the day's records. "
    "It is not a decision and places nothing. It is written only: during the frozen test it is "
    "never shown back to you when you decide. Write it honestly, in plain English.\n"
)


def _usage(stdout: str) -> dict:
    """What `claude -p --output-format stream-json` said: the text, the model that ran, the
    tokens, the list-price cost, the time, and the plan's meters."""
    u = {"text": "", "is_error": False, "model_ran": "", "input_tokens": 0, "output_tokens": 0,
         "cache_read_tokens": 0, "cache_creation_tokens": 0, "cost_usd_list": 0.0,
         "api_ms": 0, "weekly_usage": None, "five_hour_usage": None, "limit_status": ""}
    init_model = ""
    for line in (stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        t = ev.get("type")
        if t == "system" and ev.get("model"):
            init_model = str(ev["model"])
        elif t == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            win = info.get("unifiedWindows") or {}
            u["weekly_usage"] = (win.get("seven_day") or {}).get("utilization", u["weekly_usage"])
            u["five_hour_usage"] = (win.get("five_hour") or {}).get("utilization",
                                                                    u["five_hour_usage"])
            u["limit_status"] = str(info.get("status") or "")
        elif t == "result":
            u["text"] = str(ev.get("result") or "")
            u["is_error"] = bool(ev.get("is_error"))
            u["cost_usd_list"] = round(float(ev.get("total_cost_usd") or 0), 5)
            u["api_ms"] = int(ev.get("duration_api_ms") or ev.get("duration_ms") or 0)
            us = ev.get("usage") or {}
            u["input_tokens"] = int(us.get("input_tokens") or 0)
            u["output_tokens"] = int(us.get("output_tokens") or 0)
            u["cache_read_tokens"] = int(us.get("cache_read_input_tokens") or 0)
            u["cache_creation_tokens"] = int(us.get("cache_creation_input_tokens") or 0)
            mu = ev.get("modelUsage") or {}
            if mu:
                u["model_ran"] = max(mu, key=lambda k: (mu[k] or {}).get("outputTokens") or 0)
    u["model_ran"] = u["model_ran"] or init_model
    return u


def ask_model(prompt: str, system: str, model: str, effort: str, timeout_s: int = 420) -> dict:
    """One `claude -p` call: no tools, no session kept, no settings or MCP, in an empty
    scratch folder. {"ok", "kind", "reason", "text", "seconds", **usage}."""
    from asxbot.arena.agents import classify_failure

    work = Path(tempfile.mkdtemp(prefix="asxbot-journal-"))
    sysfile = work / "system.md"
    sysfile.write_text(system, encoding="utf-8")
    exe = claude_exe()
    cmd = [sys.executable, exe] if exe.endswith(".py") else [exe]  # tests: a stand-in script
    cmd += ["-p", "--model", model.split("/")[-1], "--effort", effort,
            "--system-prompt-file", str(sysfile), "--tools", "", "--output-format",
            "stream-json", "--verbose", "--no-session-persistence", "--setting-sources", "",
            "--strict-mcp-config", "--disable-slash-commands"]  # fmt: skip
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    started = _time.monotonic()
    try:
        r = proc.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                     errors="replace", timeout=timeout_s, cwd=str(work), env=env)  # fmt: skip
    except proc.TimeoutExpired:
        return {"ok": False, "kind": "timeout", "reason": f"no answer within {timeout_s}s",
                "text": "", "seconds": float(timeout_s), **_usage("")}  # fmt: skip
    except OSError as e:
        return {"ok": False, "kind": "error", "reason": f"could not start {exe}: {e}",
                "text": "", "seconds": 0.0, **_usage("")}  # fmt: skip
    finally:
        shutil.rmtree(work, ignore_errors=True)
    secs = round(_time.monotonic() - started, 1)
    u = _usage(r.stdout)
    text = u.pop("text").strip()
    said = text + "\n" + (r.stderr or "")[-400:]
    if u["limit_status"] == "rejected" or u["is_error"] or r.returncode != 0 or not text:
        kind = "usage_limit" if u["limit_status"] == "rejected" else classify_failure(said)
        why = (text[:300] or (r.stderr or "")[-300:] or f"exit {r.returncode}, no text").strip()
        return {"ok": False, "kind": kind, "reason": why, "text": text, "seconds": secs, **u}
    return {"ok": True, "kind": "", "reason": "", "text": text, "seconds": secs, **u}


def calls_today(data_dir, day: date, account: str) -> list[dict]:
    """This book's journal-call records for `day` (started and done), oldest first."""
    p = Path(data_dir) / "events" / f"{CALLS}.jsonl"
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("day") == day.isoformat() and r.get("account") == account:
                out.append(r)
    return out


def parse_lesson(text: str) -> str:
    for line in reversed((text or "").splitlines()):
        s = line.strip().lstrip("#>*- ").replace("**", "").strip()
        if s.upper().startswith("LESSON"):
            s = s.split(":", 1)[1].strip() if ":" in s else s[6:].strip()
            return s[:LESSON_MAX]
    return ""


def agent_entry(cfg, pb, facts: dict, *, again: bool = False, ask=ask_model) -> dict:
    """The agent's journal entry for one book: {"text", "lesson", "by", "call"}. One call per
    book per day: a book with a journal-call record for the day is not asked again unless
    `again`. No decision that day means no call."""
    from asxbot.arena.agents import DECIDER, expected_effort, expected_model, same_model

    day = date.fromisoformat(facts["day"])
    acct = facts["account"]
    if not facts.get("decisions"):
        return {"text": "", "lesson": "no decisions today - nothing was put to the agent",
                "by": "code (no decision to reflect on; no model call)", "call": None}  # fmt: skip
    prior = calls_today(cfg.data_dir, day, acct)
    if prior and not again:
        done = next((r for r in reversed(prior) if r.get("stage") == "done"), None)
        return {"text": "", "lesson": "", "by": "", "call": done or prior[-1], "already": True}
    model, effort = expected_model(cfg, "decider"), expected_effort(cfg, "decider")
    system, where = standing_instructions(cfg)
    prompt = agent_brief(facts, pb)
    events = EventLog(cfg.data_dir)
    base = {"day": facts["day"], "account": acct, "playbook": pb.key, "agent": DECIDER,
            "via": "claude -p (the decider's model directly, not OpenClaw)",
            "model_expected": model, "effort": effort, "again": bool(again and prior),
            "instructions": where,
            "instructions_sha": hashlib.sha256(system.encode()).hexdigest()[:12]}  # fmt: skip
    events.append(CALLS, {**base, "stage": "started", "chars_in": len(prompt) + len(system)})
    r = ask(prompt, system + SYSTEM_TAIL, model, effort)
    # The text is kept in the record too: a paid call is never lost to a failed file write.
    # `kind` is the event log's own field: why a call failed is recorded as `failure`.
    rec = {**base, "stage": "done",
           **{k: v for k, v in r.items() if k not in ("text", "kind")},
           "failure": r.get("kind") or "",
           "chars_out": len(r.get("text") or ""), "entry_text": r.get("text") or ""}  # fmt: skip
    rec["model_matches"] = bool(r.get("model_ran")) and same_model(r["model_ran"], model)
    events.append(CALLS, rec)
    if r.get("model_ran") and not rec["model_matches"]:
        log.warning("MODEL MISMATCH: the journal asked for %s, %s answered", model, r["model_ran"])
    if not r.get("ok"):
        log.error("journal call for %s failed (%s): %s", acct, r.get("kind"), r.get("reason"))
        return {"text": "", "lesson": "", "by": "", "call": rec}
    tokens_in = sum(int(r.get(k) or 0) for k in ("input_tokens", "cache_read_tokens",
                                                   "cache_creation_tokens"))
    log.info("journal for %s: %s answered in %.0fs (%d in / %d out tokens, $%.3f list)", acct,
             r.get("model_ran"), r.get("seconds") or 0, tokens_in,
             r.get("output_tokens") or 0, r.get("cost_usd_list") or 0)  # fmt: skip
    return {"text": r["text"], "lesson": parse_lesson(r["text"]),
            "by": f"{r.get('model_ran') or model} at {effort} effort (trader-decider's model, "
                  "one call)", "call": rec}  # fmt: skip


def usage_line(call: dict | None) -> str:
    if not call:
        return "no model call"
    if call.get("stage") != "done":
        return "a call was started but never finished (no record of its answer)"
    if not call.get("ok"):
        why = call.get("failure") or call.get("kind")
        return f"the call failed ({why}): {str(call.get('reason'))[:160]}"
    wk = call.get("weekly_usage")
    return (f"{call.get('model_ran')}, {call.get('effort')} effort, {call.get('seconds')}s, "
            f"{call.get('input_tokens', 0)} in + {call.get('cache_read_tokens', 0)} cached + "
            f"{call.get('cache_creation_tokens', 0)} cache-written / "
            f"{call.get('output_tokens', 0)} out tokens, "
            f"${float(call.get('cost_usd_list') or 0):.3f} at list prices (a plan call)"
            + (f"; weekly plan meter {float(wk):.0%}" if wk is not None else ""))


# --------------------------------------------------------------------------
# the file
# --------------------------------------------------------------------------
def _trade_lines(trades: list[dict]) -> list[str]:
    if not trades:
        return ["- no trades"]
    out = []
    for t in trades:
        r = f"{t['r_net']:+.2f}R net" if t.get("r_net") is not None else "R n/a (no usable stop)"
        state = "STILL OPEN" if t["still_open"] else f"closed {t['closed']}"
        slip = f"{t.get('slippage', 0):.2f}" + ("" if t.get("slippage_known", True)
                                                  else " (part unknown)")  # fmt: skip
        out.append(
            f"- {t['ticker']} {t['side']} {t['qty']}: decided {t.get('decided')}, limit "
            f"{t.get('entry_limit')}" + (f" ({t['limit_by']})" if t.get("limit_by") else "")
            + f"; in {t['entry']} at {t['opened']}, out {t['exit']} "
            f"({state}); stop {t['initial_stop']} (risk ${t['risk_aud']}); gross after "
            f"slippage {t['gross']:+.2f}, net {t['net']:+.2f} ({r}); costs "
            f"{t.get('costs_all_in', t['fees']):.2f} = brokerage {t['fees']:.2f} + slippage "
            f"{slip} (in the fill prices)" + (f" + borrow {t['borrow']:.2f}" if t["borrow"] else "")
        )
        if t.get("entry_reason"):
            out.append(f"  - why in: {t['entry_reason']}")
        out += [f"  - exit: {e}" for e in t.get("exits", [])]
        out += [f"  - {m}" for m in t.get("management", [])]
    return out


def facts_lines(f: dict) -> list[str]:
    res = f["result"]
    today = res.get("today_after_fees")
    slip = sum(t.get("slippage", 0) for t in f["trades"])
    lines = [
        f"- Result: {res['round_trips_closed']} round trips closed, {res['wins_after_fees']} won "
        f"after all costs; net {res['net_closed']:+.2f}, brokerage {res['fees']:.2f}, slippage "
        f"{slip:.2f}"
        + (f"; the book's day after fees (marked) {today:+.2f}" if today is not None else ""),
    ]
    if res.get("still_open"):
        lines.append(f"- Still open at the close: {', '.join(res['still_open'])}")
    lines += ["", "### Trades", *_trade_lines(f["trades"])]
    if "setups_decided" in f:
        lines += ["", "### Setups decided"]
        for s in f["setups_decided"] or []:
            after = (s.get("after") or {}).get("line") or "no bars after it"
            lines.append(f"- {s['at']} {s['ticker']} {s['setup']} {s['side']} at {s['last']}, "
                         f"stop {s['stop']}: **{s['decision'].upper()}** - {s['why']}")
            lines.append(f"  - after: {after}")
        if not f["setups_decided"]:
            lines.append("- none")
        if f.get("setups_not_decided"):
            lines += ["", "### Setups nobody decided (the system's facts)"]
            lines += [f"- {n} x {why}" for why, n in f["setups_not_decided"].items()]
    if "pre_open_looks" in f:
        lines += ["", "### Pre-open looks"]
        for p in f["pre_open_looks"] or []:
            lines.append(f"- {p['at']} {p['ticker']} ({'; '.join(p.get('headlines') or [])}): "
                         f"**{p['decision'].upper()}** - {p.get('why', '')}")
            if p.get("after"):
                lines.append(f"  - after: {p['after']}")
        if not f["pre_open_looks"]:
            lines.append("- none")
        lines += ["", "### Reaction looks"]
        for x in f["reaction_looks"] or []:
            lines.append(f"- {x['ticker']} ({'; '.join(x.get('headlines') or [])}), seen "
                         f"{x.get('seen')}: **{x['decision'].upper()}** - {x['why']}")
            lines.append(f"  - after: {x['after']}")
        if not f["reaction_looks"]:
            lines.append("- none")
        if f.get("never_looked"):
            lines += ["", "### Stocks with news never looked at (the system's facts)"]
            for x in f["never_looked"]:
                lines.append(f"- {x['ticker']} {x['status']}: {x['why']}; after 10:10: "
                             f"{x['after_10_10']}")
    if f.get("yardstick"):
        y = f["yardstick"]
        yr = y["result"]
        lines += ["", f"### Your yardstick today: the rule bot ({y['account']})",
                  f"- {yr['round_trips_closed']} round trips closed, {yr['wins_after_fees']} won "
                  f"after all costs, net {yr['net_closed']:+.2f}, fees {yr['fees']:.2f}"]
        for t in y["trades"]:
            r = f"{t['r_net']:+.2f}R" if t.get("r_net") is not None else "R n/a"
            lines.append(f"- {t['ticker']} {t['side']}: in {t['entry']} at {t['opened']}, out "
                         f"{t['exit']} at {t['closed'] or 'still open'}, net {t['net']:+.2f} "
                         f"({r}) - {t['entry_reason']}")
    if "rule" in f:
        r = f["rule"]
        lines += ["", "### The rule at 10:30",
                  f"- status {r.get('status')}, decided {r.get('decided_at')}, on "
                  f"{r.get('data')}" + (f" - {r['why']}" if r.get("why") else "")]  # fmt: skip
        lines += [f"- SIGNAL {s['ticker']}: {s['why']}" for s in r["signals"]]
        lines += [f"- no signal {s['ticker']}: {s['why']}" for s in r["no_signal"]]
        lines.append(f"- screened out on turnover before measuring: {r['screened_out']}")
    return lines


def render(f: dict, pb, entry: dict | None) -> str:
    who = ("the AI agent's book (trader-decider)" if f["kind"] == "agent"
           else "the rule bot's book (plain code, no model)")  # fmt: skip
    head = [
        f"# Trading journal - {f['account']} - {f['weekday']} {f['day']}",
        "",
        f"{f['title']}, {who}. {f['test']}.",
        "",
        f"> {frozen_rule(pb)}",
        ">",
        f"> {lab_note(date.fromisoformat(f['day']))}",
        "",
    ]
    body = ["## The day's facts (plain code)", "", *facts_lines(f), ""]
    data = {"version": 1, "frozen_test_rule": frozen_rule(pb),
            "locked_test_day": LOCKED_TEST[0] <= date.fromisoformat(f["day"]) <= LOCKED_TEST[1],
            "facts": f}  # fmt: skip
    if f["kind"] == "agent":
        e = entry or {}
        body += ["## The agent's journal (in its own words)", ""]
        if e.get("text"):
            body += [e["text"], ""]
        elif e.get("by", "").startswith("code"):
            body += [f"_{e.get('lesson')}_", ""]
        else:
            body += ["_No entry: " + usage_line(e.get("call")) + "._", ""]
        body += [f"Written by: {e.get('by') or 'nobody (no answer)'}. Model call: "
                 f"{usage_line(e.get('call'))}.", ""]
        call = {k: v for k, v in (e.get("call") or {}).items() if k != "entry_text"} or None
        data |= {"entry": e.get("text", ""), "lesson": e.get("lesson", ""),
                 "written_by": e.get("by", ""), "call": call}  # fmt: skip
    else:
        body += ["Written by: plain code (a rule bot keeps facts, not a journal; no model call).",
                 ""]  # fmt: skip
        data |= {"entry": "", "lesson": "", "written_by": "code", "call": None}
    tail = ["## Data (for the Practice Lab)", "", DATA_FENCE,
            json.dumps(data, indent=1, default=str), "```", ""]
    return "\n".join(head + body + tail)


def read_file(p: Path) -> dict | None:
    """The data block of one journal file, or None."""
    try:
        text = Path(p).read_text(encoding="utf-8")
    except OSError:
        return None
    i = text.rfind(DATA_FENCE)
    if i < 0:
        return None
    j = text.find("\n```", i + len(DATA_FENCE))
    try:
        return json.loads(text[i + len(DATA_FENCE):j if j > 0 else None])
    except ValueError:
        return None


def load(data_dir, since: date | None = None, until: date | None = None) -> list[dict]:
    """Every journal between two days (inclusive), oldest first, each {"day", "account",
    "path", "facts", "entry", "lesson", "written_by", "call", "locked_test_day", ...}: what
    the Practice Lab reads. Never read by the watcher or the agents during the test."""
    root = Path(data_dir) / "arena" / "journal"
    out = []
    if not root.exists():
        return out
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        try:
            day = date.fromisoformat(d.name)
        except ValueError:
            continue
        if (since and day < since) or (until and day > until):
            continue
        for p in sorted(d.glob("*.md")):
            data = read_file(p)
            if data:
                out.append({"day": day.isoformat(), "account": p.stem, "path": str(p), **data})
    return out


# --------------------------------------------------------------------------
# the evening: every book, and the three lines for the report
# --------------------------------------------------------------------------
def _earlier_entry(old: dict, call: dict) -> dict:
    """The entry already written for this book today: from its file, or from the call's own
    record when the file never got it."""
    text = old.get("entry") or call.get("entry_text") or ""
    by = old.get("written_by") or (f"{call.get('model_ran')} at {call.get('effort')} effort "
                                   "(trader-decider's model, one call)" if text else "")
    return {"text": text, "lesson": old.get("lesson") or parse_lesson(text), "by": by,
            "call": {k: v for k, v in call.items() if k != "entry_text"} or old.get("call")}


def run(cfg, arena, day: date, *, agent: bool = True, again: bool = False,
        ask=ask_model) -> list[Path]:  # fmt: skip
    """Write the day's journal for every enabled playbook's two books. The agent's books get
    their one call (unless `agent` is False, or already made today); the bots' get facts."""
    from asxbot.arena.minutes import MinuteBars
    from asxbot.io import write_text_atomic

    minutes = MinuteBars(cfg.data_dir)
    written = []
    for pb in arena.playbooks():
        for kind in ("agent", "bot"):
            f = book_facts(cfg, arena, pb, kind, day, minutes)
            entry = None
            p = journal_path(cfg.data_dir, day, f["account"])
            if kind == "agent":
                if agent:
                    entry = agent_entry(cfg, pb, f, again=again, ask=ask)
                    if entry.get("already"):
                        entry = _earlier_entry(read_file(p) or {}, entry.get("call") or {})
                else:
                    old = read_file(p) or {}
                    entry = {"text": old.get("entry", ""), "lesson": old.get("lesson", ""),
                             "by": old.get("written_by", "") or "nobody (--no-agent)",
                             "call": old.get("call")}  # fmt: skip
            p.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(render(f, pb, entry), p)
            written.append(p)
    return written


def _bot_line(pbs, day_data: dict) -> str:
    parts = []
    for pb in pbs:
        d = day_data.get(pb.bot_account)
        if not d:
            parts.append(f"{short_name(pb).lower()} no journal")
            continue
        res = d["facts"]["result"]
        rs = [t.get("r_net") for t in d["facts"]["trades"] if not t.get("still_open")]
        r_txt = ", ".join(f"{r:+.1f}R" for r in rs if r is not None)
        n = res["round_trips_closed"]
        parts.append(f"{short_name(pb).lower()} {n} trade{'s' if n != 1 else ''} "
                     f"{res['net_closed']:+,.2f} after fees" + (f" ({r_txt})" if r_txt else ""))
    return "Rule bots: " + "; ".join(parts) + "."


def summary_lines(data_dir, day: date, pbs) -> list[str]:
    """The three lines for the evening report: each agent's LESSON, then the rule bots' facts.
    Empty when no journal was written that day."""
    day_data = {d["account"]: d for d in load(data_dir, day, day)}
    if not day_data:
        return []
    lines = []
    for pb in pbs:
        d = day_data.get(pb.agent_account)
        if d is None:
            lesson = "no journal written"
        elif d.get("lesson"):
            lesson = d["lesson"]
        else:
            lesson = "no entry: " + usage_line(d.get("call"))
        lines.append(f"{short_name(pb)} agent: {lesson}")
    lines.append(_bot_line(pbs, day_data))
    return lines


def telegram_text(data_dir, day: date, pbs) -> str:
    """The journal's summary as its own Trader-chat message (`arena journal --send`)."""
    lines = summary_lines(data_dir, day, pbs)
    head = f"📓 <b>Trading journal - {day:%a %d %b}</b>"
    if not lines:
        return head + "\nNo journal was written for this day."
    where = Path("data") / "arena" / "journal" / day.isoformat()
    return "\n".join(
        [head, *[f"- {escape(x, quote=False)}" for x in lines],
         f"Written only: no agent reads it during the 10-day test. One file per book in "
         f"{escape(str(where))}."]  # fmt: skip
    )


def report_block(data_dir, day: date, pbs) -> str:
    """The journal's part of the evening report (HTML), added by code after the agent has
    written the rest: the agents never see it."""
    from asxbot.announcements.live import is_trading_day

    if not is_trading_day(day):
        return ""
    lines = summary_lines(data_dir, day, pbs)
    if not lines:
        return ("<b>Journal</b>: none written for today (the evening's journal step did not "
                "run; arena_evening.log says why).")  # fmt: skip
    return "\n".join(
        ["<b>Journal - the day's lessons</b> (written only: no agent reads it during the test)"]
        + [f"- {escape(x, quote=False)}" for x in lines]
    )
