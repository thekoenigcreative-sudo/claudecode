"""Alerts the human must see. Telegram goes through the OpenClaw trader agent, so an alert
here is: a CRITICAL log line, an event-log record, and a flag file the agent's scripts
surface at the top of every `scan`, `proposals` and `daily_report` output.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from asxbot.log import EventLog, get_logger

log = get_logger("asxbot.alerts")


class Alerts:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "alerts"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events = EventLog(data_dir)

    def raise_alert(self, key: str, message: str) -> None:
        log.critical("ALERT [%s] %s", key, message)
        self.events.append("alerts", {"key": key, "message": message})
        (self.dir / f"{key}.flag").write_text(
            f"{datetime.now().isoformat(timespec='seconds')}\n{message}\n", encoding="utf-8"
        )

    def clear(self, key: str) -> None:
        p = self.dir / f"{key}.flag"
        if p.exists():
            p.unlink()
            self.events.append("alerts", {"key": key, "message": "cleared"})

    def active(self) -> list[tuple[str, str]]:
        out = []
        for p in sorted(self.dir.glob("*.flag")):
            out.append((p.stem, p.read_text(encoding="utf-8").strip()))
        return out

    def is_active(self, key: str) -> bool:
        return (self.dir / f"{key}.flag").exists()


ACCESS_REFUSED = "collector_access_refused"
