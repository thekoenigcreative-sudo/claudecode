"""The Trader's own Telegram chat (asxbot.chat): a fake Telegram, a fake decider, a fake
OpenClaw config and a throwaway git repo. No network, no model, no real config.yaml edit."""

import copy
import json
import logging
import os
import shutil
import threading
import time
from argparse import Namespace
from datetime import date, datetime
from pathlib import Path

import pytest
import requests
import yaml

from asxbot import botctl, chat, proc
from asxbot import log as L
from asxbot import telegram as T
from asxbot.arena import settings_history as H
from asxbot.config import load_config
from test_v2_daytrader_flow import arena, mb  # noqa: F401 - fixtures for the report test

REPO = Path(__file__).resolve().parents[1]
RICK = botctl.RICK_ID
SYD = chat.SYD

OC = {
    "meta": {"lastTouchedAt": "x"},
    "agents": {
        "defaults": {"model": {"primary": "anthropic/claude-sonnet-5"},
                     "models": {"anthropic/claude-sonnet-5": {}, "anthropic/claude-opus-5-5": {},
                                "anthropic/claude-opus-4-8": {}}},
        "list": [{"id": "main"},
                 {"id": "trader-reader", "model": "anthropic/claude-sonnet-5",
                  "thinkingDefault": "medium"},
                 {"id": "trader-decider", "model": "anthropic/claude-opus-5-5",
                  "thinkingDefault": "high"}],
    },
    "messages": {"queue": {"mode": "followup", "cap": 20}},
    "channels": {"telegram": {"accounts": {"default": {"botToken": "999:JARVIS"}}}},
}  # fmt: skip
BASE = {"is_change": True, "runtime_hint": None, "summary": "Show cash before positions.",
        "question": None, "limits": [], "off_limits": None}  # fmt: skip


class FakeRun:
    """`openclaw config set/unset/validate` against the fake openclaw.json; anything else ok."""

    def __init__(self, oc_path: Path):
        self.calls, self.oc_path = [], oc_path

    def __call__(self, cmd, timeout):
        self.calls.append(cmd)
        if len(cmd) > 2 and cmd[1] == "config" and cmd[2] in ("set", "unset"):
            cfg = json.loads(self.oc_path.read_text())
            idx = int(cmd[3].split("[")[1].split("]")[0])
            fld = cmd[3].rsplit(".", 1)[1]
            if cmd[2] == "set":
                cfg["agents"]["list"][idx][fld] = cmd[4]
            else:
                cfg["agents"]["list"][idx].pop(fld, None)
            self.oc_path.write_text(json.dumps(cfg))
            return 0, "Updated", ""
        if len(cmd) > 2 and cmd[1] == "config" and cmd[2] == "validate":
            return 0, "Config valid", ""
        return 0, "{}", ""

    def sets(self):
        return [c for c in self.calls if c[1:3] == ["config", "set"]]


class FakeTg:
    def __init__(self):
        self.sent, self.answered, self.typing_n = [], [], 0

    def send(self, chat_id, text, buttons=None):
        assert chat_id == RICK
        self.sent.append((text, buttons))

    def typing(self, chat_id):
        self.typing_n += 1

    def answer_callback(self, callback_id, text=None):
        self.answered.append(callback_id)

    def texts(self):
        return [t for t, _ in self.sent]


class FakeDecider:
    """Stands in for `openclaw agent`. Blocks while `gate` is clear."""

    def __init__(self):
        self.calls = []
        self.gate = threading.Event()
        self.gate.set()
        self.started = threading.Event()

    def __call__(self, message, key):
        self.calls.append((message, key))
        self.started.set()
        assert self.gate.wait(10)
        return f"answer {len(self.calls)}"

    def asked(self):
        return [m.split("Rick's message:\n", 1)[1] for m, _ in self.calls]


def git(repo, *args):
    p = proc.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return p.stdout


@pytest.fixture
def repo(tmp_path):
    """A throwaway git repo holding a byte-for-byte copy of config.yaml."""
    r = tmp_path / "repo"
    r.mkdir()
    shutil.copyfile(REPO / "config.yaml", r / "config.yaml")
    git(r, "init", "-q")
    git(r, "config", "user.email", "test@example.com")
    git(r, "config", "user.name", "test")
    git(r, "config", "core.autocrlf", "false")
    git(r, "add", "config.yaml")
    git(r, "commit", "-q", "-m", "start")
    return r


