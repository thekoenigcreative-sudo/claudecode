"""The watcher's heartbeat: a small file the watchdog (watchdog.py) reads from outside.

The self-checks run inside the watcher, so they die with it. On 23 Sep 2026 the watcher's
console window was closed at 13:32 and nothing said so: the last log line is a routine
poll, with no error after it, and no check was left running to notice.

While the watcher runs, a background thread rewrites data/arena/watcher_heartbeat.json
every minute with:
  * pid, started, stop_at - which process this is, and when it means to stop;
  * beat          - the thread's own clock: the process is alive and not suspended;
  * last_activity - the watcher's last log line, or the end of a wait it announced;
  * last_record   - the watcher's last log line only: when it last handed the log something
    to write. The log_silent self-check holds the log file to it, because at 08:14 on
    24 Sep 2026 the watcher kept logging and the file kept nothing (Drive had cut off the
    handle);
  * quiet_until / quiet_why - a wait the watcher announced in advance (a sleep, or a model
    call that may take up to its timeout), during which a silent log is expected;
  * wait_ended    - when the last announced wait ended;
  * cycle_started / cycle_finished - when the main loop's current cycle began, and when the
    last one ended (26 Sep 2026). On 25 Sep a cycle ran 24 minutes (10:52-11:17) while still
    logging: the watchdog saw a live, talkative watcher, and fills, stops and the 10:30 rule
    waited. The watchdog now calls a cycle stuck when it has run more than 6 minutes outside
    any announced wait;
  * state         - running | stopped (reached its stop time) | crashed.

The main loop never writes the file itself, so a slow write (Google Drive) can never
stall trading, and a failed write is simply retried a minute later.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot.io import write_text_atomic

SYD = ZoneInfo("Australia/Sydney")
FILE = "watcher_heartbeat.json"
BEAT_S = 60

_current: Heartbeat | None = None


def heartbeat_path(data_dir: Path) -> Path:
    return Path(data_dir) / "arena" / FILE


def read(data_dir: Path) -> dict | None:
    """The last heartbeat written, or None if there is none (or it cannot be read)."""
    p = heartbeat_path(data_dir)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class _Activity(logging.Handler):
    """Records the time of every log line this process writes. Nothing else."""

    def __init__(self, hb: Heartbeat):
        super().__init__(level=logging.DEBUG)
        self.hb = hb

    def emit(self, record: logging.LogRecord) -> None:
        at = datetime.fromtimestamp(record.created, SYD)
        self.hb.last_activity = at
        self.hb.last_record = at


class Heartbeat:
    def __init__(self, data_dir: Path, stop_at: datetime | None = None, beat_s: float = BEAT_S):
        self.path = heartbeat_path(data_dir)
        self.pid = os.getpid()
        self.started = _now()
        self.stop_at = stop_at
        self.beat_s = float(beat_s)
        self.last_activity = self.started
        self.last_record: datetime | None = None
        self.quiet_until: datetime | None = None
        self.quiet_why = ""
        self.wait_ended: datetime | None = None
        self.cycle_started: datetime | None = None
        self.cycle_finished: datetime | None = None
        self.state = "running"
        self.last_write_error = ""
        self._handler = _Activity(self)
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def payload(self, now: datetime | None = None) -> dict:
        def iso(t: datetime | None) -> str | None:
            return t.isoformat(timespec="seconds") if t is not None else None

        return {
            "pid": self.pid,
            "started": iso(self.started),
            "stop_at": iso(self.stop_at),
            "beat": iso(now or _now()),
            "last_activity": iso(self.last_activity),
            "last_record": iso(self.last_record),
            "quiet_until": iso(self.quiet_until),
            "quiet_why": self.quiet_why,
            "wait_ended": iso(self.wait_ended),
            "cycle_started": iso(self.cycle_started),
            "cycle_finished": iso(self.cycle_finished),
            "state": self.state,
        }

    # The main loop only sets these; the background thread writes them (no I/O in the loop).
    def cycle_start(self, now: datetime | None = None) -> None:
        self.cycle_started = now or _now()

    def cycle_end(self, now: datetime | None = None) -> None:
        self.cycle_finished = now or _now()

    def write(self) -> bool:
        try:
            write_text_atomic(json.dumps(self.payload(), indent=2), self.path)
            self.last_write_error = ""
            return True
        except OSError as e:  # Drive can hold the file for a moment; try again next beat
            self.last_write_error = str(e)
            return False

    def start(self) -> Heartbeat:
        global _current
        logging.getLogger("asxbot").addHandler(self._handler)
        self.write()
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)
        self._thread.start()
        _current = self
        return self

    def _run(self) -> None:
        while not self._stopping.wait(self.beat_s):
            self.write()

    def stop(self, state: str = "stopped") -> None:
        global _current
        self._stopping.set()
        logging.getLogger("asxbot").removeHandler(self._handler)
        self.state = state
        self.quiet_until = None
        self.quiet_why = ""
        self.write()
        if _current is self:
            _current = None

    @contextmanager
    def quiet(self, seconds: float, why: str):
        """A wait of up to `seconds` during which the watcher will log nothing."""
        prior = (self.quiet_until, self.quiet_why)
        until = _now() + timedelta(seconds=float(seconds))
        if prior[0] is None or until > prior[0]:
            self.quiet_until, self.quiet_why = until, why
        try:
            yield
        finally:
            self.quiet_until, self.quiet_why = prior
            self.last_activity = self.wait_ended = _now()


@contextmanager
def quiet(seconds: float, why: str):
    """Announce a silent wait to the running watcher's heartbeat. A no-op without one."""
    hb = _current
    if hb is None:
        yield
        return
    with hb.quiet(seconds, why):
        yield


def current() -> dict | None:
    """This process's heartbeat as it stands now (not as last written), or None if this
    process is not a running watcher."""
    hb = _current
    return hb.payload() if hb is not None else None


def _now() -> datetime:
    return datetime.now(SYD)
