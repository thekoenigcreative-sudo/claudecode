"""Add fake capital to the arena accounts, once, without disturbing anything else.

Written 2026-09-23 for one job: the arena accounts opened at $10,000 on 22 Sep, before
`arena.starting_aud` became $20,000. Rick decided the warm-up should match the $20,000 setup
and keep A1M and the day's history, so each account gets $10,000 more - not a reset.

What it changes, per account, and nothing else:
  * `cash` and `starting_cash` both rise by the amount, so profit and loss (equity less
    starting cash) is exactly what it was;
  * every daily mark's `equity` and `cash` rise by the same amount. Without this the next
    day's loss limit would be measured against a $10,000 start on a $20,000 account (a 50%
    loss before it tripped), and the scoreboard would read the top-up as a +100% day.

It refuses to run, and writes nothing, unless:
  * no arena process is running - not the watcher, not the evening routine, not any
    `asxbot arena` command - and neither scheduled task reports itself Running;
  * the evening routine for the latest session has finished: its report step has returned
    in arena_evening.log (in the local logs folder), and every account has that day's mark;
  * every account is where this change expects it: starting cash plus the amount equals
    `arena.starting_aud`. A second run therefore refuses, rather than adding it twice.

Before writing, each account file and marks file is copied to data/arena/backups/. After
writing, both are read back and compared field by field with the backup.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as time_cls
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot import proc as hidden
from asxbot.arena.accounts import AccountStore
from asxbot.arena.hours import evening_slot
from asxbot.arena.levels import load_playbook
from asxbot.config import Config, load_config
from asxbot.io import write_text_atomic
from asxbot.log import EventLog, setup_logging

SYD = ZoneInfo("Australia/Sydney")
AMOUNT = 10_000.0
TASKS = ("ASXBot Arena Warmup", "ASXBot Arena Evening")
# Anything that could be reading or writing the books: the scheduled tasks' scripts (the
# PowerShell ones, and the hidden pythonw launcher), and any `asxbot arena ...` command, the
# watcher (`arena watch`) above all. The watchdog (arena_watchdog.pyw) only reads, and is
# deliberately not matched.
ARENA_PROCESS = re.compile(
    r"arena_(warmup|evening)\.(ps1|pyw)|asxbot(\.exe)?\"?\s+arena\s|\barena\s+watch\b", re.I
)
REPORT_STEP = "--- asxbot arena report"
STEP_DONE = re.compile(r"^--- exit -?\d+ ---$")
ANY_HEAD = re.compile(r"^=== evening report ")


class Refused(RuntimeError):
    """A safety check failed. Nothing was written."""


@dataclass
class Plan:
    name: str
    account_path: Path
    marks_path: Path
    raw: dict
    marks: list[dict]


# -- the checks ---------------------------------------------------------------------
def arena_processes(listing: list[tuple[int, int, str]], me: int) -> list[str]:
    """Command lines of running arena processes, ignoring this one and its ancestors."""
    parents = {pid: ppid for pid, ppid, _ in listing}
    mine, pid = set(), me
    while pid and pid not in mine:
        mine.add(pid)
        pid = parents.get(pid, 0)
    return [
        f"pid {pid}: {cmd.strip()[:160]}"
        for pid, _, cmd in listing
        if pid not in mine and cmd and ARENA_PROCESS.search(cmd)
    ]


def process_listing() -> list[tuple[int, int, str]]:
    """Every process's id, parent and command line, from Windows. Raises if it cannot."""
    ps = (
        "Get-CimInstance Win32_Process | ForEach-Object "
        '{ "$($_.ProcessId)`t$($_.ParentProcessId)`t$($_.CommandLine)" }'
    )
    r = hidden.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )  # fmt: skip
    if r.returncode != 0 or not r.stdout.strip():
        raise Refused(f"could not list running processes: {r.stderr.strip()[:200]}")
    out = []
    for line in r.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[0].isdigit():
            out.append((int(parts[0]), int(parts[1] or 0), parts[2]))
    return out


