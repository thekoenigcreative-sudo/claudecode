"""The connection doctor (ibkr/doctor.py): each cause diagnosed from its evidence, each
ladder walked with verification and escalation, Rick told at most once an episode and once
when it is fixed - and the chaos paths end to end on the persistent connection (live.py)
against the fake IB that misbehaves on demand (tests/test_ibkr_live.py): a dropped socket,
a stream that stops, a client id held by another connection, a competing session, the link
to IBKR lost, Gateway killed, pacing. Plus the supervisor's side: no restart while the PC is
offline, the doctor's restart request honoured, the "back" line. No network, no Gateway."""

import importlib.util
import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from asxbot.ibkr import doctor as D
from asxbot.ibkr import supervisor as S

SYD = ZoneInfo("Australia/Sydney")
MON = datetime(2026, 9, 28, 11, 0, tzinfo=SYD)  # a Monday, market hours


def trading(d):
    return d.weekday() < 5


def sig(now=MON, **kw):
    base = dict(now=now, in_hours=True, trading_day=True, minutes_since_open=60.0,
                connected=True, ready=True, server_ok=True, heartbeat_age_s=5.0,
                client_id=41, index_streaming=True, index_age_s=3.0, port_open=True,
                internet_ok=None, ibkr_reachable=None)  # fmt: skip
    base.update(kw)
    return D.Signals(**base)


# --------------------------------------------------------------------------
# diagnose: one cause each, from its evidence
# --------------------------------------------------------------------------
@pytest.mark.parametrize("kw,cause", [
    ({}, D.HEALTHY),
    (dict(internet_ok=False, ibkr_reachable=False, connected=False), D.NETWORK_DOWN),
    (dict(connected=False, gw_process=False, port_open=False), D.GATEWAY_DOWN),
    (dict(connected=False, gw_process=None, port_open=False), D.GATEWAY_DOWN),
    (dict(connected=False, gw_logging_in=True, port_open=False), D.LOGIN_NEEDED),
    (dict(connected=False, gw_process=True, port_open=False), D.LOGIN_NEEDED),
    (dict(connected=False, gw_process=True, client_id_clash=True), D.CLIENT_ID_IN_USE),
    (dict(connected=False, gw_process=True, gw_api_ok=False), D.GATEWAY_HUNG),
    (dict(connected=False, gw_process=True), D.WATCHER_DISCONNECTED),
    (dict(server_ok=False, ready=False, internet_ok=True), D.LINK_DOWN),
    (dict(heartbeat_age_s=400.0), D.GATEWAY_HUNG),
    (dict(competing=True), D.COMPETING),
    (dict(delayed=True), D.DELAYED),
    (dict(index_age_s=300.0), D.STALE_STREAM),
    (dict(pacing_recent=4), D.PACING),
])  # fmt: skip
def test_each_cause_is_named_from_its_evidence(kw, cause):
    d = D.diagnose(sig(**kw))
    assert d.cause == cause
    assert d.blocks == (cause in D.BLOCKING)
    assert (cause == D.HEALTHY) or d.evidence


def test_the_internet_down_but_ibkr_answering_is_not_called_offline():
    d = D.diagnose(sig(internet_ok=False, ibkr_reachable=True, server_ok=False))
    assert d.cause == D.LINK_DOWN


def test_no_stale_stream_in_the_first_minutes_or_outside_hours():
    # before 10:00 nothing streams: every stream's age is its time since subscription
    assert D.diagnose(sig(minutes_since_open=1.0, index_age_s=9000.0)).cause == D.HEALTHY
    assert D.diagnose(sig(in_hours=False, index_age_s=9000.0)).cause == D.HEALTHY
    assert D.diagnose(sig(in_hours=False, delayed=True)).cause == D.HEALTHY
    assert D.diagnose(sig(index_streaming=False, index_age_s=None)).cause == D.HEALTHY


def test_pacing_does_not_pause_entries():
    assert not D.diagnose(sig(pacing_recent=5)).blocks


