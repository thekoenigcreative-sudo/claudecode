"""A review of the Trader chat, 26 Sep 2026: every confirmed finding, replayed.

F1  model and effort changes fired from questions, negations and musings, at once and mid-test
F2  an honest refusal about the trading rules was handed to the Foreman as a build
F4  there was no real "no new entries today" switch
F5  ordinary words were read as stock codes ("what happened today pls" -> PLS)
F6  "whats", "hows it goin", "c'mon", "fuck it ..." missed
F7  money questions reached the decider without the records; the records had no time
F8  nothing answered IB Gateway / feed questions from the records
F9  decisions did not say which prices they were made on; positions were marked by fetching
F10 a trading day with the watcher down was answered as the previous session, silently
F11 "c'mon" was Monday; "this week" was today; a past day listed the positions held now
F12 misroutes: order refusals, queue modes, change lists, the report, the hours, restarts
F13 a bare "yes" to a Foreman message reached the decider without its context
F15 a clear request was swallowed as the answer to a change reader's question
F16 "tonight's report" was 19:00; botctl replies named commands

Same fakes as test_chat.py and test_plain.py: no network, no model, a throwaway git repo, a
temporary data folder, the Foreman's and IB Gateway's state in temporary folders."""

import json
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from asxbot import botctl, chat, plain
from asxbot import foreman as F
from asxbot.arena import settings_history as H
from test_chat import SYD, data_cfg, env, msg, repo, unfreeze
from test_plain import DAY, KNOWN, NOW, _reply, _tap, records

FIXTURES = (data_cfg, env, repo, records)  # pytest finds them here, by name

SESSION = NOW.replace(hour=11, minute=0)  # Fri 25 Sep 2026, in the session


def _agents(r):
    import yaml

    return yaml.safe_load((r / "config.yaml").read_text(encoding="utf-8"))["arena"]["agents"]


def _data(rec):
    return rec.app.cfg_loader().data_dir


# ------------------------------------------------------------------ F1 model changes


@pytest.mark.parametrize("text", [
    "dont switch the decider to sonnet", "sonnet for the reader?", "maybe opus for the reader",
    "use sonnet for the reader tomorrow", "the decider stays on opus",
])  # fmt: skip
def test_a_question_negation_or_hedge_only_shows_the_models(records, text):
    reply = _reply(records, text)
    assert "Models allowed here:" in reply and "freezes them" in reply
    assert records.tg.sent[-1][1] is None  # no buttons: nothing offered
    assert records.run.sets() == [] and _agents(records.repo)["history"] == []


@pytest.mark.parametrize("text", [
    "decider think harder about exits", "less thinking more trading",
    "opus 5.5 passed on everything",
])  # fmt: skip
def test_a_musing_about_models_is_conversation(records, text):
    records.app.on_update(msg(text))
    chat.wait_idle(records.app, 10)
    assert records.decider.asked() == [text]
    assert records.run.sets() == [] and _agents(records.repo)["history"] == []


def test_i_think_we_are_up_is_the_money_not_a_thinking_change(records):
    assert _reply(records, "I think we are up today").startswith("Fri 25 Sep, today - the arena")
    assert records.run.sets() == []


def test_the_test_freeze_is_read_from_config_yaml(records):
    fz = H.frozen_test(records.app.raw())
    assert fz["titles"] == ["ASX announcements v2", "ASX day trader"]
    assert (fz["start"], fz["end"], fz["days"]) == (DAY, date(2026, 10, 8), 10)
    assert H.frozen_test({"arena": {"playbooks": {"x": {"enabled": True,
                                                        "status": "passed"}}}}) is None


def test_slash_model_mid_test_needs_change_it_anyway(records):
    text = _reply(records, "/model decider sonnet-5")
    assert "would break the 10-day test" in text
    assert [b[0] for b in records.tg.sent[-1][1]] == ["Change it anyway", "Cancel"]
    # a bare "yes" is not enough to break the frozen test
    assert _reply(records, "yes").startswith("That breaks the frozen test, so it needs the "
                                             "Change it anyway button")
    assert records.run.sets() == []
    text = _reply(records, "change it anyway")
    assert "decider model set to Sonnet 5" in text
    assert "tonight's evening report (7:30pm) will mention it" in text  # F16: not 19:00
    entry = _agents(records.repo)["history"][-1]
    assert entry["by"] == ("Rick, /model in the Trader chat, confirmed on a read-back; breaks "
                           "the frozen 10-day test")


