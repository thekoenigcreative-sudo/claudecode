"""Plain language in the Trader chat (asxbot.plain, asxbot.arena.today).

Rick, 25 Sep 2026: "i need to be able to just tell it things without commands". Every
command the chat has also works from an ordinary sentence, worked out in code; a question
about the day's trading is answered from the arena's records, never guessed; and no reply
tells Rick to type a command. Same fakes as test_chat.py: no network, no model, a throwaway
git repo, a temporary data folder holding one day's records."""

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from asxbot import botctl, chat, plain
from asxbot.arena import today as T
from test_chat import RICK, SYD, data_cfg, env, msg, repo  # noqa: F401 - the chat's fixtures

DAY = date(2026, 9, 25)
NOW = datetime(2026, 9, 25, 17, 0, tzinfo=SYD)
KNOWN = {"NWL", "HLS", "REG", "EOS", "CWY", "BHP", "ALL", "A1M", "DOW"}

# (what Rick types, what the chat does). None: not one of its own things - a change
# request or a conversation with the decider, as before.
PHRASES = [
    ("how's it going today", "today"),
    ("How is it going?", "today"),
    ("how did it go today", "today"),
    ("what happened today", "today"),
    ("give me an update", "today"),
    ("how are we doing", "today"),
    ("what did it do today", "today"),
    ("what happened yesterday", "today"),
    ("what did it trade", "trades"),
    ("what did it trade today?", "trades"),
    ("any trades today", "trades"),
    ("did it buy anything", "trades"),
    ("show me the fills", "trades"),
    ("stop it for today", "pause"),
    ("stop", "stop"),
    ("cancel that", "stop"),
    ("never mind", "stop"),
    ("use opus for the decider", "model_set"),
    ("switch the reader to sonnet 5", "model_set"),
    ("put the decider on opus 5.5", "model_set"),
    ("decider back to normal", "model_set"),
    ("use sonnet", "model_set"),
    ("both on sonnet", "model_set"),
    ("change the reader's model to sonnet", "model_set"),
    ("run the decider on opus", "model_set"),
    ("what model is it on", "model_show"),
    ("which models are they running", "model_show"),
    ("is the decider on opus?", "model_show"),
    ("make the decider think harder", "think_set"),
    ("reader effort low", "think_set"),
    ("set the decider's thinking to max", "think_set"),
    ("think less", "think_set"),
    ("turn the reader's thinking down", "think_set"),
    ("how hard is it thinking", "think_show"),
    ("why did it pass on NWL", "ticker"),
    ("what happened with hls today", "ticker"),
    ("tell me about REG", "ticker"),
    ("why did you pass on BHP?", "ticker"),
    ("NWL", "ticker"),
    ("did it trade ALL", "ticker"),
    ("show me the positions", "positions"),
    ("what are we holding", "positions"),
    ("anything open?", "positions"),
    ("Hey, could you please show me the positions?", "positions"),
    ("is it running", "status"),
    ("status", "status"),
    ("are you there?", "status"),
    ("start a new conversation", "new"),
    ("start over", "new"),
    ("reset the chat", "new"),
    ("undo the last change", "undo"),
    ("roll back that change", "undo"),
    ("put it back how it was", "undo"),
    ("what changes have I asked for", "changes"),
    ("any change requests pending", "changes"),
    ("help", "help"),
    ("what can you do", "help"),
    ("answer my messages one at a time", "queue_set"),
    ("what happens if I message you while you're busy", "queue_show"),
    ("when does it start", "hours"),
    ("what are the trading hours", "hours"),
    ("how much are we up", "pnl"),
    ("what's the P&L", "pnl"),
    ("what's running", "tasks"),
    ("what's my telegram id", "whoami"),
    ("close the NWL position", "order_request"),
    ("sell everything", "order_request"),
    ("open positions", "positions"),
    ("trade list", "trades"),
    ("stop trading for now", "pause"),
    ("i want the decider to think harder", "think_set"),
    ("when is the evening report", "hours"),
    ("how did it go last friday", "today"),
    # not the chat's own: a change request, or a conversation with the decider
    ("can you make it show the cash first", None),
    ("I want it to change how it sizes trades", None),
    ("what do you make of NWL now?", None),
    ("stop trading microcaps", None),
    ("how many positions can it hold", None),
    ("why did it pass on all", None),  # "all" is a word here, ALL the stock only in capitals
    ("what's the plan for tomorrow", None),
    ("thanks", None),
    ("how are you", None),
    # a remark that mentions a model or a level is never a strategy change
    ("the decider used opus yesterday and passed on everything", None),
    ("the decider thought the high was fine and passed", None),
    ("was it opus that passed on NWL this morning", "model_show"),
    # 26 Sep 2026, a review of the chat (tests/test_chat_review.py). F1: a question, a
    # negation or a hedge only shows the models; "six words or fewer" is no instruction.
    ("dont switch the decider to sonnet", "model_show"),
    ("sonnet for the reader?", "model_show"),
    ("maybe opus for the reader", "model_show"),
    ("use sonnet for the reader tomorrow", "model_show"),
    ("the decider stays on opus", "model_show"),
    ("decider think harder about exits", None),
    ("less thinking more trading", None),
    ("opus 5.5 passed on everything", None),
    ("I think we are up today", "pnl"),
    ("decider think harder", "think_set"),
    ("reader think less", "think_set"),
    ("put it back to opus", "model_set"),  # read back, and refused mid-test unless confirmed
    # F4: Rick's "no new entries today"
    ("can you make it stop trading today", "pause"),
    ("no new entries today", "pause"),
    ("stop buying for today", "pause"),
    ("halt entries", "pause"),
    ("kill switch", "pause"),
    ("shut it down", "pause"),
    ("switch it off", "pause"),
    ("emergency stop", "pause"),
    ("fuck it stop everything", "pause"),
    ("stop everything", "pause"),
    ("dont open any more positions today", "pause"),
    ("dont trade today", "pause"),
    ("no more trades today", "pause"),
    ("call it a day", "pause"),
    ("stop the watcher", "pause"),
    ("resume trading", "resume"),
    ("start trading again", "resume"),
    ("allow entries again", "resume"),
    ("stop it", "stop"),
    ("stop the answer", "stop"),
    # F5: ordinary words are not stock codes
    ("what happened today pls", "today"),
    ("wow", None),
    ("omg", None),
    ("bet", None),
    # F6: quick spellings and swearing
    ("whats running", "tasks"),
    ("whats the status", "status"),
    ("hows it goin", "today"),
    ("hows the day trader going", "today"),
    ("watcher up?", "status"),
    ("wat happened today", "today"),
    ("ffs how did it go today", "today"),
    # F7: money in Rick's words
    ("how much did we make today", "pnl"),
    ("did we lose money", "pnl"),
    ("what's the damage", "pnl"),
    ("what's the total", "pnl"),
    ("bot vs agent", "pnl"),
    ("is the agent beating the bot", "pnl"),
    ("who's winning", "pnl"),
    # F8: IB Gateway and the prices
    ("is ibkr connected", "feed"),
    ("is the gateway up", "feed"),
    ("gateway should be back", "feed"),
    ("is the data live", "feed"),
    ("is it on ibkr or yahoo", "feed"),
    ("has the gateway crashed", "feed"),
    ("are we on live prices", "feed"),
    # F11: the week, and "c'mon" is not Monday
    ("c'mon why no trades today", "trades"),
    ("how did we go this week", "week"),
    ("how did we go last week", "week"),
    # F12: misroutes
    ("open a new chat", "new"),
    ("short answer please", None),
    ("short version", None),
    ("cover the basics", None),
    ("double check the positions", "positions"),
    ("add to the list", None),
    ("exit", None),
    ("would you buy BHP", None),
    ("sorry to interrupt", None),
    ("i didnt mean to interrupt", None),
    ("take it one at a time", None),
    ("interrupt mode", "queue_set"),
    ("what changes did it make to the stops", None),
    ("reduce the position size", None),
    ("did the evening report go out", "report_sent"),
    ("has the report been sent", "report_sent"),
    ("i didnt get the evening report", "report_sent"),
    ("is the market open", "hours"),
    ("is today a trading day", "hours"),
    ("restart the watcher", "watcher_restart"),
    ("can you restart the watcher", "watcher_restart"),
    ("sell BHP", "order_request"),
    ("could you close NWL", "order_request"),
]