def task_status(name: str) -> list[str]:
    """The Status column for each trigger of a scheduled task. Raises if it cannot tell."""
    r = hidden.run(
        ["schtasks", "/query", "/tn", name, "/fo", "csv", "/nh"],
        capture_output=True, text=True, errors="replace", timeout=60,
    )  # fmt: skip
    if r.returncode != 0:
        raise Refused(f"could not query the scheduled task {name!r}: {r.stderr.strip()[:200]}")
    rows = [row for row in csv.reader(io.StringIO(r.stdout)) if len(row) >= 3]
    if not rows:
        raise Refused(f"the scheduled task {name!r} returned no status")
    return [row[2].strip() for row in rows]


def evening_day(now: datetime) -> date:
    """The session whose evening routine must have finished: today's, or before 07:00 (and
    on a weekend) the last weekday's."""
    d = now.date()
    if now.time() < time_cls(7, 0):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def evening_finished(log_text: str, day: date, slot: time_cls) -> str | None:
    """None if the evening routine for `day` ran in its slot and its report step returned.
    Otherwise, why not."""
    lines = log_text.splitlines()
    head = re.compile(rf"^=== evening report {day.isoformat()} (\d\d):(\d\d) ===$")
    start = None
    for i, line in enumerate(lines):
        m = head.match(line.strip())
        if m and (int(m.group(1)), int(m.group(2))) >= (slot.hour, slot.minute):
            start = i
    if start is None:
        return f"no evening report for {day} at or after {slot:%H:%M} in the evening log"
    section = lines[start + 1 :]
    # Only this run's lines: a later day's run must not answer for this one.
    section = section[: next((i for i, ln in enumerate(section) if ANY_HEAD.match(ln)), None)]
    step = next((i for i, ln in enumerate(section) if ln.startswith(REPORT_STEP)), None)
    if step is None:
        return f"the evening routine for {day} has not reached its report step yet"
    if any(STEP_DONE.match(ln.strip()) for ln in section):
        # The hidden launcher (arena_evening.pyw, from 2026-09-23) writes output as it
        # arrives, so a line after the header proves nothing; it closes each step with an
        # exit line, and only that says the report returned.
        if not any(STEP_DONE.match(ln.strip()) for ln in section[step + 1 :]):
            return f"the evening report for {day} has started but not returned"
        return None
    # arena_evening.ps1 writes the step's header, runs it, then writes its output. A header
    # with nothing after it is a report still running (or one that was killed mid-way).
    if step + 1 >= len(section):
        return f"the evening report for {day} has started but not returned"
    return None


# -- the change ---------------------------------------------------------------------
def plan(cfg: Config, store: AccountStore, day: date, amount: float) -> list[Plan]:
    """Read every account and check it is where this change expects it."""
    expected = float(cfg.get("arena.starting_aud"))
    plans = []
    # The books this one-off was written for (23 Sep): announcements v1's. Since v1 was
    # retired on 2026-09-24 they are no longer the active playbook's, so they are named.
    for pb in [load_playbook(cfg, "asx_announcements")]:
        for name in (pb.agent_account, pb.bot_account):
            path = store.path(name)
            if not path.exists():
                raise Refused(f"account file {path} does not exist")
            raw = json.loads(path.read_text(encoding="utf-8"))
            start = float(raw["starting_cash"])
            if abs(start + amount - expected) > 0.005:
                raise Refused(
                    f"{name} starts at {start:,.2f}; adding {amount:,.2f} would make "
                    f"{start + amount:,.2f}, not arena.starting_aud {expected:,.2f}. "
                    "Already applied, or not the account this was written for."
                )
            mpath = store.marks_path(name)
            marks = [
                json.loads(ln)
                for ln in (mpath.read_text(encoding="utf-8").splitlines() if mpath.exists() else [])
                if ln.strip()
            ]
            if not any(m.get("day") == day.isoformat() for m in marks):
                raise Refused(f"{name} has no mark for {day}: the evening mark has not run")
            plans.append(Plan(name, path, mpath, raw, marks))
    if not plans:
        raise Refused("no enabled playbook, so no arena account to change")
    return plans


