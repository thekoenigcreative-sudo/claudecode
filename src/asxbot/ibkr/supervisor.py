"""The IB Gateway supervisor: keeps Gateway up, with as little from Rick as IBKR allows.

Why (25 Sep 2026): Gateway passed `asxbot ibkr check` at 08:15, and its log stops at
08:58:29, the second an automatic update of the Claude desktop app began: Gateway had been
started from a Claude session and went down with it. Nothing noticed until ~10:10, and the
watcher ran the morning on delayed Yahoo prices.

Run every 2 minutes (and at logon) by the scheduled task "ASXBot IB Gateway Supervisor"
(scripts/ibgateway_supervisor.pyw, pythonw: no window). Each run is one check:

  1. OBSERVE (`observe`): is 127.0.0.1:4001 open; if so, does an API connection (client 43,
     read-only, disconnected at once) say Gateway's link to IBKR is up; is the Gateway the
     launcher started (scripts/ibgateway.pyw, via IBC) alive, and how far has its login got
     (launcher.json); is any other IB Gateway running (ibgateway.exe, started by hand); is
     Rick's login stored (Credential Manager).
  2. DECIDE (`decide`, plain rules, tested with fakes in tests/test_ibgateway_supervisor.py):
       * port open and link up: healthy. Leave it alone, whoever started it.
       * nothing running at all: start Gateway through its task at once.
       * a Gateway that had logged in but no longer answers, 2 checks running: restart it.
       * port open, link to IBKR down: Gateway reconnects by itself after IBKR's resets,
         and a restart costs Rick a phone approval, so restart only after 5 checks (10 min).
       * a hand-started Gateway waiting at its login window: with Rick's login stored,
         replace it (2 checks) by one IBC logs in; without it, leave it - Rick may be typing.
       * the launcher's Gateway still logging in: wait; tell Rick what it needs from him.
     A new login (not the 23:45 auto-restart, which needs none) always ends in an IBKR
     Mobile approval on Rick's phone - IBKR's rule, which nothing can skip. So a restart
     that needs one is only started on ASX trading days, 06:45-21:00, and after the
     launcher gives up on 3 unanswered approvals it waits 30 minutes before trying again.
  3. TELL RICK on the Trader chat, once per outage: "IB Gateway restarted - approve the
     IBKR login on your phone" (or, with no login stored, to log in on the PC), and once
     more only if it is still waiting 10 minutes later.
  4. LOG every check, one line, to ibgateway_supervisor.log in the local logs folder.

A file named PAUSE in %LOCALAPPDATA%\\asx-bot\\ibgateway stops it doing anything (for a
deliberate stop of Gateway); delete the file to resume.

Plain code. It starts Gateway only through its scheduled task, stops only IB Gateway
processes (ibgateway.exe / ibgateway1.exe, or the launcher and the Java it started), and
never reads, writes or logs Rick's login: it asks Credential Manager only whether one is
stored.
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dtime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot.io import write_text_atomic

SYD = ZoneInfo("Australia/Sydney")
HOST, PORT = "127.0.0.1", 4001
PROBE_CLIENT_ID = 43  # the watcher is 41, `asxbot ibkr check` 42
LAUNCHER_TASK = "ASXBot IB Gateway"
GATEWAY_IMAGES = ("ibgateway.exe", "ibgateway1.exe")

DOWN_CHECKS = 2  # a Gateway that no longer answers: restart on the 2nd check in a row
LINK_DOWN_CHECKS = 5  # answers, but cut off from IBKR: 5 checks (10 min) before a restart
REMIND_AFTER = timedelta(minutes=10)
STUCK_AFTER = timedelta(minutes=20)  # an automatic login still not done: start it again
BACKOFF_AFTER_2FA = timedelta(minutes=30)
LOGIN_HOURS = (dtime(6, 45), dtime(21, 0))
NOTICE_WAIT_S = 75  # after a launch, how long to wait to see whether it needs the phone

TEXT_PHONE = "IB Gateway restarted - approve the IBKR login on your phone"
TEXT_MANUAL = (
    "IB Gateway restarted - log in to it on the PC (IBKR username and password), then "
    "approve the login on your phone. To skip the typing next time, double-click "
    "ibkr_login_setup.cmd in the asx-bot folder once."
)
TEXT_WAITING = (
    "IB Gateway is waiting for your login on the PC (IBKR username and password), then the "
    "approval on your phone. To make it automatic, double-click ibkr_login_setup.cmd in the "
    "asx-bot folder once."
)


# -- where things live (all off Google Drive) ------------------------------------------
def _local() -> Path:
    from asxbot.localdir import real_local_appdata

    return real_local_appdata()


STATE_DIR = Path(os.environ.get("ASXBOT_IBGATEWAY_STATE") or _local() / "asx-bot" / "ibgateway")
LAUNCHER_STATE = STATE_DIR / "launcher.json"
SUPERVISOR_STATE = STATE_DIR / "supervisor.json"
PAUSE_FILE = STATE_DIR / "PAUSE"


def read_json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_json(path: Path, body: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(json.dumps(body, indent=2, default=str), Path(path))


def _logs_dir() -> Path:
    from asxbot.log import logs_dir

    return logs_dir()


def _appender(name: str):
    def log(text: str) -> None:
        d = _logs_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        try:
            from asxbot.log import rotate_daily

            rotate_daily(p)
        except OSError:
            pass
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(SYD):%Y-%m-%d %H:%M:%S} {text}\n")

    return log


def gateway_log():
    """The launcher's log: IBC's own output, with the login blanked out by the launcher."""
    return _appender("ibgateway.log")


supervisor_log = _appender("ibgateway_supervisor.log")


# -- observing -----------------------------------------------------------------------
def pid_alive(pid) -> bool:
    if not pid:
        return False
    from asxbot.arena.watchdog import pid_alive as alive

    return alive(int(pid))


def launcher_alive(st: dict) -> bool:
    return pid_alive(st.get("pid"))


def _t(s) -> datetime | None:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=SYD)


@dataclass
class Observation:
    now: datetime
    port_open: bool = False
    api_ok: bool = False  # an API connection was accepted
    link_ok: bool = False  # ...and Gateway says its link to IBKR is up
    api_note: str = ""
    launcher_alive: bool = False
    launcher_pid: int | None = None
    java_alive: bool = False
    java_pid: int | None = None
    restart: bool = False  # the launcher's current start is Gateway's own auto-restart
    phase: str | None = None  # the launcher's: starting | login_dialog | awaiting_2fa | ...
    phase_at: datetime | None = None
    mode: str | None = None  # auto (login from Credential Manager) | manual
    launched_at: datetime | None = None
    last_exit: dict = field(default_factory=dict)
    foreign_pids: list[int] = field(default_factory=list)  # hand-started ibgateway.exe
    login_stored: bool = False
    paused: bool = False

    @property
    def healthy(self) -> bool:
        return self.port_open and self.api_ok and self.link_ok

    def summary(self) -> str:
        bits = [f"port {'open' if self.port_open else 'closed'}"]
        if self.port_open:
            bits.append("api ok" if self.api_ok else f"api FAILED ({self.api_note})")
            if self.api_ok:
                bits.append("link up" if self.link_ok else f"link DOWN ({self.api_note})")
        bits.append(f"launcher {'alive' if self.launcher_alive else 'not running'}"
                    + (f" [{self.mode}, {self.phase}]" if self.launcher_alive else ""))  # fmt: skip
        if self.foreign_pids:
            bits.append(f"hand-started gateway pid {','.join(map(str, self.foreign_pids))}")
        bits.append("login stored" if self.login_stored else "no login stored")
        return "; ".join(bits)


def port_open(host: str = HOST, port: int = PORT, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def api_probe(port: int = PORT) -> tuple[bool, bool, str]:
    """(connected, link up, note) from one short read-only API connection."""
    from asxbot.ibkr.gateway import Gateway, GatewaySettings

    gw = Gateway(GatewaySettings(port=port, client_id=PROBE_CLIENT_ID, connect_timeout_s=8))
    try:
        ready = gw.connect()
        if gw.ib is not None:
            try:
                gw.ib.sleep(1.0)  # Gateway's link messages arrive just after the handshake
            except Exception:  # noqa: BLE001
                pass
            ready = gw.ready
        return gw.ib is not None, bool(ready), gw.health.last_error
    except Exception as e:  # noqa: BLE001
        return False, False, f"{type(e).__name__}: {e}"
    finally:
        gw.disconnect()


def gateway_pids() -> list[int]:
    """Process ids of ibgateway.exe / ibgateway1.exe (image names only, no command lines)."""
    from asxbot import proc

    out = proc.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                   timeout=30).stdout  # fmt: skip
    pids = []
    for line in out.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].strip('"').lower() in GATEWAY_IMAGES:
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def observe(now: datetime) -> Observation:
    from asxbot.ibkr import credentials

    st = read_json(LAUNCHER_STATE)
    alive = launcher_alive(st)
    o = Observation(now=now, paused=PAUSE_FILE.exists())
    o.launcher_alive = alive
    o.launcher_pid = st.get("pid") if alive else None
    o.java_alive = alive and pid_alive(st.get("java_pid"))
    o.java_pid = st.get("java_pid") if o.java_alive else None
    o.restart = bool(st.get("restart"))
    o.phase = st.get("phase")
    o.phase_at = _t(st.get("phase_at"))
    o.mode = st.get("mode")
    o.launched_at = _t(st.get("started_at"))
    o.last_exit = st.get("exit") or {}
    o.foreign_pids = gateway_pids()
    o.login_stored = credentials.stored()
    o.port_open = port_open()
    if o.port_open:
        o.api_ok, o.link_ok, o.api_note = api_probe()
    return o


# -- deciding ------------------------------------------------------------------------
@dataclass
class Plan:
    action: str = "none"  # none | launch | restart
    why: str = ""
    kill: list[int] = field(default_factory=list)  # Gateway pids to stop first (ours or not)
    notice: str = ""  # phone | manual | waiting: what Rick is to be told, if anything
    reminder: bool = False
    close_outage: bool = False


def trading_day(d: date) -> bool:
    from asxbot.announcements.live import is_trading_day

    return is_trading_day(d)


def login_window(now: datetime, is_trading_day=trading_day) -> bool:
    """When a restart that needs Rick's phone may start: an ASX trading day, 06:45-21:00."""
    now = now.astimezone(SYD)
    return is_trading_day(now.date()) and LOGIN_HOURS[0] <= now.time() < LOGIN_HOURS[1]