@pytest.fixture
def data_cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )


@pytest.fixture
def env(tmp_path, monkeypatch, repo, data_cfg):
    oc = tmp_path / "openclaw.json"
    oc.write_text(json.dumps(OC))
    monkeypatch.setattr(botctl, "CHANGES_DIR", tmp_path / "changes")
    monkeypatch.setattr(botctl, "OPENCLAW_JSON", oc)
    run = FakeRun(oc)
    monkeypatch.setattr(botctl, "RUN", run)
    aborted = []
    monkeypatch.setattr(botctl, "abort_turn", aborted.append)
    tg, decider = FakeTg(), FakeDecider()
    app = chat.TraderChat(tg, RICK, repo, tmp_path / "home", agent_runner=decider,
                          cfg_loader=lambda: data_cfg)  # fmt: skip
    app.ctl.handle("/queue followup debounce:0")
    tg.sent.clear()
    yield type("E", (), {"app": app, "tg": tg, "decider": decider, "run": run, "oc": oc,
                         "repo": repo, "aborted": aborted, "tmp": tmp_path})  # fmt: skip
    decider.gate.set()
    chat.wait_idle(app, 10)


def msg(text, who=RICK, chat_id=None, when=None):
    return {"update_id": 1, "message": {
        "message_id": 1, "text": text, "from": {"id": int(who), "username": "x"},
        "chat": {"id": int(chat_id or who)},
        "date": int((when or datetime.now(SYD)).timestamp())}}  # fmt: skip


def tap(data, who=RICK):
    return {"update_id": 2, "callback_query": {
        "id": "cb1", "from": {"id": int(who)}, "data": data,
        "message": {"chat": {"id": int(who)}}}}  # fmt: skip


# ------------------------------------------------------------------ dispatch


def test_commands_never_reach_the_decider(env):
    for text in ("/help", "/start", "/status", "/positions", "/model", "/queue", "/whoami",
                 "/verbose on", "/frobnicate", "/"):  # fmt: skip
        assert env.app.on_update(msg(text)) == "handled", text
    chat.wait_idle(env.app, 10)
    assert env.decider.calls == []
    texts = env.tg.texts()
    assert texts[0].startswith("The Trader chat: talk to the decider") and "/change" in texts[0]
    assert texts[1] == texts[0]  # /start is /help
    assert texts[2].splitlines()[1].startswith("Watcher: ")
    assert "Open fake-money positions: 0" in texts[2] and "Orders today: 0 placed" in texts[2]
    assert texts[3].startswith("Arena positions (FAKE money):")
    assert "no positions, no pending orders" in texts[3]
    assert "decider (decides the trades, and answers you here): Opus 5.5" in texts[4]
    assert "doesn't apply" in texts[7]
    assert texts[8] == texts[9] == "I don't know that command. /help lists them."


def test_only_rick_is_answered(env):
    assert env.app.on_update(msg("hello", who="12345")) == "ignored: not Rick"
    assert env.app.on_update(msg("hello", who=RICK, chat_id="-100777")) == "ignored: not Rick"
    assert env.app.on_update(tap("chg:c:x", who="12345")) == "ignored: not Rick"
    chat.wait_idle(env.app, 5)
    assert env.tg.sent == [] and env.tg.answered == [] and env.decider.calls == []


def test_plain_text_is_a_turn_with_the_decider_in_its_own_session(env):
    env.app.on_update(msg("why did you pass on BHP?"))
    chat.wait_idle(env.app, 10)
    (message, key), = env.decider.calls
    assert key == "agent:trader-decider:rick-chat-1"
    assert "cannot place, change, close or approve any order" in message
    assert message.endswith("Rick's message:\nwhy did you pass on BHP?")
    assert env.tg.texts() == ["answer 1"]
    env.app.on_update(msg("/new"))
    env.app.on_update(msg("and now?"))
    chat.wait_idle(env.app, 10)
    assert env.decider.calls[-1][1] == "agent:trader-decider:rick-chat-2"
    assert any("sessions.reset" in c for c in env.run.calls)


