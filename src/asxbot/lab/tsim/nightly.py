"""What the trading simulator does in the Practice Lab's nightly tick (lab/runner._work calls
`work` with whatever time is left, only while the market is closed):

  1. FIDELITY for every live paper-test day not yet checked (reports/tsim_fidelity_<day>.md):
     the live test is the ground truth, checked every live day (CLOUD_BRIEF).
  2. SHADOW: every idea that passed its sealed run trades each new day after the sealed block
     (one account, carried day to day). After 10+ shadow days and 15+ trades in profit, it is
     written to winner_pending.json with its evidence - the one question for Rick comes from
     there (WINNER.md), never before.
  3. THE SEARCH (search.tick) for the rest of the time, inside the usage budget.
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

from asxbot.lab import store


def work(cfg, deadline: float) -> list[str]:
    did = []
    try:
        did += _fidelity(cfg)
    except Exception as e:  # noqa: BLE001 - reported in the tick's status, never silent
        did.append(f"tsim fidelity failed: {type(e).__name__}: {e}")
    try:
        did += _shadow(cfg)
    except Exception as e:  # noqa: BLE001
        did.append(f"tsim shadow failed: {type(e).__name__}: {e}")
    left = (deadline - time.monotonic()) / 60
    if left > 5:
        from asxbot.lab.tsim import search

        did.append("tsim search: " + search.tick(cfg, max_minutes=left - 2))
    return did


def _fidelity(cfg) -> list[str]:
    from asxbot.lab.tsim import cli, fidelity, run

    done = store.read_json(store.lab_data(cfg) / "tsim" / "fidelity_done.json", []) or []
    todo = [d for d in cli._live_days(cfg) if d not in done and _has_bars(date.fromisoformat(d))]
    out = []
    for d in todo:
        day = date.fromisoformat(d)
        inputs = run.prepare_inputs(cfg, [day])
        txt = fidelity.report(cfg, [day], inputs)
        p = Path(cfg.root) / "reports" / f"tsim_fidelity_{day:%Y%m%d}.md"
        p.write_text(txt, encoding="utf-8")
        done.append(d)
        out.append(f"fidelity {d}: {p.name}")
    store.write_json(store.lab_data(cfg) / "tsim" / "fidelity_done.json", done)
    return out


def _has_bars(day: date) -> bool:
    from asxbot.lab.tsim.market import INDEX, History
    from asxbot.lab.tsim.run import history_root

    return History(history_root()).path(INDEX, day).exists()


def _shadow(cfg) -> list[str]:
    from asxbot.arena.replay_ibkr import sessions
    from asxbot.lab.tsim import run, search
    from asxbot.lab.tsim import splits as S
    from asxbot.lab.tsim.report import score

    ideas = [i for i in search.load_ideas(cfg) if i["stage"] == "shadow"]
    if not ideas:
        return []
    from asxbot.lab.tsim.market import INDEX, History

    have = History(run.history_root()).days(INDEX)
    sealed_end = date.fromisoformat(S.current(cfg)["sealed"][1])
    out = []
    for i in ideas:
        start = date.fromisoformat(i["results"]["sealed"]["block"][1])
        days = [d for d in sessions(max(start, sealed_end), have[-1]) if d > start and d in have]
        if not days:
            continue
        o = run.run(f"{i['id']}_shadow", i["spec"], days, run.prepare_inputs(cfg, days), cfg=cfg)
        s = score(o, cfg)
        i["results"]["shadow"] = s
        if s["days"] >= 10 and s["trades"] >= 15 and s["net"] > 0:
            store.write_json(store.lab_data(cfg) / "tsim" / "winner_pending.json",
                             {"idea": i, "shadow": s})  # fmt: skip
            i = search.update(cfg, i, stage="winner_pending",
                              verdict="passed sealed + shadow: Rick to be asked")  # fmt: skip
        else:
            search.update(cfg, i)
        out.append(f"shadow {i['id']}: {s['days']} days, ${s['net']:,.0f}")
    return out