def decide(o: Observation, st: dict, is_trading_day=trading_day) -> tuple[Plan, dict]:
    """One check's plan, and the new supervisor state. Pure: no clock, no I/O."""
    now = o.now
    st = dict(st)
    st.setdefault("bad", 0)
    st.setdefault("link_bad", 0)
    outage = dict(st.get("outage") or {})
    window = login_window(now, is_trading_day)

    def finish(plan: Plan) -> tuple[Plan, dict]:
        st["outage"] = outage or None
        return plan, st

    if o.paused:
        return finish(Plan(why="paused (PAUSE file present)"))

    if o.healthy:
        st["bad"] = st["link_bad"] = 0
        st.pop("backoff_until", None)
        plan = Plan(why="healthy")
        if outage:
            plan.close_outage = True
            outage = {}
        return finish(plan)

    if not outage:
        outage = {"since": now.isoformat(timespec="seconds"), "notice": None,
                  "notified_at": None, "reminded_at": None}  # fmt: skip

    ours_logging_in = o.launcher_alive and o.phase not in ("logged_in", "exited")

    # -- what Rick should be told about a login that is waiting on him -----------------
    def notice_for() -> str:
        if ours_logging_in:
            if o.restart:
                return ""  # Gateway's own auto-restart: no login, nothing for Rick to do
            if o.mode == "manual":
                return "manual"
            if o.phase == "awaiting_2fa":
                return "phone"
            return ""
        if o.foreign_pids and not o.port_open and not o.login_stored:
            return "waiting"
        return ""

    def with_notice(plan: Plan) -> Plan:
        kind = notice_for()
        if kind and window:
            if not outage.get("notified_at"):
                plan.notice = kind
            elif not outage.get("reminded_at"):
                since = _t(outage.get("notified_at"))
                if since and now - since >= REMIND_AFTER:
                    plan.notice, plan.reminder = kind, True
        return plan

    # -- 1. answering, but cut off from IBKR: Gateway usually reconnects by itself -----
    if o.port_open and o.api_ok and not o.link_ok:
        st["bad"] = 0
        st["link_bad"] += 1
        if st["link_bad"] < LINK_DOWN_CHECKS:
            return finish(Plan(why=f"link to IBKR down, check {st['link_bad']} of "
                                   f"{LINK_DOWN_CHECKS} before a restart"))  # fmt: skip
        return finish(_restart(o, st, window, now, "link to IBKR down for "
                                                   f"{st['link_bad']} checks"))  # fmt: skip
    st["link_bad"] = 0

    # -- 2. our launcher's Gateway is still logging in: wait for it ---------------------
    if ours_logging_in:
        st["bad"] = 0
        started = o.phase_at or o.launched_at
        if o.mode == "auto" and started and now - started >= STUCK_AFTER and \
                o.phase != "awaiting_2fa":  # fmt: skip
            return finish(_restart(o, st, window, now,
                                   f"automatic login stuck at '{o.phase}' since "
                                   f"{started:%H:%M}"))  # fmt: skip
        return finish(with_notice(Plan(why=f"login in progress ({o.mode}, {o.phase})")))

    # -- 3. a hand-started Gateway that does not answer --------------------------------
    if o.foreign_pids and not o.launcher_alive:
        if not o.login_stored:
            st["bad"] = 0
            return finish(with_notice(Plan(
                why="a hand-started Gateway is not answering (at its login window?); no "
                    "login stored, so it is left for Rick")))  # fmt: skip
        st["bad"] += 1
        if st["bad"] < DOWN_CHECKS:
            return finish(Plan(why=f"hand-started Gateway not answering, check {st['bad']} of "
                                   f"{DOWN_CHECKS}"))  # fmt: skip
        why = "hand-started Gateway not answering; replacing it with one IBC logs in"
        return finish(_restart(o, st, window, now, why))

    # -- 4. nothing running at all: start it now ----------------------------------------
    if not o.launcher_alive and not o.foreign_pids:
        st["bad"] = 0
        return finish(_restart(o, st, window, now, "IB Gateway is not running"))

    # -- 5. ours, logged in before, now not answering ---------------------------------
    st["bad"] += 1
    if st["bad"] < DOWN_CHECKS:
        return finish(Plan(why=f"Gateway not answering, check {st['bad']} of {DOWN_CHECKS}"))
    return finish(_restart(o, st, window, now, f"Gateway not answering for {st['bad']} checks"))


