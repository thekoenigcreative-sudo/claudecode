"""The IB Gateway supervisor, IBC launcher helpers, the stored login and the 07:20 pre-flight,
with fakes: no Gateway, no IBC, no Telegram, no scheduled task. (25 Sep 2026: Gateway went
down at 08:58 with the Claude session that had started it, and nothing noticed for an hour.)

One real call: the Credential Manager round trip, on Windows, under a throwaway target name.
No real login appears anywhere; the fake one is checked to be absent from every log line,
state file and printed command these helpers produce."""

import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from asxbot.ibkr import credentials, ibc
from asxbot.ibkr import supervisor as S

SYD = ZoneInfo("Australia/Sydney")
MORNING = datetime(2026, 9, 25, 10, 20, tzinfo=SYD)  # a Friday, a trading day
NIGHT = datetime(2026, 9, 25, 23, 50, tzinfo=SYD)
SATURDAY = datetime(2026, 9, 26, 10, 0, tzinfo=SYD)
FAKE = credentials.Login("rick-fake-user", "Fake&Pa%ss!^\"word")


def trading(d):
    return d.weekday() < 5


def obs(now=MORNING, **kw):
    return S.Observation(now=now, **kw)


HEALTHY = dict(port_open=True, api_ok=True, link_ok=True)


def run(o, st=None):
    return S.decide(o, st or {}, trading)


# --------------------------------------------------------------------------
# decide: when to leave Gateway alone
# --------------------------------------------------------------------------
def test_a_healthy_gateway_is_left_alone_whoever_started_it():
    for o in (obs(**HEALTHY, foreign_pids=[52196]),
              obs(**HEALTHY, launcher_alive=True, launcher_pid=7, phase="logged_in")):  # fmt: skip
        plan, st = run(o, {"bad": 1, "link_bad": 3})
        assert plan.action == "none" and plan.kill == [] and not plan.notice
        assert st["bad"] == 0 and st["link_bad"] == 0 and st["outage"] is None


def test_back_to_healthy_closes_the_outage():
    plan, st = run(obs(**HEALTHY), {"outage": {"since": "x", "notified_at": "y"}})
    assert plan.close_outage and st["outage"] is None


def test_paused_does_nothing():
    plan, _ = run(obs(paused=True))
    assert plan.action == "none" and "paused" in plan.why


# --------------------------------------------------------------------------
# decide: when to start or restart it
# --------------------------------------------------------------------------
def test_nothing_running_starts_gateway_at_once():
    plan, st = run(obs(login_stored=True))
    assert plan.action == "launch" and plan.kill == []
    assert st["last_launch_at"] == MORNING.isoformat(timespec="seconds")


def test_a_gateway_that_stops_answering_is_restarted_on_the_second_check():
    o = obs(launcher_alive=True, launcher_pid=11, java_alive=True, java_pid=12,
            phase="logged_in", mode="auto", login_stored=True)  # fmt: skip
    plan, st = run(o)
    assert plan.action == "none" and st["bad"] == 1
    plan, st = run(replace(o, now=MORNING + timedelta(minutes=2)), st)
    assert plan.action == "restart" and plan.kill == [12, 11]


def test_a_link_to_ibkr_that_is_down_gets_ten_minutes_before_a_restart():
    o = obs(port_open=True, api_ok=True, link_ok=False, launcher_alive=True, launcher_pid=11,
            phase="logged_in", mode="auto", login_stored=True)  # fmt: skip
    st = {}
    for i in range(1, S.LINK_DOWN_CHECKS):
        plan, st = run(replace(o, now=MORNING + timedelta(minutes=2 * i)), st)
        assert plan.action == "none" and st["link_bad"] == i
    plan, st = run(replace(o, now=MORNING + timedelta(minutes=10)), st)
    assert plan.action == "restart" and plan.kill == [11]


def test_a_link_that_comes_back_resets_the_count():
    o = obs(port_open=True, api_ok=True, link_ok=False, foreign_pids=[5])
    _, st = run(o)
    _, st = run(replace(o, link_ok=True), st)
    assert st["link_bad"] == 0


def test_a_hand_started_gateway_at_its_login_window_is_left_for_rick_without_a_stored_login():
    o = obs(foreign_pids=[52196], login_stored=False)
    for i in range(5):
        plan, st = run(replace(o, now=MORNING + timedelta(minutes=2 * i)))
        assert plan.action == "none" and plan.kill == []