# --------------------------------------------------------------------------
# the ladders, with fake actions and a clock that moves
# --------------------------------------------------------------------------
class Acts:
    def __init__(self, send_ok=True, restart_ok=True):
        self.calls = []
        self.sent = []
        self.send_ok = send_ok
        self.restart_ok = restart_ok

    def actions(self):
        return D.Actions(
            resubscribe=lambda why: self.calls.append(("resubscribe", why)) or True,
            reconnect=lambda why: self.calls.append(("reconnect", why)) or True,
            use_client_id=lambda cid, why: self.calls.append(("client_id", cid)) or True,
            slow_history=lambda f, s: self.calls.append(("slow", f, s)),
            run_supervisor=lambda: (self.calls.append(("supervisor",)) or True, "SUCCESS"),
            request_restart=lambda why: self.calls.append(("restart", why)) or self.restart_ok,
            send=self.send,
        )  # fmt: skip

    def send(self, text):
        if self.send_ok:
            self.sent.append(text)
        return self.send_ok

    def names(self):
        return [c[0] for c in self.calls]


class Events:
    def __init__(self):
        self.rows = []

    def append(self, kind, row):
        self.rows.append((kind, row))


def doctor(tmp_path, acts, **kw):
    return D.Doctor(acts.actions(), Events(), path=tmp_path / "doctor.json",
                    is_trading_day=trading, **kw)  # fmt: skip


def run(doc, t0, seconds, **kw):
    """Tick every 30 s for `seconds` with the same signals."""
    t = t0
    while t <= t0 + timedelta(seconds=seconds):
        doc.tick(sig(now=t, **kw))
        t += timedelta(seconds=30)
    return t


def test_a_stale_stream_is_re_requested_and_verified_healthy(tmp_path):
    acts = Acts()
    doc = doctor(tmp_path, acts)
    doc.tick(sig(index_age_s=200.0))
    assert acts.names() == ["resubscribe"]
    assert doc.block()[0] and "stopped sending prices" in doc.block()[1]
    doc.tick(sig(now=MON + timedelta(seconds=30), index_age_s=2.0))  # bars flow again
    assert doc.episode is None and not doc.block()[0]
    hist = json.loads((tmp_path / "doctor.json").read_text())["history"][-1]
    assert hist["fixed_by"] == "re-requested every price stream" and not hist["told"]
    assert acts.sent == []  # fixed within the first rung: Rick is not bothered
    kinds = [r["event"] for k, r in doc.events.rows if k == "ibkr_doctor"]
    assert kinds == ["opened", "action", "closed"]


def test_a_stale_stream_escalates_resubscribe_reconnect_restart_then_tells_rick(tmp_path):
    acts = Acts()
    doc = doctor(tmp_path, acts)
    stale = dict(index_age_s=300.0, login_stored=True)
    t = run(doc, MON, 60, **stale)  # ticks at 0, 30, 60 s
    assert acts.names() == ["resubscribe"]  # verifying for 90 s
    t = run(doc, t, 90, **stale)  # 90 s: the deadline, the next rung
    assert acts.names() == ["resubscribe", "reconnect"]
    t = run(doc, t, 450, **stale)  # 210 s: reconnect's 120 s are up
    assert acts.names() == ["resubscribe", "reconnect", "restart", "supervisor"]
    t = run(doc, t, 0, **stale)  # 690 s: the restart's 480 s are up
    assert len(acts.sent) == 1
    msg = acts.sent[0]
    assert "stopped sending prices" in msg and "New entries are paused" in msg
    assert "re-requested every price stream" in msg and "restart IB Gateway" in msg
    run(doc, t, 300, index_age_s=300.0, login_stored=True)
    assert len(acts.sent) == 1  # told once
    doc.tick(sig(now=t + timedelta(minutes=6), index_age_s=1.0))
    assert len(acts.sent) == 2 and acts.sent[1].startswith("IBKR prices are back at")
    assert "New entries are allowed again" in acts.sent[1] and doc.episode is None


def test_no_restart_is_asked_for_without_a_stored_login_rick_is_told_at_once(tmp_path):
    acts = Acts()
    doc = doctor(tmp_path, acts)
    run(doc, MON, 90 + 120 + 60, index_age_s=300.0, login_stored=False)
    assert "restart" not in acts.names()
    ep = doc.episode
    assert any(a["action"] == "request_restart" and not a["ok"] and "no stored" in a["note"]
               for a in ep["actions"])  # fmt: skip
    assert len(acts.sent) == 1  # the next rung came at once, not 8 minutes later