def test_a_change_from_any_other_path_mid_test_is_refused_in_code(records):
    text = _reply(records, "/new sonnet")  # botctl's /new <model> goes straight to /model
    assert "Not changed: the 10-day test of ASX announcements v2 and ASX day trader" in text
    assert records.run.sets() == [] and _agents(records.repo)["history"] == []


def test_outside_a_test_plain_words_are_still_read_back(records):
    unfreeze(records.repo)
    text = _reply(records, "use sonnet for the decider")
    assert text.startswith("Switch the decider's model from Opus 5.5 to Sonnet 5? It's a "
                           "strategy change")
    assert [b[0] for b in records.tg.sent[-1][1]] == ["Change it", "Cancel"]
    assert records.run.sets() == []
    assert "decider model set to Sonnet 5" in _reply(records, "yes")
    assert _agents(records.repo)["history"][-1]["by"] == (
        "Rick, plain words in the Trader chat, confirmed on a read-back")
    # ... and /model outside a test changes it at once, as before
    assert "decider model set to Opus 5.5" in _reply(records, "/model decider opus-5-5")
    assert _agents(records.repo)["history"][-1]["by"] == "Rick, /model in the Trader chat"


def test_a_read_back_lapses(records):
    _reply(records, "use sonnet for the decider")
    records.app.clock = lambda: NOW + timedelta(minutes=31)
    assert _tap(records) == chat.LAPSED
    assert records.run.sets() == []


# ------------------------------------------------------------------ F2 honest refusals


@pytest.mark.parametrize("said", [
    "I can't change the stop rules mid-test - they're frozen until 8 Oct.",
    "I can't change the risk limits from here.",
    "I can't switch the decider's model mid-test.",
    "I can't make the watcher trade the open.",
    "I can't change the playbook's entry threshold during the test.",
])  # fmt: skip
def test_a_refusal_about_the_trading_rules_is_never_handed_over(said):
    out = F.read_reply(said)
    assert out.handover is None and out.text == said


def test_a_refusal_about_something_buildable_is_still_handed_over():
    for said in ("I can't set that up from here.", "I can't watch your usage limits from here."):
        assert F.read_reply(said).handover, said


# ------------------------------------------------------------------ F4 no new entries today


def _pause_file(rec, day=DAY):
    return _data(rec) / "arena" / "pause" / f"{day.isoformat()}.json"


def test_no_new_entries_today_is_read_back_then_set_and_shown(records):
    records.app.clock = lambda: SESSION
    text = _reply(records, "no new entries today")
    assert text == ("Stop NEW entries for the rest of today, for both playbooks and both books? "
                    "Exits, stops and the 15:50 close keep working.")
    assert not _pause_file(records).exists()  # nothing until Rick taps
    text = _tap(records)
    assert text.startswith("Done: new entries are paused for the rest of today, from 11:00")
    body = json.loads(_pause_file(records).read_text(encoding="utf-8"))
    assert body["by"] == "Rick" and body["words"] == "no new entries today"
    assert body["at"].startswith("2026-09-25T11:00")
    ev = (_data(records) / "events" / "arena_pause.jsonl").read_text(encoding="utf-8")
    assert '"event": "paused"' in ev
    # status and the day's summary say so
    assert "New entries paused since 11:00 (Rick)" in _reply(records, "status")
    assert "New entries paused since 11:00 (Rick)" in _reply(records, "how's it going today")
    assert "New entries paused since 11:00 (Rick)" in _reply(records, "stop it for today")
    # resume, with a typed yes
    assert _reply(records, "resume trading").startswith("Allow new entries again for the rest "
                                                        "of today? They have been paused since "
                                                        "11:00 (Rick).")  # fmt: skip
    assert _reply(records, "yes") == ("Done: new entries are allowed again for the rest of "
                                      "today (the pause from 11:00 is lifted).")
    assert not _pause_file(records).exists()
    assert _reply(records, "resume trading") == ("New entries aren't paused today, so there's "
                                                 "nothing to lift.")
    assert records.decider.calls == []


@pytest.mark.parametrize("text", [
    "can you make it stop trading today", "stop buying for today", "halt entries",
    "kill switch", "shut it down", "switch it off", "emergency stop", "fuck it stop everything",
    "dont open any more positions today", "dont trade today", "stop it for today",
])  # fmt: skip
def test_ricks_ways_of_asking_all_reach_the_pause(records, monkeypatch, text):
    monkeypatch.setattr(botctl, "interpret", lambda *a, **k: pytest.fail("a change request"))
    records.app.clock = lambda: SESSION
    assert _reply(records, text).startswith("Stop NEW entries for the rest of today")
    assert records.decider.calls == []