def apply(p: Plan, amount: float) -> tuple[dict, list[dict]]:
    """The changed account and marks. Only cash, starting cash and marks' equity and cash."""
    raw = dict(p.raw)
    raw["cash"] = float(raw["cash"]) + amount
    raw["starting_cash"] = float(raw["starting_cash"]) + amount
    marks = [
        {**m, "equity": round(float(m["equity"]) + amount, 2),
         "cash": round(float(m["cash"]) + amount, 2)}
        for m in p.marks
    ]  # fmt: skip
    return raw, marks


def only_these_changed(before: dict, after: dict, keys: set[str]) -> list[str]:
    """Keys outside `keys` whose values differ, or that appeared or vanished."""
    return sorted(
        k for k in set(before) | set(after) if k not in keys and before.get(k) != after.get(k)
    )


def run(cfg: Config, now: datetime, amount: float, dry_run: bool, checks: bool = True) -> int:
    log = setup_logging(cfg.logs_dir)
    store = AccountStore(cfg.data_dir)
    day = evening_day(now)

    if checks:
        running = arena_processes(process_listing(), os.getpid())
        if running:
            raise Refused("an arena process is running:\n  " + "\n  ".join(running))
        for task in TASKS:
            states = task_status(task)
            if any(s.lower() == "running" for s in states):
                raise Refused(f"the scheduled task {task!r} is running")
        evening_log = cfg.logs_dir / "arena_evening.log"
        text = evening_log.read_text(encoding="utf-8-sig") if evening_log.exists() else ""
        why = evening_finished(text, day, evening_slot(cfg, day))
        if why:
            raise Refused(why)
        print(f"checks passed: no arena process or task running; the {day} evening finished")

    plans = plan(cfg, store, day, amount)
    for p in plans:
        raw, marks = apply(p, amount)
        print(
            f"{p.name}: cash {float(p.raw['cash']):,.2f} -> {raw['cash']:,.2f}; starting "
            f"{float(p.raw['starting_cash']):,.2f} -> {raw['starting_cash']:,.2f}; "
            f"{len(marks)} mark(s) restated by {amount:+,.2f} in equity and cash"
        )
    if dry_run:
        print("dry run: nothing written")
        return 0

    backup = cfg.data_dir / "arena" / "backups" / f"add_capital_{now:%Y%m%d_%H%M%S}"
    backup.mkdir(parents=True, exist_ok=False)
    for p in plans:
        shutil.copy2(p.account_path, backup / p.account_path.name)
        if p.marks_path.exists():
            shutil.copy2(p.marks_path, backup / p.marks_path.name)

    events = EventLog(cfg.data_dir)
    for p in plans:
        raw, marks = apply(p, amount)
        write_text_atomic(json.dumps(raw, indent=2), p.account_path)
        write_text_atomic("\n".join(json.dumps(m) for m in marks) + "\n", p.marks_path)

        # Read back what is on disk, and prove nothing else moved.
        got = json.loads(p.account_path.read_text(encoding="utf-8"))
        stray = only_these_changed(p.raw, got, {"cash", "starting_cash"})
        got_marks = [json.loads(ln) for ln in p.marks_path.read_text(encoding="utf-8").splitlines()]
        stray += [
            f"mark {m0.get('day')}: {k}"
            for m0, m1 in zip(p.marks, got_marks, strict=True)
            for k in only_these_changed(m0, m1, {"equity", "cash"})
        ]
        if stray or abs(got["cash"] - float(p.raw["cash"]) - amount) > 1e-6:
            raise Refused(f"{p.name} did not read back as written ({stray}); restore from {backup}")
        store.open(p.name, got["playbook"], got["kind"], int(got["level"]), 0.0)  # it loads

        rec = {
            "account": p.name, "event": "capital_added", "amount": amount,
            "cash_before": float(p.raw["cash"]), "cash_after": got["cash"],
            "starting_before": float(p.raw["starting_cash"]),
            "starting_after": got["starting_cash"], "marks_restated": len(marks),
            "backup": str(backup),
        }  # fmt: skip
        events.append("arena_accounts", rec)
        log.info("arena capital added: %s", rec)
    print(f"done. Backups in {backup}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="run every check, write nothing")
    args = ap.parse_args(argv)
    cfg = load_config()
    try:
        return run(cfg, datetime.now(SYD), AMOUNT, args.dry_run)
    except Refused as e:
        print(f"REFUSED - nothing was written: {e}")
        return 2
