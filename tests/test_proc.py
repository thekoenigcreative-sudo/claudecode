"""No child process opens a window (2026-09-24). The watcher, the watchdog and the evening
routine run under pythonw; anything they start must go through asxbot.proc, which always adds
CREATE_NO_WINDOW. This reads every module and fails on any other way of starting a process."""

import ast
import subprocess
from pathlib import Path

import pytest

from asxbot import proc

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "src" / "asxbot" / "proc.py"
BANNED_MODULES = {"subprocess", "multiprocessing", "pty"}
BANNED_OS = {"system", "popen", "startfile", "posix_spawn", "posix_spawnp"}
BANNED_OS_PREFIXES = ("spawn", "exec")


def _sources():
    for folder in ("src", "scripts"):
        for p in sorted((ROOT / folder).rglob("*")):
            if p.suffix in (".py", ".pyw") and "__pycache__" not in p.parts and p != HELPER:
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
    assert {"agents.py", "capital.py", "arena_evening.pyw", "arena_warmup.pyw"} <= names


@pytest.mark.parametrize("fn, target", [(proc.run, "run"), (proc.popen, "Popen")])
def test_every_child_gets_create_no_window(monkeypatch, fn, target):
    seen = {}
    monkeypatch.setattr(subprocess, target, lambda args, **kw: seen.update(kw) or "ok")
    assert fn(["x"], creationflags=0x10) == "ok"
    assert seen["creationflags"] & proc.CREATE_NO_WINDOW == proc.CREATE_NO_WINDOW
    assert seen["creationflags"] & 0x10  # an existing flag is kept
    assert proc.CREATE_NO_WINDOW == 0x08000000  # this project runs on Windows
