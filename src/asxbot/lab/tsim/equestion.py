"""E (CLOUD_BRIEF): the current day trader as it runs in the 10-day test - its rules plus its AI
agent choosing which setups to take - over the same days as its rules alone. Rick's question:
the replay only ran the rules; does the AI's choosing rescue those setups?

Run through the Practice Lab's time machine (lab/sim.run_day: the live code, the live
decider's model, effort and standing instructions via agentcall), on days AFTER the models'
knowledge cutoff (the check window), so the agent sees the days as they were. The bot book and
the agent book trade the same setups on the same day; the only difference is the agent's
take-or-skip (and its tighter stop).
"""

from __future__ import annotations

import json
import random
from datetime import date

from asxbot.lab import store


def run(cfg, days: list[date], progress=None) -> dict:
    from asxbot.lab import runner, sim
    from asxbot.lab.variants import BASELINE

    codes, shorts, ann, hist, universe = runner.common_inputs(cfg, days)
    out = []
    p = store.lab_data(cfg) / "tsim" / "e_question.json"
    have = {r["day"]: r for r in (store.read_json(p, {}) or {}).get("days", [])}
    for d in days:
        if d.isoformat() in have:
            out.append(have[d.isoformat()])
            continue
        rec = sim.run_day(cfg, d, {"id": BASELINE}, books=["bot", "agent"], anon=False,
                          codes=codes, shorts=shorts, ann=ann, hist=hist,
                          universe=universe)  # fmt: skip
        books = (rec.get("daytrader") or {}).get("books") or {}
        row = {"day": d.isoformat(), "market_move_pct": rec.get("market_move_pct"),
               "gaps": rec.get("gaps")}  # fmt: skip
        for k in ("bot", "agent"):
            b = books.get(k) or {}
            row[k] = {"pnl": b.get("pnl"), "fees": b.get("fees"),
                      "trades": len(b.get("trades") or [])}  # fmt: skip
        row["agent_stats"] = rec.get("agent_stats")
        row["setups"] = len((rec.get("daytrader") or {}).get("setups") or [])
        out.append(row)
        have[row["day"]] = row
        store.write_json(p, {"days": sorted(have.values(), key=lambda r: r["day"])})
        if progress:
            progress(row)
    return {"days": out}


def sample_days(cfg, n: int = 10, seed: str = "e-question") -> list[date]:
    from asxbot.lab.tsim import splits as S

    days = S.days(cfg, "check")
    return sorted(random.Random(seed).sample(days, min(n, len(days))))


def answer(cfg) -> str:
    p = store.lab_data(cfg) / "tsim" / "e_question.json"
    d = store.read_json(p, {}) or {}
    rows = [r for r in d.get("days", []) if not r.get("gaps")]
    if not rows:
        return "E has not run yet."
    bot = sum(r["bot"]["pnl"] or 0 for r in rows)
    agent = sum(r["agent"]["pnl"] or 0 for r in rows)
    bt = sum(r["bot"]["trades"] for r in rows)
    at = sum(r["agent"]["trades"] for r in rows)
    better = sum(1 for r in rows if (r["agent"]["pnl"] or 0) > (r["bot"]["pnl"] or 0))
    verdict = ("the agent's choosing did better than the rules alone" if agent > bot else
               "the agent's choosing did NOT rescue the rules")  # fmt: skip
    rescue = "and both still lost money" if agent <= 0 and bot <= 0 else (
        "and the agent's book made money" if agent > 0 else "")  # fmt: skip
    return (
        f"E over {len(rows)} days after the knowledge cutoff: rules alone ${bot:,.0f} "
        f"({bt} trades), rules + AI agent ${agent:,.0f} ({at} trades); the agent's book was "
        f"better on {better} of {len(rows)} days - {verdict} {rescue}."
    ).strip()


_ = json
