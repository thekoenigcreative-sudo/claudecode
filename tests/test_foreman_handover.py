"""ONE VOICE in the Trader chat (asxbot.foreman).

Rick, 25 Sep 2026 23:50: "everytime i ask it something it says it can't do shit". That night
the decider answered his build and usage messages with "I can't set that up from here" while
the Foreman read the same messages and acted on them. Now a Foreman topic is handed to the
Foreman's inbox in code and the Trader says nothing; anything else the decider can't do it
hands over too, with at most one short line. Trading messages are answered as before. Same
fakes as test_chat.py; the Foreman's folder is a temporary one (conftest.foreman_home)."""

import json
import logging
import re
from datetime import datetime, timedelta

import pytest

from asxbot import chat, plain
from asxbot import foreman as F
from test_chat import RICK, SYD, data_cfg, env, msg, quiet_main, repo  # noqa: F401 - fixtures

# Rick's messages in the Trader chat, 25 Sep 2026 23:20-23:46 (chat.log), word for word.
NIGHT = [
    ("2026-09-25 23:20:05", "No keep building I have a full reset"),
    ("2026-09-25 23:29:39", "I have a limit reset to use when it runs out"),
    ("2026-09-25 23:31:13", "I’ll use the reset at 99 percent usage"),
    ("2026-09-25 23:32:14", "Yes tell me at 99"),
    ("2026-09-25 23:33:15", "What no keep everything going I won’t allow it to stop"),
    ("2026-09-25 23:45:44", "Prioritise the simulator ahead of fetch"),
]
# The decider's answer that night to "Yes tell me at 99" (chat.log, 23:32), in full as far as
# the log has it.
SAID_THEN = ("I can't set that up from here. I have no way to schedule messages or watch the "
             "usage meter, so no alert will come from me.")  # fmt: skip
CANT = re.compile(r"\b(can't|can’t|cannot|unable|no way to)\b", re.I)


def _at(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SYD)


def _beat(at: datetime | None = None, state: str = "running") -> None:
    F.home().mkdir(parents=True, exist_ok=True)
    (F.home() / "heartbeat.json").write_text(json.dumps(
        {"at": (at or datetime.now(SYD)).isoformat(timespec="seconds"), "pid": 1,
         "state": state}), encoding="utf-8")  # fmt: skip


def _inbox() -> list[dict]:
    """The handovers in the order Rick sent them (files written in the same second sort by
    their random suffix)."""
    box = F.inbox()
    if not box.exists():
        return []
    assert not list(box.glob("*.tmp")), "a half-written handover"
    got = [json.loads(p.read_text(encoding="utf-8")) for p in box.glob("*.json")]
    return sorted(got, key=lambda g: g["at"])


def _say(chat_env, text: str, at: datetime) -> None:
    assert chat_env.app.on_update(msg(text, when=at), now=at + timedelta(seconds=2)) == "handled"


@pytest.fixture
def alive(env):  # noqa: F811 - the imported fixture, by name
    _beat()
    return env


# ------------------------------------------------------------------ Rick's night, replayed


def test_ricks_night_is_handed_to_the_foreman_and_the_trader_says_nothing(alive):
    for stamp, text in NIGHT:
        _say(alive, text, _at(stamp))
    chat.wait_idle(alive.app, 10)
    assert alive.decider.calls == []  # no "I can't" can come from a decider never asked
    assert alive.tg.sent == []  # the Foreman answers these itself
    got = _inbox()
    assert [g["text"] for g in got] == [t for _, t in NIGHT]
    for g, (stamp, _) in zip(got, NIGHT, strict=True):
        assert g == {"bot": "trader", "from": "Rick", "text": g["text"],
                     "at": _at(stamp).isoformat(), "why": "foreman topic"}  # fmt: skip
    assert got[0]["at"] == "2026-09-25T23:20:05+10:00"


