"""The IBKR connection doctor (26 Sep 2026): watch, diagnose, recover, verify.

Rick, 25 Sep: "a safeguard to stop the IBKR connection dropping is needed too, using an
agentic workflow". The persistent connection (live.py) reconnects by itself and the Gateway
supervisor (supervisor.py, every 2 minutes, from outside) relaunches a dead Gateway. What
neither did: say WHY the prices stopped, pick the recovery that cause needs, check that it
worked, and try the next thing when it did not. On 25 Sep the morning was lost to exactly
that gap - a feed that said "IBKR" while Gateway was dead, a fallback that never came back.

The doctor is an agent in the plain sense - a loop that observes, decides and acts on its
own - written as plain code, not a model: it has to keep working when the Claude plan is at
its limit (25 Sep 22:41), and every cause it can name has one right answer. It runs inside
the watcher, once a cycle (FailoverFeed.check), and on each tick:

  1. OBSERVES (`gather`): the watcher's own connection (connected, heartbeat, Gateway's link
     to IBKR, a client-id clash, a competing session, pacing, the market data type, how old
     the index's streamed bars are), Gateway from outside (port 4001, the process, the
     supervisor's last observation: its login phase, whether Rick's login is stored), and -
     only when something is wrong - the network (the internet, and IBKR's own servers as
     named in Gateway's jts.ini);
  2. DIAGNOSES (`diagnose`, pure): one cause from a fixed list, with the evidence -
       network_down        the PC cannot reach the internet or IBKR;
       gateway_down        no Gateway process / nothing listening on 4001;
       login_needed        Gateway is at its login window or waiting for the phone approval;
       gateway_hung        Gateway listens but does not answer (API or heartbeat);
       client_id_in_use    our client id is held by another connection (326);
       watcher_disconnected Gateway is fine, our socket is not;
       ibkr_link_down      Gateway is up but cut off from IBKR's servers (1100/2110);
       competing_session   IBKR sends no data while another session (phone, web) holds it;
       delayed_data        IBKR sends delayed data in market hours (the subscription);
       stale_stream        connected and answering, but the index's bars stopped;
       pacing              IBKR is throttling history requests (does not stop entries);
  3. ACTS (`LADDERS`): the first rung of that cause's ladder - wait, re-request the streams,
     reconnect, switch to a spare client id, slow the history queue, ask the supervisor to
     check now (it relaunches Gateway through IBC), ask it to restart Gateway, tell Rick;
  4. VERIFIES: each rung has a deadline. If the cause has cleared by then the episode closes
     with what fixed it ("verified: healthy 40 s after reconnect"); if not, the next rung.

While an episode that makes the prices untrustworthy is open, NEW ENTRIES ARE PAUSED
(`block`, read by FailoverFeed.entries_allowed); stops, targets and trailing keep working.
Entries resume by themselves when the cause clears.

Rick hears from the doctor at most twice an episode, on a trading day 07:00-17:00: once
when it needs him or has run out of rungs, in plain words with what it tried, and once when
it is fixed. A Gateway that needs his login or phone approval is the supervisor's message
(it knows the login phase); the doctor does not repeat it. Everything is recorded: the
state in %LOCALAPPDATA%\\asx-bot\\ibgateway\\doctor.json (the supervisor and `asxbot ibkr
doctor` read it), every diagnosis change and action in data/events/ibkr_doctor.jsonl.

Read-only like the rest of src/asxbot/ibkr: no order call, and nothing here can start or
stop Gateway except through the supervisor's scheduled task.
"""

from __future__ import annotations

import json
import socket
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot.io import write_text_atomic
from asxbot.log import get_logger

log = get_logger("asxbot.ibkr.doctor")
SYD = ZoneInfo("Australia/Sydney")

HEALTHY = "healthy"
NETWORK_DOWN = "network_down"
GATEWAY_DOWN = "gateway_down"
LOGIN_NEEDED = "login_needed"
GATEWAY_HUNG = "gateway_hung"
CLIENT_ID_IN_USE = "client_id_in_use"
WATCHER_DISCONNECTED = "watcher_disconnected"
LINK_DOWN = "ibkr_link_down"
COMPETING = "competing_session"
DELAYED = "delayed_data"
STALE_STREAM = "stale_stream"
PACING = "pacing"