def test_a_message_sent_while_the_chat_was_down_is_not_answered(env):
    old = datetime.now(SYD).replace(year=2025)
    assert env.app.on_update(msg("anyone there?", when=old)).startswith("stale")
    assert env.decider.calls == [] and env.tg.sent == []


# ------------------------------------------------------------------ change requests


def test_a_change_request_is_offered_with_buttons_and_only_ricks_tap_queues_it(env, monkeypatch):
    seen = []

    def interpret(ad, words, answer=None):
        seen.append(words)
        return dict(BASE)

    monkeypatch.setattr(botctl, "interpret", interpret)
    env.app.on_update(msg("can you make it show the cash first"))
    chat.wait_idle(env.app, 10)
    assert seen == ["can you make it show the cash first"] and env.decider.calls == []
    text, buttons = env.tg.sent[-1]
    assert "Show cash before positions." in text
    assert [b[0] for b in buttons] == ["Build it", "Cancel"]
    assert chat.keyboard(buttons) == {"inline_keyboard": [[
        {"text": "Build it", "callback_data": buttons[0][1]},
        {"text": "Cancel", "callback_data": buttons[1][1]}]]}  # fmt: skip
    rid = buttons[0][1].split(":", 2)[2]
    req = env.tmp / "changes" / "requests" / f"{rid}.json"
    assert json.loads(req.read_text(encoding="utf-8"))["status"] == "proposed"

    env.app.on_update(tap(buttons[0][1], who="12345"))  # not Rick: nothing happens
    assert json.loads(req.read_text(encoding="utf-8"))["status"] == "proposed"
    env.app.on_update(tap(buttons[0][1]))
    rec = json.loads(req.read_text(encoding="utf-8"))
    assert rec["status"] == "queued" and rec["repo"] == str(env.repo) and rec["bot"] == "trader"
    assert env.tg.answered == ["cb1"] and env.tg.texts()[-1].startswith("Queued")


def test_a_real_money_request_is_refused_in_code(env, monkeypatch):
    monkeypatch.setattr(botctl, "interpret", lambda *a, **k: pytest.fail("reader asked"))
    env.app.on_update(msg("/change let the decider place real money orders"))
    assert "can't take that one" in env.tg.texts()[-1]


# ------------------------------------------------------------------ /stop and /queue


def _wait(cond, s=5.0):
    end = time.time() + s
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_stop_drops_the_running_answer(env):
    env.decider.gate.clear()
    env.app.on_update(msg("tell me a long story"))
    assert env.decider.started.wait(5) and _wait(lambda: botctl.TURNS.running())
    env.app.on_update(msg("/stop"))
    assert env.tg.texts()[-1].startswith("Stopped: the decider's answer to you")
    assert _wait(lambda: env.aborted)
    assert env.aborted[0].full_key() == "agent:trader-decider:rick-chat-1"
    env.decider.gate.set()  # the killed call returns late: its answer must be dropped
    chat.wait_idle(env.app, 10)
    assert "answer 1" not in env.tg.texts()
    env.app.on_update(msg("still there?"))
    chat.wait_idle(env.app, 10)
    assert env.tg.texts()[-1] == "answer 2"


def test_queue_followup_answers_each_in_turn(env):
    env.decider.gate.clear()
    for t in ("one", "two", "three"):
        env.app.on_update(msg(t))
    assert env.decider.started.wait(5)
    assert env.app.ctl.conveyor.waiting() == 2
    env.decider.gate.set()
    chat.wait_idle(env.app, 10)
    assert env.decider.asked() == ["one", "two", "three"]
    assert env.tg.texts() == ["answer 1", "answer 2", "answer 3"]


def test_queue_collect_sends_the_waiting_messages_as_one(env):
    env.app.on_update(msg("/queue collect debounce:0"))
    env.decider.gate.clear()
    for t in ("one", "two", "three"):
        env.app.on_update(msg(t))
    assert env.decider.started.wait(5)
    env.decider.gate.set()
    chat.wait_idle(env.app, 10)
    assert env.decider.asked() == [
        "one", "Messages that came in while I was busy:\n1. two\n2. three"]  # fmt: skip