def _restart(o: Observation, st: dict, window: bool, now: datetime, why: str) -> Plan:
    """Start (or replace) Gateway, unless a login now would ping Rick's phone at a bad time.
    """
    backoff = _t(st.get("backoff_until"))
    if o.last_exit.get("gave_up_2fa") and not backoff:
        gave_up_at = _t(o.last_exit.get("at")) or now
        backoff = gave_up_at + BACKOFF_AFTER_2FA
        st["backoff_until"] = backoff.isoformat(timespec="seconds")
    if backoff and now < backoff:
        return Plan(why=f"{why}; waiting until {backoff:%H:%M} (the last 3 phone approvals "
                        "went unanswered)")  # fmt: skip
    if not window and o.login_stored:
        return Plan(why=f"{why}; an automatic login pings Rick's phone, so it waits for an "
                        "ASX trading day 06:45-21:00")  # fmt: skip
    st.pop("backoff_until", None)
    st["bad"] = st["link_bad"] = 0
    st["last_launch_at"] = now.isoformat(timespec="seconds")
    ours = [p for p in (o.java_pid, o.launcher_pid) if p]
    kill = [*ours, *o.foreign_pids]
    return Plan(action="restart" if kill else "launch", why=why, kill=kill)


# -- acting --------------------------------------------------------------------------
def kill_tree(pid: int) -> bool:
    from asxbot import proc

    r = proc.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True,
                 text=True, timeout=30)  # fmt: skip
    return r.returncode == 0