def test_cancel_pauses_nothing_and_a_weekend_needs_no_pause(records):
    records.app.clock = lambda: SESSION
    _reply(records, "stop trading for today")
    assert _tap(records, 1) == "OK - nothing was paused; entries carry on as normal."
    assert not _pause_file(records).exists()
    records.app.clock = lambda: datetime(2026, 9, 26, 11, 0, tzinfo=SYD)
    assert _reply(records, "stop trading for today").startswith(
        "Sat 26 Sep isn't an ASX trading day, so nothing enters today anyway.")
    records.app.clock = lambda: SESSION
    text = _reply(records, "stop the watcher")
    assert "The watcher itself keeps running" in text


def test_stop_everything_also_stops_the_answer_being_worked_on(records):
    records.app.clock = lambda: SESSION
    records.decider.gate.clear()
    records.app.on_update(msg("tell me a long story"))
    assert records.decider.started.wait(5)
    records.app.on_update(msg("stop everything"))
    text = records.tg.texts()[-1]
    records.decider.gate.set()
    assert text.startswith("Stopped: the decider's answer to you")
    assert "Stop NEW entries for the rest of today" in text


# ------------------------------------------------------------------ F5 codes, F6 spellings


def test_ordinary_words_are_not_stock_codes():
    known = {"PLS", "WOW", "BET", "OMG", "MAD", "FRI", "MIN", "NWL", "HLS", "REG"}
    assert plain.tickers_in("what happened today pls", known) == []
    assert plain.tickers_in("wow what a day", known) == []
    assert plain.tickers_in("omg", known) == []
    assert plain.tickers_in("is nwl up", known) == []  # lower case, no context
    assert plain.tickers_in("is nwl up", known, recent={"NWL"}) == ["NWL"]  # the day's records
    assert plain.tickers_in("what about nwl", known) == ["NWL"]
    assert plain.tickers_in("any news on hls and reg", known) == ["HLS", "REG"]
    assert plain.tickers_in("WHAT HAPPENED TODAY PLS", known) == []  # the whole message shouts
    assert plain.tickers_in("why did it pass on PLS", known) == ["PLS"]
    assert plain.tickers_in("NWL", known) == ["NWL"]


def test_what_happened_today_pls_is_the_day_not_pilbara(records):
    records.app.known_codes = lambda: KNOWN | {"PLS"}
    assert _reply(records, "what happened today pls").startswith("Fri 25 Sep, today - the arena")


def test_quick_spellings(records):
    assert _reply(records, "whats running") == "Nothing running."
    assert _reply(records, "hows it goin").startswith("Fri 25 Sep, today - the arena")
    assert _reply(records, "watcher up?").startswith("The Trader\nWatcher: ")


# ------------------------------------------------------------------ F7 money, the facts' time


def test_money_questions_are_answered_from_the_records(records):
    for text in ("how much did we make today", "did we lose money", "is the agent beating "
                 "the bot", "bot vs agent"):  # fmt: skip
        assert "ASX announcements v2: AGENT $20,000.00" in _reply(records, text), text
    assert records.decider.calls == []


def test_the_facts_carry_their_time_and_say_earlier_numbers_are_stale(records):
    records.app.on_update(msg("explain the pending orders to me"))
    chat.wait_idle(records.app, 10)
    (message, _), = records.decider.calls
    assert "It is the truth about what happened as of\n17:00 on Fri 25 Sep Sydney time." \
        in message
    assert "Numbers from earlier turns of this conversation are older" in message
    assert "FACTS ON RECORD:" in message
    assert plain.about_trading("is the gateway back") and plain.about_trading("what's pending")


# ------------------------------------------------------------------ F8 the feed