# ------------------------------------------------------------------ strategy changes


def _agents(repo):
    return yaml.safe_load((repo / "config.yaml").read_text(encoding="utf-8"))["arena"]["agents"]


def test_model_change_is_recorded_in_config_yaml_and_committed(env):
    before = (env.repo / "config.yaml").read_text(encoding="utf-8")
    env.app.on_update(msg("/model decider sonnet-5"))
    reply = env.tg.texts()[-1]
    assert "set to Sonnet 5" in reply and "was Opus 5.5" in reply
    assert "Recorded as a dated strategy change in config.yaml (commit " in reply
    oc = json.loads(env.oc.read_text())
    assert oc["agents"]["list"][2]["model"] == "anthropic/claude-sonnet-5"

    ag = _agents(env.repo)
    assert ag["models"] == {"reader": "anthropic/claude-sonnet-5",
                            "decider": "anthropic/claude-sonnet-5"}  # fmt: skip
    (e,) = ag["history"]
    assert (e["role"], e["setting"], e["old"], e["new"]) == (
        "decider", "model", "anthropic/claude-opus-5-5", "anthropic/claude-sonnet-5")
    assert e["by"] == "Rick, /model in the Trader chat"
    assert e["note"] == "deliberate change by Rick, not a finding"
    assert e["date"] == datetime.now(SYD).date().isoformat()

    after = (env.repo / "config.yaml").read_text(encoding="utf-8")
    removed = [x for x in before.splitlines() if x not in after.splitlines()]
    assert removed == ["      decider: anthropic/claude-opus-5-5", "    history: []"]
    assert sum(x.lstrip().startswith("#") for x in after.splitlines()) == sum(
        x.lstrip().startswith("#") for x in before.splitlines())  # every comment survives
    assert git(env.repo, "status", "--porcelain") == ""
    log1 = git(env.repo, "log", "-1", "--format=%B")
    assert log1.startswith("Strategy change by Rick: decider model anthropic/claude-opus-5-5 ")
    assert "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" in log1
    assert git(env.repo, "show", "--name-only", "--format=", "HEAD").split() == ["config.yaml"]

    # /think is one too, and the pins follow config.yaml: 'default' now means Sonnet 5
    env.app.on_update(msg("/think decider max"))
    assert "thinking level set to max" in env.tg.texts()[-1]
    ag = _agents(env.repo)
    assert ag["effort"]["decider"] == "max" and len(ag["history"]) == 2
    assert ag["history"][1]["setting"] == "effort" and ag["history"][1]["old"] == "high"
    env.app.on_update(msg("/model decider default"))
    assert env.tg.texts()[-1] == "the Trader's decider already uses Sonnet 5."


def test_model_change_waits_for_uncommitted_config_edits_or_a_building_change(env):
    p = env.repo / "config.yaml"
    p.write_text(p.read_text(encoding="utf-8") + "# someone's edit\n", encoding="utf-8")
    env.app.on_update(msg("/model decider sonnet-5"))
    assert "config.yaml has edits that aren't committed yet" in env.tg.texts()[-1]
    assert env.run.sets() == []
    git(env.repo, "checkout", "--", "config.yaml")

    rec = {"id": "trader-20260924-221500", "bot": "trader", "repo": str(env.repo),
           "status": "building", "history": [], "created": "2026-09-24T22:15:00"}  # fmt: skip
    botctl.save_req(rec)
    env.app.on_update(msg("/think reader high"))
    assert "while change #0924-2215 is being built" in env.tg.texts()[-1]
    assert env.run.sets() == [] and _agents(env.repo)["history"] == []


def test_a_failed_recording_puts_the_openclaw_setting_back(env, monkeypatch):
    def boom(*a, **k):
        raise H.RecordFailed("git commit failed: disk full")

    monkeypatch.setattr(H, "record", boom)
    env.app.on_update(msg("/model decider sonnet-5"))
    assert "Not changed: recording it failed (git commit failed: disk full)" in env.tg.texts()[-1]
    oc = json.loads(env.oc.read_text())
    assert oc["agents"]["list"][2]["model"] == "anthropic/claude-opus-5-5"


