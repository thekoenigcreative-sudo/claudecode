"""Deploy asx-bot: the scheduled tasks run a committed snapshot outside Google Drive.

Until 25 Sep 2026 every task (the watcher, the watchdog, the evening routine, the chat, the
IB Gateway launcher, its supervisor, the pre-flight) ran straight from this working tree on
Google Drive, so a build editing files here changed what the next task start loaded, and a
build restarted the watcher mid-session. Now they run from %LOCALAPPDATA%\\asx-bot\\releases\\
<release>, a `git archive` export of one commit, and only this script changes which one.
Same scheme as bet-bot's and shop-bot's scripts/deploy.py.

    C:\\venvs\\asx-bot\\Scripts\\python.exe scripts\\deploy.py            # deploy HEAD
    C:\\venvs\\asx-bot\\Scripts\\python.exe scripts\\deploy.py <commit>   # deploy that commit
    C:\\venvs\\asx-bot\\Scripts\\python.exe scripts\\deploy.py --status   # what is live
    C:\\venvs\\asx-bot\\Scripts\\python.exe scripts\\deploy.py --rollback # back to the previous
    C:\\venvs\\asx-bot\\Scripts\\python.exe scripts\\deploy.py --test-only <tree-ish>
                                                    # export + full suite, change nothing
    C:\\venvs\\asx-bot\\Scripts\\python.exe scripts\\deploy.py --shims-only
                                                    # (re)write the task shims, change nothing else

A deploy exports the commit (from git's objects, never the working tree), runs the full test
suite inside the export with only the export on the import path, and only if every test
passes points releases\\CURRENT at it (PREVIOUS keeps the one before, for --rollback). It
writes the task shims (bin\\<task>.pyw, asxbot.release.SHIM_TEMPLATE) that the tasks run:
each resolves CURRENT at start, so the new release is used from each task's NEXT start. It
never starts, stops or restarts anything: scripts\\watcher.py does that, under the
market-hours lock (asxbot.arena.lock), and this script refuses to change CURRENT inside that
lock unless forced with a reason. Old releases are pruned, keeping the newest few plus
CURRENT and PREVIOUS, every release deployed in the last 3 days, and every release a task
last started from (logs\\releases.log): a task that runs for days - the chat - imports
modules as it goes, from the release it started in (26 Sep 2026: six deploys between
18:33 and 00:43 would have pruned the watcher's own release from under it).

Settings and data are not in the release: ASXBOT_HOME (set by the shims) points every
process at this checkout for config.yaml, .env, data/ and reports/.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import tarfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from asxbot import proc  # noqa: E402
from asxbot import release as R  # noqa: E402

SYD = ZoneInfo("Australia/Sydney")
KEEP = 5
KEEP_DAYS = 3  # never prune a release deployed more recently than this
STARTED_FROM = re.compile(r"^\S+ \S+ (\S+): running from (\S+)\s*$")


def git(*args: str) -> str:
    r = proc.run(["git", "-C", str(REPO), *args], capture_output=True, text=True,
                 encoding="utf-8", errors="replace", check=False)  # fmt: skip
    if r.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def set_pointer(name: str, value: str, base: Path) -> None:
    tmp = base / f"{name}.tmp"
    tmp.write_text(value + "\n", encoding="utf-8")
    os.replace(tmp, base / name)


def export(treeish: str, dest: Path) -> None:
    """`git archive` of treeish into dest: exactly what git has, whatever the working tree
    (edited, stashed or locked by Drive) looks like."""
    r = proc.run(["git", "-C", str(REPO), "archive", "--format=tar", treeish],
                 capture_output=True, check=True)  # fmt: skip
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tar:
        tar.extractall(dest, filter="data")


def run_tests(where: Path) -> bool:
    """The full suite, inside the export, importing asxbot from the export only."""
    env = {k: v for k, v in os.environ.items() if k not in (R.HOME_ENV, R.RELEASE_ENV)}
    env["PYTHONPATH"] = str(where / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    probe = proc.run([sys.executable, "-c", "import asxbot; print(asxbot.__file__)"],
                     cwd=where, env=env, capture_output=True, text=True, check=False)  # fmt: skip
    loaded = Path(probe.stdout.strip() or ".").resolve()
    if not str(loaded).lower().startswith(str(where.resolve()).lower()):
        print(f"REFUSING: the tests would import asxbot from {loaded}, not the export")
        return False
    print(f"running the full suite in {where} (asxbot from {loaded.parent})")
    r = proc.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                 cwd=where, env=env, capture_output=True, text=True, encoding="utf-8",
                 errors="replace", check=False)  # fmt: skip
    tail = (r.stdout + r.stderr).strip().splitlines()
    print("\n".join(tail[-15:]))
    return r.returncode == 0


def write_shims(home: Path, base: Path) -> list[str]:
    """bin\\<task>.pyw for every task, rewritten only when the text differs."""
    bin_dir = base / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    changed = []
    for name in R.TASK_SCRIPTS:
        p = bin_dir / f"{name}.pyw"
        text = R.shim_source(name, home)
        if p.exists() and p.read_text(encoding="utf-8") == text:
            continue
        tmp = p.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
        changed.append(p.name)
    return changed


def status(base: Path) -> int:
    cur, prev = R.pointer("CURRENT", base), R.pointer("PREVIOUS", base)
    for label, name in (("CURRENT", cur), ("PREVIOUS", prev)):
        info = R.release_info(base / name) if name else {}
        print(f"{label}: {name or '(none)'}"
              + (f"  commit {info.get('commit', '?')[:12]}, deployed {info.get('deployed_at')}:"
                 f" {info.get('subject', '')}" if info else ""))  # fmt: skip
    head = git("rev-parse", "HEAD")
    if cur:
        live = R.release_info(base / cur).get("commit")
        print("HEAD is the deployed release (a task started before the deploy runs it from "
              "its next start)." if live == head else f"HEAD ({head[:12]}) is NOT deployed yet.")
    shims = sorted(p.name for p in R.bin_dir().glob("*.pyw")) if R.bin_dir().exists() else []
    print(f"shims in {R.bin_dir()}: {', '.join(shims) or 'none'}")
    return 0


def release_time(name: str) -> datetime | None:
    """When a release was made, from its name (<yyyymmdd-hhmmss-sha10>), or None."""
    try:
        return datetime.strptime(name[:15], "%Y%m%d-%H%M%S").replace(tzinfo=SYD)
    except ValueError:
        return None


def last_started(log: Path) -> dict[str, str]:
    """task -> the release it last started from, per logs\\releases.log (the shims write a
    "running from <release>" line at every task start)."""
    out: dict[str, str] = {}
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        m = STARTED_FROM.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def prune(base: Path, now: datetime | None = None) -> list[str]:
    """Delete old releases; returns the names deleted. Kept: CURRENT, PREVIOUS, the newest
    KEEP, anything made in the last KEEP_DAYS days or whose age cannot be read, and the
    release each task last started from (it may still be running from it)."""
    now = now or datetime.now(SYD)
    keep = {R.pointer("CURRENT", base), R.pointer("PREVIOUS", base)}
    keep |= set(last_started(base.parent / "logs" / "releases.log").values())
    dirs = sorted((d for d in base.iterdir() if d.is_dir() and not d.name.endswith(".partial")),
                  key=lambda d: d.name)  # fmt: skip
    gone = []
    for d in dirs[:-KEEP]:
        made = release_time(d.name)
        if d.name in keep or made is None or now - made < timedelta(days=KEEP_DAYS):
            continue
        shutil.rmtree(d, ignore_errors=True)
        gone.append(d.name)
    return gone


def lock_or_exit(force: bool, reason: str, what: str) -> None:
    """The market-hours lock (asxbot.arena.lock): CURRENT is not changed in market hours
    without a forced, written reason. An open position does not block a deploy (nothing is
    restarted by it), but it is said."""
    from asxbot.arena.lock import LOCK_FROM, in_market_lock, lock_end, open_exposure

    now = datetime.now(SYD)
    if in_market_lock(now) and not (force and reason.strip()):
        sys.exit(
            f"REFUSED: {now:%H:%M} Sydney is inside the market-hours lock "
            f"({LOCK_FROM:%H:%M}-{lock_end(now.date()):%H:%M} on a "
            f"trading day); {what} would change what the next task start runs. "
            'Override with --force --reason "why" (recorded in RELEASE.json).'
        )
    exposed = open_exposure(REPO / "data")
    if exposed:
        print("note: an arena position or order is open; the running watcher keeps its code "
              "until scripts\\watcher.py restarts it (refused while anything is open): "
              + "; ".join(exposed))  # fmt: skip
    if in_market_lock(now):
        print(f"FORCED in market hours ({now:%H:%M}): {reason.strip()}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("rev", nargs="?", default="HEAD")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--shims-only", action="store_true", help="(re)write bin\\*.pyw only")
    ap.add_argument("--note", default="", help="what changed, in a line, kept in RELEASE.json")
    ap.add_argument("--force", action="store_true", help="deploy inside the market-hours lock")
    ap.add_argument("--reason", default="",
                    help="why the lock is overridden (required with --force)")  # fmt: skip
    ap.add_argument("--test-only", action="store_true",
                    help="export rev (any tree-ish) and run the suite; change nothing")  # fmt: skip
    args = ap.parse_args()
    base = R.releases_dir()
    base.mkdir(parents=True, exist_ok=True)

    if args.status:
        return status(base)
    if args.shims_only:
        changed = write_shims(REPO, R.local_base())
        print(f"shims written to {R.bin_dir()}: {', '.join(changed) or 'all up to date'}")
        return 0
    if args.rollback:
        lock_or_exit(args.force, args.reason, "a rollback")
        cur, prev = R.pointer("CURRENT", base), R.pointer("PREVIOUS", base)
        if not prev or not (base / prev).is_dir():
            sys.exit("no previous release to roll back to")
        set_pointer("CURRENT", prev, base)
        if cur:
            set_pointer("PREVIOUS", cur, base)
        print(f"CURRENT is now {prev} (was {cur}). Each task uses it from its next start; "
              "the watcher only through scripts\\watcher.py restart (market-hours lock).")
        return 0

    if args.test_only:
        where = base.parent / "deploy-test"
        shutil.rmtree(where, ignore_errors=True)
        export(args.rev, where)
        ok = run_tests(where)
        shutil.rmtree(where, ignore_errors=True)
        print("TESTS PASS" if ok else "TESTS FAILED")
        return 0 if ok else 1

    lock_or_exit(args.force, args.reason, "a deploy")
    sha = git("rev-parse", "--verify", f"{args.rev}^{{commit}}")
    subject = git("log", "-1", "--format=%s", sha)
    name = f"{datetime.now(SYD):%Y%m%d-%H%M%S}-{sha[:10]}"
    tmp = base / f"{name}.partial"
    export(sha, tmp)
    if not run_tests(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"NOT DEPLOYED: a test failed on {sha[:12]}. CURRENT is unchanged "
              f"({R.pointer('CURRENT', base)}).")  # fmt: skip
        return 1
    (tmp / "RELEASE.json").write_text(json.dumps({
        "commit": sha, "subject": subject,
        "deployed_at": datetime.now(SYD).isoformat(timespec="seconds"),
        "from": str(REPO), "tests": "passed", "note": args.note.strip(),
        "forced_reason": args.reason.strip() if args.force else "",
    }, indent=2) + "\n", encoding="utf-8")  # fmt: skip
    os.replace(tmp, base / name)
    cur = R.pointer("CURRENT", base)
    if cur:
        set_pointer("PREVIOUS", cur, base)
    set_pointer("CURRENT", name, base)
    changed = write_shims(REPO, R.local_base())
    prune(base)
    print(f"DEPLOYED {sha[:12]} ({subject}) as {base / name}.")
    print(f"shims: {', '.join(changed) or 'unchanged'} in {R.bin_dir()}")
    print("Nothing was restarted. Each task runs the new release from its next start; the "
          "watcher only through scripts\\watcher.py restart (refused in market hours or with a "
          "position open).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
