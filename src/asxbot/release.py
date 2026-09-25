"""Releases: the code the scheduled tasks run is an export of one commit, outside Google
Drive, and only scripts/deploy.py changes which one (2026-09-25, Rick's brief: "builds can't
touch the running bot").

Layout, all under %LOCALAPPDATA%\\asx-bot:
    releases\\<yyyymmdd-hhmmss-sha10>\\   a `git archive` of one commit (src, scripts, tests,
                                        config.yaml, docs); RELEASE.json says which
    releases\\CURRENT                    the name of the release the tasks run
    releases\\PREVIOUS                   the one before it, for --rollback
    bin\\<task>.pyw                      one small shim per scheduled task: resolves CURRENT,
                                        sets ASXBOT_HOME, runs CURRENT\\scripts\\<task>.pyw
    logs\\releases.log                   one line per task start: which release ran

ASXBOT_HOME (config.repo_root) is the checkout on Drive: config.yaml, .env, data/ and
reports/ stay there, so a /model change from the Trader chat, the account books and the
event log are the same files whichever release is running. The shims set it; the launchers
pass it to every child they start, with PYTHONPATH pointing at the release's src/.

This module only DESCRIBES where things are (the watcher logs it at start, the chat says
it, the tests check it). Deploying is scripts/deploy.py.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

HOME_ENV = "ASXBOT_HOME"
RELEASE_ENV = "ASXBOT_RELEASE"
# Where a shim that cannot start its task says so (26 Sep 2026: overridable, because the
# shim tests wrote real "ABORTED (probe shim)" lines into Rick's task-failures.log).
FAILURES_ENV = "ASXBOT_TASK_FAILURES"
TASK_SCRIPTS = (
    "arena_warmup", "arena_watchdog", "arena_evening", "chat", "ibgateway",
    "ibgateway_supervisor", "ibkr_preflight", "arena_filter_cost", "ibkr_fetch_history",
)  # fmt: skip


def local_base() -> Path:
    """%LOCALAPPDATA%\\asx-bot as the scheduled tasks see it (asxbot.localdir: a Claude
    Code shell sees a virtualised copy), or ASXBOT_LOCAL (the tests' scratch folder)."""
    override = os.environ.get("ASXBOT_LOCAL")
    if override:
        return Path(override)
    from asxbot.localdir import asx_local

    return asx_local()


def releases_dir() -> Path:
    return local_base() / "releases"


def bin_dir() -> Path:
    return local_base() / "bin"


def pointer(name: str, base: Path | None = None) -> str | None:
    p = (base or releases_dir()) / name
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def current_release(base: Path | None = None) -> Path | None:
    name = pointer("CURRENT", base)
    if not name:
        return None
    d = (base or releases_dir()) / name
    return d if d.is_dir() else None


def running_from() -> Path:
    """The tree this process's asxbot was imported from (a release, or the checkout)."""
    return Path(__file__).resolve().parents[2]


def release_info(tree: Path | None = None) -> dict:
    """RELEASE.json of the tree the code runs from, or {} for a plain checkout."""
    tree = tree or running_from()
    p = tree / "RELEASE.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# The shim every scheduled task runs (bin\<task>.pyw), written by scripts/deploy.py with the
# task name and HOME filled in. It imports nothing of asxbot: it must work before the release
# is on the path, and it must not change from release to release (the tasks point at it by
# name; deploy.py rewrites it only when this template changes).
SHIM_TEMPLATE = r'''"""ASXBot task shim for @NAME@: runs CURRENT/scripts/@NAME@.pyw from the release
scripts/deploy.py last pointed CURRENT at. Written by deploy.py; do not edit here."""

import os
import runpy
import sys
from datetime import datetime
from pathlib import Path

NAME = "@NAME@"
HOME = r"@HOME@"  # the checkout: config.yaml, .env, data/ and reports/
_local = os.environ.get("ASXBOT_LOCAL")
BASE = Path(_local) if _local else Path(
    os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")) / "asx-bot"
# ASXBOT_TASK_FAILURES moves it (the tests: a probe shim must not write the real one).
FAILURES = Path(os.environ.get("ASXBOT_TASK_FAILURES") or r"C:\venvs\asx-bot\task-failures.log")


def note(text):
    try:
        d = BASE / "logs"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "releases.log", "a", encoding="utf-8") as fh:
            fh.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " " + NAME + ": " + text + "\n")
    except OSError:
        pass


def fail(text):
    note("FAILED: " + text)
    try:
        FAILURES.parent.mkdir(parents=True, exist_ok=True)
        with open(FAILURES, "a", encoding="utf-8") as fh:
            fh.write(datetime.now().strftime("%Y-%m-%d %H:%M") + "  ABORTED (" + NAME
                     + " shim): " + text + "\n")
    except OSError:
        pass
    return 1


def main():
    try:
        name = (BASE / "releases" / "CURRENT").read_text(encoding="utf-8").strip()
    except OSError as e:
        return fail("no CURRENT release pointer (" + str(e) + "); run scripts\\deploy.py")
    release = BASE / "releases" / name
    script = release / "scripts" / (NAME + ".pyw")
    if not script.exists():
        return fail(str(script) + " does not exist")
    os.environ["ASXBOT_HOME"] = HOME
    os.environ["ASXBOT_RELEASE"] = str(release)
    src = str(release / "src")
    os.environ["PYTHONPATH"] = src + os.pathsep + os.environ.get("PYTHONPATH", "")
    sys.path.insert(0, src)
    note("running from " + name)
    sys.argv = [str(script)] + sys.argv[1:]
    runpy.run_path(str(script), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def shim_source(name: str, home: Path) -> str:
    return SHIM_TEMPLATE.replace("@NAME@", name).replace("@HOME@", str(home))


def describe() -> str:
    """One line for a start-up log: 'release 20260925-1930-271bb61d1f (commit 271bb61)' or
    'the checkout G:\\...' when no release is in use."""
    tree = running_from()
    info = release_info(tree)
    home = os.environ.get(HOME_ENV)
    if info:
        line = f"release {tree.name} (commit {str(info.get('commit', ''))[:7]})"
    else:
        line = f"the checkout {tree}"
    if home:
        line += f", settings and data in {home}"
    return line


__all__ = [
    "FAILURES_ENV", "HOME_ENV", "RELEASE_ENV", "SHIM_TEMPLATE", "TASK_SCRIPTS", "bin_dir",
    "current_release", "describe", "local_base", "pointer", "release_info", "releases_dir",
    "running_from", "shim_source",
]  # fmt: skip