@pytest.mark.parametrize("text,want", PHRASES, ids=[p[0] for p in PHRASES])
def test_every_phrasing_is_understood(text, want):
    it = plain.understand(text, KNOWN)
    assert (it.name if it else None) == want


def test_the_words_carry_their_details():
    assert plain.understand("use opus for the decider", KNOWN).args == {
        "agent": "decider", "model": "opus"}  # fmt: skip
    assert plain.understand("switch the reader to sonnet 5", KNOWN).args == {
        "agent": "reader", "model": "sonnet-5"}  # fmt: skip
    assert plain.understand("put the decider on opus 5.5", KNOWN).model == "opus-5-5"
    assert plain.understand("decider back to normal", KNOWN).model == "default"
    assert plain.understand("use sonnet", KNOWN).agent is None
    assert plain.understand("both on sonnet", KNOWN).agent == "both"
    assert plain.understand("make the decider think harder", KNOWN).args == {
        "agent": "decider", "level": "up"}  # fmt: skip
    assert plain.understand("reader effort low", KNOWN).level == "low"
    assert plain.understand("set the decider's thinking to max", KNOWN).level == "max"
    assert plain.understand("turn the reader's thinking down", KNOWN).level == "down"
    assert plain.understand("stop it for today", KNOWN).stop_answer is True
    assert plain.understand("no new entries today", KNOWN).stop_answer is False
    assert plain.understand("stop the watcher", KNOWN).watcher is True
    assert plain.understand("stop", KNOWN).trading is False
    assert plain.understand("reset the chat", KNOWN).reset is True
    assert plain.understand("answer my messages one at a time", KNOWN).mode == "followup"
    assert plain.understand("bundle my messages", KNOWN).mode == "collect"
    assert plain.understand("why did it pass on NWL", KNOWN).tickers == ["NWL"]
    assert plain.understand("what happened with hls and reg", KNOWN).tickers == ["HLS", "REG"]
    assert plain.answer_agent("the decider") == "decider"
    assert plain.answer_agent("both of them") == "both"
    assert plain.answer_agent("what?") is None
    assert plain.step_level("medium", "up") == "high"
    assert plain.step_level("high", "down") == "medium"
    assert plain.step_level("max", "up") == "max"
    assert plain.step_level("adaptive", "up") is None
    assert plain.normalise("Hey, could you please show me the positions?") == \
        "show me the positions"  # fmt: skip
    assert T.named_day("what happened yesterday", DAY) == date(2026, 9, 24)
    assert T.named_day("how did it go on wednesday", DAY) == date(2026, 9, 23)
    assert T.named_day("how did it go last friday", DAY) == date(2026, 9, 18)
    assert T.named_day("how did it go on friday", DAY) is None  # a Friday's Friday is today
    assert T.named_day("how did it go", DAY) is None
    assert T.named_day("what happened yesterday", date(2026, 9, 28)) == DAY  # Monday: Friday
    assert T.named_day("c'mon why no trades today", DAY) is None  # not Monday
    assert T.named_day("wed be up if it had traded", DAY) is None  # "we'd", not Wednesday
    assert T.named_day("how did it go on wed", DAY) == date(2026, 9, 23)
    assert plain.normalise("whats running") == "what's running"
    assert plain.normalise("i didnt get the report") == "i didn't get the report"
    assert plain.normalise("fuck it stop everything") == "stop everything"
    assert plain.yes_no("ok do it") == "yes" and plain.yes_no("nah") == "no"
    assert plain.yes_no("yes but why") is None