WORDS = {
    HEALTHY: "IBKR prices are flowing",
    NETWORK_DOWN: "the PC cannot reach the internet",
    GATEWAY_DOWN: "IB Gateway is not running",
    LOGIN_NEEDED: "IB Gateway is waiting for a login (or the phone approval)",
    GATEWAY_HUNG: "IB Gateway is running but not answering",
    CLIENT_ID_IN_USE: "another connection is using the watcher's IBKR client id",
    WATCHER_DISCONNECTED: "the watcher lost its connection to IB Gateway",
    LINK_DOWN: "IB Gateway is cut off from IBKR's servers (IBKR's side; the internet is fine)",
    COMPETING: ("IBKR stopped the market data because someone logged in to IBKR with the "
                "same username (the phone app or the website)"),
    DELAYED: "IBKR is sending delayed prices instead of real-time ones",
    STALE_STREAM: "IBKR stopped sending prices although the connection is up",
    PACING: "IBKR is throttling history requests (pacing)",
}  # fmt: skip

# Causes that make the prices untrustworthy: entries pause while one is open. Pacing only
# slows history; entries are judged on the streamed bars, which pacing does not touch.
BLOCKING = {NETWORK_DOWN, GATEWAY_DOWN, LOGIN_NEEDED, GATEWAY_HUNG, CLIENT_ID_IN_USE,
            WATCHER_DISCONNECTED, LINK_DOWN, COMPETING, DELAYED, STALE_STREAM}  # fmt: skip
# The supervisor tells Rick about these (it knows the login phase); the doctor does not.
SUPERVISOR_SPEAKS = {GATEWAY_DOWN, LOGIN_NEEDED}

STALE_S = 120.0  # the index's streamed bars older than this in market hours: stale stream
# ...but not in the first minutes of the session: before 10:00 nothing streams, so every
# stream's age is its time since subscription until the first bars arrive.
OPEN_GRACE_MIN = 3.0
HEARTBEAT_STALE_S = 150.0  # connected, but no heartbeat answer for this long: hung
CLASH_WINDOW_S = 300.0
PACING_HITS = 3  # pacing violations in 10 minutes before the history queue is slowed
SUPERVISOR_FRESH = timedelta(minutes=5)
# 41 watcher, 42 check, 43 supervisor probe, 45 the chaos script, 46 the history fetch
SPARE_CLIENT_IDS = (44, 47, 48)
PROBE_CACHE_S = 60.0  # the network and process probes are not repeated more often than this
TELL_HOURS = (dtime(7, 0), dtime(17, 0))
SUPERVISOR_TASK = "ASXBot IB Gateway Supervisor"


@dataclass
class Step:
    action: str  # wait | resubscribe | reconnect | switch_client_id | slow_history |
    #              run_supervisor | request_restart | tell_rick
    wait_s: float  # how long the cause has to clear after this step before the next one


# The recovery for each cause, first rung first. The last rung stays in place (verify
# continues) until the cause clears or changes.
LADDERS: dict[str, list[Step]] = {
    NETWORK_DOWN: [Step("wait", 180), Step("tell_rick", 0)],
    GATEWAY_DOWN: [Step("run_supervisor", 180), Step("wait", 0)],
    LOGIN_NEEDED: [Step("run_supervisor", 0)],
    GATEWAY_HUNG: [Step("reconnect", 90), Step("run_supervisor", 240), Step("tell_rick", 0)],
    CLIENT_ID_IN_USE: [Step("switch_client_id", 90), Step("switch_client_id", 90),
                       Step("tell_rick", 0)],  # fmt: skip
    WATCHER_DISCONNECTED: [Step("wait", 60), Step("reconnect", 90),
                           Step("switch_client_id", 90), Step("tell_rick", 0)],  # fmt: skip
    LINK_DOWN: [Step("wait", 180), Step("tell_rick", 0)],  # the supervisor restarts at 10 min
    COMPETING: [Step("wait", 60), Step("tell_rick", 0)],
    DELAYED: [Step("reconnect", 120), Step("tell_rick", 0)],
    STALE_STREAM: [Step("resubscribe", 90), Step("reconnect", 120), Step("request_restart", 480),
                   Step("tell_rick", 0)],  # fmt: skip
    PACING: [Step("slow_history", 600)],
}

WHAT_DID = {
    "wait": "waited for it to recover by itself",
    "resubscribe": "re-requested every price stream",
    "reconnect": "reconnected to IB Gateway",
    "switch_client_id": "reconnected under a spare client id",
    "slow_history": "slowed the history requests",
    "run_supervisor": "asked the Gateway supervisor to check at once",
    "request_restart": "asked the supervisor to restart IB Gateway",
    "tell_rick": "told Rick",
}