def test_a_client_id_clash_moves_to_a_spare_id(tmp_path):
    acts = Acts()
    doc = doctor(tmp_path, acts)
    doc.tick(sig(connected=False, ready=False, gw_process=True, client_id_clash=True))
    assert acts.calls == [("client_id", 44)]
    doc.tick(sig(now=MON + timedelta(seconds=100), connected=False, ready=False,
                 gw_process=True, client_id_clash=True, client_id=44))  # fmt: skip
    assert acts.calls[-1] == ("client_id", 47)  # the second spare, never the one in use
    doc.tick(sig(now=MON + timedelta(seconds=130), client_id=45))
    assert doc.episode is None and acts.sent == []


def test_the_network_down_waits_then_says_so_when_it_is_back(tmp_path):
    acts = Acts(send_ok=False)  # no internet: Telegram cannot be reached either
    doc = doctor(tmp_path, acts)
    down = dict(connected=False, ready=False, internet_ok=False, ibkr_reachable=False)
    t = run(doc, MON, 600, **down)
    assert "reconnect" not in acts.names() and "restart" not in acts.names()
    assert doc.block()[0] and "internet" in doc.block()[1]
    acts.send_ok = True
    doc.tick(sig(now=t))
    assert len(acts.sent) == 1 and "IBKR prices are back" in acts.sent[0]
    assert "cannot reach the internet" in acts.sent[0]


def test_a_competing_session_is_explained_to_rick_and_resumes_by_itself(tmp_path):
    acts = Acts()
    doc = doctor(tmp_path, acts)
    t = run(doc, MON, 60, competing=True)
    assert len(acts.sent) == 1
    assert "logged in to IBKR with the same username" in acts.sent[0]
    assert "Log out of IBKR on the phone" in acts.sent[0]
    doc.tick(sig(now=t + timedelta(seconds=30)))
    assert len(acts.sent) == 2 and "are back" in acts.sent[1]


def test_a_short_link_blip_recovers_quietly(tmp_path):
    acts = Acts()
    doc = doctor(tmp_path, acts)
    t = run(doc, MON, 60, server_ok=False, ready=False, internet_ok=True)
    doc.tick(sig(now=t))
    assert acts.sent == [] and doc.episode is None  # 22:15 on 25 Sep was 30 s: no message
    hist = json.loads((tmp_path / "doctor.json").read_text())["history"][-1]
    assert hist["fixed_by"] == "recovered by itself"


def test_gateway_down_asks_the_supervisor_now_and_leaves_the_message_to_it(tmp_path):
    acts = Acts()
    doc = doctor(tmp_path, acts)
    run(doc, MON, 600, connected=False, ready=False, gw_process=False, port_open=False)
    assert acts.names() == ["supervisor"] and acts.sent == []
    doc.tick(sig(now=MON + timedelta(minutes=11)))
    assert doc.episode is None and acts.sent == []  # it did not tell, so no "back" either


def test_pacing_slows_the_history_queue_without_pausing_entries(tmp_path):
    acts = Acts()
    doc = doctor(tmp_path, acts)
    doc.tick(sig(pacing_recent=3))
    assert acts.calls == [("slow", 2.0, 600.0)] and not doc.block()[0]


def test_outside_rick_s_hours_the_message_waits_for_seven(tmp_path):
    acts = Acts()
    doc = doctor(tmp_path, acts)
    early = datetime(2026, 9, 28, 6, 30, tzinfo=SYD)
    t = run(doc, early, 20 * 60, competing=True, in_hours=False)
    assert acts.sent == []
    run(doc, t, 20 * 60, competing=True, in_hours=False)  # past 07:00
    assert len(acts.sent) == 1


def test_a_cause_that_changes_keeps_the_episode_and_starts_the_new_ladder(tmp_path):
    acts = Acts()
    doc = doctor(tmp_path, acts)
    doc.tick(sig(index_age_s=300.0))  # stale -> resubscribe
    doc.tick(sig(now=MON + timedelta(seconds=30), connected=False, ready=False,
                 gw_process=True))  # the reconnect in flight: disconnected  # fmt: skip
    ep = doc.episode
    assert ep["causes"] == [D.STALE_STREAM, D.WATCHER_DISCONNECTED] and ep["rung"] == 0
    doc.tick(sig(now=MON + timedelta(seconds=60)))
    assert doc.episode is None