def test_a_slash_command_is_never_read_as_plain_words():
    assert plain.understand("/status", KNOWN) is None
    assert plain.understand("", KNOWN) is None


# ------------------------------------------------------------------ one day's records


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


@pytest.fixture
def records(env):  # noqa: F811 - the imported fixture, by name
    """A day like 25 Sep 2026 in the temporary data folder: NWL's news, screen, reader, a
    pre-open PASS, a quiet reaction look, the rule bot's short and its fills; the day
    trader's REG (agent took it) and CWY (agent rejected); one order the limits refused."""
    data = env.app.cfg_loader().data_dir
    ev = data / "events"
    udir = data / "universe"
    udir.mkdir(parents=True, exist_ok=True)
    (udir / "asx_directory.csv").write_text(
        "code,name,industry,listing_date,market_cap,source\n"
        + "".join(f"{c},{c} Ltd,x,2020-01-01,1,test\n" for c in sorted(KNOWN)), encoding="utf-8")
    _jsonl(ev / "arena_alerts.jsonl", [
        {"ts": "2026-09-24T22:18:30+00:00", "kind": "arena_alerts", "ticker": "NWL",
         "ids_id": "03143747", "headline": "Netwealth to defend class action",
         "price_sensitive": True, "released_at": "2026-09-25T08:18:00"},
    ])
    _jsonl(ev / "arena_screened.jsonl", [
        {"ts": "2026-09-24T22:20:00+00:00", "kind": "arena_screened", "ticker": "NWL",
         "ids_id": "03143747", "headline": "Netwealth to defend class action", "ok": True,
         "test": "", "why": "tradeable: $14,850,880 median turnover, tick 0.05% of price"},
        {"ts": "2026-09-24T22:20:01+00:00", "kind": "arena_screened", "ticker": "HLS",
         "ids_id": "03143800", "headline": "Guidance update", "ok": False, "test": "turnover",
         "why": "a $5,000 order is more than 5% of its median daily turnover $14,149"},
    ])
    _jsonl(ev / "arena_decisions.jsonl", [
        {"ts": "2026-09-24T22:20:30+00:00", "kind": "arena_decisions", "stage": "reader",
         "ticker": "NWL", "ids_id": "03143747", "model": "claude-sonnet-5",
         "trade_worthy": "True", "why": "reader says trade-worthy", "can_size_and_exit": "True",
         "can_size_why": "reader says it can be sized and exited", "summary": "..."},
        {"ts": "2026-09-24T22:21:13+00:00", "kind": "arena_decisions", "stage": "decider",
         "ticker": "NWL", "ids_id": "03143747", "model": "claude-opus-5-5", "v2": "pre_open",
         "decision": {"action": "pass", "why": "the market has had four days to price it; "
                      "deciding after ten minutes of trading beats a blind auction fill"}},
        {"ts": "2026-09-25T05:55:00+00:00", "kind": "arena_decisions", "stage": "preclose",
         "ticker": "NWL", "action": "close", "reason": "flat at the close: v2 holds intraday "
         "only (the rule exits at 15:55)", "model": "code", "level": 1,
         "account": "asx_announcements_v2__bot"},
    ])
    _jsonl(ev / "v2_reaction.jsonl", [
        {"ts": "2026-09-25T00:16:53+00:00", "kind": "v2_reaction", "ticker": "NWL",
         "status": "quiet", "why": "no previous close in the minute cache", "ids": ["03143747"],
         "headlines": ["Netwealth to defend class action"],
         "reaction": {"available": False, "why": "no previous close in the minute cache"}},
    ])
    _jsonl(ev / "arena_orders.jsonl", [
        {"ts": "2026-09-25T00:53:52+00:00", "kind": "arena_orders", "account":
         "asx_announcements_v2__bot", "playbook": "asx_announcements_v2", "level": 1,
         "ticker": "NWL", "side": "short", "qty": 285, "limit": 17.51, "stop": 18.34,
         "placed_by": "bot", "model": "v2 rule", "outcome": "accepted", "order_id": "ARN-000004"},
        {"ts": "2026-09-25T00:53:52+00:00", "kind": "arena_orders", "order_id": "ARN-000004",
         "account": "asx_announcements_v2__bot", "ticker": "NWL", "side": "short", "qty": 285,
         "limit": 17.51, "decided_at": "2026-09-25T10:53:52+10:00", "status": "pending_fill",
         "stop": 18.34, "reason": "v2 rule: -3.15% vs the ASX 200 at 10:30 on 6.1x usual "
         "first-30-minute volume", "model": "v2 rule", "placed_by": "bot", "event": "submitted"},
        {"ts": "2026-09-25T01:30:00+00:00", "kind": "arena_orders", "account":
         "asx_daytrader_v1__agent", "playbook": "asx_daytrader", "level": 1, "ticker": "HLS",
         "side": "buy", "qty": 100, "limit": 0.4, "stop": 0.3, "placed_by": "agent",
         "model": "claude-opus-5-5", "outcome": "refused",
         "reason": "risk 6.0% of equity is over the 5.0% limit"},
    ])
    _jsonl(ev / "arena_fills.jsonl", [
        {"ts": "2026-09-25T01:17:52+00:00", "kind": "arena_fills", "order_id": "ARN-000004",
         "account": "asx_announcements_v2__bot", "ticker": "NWL", "side": "short", "qty": 285,
         "limit": 17.51, "decided_at": "2026-09-25T10:53:52+10:00", "status": "filled",
         "filled_qty": 285, "avg_price": 17.8656, "commission": 6.6,
         "fill_minute": "2026-09-25T10:54+10:00", "stop": 18.34, "reason": "v2 rule",
         "model": "v2 rule", "placed_by": "bot", "realised": 0.0, "event": "filled"},
        {"ts": "2026-09-25T06:18:00+00:00", "kind": "arena_fills", "order_id": "ARN-000018",
         "account": "asx_announcements_v2__bot", "ticker": "NWL", "side": "cover", "qty": 285,
         "limit": 17.242, "decided_at": "2026-09-25T15:55:52+10:00", "status": "filled",
         "filled_qty": 285, "avg_price": 16.987, "commission": 6.6,
         "fill_minute": "2026-09-25T15:56+10:00", "reason": "flat at the close",
         "model": "code (pre-close sweep: flat)", "placed_by": "bot", "realised": 250.41,
         "event": "filled"},
    ])
    arena = data / "arena"
    (arena / "daytrader").mkdir(parents=True, exist_ok=True)
    (arena / "daytrader" / "2026-09-25.json").write_text(json.dumps({
        "fired": [], "last_eval": {}, "universe": ["REG", "CWY", "NWL", "EOS"],
        "signals": [
            {"at": "2026-09-25T11:27:59+10:00", "ticker": "REG", "setup": "vwap_reclaim",
             "side": "short", "last": 4.535, "stop": 4.565, "why": "down -1.5% vs index, VWAP "
             "falling, closed back below it at 4.535", "bot": {"skipped": "the account is "
             "full (3 open or working)"}, "agent": {"order_id": "ARN-000010", "qty": 1111,
             "limit": 4.48, "stop": 4.57, "seconds": 23.7}},
            {"at": "2026-09-25T11:24:26+10:00", "ticker": "CWY", "setup":
             "opening_range_breakout", "side": "short", "last": 2.605, "stop": 2.6325,
             "why": "closed 2.605 below the 30-min low 2.625", "bot": {"order_id":
             "ARN-000009", "qty": 1428, "limit": 2.57, "stop": 2.64}, "agent": {"rejected":
             "one block moved the price half a cent, no follow-through", "seconds": 21.7}},
        ]}), encoding="utf-8")  # fmt: skip
    (arena / "v2bot").mkdir(parents=True, exist_ok=True)
    (arena / "v2bot" / "2026-09-25.json").write_text(json.dumps({
        "status": "done", "decided_at": "2026-09-25T10:53:55+10:00", "data": "live data (IBKR)",
        "candidates": [{"ticker": "NWL", "signal": True, "why": "-3.15% vs the ASX 200 at "
                        "10:30 on 6.1x usual first-30-minute volume"}],
        "orders": [{"ticker": "NWL", "order_id": "ARN-000004", "side": "short", "qty": 285,
                    "limit": 17.51, "stop": 18.34}]}), encoding="utf-8")  # fmt: skip
    (arena / "reaction").mkdir(parents=True, exist_ok=True)
    (arena / "reaction" / "2026-09-25.json").write_text(json.dumps({
        "NWL": {"ticker": "NWL", "status": "quiet", "ids": ["03143747"],
                "headlines": ["Netwealth to defend class action"],
                "why": "no previous close in the minute cache"}}), encoding="utf-8")  # fmt: skip
    env.app.clock = lambda: NOW
    return env