def start_launcher_task() -> tuple[bool, str]:
    from asxbot import proc

    r = proc.run(["schtasks", "/run", "/tn", LAUNCHER_TASK], capture_output=True, text=True,
                 timeout=30)  # fmt: skip
    return r.returncode == 0, (r.stdout or r.stderr or "").strip()


def message(kind: str, reminder: bool, now: datetime) -> str:
    text = {"phone": TEXT_PHONE, "manual": TEXT_MANUAL, "waiting": TEXT_WAITING}[kind]
    if reminder:
        text = "Still waiting: " + text
    return escape(text)


@dataclass
class Deps:
    """Everything `run_once` touches outside itself, so the tests can use fakes."""

    observe: object = observe
    send: object = None  # text -> bool
    kill: object = kill_tree
    start: object = start_launcher_task
    log: object = supervisor_log
    sleep: object = time.sleep
    is_trading_day: object = trading_day
    state_path: Path = SUPERVISOR_STATE


def run_once(now: datetime, deps: Deps) -> Plan:
    st = read_json(deps.state_path)
    o = deps.observe(now)
    plan, st = decide(o, st, deps.is_trading_day)
    line = f"{plan.action.upper()}: {plan.why} | {o.summary()}"

    if plan.action in ("launch", "restart"):
        for pid in plan.kill:
            line += f" | stopped gateway pid {pid}: {deps.kill(int(pid))}"
        if plan.kill:
            deps.sleep(3)  # let Task Scheduler see the old launcher end before /run
        ok, said = deps.start()
        line += f" | started task '{LAUNCHER_TASK}': {'ok' if ok else 'FAILED ' + said}"
        if ok:
            # Give it time to show whether this login needs Rick (the phone or the PC), so he
            # is told now rather than at the next check.
            waited = 0
            while waited < NOTICE_WAIT_S:
                deps.sleep(5)
                waited += 5
                o2 = deps.observe(now + timedelta(seconds=waited))
                if o2.healthy or (o2.launcher_alive and (o2.mode == "manual" or
                                                         o2.phase == "awaiting_2fa")):  # fmt: skip
                    break
            p2, st = decide(o2, st, deps.is_trading_day)
            plan.notice, plan.reminder = p2.notice, p2.reminder
            line += f" | after {waited}s: {o2.summary()}"

    if plan.notice:
        outage = st.get("outage") or {}
        sent = bool(deps.send and deps.send(message(plan.notice, plan.reminder, now)))
        if sent:
            key = "reminded_at" if plan.reminder else "notified_at"
            outage[key] = now.isoformat(timespec="seconds")
            outage["notice"] = plan.notice
            st["outage"] = outage
        line += f" | told Rick ({plan.notice}{', reminder' if plan.reminder else ''}): " \
                f"{'sent' if sent else 'NOT sent, will retry'}"  # fmt: skip
    if plan.close_outage:
        line += " | outage over"

    st["last_check"] = now.isoformat(timespec="seconds")
    st["last_result"] = line
    write_json(deps.state_path, st)
    deps.log(line)
    return plan