def test_gateway_questions_are_answered_from_the_records(records, ibkr_state_isolated):
    data = _data(records)
    (data / "arena").mkdir(parents=True, exist_ok=True)
    (data / "arena" / "live_data.json").write_text(json.dumps({
        "at": "2026-09-25T16:58:00+10:00", "provider_in_use": "yfinance",
        "label": "delayed data (Yahoo) - IBKR unavailable", "entries": "paused",
        "paused_why": "live feed down: not connected", "paused_since": "2026-09-25T16:40:00+10:00",
        "gateway": {"connected": False, "server_ok": False, "market_data": "real-time",
                    "last_ok_at": "2026-09-25T16:39:30+10:00"}}), encoding="utf-8")  # fmt: skip
    d = ibkr_state_isolated
    d.mkdir(parents=True, exist_ok=True)
    (d / "doctor.json").write_text(json.dumps({
        "last": {"at": "2026-09-25T16:59:00+10:00", "cause": "gateway_down",
                 "evidence": ["port 4001 closed"]},
        "episode": {"cause": "gateway_down", "since": "2026-09-25T16:40:00+10:00",
                    "causes": ["gateway_down"], "actions": [
                        {"at": "2026-09-25T16:40:00+10:00", "action": "run_supervisor"}]},
        "history": [{"since": "2026-09-25T09:02:00+10:00", "until": "2026-09-25T10:16:00+10:00",
                     "causes": ["gateway_down"], "fixed_by": "the supervisor restarted IB "
                     "Gateway"}]}), encoding="utf-8")  # fmt: skip
    (d / "supervisor.json").write_text(json.dumps({
        "last_check": "2026-09-25T16:58:30+10:00", "outage": {"since": "2026-09-25T16:40:00"},
        "last_result": "RESTART: down | port closed; launcher not running; login stored"}),
        encoding="utf-8")  # fmt: skip
    for text in ("is the gateway up", "gateway should be back", "is it on ibkr or yahoo"):
        reply = _reply(records, text)
        assert reply.startswith("IBKR and the prices, from the records:"), text
    assert "- The watcher's feed, as of 16:58, 2 min ago: prices from Yahoo (delayed)" in reply
    assert "- Connection: NOT connected to IB Gateway, real-time prices, last good answer " \
           "16:39, 20 min ago." in reply
    assert "- New entries: PAUSED by the feed since 16:40 - live feed down" in reply
    assert "- Connection doctor, last check 16:59, just now: IB Gateway is not running " \
           "(port 4001 closed)." in reply
    assert "tried so far: run_supervisor" in reply
    assert "- The last problem it closed: 09:02 to 10:16, IB Gateway is not running - the " \
           "supervisor restarted IB Gateway." in reply
    assert "- IB Gateway, the supervisor's check at 16:58, 2 min ago: port closed" in reply
    assert "- Your IBKR login is stored" in reply
    assert "The supervisor has an outage open since 16:40" in reply
    assert "OpenClaw" not in reply and records.decider.calls == []
    status = _reply(records, "status")
    assert "Data: delayed data (Yahoo) - IBKR unavailable (as of 16:58, 2 min ago); new " \
           "entries paused by the feed - live feed down: not connected" in status


def test_gateway_questions_with_nothing_on_record(records):
    reply = _reply(records, "is ibkr connected")
    assert "- The watcher's feed: nothing on record yet" in reply
    assert "- IB Gateway supervisor: nothing on record." in reply


# ------------------------------------------------------------------ F9 price sources


def test_each_decision_says_which_prices_it_was_made_on(records):
    src = _data(records) / "arena" / "price_sources"
    src.mkdir(parents=True, exist_ok=True)
    (src / "2026-09-25.json").write_text(json.dumps({"labels": {
        "NWL pre_open": "delayed data (Yahoo) - IBKR unavailable"}}), encoding="utf-8")
    text = _reply(records, "why did it pass on NWL")
    assert ("08:21 decider (pre-open look): PASS - the market has had four days to price it; "
            "deciding after ten minutes of trading beats a blind auction fill [prices: delayed "
            "data (Yahoo) - IBKR unavailable]") in text
    assert "10:30 rule bot: signal - -3.15% vs the ASX 200 at 10:30 on 6.1x usual first-30-" \
           "minute volume; ordered ARN-000004 short 285 @ 17.51, stop 18.34 [prices: live " \
           "data (IBKR)]" in text


def _hold_nwl(rec):
    from asxbot.arena.accounts import Position

    arena = rec.app.light_arena()
    pb = next(p for p in arena.playbooks() if p.key == "asx_announcements_v2")
    acct = arena.account(pb, "bot")
    acct.positions["NWL"] = Position("NWL", -285, 17.8656, "2026-09-25T10:54+10:00", stop=18.34,
                                     opened_by="bot")  # fmt: skip
    arena.store.save(acct)