def test_an_action_that_raises_never_takes_the_watcher_down(tmp_path):
    acts = Acts()
    a = acts.actions()
    a.resubscribe = lambda why: 1 / 0
    doc = D.Doctor(a, Events(), path=tmp_path / "d.json", is_trading_day=trading)
    doc.tick(sig(index_age_s=300.0))
    act = doc.episode["actions"][0]
    assert act["action"] == "resubscribe" and not act["ok"] and "ZeroDivision" in act["note"]


def test_the_state_survives_a_new_doctor(tmp_path):
    acts = Acts()
    doc = doctor(tmp_path, acts)
    doc.tick(sig(competing=True))
    doc2 = doctor(tmp_path, acts)
    assert doc2.episode["cause"] == D.COMPETING and doc2.block()[0]


def test_ibkr_servers_come_from_gateway_s_jts_ini(tmp_path):
    ini = tmp_path / "jts.ini"
    ini.write_text("[Logon]\nSupportsSSL=ndc1.ibllc.com:4000,true,20260924,false;"
                   "sdc1.ibllc.com:4000,true,20260925,false\n[Communication]\n"
                   "Peer=sdc1.ibllc.com:4001\n")  # fmt: skip
    assert D.ibkr_servers(ini) == [("ndc1.ibllc.com", 4000), ("sdc1.ibllc.com", 4000),
                                   ("sdc1.ibllc.com", 4001)]  # fmt: skip
    assert D.ibkr_servers(tmp_path / "missing.ini") == [("ndc1.ibllc.com", 4000)]


# --------------------------------------------------------------------------
# end to end: the persistent connection against the misbehaving fake IB
# --------------------------------------------------------------------------
_spec = importlib.util.spec_from_file_location(
    "ibkr_live_fakes", Path(__file__).with_name("test_ibkr_live.py"))
LT = importlib.util.module_from_spec(_spec)
sys.modules["ibkr_live_fakes"] = LT
_spec.loader.exec_module(LT)


class Clock:
    """A monotonic clock the test moves (data ages, heartbeat ages)."""

    def __init__(self):
        import time

        self.base = time.monotonic()
        self.extra = 0.0

    def __call__(self):
        import time

        return time.monotonic() + self.extra


def gateway(tmp_path, script=None, fake=None, **kw):
    from asxbot.ibkr.live import LiveGateway

    script = script or LT.Script()
    clock = Clock()
    factory = fake or (lambda: LT.FakeIB(script))
    gw = LiveGateway(LT.settings(**kw), ib_factory=factory, clock=clock,
                     wall=LT.Wall(MON), history_dir=tmp_path / "hist")  # fmt: skip
    gw.start()
    return gw, script, clock


def live_doctor(tmp_path, gw, acts):
    a = D.gateway_actions(gw, send=acts.send)
    a.run_supervisor = lambda: (acts.calls.append(("supervisor",)) or True, "ok")
    a.request_restart = lambda why: acts.calls.append(("restart", why)) or True
    return D.Doctor(a, Events(), path=tmp_path / "doctor.json", is_trading_day=trading)


def tick(doc, gw, now, **kw):
    s = D.gather(gw, now, "^AXJO", probe_net=False, observe_gateway=lambda n: kw.pop(
        "outside", {"port_open": True, "gw_process": True}))  # fmt: skip
    return doc.tick(s)


@pytest.fixture
def cleanup():
    made = []
    yield made
    for gw in made:
        gw.stop(timeout=3)


def test_chaos_socket_dropped_reconnects_by_itself_and_the_doctor_verifies(tmp_path, cleanup):
    gw, script, clock = gateway(tmp_path)
    cleanup.append(gw)
    assert LT.wait_for(lambda: gw.ready)
    acts = Acts()
    doc = live_doctor(tmp_path, gw, acts)
    assert tick(doc, gw, MON).cause == D.HEALTHY
    script.instances[-1].drop()
    assert LT.wait_for(lambda: not gw.health.connected or script.connects >= 2)
    assert LT.wait_for(lambda: gw.ready and script.connects >= 2, timeout=5)
    assert tick(doc, gw, MON + timedelta(seconds=30)).cause == D.HEALTHY
    assert gw.reconnects >= 1 and acts.sent == []


