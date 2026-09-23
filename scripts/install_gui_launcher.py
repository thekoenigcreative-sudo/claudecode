"""Give the venv a pythonw.exe that really has no console (TRACKER #32).

    C:\\venvs\\asx-bot\\Scripts\\python.exe scripts\\install_gui_launcher.py
    (options: --dry-run, --venv <another venv>)

Why: uv 0.10.2 made C:\\venvs\\asx-bot\\Scripts\\pythonw.exe a CONSOLE program, byte for
byte the same as python.exe. The Warmup (07:30), Evening (19:30) and Watchdog (every 5
minutes) tasks all start that pythonw.exe, so each start opened a console window (conhost),
and that launcher then started the base interpreter's console python.exe. Rick saw a
window flash every 5 minutes on 23 Sep 2026.

The fix is the launcher CPython itself puts in every venv made by `python -m venv`:
`<base>\\Lib\\venv\\scripts\\nt\\pythonw.exe` (venvwlauncher), a GUI program. It reads
pyvenv.cfg next to it and starts the base interpreter's GUI pythonw.exe inside the venv. So
nothing in the chain has a console. The scheduled tasks keep the same command line.

The replaced launcher is kept as pythonw.exe.uv-console-bak-<date>. To undo:
    copy /y C:\\venvs\\asx-bot\\Scripts\\pythonw.exe.uv-console-bak-20260923 ^
        C:\\venvs\\asx-bot\\Scripts\\pythonw.exe

Rebuilding the venv with uv puts the console launcher back; the `console_launcher`
self-check says so on the next watcher cycle, and this script puts it right again.
Nothing may be running from the venv's pythonw.exe while it runs (Windows will not replace
a running program); it refuses rather than half-doing it.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from asxbot.proc import pe_subsystem  # noqa: E402


def base_home(venv: Path) -> Path:
    for line in (venv / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip().lower() == "home":
            return Path(value.strip())
    raise SystemExit(f"no 'home' in {venv / 'pyvenv.cfg'}")


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()[:12]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--venv", default=r"C:\venvs\asx-bot")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    venv = Path(args.venv)
    target = venv / "Scripts" / "pythonw.exe"
    source = base_home(venv) / "Lib" / "venv" / "scripts" / "nt" / "pythonw.exe"
    for p in (target, source):
        if not p.exists():
            raise SystemExit(f"not found: {p} - nothing changed")
    print(f"venv launcher:    {target}  {pe_subsystem(target)}  md5 {md5(target)}")
    print(f"CPython launcher: {source}  {pe_subsystem(source)}  md5 {md5(source)}")
    if pe_subsystem(source) != "GUI":
        raise SystemExit("CPython's launcher is not a GUI program - nothing changed")
    if pe_subsystem(target) == "GUI":
        print("OK: the venv's pythonw.exe is already a GUI program; nothing to do.")
        return 0
    backup = target.with_name(f"pythonw.exe.uv-console-bak-{date.today():%Y%m%d}")
    if args.dry_run:
        print(f"dry run: would keep the old one as {backup.name} and copy CPython's in its place")
        return 0
    if not backup.exists():
        shutil.copy2(target, backup)
    try:
        shutil.copy2(source, target)
    except PermissionError as e:
        raise SystemExit(f"could not replace {target} (is something running from it?): {e}") from e
    # Read it back: the artefact, not this script's account of it.
    kind = pe_subsystem(target)
    print(f"now:              {target}  {kind}  md5 {md5(target)}; old one kept as {backup.name}")
    if kind != "GUI" or md5(target) != md5(source):
        raise SystemExit(f"FAILED: {target} is still {kind} - restore it from {backup}")
    print("OK: the scheduled tasks now start with no console window.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
