"""Shadow trading, phase 1 (PRACTICE_LAB.md 4): every promoted variant trades each new trading
day - a day nobody had seen when it was promoted - through the same engine at the live decision
times, once that day's IBKR bars are in (the 17:30 history fetch). Paper only; the watcher and
the frozen 10-day test are untouched. The frozen rule bot runs the same days for comparison.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

from asxbot.lab import runner, score, splits
from asxbot.lab.variants import Registry


def next_session(after: date) -> date:
    from asxbot.arena.replay_ibkr import sessions

    days = sessions(after + timedelta(days=1), after + timedelta(days=10))
    return days[0]


def promote(cfg, reg: Registry, v: dict, today: date | None = None) -> dict:
    today = today or datetime.now(runner.SYD).date()
    start = max(next_session(today), splits.WINDOWS["shadow"].first)
    v["shadow"] = {
        "from": start.isoformat(),
        "promoted_at": datetime.now().isoformat(timespec="seconds"),
    }
    reg.set_stage(v, "shadow", f"validated: shadow trading from {start}")
    reg.log_run({"event": "promoted", "variant": v["id"], "from": start.isoformat()})
    return v


def bars_in(day: date) -> bool:
    from asxbot.arena.replay_ibkr import HistoryBars, history_root

    return HistoryBars(history_root())._path("^AXJO", day).exists()


def shadow_days(v: dict, until: date | None = None) -> list[date]:
    from asxbot.arena.replay_ibkr import sessions

    first = date.fromisoformat(v["shadow"]["from"])
    until = until or datetime.now(runner.SYD).date()
    if until < first:
        return []
    return [d for d in sessions(first, until) if bars_in(d)]


def catch_up(cfg, reg: Registry, deadline: float | None) -> str:
    out = []
    for v in [v for v in reg.all() if v["stage"] in ("shadow", "final", "winner")]:
        days = shadow_days(v)
        if not days:
            continue
        for who, books in ((runner.BASE_VARIANT, ["bot"]), (v, runner.books_for(v))):
            r = runner.run_days(cfg, who, "shadow", days, books, deadline=deadline)
            if r != "done":
                return f"shadow {v['id']}: {r}"
        out.append(f"shadow {v['id']}: {len(days)} day(s)")
        if deadline is not None and time.monotonic() > deadline:
            break
    return "; ".join(out)


def card(v: dict, book: str, days: list[date] | None = None) -> dict:
    days = days if days is not None else shadow_days(v)
    return runner.cards(v["id"], "shadow", v["playbook"], [book], days=days)[book]


def ready_for_final(cfg, v: dict) -> bool:
    days = shadow_days(v)
    if len(days) < runner.SHADOW_DAYS_FOR_FINAL:
        return False
    return any(card(v, b, days)["pnl_after_fees"] > 0 for b in runner.books_for(v))


def summary(v: dict) -> dict:
    days = shadow_days(v)
    base = runner.cards("baseline", "shadow", v["playbook"], ["bot"], days=days)["bot"]
    out = {"days": len(days)}
    for b in runner.books_for(v):
        c = card(v, b, days)
        out[b] = {
            "pnl": c["pnl_after_fees"],
            "trades": c["trades"],
            "vs_frozen_bot": score.versus(c, base),
        }
    return out