# -- observing -------------------------------------------------------------------------
@dataclass
class Signals:
    now: datetime
    in_hours: bool = False  # ASX continuous session on a trading day
    trading_day: bool = False
    minutes_since_open: float = 0.0  # since 10:00, in market hours
    # the watcher's own connection (live.py)
    connected: bool = False
    ready: bool = False
    server_ok: bool = False
    refused: bool = False
    heartbeat_age_s: float | None = None
    last_error: str = ""
    connect_error: str = ""
    client_id: int = 0
    client_id_clash: bool = False
    competing: bool = False
    pacing_recent: int = 0
    delayed: bool = False
    index_streaming: bool = False
    index_age_s: float | None = None
    disconnected_for_s: float = 0.0
    # IB Gateway from outside
    port_open: bool | None = None
    gw_process: bool | None = None
    gw_api_ok: bool | None = None  # the supervisor's last probe (None: no fresh one)
    gw_link_ok: bool | None = None
    gw_logging_in: bool = False  # the launcher's Gateway is at its login / phone approval
    login_stored: bool | None = None
    # the network (probed only when something is wrong)
    internet_ok: bool | None = None
    ibkr_reachable: bool | None = None

    def brief(self) -> dict:
        d = asdict(self)
        d["now"] = self.now.isoformat(timespec="seconds")
        return d


@dataclass
class Diagnosis:
    cause: str
    evidence: list[str] = field(default_factory=list)

    @property
    def words(self) -> str:
        return WORDS.get(self.cause, self.cause)

    @property
    def blocks(self) -> bool:
        return self.cause in BLOCKING


def diagnose(s: Signals) -> Diagnosis:
    """One cause, and the evidence for it. Pure: the order below is the order of trust -
    the network before Gateway, Gateway before our socket, the socket before the data."""
    ev: list[str] = []
    if s.internet_ok is False and s.ibkr_reachable is not True:
        ev.append("no answer from the internet or from IBKR's servers")
        return Diagnosis(NETWORK_DOWN, ev)
    if not s.connected:
        ev.append(f"watcher not connected for {s.disconnected_for_s:.0f}s"
                  + (f" ({s.connect_error[:80]})" if s.connect_error else ""))  # fmt: skip
        if s.gw_logging_in:
            return Diagnosis(LOGIN_NEEDED, [*ev, "the launcher's Gateway is at its login"])
        if s.gw_process is False:
            return Diagnosis(GATEWAY_DOWN, [*ev, "no IB Gateway process"])
        if s.port_open is False:
            if s.gw_process:
                why = "Gateway runs but port 4001 is closed: at its login window, or restarting"
                return Diagnosis(LOGIN_NEEDED, [*ev, why])
            return Diagnosis(GATEWAY_DOWN, [*ev, "nothing listening on port 4001"])
        if s.client_id_clash:
            return Diagnosis(CLIENT_ID_IN_USE, [*ev, f"client id {s.client_id} already in use"])
        if s.gw_api_ok is False:
            return Diagnosis(GATEWAY_HUNG, [*ev, "port 4001 open but the API does not answer"])
        return Diagnosis(WATCHER_DISCONNECTED, ev)
    if not s.server_ok:
        ev.append(f"Gateway reports its link to IBKR down ({s.last_error[:80]})")
        if s.internet_ok is not False:
            ev.append("the internet answers" if s.internet_ok else "internet not probed")
        return Diagnosis(LINK_DOWN, ev)
    if s.heartbeat_age_s is not None and s.heartbeat_age_s > HEARTBEAT_STALE_S:
        return Diagnosis(GATEWAY_HUNG, [f"no heartbeat answer for {s.heartbeat_age_s:.0f}s"])
    if s.competing:
        return Diagnosis(COMPETING, ["IBKR error 10197 and no bar since"])
    if s.in_hours and s.delayed:
        return Diagnosis(DELAYED, ["the probe quote came back delayed / not subscribed"])
    # 10:03-16:00: in the closing auction (16:00-16:10) nothing trades and streams fall
    # silent, which is not a dead stream.
    if (s.in_hours and OPEN_GRACE_MIN <= s.minutes_since_open < 360 and s.index_streaming
            and s.index_age_s is not None and s.index_age_s > STALE_S):  # fmt: skip
        return Diagnosis(STALE_STREAM, [f"the index's last streamed bar is {s.index_age_s:.0f}s "
                                        "old; heartbeat fine"])  # fmt: skip
    if s.pacing_recent >= PACING_HITS:
        return Diagnosis(PACING, [f"{s.pacing_recent} pacing violations in 10 minutes"])
    return Diagnosis(HEALTHY)


