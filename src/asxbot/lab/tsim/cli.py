"""`asxbot lab sim ...`: the proper trading simulator, the AI trader and the strategy search.

    asxbot lab sim fidelity [--days 2026-09-25,...]     the check against the live paper test
    asxbot lab sim run --trader rules:orb --from D --to D [--params JSON] [--id NAME]
    asxbot lab sim run --trader ai --from D --to D [--max-calls 40] [--addendum TEXT]
    asxbot lab sim yardsticks                            A, its D variants, rules drift
    asxbot lab sim tick [--max-minutes 50] [--force]     one unit of the never-ending search
    asxbot lab sim log                                   reports/tsim_research_log.md
    asxbot lab sim e-question [--days 10]                E: the day trader's rules vs + its agent
    asxbot lab sim contamination RUN_ID                  can the model see through the disguise?
    asxbot lab sim scoreboard                            the weekly scoreboard text
Lab work runs only while the market is closed (lab.runner.market_quiet) unless --force.
"""

from __future__ import annotations

import json
from datetime import date


def _cfg():
    from asxbot.lab.cli import _cfg as c

    return c()


def _quiet_or_force(a) -> bool:
    from asxbot.lab import runner

    ok, why = runner.market_quiet()
    if not ok and not getattr(a, "force", False):
        print(f"not now: {why}")
        return False
    return True


def _days(a):
    from asxbot.arena.replay_ibkr import sessions

    if getattr(a, "days", None):
        return [date.fromisoformat(x) for x in a.days.split(",")]
    return sessions(date.fromisoformat(a.frm), date.fromisoformat(a.to))


def cmd_fidelity(a) -> int:
    from pathlib import Path

    from asxbot.lab.tsim import fidelity, run

    cfg = _cfg()
    days = _days(a) if (a.days or a.frm) else sorted(
        {date.fromisoformat(d) for d in _live_days(cfg)})  # fmt: skip
    inputs = run.prepare_inputs(cfg, days)
    txt = fidelity.report(cfg, days, inputs, with_decisions=not a.no_decisions)
    out = Path(cfg.root) / "reports" / f"tsim_fidelity_{max(days):%Y%m%d}.md"
    out.write_text(txt, encoding="utf-8")
    print(out)
    return 0


def _live_days(cfg) -> list[str]:
    import glob

    days = set()
    for p in glob.glob(str(cfg.data_dir / "arena" / "accounts" / "asx_daytrader_v1__*.json")):
        d = json.loads(open(p, encoding="utf-8").read())
        days |= {o["decided_at"][:10] for o in d.get("orders", {}).values()}
    return sorted(days)


def cmd_run(a) -> int:
    from asxbot.lab.tsim import run
    from asxbot.lab.tsim.report import score

    if not _quiet_or_force(a):
        return 1
    cfg = _cfg()
    days = _days(a)
    kind, _, fam = a.trader.partition(":")
    spec = {"kind": kind}
    if kind == "rules":
        spec.update(family=fam, params=json.loads(a.params or "{}"))
    else:
        spec.update(max_calls_per_day=a.max_calls, addendum=a.addendum or "")
    from asxbot.lab.tsim import splits as S

    sealed = S.current(cfg)["sealed"]
    if any(sealed[0] <= d.isoformat() <= sealed[1] for d in days) and not a.calibration:
        print("refused: those days include the sealed block (only a finalist's one run, or "
              "--calibration for the frozen live configuration)")  # fmt: skip
        return 2
    inputs = run.prepare_inputs(cfg, days)
    rid = a.id or f"{a.trader.replace(':', '_')}_{days[0]:%m%d}_{days[-1]:%m%d}"
    out = run.run(rid, spec, days, inputs, cfg=cfg,
                  progress=lambda i, n, r: print(f"{r['day']} ({i}/{n}): P&L {r['pnl']:+,.2f}, "
                                                 f"{r['calls']} calls, {r['wall_s']:.0f}s",
                                                 flush=True))  # fmt: skip
    print(json.dumps(score(out, cfg), indent=1, default=float))
    return 0


