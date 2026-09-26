"""The weekly scoreboard for Rick (CLOUD_BRIEF): the AI trader against the yardsticks and the old
rules, what the AI trader is trying now, the search's count and the lab's usage - in plain words,
for the Trader chat (sent by the Foreman with the Practice Lab's weekly summary)."""

from __future__ import annotations

from asxbot.lab import store
from asxbot.lab.tsim import llm, search
from asxbot.lab.tsim.report import SURVIVORSHIP


def text(cfg) -> str:
    ideas = search.load_ideas(cfg)
    yard = store.read_json(search.lab_dir(cfg) / "yardsticks.json", {}) or {}
    lines = ["Trading simulator - weekly scoreboard (simulated money on real ASX history, "
             "after all costs):"]  # fmt: skip
    for w in ("check", "practice"):
        ys = yard.get(w) or {}
        if ys:
            best = max(ys.items(), key=lambda kv: kv[1]["net"])
            anyv = next(iter(ys.values()))
            lines.append(f"- {w.title()} days ({anyv['days']}): best yardstick {best[0]} "
                         f"${best[1]['net']:,.0f}; old rules ${anyv.get('old_rules', 0):,.0f}.")
    ai = [i for i in ideas if i["spec"].get("kind") == "ai" and i["results"]]
    for i in ai[-3:]:
        r = i["results"].get("check") or i["results"].get("practice")
        lines.append(f"- AI trader {i['id']}: ${r['net']:,.0f} over {r['days']} days, "
                     f"{r['trades']} trades ({i['verdict'] or i['stage']}).")
    base = store.read_json(search.lab_dir(cfg) / "ai_baseline.json", None)
    if base:
        lines.append(f"- AI trader (no added instructions): ${base['net']:,.0f} over "
                     f"{base['days']} practice days, {base['trades']} trades.")
    trying = next((i for i in reversed(ideas) if i["stage"] in ("queued", "check", "finalist",
                                                               "shadow")), None)  # fmt: skip
    if trying:
        lines.append(f"- Trying now: {trying['id']} - {trying['reason'][:160]}")
    from asxbot.lab.tsim import equestion

    lines.append("- " + equestion.answer(cfg))
    n = search.n_tried(cfg)
    passed = [i for i in ideas if i["stage"] in ("shadow",)]
    lines.append(f"- Ideas tried: {n}; in shadow trading: {len(passed)}; winners: 0 until one "
                 "passes WINNER.md in full.")
    wk = llm.week_state()
    lines.append(f"- Lab's Claude usage this week: {wk['lab_share']:.0%} of the allowance "
                 f"(cap 15%), {wk['calls']} calls.")
    lines.append(SURVIVORSHIP)
    return "\n".join(lines)