def test_with_a_login_stored_it_is_replaced_by_one_ibc_logs_in():
    o = obs(foreign_pids=[52196], login_stored=True)
    plan, st = run(o)
    assert plan.action == "none"
    plan, st = run(replace(o, now=MORNING + timedelta(minutes=2)), st)
    assert plan.action == "restart" and plan.kill == [52196]


def test_a_login_in_progress_is_not_interrupted():
    for phase in ("starting", "login_dialog", "awaiting_2fa", "existing_session"):
        o = obs(launcher_alive=True, launcher_pid=11, phase=phase, mode="auto",
                phase_at=MORNING - timedelta(minutes=5), login_stored=True)  # fmt: skip
        plan, _ = run(o, {"bad": 1})
        assert plan.action == "none", phase


def test_an_automatic_login_stuck_for_20_minutes_is_started_again():
    o = obs(launcher_alive=True, launcher_pid=11, java_alive=True, java_pid=12,
            phase="login_dialog", mode="auto", login_stored=True,
            phase_at=MORNING - timedelta(minutes=21))  # fmt: skip
    plan, _ = run(o)
    assert plan.action == "restart" and plan.kill == [12, 11]
    # waiting for the phone is IBC's to time out, not a stuck login
    plan, _ = run(replace(o, phase="awaiting_2fa"))
    assert plan.action == "none"


def test_an_automatic_login_waits_for_trading_hours_so_the_phone_is_not_pinged_at_night():
    for when in (NIGHT, SATURDAY):
        plan, _ = run(obs(now=when, login_stored=True))
        assert plan.action == "none" and "06:45-21:00" in plan.why
    # without a stored login nothing pings the phone: the login window just waits for Rick
    plan, _ = run(obs(now=NIGHT, login_stored=False))
    assert plan.action == "launch"


def test_after_three_unanswered_approvals_it_waits_30_minutes():
    gave_up = {"code": 1111, "gave_up_2fa": True, "at": MORNING.isoformat()}
    o = obs(login_stored=True, last_exit=gave_up)
    plan, st = run(o)
    assert plan.action == "none" and "10:50" in plan.why
    plan, st = run(replace(o, now=MORNING + timedelta(minutes=29)), st)
    assert plan.action == "none"
    plan, st = run(replace(o, now=MORNING + timedelta(minutes=31)), st)
    assert plan.action == "launch" and "backoff_until" not in st


# --------------------------------------------------------------------------
# decide: what Rick is told, and how often
# --------------------------------------------------------------------------
def _waiting_on_phone(now):
    return obs(now=now, launcher_alive=True, launcher_pid=11, phase="awaiting_2fa",
               mode="auto", login_stored=True, phase_at=now)  # fmt: skip


def test_rick_is_told_once_and_reminded_once_after_ten_minutes():
    plan, st = run(_waiting_on_phone(MORNING))
    assert plan.notice == "phone" and not plan.reminder
    st["outage"]["notified_at"] = MORNING.isoformat()  # what run_once records once sent
    for m in (2, 4, 8):
        plan, st = run(_waiting_on_phone(MORNING + timedelta(minutes=m)), st)
        assert not plan.notice, m
    plan, st = run(_waiting_on_phone(MORNING + timedelta(minutes=10)), st)
    assert plan.notice == "phone" and plan.reminder
    st["outage"]["reminded_at"] = (MORNING + timedelta(minutes=10)).isoformat()
    plan, st = run(_waiting_on_phone(MORNING + timedelta(minutes=30)), st)
    assert not plan.notice


def test_no_message_before_the_phone_is_actually_asked():
    plan, _ = run(obs(launcher_alive=True, phase="login_dialog", mode="auto",
                      login_stored=True, phase_at=MORNING))  # fmt: skip
    assert not plan.notice


def test_without_a_stored_login_rick_is_asked_to_log_in_on_the_pc():
    plan, _ = run(obs(launcher_alive=True, phase="starting", mode="manual", phase_at=MORNING))
    assert plan.notice == "manual"
    plan, _ = run(obs(foreign_pids=[52196], login_stored=False))
    assert plan.notice == "waiting"
    assert "ibkr_login_setup.cmd" in S.message("waiting", False, MORNING)