def _reply(rec, text: str) -> str:
    n = len(rec.tg.sent)
    assert rec.app.on_update(msg(text)) == "handled"
    chat.wait_idle(rec.app, 10)
    assert len(rec.tg.sent) > n, f"no reply to {text!r}"
    return rec.tg.texts()[-1]


def test_how_is_it_going_is_answered_from_the_records(records):
    text = _reply(records, "how's it going today")
    assert records.decider.calls == []
    assert text.startswith("Fri 25 Sep, today - the arena, FAKE money.")
    assert "Watcher: " in text
    assert "1 seen, 1 screened out (turnover 1), 1 read, 1 reached the decider (0 traded, " \
           "1 passed); reaction looks: 1 quiet; 10:30 rule bot: 1 order (NWL short 285)" in text
    assert "Day trader: 2 setups in 4 stocks (vwap_reclaim 1, opening_range_breakout 1); " \
           "agent took 1 (REG), rejected 1, not asked 0; rule bot took 1 (CWY)" in text
    assert "Orders: 1 placed (0 by the agent, 1 by the rule bots, 0 by code" in text
    assert "1 refused by the limits; 2 fills." in text
    assert "refused: buy HLS - risk 6.0% of equity is over the 5.0% limit" in text
    # the fill is timed by its bar (10:54), not by the minute the broker wrote it (11:17)
    assert "10:54 BOT short 285 NWL @ 17.8656 (fee 6.60) [ASX announcements v2 bot]" in text
    assert "15:56 SWEEP cover 285 NWL @ 16.987 (fee 6.60), result +250.41 before fees" in text
    assert "ASX announcements v2: AGENT $20,000.00 (+0.00 today, +0.00 since the start); " \
           "BOT $20,000.00" in text
    assert text.endswith("Open positions: none.")


