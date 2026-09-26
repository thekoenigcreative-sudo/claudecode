"""`asxbot lab tick`: the next unit of practice, whenever the market is closed (PRACTICE_LAB.md 5).

Each tick runs for at most `max_minutes` and is resumable at day granularity (every simulated
day is its own file), so the Foreman can start one every evening and weekend hour, and 06:45
on a trading day always finds the machine free. Order of work:
  1. the frozen rule bots' baseline on TUNE and VALIDATE (what every variant must beat);
  2. shadow: yesterday's (and any missed) forward day for every promoted variant;
  3. finals: a shadow variant with 10 good shadow days gets its one locked-test run;
  4. screens (TUNE) of registered variants, simplest first;
  5. validations (VALIDATE) of screened variants -> promotion to shadow;
  6. the contamination measurement (once, then monthly);
  7. nothing left -> status.json says research is needed (the Foreman queues a session).
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot import proc
from asxbot.lab import agentcall, score, splits, store
from asxbot.lab.variants import BASELINE, Registry
from asxbot.log import get_logger

log = get_logger("asxbot.lab")
SYD = ZoneInfo("Australia/Sydney")
BASE_VARIANT = {"id": BASELINE, "playbook": "asx_daytrader", "overrides": {}, "agent_prompt": ""}
SHADOW_DAYS_FOR_FINAL = 10
WORKERS = int(os.environ.get("ASXBOT_LAB_WORKERS", "6"))
AGENT_WORKERS = 2


# --------------------------------------------------------------------------------------------
# when the lab may run
# --------------------------------------------------------------------------------------------
def market_quiet(now: datetime | None = None) -> tuple[bool, str]:
    """Never 06:45-19:35 on an ASX trading day (the watcher's time)."""
    now = (now or datetime.now(SYD)).astimezone(SYD)
    from asxbot.arena.replay_ibkr import sessions

    trading = bool(sessions(now.date(), now.date()))
    hm = now.strftime("%H:%M")
    if trading and "06:45" <= hm < "19:35":
        return False, "market day, watcher's hours (06:45-19:35)"
    return True, ""


def stop_time(now: datetime | None = None) -> datetime:
    """When this tick must be finished: the next trading day's 06:45, at the latest."""
    from asxbot.arena.replay_ibkr import sessions

    now = (now or datetime.now(SYD)).astimezone(SYD)
    d = now.date()
    for i in range(0, 8):
        day = d + timedelta(days=i)
        at = datetime.combine(day, datetime.min.time(), tzinfo=SYD).replace(hour=6, minute=45)
        if at > now and sessions(day, day):
            return at
    return now + timedelta(hours=12)


# --------------------------------------------------------------------------------------------
# running days
# --------------------------------------------------------------------------------------------
def salt() -> str:
    p = store.lab_local() / "salt.txt"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(secrets.token_hex(16), encoding="utf-8")
    return p.read_text(encoding="utf-8").strip()


def common_inputs(cfg, days: list[date]):
    from asxbot.arena import replay_ibkr as R
    from asxbot.data.universe import asx200_codes

    codes, universe = R.arena_universe(cfg)
    shorts = asx200_codes(cfg.data_dir, cfg.get("collector.user_agent"))
    ann = R.announcements(cfg, min(days) - timedelta(days=5), max(days))
    hist = R.HistoryBars(
        R.history_root(), str((cfg.get("arena.fill") or {}).get("minute_price", "close"))
    )
    return codes, shorts, ann, hist, universe


def run_days_here(
    cfg,
    variant: dict,
    window: str,
    days: list[date],
    books: list[str],
    anon: bool,
    allow_locked: bool = False,
) -> int:
    """Simulate `days` in this process, one result file per day. Returns days written."""
    from asxbot.lab import sim

    if not days:
        return 0
    codes, shorts, ann, hist, universe = common_inputs(cfg, days)
    order = (
        {d: i + 1 for i, d in enumerate(splits.sessions_in(window, until=max(days)))}
        if window != "sample"
        else {}
    )
    n = 0
    for day in days:
        out = store.results_dir(variant["id"], window, anon) / f"{day.isoformat()}.json"
        if out.exists():
            continue
        started = time.monotonic()
        try:
            rec = sim.run_day(
                cfg,
                day,
                variant,
                books=books,
                anon=anon,
                codes=codes,
                shorts=shorts,
                ann=ann,
                hist=hist,
                universe=universe,
                salt=salt(),
                day_number=order.get(day, 0),
                allow_locked=allow_locked,
            )
        except agentcall.UsageStop:
            raise
        except Exception as e:  # noqa: BLE001 - a day that fails is a gap, named in the report
            log.exception("lab day %s %s failed", variant["id"], day)
            rec = {
                "day": day.isoformat(),
                "gaps": [f"lab run failed: {type(e).__name__}: {e}"],
                "variant": variant["id"],
            }
        rec["seconds"] = round(time.monotonic() - started, 1)
        store.write_json(out, rec)
        n += 1
    return n


def run_days(
    cfg,
    variant: dict,
    window: str,
    days: list[date],
    books: list[str],
    anon: bool = False,
    allow_locked: bool = False,
    workers: int | None = None,
    deadline: float | None = None,
) -> str:
    """Simulate the days not done yet, in parallel worker processes.
    Returns 'done' | 'partial' (deadline) | 'usage' (Rick's weekly stop)."""
    splits.check_days(days, allow_locked)
    done = store.day_files(variant["id"], window, anon)
    todo = [d for d in days if d.isoformat() not in done]
    if not todo:
        return "done"
    n = max(1, min(workers or (AGENT_WORKERS if "agent" in books else WORKERS), len(todo)))
    # Rounds of n days, so a deadline is kept to within one day's run.
    while todo:
        if deadline is not None and time.monotonic() > deadline:
            return "partial"
        batch, todo = todo[:n], todo[n:]
        if n == 1:
            try:
                run_days_here(cfg, variant, window, batch, books, anon, allow_locked)
            except agentcall.UsageStop:
                return "usage"
            continue
        procs = []
        job_dir = store.lab_local() / "jobs"
        job_dir.mkdir(parents=True, exist_ok=True)
        for d in batch:
            job = (
                job_dir / f"{variant['id']}_{window}{'-anon' if anon else ''}_{d.isoformat()}.json"
            )
            store.write_json(
                job,
                {
                    "variant": variant,
                    "window": window,
                    "days": [d.isoformat()],
                    "books": books,
                    "anon": anon,
                    "allow_locked": allow_locked,
                },
            )
            logf = open(job.with_suffix(".log"), "w", encoding="utf-8")  # noqa: SIM115
            env = dict(os.environ)
            env.pop("PYTHONHOME", None)
            cmd = [sys.executable, "-m", "asxbot.cli", "lab", "worker", "--job", str(job)]
            procs.append(
                (proc.popen(cmd, cwd=str(cfg.root), stdout=logf, stderr=proc.STDOUT, env=env), logf)
            )
        codes = []
        for p, logf in procs:
            codes.append(p.wait())
            logf.close()
        if 3 in codes:
            return "usage"
    return "done"


def worker_main(cfg, job_path: Path) -> int:
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    days = [date.fromisoformat(d) for d in job["days"]]
    try:
        run_days_here(
            cfg,
            job["variant"],
            job["window"],
            days,
            job["books"],
            job["anon"],
            job.get("allow_locked", False),
        )
    except agentcall.UsageStop as e:
        print(f"usage stop: {e}", flush=True)
        return 3
    return 0


# --------------------------------------------------------------------------------------------
# evaluating
# --------------------------------------------------------------------------------------------
def cards(
    variant_id: str,
    window: str,
    playbook: str,
    books: list[str],
    anon: bool = False,
    days: list[date] | None = None,
) -> dict:
    recs = store.load_days(variant_id, window, anon)
    if days is not None:
        want = {d.isoformat() for d in days}
        recs = [r for r in recs if r["day"] in want]
    return {b: score.card(recs, playbook, b) for b in books}


def baseline_card(window: str, playbook: str, days: list[date] | None = None) -> dict:
    return cards(BASELINE, window, playbook, ["bot"], days=days)["bot"]


def books_for(v: dict) -> list[str]:
    return list(v.get("books") or ["bot"])


def evaluate(reg: Registry, v: dict, window: str, stage_ok: str, stage_fail: str) -> dict:
    books = books_for(v)
    anon = window == "tune" and "agent" in books
    days = splits.sessions_in(window)
    res = cards(v["id"], window, v["playbook"], books, anon=anon, days=days)
    base = baseline_card(window, v["playbook"], days)
    n_val = reg.count_validated() + (
        1 if window == "validate" and "validate" not in v["results"] else 0
    )
    verdicts = {}
    for b, s in res.items():
        ok, why = score.gate("validate" if window == "validate" else "screen", s, base, n_val)
        verdicts[b] = {
            "passed": ok,
            "why": why,
            "card": {k: s[k] for k in s if k != "daily"},
            "daily": s["daily"],
        }
    v["results"][window] = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "anon": anon,
        "books": verdicts,
        "baseline_pnl": base["pnl_after_fees"],
        "n_validated": n_val if window == "validate" else None,
    }
    passed = [b for b, r in verdicts.items() if r["passed"]]
    reg.set_stage(
        v,
        stage_ok if passed else stage_fail,
        "; ".join(f"{b}: {'; '.join(r['why'])}" for b, r in verdicts.items()),
    )
    reg.log_run(
        {
            "event": window,
            "variant": v["id"],
            "passed": passed,
            "pnl": {b: r["card"]["pnl_after_fees"] for b, r in verdicts.items()},
        }
    )
    return v


