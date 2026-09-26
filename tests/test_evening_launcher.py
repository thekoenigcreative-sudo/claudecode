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

from asxbot.log import logs_dir

LAUNCHER = Path(__file__).resolve().parents[1] / "scripts" / "arena_evening.pyw"
HEADER = re.compile(r"^=== evening report \d{4}-\d\d-\d\d \d\d:\d\d ===$")
ANY_HEAD = re.compile(r"^=== evening report ")
REPORT_STEP = "--- asxbot arena report"
STEP_DONE = re.compile(r"^--- exit -?\d+ ---$")


def evening_finished(log_text: str, day: date, slot: time) -> str | None:
    """None if the evening routine for `day` ran in its slot and its report step returned;
    otherwise why not. Moved here on 2026-09-26 from arena/capital.py (the 23 Sep top-up,
    deleted), so the launcher's log is still proven readable as finished or not."""
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
    # The launcher writes output as it arrives, so a line after the step's header proves
    # nothing; it closes each step with an exit line, and only that says the report returned.
    if not any(STEP_DONE.match(ln.strip()) for ln in section[step + 1 :]):
        return f"the evening report for {day} has started but not returned"
    return None


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
if args[:2] == ["arena", "journal"]:
    print("wrote the journal")
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


def evening_log(root: Path) -> Path:
    """Where the launcher logs: the local logs folder (conftest points it at the test's)."""
    return root.parent / "local_logs" / "arena_evening.log"


def log_lines(root: Path) -> list[str]:
    return evening_log(root).read_text(encoding="utf-8").splitlines()


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
        "--- asxbot arena journal ---",
        "wrote the journal",
        "--- exit 0 ---",
        "--- asxbot arena report --agent --send ---",
        "the report, first line",
        "[sent to Telegram: 1 message(s)]",
        "--- exit 0 ---",
    ]
    assert calls(repo) == [
        "arena evening-due", "arena resolve", "arena mark", "arena journal",
        "arena report --agent --send",
    ]  # fmt: skip
    # A reader of this log sees a finished evening.
    text = "\n".join(lines)
    assert evening_finished(text, date.today(), time(0, 0)) is None
    # Nothing on Drive is held open: the log is local, and copied to data/logs/ whole.
    mirrored = repo / "data" / "logs" / "arena_evening.log"
    assert mirrored.read_bytes() == evening_log(repo).read_bytes()
    assert not (repo / "data" / "arena_evening.log").exists()


def test_a_failed_step_does_not_stop_the_next_and_the_task_still_gets_0(repo, monkeypatch):
    # 0 whatever the steps returned, as the PowerShell gave: the task restarts a failed run,
    # and a restart must never send a second report.
    monkeypatch.setenv("STANDIN_RESOLVE", "2")
    assert load(repo).main() == 0
    lines = log_lines(repo)
    assert lines[lines.index("--- asxbot arena resolve ---") + 3] == "--- exit 2 ---"
    assert lines[-1] == "--- exit 0 ---" and len(calls(repo)) == 5


def test_no_repo_writes_to_the_local_failures_log_and_exits_1(repo):
    (repo / "config.yaml").unlink()
    assert load(repo).main() == 1
    failures = (repo.parent / "task-failures.log").read_text(encoding="utf-8")
    assert "ABORTED (evening)" in failures
    assert not evening_log(repo).exists()


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
    log = evening_log(repo)
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
    why = evening_finished("\n".join(lines), date.today(), time(0, 0))
    assert why is not None and "not returned" in why


def test_the_launchers_log_where_asxbot_logs_and_not_on_drive(monkeypatch, tmp_path):
    """The launchers work the folder out for themselves (asxbot lives on G:); it must be the
    same folder asxbot.log uses, and never inside the repo."""
    root = Path(__file__).resolve().parents[1]
    for name in ("arena_evening.pyw", "arena_warmup.pyw"):
        for env in (str(tmp_path / "elsewhere"), None):
            if env:
                monkeypatch.setenv("ASXBOT_LOG_DIR", env)
            else:
                monkeypatch.delenv("ASXBOT_LOG_DIR", raising=False)
                monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
            path = str(root / "scripts" / name)
            loader = importlib.machinery.SourceFileLoader(f"launcher_{name[:-4]}", path)
            spec = importlib.util.spec_from_loader(loader.name, loader)
            mod = importlib.util.module_from_spec(spec)
            loader.exec_module(mod)
            assert mod.LOG_DIR == logs_dir()
            assert mod.LOG.parent == logs_dir() and root not in mod.LOG.parents
    assert logs_dir() == tmp_path / "AppData" / "Local" / "asx-bot" / "logs"