def test_the_2345_auto_restart_needs_nothing_from_rick():
    plan, _ = run(obs(launcher_alive=True, phase="starting", mode="manual", restart=True,
                      phase_at=MORNING))  # fmt: skip
    assert not plan.notice


def test_no_messages_at_night():
    plan, _ = run(_waiting_on_phone(NIGHT))
    assert not plan.notice


def test_the_messages_are_the_ones_rick_asked_for():
    assert S.message("phone", False, MORNING) == (
        "IB Gateway restarted - approve the IBKR login on your phone")
    assert S.message("phone", True, MORNING).startswith("Still waiting: IB Gateway restarted")


# --------------------------------------------------------------------------
# run_once: the actions, with fakes
# --------------------------------------------------------------------------
class Fakes:
    def __init__(self, tmp_path, observations):
        self.observations = list(observations)
        self.sent, self.killed, self.started, self.lines, self.slept = [], [], [], [], []
        self.deps = S.Deps(
            observe=self.observe, send=self.send, kill=self.kill, start=self.start,
            log=self.lines.append, sleep=self.slept.append, is_trading_day=trading,
            state_path=tmp_path / "supervisor.json",
        )  # fmt: skip

    def observe(self, now):
        o = self.observations.pop(0) if len(self.observations) > 1 else self.observations[0]
        return replace(o, now=now)

    def send(self, text):
        self.sent.append(text)
        return True

    def kill(self, pid):
        self.killed.append(pid)
        return True

    def start(self):
        self.started.append(S.LAUNCHER_TASK)
        return True, "SUCCESS"


def test_gateway_gone_is_restarted_through_its_task_and_rick_told_once(tmp_path):
    gone = obs(login_stored=True)
    phone = obs(launcher_alive=True, launcher_pid=11, phase="awaiting_2fa", mode="auto",
                login_stored=True, phase_at=MORNING)  # fmt: skip
    f = Fakes(tmp_path, [gone, obs(launcher_alive=True, phase="starting", mode="auto",
                                   login_stored=True, phase_at=MORNING), phone])  # fmt: skip
    plan = S.run_once(MORNING, f.deps)
    assert plan.action == "launch" and f.started == [S.LAUNCHER_TASK] and f.killed == []
    assert f.sent == ["IB Gateway restarted - approve the IBKR login on your phone"]
    st = json.loads((tmp_path / "supervisor.json").read_text())
    assert st["outage"]["notified_at"] == MORNING.isoformat(timespec="seconds")
    assert "LAUNCH" in f.lines[0] and "told Rick (phone)" in f.lines[0]

    # the next checks: still waiting on the phone - no relaunch, no second message
    f.observations = [phone]
    for m in (2, 4):
        S.run_once(MORNING + timedelta(minutes=m), f.deps)
    assert f.started == [S.LAUNCHER_TASK] and len(f.sent) == 1
    # ten minutes on: one reminder
    S.run_once(MORNING + timedelta(minutes=10), f.deps)
    assert len(f.sent) == 2 and f.sent[1].startswith("Still waiting")
    # logged in: the outage closes, nothing more is sent
    f.observations = [obs(**HEALTHY, launcher_alive=True, phase="logged_in")]
    S.run_once(MORNING + timedelta(minutes=12), f.deps)
    assert len(f.sent) == 2 and "outage over" in f.lines[-1]
    assert json.loads((tmp_path / "supervisor.json").read_text())["outage"] is None


def test_an_auto_restart_that_logs_straight_in_sends_nothing(tmp_path):
    f = Fakes(tmp_path, [obs(login_stored=True), obs(**HEALTHY, launcher_alive=True,
                                                    phase="logged_in")])  # fmt: skip
    S.run_once(MORNING, f.deps)
    assert f.started and f.sent == []


def test_a_restart_stops_the_old_gateway_first(tmp_path):
    o = obs(foreign_pids=[52196], login_stored=True)
    f = Fakes(tmp_path, [o])
    S.run_once(MORNING, f.deps)
    assert f.killed == [] and f.started == []
    f.observations = [o, obs(launcher_alive=True, phase="awaiting_2fa", mode="auto",
                             login_stored=True, phase_at=MORNING)]  # fmt: skip
    S.run_once(MORNING + timedelta(minutes=2), f.deps)
    assert f.killed == [52196] and f.started == [S.LAUNCHER_TASK] and 3 in f.slept


