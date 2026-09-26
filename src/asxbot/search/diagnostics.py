"""Information for the report that can never promote an idea (CLOUD_BRIEF_SEARCH.md asks for
both brokers; the next session needs to know where the near-misses went):

  * every idea's practice result with the cheapest API broker's brokerage instead of IBKR's;
  * for ideas that made money on practice with 20+ trades but were stopped ONLY by the
    frozen-bot comparison, their check-set result - labelled as information: they failed a gate
    and stay failed. The sealed (locked) window is never touched here.
"""

from __future__ import annotations

import json

from asxbot.search.ideas import all_ideas
from asxbot.search.report import latest
from asxbot.search.run import Search, _slim

ONLY_BOT = "no better than the frozen rule bot"


def run(s: Search) -> dict:
    ideas = {i["id"]: i for i in all_ideas(s.m.data)}
    out = {"cheapest_broker_practice": {}, "info_check": {}, "frozen_bots": {}}
    for r in latest(s.tried()):
        idea = ideas.get(r["id"])
        if idea is None or r.get("verdict") == "not run - no data":
            continue
        c, _ = s.run_idea(idea, s.win["tune"], broker="cheapest_api")
        out["cheapest_broker_practice"][r["id"]] = _slim(c, s.base_card(idea, s.win["tune"]))
        pr = r.get("practice") or {}
        why = r.get("why") or []
        blocked_only_by_bot = (r.get("stage") == "practice" and pr.get("pnl", 0) > 0
                               and pr.get("trades", 0) >= 20
                               and all(w.startswith(ONLY_BOT) for w in why))  # fmt: skip
        if blocked_only_by_bot:
            v, _ = s.run_idea(idea, s.win["validate"])
            out["info_check"][r["id"]] = _slim(v, s.base_card(idea, s.win["validate"]))
    for which in ("daytrader", "v2"):
        for w in ("tune", "validate"):
            b = s.base_card({"family": "orb_inplay" if which == "daytrader" else "drift"},
                            s.win[w])  # fmt: skip
            out["frozen_bots"][f"{which}:{w}"] = {"trades": b.get("trades"),
                                                  "pnl": b.get("pnl_after_fees")}  # fmt: skip
    (s.dir / "diagnostics.json").write_text(json.dumps(out, indent=1, default=str),
                                            encoding="utf-8")  # fmt: skip
    return out


__all__ = ["run"]
