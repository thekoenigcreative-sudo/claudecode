"""No child process opens a window (2026-09-24). The watcher, the watchdog and the evening
routine run under pythonw; anything they start must go through asxbot.proc, which always adds
CREATE_NO_WINDOW. This reads every module and fails on any other way of starting a process."""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from asxbot import proc

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "src" / "asxbot" / "proc.py"
# The shared botctl module is vendored VERBATIM from C:\Users\Richa\.cc-jobs\changes (the
# same file in every bot), so it keeps its own default process runner, `_default_run`, which
# already passes CREATE_NO_WINDOW. It is the one allowance, and a narrow one: the tests
# below hold it to starting processes only in that hook, and hold asxbot.chat to replacing
# the hook with asxbot.proc before anything can call it.
VENDORED = ROOT / "src" / "asxbot" / "botctl.py"
BANNED_MODULES = {"subprocess", "multiprocessing", "pty"}
BANNED_OS = {"system", "popen", "startfile", "posix_spawn", "posix_spawnp"}
BANNED_OS_PREFIXES = ("spawn", "exec")


def _sources():
    for folder in ("src", "scripts"):
        for p in sorted((ROOT / folder).rglob("*")):
            if p.suffix in (".py", ".pyw") and "__pycache__" not in p.parts and p not in (
                HELPER, VENDORED
            ):
                yield p


def _offences(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = []
    for node in ast.walk(tree):
        where = f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in BANNED_MODULES:
                    out.append(f"{where} imports {a.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod in BANNED_MODULES:
                out.append(f"{where} imports from {node.module}")
            if mod == "os" and any(
                a.name in BANNED_OS or a.name.startswith(BANNED_OS_PREFIXES) for a in node.names
            ):
                out.append(f"{where} imports a process starter from os")
            if mod == "asyncio" and any(a.name.startswith("create_subprocess") for a in node.names):
                out.append(f"{where} imports asyncio's subprocess starters")
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "os" and (
                node.attr in BANNED_OS or node.attr.startswith(BANNED_OS_PREFIXES)
            ):
                out.append(f"{where} uses os.{node.attr}")
            if node.value.id == "asyncio" and node.attr.startswith("create_subprocess"):
                out.append(f"{where} uses asyncio.{node.attr}")
    return out


def test_nothing_starts_a_process_except_through_the_windowless_helper():
    found = [o for p in _sources() for o in _offences(p)]
    assert not found, "start processes through asxbot.proc (CREATE_NO_WINDOW):\n" + "\n".join(found)


def test_the_check_would_catch_a_bypass(tmp_path):
    """The scan is not vacuous: each way of starting a process is caught."""
    bad = tmp_path / "bad.py"
    for src in (
        "import subprocess\nsubprocess.run(['x'])\n",
        "from subprocess import Popen\n",
        "import os\nos.system('x')\n",
        "import os\nos.startfile('x')\n",
        "import os\nos.spawnl(0, 'x')\n",
        "import asyncio\nasyncio.create_subprocess_exec('x')\n",
        "import multiprocessing\n",
    ):
        bad.write_text(src, encoding="utf-8")
        global ROOT
        saved, ROOT = ROOT, tmp_path
        try:
            assert _offences(bad), src
        finally:
            ROOT = saved


def test_the_scan_sees_the_modules_that_start_processes():
    names = {p.name for p in _sources()}
    # capital.py (the 23 Sep top-up) was deleted 26 Sep; selfcheck.py also starts processes.
    assert {"agents.py", "selfcheck.py", "arena_evening.pyw", "arena_warmup.pyw"} <= names


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only (CREATE_NO_WINDOW / UNC / taskkill)")
@pytest.mark.parametrize("fn, target", [(proc.run, "run"), (proc.popen, "Popen")])
def test_every_child_gets_create_no_window(monkeypatch, fn, target):
    seen = {}
    monkeypatch.setattr(subprocess, target, lambda args, **kw: seen.update(kw) or "ok")
    assert fn(["x"], creationflags=0x10) == "ok"
    assert seen["creationflags"] & proc.CREATE_NO_WINDOW == proc.CREATE_NO_WINDOW
    assert seen["creationflags"] & 0x10  # an existing flag is kept
    assert proc.CREATE_NO_WINDOW == 0x08000000  # this project runs on Windows


def test_the_vendored_botctl_starts_processes_only_in_its_default_hook():
    """botctl.py may use subprocess in `_default_run` only, and only windowless."""
    tree = ast.parse(VENDORED.read_text(encoding="utf-8"))
    hook = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_default_run")
    inside = {id(n) for n in ast.walk(hook)}
    outside = [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
        and n.value.id == "subprocess" and id(n) not in inside
        and n.attr not in ("TimeoutExpired", "CREATE_NO_WINDOW")
    ]  # fmt: skip
    assert not outside, f"botctl.py starts a process outside _default_run at lines {outside}"
    calls = [n for n in ast.walk(hook) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "run"]  # fmt: skip
    assert calls and all(any(k.arg == "creationflags" for k in c.keywords) for c in calls)
    rest = [o for o in _offences(VENDORED) if "imports subprocess" not in o]
    assert not rest, rest


def test_the_chat_points_botctl_at_the_windowless_helper(monkeypatch):
    from asxbot import botctl, chat

    assert botctl.RUN is chat.run_hidden
    seen = {}

    def fake_run(args, **kw):
        seen.update(kw, args=args)
        return subprocess.CompletedProcess(args, 0, "out", "")

    monkeypatch.setattr(proc, "run", fake_run)
    assert botctl.RUN(["openclaw", "config", "validate"], 5) == (0, "out", "")
    assert seen["args"] == ["openclaw", "config", "validate"] and seen["timeout"] == 5
