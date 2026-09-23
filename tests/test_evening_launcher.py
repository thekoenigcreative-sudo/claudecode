"""scripts/arena_evening.pyw, the windowless evening routine, run against a stand-in asxbot.

The launcher is copied into a temporary repo with its asxbot command pointed at a small
Python stand-in, so nothing here touches the real books, the real log or Telegram.
"""

import importlib.machinery
import importlib.util
import os
import re
import subprocess
import sys
import time as clock
from datetime import date, time
from pathlib import Path

import pytest

from asxbot.arena import capital as C

LAUNCHER = Path(__file__).resolve().parents[1] / "scripts" / "arena_evening.pyw"
HEADER = re.compile(r"^=== evening report \d{4}-\d\d-\d\d \d\d:\d\d ===$")

# Behaves like `asxbot arena ...` as far as the launcher can tell: prints, exits with a code.
STANDIN = r"""
import os, sys, time
args = sys.argv[1:]
with open(os.environ["STANDIN_CALLS"], "a", encoding="utf-8") as fh:
    fh.write(" ".join(args) + "\n")
if args == ["arena", "evening-due"]:
    print("due or not")
    sys.exit(int(os.environ.get("STANDIN_DUE", "0")))
if args[:2] == ["arena", "resolve"]:
    print("nothing to resolve")
    print("WARNING something on stderr", file=sys.stderr)
    sys.exit(int(os.environ.get("STANDIN_RESOLVE", "0")))
if args[:2] == ["arena", "mark"]:
    print("asx_announcements__agent: equity 10,230.89 - ünïcode ok")
    sys.exit(0)
if args[:2] == ["arena", "report"]:
    print("the report, first line")
    if os.environ.get("STANDIN_HANG"):
        with open(os.environ["STANDIN_HANG"], "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        time.sleep(120)
    print("[sent to Telegram: 1 message(s)]")
    sys.exit(0)
sys.exit(9)
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A temporary repo holding a copy of the launcher wired to the stand-in."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "config.yaml").write_text("broker: sim\n", encoding="utf-8")
    standin = tmp_path / "asxbot_standin.py"
    standin.write_text(STANDIN, encoding="utf-8")
    src = LAUNCHER.read_text(encoding="utf-8")
    wired = src.replace(
        'ASXBOT = [str(VENV / "asxbot.exe")]', f"ASXBOT = [{sys.executable!r}, {str(standin)!r}]"
    ).replace(
        r'FAILURES = Path(r"C:\venvs\asx-bot\task-failures.log")',
        f"FAILURES = Path({str(tmp_path / 'task-failures.log')!r})",
    )
    assert "asxbot.exe" not in wired.split("ASXBOT = ")[1].splitlines()[0]
    assert repr(str(tmp_path / "task-failures.log")) in wired
    (root / "scripts" / "arena_evening.pyw").write_text(wired, encoding="utf-8")
    monkeypatch.setenv("STANDIN_CALLS", str(tmp_path / "calls.txt"))
    for k in ("STANDIN_DUE", "STANDIN_RESOLVE", "STANDIN_HANG"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    return root


def load(root: Path):
    path = root / "scripts" / "arena_evening.pyw"
    loader = importlib.machinery.SourceFileLoader("arena_evening_under_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    assert mod.REPO == root and mod.ASXBOT[0] == sys.executable
    return mod


def log_lines(root: Path) -> list[str]:
    return (root / "data" / "arena_evening.log").read_text(encoding="utf-8").splitlines()


def calls(root: Path) -> list[str]:
    return (root.parent / "calls.txt").read_text(encoding="utf-8").splitlines()


def test_the_wrong_slot_logs_a_skip_and_runs_nothing(repo, monkeypatch):
    monkeypatch.setenv("STANDIN_DUE", "1")
    assert load(repo).main() == 0
    (line,) = log_lines(repo)
    assert re.fullmatch(r"skipped \d{4}-\d\d-\d\d \d\d:\d\d: not today's evening slot", line)
    assert calls(repo) == ["arena evening-due"]


def test_the_routine_runs_every_step_in_order_and_logs_as_the_powershell_did(repo):
    assert load(repo).main() == 0
    lines = log_lines(repo)
    assert lines[0] == "" and HEADER.match(lines[1])
    assert lines[2:] == [
        "--- asxbot arena resolve ---",
        "nothing to resolve",
        "WARNING something on stderr",
        "--- exit 0 ---",
        "--- asxbot arena mark ---",
        "asx_announcements__agent: equity 10,230.89 - ünïcode ok",
        "--- exit 0 ---",
        "--- asxbot arena report --agent --send ---",
        "the report, first line",
        "[sent to Telegram: 1 message(s)]",
        "--- exit 0 ---",
    ]
    assert calls(repo) == [
        "arena evening-due", "arena resolve", "arena mark", "arena report --agent --send"
    ]  # fmt: skip
    # The top-up's check reads this log and sees a finished evening.
    text = "\n".join(lines)
    assert C.evening_finished(text, date.today(), time(0, 0)) is None


def test_a_failed_step_does_not_stop_the_next_and_the_task_still_gets_0(repo, monkeypatch):
    # 0 whatever the steps returned, as the PowerShell gave: the task restarts a failed run,
    # and a restart must never send a second report.
    monkeypatch.setenv("STANDIN_RESOLVE", "2")
    assert load(repo).main() == 0
    lines = log_lines(repo)
    assert lines[lines.index("--- asxbot arena resolve ---") + 3] == "--- exit 2 ---"
    assert lines[-1] == "--- exit 0 ---" and len(calls(repo)) == 4


def test_no_repo_writes_to_the_local_failures_log_and_exits_1(repo):
    (repo / "config.yaml").unlink()
    assert load(repo).main() == 1
    failures = (repo.parent / "task-failures.log").read_text(encoding="utf-8")
    assert "ABORTED (evening)" in failures
    assert not (repo / "data" / "arena_evening.log").exists()


def test_asxbot_missing_is_logged_and_exits_1(repo):
    mod = load(repo)
    mod.ASXBOT = [str(repo / "no-such-asxbot.exe")]
    assert mod.main() == 1
    (line,) = log_lines(repo)
    assert line.startswith("could not run asxbot arena evening-due")


def test_a_killed_run_leaves_its_last_lines_and_reads_as_not_returned(repo):
    """Run the launcher as its own process, kill it mid-report, and read what it left.

    The launcher is killed first and its hanging child second. Until 2026-09-24 the test
    killed the tree with taskkill /T, which kills in no fixed order: about one run in five
    the child died first, the launcher saw its pipe close and wrote "--- exit 1 ---" before
    it was killed in turn - a step that returned, not a killed run.
    """
    pid_file = repo.parent / "standin.pid"
    env = {**os.environ, "STANDIN_HANG": str(pid_file)}
    proc = subprocess.Popen(
        [sys.executable, str(repo / "scripts" / "arena_evening.pyw")], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    log = repo / "data" / "arena_evening.log"
    try:
        deadline = clock.monotonic() + 60
        while clock.monotonic() < deadline:
            if (
                pid_file.exists() and pid_file.read_text(encoding="utf-8").strip()
                and log.exists() and "the report, first line" in log.read_text(encoding="utf-8")
            ):  # fmt: skip
                break
            assert proc.poll() is None, "the launcher ended before its report step"
            clock.sleep(0.2)
        else:
            pytest.fail("the report's first line never reached the log while it ran")
        assert proc.poll() is None  # still running: the line was written as it arrived
    finally:
        proc.kill()  # the launcher, before anything it could see end
        proc.wait(30)
        if pid_file.exists():
            subprocess.run(
                ["taskkill", "/F", "/PID", pid_file.read_text(encoding="utf-8").strip()],
                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
            )  # fmt: skip
    lines = log_lines(repo)
    assert lines[-2:] == ["--- asxbot arena report --agent --send ---", "the report, first line"]
    why = C.evening_finished("\n".join(lines), date.today(), time(0, 0))
    assert why is not None and "not returned" in why