def test_what_did_it_trade_lists_fills_and_refusals(records):
    text = _reply(records, "what did it trade")
    assert "Fills (2):" in text and "result +250.41 before fees" in text
    assert "Refused by the limits (1):" in text
    assert "AGENT buy 100 HLS - risk 6.0% of equity is over the 5.0% limit" in text
    assert records.decider.calls == []
    text = _reply(records, "how much are we up")
    assert text.splitlines()[1].startswith("ASX announcements v2: AGENT $20,000.00")
    # a past day is read from its closing mark, and says so when there is none
    text = _reply(records, "how did it go yesterday")
    assert text.startswith("Thu 24 Sep - the arena, FAKE money.")
    assert "Day trader: no record for the day." in text
    assert "AGENT $20,000.00 (no mark for that day, +0.00 since the start)" in text
    assert "Watcher:" not in text


def test_why_did_it_pass_is_the_stocks_day_from_the_records(records):
    text = _reply(records, "why did it pass on NWL")
    assert records.decider.calls == []
    lines = text.splitlines()
    assert lines[0] == "NWL on Fri 25 Sep, today, from the arena's records:"
    assert lines[1] == "08:18 news: Netwealth to defend class action (price-sensitive)"
    assert lines[2].startswith("08:20 screen: tradeable")
    assert lines[3].startswith("08:20 reader: trade-worthy YES")
    assert lines[4] == ("08:21 decider (pre-open look): PASS - the market has had four days to "
                        "price it; deciding after ten minutes of trading beats a blind auction "
                        "fill [prices: not recorded]")
    assert "10:16 reaction look: quiet - no previous close in the minute cache" in text
    assert "10:53 10:30 rule bot: signal - -3.15% vs the ASX 200 at 10:30" in text
    assert "10:54 filled: BOT short 285 NWL @ 17.8656" in text
    assert "15:55 pre-close sweep: close - flat at the close" in text
    assert "15:56 filled: SWEEP cover 285 NWL @ 16.987 (fee 6.60), result +250.41" in text
    assert [x[:5] for x in lines[1:]] == sorted(x[:5] for x in lines[1:])  # time order

    text = _reply(records, "what happened with reg today")  # lower case, a real code
    assert text.startswith("REG on Fri 25 Sep, today")
    assert ("11:27 day trader setup, vwap_reclaim short: down -1.5% vs index, VWAP falling, "
            "closed back below it at 4.535; rule bot skipped - the account is full (3 open or "
            "working); agent took it, ARN-000010, 1,111 @ 4.48, stop 4.57") in text
    text = _reply(records, "tell me about CWY")
    assert "agent REJECTED - one block moved the price half a cent, no follow-through" in text
    assert "rule bot ordered ARN-000009, 1,428 @ 2.57, stop 2.64" in text

    text = _reply(records, "why did it pass on BHP")  # a real code with nothing on record
    assert text == ("Nothing on record for BHP on Fri 25 Sep, today: no announcement of its "
                    "reached the arena (so the screen, the reader and the decider never saw "
                    "it), it is not in the day trader's universe, and no order names it.")
    text = _reply(records, "anything on EOS?")
    assert "the day trader scanned it and found no setup" in text