def port_open(host: str = "127.0.0.1", port: int = 4001, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ibkr_servers(jts_ini: Path | None = None) -> list[tuple[str, int]]:
    """IBKR's login servers as Gateway's jts.ini names them (SupportsSSL, Peer)."""
    from asxbot.localdir import real_local_appdata

    p = jts_ini or real_local_appdata() / "Programs" / "ibgateway" / "jts.ini"
    out: list[tuple[str, int]] = []
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for line in text.splitlines():
        key, _, val = line.partition("=")
        if key.strip() not in ("SupportsSSL", "Peer"):
            continue
        for part in val.split(";"):
            hostport = part.split(",")[0].strip()
            host, _, port = hostport.partition(":")
            if host and port.isdigit() and (host, int(port)) not in out:
                out.append((host, int(port)))
    return out or [("ndc1.ibllc.com", 4000)]


def reachable(targets, timeout: float = 2.0) -> bool:
    for host, port in targets:
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return True
        except OSError:
            continue
    return False


INTERNET = (("1.1.1.1", 443), ("8.8.8.8", 443), ("api.telegram.org", 443))


_PROBES: dict = {}


def _cached(key: str, fn):
    """The probes run on the watcher's thread: at most once a minute each."""
    import time

    hit = _PROBES.get(key)
    if hit is not None and time.monotonic() - hit[0] < PROBE_CACHE_S:
        return hit[1]
    val = fn()
    _PROBES[key] = (time.monotonic(), val)
    return val


def net_probe() -> tuple[bool, bool]:
    """(the internet answers, IBKR's servers answer): TCP connections only, no request."""
    return _cached("net", lambda: (reachable(INTERNET), reachable(ibkr_servers())))


def supervisor_view(now: datetime) -> dict:
    """The supervisor's last observation, if fresh (supervisor.json `observation`)."""
    from asxbot.ibkr.supervisor import SUPERVISOR_STATE, read_json

    st = read_json(SUPERVISOR_STATE)
    obs = st.get("observation") or {}
    try:
        at = datetime.fromisoformat(str(st.get("last_check")))
        at = at if at.tzinfo else at.replace(tzinfo=SYD)
    except (TypeError, ValueError):
        return {}
    if now - at > SUPERVISOR_FRESH:
        return {}
    return obs


def gather(gw, now: datetime, index: str = "^AXJO", delayed: bool = False,
           probe_net: bool | None = None, observe_gateway=None) -> Signals:  # fmt: skip
    """The signals for one tick, from the watcher's LiveGateway `gw`."""
    from asxbot.ibkr.gateway import market_hours
    from asxbot.ibkr.supervisor import trading_day

    now = now.astimezone(SYD)
    h = gw.health
    s = Signals(now=now, in_hours=market_hours(now), trading_day=trading_day(now.date()))
    if s.in_hours:
        opened = now.replace(hour=10, minute=0, second=0, microsecond=0)
        s.minutes_since_open = (now - opened).total_seconds() / 60.0
    s.connected = bool(h.connected and gw.ib is not None)
    s.ready = bool(gw.ready)
    s.server_ok = bool(h.server_ok)
    s.refused = bool(h.refused)
    s.heartbeat_age_s = gw.heartbeat_age_s()
    s.last_error = str(h.last_error or "")
    s.connect_error = str(getattr(gw, "last_connect_error", "") or "")
    s.client_id = int(gw.s.client_id)
    s.client_id_clash = bool(gw.clash_recent(CLASH_WINDOW_S))
    s.competing = bool(gw.competing)
    s.pacing_recent = int(gw.pacing_recent())
    s.delayed = bool(delayed)
    s.index_streaming = bool(gw.streaming(index))
    s.index_age_s = gw.data_age_s(index) if s.index_streaming else None
    s.disconnected_for_s = float(gw.disconnected_for_s())
    if not s.connected:
        obs = (observe_gateway or _observe_gateway)(now)
        s.port_open = obs.get("port_open")
        s.gw_process = obs.get("gw_process")
        s.gw_api_ok = obs.get("api_ok")
        s.gw_link_ok = obs.get("link_ok")
        s.gw_logging_in = bool(obs.get("logging_in"))
        s.login_stored = obs.get("login_stored")
    trouble = not s.connected or not s.server_ok
    if probe_net is None:
        probe_net = trouble
    if probe_net:
        s.internet_ok, s.ibkr_reachable = net_probe()
    return s


def _observe_gateway(now: datetime) -> dict:
    """Gateway from outside: the port (probed now), the process, and the supervisor's
    fresh observation for the rest."""
    from asxbot.ibkr.supervisor import gateway_pids

    obs = dict(supervisor_view(now))
    obs["port_open"] = port_open()
    try:
        pids = _cached("pids", gateway_pids)
        obs["gw_process"] = bool(pids) or bool(obs.get("launcher_alive"))
    except Exception:  # noqa: BLE001 - tasklist failing is not evidence either way
        obs["gw_process"] = None
    return obs


# -- the episode: diagnose, act, verify --------------------------------------------------
def state_path() -> Path:
    from asxbot.ibkr.supervisor import STATE_DIR

    return STATE_DIR / "doctor.json"


def restart_request_path() -> Path:
    from asxbot.ibkr.supervisor import STATE_DIR

    return STATE_DIR / "RESTART_REQUEST.json"


def _iso(t: datetime) -> str:
    return t.astimezone(SYD).isoformat(timespec="seconds")


def _t(s) -> datetime | None:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=SYD)