def test_each_message_of_the_night_on_its_own_is_handed_over_never_answered_with_cant(alive):
    """Without the conversation around it "Yes tell me at 99" is not a Foreman topic by its
    words, so the decider gets it. Given that night's answer, it is handed over anyway."""
    for stamp, text in NIGHT:
        app = chat.TraderChat(alive.tg, RICK, alive.repo, alive.tmp / f"home-{stamp[-2:]}",
                              agent_runner=lambda message, key: SAID_THEN,
                              cfg_loader=alive.app.cfg_loader)  # fmt: skip
        app.ctl.handle("/queue followup debounce:0")
        alive.tg.sent.clear()
        at = _at(stamp)
        assert app.on_update(msg(text, when=at), now=at + timedelta(seconds=2)) == "handled"
        chat.wait_idle(app, 10)
        assert _inbox()[-1]["text"] == text and _inbox()[-1]["at"] == at.isoformat()
        assert not any(CANT.search(t) for t in alive.tg.texts()), (text, alive.tg.texts())
        assert alive.tg.texts() in ([], [F.ON_IT])
    got = _inbox()
    assert len(got) == len(NIGHT)
    tell = next(g for g in got if g["text"] == "Yes tell me at 99")
    assert tell["why"] == "can't do: the decider said \"I can't set that up from here.\""
    assert [g["why"] for g in got].count("foreman topic") == len(NIGHT) - 1


def test_the_night_was_answered_with_cant_and_would_now_be_handed_over():
    """The decider's own words that night, through the reply reader."""
    for said in (SAID_THEN,
                 "Nothing is being stopped, and I haven't paused anything. I can't pause "
                 "anything from here anyway.",
                 "I can't set that from here. I don't control what runs or in what order, so "
                 "right now nothing has changed."):  # fmt: skip
        out = F.read_reply(said)
        assert out.handover and out.handover.startswith("can't do: ")
        assert not CANT.search(out.text) and out.text.endswith(F.ON_IT)
    out = F.read_reply("Nothing is being stopped, and I haven't paused anything. I can't pause "
                       "anything from here anyway.")  # fmt: skip
    assert out.text == "Nothing is being stopped, and I haven't paused anything.\n\n" + F.ON_IT


# ------------------------------------------------------------------ trading stays the Trader's


def test_trading_messages_are_still_the_traders_even_straight_after_the_night(alive):
    for stamp, text in NIGHT:
        _say(alive, text, _at(stamp))
    chat.wait_idle(alive.app, 10)
    handed = len(_inbox())
    t = _at("2026-09-25 23:47:00")
    clock = {"t": t}
    alive.app.clock = lambda: clock["t"]
    _say(alive, "stop trading for today", t)
    # 26 Sep 2026: "stop trading for today" is Rick's no-new-entries switch (arena/pause.py);
    # at 23:47 the market has closed, so nothing is set - and it is still the Trader's.
    assert alive.tg.texts()[-1].startswith("The market has closed for today")
    clock["t"] = t + timedelta(seconds=20)
    _say(alive, "sell BHP", t + timedelta(seconds=20))
    assert alive.tg.texts()[-1] == chat.NO_ORDERS
    _say(alive, "what's my risk per trade", t + timedelta(seconds=40))
    chat.wait_idle(alive.app, 10)
    _say(alive, "how did the day trader go", t + timedelta(seconds=60))
    chat.wait_idle(alive.app, 10)
    assert alive.decider.asked() == ["what's my risk per trade", "how did the day trader go"]
    assert alive.tg.texts()[-2:] == ["answer 1", "answer 2"]
    assert len(_inbox()) == handed  # none of them went to the Foreman

    # the decider's first turn after the handovers is told what went to the Foreman
    first, second = (m for m, _ in alive.decider.calls)
    assert "Since your last answer Rick said these to the Foreman" in first
    assert "- 11:45pm \"Prioritise the simulator ahead of fetch\"" in first
    assert "- 11:20pm \"No keep building I have a full reset\"" not in first  # the last 5 only
    assert "Since your last answer" not in second
    assert "The Foreman is Rick's orchestrator" in first and "HANDOVER:" in first
    assert "You cannot change it" not in first


def test_trading_messages_on_their_own(alive):
    for text in ("stop trading for today", "sell BHP", "what's my risk per trade",
                 "how did the day trader go", "what are you doing", "status", "stop",
                 "answer them one at a time", "what's the daily loss limit",
                 "is it building a position in BHP", "what changes are being built"):
        assert F.topic(text) is None, text
    assert _inbox() == []


# ------------------------------------------------------------------ what is the Foreman's

FOREMANS = [
    "No keep building I have a full reset",
    "I have a limit reset to use when it runs out",
    "I’ll use the reset at 99 percent usage",
    "What no keep everything going I won’t allow it to stop",
    "Prioritise the simulator ahead of fetch",
    "what are you working on",
    "what's being worked on",
    "what's the build queue",
    "how did the build go",
    "stop the builds",
    "pause all builds at 70%",
    "how much claude usage is left",
    "when does my weekly limit reset",
    "are we close to the session limit",
    "what are the priorities for the bots",
    "foreman status",
    "tell the foreman to hurry up",
    "don't stop the builds",
    "what's in the backlog",
]
NOT_FOREMANS = [
    "stop trading for today", "sell BHP", "what's my risk per trade", "how did the day trader go",
    "stop", "status", "what's running", "what are you doing", "what's the daily loss limit",
    "is it building a position in BHP", "what changes are being built", "is my change built",
    "can you make it build the evening report earlier", "reset the chat", "start over",
    "run the history fetch first", "how's it going today", "keep the watcher going",
    "what's the plan for tomorrow", "use opus for the decider", "thanks", "how many positions "
    "can it hold", "the decider used opus yesterday",
]


