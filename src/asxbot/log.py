"""Logging: console plus a daily rotating file in data/logs/, and an append-only
event log (JSON lines) for announcements, signals, proposals, approvals, orders, fills.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_configured = False


def setup_logging(
    data_dir: Path, level: int = logging.INFO, name: str = "asxbot"
) -> logging.Logger:
    global _configured
    logger = logging.getLogger(name)
    if _configured:
        return logger
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    fileh = logging.handlers.TimedRotatingFileHandler(
        log_dir / "asxbot.log", when="midnight", backupCount=30, encoding="utf-8"
    )
    fileh.setFormatter(fmt)
    logger.setLevel(level)
    logger.addHandler(console)
    logger.addHandler(fileh)
    logger.propagate = False
    _configured = True
    return logger


def get_logger(name: str = "asxbot") -> logging.Logger:
    return logging.getLogger(name)


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