def test_chaos_a_stream_that_stops_is_re_requested_and_bars_flow_again(tmp_path, cleanup):
    gw, script, clock = gateway(tmp_path)
    cleanup.append(gw)
    assert LT.wait_for(lambda: gw.ready)
    gw.set_streaming(["^AXJO"])
    assert LT.wait_for(lambda: gw.streaming("^AXJO"))
    script.instances[-1].push_bar("XJO", MON, 8800.0, 0.0)
    assert LT.wait_for(lambda: (gw.data_age_s("^AXJO") or 99) < 5)
    acts = Acts()
    doc = live_doctor(tmp_path, gw, acts)
    before = script.rt_requests.count("XJO")
    clock.extra += 300.0  # five minutes with no bar, heartbeat still answering
    assert LT.wait_for(lambda: gw.heartbeat_age_s() is not None and gw.heartbeat_age_s() < 1)
    d = tick(doc, gw, MON + timedelta(minutes=5))
    assert d.cause == D.STALE_STREAM and doc.block()[0]
    assert LT.wait_for(lambda: script.rt_requests.count("XJO") == before + 1)  # re-requested
    script.instances[-1].push_bar("XJO", MON + timedelta(minutes=5), 8801.0, 0.0)
    assert LT.wait_for(lambda: (gw.data_age_s("^AXJO") or 99) < 5)
    assert tick(doc, gw, MON + timedelta(minutes=6)).cause == D.HEALTHY
    assert doc.episode is None and not doc.block()[0]


class ClashIB:
    """Refuses client ids in `held` as Gateway does: 326, then no API start."""

    def __init__(self, script, held):
        self.inner = LT.FakeIB(script)
        self.held = held

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def connectAsync(self, host, port, clientId, timeout, readonly, fetchFields):
        if clientId in self.held:
            self.inner.s.connects += 1
            self.inner.errorEvent.emit(-1, 326, "Unable to connect as the client id is already "
                                       "in use. Retry with a unique client id.", None)  # fmt: skip
            raise TimeoutError()
        return await self.inner.connectAsync(host, port, clientId, timeout, readonly,
                                             fetchFields)  # fmt: skip


def test_chaos_client_id_in_use_switches_to_a_spare_and_connects(tmp_path, cleanup):
    script = LT.Script()
    gw, script, clock = gateway(tmp_path, script, fake=lambda: ClashIB(script, {41}))
    cleanup.append(gw)
    assert LT.wait_for(lambda: gw.clash_recent())
    assert not gw.ready
    acts = Acts()
    doc = live_doctor(tmp_path, gw, acts)
    d = tick(doc, gw, MON, outside={"port_open": True, "gw_process": True})
    assert d.cause == D.CLIENT_ID_IN_USE
    assert LT.wait_for(lambda: gw.ready, timeout=5) and gw.s.client_id == 44
    assert tick(doc, gw, MON + timedelta(seconds=30)).cause == D.HEALTHY
    assert doc.episode is None


def test_chaos_competing_session_pauses_entries_until_data_flows(tmp_path, cleanup):
    gw, script, clock = gateway(tmp_path)
    cleanup.append(gw)
    assert LT.wait_for(lambda: gw.ready)
    gw.set_streaming(["^AXJO"])
    assert LT.wait_for(lambda: gw.streaming("^AXJO"))
    ib = script.instances[-1]
    ib.loop.call_soon_threadsafe(ib.errorEvent.emit, 7, 10197,
                                 "No market data during competing live session", None)
    assert LT.wait_for(lambda: gw.competing)
    assert gw.entries_allowed(MON)[0] is False
    acts = Acts()
    doc = live_doctor(tmp_path, gw, acts)
    assert tick(doc, gw, MON).cause == D.COMPETING
    tick(doc, gw, MON + timedelta(seconds=70))
    assert len(acts.sent) == 1 and "same username" in acts.sent[0]
    ib.push_bar("XJO", MON + timedelta(minutes=2), 8800.0, 0.0)
    assert LT.wait_for(lambda: not gw.competing)
    assert tick(doc, gw, MON + timedelta(minutes=3)).cause == D.HEALTHY
    assert len(acts.sent) == 2 and "back" in acts.sent[1]


