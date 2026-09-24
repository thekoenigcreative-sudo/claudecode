import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from asxbot.io import write_parquet_atomic, write_text_atomic
from asxbot.log import EventLog, logs_dir, mirror_logs, rotate_daily


def test_event_log_roundtrip(tmp_path):
    ev = EventLog(tmp_path)
    ev.append("orders", {"ticker": "BHP", "qty": 10})
    ev.append("orders", {"ticker": "RIO", "qty": 5})
    rows = ev.read("orders")
    assert [r["ticker"] for r in rows] == ["BHP", "RIO"]
    assert all("ts" in r for r in rows)
    assert ev.read("nothing") == []


def test_parquet_atomic(tmp_path):
    df = pd.DataFrame({"a": [1, 2]}, index=pd.Index([0, 1], name="i"))
    p = write_parquet_atomic(df, tmp_path / "x" / "y.parquet")
    assert p.exists()
    assert not list((tmp_path / "x").glob("*.tmp"))
    assert pd.read_parquet(p)["a"].tolist() == [1, 2]


def test_text_atomic(tmp_path):
    p = write_text_atomic("hello", tmp_path / "r" / "a.md")
    assert p.read_text(encoding="utf-8") == "hello"
    assert not list((tmp_path / "r").glob("*.tmp"))


def test_safe_stem_windows_reserved_names():
    from asxbot.io import safe_stem

    assert safe_stem("PRN") == "PRN_" and safe_stem("CON") == "CON_" and safe_stem("BHP") == "BHP"


# -- logs off Google Drive (24 Sep 2026) --------------------------------------
# At 08:14 Drive silently cut off the watcher's long-open append handles on
# data/logs/asxbot.log and data/arena_warmup.log. The logs now live on a local disk.
def _touch(p: Path, text: str, day: date) -> Path:
    p.write_text(text, encoding="utf-8")
    t = datetime.combine(day, datetime.min.time()).replace(hour=19).timestamp()
    os.utime(p, (t, t))
    return p


def test_logs_are_local_and_outside_the_repo(local_logs, monkeypatch, tmp_path):
    from asxbot.config import load_config, repo_root

    assert logs_dir() == local_logs  # conftest: every test logs to its own folder
    cfg = load_config(env_file=tmp_path / "none.env")
    assert cfg.logs_dir == local_logs
    monkeypatch.delenv("ASXBOT_LOG_DIR")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
    assert logs_dir() == Path(r"C:\Users\someone\AppData\Local") / "asx-bot" / "logs"
    assert repo_root() not in logs_dir().parents


def test_a_launcher_log_is_rotated_once_a_day_and_30_are_kept(tmp_path):
    log = tmp_path / "arena_warmup.log"
    assert rotate_daily(log) is None  # nothing there yet
    _touch(log, "today's run\n", date.today())
    assert rotate_daily(log) is None and log.exists()  # written today: carry on appending

    _touch(log, "24 Sep run\n", date(2026, 9, 24))
    moved = rotate_daily(log, today=date(2026, 9, 25))
    assert moved == tmp_path / "arena_warmup.log.2026-09-24" and not log.exists()
    assert moved.read_text(encoding="utf-8") == "24 Sep run\n"

    for d in range(1, 32):  # 31 older days, then one more rotation
        _touch(tmp_path / f"arena_warmup.log.2026-08-{d:02d}", "x", date(2026, 8, d))
    _touch(log, "25 Sep run\n", date(2026, 9, 25))
    rotate_daily(log, today=date(2026, 9, 26))
    kept = sorted(p.name for p in tmp_path.glob("arena_warmup.log.*"))
    assert len(kept) == 30 and kept[-1] == "arena_warmup.log.2026-09-25"
    assert "arena_warmup.log.2026-09-24" in kept  # the newest are the ones kept


def test_logs_are_copied_to_drive_whole_and_only_when_changed(tmp_path):
    src, dest = tmp_path / "local", tmp_path / "drive" / "logs"
    src.mkdir()
    (src / "asxbot.log").write_text("line 1\n", encoding="utf-8")
    (src / "asxbot.log.2026-09-23").write_text("old day\n", encoding="utf-8")
    assert mirror_logs(src, dest) == ["asxbot.log", "asxbot.log.2026-09-23"]
    assert (dest / "asxbot.log").read_text(encoding="utf-8") == "line 1\n"
    assert mirror_logs(src, dest) == []  # nothing changed, nothing copied

    with open(src / "asxbot.log", "a", encoding="utf-8") as fh:
        fh.write("line 2\n")
    assert mirror_logs(src, dest) == ["asxbot.log"]
    assert (dest / "asxbot.log").read_text(encoding="utf-8") == "line 1\nline 2\n"
    assert not list(dest.glob("*.tmp"))


# The watcher's launcher, run against a stand-in that reports what it was told.
STANDIN = """
import os, sys
print("2026-09-24 08:14:12,835 INFO asxbot.arena.watch: args " + " ".join(sys.argv[1:]))
print("log dir " + os.environ.get("ASXBOT_LOG_DIR", "-"))
print("stdout log " + os.environ.get("ASXBOT_STDOUT_LOG", "-"))
"""


def test_the_warmup_launcher_logs_locally_and_tells_the_watcher_where(tmp_path, local_logs):
    launcher = Path(__file__).resolve().parents[1] / "scripts" / "arena_warmup.pyw"
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "config.yaml").write_text("broker: sim\n", encoding="utf-8")
    standin = tmp_path / "standin.py"
    standin.write_text(STANDIN, encoding="utf-8")
    src = launcher.read_text(encoding="utf-8")
    old = '[str(VENV / "asxbot.exe"), "arena", "watch", "--until", "auto"]'
    assert src.count(old) == 1
    wired = src.replace(old, f'[{sys.executable!r}, {str(standin)!r}, "arena", "watch"]')
    (root / "scripts" / "arena_warmup.pyw").write_text(wired, encoding="utf-8")
    # yesterday's log, to be rotated away before today's run
    local_logs.mkdir(parents=True, exist_ok=True)
    _touch(local_logs / "arena_warmup.log", "yesterday\n", date(2026, 9, 23))

    done = subprocess.run(
        [sys.executable, str(root / "scripts" / "arena_warmup.pyw")], capture_output=True,
        timeout=60, env={**os.environ, "ASXBOT_LOG_DIR": str(local_logs)},
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    lines = (local_logs / "arena_warmup.log").read_text(encoding="utf-8").splitlines()
    assert lines[1].startswith("=== warm-up starting") and lines[-1].endswith("(exit 0) ===")
    assert f"log dir {local_logs}" in lines
    assert f"stdout log {local_logs / 'arena_warmup.log'}" in lines
    assert (local_logs / "arena_warmup.log.2026-09-23").read_text(encoding="utf-8") == "yesterday\n"
    assert not (root / "data").exists()  # nothing written into the repo
