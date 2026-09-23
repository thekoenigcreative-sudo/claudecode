"""TRACKER #32: the venv's pythonw.exe must be a GUI program, or task starts flash a window."""

import sys
from pathlib import Path

import pytest

from asxbot.arena.selfcheck import check_gui_launcher, run_checks
from asxbot.proc import pe_subsystem

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import install_gui_launcher  # noqa: E402


def fake_exe(path: Path, subsystem: int) -> Path:
    """The smallest file pe_subsystem reads as a Windows program of this subsystem."""
    b = bytearray(4096)
    b[:2] = b"MZ"
    b[0x3C:0x40] = (0x80).to_bytes(4, "little")
    b[0x80:0x84] = b"PE\0\0"
    b[0x80 + 24 + 68 : 0x80 + 24 + 70] = subsystem.to_bytes(2, "little")
    b[-8:] = bytes([subsystem]) * 8  # different bytes for different programs
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(b))
    return path


def test_pe_subsystem_reads_the_header(tmp_path):
    assert pe_subsystem(fake_exe(tmp_path / "g.exe", 2)) == "GUI"
    assert pe_subsystem(fake_exe(tmp_path / "c.exe", 3)) == "CONSOLE"
    (tmp_path / "x.exe").write_bytes(b"not a program")
    with pytest.raises(ValueError):
        pe_subsystem(tmp_path / "x.exe")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows programs")
def test_pe_subsystem_on_real_programs():
    base = Path(sys.base_prefix)
    assert pe_subsystem(base / "python.exe") == "CONSOLE"
    assert pe_subsystem(base / "pythonw.exe") == "GUI"


def test_check_fails_on_a_console_launcher_and_passes_on_a_gui_one(tmp_path):
    fake_exe(tmp_path / "pythonw.exe", 3)
    c = check_gui_launcher(tmp_path)
    assert not c.ok and "CONSOLE" in c.detail and "install_gui_launcher" in c.detail
    fake_exe(tmp_path / "pythonw.exe", 2)
    assert check_gui_launcher(tmp_path).ok


def test_the_check_runs_every_cycle():
    import inspect

    assert '"console_launcher", check_gui_launcher' in inspect.getsource(run_checks)


def make_venv(tmp_path: Path, venv_launcher: int) -> Path:
    home = tmp_path / "base"
    fake_exe(home / "Lib" / "venv" / "scripts" / "nt" / "pythonw.exe", 2)
    venv = tmp_path / "venv"
    fake_exe(venv / "Scripts" / "pythonw.exe", venv_launcher)
    (venv / "pyvenv.cfg").write_text(f"home = {home}\nimplementation = CPython\n", encoding="utf-8")
    return venv


def test_install_replaces_a_console_launcher_and_keeps_the_old_one(tmp_path):
    venv = make_venv(tmp_path, 3)
    old = (venv / "Scripts" / "pythonw.exe").read_bytes()
    assert install_gui_launcher.main(["--venv", str(venv), "--dry-run"]) == 0
    assert pe_subsystem(venv / "Scripts" / "pythonw.exe") == "CONSOLE"  # dry run changes nothing
    assert install_gui_launcher.main(["--venv", str(venv)]) == 0
    assert pe_subsystem(venv / "Scripts" / "pythonw.exe") == "GUI"
    backups = list((venv / "Scripts").glob("pythonw.exe.uv-console-bak-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == old


def test_install_leaves_a_gui_launcher_alone(tmp_path):
    venv = make_venv(tmp_path, 2)
    assert install_gui_launcher.main(["--venv", str(venv)]) == 0
    assert not list((venv / "Scripts").glob("pythonw.exe.uv-console-bak-*"))


@pytest.mark.skipif(not Path(r"C:\venvs\asx-bot\pyvenv.cfg").exists(), reason="Rick's PC only")
def test_this_pcs_venv_launcher_is_gui():
    assert check_gui_launcher(Path(r"C:\venvs\asx-bot\Scripts")).ok