def tell_window(now: datetime, is_trading_day) -> bool:
    now = now.astimezone(SYD)
    return is_trading_day(now.date()) and TELL_HOURS[0] <= now.time() < TELL_HOURS[1]


@dataclass
class Actions:
    """What the doctor can do (fakes in the tests)."""

    resubscribe: object = None  # why -> bool
    reconnect: object = None  # why -> bool
    use_client_id: object = None  # (id, why) -> bool
    slow_history: object = None  # (factor, seconds) -> None
    run_supervisor: object = None  # () -> (ok, text)
    request_restart: object = None  # why -> bool
    send: object = None  # text -> bool


def run_supervisor_task() -> tuple[bool, str]:
    from asxbot import proc

    r = proc.run(["schtasks", "/run", "/tn", SUPERVISOR_TASK], capture_output=True, text=True,
                 timeout=30)  # fmt: skip
    return r.returncode == 0, (r.stdout or r.stderr or "").strip()


def write_restart_request(why: str, now: datetime | None = None) -> bool:
    now = now or datetime.now(SYD)
    p = restart_request_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(json.dumps({"at": _iso(now), "why": why}), p)
        return True
    except OSError as e:
        log.warning("could not write the Gateway restart request: %s", e)
        return False


def stream_dead_witness(gw, index: str, now: datetime | None = None) -> tuple[bool, str]:
    """A second witness before Gateway is restarted for a silent stream: does IBKR's
    HISTORY hold index bars from the last few minutes? Then the market is trading and only
    the stream is dead - a restart is the fix. If history is silent too, the market data
    itself has stopped (or the market is quiet), and a restart would not help."""
    now = (now or datetime.now(SYD)).astimezone(SYD)
    try:
        df = gw.history_sync(index, "600 S", timeout=25.0)
    except Exception as e:  # noqa: BLE001
        return False, f"the history check failed: {e!r}"
    if df is None or not len(df):
        return False, "IBKR's history has no recent index bars either"
    newest = df.index.max().to_pydatetime().astimezone(SYD)
    lag = (now - newest).total_seconds()
    if lag <= 240:
        return True, f"history has index bars to {newest:%H:%M} while the stream is silent"
    return False, f"history's newest index bar is {lag / 60:.0f} min old too"


def gateway_actions(gw, send=None, index: str = "^AXJO") -> Actions:
    """The real actions on the watcher's LiveGateway. Re-requesting is for the index only
    (IBKR allows ~60 new real-time bar requests in 10 minutes; re-requesting all ~90 streams
    would spend the budget and could make the staleness it is meant to cure)."""

    def restart(why: str) -> bool:
        dead, evidence = stream_dead_witness(gw, index)
        log.warning("connection doctor: second witness before a Gateway restart: %s", evidence)
        return dead and write_restart_request(f"{why}; {evidence}")

    return Actions(
        resubscribe=lambda why: gw.request_resubscribe(why, [index]),
        reconnect=lambda why: gw.request_reconnect(why),
        use_client_id=lambda cid, why: gw.use_client_id(cid, why),
        slow_history=lambda f, secs: gw.slow_history(f, secs),
        run_supervisor=run_supervisor_task,
        request_restart=restart,
        send=send,
    )