def test_positions_are_marked_from_disk_never_fetched(records, monkeypatch):
    from asxbot.arena import minutes as M

    monkeypatch.setattr(M.MinuteBars, "_yahoo_fetch",
                        lambda *a, **k: pytest.fail("the chat fetched prices"))
    _hold_nwl(records)
    text = _reply(records, "how much are we up")
    assert "NWL -285 @ 17.8656, now 17.8656 (at cost - no price on disk yet; +0.00)" in text
    bars = pd.DataFrame({"open": [16.9], "high": [17.0], "low": [16.9], "close": [16.99],
                         "volume": [100]},
                        index=pd.DatetimeIndex([pd.Timestamp("2026-09-25 15:59", tz=SYD)]))
    path = _data(records) / "arena" / "minutes" / "NWL" / "2026-09-25.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    bars.to_parquet(path)
    text = _reply(records, "how much are we up")
    assert "now 16.99 (last minute bar on disk, 15:59, not a live quote; +249.55)" in text
    text = _reply(records, "show me the positions")
    assert "Prices: NWL at its last minute bar on disk, 15:59. None of these is a live " \
           "quote." in text


# ------------------------------------------------------------------ F10 the watcher first


def test_a_trading_day_with_nothing_recorded_leads_with_todays_watcher(records):
    records.app.clock = lambda: datetime(2026, 9, 28, 11, 0, tzinfo=SYD)  # Monday
    text = _reply(records, "how's it going today")
    lines = text.splitlines()
    assert lines[0].startswith("Nothing recorded today (Mon 28 Sep) - Watcher: ")
    assert lines[2] == "Fri 25 Sep, the last session on record - the arena, FAKE money."
    records.app.clock = lambda: datetime(2026, 9, 26, 11, 0, tzinfo=SYD)  # Saturday
    text = _reply(records, "how's it going today")
    assert text.startswith("Fri 25 Sep, the last session on record - the arena")


# ------------------------------------------------------------------ F11 days and weeks


def test_cmon_is_not_monday_and_the_week_is_day_by_day(records):
    text = _reply(records, "c'mon why no trades today")
    assert text.startswith("Fri 25 Sep, today - the arena")
    records.app.clock = lambda: datetime(2026, 9, 26, 10, 0, tzinfo=SYD)
    text = _reply(records, "how did we go this week")
    assert text.splitlines() == [
        "This week, Mon 21 Sep to Fri 25 Sep - the arena, FAKE money. I answer day by day "
        "from the records:",
        "  Fri 25 Sep: 1 order placed, 1 refused, 2 fills, realised +250.41 before fees",
        "Over the week, from the closing marks:",
        "  ASX announcements v2: AGENT no closing mark that week; BOT no closing mark that week",
        "  ASX day trader: AGENT no closing mark that week; BOT no closing mark that week",
        "Ask about any one of those days for its detail.",
    ]
    assert _reply(records, "how did we go last week").splitlines()[1] == \
        "Nothing on record for those days."  # fmt: skip


def test_a_past_day_never_lists_the_positions_held_now(records):
    _hold_nwl(records)
    text = _reply(records, "how did it go yesterday")
    assert "NWL" not in text
    assert text.endswith("Positions at that day's close: not all on record (no closing mark).")


# ------------------------------------------------------------------ F12 misroutes


@pytest.mark.parametrize("text", [
    "short answer please", "short version", "cover the basics", "add to the list", "exit",
    "would you buy BHP", "sorry to interrupt", "i didnt mean to interrupt",
    "take it one at a time", "what changes did it make to the stops",
    "reduce the position size",
])  # fmt: skip
def test_not_an_order_not_a_setting_just_conversation(records, text):
    records.app.on_update(msg(text))
    chat.wait_idle(records.app, 10)
    assert records.decider.asked() == [text]
    assert chat.NO_ORDERS not in records.tg.texts()
    assert records.app.ctl.queue()["mode"] == "followup"  # the fixture's, unchanged


def test_open_a_new_chat_and_real_orders(records):
    assert _reply(records, "open a new chat").startswith("Fresh start.")
    assert _reply(records, "sell BHP") == chat.NO_ORDERS
    assert _reply(records, "double check the positions").startswith("Arena positions")


def test_did_the_report_go_out(records):
    sent = _data(records) / "arena" / "report_sent.json"
    sent.parent.mkdir(parents=True, exist_ok=True)
    sent.write_text(json.dumps({"sent_at": "2026-09-24T19:31:09+10:00"}), encoding="utf-8")
    assert _reply(records, "did the evening report go out") == (
        "The last evening report went out at 7:31pm on Thu 24 Sep (its record of being "
        "sent).\nTonight's is due at 7:30pm.")
    records.app.clock = lambda: NOW.replace(hour=20, minute=30)
    assert "Tonight's was due at 7:30pm and is NOT on record as sent" in _reply(
        records, "i didnt get the evening report")
    sent.write_text(json.dumps({"sent_at": "2026-09-25T19:31:09+10:00"}), encoding="utf-8")
    assert _reply(records, "has the report been sent").endswith("That's tonight's.")