@pytest.mark.parametrize("text", FOREMANS)
def test_the_foremans_topics(text):
    assert F.topic(text), text


@pytest.mark.parametrize("text", NOT_FOREMANS)
def test_not_the_foremans_topics(text):
    assert F.topic(text) is None, text


def test_a_follow_up_counts_only_straight_after_a_foreman_topic(alive):
    t0 = _at("2026-09-25 23:31:13")
    _say(alive, "I’ll use the reset at 99 percent usage", t0)
    _say(alive, "Yes tell me at 99", t0 + timedelta(minutes=9))
    assert [g["text"] for g in _inbox()][-1] == "Yes tell me at 99"
    # ten minutes after the last Foreman topic, the same words go to the decider
    _say(alive, "Yes tell me at 99", t0 + timedelta(minutes=9 + 11))
    chat.wait_idle(alive.app, 10)
    assert alive.decider.asked() == ["Yes tell me at 99"]
    for text in ("yes tell me at 99", "stop at 80%", "carry on", "ok keep going",
                 "two at a time", "status"):
        assert F.follow_up(text), text
    for text in ("thanks", "yes", "stop trading", "tell me when BHP hits 45",
                 "answer them one at a time", "why did it pass on NWL", "sell at 99",
                 "stop the answer"):
        assert not F.follow_up(text, {"BHP", "NWL"}), text


# ------------------------------------------------------------------ the decider hands over


def test_the_decider_hands_over_with_one_short_line(alive):
    alive.app.agent_runner = lambda message, key: ("HANDOVER: remind him at 3pm tomorrow\n"
                                                   "The Foreman's on it - it'll answer here.\n"
                                                   "It will remind you then.")  # fmt: skip
    at = _at("2026-09-26 00:30:00")
    _say(alive, "remind me at 3pm tomorrow to call the accountant", at)
    chat.wait_idle(alive.app, 10)
    (got,) = _inbox()
    assert got == {"bot": "trader", "from": "Rick",
                   "text": "remind me at 3pm tomorrow to call the accountant",
                   "at": "2026-09-26T00:30:00+10:00",
                   "why": "can't do: remind him at 3pm tomorrow"}  # fmt: skip
    assert alive.tg.texts() == [F.ON_IT]  # at most one short line


def test_a_handover_line_that_says_cant_is_replaced(alive):
    alive.app.agent_runner = lambda m, k: "HANDOVER: a price alert\nI can't do alerts myself."
    _say(alive, "tell me when BHP hits 45", datetime.now(SYD))
    chat.wait_idle(alive.app, 10)
    assert alive.tg.texts() == [F.ON_IT]
    assert _inbox()[0]["why"] == "can't do: a price alert"


def test_silent_sends_nothing_and_hands_nothing_over(alive):
    alive.app.agent_runner = lambda m, k: "SILENT"
    _say(alive, "thanks mate", datetime.now(SYD))
    chat.wait_idle(alive.app, 10)
    assert alive.tg.sent == [] and _inbox() == []


def test_a_knowing_cant_or_a_hard_rule_is_left_alone(alive):
    said = ("I can't tell from the records why it passed. Orders aren't placed from this "
            "chat, and I can't close a position from here.")  # fmt: skip
    alive.app.agent_runner = lambda m, k: said
    _say(alive, "why did it pass, and can you close it", datetime.now(SYD))
    chat.wait_idle(alive.app, 10)
    assert alive.tg.texts() == [said] and _inbox() == []


def test_a_hard_rule_is_never_handed_over(alive):
    alive.app.agent_runner = lambda m, k: "HANDOVER: switch to real money"
    _say(alive, "can we go live with real money next week", datetime.now(SYD))
    chat.wait_idle(alive.app, 10)
    assert _inbox() == []
    assert alive.tg.texts() == ["That's one of your hard rules (no real-money orders from the "
                                "trade bot), so nothing will be built for it."]  # fmt: skip