def test_chaos_link_to_ibkr_lost_then_restored(tmp_path, cleanup):
    gw, script, clock = gateway(tmp_path)
    cleanup.append(gw)
    assert LT.wait_for(lambda: gw.ready)
    acts = Acts()
    doc = live_doctor(tmp_path, gw, acts)
    script.instances[-1].error(1100, "Connectivity between IBKR and Trader Workstation has "
                                     "been lost.")  # fmt: skip
    assert LT.wait_for(lambda: not gw.health.server_ok)
    assert tick(doc, gw, MON).cause == D.LINK_DOWN and doc.block()[0]
    script.instances[-1].error(1102, "Connectivity between IBKR and Trader Workstation has "
                                     "been restored - data maintained.")  # fmt: skip
    assert LT.wait_for(lambda: gw.ready)
    assert tick(doc, gw, MON + timedelta(seconds=40)).cause == D.HEALTHY
    assert acts.sent == []  # a 40-second blip: quiet


def test_chaos_gateway_killed_asks_the_supervisor_and_verifies_when_it_is_back(tmp_path, cleanup):
    gw, script, clock = gateway(tmp_path)
    cleanup.append(gw)
    assert LT.wait_for(lambda: gw.ready)
    acts = Acts()
    doc = live_doctor(tmp_path, gw, acts)
    script.refuse = 10_000  # killed: every connection refused
    script.instances[-1].drop()
    assert LT.wait_for(lambda: not gw.health.connected)
    d = tick(doc, gw, MON, outside={"port_open": False, "gw_process": False})
    assert d.cause == D.GATEWAY_DOWN and ("supervisor",) in acts.calls
    assert entries_blocked(doc)
    script.refuse = 0  # the supervisor relaunched it
    assert LT.wait_for(lambda: gw.ready, timeout=5)
    assert tick(doc, gw, MON + timedelta(minutes=3)).cause == D.HEALTHY
    assert doc.episode is None


def entries_blocked(doc):
    return doc.block()[0]


def test_chaos_pacing_violations_slow_the_history_queue(tmp_path, cleanup):
    gw, script, clock = gateway(tmp_path)
    cleanup.append(gw)
    assert LT.wait_for(lambda: gw.ready)
    for _ in range(3):
        script.instances[-1].error(420, "Invalid Real-time Query: pacing violation")
    assert LT.wait_for(lambda: gw.pacing_recent() >= 3)
    acts = Acts()
    doc = live_doctor(tmp_path, gw, acts)
    assert tick(doc, gw, MON).cause == D.PACING
    assert gw.history_slow_factor == 2.0 and gw.history_slow_until > clock()
    assert not doc.block()[0]


def test_the_failover_feed_pauses_entries_while_the_doctor_has_an_episode(tmp_path, cleanup):
    from asxbot.arena.intraday import YahooDelayedFeed
    from asxbot.arena.minutes import MinuteBars
    from asxbot.ibkr import feed as F

    gw, script, clock = gateway(tmp_path)
    cleanup.append(gw)
    assert LT.wait_for(lambda: gw.ready)
    acts = Acts()
    doc = live_doctor(tmp_path, gw, acts)
    mb = MinuteBars(tmp_path / "minutes")
    feed = F.FailoverFeed(mb, F.IBKRLiveFeed(mb, gw, None, "^AXJO"),
                          YahooDelayedFeed(mb, 10, None), tmp_path, None, now=MON,
                          doctor=doc)  # fmt: skip
    ib = script.instances[-1]
    ib.loop.call_soon_threadsafe(ib.errorEvent.emit, 7, 10197, "competing", None)
    assert LT.wait_for(lambda: gw.competing)
    feed.check(MON)
    ok, why = feed.entries_allowed(MON)
    assert not ok
    status = json.loads((tmp_path / "arena" / "live_data.json").read_text())
    assert status["doctor"]["cause"] == D.COMPETING


# --------------------------------------------------------------------------
# the supervisor's side
# --------------------------------------------------------------------------
HEALTHY = dict(port_open=True, api_ok=True, link_ok=True)