def cmd_yardsticks(a) -> int:
    from asxbot.lab.tsim import run, search

    if not _quiet_or_force(a):
        return 1
    cfg = _cfg()
    cache = {}

    def inputs_for(days):
        k = (min(days), max(days))
        if k not in cache:
            cache[k] = run.prepare_inputs(cfg, days)
        return cache[k]

    y = search.ensure_yardsticks(cfg, inputs_for)
    print(
        json.dumps(
            {
                w: {
                    k: {x: v[x] for x in ("net", "trades", "t_stat", "old_rules")}
                    for k, v in y[w].items()
                }
                for w in ("practice", "check")
            },
            indent=1,
        )
    )
    search.write_log(cfg)
    return 0


def cmd_tick(a) -> int:
    from asxbot.lab.tsim import search

    if not _quiet_or_force(a):
        return 1
    print(search.tick(_cfg(), max_minutes=float(a.max_minutes), propose_ok=not a.no_propose))
    return 0


def cmd_log(a) -> int:
    from asxbot.lab.tsim import search

    print(search.write_log(_cfg()))
    return 0


def cmd_e(a) -> int:
    from asxbot.lab.tsim import equestion

    if not _quiet_or_force(a):
        return 1
    cfg = _cfg()
    days = equestion.sample_days(cfg, int(a.n))
    equestion.run(cfg, days, progress=lambda r: print(json.dumps(r), flush=True))
    print(equestion.answer(cfg))
    return 0


def cmd_contamination(a) -> int:
    from asxbot.lab.tsim import contamination

    print(json.dumps(contamination.probe(_cfg(), a.run_id, int(a.n)), indent=1)[:4000])
    return 0


def cmd_compare(a) -> int:
    from asxbot.lab.tsim import compare

    print(compare.report(_cfg(), a.run_a, a.run_b))
    return 0


def cmd_scoreboard(a) -> int:
    from asxbot.lab.tsim import scoreboard

    print(scoreboard.text(_cfg()))
    return 0


def add_parsers(ls) -> None:
    sim = ls.add_parser("sim", help="the proper trading simulator, the AI trader, the search")
    ss = sim.add_subparsers(dest="sim_cmd", required=True)
    f = ss.add_parser("fidelity", help="the simulator against the live paper test's days")
    f.add_argument("--days", default=None)
    f.add_argument("--from", dest="frm", default=None)
    f.add_argument("--to", default=None)
    f.add_argument("--no-decisions", action="store_true")
    f.set_defaults(fn=cmd_fidelity)
    r = ss.add_parser("run", help="run a trader over days (one account, in date order)")
    r.add_argument("--trader", required=True, help="rules:<family>, ai or team")
    r.add_argument("--from", dest="frm")
    r.add_argument("--to")
    r.add_argument("--days", default=None)
    r.add_argument("--params", default="{}")
    r.add_argument("--addendum", default="")
    r.add_argument("--max-calls", type=int, default=40)
    r.add_argument("--id", default=None)
    r.add_argument(
        "--calibration",
        action="store_true",
        help="allow sealed days for a calibration of the frozen live configuration",
    )
    r.add_argument("--force", action="store_true")
    r.set_defaults(fn=cmd_run)
    y = ss.add_parser("yardsticks", help="run the yardsticks on the practice and check windows")
    y.add_argument("--force", action="store_true")
    y.set_defaults(fn=cmd_yardsticks)
    t = ss.add_parser("tick", help="one unit of the never-ending strategy search")
    t.add_argument("--max-minutes", default=50)
    t.add_argument("--no-propose", action="store_true")
    t.add_argument("--force", action="store_true")
    t.set_defaults(fn=cmd_tick)
    ss.add_parser("log", help="write reports/tsim_research_log.md").set_defaults(fn=cmd_log)
    e = ss.add_parser("e-question", help="E: the day trader's rules alone vs with its AI agent")
    e.add_argument("--days", dest="n", default=10)
    e.add_argument("--force", action="store_true")
    e.set_defaults(fn=cmd_e)
    c = ss.add_parser("contamination", help="can the model name what a disguised run showed it?")
    c.add_argument("run_id")
    c.add_argument("--days", dest="n", default=5)
    c.set_defaults(fn=cmd_contamination)
    ss.add_parser("scoreboard", help="the weekly scoreboard text").set_defaults(fn=cmd_scoreboard)
    cp = ss.add_parser("compare", help="two runs on the same days, side by side")
    cp.add_argument("run_a")
    cp.add_argument("run_b")
    cp.set_defaults(fn=cmd_compare)