# ------------------------------------------------------------------ the Foreman down, the file


def test_when_the_foreman_is_down_rick_is_told_plainly(env):  # noqa: F811
    _beat(datetime.now(SYD) - timedelta(minutes=30))
    _say(env, "keep building", datetime.now(SYD))
    assert len(_inbox()) == 1
    (text,) = env.tg.texts()
    assert text.startswith("That one's for the Foreman (it runs the builds and watches usage")
    assert "hasn't checked in since" in text and "waiting in its inbox" in text
    assert not CANT.search(text)
    (F.home() / "heartbeat.json").unlink()
    _say(env, "what's the build queue", datetime.now(SYD))
    assert "it isn't running on this PC right now" in env.tg.texts()[-1]
    # what the decider did answer is kept; only "the Foreman's on it" becomes the truth
    kept = "Nothing's paused on the arena side; the watcher runs by itself."
    env.app.agent_runner = lambda m, k: kept + " I can't pause anything from here."
    _say(env, "is anything paused", datetime.now(SYD))
    chat.wait_idle(env.app, 10)
    assert env.tg.texts()[-1].startswith(kept + "\n\nThat one's for the Foreman")


def test_the_handover_file(tmp_path):
    at = datetime(2026, 9, 25, 23, 45, 44, tzinfo=SYD)
    path = F.hand_over("Prioritise the simulator ahead of fetch", at, "foreman topic",
                       now=datetime(2026, 9, 25, 23, 45, 45, tzinfo=SYD))  # fmt: skip
    assert path.parent == F.home() / "inbox"
    assert re.fullmatch(r"20260925-234545-trader-[0-9a-f]{4}\.json", path.name)
    assert list(path.parent.iterdir()) == [path]  # the .tmp is gone
    body = path.read_bytes().decode("utf-8")
    assert json.loads(body) == {"bot": "trader", "from": "Rick", "at": "2026-09-25T23:45:44+10:00",
                                "text": "Prioritise the simulator ahead of fetch",
                                "why": "foreman topic"}  # fmt: skip
    p2 = F.hand_over("I’ll use the reset", at, "foreman topic")
    assert "I’ll use the reset" in p2.read_text(encoding="utf-8")  # UTF-8, not \u escapes


def test_the_whole_message_is_logged_on_one_line_for_the_foreman(alive, caplog):
    long = "prioritise " + " ".join(f"thing{i}" for i in range(60)) + "\nand then fetch"
    with caplog.at_level(logging.INFO, logger="asxbot.chat"):
        _say(alive, long, datetime.now(SYD))
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("from Rick:"))
    assert line == "from Rick: " + " ".join(long.split())
    assert len(line) > 200 and "\n" not in line
    assert _inbox()[0]["text"] == long.strip()


def test_handover_is_only_the_first_or_last_line():
    said = ("Here's how it works. When he asks for an alert you reply\nHANDOVER: an alert\n"
            "and the Foreman picks it up.")  # fmt: skip
    assert F.read_reply(said).handover is None
    assert F.read_reply("**HANDOVER:** a reminder").handover == "can't do: a reminder"
    assert F.read_reply("Fair enough.\nHANDOVER: a reminder").text == "Fair enough."
    # the --no-agent probe echoes the whole prompt, HANDOVER example and all
    echo = "(--no-agent: the decider was not called)\n\n" + chat.chat_message("hi")
    assert F.read_reply(echo).handover is None


def test_a_probe_prints_the_handover_and_never_writes_it(monkeypatch, quiet_main, capsys,  # noqa: F811
                                                         tmp_path):  # fmt: skip
    from argparse import Namespace

    from asxbot import botctl
    from test_chat import OC

    oc = tmp_path / "openclaw.json"
    oc.write_text(json.dumps(OC))
    monkeypatch.setattr(botctl, "OPENCLAW_JSON", oc)
    monkeypatch.setattr(botctl, "CHANGES_DIR", tmp_path / "changes")
    _beat()
    args = Namespace(probe="keep building", button=None, no_agent=True)
    assert chat.main(args) == chat.EXIT_OK
    out = capsys.readouterr().out
    assert "--- to the Foreman's inbox (probe: not written) ---" in out
    assert '"text": "keep building"' in out and '"why": "foreman topic"' in out
    assert "--- to Rick" not in out  # the Trader says nothing
    assert _inbox() == []


def test_help_names_the_foreman(alive):
    _say(alive, "help", datetime.now(SYD))
    assert "the Foreman answers those here itself" in alive.tg.texts()[-1]
    assert plain.understand("help") is not None