class SupFakes:
    def __init__(self, tmp_path, observations):
        self.observations = list(observations)
        self.sent, self.killed, self.started, self.lines = [], [], [], []
        self.deps = S.Deps(
            observe=self.observe, send=lambda t: self.sent.append(t) or True,
            kill=lambda pid: self.killed.append(pid) or True,
            start=lambda: (self.started.append(S.LAUNCHER_TASK) or True, "SUCCESS"),
            log=self.lines.append, sleep=lambda s: None, is_trading_day=trading,
            state_path=tmp_path / "supervisor.json", request_path=tmp_path / "RR.json",
        )  # fmt: skip

    def observe(self, now):
        o = self.observations.pop(0) if len(self.observations) > 1 else self.observations[0]
        return replace(o, now=now)


def test_the_supervisor_never_restarts_gateway_while_the_pc_is_offline(tmp_path):
    offline = S.Observation(now=MON, internet_ok=False, ibkr_ok=False, login_stored=True)
    f = SupFakes(tmp_path, [offline])
    for m in range(0, 20, 2):
        plan = S.run_once(MON + timedelta(minutes=m), f.deps)
        assert plan.action == "none" and "offline" in plan.why
    assert f.started == [] and f.killed == []
    st = json.loads((tmp_path / "supervisor.json").read_text())
    assert st["observation"]["internet_ok"] is False and st["outage"]


def test_the_supervisor_honours_the_doctor_s_restart_request(tmp_path):
    req = {"at": MON.isoformat(), "why": "doctor: stale_stream"}
    (tmp_path / "RR.json").write_text(json.dumps(req))
    o = S.Observation(now=MON, **HEALTHY, login_stored=True, launcher_alive=True,
                      launcher_pid=11, java_alive=True, java_pid=12, phase="logged_in",
                      restart_request=req)  # fmt: skip
    f = SupFakes(tmp_path, [o, replace(o, restart_request={})])
    plan = S.run_once(MON, f.deps)
    assert plan.action == "restart" and "connection doctor" in plan.why
    assert f.killed == [12, 11] and f.started == [S.LAUNCHER_TASK]
    assert not (tmp_path / "RR.json").exists()  # answered once
    assert "doctor's restart request answered" in f.lines[-1]


def test_without_a_stored_login_the_doctor_s_request_is_dropped_not_obeyed(tmp_path):
    req = {"at": MON.isoformat(), "why": "doctor: stale_stream"}
    (tmp_path / "RR.json").write_text(json.dumps(req))
    o = S.Observation(now=MON, **HEALTHY, login_stored=False, foreign_pids=[5],
                      restart_request=req)  # fmt: skip
    f = SupFakes(tmp_path, [o])
    plan = S.run_once(MON, f.deps)
    assert plan.action == "none" and "no stored login" in plan.why and f.killed == []
    assert not (tmp_path / "RR.json").exists()


def test_a_stale_restart_request_is_ignored(tmp_path):
    p = tmp_path / "RR.json"
    p.write_text(json.dumps({"at": (MON - timedelta(minutes=20)).isoformat(), "why": "x"}))
    assert S.restart_request(MON, p) == {}
    p.write_text(json.dumps({"at": (MON - timedelta(minutes=2)).isoformat(), "why": "x"}))
    assert S.restart_request(MON, p)["why"] == "x"
    assert S.restart_request(MON, tmp_path / "none.json") == {}


def test_the_doctor_reads_the_supervisor_s_observation(tmp_path, monkeypatch):
    p = tmp_path / "supervisor.json"
    p.write_text(json.dumps({"last_check": MON.isoformat(),
                             "observation": {"port_open": False, "logging_in": True}}))
    monkeypatch.setattr(S, "SUPERVISOR_STATE", p)
    assert D.supervisor_view(MON + timedelta(minutes=1))["logging_in"] is True
    assert D.supervisor_view(MON + timedelta(minutes=10)) == {}  # stale: not evidence


def test_the_doctor_is_read_only_and_starts_nothing_but_the_supervisor_task():
    src = Path(D.__file__).read_text(encoding="utf-8")
    for bad in ("placeOrder", "place_order", "taskkill", "ibgateway.exe", "StartIBC"):
        assert bad not in src
    assert src.count('"schtasks"') == 1 and "SUPERVISOR_TASK" in src
    assert SimpleNamespace  # imported for fakes elsewhere
