"""No hindsight, measured (CLOUD_BRIEF "HONEST TESTING"): before trusting a disguised run, check
the decision model cannot name the real company or date from what it was shown.

For a sample of a disguised run's days, the pre-open packet the AI trader actually saw (its
call log) is shown to the same model with one question: which real ASX companies (codes) and
which real date is this? Guesses are checked against the truth (the run's alias table). A day
where it names a real code among the aliases correctly, or the date within 3 days, is marked
CONTAMINATED and its results are excluded from any verdict (data/lab/tsim/contamination.json).
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

from asxbot.lab import store
from asxbot.lab.tsim import llm

SYSTEM = """You are shown a disguised snapshot of a stock market: codes are aliases and prices
scaled. Try your hardest to identify it. Reply with ONE JSON object: {"codes": [{"alias":
"S1A2B", "real_code": "XXX", "confidence": 0-1}], "date_guess": "YYYY-MM-DD or null",
"market_guess": "..."}. Guess only where you have a reason; an empty list is fine."""


def probe(cfg, run_id: str, n_days: int = 5, ask=None) -> dict:
    from asxbot.lab.tsim.anon import RunAnon
    from asxbot.lab.tsim.run import tsim_local

    ask = ask or llm.ask
    rdir = tsim_local() / "runs" / run_id
    spec = store.read_json(rdir / "spec.json", {}) or {}
    if not spec.get("disguised"):
        return {"run": run_id, "note": "not a disguised run"}
    days = [date.fromisoformat(d) for d in spec["days"]]
    anon = RunAnon(f"tsim:{run_id}", days)
    out = []
    for p in sorted((rdir / "calls").glob("*.jsonl"))[:n_days]:
        first = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
        packet = first["prompt"][:12000]
        aliases = set(re.findall(r"\bS[0-9A-F]{4}\b", packet))
        res = ask(packet, system=SYSTEM, model=llm.DECIDER[0], effort=llm.DECIDER[1], cfg=cfg)
        g = llm.parse_json(res.get("text", "")) or {}
        truth_day = date.fromisoformat(p.stem)
        hits = []
        # rebuild the alias table for the codes in the history (aliases are deterministic)
        from asxbot.lab.tsim.market import History
        from asxbot.lab.tsim.run import history_root

        table = {anon.alias(c): c for c in History(history_root()).codes()}
        for x in g.get("codes") or []:
            a, real = str(x.get("alias", "")).upper(), str(x.get("real_code", "")).upper()
            if a in aliases and table.get(a) == real:
                hits.append(a)
        dg = g.get("date_guess")
        date_hit = False
        try:
            date_hit = dg is not None and abs(date.fromisoformat(str(dg)) - truth_day) <= timedelta(3)
        except ValueError:
            pass
        out.append({"day": truth_day.isoformat(), "code_hits": hits, "date_hit": date_hit,
                    "guesses": g, "contaminated": bool(hits) or date_hit})  # fmt: skip
    rec = {"run": run_id, "days": out,
           "contaminated_days": [r["day"] for r in out if r["contaminated"]]}  # fmt: skip
    path = store.lab_data(cfg) / "tsim" / "contamination.json"
    allr = store.read_json(path, {}) or {}
    allr[run_id] = rec
    store.write_json(path, allr)
    return rec


def contaminated_days(cfg) -> set[str]:
    allr = store.read_json(store.lab_data(cfg) / "tsim" / "contamination.json", {}) or {}
    return {d for r in allr.values() for d in r.get("contaminated_days", [])}


_ = Path