def status_path(cfg) -> Path:
    return store.lab_data(cfg) / "status.json"


def write_status(cfg, **kw) -> dict:
    cur = store.read_json(status_path(cfg), {}) or {}
    cur.update(kw)
    cur["updated"] = datetime.now(SYD).isoformat(timespec="seconds")
    store.write_json(status_path(cfg), cur)
    return cur


# --------------------------------------------------------------------------------------------
# the tick
# --------------------------------------------------------------------------------------------
def tick(cfg, max_minutes: float = 55.0, force: bool = False, now: datetime | None = None) -> str:
    ok, why = market_quiet(now)
    if not ok and not force:
        return f"not now: {why}"
    lock = store.lab_local() / "tick.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        try:
            pid = int(lock.read_text().strip() or 0)
        except (OSError, ValueError):
            pid = 0
        if pid and _alive(pid):
            return f"another tick is running (pid {pid})"
        lock.unlink(missing_ok=True)
        return tick(cfg, max_minutes, force, now)
    try:
        hard_stop = stop_time(now).timestamp() - time.time()
        deadline = time.monotonic() + max(60.0, min(max_minutes * 60, hard_stop - 20 * 60))
        return _work(cfg, deadline)
    finally:
        lock.unlink(missing_ok=True)


def _alive(pid: int) -> bool:
    """A tick killed without its `finally` (a reboot, a kill) leaves its lock behind. Until
    26 Sep 2026 this asked psutil, which is not in the venv, and without it read every lock as
    alive: after one such kill no tick would ever have run again."""
    from asxbot.arena.watchdog import pid_alive

    return pid_alive(pid)


