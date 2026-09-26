"""`asxbot lab ...` (PRACTICE_LAB.md). The Foreman runs `lab tick` whenever the market is closed."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path


def _cfg():
    from asxbot.config import load_config
    from asxbot.log import setup_logging

    cfg = load_config()
    setup_logging(cfg.logs_dir)
    return cfg


def cmd_tick(a) -> int:
    from asxbot.lab import runner

    print(runner.tick(_cfg(), max_minutes=float(a.max_minutes), force=a.force))
    return 0


def cmd_worker(a) -> int:
    from asxbot.lab import runner

    return runner.worker_main(_cfg(), Path(a.job))


def cmd_status(a) -> int:
    from asxbot.lab import runner, store

    cfg = _cfg()
    print(json.dumps(store.read_json(runner.status_path(cfg), {}), indent=1))
    return 0


def cmd_report(a) -> int:
    from asxbot.lab import report, store
    from asxbot.lab.variants import Registry

    cfg = _cfg()
    print(report.write(cfg, Registry(store.lab_data(cfg))))
    return 0


def cmd_weekly(a) -> int:
    from asxbot.lab import report

    print(report.weekly_summary(_cfg()))
    return 0


def cmd_evening(a) -> int:
    from asxbot.lab import report

    print("\n".join(report.evening_section(_cfg())))
    return 0


def cmd_register(a) -> int:
    from asxbot.arena.levels import load_playbook
    from asxbot.lab import store
    from asxbot.lab.variants import Registry

    cfg = _cfg()
    overrides = {}
    for kv in a.set or []:
        k, _, v = kv.partition("=")
        overrides[k] = json.loads(v)
    v = Registry(store.lab_data(cfg)).register(
        a.playbook,
        overrides,
        agent_prompt=a.prompt or "",
        why=a.why,
        proposed_by=a.by,
        base_raw=load_playbook(cfg, a.playbook).raw,
    )
    print(v["id"])
    return 0


def cmd_research_packet(a) -> int:
    from asxbot.lab import research

    print(research.build_packet(_cfg()))
    return 0


def cmd_ingest(a) -> int:
    from asxbot.lab import research

    print(json.dumps(research.ingest(_cfg(), Path(a.folder)), indent=1))
    return 0


def cmd_day(a) -> int:
    """One day for one variant, printed (debugging; never the locked test)."""
    from asxbot.lab import runner, store
    from asxbot.lab.variants import Registry

    cfg = _cfg()
    v = (
        runner.BASE_VARIANT
        if a.variant == "baseline"
        else Registry(store.lab_data(cfg)).load(a.variant)
    )
    books = a.books.split(",")
    day = date.fromisoformat(a.day)
    runner.run_days_here(cfg, v, "sample", [day], books, anon=a.anon)
    print(
        json.dumps(
            store.read_json(store.results_dir(v["id"], "sample", a.anon) / f"{day}.json"), indent=1
        )[:6000]
    )
    return 0


def add_parsers(sub) -> None:
    lab = sub.add_parser(
        "lab", help="the Practice Lab (PRACTICE_LAB.md): simulated practice, variants, shadow"
    )
    ls = lab.add_subparsers(dest="lab_cmd", required=True)
    t = ls.add_parser("tick", help="the next unit of practice (only while the market is closed)")
    t.add_argument("--max-minutes", default=55)
    t.add_argument(
        "--force", action="store_true", help="run even in the watcher's hours (testing only)"
    )
    t.set_defaults(fn=cmd_tick)
    w = ls.add_parser("worker", help="(internal) simulate the days in a job file")
    w.add_argument("--job", required=True)
    w.set_defaults(fn=cmd_worker)
    ls.add_parser("status", help="what the lab did last and what it needs").set_defaults(
        fn=cmd_status
    )
    ls.add_parser("report", help="write reports/lab_<stamp>.md and the scoreboard").set_defaults(
        fn=cmd_report
    )
    ls.add_parser("weekly", help="the weekly summary text").set_defaults(fn=cmd_weekly)
    ls.add_parser("evening", help="the evening report's lab lines").set_defaults(fn=cmd_evening)
    r = ls.add_parser("register", help="register a variant by hand")
    r.add_argument("--playbook", default="asx_daytrader")
    r.add_argument(
        "--set", action="append", help="dotted.path=JSON value, e.g. manage.trail_distance_r=1.5"
    )
    r.add_argument("--prompt", default="")
    r.add_argument("--why", default="")
    r.add_argument("--by", default="hand")
    r.set_defaults(fn=cmd_register)
    ls.add_parser("research-packet", help="write a research packet folder").set_defaults(
        fn=cmd_research_packet
    )
    i = ls.add_parser("ingest", help="register the proposals a research session wrote")
    i.add_argument("folder")
    i.set_defaults(fn=cmd_ingest)
    d = ls.add_parser("day", help="simulate one day for one variant and print it")
    d.add_argument("day")
    d.add_argument("--variant", default="baseline")
    d.add_argument("--books", default="bot")
    d.add_argument("--anon", action="store_true")
    d.set_defaults(fn=cmd_day)