def test_a_question_for_the_decider_carries_the_records(records):
    records.app.on_update(msg("what do you make of NWL now?"))
    chat.wait_idle(records.app, 10)
    (message, key), = records.decider.calls
    assert message.endswith("Rick's message:\nwhat do you make of NWL now?")
    assert "FACTS ON RECORD:" in message and "Fri 25 Sep, today - the arena" in message
    assert "\nNWL:\n" in message
    assert "08:21 decider (pre-open look): PASS - the market has had four days" in message
    assert "say \"not on record\"" in message
    assert "Never tell him to type a command" in message and "/change" not in message
    records.app.on_update(msg("what's your favourite colour?"))
    chat.wait_idle(records.app, 10)
    assert "FACTS ON RECORD" not in records.decider.calls[-1][0]


def test_stop_for_today_is_the_pause_and_a_bare_stop_the_answer(records):
    # 17:00, after the close: nothing more enters today anyway, and nothing is set
    text = _reply(records, "stop it for today")
    assert text.startswith("The market has closed for today (4:10pm)")
    assert records.tg.sent[-1][1] is None
    # in the session it is a read-back with buttons (tests/test_chat_review.py taps them)
    records.app.clock = lambda: NOW.replace(hour=11)
    text = _reply(records, "stop it for today")
    assert text.startswith("Stop NEW entries for the rest of today, for both playbooks")
    assert [b[0] for b in records.tg.sent[-1][1]] == ["Pause new entries", "Cancel"]
    assert _reply(records, "stop") == "Nothing was running, so there was nothing to stop."
    assert records.decider.calls == []