def test_a_message_that_fails_is_tried_again_next_check(tmp_path):
    f = Fakes(tmp_path, [_waiting_on_phone(MORNING)])
    f.send = lambda text: False
    f.deps.send = f.send
    S.run_once(MORNING, f.deps)
    assert "NOT sent" in f.lines[-1]
    sent = []
    f.deps.send = lambda text: sent.append(text) or True
    S.run_once(MORNING + timedelta(minutes=2), f.deps)
    assert sent == ["IB Gateway restarted - approve the IBKR login on your phone"]


# --------------------------------------------------------------------------
# IBC: the command, the login kept out of every printed form, the restart rules
# --------------------------------------------------------------------------
@pytest.fixture
def install(tmp_path):
    gw = tmp_path / "ibgateway"
    (gw / "jars").mkdir(parents=True)
    for j in ("twslaunch.jar", "jts4launch.jar"):
        (gw / "jars" / j).write_bytes(b"PK")
    (gw / ".install4j").mkdir()
    (gw / ".install4j" / "i4jparams.conf").write_text(
        '<variable name="javaOptions" value="--add-opens=java.base/java.util=ALL-UNNAMED '
        '-DjxBrowserKey=ABC123" />', encoding="utf-8")  # fmt: skip
    (gw / "jre" / "bin").mkdir(parents=True)
    (gw / "jre" / "bin" / "java.exe").write_bytes(b"MZ")
    (gw / "ibgateway.vmoptions").write_text(
        "# comment\n-Xmx768m\n\n-XX:+UseG1GC\n-DvmOptionsPath=C:\\x y\n### keep on update\n",
        encoding="utf-8")  # fmt: skip
    ibc_dir = tmp_path / "IBC"
    ibc_dir.mkdir()
    return ibc.Paths(gateway=gw, settings=gw, ibc=ibc_dir, config=ibc_dir / "asxbot" / "c.ini")


def test_the_command_is_startibc_bats_with_the_login_as_two_arguments(install):
    cmd = ibc.command(install, FAKE, session_id="42")
    assert cmd[0].endswith("java.exe")
    assert cmd[cmd.index("-cp") + 1].endswith("IBC.jar")
    assert "-Xmx768m" in cmd and "-XX:+UseG1GC" in cmd and "-DvmOptionsPath=C:\\x" in cmd
    assert "--add-opens=java.base/java.util=ALL-UNNAMED" in cmd
    assert f"-DjtsConfigDir={install.settings}" in cmd and "-Dibcsessionid=42" in cmd
    i = cmd.index(ibc.ENTRY_POINT)
    assert cmd[i + 1:] == [str(install.config), FAKE.user, FAKE.password, "live"]
    assert not any(a.startswith("-Drestart") for a in cmd)
    # no login stored: Gateway asks Rick
    assert ibc.command(install, None)[-2:] == [str(install.config), "live"]


def test_the_auto_restart_logs_back_in_without_a_login(install):
    user_dir = install.settings / "pngciam"
    user_dir.mkdir()
    (user_dir / "autorestart").write_text("x")
    assert ibc.autorestart_user_dir(install.settings) == "pngciam"
    assert "-Drestart=pngciam" in ibc.command(install, FAKE, restart_dir="pngciam")
    # two of them: IBC's rule is to trust neither and delete both
    (install.settings / "other").mkdir()
    (install.settings / "other" / "autorestart").write_text("x")
    assert ibc.autorestart_user_dir(install.settings) is None
    assert not list(install.settings.glob("*/autorestart"))


def test_the_login_never_appears_in_anything_printed(install):
    cmd = ibc.command(install, FAKE)
    shown = ibc.redacted(cmd, FAKE)
    assert FAKE.user not in shown and FAKE.password not in shown and shown.count("***") == 2
    assert "ABC123" not in shown and "IBC.jar" not in shown  # key and classpath shortened
    line = f"IBC: arguments {FAKE.user} {FAKE.password} and again {FAKE.password}"
    assert ibc.scrub(line, FAKE) == "IBC: arguments *** *** and again ***"
    assert repr(FAKE) == str(FAKE) == "Login(user=***, password=***)"


