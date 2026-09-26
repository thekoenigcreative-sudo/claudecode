"""Per-day books in the Practice Lab's record shape, so the lab's own scoring (lab/score.py:
the scorecard, the daily t-statistic, P&L without the best three trades, the gates) and
WINNER.md's checks read the search's results exactly as they read a variant's.

A record: {"day", "gaps", "market_move_pct", "daytrader": {"books": {"bot": book}}} where the
book is {"pnl", "fees", "trades": [...], "stuck_at_close": [...]}. The key "daytrader" is only
where lab/score.card looks for the book; it does not mean the frozen day trader ran.

Intraday families start each day with a fresh $20,000 book (as the lab does); multi-day
families run one $20,000 book across the window and each day carries its marked P&L, with a
trade counted on the day it closed.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from asxbot.lab.score import card

PLAYBOOK, BOOK = "asx_daytrader", "bot"


def records(ctx, days: list[date], raw: list, kind: str) -> list[dict]:
    out = []
    for d, x in zip(days, raw, strict=True):
        rec = {"day": d.isoformat(), "gaps": [], "market_move_pct": ctx.m.market_move_pct(d)}
        if x is None:
            rec["gaps"].append("no bars cached for this session")
            out.append(rec)
            continue
        if kind == "intraday":
            trades = [t.record() for t in x]
            stuck = [{"ticker": t.code, "qty": t.stuck, "value": round(t.stuck * t.exit_raw, 2)}
                     for t in x if t.stuck]  # fmt: skip
            book = {"pnl": round(sum(t["net"] for t in trades), 2),
                    "fees": round(sum(t["fees"] for t in trades), 2),
                    "trades": trades, "stuck_at_close": stuck}  # fmt: skip
        else:
            trades = [t.record() for t in x["trades"]]
            book = {"pnl": round(x["pnl"], 2), "fees": round(x["fees"], 2), "trades": trades,
                    "stuck_at_close": x.get("stuck") or []}  # fmt: skip
        rec["daytrader"] = {"books": {BOOK: book}}
        out.append(rec)
    return out


def score(recs: list[dict]) -> dict:
    return card(recs, PLAYBOOK, BOOK)


def baseline_records(replay_json: Path, which: str) -> list[dict]:
    """The frozen rule bots' days from the IBKR replay (reports/replay_ibkr_*.json): `which`
    is "daytrader" (day trader v1) or "v2" (announcements v2), reshaped like `records`."""
    d = json.loads(Path(replay_json).read_text(encoding="utf-8"))
    out = []
    for r in d["days"]:
        if r.get("gaps") or which not in r:
            continue
        out.append({"day": r["day"], "gaps": [], "market_move_pct": r.get("market_move_pct"),
                    "daytrader": {"books": {BOOK: r[which]}}})  # fmt: skip
    return out


def in_window(recs: list[dict], days: list[date]) -> list[dict]:
    keep = {d.isoformat() for d in days}
    return [r for r in recs if r["day"] in keep]


__all__ = ["baseline_records", "in_window", "records", "score"]