def test_market_hours_and_the_watcher_lock(records):
    records.app.clock = lambda: SESSION
    assert "Right now (11:00) the market is open" in _reply(records, "is the market open")
    assert _reply(records, "is today a trading day").startswith("Fri 25 Sep: an ASX trading day")
    text = _reply(records, "restart the watcher")
    assert text.startswith(chat.WATCHER_LOCK) and "\nRight now: Watcher: " in text
    assert records.decider.calls == []


# ------------------------------------------------------------------ F13 replies to the Foreman


def _reply_to(text: str, to: str, who_is_bot: bool = True) -> dict:
    m = msg(text)
    m["message"]["reply_to_message"] = {"message_id": 7, "text": to,
                                        "from": {"id": 1, "is_bot": who_is_bot}}  # fmt: skip
    return m


def _inbox():
    box = F.inbox()
    return [json.loads(p.read_text(encoding="utf-8")) for p in box.glob("*.json")] \
        if box.exists() else []  # fmt: skip


def test_a_yes_to_the_foreman_goes_to_the_foreman_with_its_question(records):
    F.home().mkdir(parents=True, exist_ok=True)  # the Foreman is running
    (F.home() / "heartbeat.json").write_text(json.dumps(
        {"at": datetime.now(SYD).isoformat(timespec="seconds"), "state": "running"}),
        encoding="utf-8")  # fmt: skip
    asked = "Foreman: builds are paused at 99% usage. Carry on after the reset?"
    assert records.app.on_update(_reply_to("ok do it", asked)) == "handled"
    chat.wait_idle(records.app, 10)
    (got,) = _inbox()
    assert got["text"] == "ok do it" and got["reply_to"] == asked
    assert got["why"] == "reply to a Foreman message"
    assert records.decider.calls == [] and records.tg.sent == []
    # a change job's report counts too
    records.app.on_update(_reply_to("yes", "Change #0926-1015 is built and tested: ..."))
    assert len(_inbox()) == 2
    # a reply about trading is still the Trader's
    records.app.on_update(_reply_to("sell BHP", asked))
    assert records.tg.texts()[-1] == chat.NO_ORDERS and len(_inbox()) == 2
    # a yes to anything else is a conversation, as before
    records.app.on_update(_reply_to("yes", "NWL on Fri 25 Sep, today, from the records: ..."))
    chat.wait_idle(records.app, 10)
    assert records.decider.asked() == ["yes"] and len(_inbox()) == 2


# ------------------------------------------------------------------ F15 clear intents first


def test_a_clear_request_is_never_swallowed_as_a_change_answer(records, monkeypatch):
    monkeypatch.setattr(botctl, "interpret", lambda ad, words, answer=None: {
        "is_change": True, "runtime_hint": None, "summary": "x", "limits": [],
        "off_limits": None, "question": None if answer else "Which account?"})  # fmt: skip
    records.app.on_update(msg("can you make it show the cash first"))
    chat.wait_idle(records.app, 10)
    assert records.tg.texts()[-1] == "One question first: Which account?"
    assert _reply(records, "how's it going today").startswith("Fri 25 Sep, today - the arena")
    assert _reply(records, "stop") == "Nothing was running, so there was nothing to stop."
    # ... while a real answer still answers it
    records.app.on_update(msg("the bot's"))
    chat.wait_idle(records.app, 10)
    assert [b[0] for b in records.tg.sent[-1][1]] == ["Build it", "Cancel"]


# ------------------------------------------------------------------ F16 no commands named


def test_botctl_replies_are_reworded_without_commands(records):
    rec = {"id": "trader-20260925-110000", "bot": "trader", "repo": str(records.repo),
           "status": "building", "history": [], "created": "2026-09-25T11:00:00"}  # fmt: skip
    botctl.save_req(rec)
    records.decider.gate.clear()
    records.app.on_update(msg("tell me a long story"))
    assert records.decider.started.wait(5)
    records.app.on_update(msg("stop"))
    text = records.tg.texts()[-1]
    records.decider.gate.set()
    assert "saying \"undo the last change\" reverses it once it's live" in text
    assert "/undo" not in text