def test_the_text_edit_refuses_anything_unsafe(repo):
    text = (repo / "config.yaml").read_text(encoding="utf-8")
    entry = {"date": "2026-09-24", "time": "22:15", "role": "decider"}
    with pytest.raises(H.RecordFailed):
        H.edit_text(text, "decider", "model", "evil: {x}", entry)
    with pytest.raises(H.RecordFailed):
        H.check_edit(text, text.replace("max_orders_per_day: 40", "max_orders_per_day: 400"),
                     "decider", "model", "x", entry)  # fmt: skip


# ------------------------------------------------------------------ the evening report


@pytest.fixture
def cfg(config_file, tmp_path, base_config):
    """The report test's config: 25 Sep, with one change Rick made that evening."""
    raw = copy.deepcopy(base_config["arena"])
    raw["agents"]["history"] = [
        {"date": "2026-09-24", "time": "18:00", "role": "reader", "setting": "effort",
         "old": "low", "new": "medium", "by": "Rick, /think in the Trader chat"},
        {"date": "2026-09-25", "time": "22:15", "role": "decider", "setting": "model",
         "old": "anthropic/claude-opus-5-5", "new": "anthropic/claude-sonnet-5",
         "by": "Rick, /model in the Trader chat", "note": "deliberate change by Rick"},
    ]  # fmt: skip
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}, arena=raw),
        env_file=tmp_path / "none.env",
    )


def test_the_evening_report_names_settings_changed_since_the_last_one(arena, cfg):  # noqa: F811
    from asxbot.arena.report import agent_brief, gather, mark_report_sent, render_plain

    facts = gather(arena, date(2026, 9, 25))
    text = render_plain(facts)
    line = "Settings changed: decider model Opus 5.5 -&gt; Sonnet 5 (Rick, 10:15pm)"
    assert line in text  # (HTML-escaped in the plain report, which is sent as HTML)
    assert "reader thinking" not in text  # the day before, and no report on record
    brief = agent_brief(facts)
    assert "You MUST mention every one" in brief
    assert "decider model Opus 5.5 -> Sonnet 5 (Rick, 10:15pm)" in brief

    # the previous report went out on the 24th at 19:30: the reader change at 18:00 was in
    # it, and the next report (25th) lists only what came after
    mark_report_sent(cfg, datetime(2026, 9, 24, 17, 0, tzinfo=SYD))
    lines = [c["line"] for c in gather(arena, date(2026, 9, 25))["settings_changed"]]
    assert lines == ["reader thinking low -> medium (Rick, Thu 6:00pm)",
                     "decider model Opus 5.5 -> Sonnet 5 (Rick, 10:15pm)"]  # fmt: skip
    mark_report_sent(cfg, datetime(2026, 9, 24, 19, 30, tzinfo=SYD))
    lines = [c["line"] for c in gather(arena, date(2026, 9, 25))["settings_changed"]]
    assert lines == ["decider model Opus 5.5 -> Sonnet 5 (Rick, 10:15pm)"]


# ------------------------------------------------------------------ Telegram and start-up


class _Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


def test_telegram_errors_are_classified_and_never_show_the_token():
    token = "123456:SECRETSECRETSECRET"

    def conflict(url, json=None, timeout=None):
        return _Resp({"ok": False, "error_code": 409, "description": "Conflict: terminated "
                      "by other getUpdates request"}, 409)  # fmt: skip

    with pytest.raises(chat.TgConflict):
        chat.Telegram(token, post=conflict).get_updates(None)

    def down(url, json=None, timeout=None):
        raise requests.ConnectionError(f"Max retries exceeded with url: /bot{token}/getUpdates")

    with pytest.raises(chat.TgError) as e:
        chat.Telegram(token, post=down).get_updates(None)
    assert token not in str(e.value) and "...CRET" in str(e.value)

    def bad(url, json=None, timeout=None):
        return _Resp({"ok": False, "error_code": 401, "description": "Unauthorized"}, 401)

    with pytest.raises(chat.TgUnauthorized):
        chat.Telegram(token, post=bad).get_updates(None)


