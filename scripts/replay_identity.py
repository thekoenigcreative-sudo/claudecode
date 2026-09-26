"""Same signals, before and after an engine change (item #18, 27 Sep 2026).

Replays days through arena/replay_ibkr.replay_day (both rule bots, the live code) with
whatever code PYTHONPATH points at, and writes one canonical record per day: the day's
result (books, trades, setup counts) plus every file the scratch arena wrote that decides
anything - the day trader's state (every setup, what fired, how far each stock was
evaluated, the bot's decision on each), the v2 bot's state and every account's orders and
fills. `compare` diffs two such folders byte for byte.

    set PYTHONPATH=<old code>\\src & python scripts\\replay_identity.py dump --out A --days ...
    set PYTHONPATH=<new code>\\src & python scripts\\replay_identity.py dump --out B --days ...
    python scripts\\replay_identity.py compare A B

A plumbing check of the engine, not a result: nothing here is a go/no-go.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

# The two fields that hold the wall clock (when the record was written), not the simulated
# clock: an event line's "ts" (log.EventLog) and an account's "created" (arena/accounts.py).
WALL_CLOCK_KEYS = {"ts", "created"}


def _scrub(x):
    if isinstance(x, dict):
        return {k: _scrub(v) for k, v in x.items() if k not in WALL_CLOCK_KEYS}
    if isinstance(x, list):
        return [_scrub(v) for v in x]
    return x


def _files(root: Path) -> dict:
    """Every JSON / JSONL file under the kept scratch folder, parsed, keyed by its path
    relative to the scratch root (the random scratch folder name removed)."""
    out = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix not in (".json", ".jsonl"):
            continue
        rel = "/".join(p.relative_to(root).parts[1:])  # drop replay_MMDD_xxxx
        text = p.read_text(encoding="utf-8")
        if p.suffix == ".json":
            out[rel] = _scrub(json.loads(text))
        else:
            out[rel] = [_scrub(json.loads(x)) for x in text.splitlines() if x.strip()]
    return out


def dump_day(day_s: str, out: str) -> tuple[str, float, str]:
    from asxbot.arena import replay_ibkr as R
    from asxbot.config import load_config
    from asxbot.lab.runner import common_inputs

    day = date.fromisoformat(day_s)
    cfg = load_config(env_file=Path(tempfile.gettempdir()) / "no-such.env")  # no secrets needed
    codes, shorts, ann, hist, universe = common_inputs(cfg, [day])
    keep = Path(tempfile.mkdtemp(prefix=f"ident_{day:%m%d}_"))
    t0 = time.perf_counter()
    res = R.replay_day(cfg, day, codes, shorts, ann, hist, keep=keep, universe=universe)
    took = time.perf_counter() - t0
    rec = {"day": day_s, "result": res, "files": _files(keep)}
    text = json.dumps(rec, indent=1, sort_keys=True, default=str)
    Path(out, f"{day_s}.json").write_text(text, encoding="utf-8")
    import shutil

    shutil.rmtree(keep, ignore_errors=True)
    return day_s, took, hashlib.sha256(text.encode()).hexdigest()[:16]


def cmd_dump(a) -> int:
    import asxbot.arena.daytrader as dt

    print("code:", dt.__file__, flush=True)
    Path(a.out).mkdir(parents=True, exist_ok=True)
    days = [d for d in a.days.split(",") if d]
    times = {}
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for day, took, digest in ex.map(dump_day, days, [a.out] * len(days)):
            times[day] = round(took, 1)
            print(f"{day} {took:7.1f}s sha {digest}", flush=True)
    Path(a.out, "_timing.json").write_text(json.dumps({"code": dt.__file__, "seconds": times},
                                                      indent=1), encoding="utf-8")  # fmt: skip
    return 0


def _canonical(p: Path) -> bytes:
    """A day's record with the wall-clock fields removed, as sorted, indented JSON."""
    rec = _scrub(json.loads(p.read_text(encoding="utf-8")))
    return json.dumps(rec, indent=1, sort_keys=True, default=str).encode()


def cmd_compare(a) -> int:
    A, B = Path(a.a), Path(a.b)
    days = sorted(p.name for p in A.glob("20*.json"))
    bad = 0
    for name in days:
        pa, pb = A / name, B / name
        if not pb.exists():
            print(f"{name}: MISSING in {B}")
            bad += 1
            continue
        ra, rb = _canonical(pa), _canonical(pb)
        if ra == rb:
            ja = json.loads(ra)
            dt_files = [k for k in ja["files"] if "daytrader" in k]
            sig = sum(len(ja["files"][k].get("signals", [])) for k in dt_files
                      if isinstance(ja["files"][k], dict))  # fmt: skip
            gaps = ja["result"].get("gaps")
            print(
                f"{name}: identical ({len(ra)} bytes, {sig} day-trader setups"
                f"{', GAP ' + str(gaps) if gaps else ''})"
            )
        else:
            bad += 1
            print(f"{name}: DIFFERENT")
            la, lb = ra.decode().splitlines(), rb.decode().splitlines()
            import difflib

            for line in list(difflib.unified_diff(la, lb, lineterm="", n=2))[:40]:
                print("   ", line)
    ta, tb = A / "_timing.json", B / "_timing.json"
    if ta.exists() and tb.exists():
        sa, sb = json.loads(ta.read_text())["seconds"], json.loads(tb.read_text())["seconds"]
        both = [d for d in sa if d in sb]
        if both:
            x, y = sum(sa[d] for d in both), sum(sb[d] for d in both)
            print(f"time over {len(both)} days: {x:.0f}s -> {y:.0f}s ({x / max(y, 1e-9):.1f}x)")
    print("ALL IDENTICAL" if not bad and days else f"{bad} of {len(days)} days differ")
    return 1 if bad or not days else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump")
    d.add_argument("--out", required=True)
    d.add_argument("--days", required=True, help="comma-separated ISO days")
    d.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 3))
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    a = ap.parse_args()
    return cmd_dump(a) if a.cmd == "dump" else cmd_compare(a)


if __name__ == "__main__":
    sys.exit(main())