def test_an_order_request_is_refused_in_code(records):
    assert _reply(records, "close the NWL position") == chat.NO_ORDERS
    assert _reply(records, "sell everything") == chat.NO_ORDERS
    assert records.decider.calls == []


def _agents(r: Path) -> dict:
    import yaml

    return yaml.safe_load((r / "config.yaml").read_text(encoding="utf-8"))["arena"]["agents"]


def _tap(rec, which: int = 0) -> str:
    """Tap a button of the last message that had buttons (0: go ahead, 1: cancel)."""
    from test_chat import tap

    data = next(b for _, b in reversed(rec.tg.sent) if b)[which][1]
    n = len(rec.tg.sent)
    assert rec.app.on_update(tap(data)) == "button"
    assert len(rec.tg.sent) > n, f"no reply to the tap on {data}"
    return rec.tg.texts()[-1]


def test_a_model_change_in_plain_words_is_read_back_and_the_test_freeze_named(records):
    """26 Sep 2026: a plain-words change is never applied at once. Mid-test (config.yaml's
    playbooks are in their 10-day test) the read-back says it breaks the frozen test."""
    text = _reply(records, "use sonnet for the decider")
    assert text.startswith("Switching the decider's model from Opus 5.5 to Sonnet 5 now would "
                           "break the 10-day test of ASX announcements v2 and ASX day trader "
                           "(Fri 25 Sep to Thu 08 Oct)")
    assert [b[0] for b in records.tg.sent[-1][1]] == ["Change it anyway", "Cancel"]
    assert _agents(records.repo)["history"] == [] and records.run.sets() == []
    text = _tap(records)
    assert "decider model set to Sonnet 5" in text and "was Opus 5.5" in text
    assert "Recorded as a dated strategy change in config.yaml (commit " in text
    ag = _agents(records.repo)
    assert ag["models"]["decider"] == "anthropic/claude-sonnet-5"
    assert ag["history"][-1]["by"] == ("Rick, plain words in the Trader chat, confirmed on a "
                                       "read-back; breaks the frozen 10-day test")
    assert "breaks the frozen playbook test (to 08 Oct)" in ag["history"][-1]["note"]
    assert json.loads(records.oc.read_text())["agents"]["list"][2]["model"] == \
        "anthropic/claude-sonnet-5"  # fmt: skip

    # no agent named: the chat asks, the next message answers it, and it is read back
    assert _reply(records, "use opus 5.5") == chat.WHICH_AGENT
    text = _reply(records, "the reader")
    assert "the reader's model from Sonnet 5 to Opus 5.5" in text
    text = _tap(records, 1)  # Cancel
    assert text == "Cancelled. Nothing was changed."
    assert _agents(records.repo)["models"]["reader"] == "anthropic/claude-sonnet-5"
    # "normal" is what config.yaml expects now - the change just recorded - as /model default
    assert _reply(records, "put the decider back to normal") == \
        "The Trader's decider already uses Sonnet 5."  # fmt: skip
    assert records.decider.calls == []


def test_think_harder_steps_the_level_and_records_it(records):
    text = _reply(records, "make the reader think harder")  # medium -> high, read back
    assert "the reader's thinking from medium to high" in text
    text = _tap(records)
    assert "reader thinking level set to high" in text and "was medium" in text
    assert _agents(records.repo)["effort"]["reader"] == "high"
    _reply(records, "reader effort low")
    assert "reader thinking level set to low" in _tap(records)
    _reply(records, "decider think harder")
    assert "decider thinking level set to xhigh" in _tap(records)
    _reply(records, "turn the reader's thinking down")
    assert _tap(records).startswith("The Trader's reader thinking level set to minimal")
    assert _reply(records, "reader think less") == \
        "The reader is already at minimal, the bottom of the ladder."  # fmt: skip
    assert _reply(records, "think less") == chat.WHICH_AGENT
    assert "the decider's thinking from xhigh to high" in _reply(records, "decider")
    assert "decider thinking level set to high" in _tap(records)
    assert records.decider.calls == []