def main() -> int:
    from asxbot.arena.notify import Notifier
    from asxbot.config import load_config

    try:
        cfg = load_config()
        # Enabled regardless of arena.alerts.telegram: this is about the data feed, not a trade.
        send = Notifier(cfg, enabled=True).send
    except Exception as e:  # noqa: BLE001 - keep supervising even if the repo config is broken
        supervisor_log(f"config/notifier unavailable ({type(e).__name__}: {e}); no messages")
        send = None
    try:
        run_once(datetime.now(SYD), Deps(send=send))
    except Exception as e:  # noqa: BLE001 - pythonw has nowhere to print
        supervisor_log(f"the supervisor itself failed: {type(e).__name__}: {e}")
        return 1
    return 0


# -- the 07:20 pre-flight -------------------------------------------------------------
def preflight(cfg, now: datetime, send, run_check=None, is_trading_day=trading_day) -> str:
    """`asxbot ibkr check`; tell Rick only if it fails. Trading days only."""
    if not is_trading_day(now.date()):
        return "not a trading day"
    if run_check is None:
        from asxbot.ibkr.check import run_check
    lines: list[str] = []
    code = run_check(cfg, out=lines.append, now=now)
    verdict = next((ln for ln in reversed(lines) if ln.startswith(("PASS", "FAIL"))), "")
    if code == 0:
        return f"passed: {verdict}"
    text = (f"IB Gateway pre-flight FAILED ({now:%H:%M}): {verdict or 'no result'}. "
            "The watcher will use delayed Yahoo prices until IBKR answers.")  # fmt: skip
    sent = bool(send and send(escape(text)))
    return f"failed ({'Rick told' if sent else 'message NOT sent'}): {verdict}"


def preflight_main() -> int:
    from asxbot.arena.notify import Notifier
    from asxbot.config import load_config
    from asxbot.log import setup_logging

    log = _appender("ibkr_preflight.log")
    try:
        cfg = load_config()
        setup_logging(cfg.logs_dir)
        result = preflight(cfg, datetime.now(SYD), Notifier(cfg, enabled=True).send)
    except Exception as e:  # noqa: BLE001
        log(f"the pre-flight itself failed: {type(e).__name__}: {e}")
        return 1
    log(result)
    return 0


__all__ = [
    "Deps", "Observation", "Plan", "decide", "login_window", "main", "message", "observe",
    "preflight", "preflight_main", "run_once",
]  # fmt: skip