class Doctor:
    """One episode at a time: opened when a cause appears, walked up its ladder, closed
    (verified) when the diagnosis is healthy again."""

    def __init__(self, actions: Actions, events=None, path: Path | None = None,
                 is_trading_day=None):  # fmt: skip
        from asxbot.ibkr.supervisor import trading_day

        self.actions = actions
        self.events = events
        self.path = path or state_path()
        self.is_trading_day = is_trading_day or trading_day
        self.st = self._load()
        self.last: Diagnosis = Diagnosis(HEALTHY)
        self.spare = list(SPARE_CLIENT_IDS)

    # -- state ---------------------------------------------------------------
    def _load(self) -> dict:
        try:
            st = json.loads(Path(self.path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            st = {}
        return st if isinstance(st, dict) else {}

    def _save(self) -> None:
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(json.dumps(self.st, indent=2, default=str), Path(self.path))
        except OSError as e:
            log.warning("could not write the connection doctor's state: %s", e)

    def _event(self, what: str, **kw) -> None:
        if self.events is not None:
            try:
                self.events.append("ibkr_doctor", {"event": what, **kw})
            except Exception:  # noqa: BLE001
                pass

    @property
    def episode(self) -> dict | None:
        return self.st.get("episode")

    def block(self) -> tuple[bool, str]:
        """Should new entries wait? Yes while a blocking episode is open."""
        ep = self.episode
        if ep and ep.get("cause") in BLOCKING:
            return True, f"connection doctor: {WORDS.get(ep['cause'], ep['cause'])}"
        return False, ""

    # -- one tick ------------------------------------------------------------
    def tick(self, s: Signals) -> Diagnosis:
        now = s.now.astimezone(SYD)
        d = diagnose(s)
        self.last = d
        ep = self.episode
        self.st["last"] = {"at": _iso(now), "cause": d.cause, "evidence": d.evidence}
        if d.cause == HEALTHY:
            if ep:
                self._close(ep, now)
            self._save()
            return d
        if not ep:
            ep = self._open(d, now)
        elif ep["cause"] != d.cause:
            self._change(ep, d, now)
        else:
            ep["evidence"] = d.evidence
        self._advance(ep, d, now, s)
        self.st["episode"] = ep
        self._save()
        return d

    def _open(self, d: Diagnosis, now: datetime) -> dict:
        ep = {"cause": d.cause, "since": _iso(now), "first_cause": d.cause, "rung": -1,
              "rung_at": None, "deadline": None, "actions": [], "told_at": None,
              "evidence": d.evidence, "causes": [d.cause]}  # fmt: skip
        log.error("connection doctor: %s (%s)", d.words, "; ".join(d.evidence))
        self._event("opened", cause=d.cause, evidence=d.evidence, at=_iso(now))
        return ep

    def _change(self, ep: dict, d: Diagnosis, now: datetime) -> None:
        log.warning("connection doctor: the cause changed: %s -> %s (%s)", ep["cause"], d.cause,
                    "; ".join(d.evidence))  # fmt: skip
        self._event("changed", was=ep["cause"], cause=d.cause, evidence=d.evidence, at=_iso(now))
        ep["cause"] = d.cause
        ep["evidence"] = d.evidence
        ep["rung"], ep["rung_at"], ep["deadline"] = -1, None, None
        if d.cause not in ep["causes"]:
            ep["causes"].append(d.cause)

    def _advance(self, ep: dict, d: Diagnosis, now: datetime, s: Signals) -> None:
        ladder = LADDERS.get(d.cause) or [Step("tell_rick", 0)]
        deadline = _t(ep.get("deadline"))
        if ep["rung"] >= 0 and deadline is not None and now < deadline:
            return  # verifying the current rung
        if ep["rung"] >= len(ladder) - 1:
            if ep["rung"] >= 0 and not ep.get("told_at") and d.cause not in SUPERVISOR_SPEAKS:
                self._tell(ep, d, now)  # out of rungs and Rick not told yet (e.g. overnight)
            return
        if ep["rung"] >= 0:
            prev = ladder[ep["rung"]].action
            log.warning("connection doctor: %s did not clear it within %ds; next step",
                        WHAT_DID.get(prev, prev), int(ladder[ep["rung"]].wait_s))  # fmt: skip
        ep["rung"] += 1
        step = ladder[ep["rung"]]
        ok, note = self._do(step.action, ep, d, now, s)
        ep["rung_at"] = _iso(now)
        # A step that could not be taken is not waited on: the next one follows at the next
        # tick (telling Rick outside his hours is retried by the last rung instead).
        wait = step.wait_s if ok or step.action == "tell_rick" else 0.0
        ep["deadline"] = _iso(now + timedelta(seconds=wait))
        ep["actions"].append({"at": _iso(now), "action": step.action, "ok": ok, "note": note})
        self._event("action", cause=d.cause, action=step.action, ok=ok, note=note, at=_iso(now))
        log.warning("connection doctor [%s]: %s -> %s%s", d.cause, step.action,
                    "ok" if ok else "FAILED", f" ({note})" if note else "")  # fmt: skip

    def _do(self, action: str, ep: dict, d: Diagnosis, now: datetime,
            s: Signals) -> tuple[bool, str]:  # fmt: skip
        a = self.actions
        why = f"doctor: {d.cause}"
        try:
            if action == "wait":
                return True, ""
            if action == "resubscribe":
                return bool(a.resubscribe and a.resubscribe(why)), ""
            if action == "reconnect":
                return bool(a.reconnect and a.reconnect(why)), ""
            if action == "switch_client_id":
                used = set(self.st.get("client_ids_tried") or [])
                spare = next((c for c in self.spare if c not in used and c != s.client_id), None)
                if spare is None:
                    return False, "no spare client id left"
                self.st["client_ids_tried"] = sorted(used | {spare})
                return bool(a.use_client_id and a.use_client_id(spare, why)), f"client id {spare}"
            if action == "slow_history":
                if a.slow_history:
                    a.slow_history(2.0, 600.0)
                return True, "history x2 slower for 10 min"
            if action == "run_supervisor":
                if a.run_supervisor is None:
                    return False, "no supervisor hook"
                ok, text = a.run_supervisor()
                return bool(ok), str(text)[:120]
            if action == "request_restart":
                if s.login_stored is False:
                    # A restart with no stored login leaves Gateway at its login window until
                    # Rick types it: worse than a stale stream (entries are paused either way).
                    return False, "not restarting: no stored IBKR login, Rick is told instead"
                ok = bool(a.request_restart and a.request_restart(why))
                if ok and a.run_supervisor is not None:
                    a.run_supervisor()
                return ok, ""
            if action == "tell_rick":
                if d.cause in SUPERVISOR_SPEAKS:
                    return True, "the supervisor tells Rick"
                return self._tell(ep, d, now), ""
        except Exception as e:  # noqa: BLE001 - the doctor must never take the watcher down
            log.exception("connection doctor: %s failed: %s", action, e)
            return False, f"{type(e).__name__}: {e}"
        return False, f"unknown action {action}"

    def _tell(self, ep: dict, d: Diagnosis, now: datetime) -> bool:
        if ep.get("told_at"):
            return True
        if not tell_window(now, self.is_trading_day) or self.actions.send is None:
            return False
        since = _t(ep.get("since")) or now
        tried = [WHAT_DID.get(x["action"], x["action"]) for x in ep.get("actions", [])
                 if x["action"] not in ("wait", "tell_rick")]  # fmt: skip
        text = (f"IBKR prices stopped at {since:%H:%M}: {d.words}. New entries are paused; "
                "stops and exits keep working.")
        if tried:
            text += f" Tried: {'; '.join(dict.fromkeys(tried))}."
        text += " " + _what_now(d.cause)
        ok = bool(self.actions.send(_escape(text)))
        if ok:
            ep["told_at"] = _iso(now)
            self._event("told", cause=d.cause, at=_iso(now), text=text)
        return ok

    def _close(self, ep: dict, now: datetime) -> None:
        since = _t(ep.get("since")) or now
        mins = (now - since).total_seconds() / 60.0
        acts = [x for x in ep.get("actions", []) if x["action"] not in ("wait", "tell_rick")]
        by = (WHAT_DID.get(acts[-1]["action"], acts[-1]["action"]) if acts
              else "recovered by itself")  # fmt: skip
        log.warning("connection doctor: verified healthy at %s after %.1f min (%s; was: %s)",
                    f"{now:%H:%M:%S}", mins, by, ", ".join(ep.get("causes", [])))  # fmt: skip
        self._event("closed", causes=ep.get("causes"), since=ep.get("since"), at=_iso(now),
                    minutes=round(mins, 1), fixed_by=by, actions=ep.get("actions"))  # fmt: skip
        told = bool(ep.get("told_at"))
        network = NETWORK_DOWN in ep.get("causes", [])
        if (told or (network and mins >= 2)) and self.actions.send is not None and \
                tell_window(now, self.is_trading_day):  # fmt: skip
            what = WORDS.get(ep.get("first_cause"), ep.get("first_cause"))
            text = (f"IBKR prices are back at {now:%H:%M} after {mins:.0f} min ({what}; "
                    f"{by}). New entries are allowed again.")  # fmt: skip
            self.actions.send(_escape(text))
        history = list(self.st.get("history") or [])
        history.append({"since": ep.get("since"), "until": _iso(now), "causes": ep.get("causes"),
                        "fixed_by": by, "told": told})  # fmt: skip
        self.st["history"] = history[-50:]
        self.st["episode"] = None
        self.st.pop("client_ids_tried", None)


def _what_now(cause: str) -> str:
    return {
        NETWORK_DOWN: "Check the PC's internet connection; it resumes by itself when it is back.",
        LINK_DOWN: ("Gateway usually reconnects by itself; if not, the supervisor restarts it "
                    "after 10 minutes, which needs your phone approval."),
        COMPETING: ("Log out of IBKR on the phone or the website (or close that session); the "
                    "prices come back by themselves."),
        DELAYED: "Check the ASX market data subscription in IBKR's client portal.",
        STALE_STREAM: ("If it does not come back soon, restarting IB Gateway will fix it (it "
                       "needs your login and phone approval)."),
        CLIENT_ID_IN_USE: "An old watcher may still be connected; nothing to do unless it lasts.",
        WATCHER_DISCONNECTED: "It keeps retrying by itself.",
        GATEWAY_HUNG: "The supervisor restarts Gateway if it stays like this.",
    }.get(cause, "It keeps trying by itself.")


def _escape(text: str) -> str:
    from html import escape

    return escape(text)


# -- the command-line view ----------------------------------------------------------------
def status_lines(now: datetime | None = None) -> list[str]:
    """`asxbot ibkr doctor`: the doctor's state and the supervisor's, in plain words."""
    from asxbot.ibkr.supervisor import SUPERVISOR_STATE, read_json

    now = (now or datetime.now(SYD)).astimezone(SYD)
    st = read_json(state_path())
    sup = read_json(SUPERVISOR_STATE)
    out = []
    last = st.get("last") or {}
    if last:
        out.append(f"doctor (in the watcher), last tick {str(last.get('at'))[11:19]}: "
                   f"{WORDS.get(last.get('cause'), last.get('cause'))}")  # fmt: skip
    else:
        out.append("doctor: no tick recorded yet (it runs inside the watcher)")
    ep = st.get("episode")
    if ep:
        out.append(f"  open episode since {str(ep.get('since'))[11:16]}: {', '.join(ep['causes'])}")
        for a in ep.get("actions", [])[-6:]:
            out.append(f"    {str(a['at'])[11:19]} {a['action']}: {'ok' if a['ok'] else 'FAILED'}"
                       + (f" ({a['note']})" if a.get("note") else ""))  # fmt: skip
    for h in (st.get("history") or [])[-5:]:
        out.append(f"  earlier: {str(h.get('since'))[5:16]} to {str(h.get('until'))[11:16]} "
                   f"{', '.join(h.get('causes') or [])} - {h.get('fixed_by')}")  # fmt: skip
    if sup:
        out.append(f"supervisor, last check {str(sup.get('last_check'))[11:19]}: "
                   f"{str(sup.get('last_result'))[:160]}")  # fmt: skip
    internet, ibkr = net_probe()
    out.append(f"now: port 4001 {'open' if port_open() else 'CLOSED'}; internet "
               f"{'ok' if internet else 'DOWN'}; IBKR servers {'ok' if ibkr else 'UNREACHABLE'}")
    return out


__all__ = [
    "BLOCKING", "Diagnosis", "Doctor", "LADDERS", "Signals", "Step", "WORDS", "Actions",
    "diagnose", "gateway_actions", "gather", "net_probe", "status_lines",
]  # fmt: skip