def _work(cfg, deadline: float) -> str:
    from asxbot.lab import report, shadow, winnerflow

    reg = Registry(store.lab_data(cfg))
    did = []
    # 0. the proper trading simulator gets the first half of every tick (CLOUD_BRIEF: the
    # search, the AI trader and the AI experiments run on it); the time machine the rest
    from asxbot.lab.tsim import nightly

    did += nightly.work(cfg, time.monotonic() + (deadline - time.monotonic()) / 2)
    # 1. the baseline the gates compare with
    for window in ("tune", "validate"):
        r = run_days(
            cfg, BASE_VARIANT, window, splits.sessions_in(window), ["bot"], deadline=deadline
        )
        did.append(f"baseline {window}: {r}")
        if r != "done":
            return _finish(cfg, reg, did, busy=True)
    # 2. shadow days for promoted variants
    r = shadow.catch_up(cfg, reg, deadline)
    if r:
        did.append(r)
    # 3. finals
    for v in [v for v in reg.all() if v["stage"] == "shadow"]:
        if shadow.ready_for_final(cfg, v):
            did.append(winnerflow.final(cfg, reg, v, deadline))
            return _finish(cfg, reg, did, busy=True)
    # 4-5. screens and validations, simplest first
    todo = sorted(
        [v for v in reg.all() if v["stage"] in ("registered", "screened")],
        key=lambda v: (v["stage"] != "screened", v.get("complexity", 0), v["id"]),
    )
    for v in todo:
        window = "tune" if v["stage"] == "registered" else "validate"
        books = books_for(v)
        anon = window == "tune" and "agent" in books
        r = run_days(
            cfg, v, window, splits.sessions_in(window), books, anon=anon, deadline=deadline
        )
        did.append(f"{v['id']} {window}: {r}")
        if r == "usage":
            return _finish(cfg, reg, did, busy=False, why="weekly Claude usage at Rick's stop")
        if r != "done":
            return _finish(cfg, reg, did, busy=True)
        if window == "tune":
            evaluate(reg, v, "tune", "screened", "screen_failed")
        else:
            evaluate(reg, v, "validate", "validated", "validate_failed")
            if v["stage"] == "validated":
                shadow.promote(cfg, reg, v)
        if time.monotonic() > deadline:
            return _finish(cfg, reg, did, busy=True)
    # 6. contamination, once there is time and a baseline agent to compare
    from asxbot.lab import contamination

    r = contamination.maybe_run(cfg, deadline)
    if r:
        did.append(r)
        return _finish(cfg, reg, did, busy=True)
    # 7. whatever time is left: the trading simulator's search again
    did += nightly.work(cfg, deadline)
    report.write(cfg, reg)
    return _finish(cfg, reg, did, busy=False)


def _finish(cfg, reg: Registry, did: list[str], busy: bool, why: str = "") -> str:
    pending = [v["id"] for v in reg.all() if v["stage"] in ("registered", "screened")]
    write_status(
        cfg,
        last_tick=did,
        pending=pending,
        research_needed=not busy and not pending and not why,
        held=why,
        variants_tried=len(reg.all()),
        shadow=[v["id"] for v in reg.all() if v["stage"] == "shadow"],
    )
    return "; ".join(did) or "nothing to do"