def test_sending_splits_long_text_and_puts_the_buttons_on_the_last_part():
    posts = []

    def ok(url, json=None, timeout=None):
        posts.append((url.rsplit("/", 1)[1], json))
        return _Resp({"ok": True, "result": {"message_id": len(posts)}})

    tg = chat.Telegram("123456:SECRET", post=ok)
    tg.send(RICK, ("line\n" * 1500), [("Build it", "chg:b:x"), ("Cancel", "chg:c:x"),
                                      ("Third", "chg:x:none")])  # fmt: skip
    assert len(posts) == 2 and all(len(p[1]["text"]) <= 4096 for p in posts)
    assert "parse_mode" not in posts[0][1] and "reply_markup" not in posts[0][1]
    assert [len(r) for r in posts[1][1]["reply_markup"]["inline_keyboard"]] == [2, 1]
    tg.get_updates(41)
    assert posts[-1] == ("getUpdates", {"timeout": 20, "offset": 41,
                                        "allowed_updates": ["message", "callback_query"]})


def test_a_409_stops_polling_with_its_own_exit_code(env):
    class Conflicted(FakeTg):
        def get_updates(self, offset, timeout=20):
            raise chat.TgConflict("terminated by other getUpdates request")

    env.app.tg = Conflicted()
    assert chat.poll(env.app, threading.Event()) == chat.EXIT_CONFLICT


def test_network_errors_back_off_and_keep_polling(env, monkeypatch):
    monkeypatch.setattr(chat, "BACKOFF_START_S", 0.05)
    stop = threading.Event()
    seen = []

    class Flaky(FakeTg):
        def get_updates(self, offset, timeout=20):
            seen.append(offset)
            if len(seen) == 1:
                raise chat.TgError("network error")
            if len(seen) == 2:
                return [dict(msg("/whoami"), update_id=77)]
            stop.set()
            return []

    env.app.tg = Flaky()
    t = threading.Thread(target=chat.poll, args=(env.app, stop))
    t.start()
    t.join(20)
    assert not t.is_alive() and seen == [None, None, 78]
    assert env.app.state()["offset"] == 78 and "Your Telegram id" in env.app.tg.texts()[-1]


@pytest.fixture
def quiet_main(tmp_path, monkeypatch, caplog, data_cfg):
    """chat.main with a fake token, a test config and a temporary home (never the real
    .env), its log lines captured."""
    import asxbot.config as C

    monkeypatch.setattr(C, "load_config", lambda *a, **k: data_cfg)
    monkeypatch.setattr(L, "_configured", True)  # main() must not reconfigure test logging
    monkeypatch.setattr(T, "load_bot", lambda cfg: T.Bot("123456:SECRETTOKEN", RICK))
    monkeypatch.setenv("ASXBOT_CHAT_HOME", str(tmp_path / "home"))
    lg = logging.getLogger("asxbot.chat")
    lg.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="asxbot.chat")
    yield caplog
    lg.removeHandler(caplog.handler)


def test_refuses_to_start_while_openclaw_still_polls_the_bot(tmp_path, monkeypatch, quiet_main):
    oc = tmp_path / "openclaw.json"
    polled = copy.deepcopy(OC)
    polled["channels"]["telegram"]["accounts"]["trader"] = {"botToken": "123456:SECRETTOKEN"}
    oc.write_text(json.dumps(polled), encoding="utf-8-sig")
    monkeypatch.setattr(botctl, "OPENCLAW_JSON", oc)
    args = Namespace(probe=None, button=None, no_agent=False)
    assert chat.main(args) == chat.EXIT_OPENCLAW_POLLS
    text = quiet_main.text
    assert "starting on commit" in text and "REFUSING TO START" in text
    assert "channels.telegram.accounts.trader" in text and "Running." not in text
    assert "SECRETTOKEN" not in text

    renamed = copy.deepcopy(OC)
    renamed["channels"]["telegram"]["accounts"]["other"] = {"botToken": "123456:SECRETTOKEN"}
    oc.write_text(json.dumps(renamed), encoding="utf-8")
    assert "accounts.other uses this bot's token" in chat.openclaw_still_polls(
        "123456:SECRETTOKEN", oc)
    oc.write_text(json.dumps(OC), encoding="utf-8")
    assert chat.openclaw_still_polls("123456:SECRETTOKEN", oc) is None