def test_the_launchers_restart_rules():
    assert ibc.after_exit(1111, None, 1, 3)[0] == ibc.RESTART
    assert ibc.after_exit(1111, None, 3, 3)[0] == ibc.STOP
    assert ibc.after_exit(1112, None, 0, 3)[0] == ibc.RESTART
    assert ibc.after_exit(0, "pngciam", 0, 3)[0] == ibc.RESTART
    assert ibc.after_exit(0, None, 0, 3)[0] == ibc.STOP
    assert ibc.after_exit(1, None, 0, 3)[0] == ibc.STOP


@pytest.mark.parametrize("line, phase", [
    ("2026-09-25 10:20:01:123 IBC: Second Factor Authentication initiated", "awaiting_2fa"),
    ("IBC: Login frame has now become SecondFactorAuthenticationDialog", "awaiting_2fa"),
    ("IBC: Login has completed", "logged_in"),
    ("IBC: Configuration tasks completed", "logged_in"),
    ("IBC: Login dialog WINDOW_OPENED: LoginState is LOGGED_OUT", "login_dialog"),
    ("IBC: detected dialog entitled: Existing session detected; event=Opened", "existing_session"),
    ("IBC: something else", None),
])  # fmt: skip
def test_ibcs_log_lines_say_how_far_the_login_has_got(line, phase):
    assert ibc.phase_of(line) == phase


def test_the_ibc_settings_are_the_ones_rick_asked_for():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "scripts" / "ibc" / "config.ini").read_text()
    kv = dict(ln.split("=", 1) for ln in text.splitlines() if "=" in ln and not ln.startswith("#"))
    assert kv["IbLoginId"] == "" and kv["IbPassword"] == ""  # never a login in a file
    assert kv["TradingMode"] == "live"
    assert kv["ExistingSessionDetectedAction"] == "primary"
    assert kv["ReloginAfterSecondFactorAuthenticationTimeout"] == "yes"
    assert kv["ReadOnlyApi"] == "yes"
    assert kv["AcceptIncomingConnectionAction"] == "reject"
    assert kv["AllowBlindTrading"] == "no"
    assert kv["AutoRestartTime"] == "11:45 PM"
    assert kv["IbAutoClosedown"] == "no"
    assert kv["OverrideTwsApiPort"] == "4001"
    assert kv["CommandServerPort"] == "0"


# --------------------------------------------------------------------------
# the stored login: Windows Credential Manager, under a throwaway name
# --------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="Credential Manager is Windows only")
def test_the_login_round_trips_through_credential_manager():
    target = "asxbot/test-only-not-a-login"
    try:
        assert credentials.load(target) is None
        credentials.store(FAKE.user, FAKE.password, target)
        back = credentials.load(target)
        assert back == FAKE and credentials.stored(target)
        credentials.store("someone-else", "other", target)  # replaced, not added
        assert credentials.load(target).user == "someone-else"
    finally:
        credentials.forget(target)
    assert credentials.load(target) is None and credentials.forget(target) is False


def test_an_empty_login_is_refused():
    with pytest.raises(credentials.CredentialError):
        credentials.store("", "x", "asxbot/test-only-not-a-login")
    with pytest.raises(credentials.CredentialError):
        credentials.store("u", "", "asxbot/test-only-not-a-login")


# --------------------------------------------------------------------------
# the 07:20 pre-flight
# --------------------------------------------------------------------------
def test_the_preflight_says_nothing_when_the_check_passes():
    sent = []
    r = S.preflight(None, MORNING, sent.append,
                    run_check=lambda cfg, out, now: out("PASS: BHP real-time") or 0,
                    is_trading_day=trading)  # fmt: skip
    assert r.startswith("passed") and sent == []


def test_the_preflight_tells_rick_when_the_check_fails():
    sent = []
    r = S.preflight(None, MORNING, lambda t: sent.append(t) or True,
                    run_check=lambda cfg, out, now: out("FAIL: IB Gateway is not accepting "
                                                        "connections") or 1,
                    is_trading_day=trading)  # fmt: skip
    assert "Rick told" in r and len(sent) == 1
    assert sent[0].startswith("IB Gateway pre-flight FAILED") and "not accepting" in sent[0]


def test_the_preflight_does_nothing_when_the_asx_is_shut():
    called = []
    r = S.preflight(None, SATURDAY, called.append, run_check=lambda *a, **k: called.append(1),
                    is_trading_day=trading)  # fmt: skip
    assert r == "not a trading day" and called == []
