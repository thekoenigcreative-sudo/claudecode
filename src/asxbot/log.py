"""Logging: console plus a daily rotating file, and an append-only event log (JSON lines)
for announcements, signals, proposals, approvals, orders, fills.

The log files live on a LOCAL disk (`logs_dir()`: %LOCALAPPDATA%/asx-bot/logs), never in
the repo. At 08:14 on 24 Sep 2026 Google Drive silently cut off the watcher's long-open
append handles: the process kept trading, both data/logs/asxbot.log and the launcher's
stdout log stopped receiving a single line, and nothing said so. Files rewritten whole
(the heartbeat) kept updating. So nothing holds a file open on Drive any more; the evening
routine copies the local logs to data/logs/ whole (`mirror_logs`), for the record and the
other PC. The event log below stays on Drive: every append opens, writes and closes.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_configured = False
KEEP_DAYS = 30
# The watcher's own log (26 Sep 2026). Until then the watcher, the history fetch, the replay
# workers, the evening steps and the tests all wrote asxbot.log, and the watcher's
# errors_logged self-check read every one of them as its own: an 18:57 chaos-test line on
# 25 Sep reached Rick as a SELF-CHECK. `asxbot arena watch` now logs here, and only here;
# the self-checks read this file. The evening routine copies it to data/logs/ like the rest.
WATCH_LOG = "arena_watch.log"
DEFAULT_LOG = "asxbot.log"


def logs_dir() -> Path:
    """Where every log file is written: a local folder outside Google Drive.

    ASXBOT_LOG_DIR overrides it (the tests use this). The launchers in scripts/ work the
    same place out for themselves, because they run before G: is known to be mounted.
    """
    override = os.environ.get("ASXBOT_LOG_DIR")
    if override:
        return Path(override)
    from asxbot.localdir import asx_local

    return asx_local() / "logs"


def setup_logging(
    log_dir: Path | None = None,
    level: int = logging.INFO,
    name: str = "asxbot",
    file: bool = True,
    stream=None,
    filename: str = DEFAULT_LOG,
) -> logging.Logger:
    """Console plus asxbot.log (or `filename`: the watcher's is WATCH_LOG). `file=False` is
    console only (to `stream`, default stderr): the Trader chat runs all day beside the
    watcher, and its launcher writes what it prints to chat.log, so it must not hold
    asxbot.log open too (two processes rotating one file at midnight is a fight on
    Windows). The first call wins; later calls are no-ops."""
    global _configured
    logger = logging.getLogger(name)
    if _configured:
        return logger
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler(stream)
    console.setFormatter(fmt)
    logger.setLevel(level)
    logger.addHandler(console)
    if file:
        log_dir = Path(log_dir) if log_dir is not None else logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        fileh = logging.handlers.TimedRotatingFileHandler(
            log_dir / filename, when="midnight", backupCount=KEEP_DAYS, encoding="utf-8"
        )
        fileh.setFormatter(fmt)
        logger.addHandler(fileh)
    logger.propagate = False
    _configured = True
    return logger


def get_logger(name: str = "asxbot") -> logging.Logger:
    return logging.getLogger(name)


def rotate_daily(path: Path, today: date | None = None, keep: int = KEEP_DAYS) -> Path | None:
    """Daily rotation for a launcher's log, which is opened once a run, not held all day.

    If `path` was last written before today it becomes `<name>.<that day>`, the same naming
    as asxbot.log's rotation, and only the newest `keep` of those are kept. Returns the
    rotated file, or None if there was nothing to rotate.
    """
    path = Path(path)
    today = today or date.today()
    try:
        written = date.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None
    if written >= today:
        return None
    dest = path.with_name(f"{path.name}.{written.isoformat()}")
    if dest.exists():  # two runs' worth for one day: keep both, never overwrite
        dest = path.with_name(f"{path.name}.{written.isoformat()}.{datetime.now():%H%M%S}")
    path.replace(dest)
    old = sorted(path.parent.glob(f"{path.name}.????-??-??*"))
    for p in old[:-keep] if keep > 0 else old:
        try:
            p.unlink()
        except OSError:
            pass
    return dest


def mirror_logs(src: Path, dest: Path) -> list[str]:
    """Copy every log in `src` to `dest` (Drive) whole, where it differs. Returns the names
    copied.

    Each copy goes to a temporary name and is then swapped in, so nothing on Drive is ever
    held open or appended to: exactly what the heartbeat does, and the heartbeat survived
    the 24 Sep cut-off that silenced both appended logs.
    """
    src, dest = Path(src), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for p in sorted(src.iterdir()) if src.exists() else []:
        # A .lock file is a process's single-instance lock, not a log (26 Sep 2026): a held
        # one cannot be read on Windows, and a copy of one means nothing on the other PC.
        if not p.is_file() or p.suffix in (".tmp", ".lock"):
            continue
        target = dest / p.name
        s = p.stat()
        try:
            t = target.stat()
            if t.st_size == s.st_size and int(t.st_mtime) == int(s.st_mtime):
                continue
        except OSError:
            pass
        tmp = target.with_name(target.name + ".tmp")
        shutil.copy2(p, tmp)
        tmp.replace(target)
        copied.append(p.name)
    return copied


def event_day(rec: dict[str, Any]) -> str:
    """The Sydney date (ISO) of an event record: its "day" if it carries one, else its "ts"
    converted from UTC. The log stamps in UTC, and 00:00-10:00 Sydney (to 11:00 in daylight
    saving) is the previous day in UTC: until 26 Sep 2026 the "positions opened today" limit
    compared the UTC date, so from 4 Oct a buy filled 10:00-11:00 would have counted on the
    day before."""
    if rec.get("day"):
        return str(rec["day"])
    ts = str(rec.get("ts") or "")
    try:
        t = datetime.fromisoformat(ts)
    except ValueError:
        return ts[:10]
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(ZoneInfo("Australia/Sydney")).date().isoformat()


class EventLog:
    """Append-only JSON-lines log. One file per event kind, e.g. data/events/orders.jsonl.

    Appends are single write() calls with a trailing newline, which is safe enough for
    Drive sync (no partial-record rewrites, no database file to corrupt).
    """

    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "events"
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, kind: str) -> Path:
        return self.dir / f"{kind}.jsonl"

    def append(self, kind: str, record: dict[str, Any]) -> dict[str, Any]:
        rec = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "kind": kind, **record}
        line = json.dumps(rec, default=str, ensure_ascii=False)
        with open(self.path(kind), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return rec

    def read(self, kind: str) -> list[dict[str, Any]]:
        p = self.path(kind)
        if not p.exists():
            return []
        out = []
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out