def test_showing_models_and_thinking_never_names_a_command(records):
    text = _reply(records, "what model is it on")
    assert "decider (decides the trades, and answers you here): Opus 5.5, thinking high" in text
    assert "Models allowed here: Opus 5.5, Sonnet 5, Opus 4.8." in text
    assert "say for example \"use Sonnet 5 for the reader\"" in text
    text = _reply(records, "how hard is it thinking")
    assert "reader (reads each announcement): Sonnet 5, thinking medium" in text
    assert "Thinking levels, lowest to highest: minimal, low, medium, high, xhigh, max." in text


def test_the_rest_of_the_commands_from_plain_words(records):
    assert _reply(records, "start over").startswith("Fresh start. The decider starts a new "
                                                    "conversation with you (#2)")
    assert any("sessions.reset" in c for c in records.run.calls)
    assert _reply(records, "what changes have I asked for").startswith("No change requests yet.")
    assert _reply(records, "undo the last change") == \
        "There's no finished change to undo on the Trader."  # fmt: skip
    text = _reply(records, "what can you do")
    assert text.startswith("The Trader chat: talk to the decider")
    assert "\"how's it going today\"" in text and "/change" in text
    text = _reply(records, "is it running")
    assert text.startswith("The Trader\nWatcher: ")
    assert "Conversation: #2 (say \"start over\" for a fresh one)" in text
    text = _reply(records, "when does it start")
    assert text.startswith("Fri 25 Sep: an ASX trading day.")
    assert "the watcher starts itself at 7:30am and stops at " in text  # hours.py says when
    assert "Right now (17:00) the market is closed" in text
    assert _reply(records, "answer my messages one at a time") == "Queue mode set to followup."
    text = _reply(records, "what happens if I message you while you're busy")
    assert text.startswith("While I'm busy with an answer, a new message waits its turn")
    assert _reply(records, "show me the positions").startswith("Arena positions (FAKE money):")
    assert _reply(records, "what's running") == "Nothing running."
    assert _reply(records, "what's my telegram id") == f"Your Telegram id: {RICK}"
    assert records.decider.calls == []


def test_a_change_request_still_wins_over_a_look_alike(records, monkeypatch):
    seen = []
    monkeypatch.setattr(botctl, "interpret", lambda ad, words, answer=None: seen.append(words)
                        or {"is_change": True, "runtime_hint": None, "summary": "Show "
                            "positions first.", "question": None, "limits": [],
                            "off_limits": None})  # fmt: skip
    records.app.on_update(msg("can you make it show the positions first"))
    chat.wait_idle(records.app, 10)
    assert seen == ["can you make it show the positions first"]
    assert not records.tg.texts()[-1].startswith("Arena positions")
    assert [b[0] for b in records.tg.sent[-1][1]] == ["Build it", "Cancel"]
    # ... but a setting in the same shape is read back on the spot, not offered as a build
    text = _reply(records, "can you make the decider think harder")
    assert "the decider's thinking from high to xhigh" in text and len(seen) == 1
    assert [b[0] for b in records.tg.sent[-1][1]] == ["Change it anyway", "Cancel"]
    assert records.decider.calls == []


def test_no_reply_tells_rick_to_type_a_command(records):
    import re

    asked = ["how's it going today", "what did it trade", "why did it pass on NWL",
             "show me the positions", "how much are we up", "is it running", "stop it for today",
             "what model is it on", "how hard is it thinking", "when does it start",
             "what changes have I asked for", "undo the last change", "start over",
             "what happens if I message you while you're busy", "close the NWL position",
             "use opus 5.5", "the decider", "/frobnicate"]  # fmt: skip
    for text in asked:
        reply = _reply(records, text)
        assert not re.search(r"(?<![\w.])/[a-z]", reply), f"{text!r} -> {reply!r}"


def test_an_order_for_an_unknown_code_is_still_refused_in_code():
    # 26 Sep 2026: "sell BHP" with no directory loaded went to the decider
    assert plain.understand("sell BHP", set(), []).name == "order_request"
    reply = plain.understand("short answer please", set(), [])
    assert reply is None or reply.name != "order_request"