def test_one_chat_per_pc(tmp_path):
    a, b = chat.SingleInstance(tmp_path / "chat.lock"), chat.SingleInstance(tmp_path / "chat.lock")
    assert a.acquire()
    assert not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


def test_probe_runs_the_pipeline_and_prints_instead_of_sending(monkeypatch, quiet_main,
                                                               capsys, tmp_path):  # fmt: skip
    oc = tmp_path / "openclaw.json"
    oc.write_text(json.dumps(OC))
    monkeypatch.setattr(botctl, "OPENCLAW_JSON", oc)
    args = Namespace(probe="how was today?", button=None, no_agent=True)
    assert chat.main(args) == chat.EXIT_OK
    out = capsys.readouterr().out
    assert "--- to Rick (chat 8998104023) ---" in out
    assert "--no-agent: the decider was not called" in out
    assert "Rick's message:\nhow was today?" in out


def test_the_vendored_botctl_is_the_shared_module():
    src = (REPO / "src" / "asxbot" / "botctl.py").read_text(encoding="utf-8")
    assert src.startswith('"""botctl: change requests and OpenClaw\'s chat commands')
    assert "Master copy: C:\\\\Users\\\\Richa\\\\.cc-jobs\\\\changes\\\\botctl.py" in src
    assert botctl.BOTCTL_VERSION[:4] == "2026"


# ------------------------------------------------------------------ the launcher

STANDIN = """
import os, sys
n_file = os.environ["STANDIN_N"]
n = int(open(n_file).read()) if os.path.exists(n_file) else 0
open(n_file, "w").write(str(n + 1))
codes = [int(c) for c in os.environ["STANDIN_CODES"].split(",")]
print("INFO asxbot.chat: Trader chat starting on commit abc1234 (master)")
print("INFO asxbot.chat: Running. Polling, parent " + os.environ["ASXBOT_CHAT_PARENT"])
sys.exit(codes[min(n, len(codes) - 1)])
"""


def _launcher(tmp_path, monkeypatch, codes):
    import importlib.machinery
    import importlib.util
    import sys

    standin = tmp_path / "standin.py"
    standin.write_text(STANDIN, encoding="utf-8")
    monkeypatch.setenv("STANDIN_N", str(tmp_path / "n.txt"))
    monkeypatch.setenv("STANDIN_CODES", codes)
    monkeypatch.chdir(tmp_path)  # main() changes folder; put it back afterwards
    path = REPO / "scripts" / "chat.pyw"
    loader = importlib.machinery.SourceFileLoader("chat_launcher_under_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    mod.ASXBOT = [sys.executable, str(standin)]
    mod.RESTART_WAIT_S = 0
    return mod


def test_the_launcher_restarts_a_crash_and_stops_on_a_code_a_restart_cannot_fix(
    tmp_path, monkeypatch, local_logs
):
    from asxbot.log import logs_dir

    mod = _launcher(tmp_path, monkeypatch, "1,12,10")
    assert mod.LOG == logs_dir() / "chat.log" and REPO not in mod.LOG.parents
    assert mod.main() == 10
    text = mod.LOG.read_text(encoding="utf-8")
    assert text.count("=== chat launcher starting") == 1
    assert text.count("starting on commit abc1234") == 3 and text.count("Running.") == 3
    assert f"Polling, parent {os.getpid()}" in text  # the chat is told who to outlive
    assert "restarting in 0s (1 of 5 this hour)" in text
    assert "restarting in 0s (2 of 5 this hour)" in text
    assert "not restarting: OpenClaw still polls this bot" in text


def test_the_launcher_gives_up_after_five_restarts_in_an_hour(tmp_path, monkeypatch):
    mod = _launcher(tmp_path, monkeypatch, "1")
    assert mod.main() == 1
    text = mod.LOG.read_text(encoding="utf-8")
    assert text.count("=== chat exited 1") == 6 and "gave up: 5 restarts" in text
