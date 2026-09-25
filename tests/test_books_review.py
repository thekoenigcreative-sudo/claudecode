"""The 26 Sep 2026 review of the books, the broker and the agent calls. No network, and no
model is called: OpenClaw is a stand-in process object, and every bar is written to a
temporary minute cache.

G1/G8  a failed agent call is recorded, classified and counted apart from a rejection
G2     a fresh OpenClaw session per call when config asks for it
E6     a timed-out call stops the whole process tree and cannot wait forever
B2/D2  the flat sweep takes the whole position out: a half-off or a target cannot keep it
D3     an exit is never refused by the minimum-order or runaway guards
pause  Rick's "no new entries today" switch refuses openings only
B6     a later slice of an entry does not undo a stop management has moved
D4     trades are round trips, won after every cost
D5     the report counts the Sydney day, not the UTC date
D8     with live bars an expired entry is done a minute after its good-till
D10    the 16:10 summary counts openings, with exits apart
D11    the day trader's stale setups are counted
D13    a position with no trade that day is marked at its last traded close, labelled
D15    each fill slice says which feed priced it
H5     no decisions is not "delayed data"
H13    deferrals are not rejections (tally)
G10    every order record names the code that made it
"""

import json
import re
import subprocess
import sys
import time as time_mod
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from asxbot.arena import agents as A
from asxbot.arena import broker as B
from asxbot.arena import v2_flow
from asxbot.arena.accounts import ArenaOrder, Position
from asxbot.arena.broker import ArenaBroker
from asxbot.arena.levels import load_playbook
from asxbot.arena.minutes import MinuteBars
from asxbot.arena.orders import ArenaOrderRefused, arena_place_order
from asxbot.arena.runtime import Arena
from asxbot.backtest.costs import CostModel
from asxbot.config import load_config
from asxbot.io import write_parquet_atomic

SYD = ZoneInfo("Australia/Sydney")
DAY = date(2026, 1, 8)  # a Thursday, far enough back that nothing reaches for the network


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def at(h, m, s=0, day=DAY):
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=SYD)


def put(mb, code, day, rows):
    """rows: {(h, m): (open, high, low, close, volume)}"""
    idx = pd.DatetimeIndex([at(h, m, day=day) for (h, m) in rows])
    df = pd.DataFrame(list(rows.values()), index=idx,
                      columns=["open", "high", "low", "close", "volume"])  # fmt: skip
    write_parquet_atomic(df, mb._path(code, day))


def session(rows_from=(10, 31), until=(16, 0), px=1.0, vol=1000.0):
    rows, t = {}, at(*rows_from)
    while (t.hour, t.minute) < until:
        rows[(t.hour, t.minute)] = (px, px, px, px, vol)
        t += timedelta(minutes=1)
    return rows


@pytest.fixture
def cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )


@pytest.fixture
def mb(cfg):
    return MinuteBars(cfg.data_dir, "close")


@pytest.fixture
def broker(cfg, mb):
    b = ArenaBroker(
        cfg.data_dir, CostModel.from_config(cfg), mb, lambda t: 2_000_000.0,
        resolve_after_minutes=22, clock=Clock(at(10, 0)), opening_auction="first_minute",
    )  # fmt: skip
    return b


@pytest.fixture
def arena(cfg, broker, monkeypatch):
    import asxbot.data.universe as U

    monkeypatch.setattr(U, "fetch_directory", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    return Arena(cfg, broker, broker.store, {"DTX", "AAA"}, {"DTX", "AAA"})


def events(cfg, kind, rows):
    """Write event records with the given UTC times, as the event log would have."""
    p = cfg.data_dir / "events" / f"{kind}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        for ts, rec in rows:
            fh.write(json.dumps({"ts": ts.astimezone(UTC).isoformat(), "kind": kind, **rec}))
            fh.write("\n")


# --------------------------------------------------------------------------
# G1 / G8 / G2 / E6: the agent call
# --------------------------------------------------------------------------
class FakePopen:
    def __init__(self, out="", err="", code=0, hang=False):
        self.out, self.err, self.code, self.hang = out, err, code, hang
        self.pid, self.returncode, self.killed, self.waits = 4242, None, False, []
        self.stdout = self.stderr = None

    def communicate(self, timeout=None):
        self.waits.append(timeout)
        if self.hang and len(self.waits) == 1:
            raise subprocess.TimeoutExpired("openclaw", timeout)
        self.returncode = self.code
        return self.out, self.err

    def kill(self):
        self.killed = True


def body(text="", status="ok", **extra):
    return json.dumps({
        "status": status, "runId": "run-1", **extra,
        "result": {"payloads": [{"text": text}] if text else [],
                   "meta": {"durationMs": 1200,
                            "executionTrace": {"winnerModel": "claude-opus-5-5"},
                            "requestShaping": {"thinking": "high"}}},
    })  # fmt: skip


@pytest.fixture
def openclaw(monkeypatch):
    """Every call_agent in the test runs a FakePopen; `seen` holds the commands."""
    state = {"proc": FakePopen(body("ok")), "seen": [], "taskkill": []}

    def popen(cmd, **kw):
        state["seen"].append(list(cmd))
        return state["proc"]

    def run(cmd, **kw):
        state["taskkill"].append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(A, "_openclaw_bin", lambda: "openclaw")
    monkeypatch.setattr(A.hidden, "popen", popen)
    monkeypatch.setattr(A.hidden, "run", run)
    return state


def calls(cfg):
    p = cfg.data_dir / "events" / "arena_agent_calls.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def test_a_refused_call_is_a_usage_limit_failure_recorded_and_logged(cfg, openclaw, caplog):
    """Until 26 Sep a reply OpenClaw did not mark ok came back as an answer, and only
    successful calls were recorded: the self-checks never saw a failure."""
    openclaw["proc"] = FakePopen(body(status="error",
                                      error="You've hit your weekly usage limit. Resets Mon"))
    with pytest.raises(A.AgentCallFailed) as e, caplog.at_level("ERROR"):
        A.call_agent("trader-decider", "hi", data_dir=cfg.data_dir, purpose="day trader X")
    assert e.value.kind == "usage_limit" and "usage limit" in str(e.value)
    (rec,) = calls(cfg)
    assert rec["ok"] is False and rec["kind"] == "usage_limit" and rec["purpose"] == "day trader X"
    assert "AGENT UNAVAILABLE (usage limit)" in caplog.text


def test_a_reply_that_is_only_the_limit_notice_is_a_failure(cfg, openclaw):
    openclaw["proc"] = FakePopen(body("You've hit your limit · resets 5pm (Australia/Sydney)"))
    with pytest.raises(A.AgentCallFailed) as e:
        A.call_agent("trader-decider", "hi", data_dir=cfg.data_dir)
    assert e.value.kind == "usage_limit" and calls(cfg)[0]["ok"] is False


def test_a_real_answer_that_mentions_a_limit_is_an_answer(cfg, openclaw):
    text = 'the limit order is fine\n{"action": "take", "stop": 1.04, "why": "rate limit? no"}'
    openclaw["proc"] = FakePopen(body(text))
    reply = A.call_agent("trader-decider", "hi", data_dir=cfg.data_dir)
    assert reply.ok and reply.text == text
    (rec,) = calls(cfg)
    assert rec["ok"] is True and rec["model_ran"] == "claude-opus-5-5"


def test_a_timeout_stops_the_process_tree_and_is_recorded_as_a_timeout(cfg, openclaw):
    openclaw["proc"] = p = FakePopen(hang=True)
    with pytest.raises(A.AgentCallFailed) as e:
        A.call_agent("trader-decider", "hi", timeout_s=60, process_timeout_s=75,
                     data_dir=cfg.data_dir)  # fmt: skip
    assert e.value.kind == "timeout" and "75s" in str(e.value)
    assert p.killed and p.waits == [75, 10]  # the second wait is bounded
    if sys.platform == "win32":
        assert openclaw["taskkill"] == [["taskkill", "/T", "/F", "/PID", "4242"]]
    assert calls(cfg)[0]["kind"] == "timeout"


def test_an_exit_with_nothing_on_stdout_is_classified_from_stderr(cfg, openclaw):
    openclaw["proc"] = FakePopen("", "Error: 429 Too Many Requests", code=1)
    with pytest.raises(A.AgentCallFailed) as e:
        A.call_agent("trader-reader", "hi", data_dir=cfg.data_dir)
    assert e.value.kind == "usage_limit"


@pytest.mark.parametrize("text, kind", [
    ("Claude AI usage limit reached|1759000000", "usage_limit"),
    ("You've reached your session limit", "usage_limit"),
    ("rate_limit_error: slow down", "usage_limit"),
    ("request timed out after 60000ms", "timeout"),
    ("ECONNREFUSED 127.0.0.1:18789", "error"),
])  # fmt: skip
def test_failures_are_classified(text, kind):
    assert A.classify_failure(text) == kind


def test_a_hung_grandchild_cannot_hold_the_call_past_its_bound():
    """E6: openclaw.cmd is cmd.exe running node. subprocess.run killed only the first process
    on a timeout and then waited, without a timeout, on pipes the second still held."""
    child = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(40)']); "
        "time.sleep(40)"
    )
    started = time_mod.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        A._run_bounded([sys.executable, "-c", child], 2)
    assert time_mod.monotonic() - started < 25


def test_a_fresh_session_key_is_new_every_call_and_says_what_it_was_for(openclaw):
    k1 = A.fresh_session_key("day trader gap_and_go DTX", datetime(2026, 9, 28, 10, 5, tzinfo=SYD))
    k2 = A.fresh_session_key("day trader gap_and_go DTX", datetime(2026, 9, 28, 10, 5, tzinfo=SYD))
    assert re.fullmatch(r"arena-20260928-day-trader-gap-and-go-dtx-[0-9a-f]{8}", k1) and k1 != k2
    A.call_agent("trader-decider", "hi", fresh_session=True, purpose="v2 reaction look NEWS")
    cmd = openclaw["seen"][-1]
    key = cmd[cmd.index("--session-key") + 1]
    assert key.startswith("arena-") and "-v2-reaction-look-news-" in key
    A.call_agent("trader-decider", "hi")  # no key: OpenClaw's own session, as before
    assert "--session-key" not in openclaw["seen"][-1]
    A.call_agent("trader-decider", "hi", session_key="chat-7", fresh_session=True)
    assert "chat-7" in openclaw["seen"][-1]  # the chat's own key is never replaced


def test_fresh_sessions_follow_config(tmp_path):
    class Cfg:
        path = tmp_path / "config.yaml"

        def get(self, key, default=None):
            return default

    Cfg.path.write_text("arena:\n  agents:\n    fresh_session_per_call: false\n", "utf-8")
    assert A.session_for(Cfg(), "x") is None
    time_mod.sleep(0.05)
    Cfg.path.write_text("arena:\n  agents:\n    fresh_session_per_call: true\n", "utf-8")
    import os

    os.utime(Cfg.path, (time_mod.time() + 5, time_mod.time() + 5))  # a new mtime, re-read
    assert A.session_for(Cfg(), "x").startswith("arena-")


# --------------------------------------------------------------------------
# D3 and the pause: exits are never refused
# --------------------------------------------------------------------------
def _held(acct, ticker="AAA", qty=300, px=1.0, **kw):
    acct.positions[ticker] = Position(ticker=ticker, qty=qty, avg_cost=px,
                                      opened_at=at(10, 30).isoformat(timespec="minutes"),
                                      stop=0.9, opened_by="agent", **kw)  # fmt: skip


def test_a_leftover_under_the_minimum_order_is_still_swept(cfg, broker):
    """D3: $291 of stock left after a part fill was refused every cycle (min_order_aud 500)."""
    pb = load_playbook(cfg, "asx_daytrader")
    acct = broker.store.open("dt__agent", pb.key, "agent", 1, 20_000.0)
    _held(acct)
    broker.clock = Clock(at(15, 50))
    o = arena_place_order(cfg, broker, acct, pb, ticker="AAA", side="sell", qty=300,
                          limit=0.97, model="code (pre-close sweep: flat)")  # fmt: skip
    assert o.order_id and o.flat


def test_exits_pass_the_runaway_guard_and_openings_do_not(cfg, broker):
    pb = load_playbook(cfg, "asx_daytrader")
    acct = broker.store.open("dt__agent", pb.key, "agent", 1, 20_000.0)
    _held(acct)
    broker.clock = Clock(at(11, 0))
    for i in range(int((cfg.get("arena.guards") or {}).get("max_orders_per_day", 40))):
        acct.orders[f"X{i}"] = ArenaOrder(f"X{i}", acct.name, "ZZZ", "buy", 1, 1.0,
                                          at(10, 0).isoformat(), status="expired")  # fmt: skip
    arena_place_order(cfg, broker, acct, pb, ticker="AAA", side="sell", qty=300, limit=0.97)
    with pytest.raises(ArenaOrderRefused, match="runaway guard"):
        arena_place_order(cfg, broker, acct, pb, ticker="BBB", side="buy", qty=1000, limit=1.0,
                          stop=0.95)  # fmt: skip


def test_the_pause_refuses_every_opening_in_both_books_and_no_exit(cfg, broker):
    from asxbot.arena.pause import set_pause

    pb = load_playbook(cfg, "asx_daytrader")
    broker.clock = Clock(at(11, 0))
    set_pause(cfg.data_dir, at(9, 0), "Rick", "stop it for today")
    for kind in ("agent", "bot"):
        acct = broker.store.open(f"dt__{kind}", pb.key, kind, 1, 20_000.0)
        with pytest.raises(ArenaOrderRefused, match="entries paused for today by Rick"):
            arena_place_order(cfg, broker, acct, pb, ticker="BBB", side="buy", qty=1000,
                              limit=1.0, stop=0.95, placed_by=kind)  # fmt: skip
    _held(acct)
    assert arena_place_order(cfg, broker, acct, pb, ticker="AAA", side="sell", qty=300,
                             limit=0.97, placed_by="bot").order_id  # fmt: skip
    # The next day the file no longer applies.
    broker.clock = Clock(at(11, 0, day=date(2026, 1, 9)))
    assert arena_place_order(cfg, broker, acct, pb, ticker="BBB", side="buy", qty=1000,
                             limit=1.0, stop=0.95).order_id  # fmt: skip


# --------------------------------------------------------------------------
# B2 / D2: flat by the close means flat
# --------------------------------------------------------------------------
MANAGE = {"breakeven_at_r": 1.0, "half_at_r": 2.0, "trail_at_r": 2.0, "trail_distance_r": 1.0,
          "entry": 1.0, "r": 0.05, "best": 1.0}  # fmt: skip


def test_a_part_filled_half_off_does_not_keep_half_the_position_overnight(arena, cfg, mb):
    """The half-off at +2R filled 100 of 400; flatten sold only the other 400 of the 700 held,
    because the half-off's rest counted as "already closing", and the half-off kept working
    across sessions. Fails on the old code: 300 shares left at 16:40."""
    pb = load_playbook(cfg, "asx_daytrader")
    acct = arena.account(pb, "agent")
    _held(acct, "DTX", 800, 1.0, manage=dict(MANAGE))
    acct.positions["DTX"].stop = 0.95
    rows = session(until=(14, 0))
    rows[(14, 0)] = (1.05, 1.10, 1.05, 1.08, 500)  # +2R: half-off raised, 100 of 400 fill
    rows.update({k: (1.07, 1.08, 1.06, 1.07, 10000) for k in session((14, 1), (16, 0))})
    rows[(16, 10)] = (1.07, 1.07, 1.07, 1.07, 100000)
    put(mb, "DTX", DAY, rows)
    arena.broker.clock = Clock(at(15, 50, 30))
    arena.broker.work(acct, at(15, 50, 30))  # saves the book
    (half,) = acct.orders.values()
    assert half.order_type == "target" and half.filled_qty == 100
    assert acct.positions["DTX"].qty == 700
    out = v2_flow.flatten(arena, pb, at(15, 50), price_of=lambda t: 1.07)
    acct = arena.account(pb, "agent")  # flatten worked on the saved book
    flat = acct.orders[out[0]["order_id"]]
    assert flat.qty == 700 and flat.flat  # the old code: 400, the half-off's rest held back
    half = acct.orders[half.order_id]
    assert half.status == "partial" and "flat-by-close sweep" in half.message
    arena.broker.clock = Clock(at(16, 40))
    arena.broker.work(acct, at(16, 40))
    assert "DTX" not in acct.positions
    assert v2_flow.flatten(arena, pb, at(16, 40), price_of=lambda t: 1.07) == []


def test_a_target_reached_after_the_sweep_does_not_cancel_it(arena, cfg, mb):
    """The target was reached at 15:51, after the sweep's order; it cancelled the sweep's
    order, filled 100 of 800 and rested overnight, and at 16:40 flatten placed 0."""
    pb = load_playbook(cfg, "asx_daytrader")
    acct = arena.account(pb, "agent")
    _held(acct, "DTX", 800, 1.0, target=1.10,
          target_from=at(10, 30).isoformat(timespec="minutes"))  # fmt: skip
    rows = session(until=(15, 51))
    rows[(15, 51)] = (1.05, 1.10, 1.05, 1.06, 500)
    rows.update({k: (1.06, 1.06, 1.06, 1.06, 10000) for k in session((15, 52), (16, 0))})
    rows[(16, 10)] = (1.06, 1.06, 1.06, 1.06, 100000)
    put(mb, "DTX", DAY, rows)
    arena.broker.clock = Clock(at(15, 50, 30))
    arena.broker.work(acct, at(15, 50, 30))  # saves the book
    (sweep,) = v2_flow.flatten(arena, pb, at(15, 50), price_of=lambda t: 1.0)
    acct = arena.account(pb, "agent")
    arena.broker.clock = Clock(at(16, 40))
    arena.broker.work(acct, at(16, 40))
    o = acct.orders[sweep["order_id"]]
    assert o.status == "filled" and o.filled_qty == 800
    assert not [x for x in acct.orders.values() if x.order_type == "target"]
    assert "DTX" not in acct.positions


def test_a_working_stop_exit_still_counts_as_already_closing(arena, cfg, mb):
    pb = load_playbook(cfg, "asx_daytrader")
    acct = arena.account(pb, "agent")
    _held(acct, "DTX", 800, 1.0)
    rows = session(until=(15, 40))
    rows[(15, 40)] = (0.89, 0.89, 0.88, 0.88, 500)  # stop 0.90: 100 of 800 fill
    rows.update({k: (0.88, 0.88, 0.88, 0.88, 0) for k in session((15, 41), (16, 0))})
    rows[(16, 10)] = (0.88, 0.88, 0.88, 0.88, 0)  # nothing more trades
    put(mb, "DTX", DAY, rows)
    arena.broker.clock = Clock(at(15, 50, 30))
    arena.broker.work(acct, at(15, 50, 30))  # saves the book
    assert acct.closing_qty_working("DTX") == 700
    assert v2_flow.flatten(arena, pb, at(15, 50), price_of=lambda t: 0.88) == []


# --------------------------------------------------------------------------
# B6: a later slice never undoes management
# --------------------------------------------------------------------------
def test_a_later_slice_of_an_entry_does_not_undo_a_breakeven_stop(broker, mb):
    acct = broker.store.open("dt__bot", "asx_daytrader", "bot", 1, 20_000.0)
    rows = session(until=(10, 31))
    rows[(10, 31)] = (1.0, 1.0, 1.0, 1.0, 5000)  # room 1,000: the first slice
    rows[(10, 32)] = (1.05, 1.06, 1.0, 1.05, 5000)  # +1R: breakeven, then the second slice
    rows[(10, 33)] = (1.05, 1.05, 1.05, 1.05, 5000)
    put(mb, "DTX", DAY, rows)
    broker.clock = Clock(at(10, 30, 10))
    o = broker.submit(acct, ticker="DTX", side="buy", qty=2000, limit=1.10, stop=0.95,
                      manage={"breakeven_at_r": 1.0, "half_at_r": 2.0, "trail_at_r": 2.0,
                              "trail_distance_r": 1.0})  # fmt: skip
    broker.work(acct, at(10, 34))
    pos = acct.positions["DTX"]
    assert pos.qty == 2000 and [f["minute"][11:16] for f in o.fills] == ["10:31", "10:32"]
    # Breakeven is the first slice's price (1.00 plus slippage); the old code put the stop
    # back to the entry's 0.95 when the second slice filled.
    assert pos.stop == pytest.approx(o.fills[0]["price"], abs=1e-4) and pos.stop > 0.95


# --------------------------------------------------------------------------
# D8, D13, D15, G10: the broker's records
# --------------------------------------------------------------------------
class FakeLive:
    def __init__(self, ok=True, frame=None):
        self.ok, self.frame = ok, frame

    def live_ok(self):
        return self.ok

    def bars_today(self, code, day):
        return self.frame


def test_with_live_bars_an_expired_entry_is_done_a_minute_after_its_good_till(cfg, mb):
    rows = session(until=(10, 50), px=1.05)
    put(mb, "DTX", DAY, rows)
    b = ArenaBroker(cfg.data_dir, CostModel.from_config(cfg), mb, lambda t: 2_000_000.0,
                    resolve_after_minutes=22, clock=Clock(at(10, 30)),
                    opening_auction="first_minute", live_expiry_minutes=1)  # fmt: skip
    acct = b.store.open("dt__agent", "asx_daytrader", "agent", 1, 20_000.0)
    o = b.submit(acct, ticker="DTX", side="buy", qty=1000, limit=1.0, stop=0.95,
                 good_till=at(10, 40))  # fmt: skip
    b.work(acct, at(10, 41, 30))
    assert o.status == "pending_fill"  # Yahoo's bars: the 22-minute allowance holds
    mb.live = FakeLive(ok=True)
    mb._live_days.add(("DTX", DAY))
    b.work(acct, at(10, 41, 30))
    assert o.status == "expired"


def test_the_live_allowance_is_not_used_while_the_stream_is_down(cfg, mb):
    put(mb, "DTX", DAY, session(until=(10, 50), px=1.05))
    b = ArenaBroker(cfg.data_dir, CostModel.from_config(cfg), mb, lambda t: 2_000_000.0,
                    resolve_after_minutes=22, clock=Clock(at(10, 30)),
                    opening_auction="first_minute", live_expiry_minutes=1)  # fmt: skip
    acct = b.store.open("dt__agent", "asx_daytrader", "agent", 1, 20_000.0)
    o = b.submit(acct, ticker="DTX", side="buy", qty=1000, limit=1.0, stop=0.95,
                 good_till=at(10, 40))  # fmt: skip
    mb.live = FakeLive(ok=False)
    mb._live_days.add(("DTX", DAY))
    b.work(acct, at(10, 41, 30))
    assert o.status == "pending_fill"


def test_a_position_with_no_trade_that_day_is_marked_at_its_last_close(broker, mb):
    acct = broker.store.open("v1__agent", "asx_announcements", "agent", 1, 20_000.0)
    _held(acct, "AAA", 1000, 1.0)
    put(mb, "AAA", date(2026, 1, 7), {(15, 59): (0.8, 0.8, 0.8, 0.8, 100)})
    marks = broker.price_marks(acct, DAY)
    assert marks["AAA"][0] == pytest.approx(0.8) and "07 Jan" in marks["AAA"][1]
    assert broker.prices(acct, DAY)["AAA"] == pytest.approx(0.8)  # was 1.00, the cost
    m = broker.mark_to_market(acct, DAY)
    assert "AAA at 0.8000, last traded close 07 Jan" in m.note
    acct.positions["ZZZ"] = Position("ZZZ", 10, 2.0, at(10, 0).isoformat())
    assert broker.price_marks(acct, DAY)["ZZZ"] == (2.0, "cost (no traded price known)")


def test_each_fill_slice_names_its_feed_and_each_order_its_code(broker, mb, monkeypatch):
    monkeypatch.setattr(B, "_RELEASE", [])
    monkeypatch.delenv("ASXBOT_RELEASE", raising=False)
    acct = broker.store.open("dt__bot", "asx_daytrader", "bot", 1, 20_000.0)
    put(mb, "DTX", DAY, session(until=(10, 40), vol=2000))
    broker.clock = Clock(at(10, 30, 10))
    o = broker.submit(acct, ticker="DTX", side="buy", qty=600, limit=1.1, stop=0.95)
    live = pd.DataFrame(index=pd.DatetimeIndex([at(10, 32)]))
    broker.minutes.live = FakeLive(frame=live)
    broker.work(acct, at(10, 40))
    feeds = [f["feed"] for f in o.fills]
    assert feeds == ["Yahoo minute", "IBKR live minute"]  # 400 a bar: 10:31, then 10:32
    assert o.release == "checkout"
    rec = [json.loads(x) for x in (broker.store.root.parent / "events" / "arena_orders.jsonl")
           .read_text(encoding="utf-8").splitlines()]  # fmt: skip
    assert rec[0]["release"] == "checkout"


def test_a_book_with_fields_this_version_does_not_know_still_loads():
    from asxbot.arena.accounts import _order, _position

    o = _order({"order_id": "A", "account": "x", "ticker": "T", "side": "buy", "qty": 1,
                "limit": 1.0, "decided_at": "2026-09-28T10:00:00+10:00", "from_2027": 1})
    p = _position({"ticker": "T", "qty": 1, "avg_cost": 1.0, "opened_at": "x", "new": 2})
    assert o.order_id == "A" and p.qty == 1


def test_the_release_stamp_is_the_commit_the_task_runs(tmp_path, monkeypatch):
    rel = tmp_path / "20260926-0800-abcdef1234"
    rel.mkdir()
    (rel / "RELEASE.json").write_text(json.dumps({"commit": "abcdef1234567890"}), "utf-8")
    monkeypatch.setattr(B, "_RELEASE", [])
    monkeypatch.setenv("ASXBOT_RELEASE", str(rel))
    assert B.release_stamp() == "abcdef1234"


# --------------------------------------------------------------------------
# D4: round trips after every cost
# --------------------------------------------------------------------------
def _order(oid, side, ticker, fills, *, order_type="limit", status="filled", model="bot",
           commission=6.6, realised=0.0, qty=None):
    filled = sum(q for _, q, _ in fills)
    return ArenaOrder(
        oid, "dt__bot", ticker, side, qty or filled, fills[0][2], at(*fills[0][0]).isoformat(),
        status=status, order_type=order_type, filled_qty=filled,
        avg_price=sum(q * p for _, q, p in fills) / filled, commission=commission,
        realised=realised, model=model,
        fills=[{"minute": at(*m).isoformat(timespec="minutes"), "qty": q, "price": p}
               for m, q, p in fills],
    )  # fmt: skip


def _day_trader_bot_25_sep(acct):
    """The day trader's rule bot on 25 Sep 2026, as its book recorded it."""
    for o in (
        _order("ARN-6", "short", "NWL", [((11, 21), 208, 17.6223)]),
        _order("ARN-7", "buy", "DOW", [((11, 22), 737, 6.7004)]),
        _order("ARN-9", "short", "CWY", [((11, 25), 1084, 2.5974), ((11, 26), 344, 2.5974)]),
        _order("ARN-17", "cover", "NWL", [((15, 30), 104, 16.7437)], order_type="target",
               realised=91.37),
        _order("ARN-14", "cover", "NWL", [((15, 51), 104, 17.047)], status="partial", qty=208,
               realised=59.83),
        _order("ARN-15", "sell", "DOW", [((15, 51), 737, 6.7083)], realised=5.82),
        _order("ARN-16", "cover", "CWY", [((15, 51), 1428, 2.5926)], realised=6.85),
    ):  # fmt: skip
        acct.orders[o.order_id] = o


def test_trades_are_round_trips_won_after_every_cost(broker):
    """25 Sep: printed 4 trades, all won; the truth was 3 round trips and 1 winner after
    costs (NWL +131.40, CWY -6.33, DOW -7.42 in the book's unrounded prices)."""
    from asxbot.arena.scoreboard import round_trips, score

    acct = broker.store.open("dt__bot", "asx_daytrader", "bot", 1, 20_000.0)
    _day_trader_bot_25_sep(acct)
    trips = {t.ticker: t for t in round_trips(acct)}
    assert set(trips) == {"NWL", "DOW", "CWY"} and all(t.is_closed for t in trips.values())
    assert trips["NWL"].net == pytest.approx(131.40, abs=0.05)
    assert trips["NWL"].entry_fees + trips["NWL"].exit_fees == pytest.approx(19.8)
    assert trips["DOW"].net == pytest.approx(5.82 - 13.2, abs=0.05)
    assert trips["CWY"].net == pytest.approx(6.85 - 13.2, abs=0.05)
    s = score(broker.store, acct, {})
    assert (s.trades, s.wins, s.win_rate_pct) == (3, 1, pytest.approx(33.3))


def test_the_scorecard_counts_round_trips_and_the_best_trades_true_share(arena, cfg):
    from asxbot.arena.report import scorecard_facts, scorecard_lines

    pb = load_playbook(cfg, "asx_daytrader")
    acct = arena.account(pb, "bot")
    _day_trader_bot_25_sep(acct)
    arena.store.save(acct)
    rows = scorecard_facts(arena, DAY, [])
    (a,) = [a for r in rows if r["key"] == pb.key for a in r["accounts"] if a["kind"] == "bot"]
    assert (a["trades"], a["wins_after_fees"]) == (3, 1)
    assert a["best_trade_share_pct"] == pytest.approx(92.3, abs=0.2)  # was 56%
    text = "\n".join(scorecard_lines({"scorecard": rows}))
    assert "round trips 3 (1 won after all costs)" in text and "NWL short: +131.4" in text


# --------------------------------------------------------------------------
# The report and the 16:10 summary
# --------------------------------------------------------------------------
def test_the_report_counts_the_sydney_day(arena, cfg):
    """D5: 08:30 Sydney on 25 Sep is 24 Sep in UTC; the old test counted by UTC date."""
    from asxbot.arena.report import gather

    events(cfg, "announcements", [
        (datetime(2026, 9, 25, 8, 30, tzinfo=SYD), {"code": "AAA"}),
        (datetime(2026, 9, 25, 12, 0, tzinfo=SYD), {"code": "BBB"}),
        (datetime(2026, 9, 26, 6, 0, tzinfo=SYD), {"code": "CCC"}),  # 25 Sep in UTC
    ])  # fmt: skip
    # The old count got 2 for 25 Sep too, but BBB and CCC: AAA was lost, CCC borrowed.
    assert gather(arena, date(2026, 9, 25))["announcements_seen"] == 2
    assert gather(arena, date(2026, 9, 26))["announcements_seen"] == 1


def test_failed_calls_are_said_plainly_and_not_counted_as_rejections(arena, cfg):
    from asxbot.arena import daytrader as DT
    from asxbot.arena.report import agent_brief, daytrader_facts, gather, render_plain

    day = date(2026, 9, 25)
    fail = {"agent": "trader-decider", "ok": False, "kind": "usage_limit", "reason": "limit"}
    events(cfg, "arena_agent_calls", [
        (datetime(2026, 9, 25, 10, 5, tzinfo=SYD), {**fail, "purpose": "day trader a DTX"}),
        (datetime(2026, 9, 25, 10, 30, tzinfo=SYD), {**fail, "purpose": "v2 reaction look A"}),
        (datetime(2026, 9, 25, 11, 0, tzinfo=SYD), {**fail, "purpose": "chat with Rick"}),
        (datetime(2026, 9, 25, 11, 5, tzinfo=SYD), {"agent": "trader-decider", "ok": True}),
    ])  # fmt: skip
    st = DT.load_state(cfg.data_dir, day)
    st["universe"] = ["DTX"]
    st["signals"] = [
        {"setup": "gap_and_go", "agent": {"rejected": "no answer within 60s (x timed out)"}},
        {"setup": "gap_and_go", "agent": {"unavailable": "usage_limit", "why": "limit"}},
        {"setup": "orb", "agent": {"rejected": "market too weak"}},
        {"setup": "orb", "skipped": "stale: the trigger bar is 9 bars old", "agent": None},
        {"setup": "orb", "agent": {"skipped": "stale by its turn: 4 bars old at the scan"}},
    ]
    DT.save_state(cfg.data_dir, day, st)
    dt = daytrader_facts(cfg, day)
    assert (dt["agent_rejected"], dt["agent_unavailable"]) == (1, 2)
    assert (dt["stale"], dt["agent_stale_by_its_turn"]) == (1, 1)
    facts = gather(arena, day)
    line = facts["agent_unavailable"]["line"]
    assert line == "the agent could not be asked 2 times from 10:05 to 10:30: usage limit"
    assert facts["agent_unavailable"]["chat_calls_failed"] == 1
    assert f"AGENT UNAVAILABLE</b>: {line}" in render_plain(facts)
    assert line in agent_brief(facts).split("FACTS (JSON)")[0]


def test_a_reaction_look_whose_call_failed_is_not_a_look(cfg):
    from asxbot.arena.reaction_v2 import save_queue
    from asxbot.arena.report import v2_facts

    day = date(2026, 9, 25)
    save_queue(cfg.data_dir, day, {
        "AAA": {"status": "looked", "outcome": {"error": "decider failed: usage limit: x"}},
        "BBB": {"status": "looked", "outcome": {"decision": "pass"}},
    })  # fmt: skip
    v2 = v2_facts(cfg, day)
    assert v2["reaction_looks_by_outcome"] == {"agent unavailable": 1, "looked": 1}
    assert [x["ticker"] for x in v2["looked"]] == ["BBB"]


def test_a_day_with_no_decisions_does_not_claim_delayed_data(cfg):
    from asxbot.arena.intraday import DELAYED_LABEL
    from asxbot.arena.report import NO_DECISIONS, data_line

    pb = load_playbook(cfg, "asx_daytrader")
    assert data_line(pb, {}) == NO_DECISIONS
    assert data_line(pb, {DELAYED_LABEL: 3}) == (pb.data_basis or DELAYED_LABEL)
    assert data_line(pb, {"live data (IBKR)": 2}) == "prices per decision: 2 on live data (IBKR)"


def test_the_brief_no_longer_hands_the_agent_the_delayed_phrase(arena):
    from asxbot.arena.report import agent_brief, gather

    rules = agent_brief(gather(arena, date(2026, 9, 25))).split("FACTS (JSON)")[0]
    assert "rehearsal until IBKR live prices" not in rules


def test_the_session_summary_counts_openings_and_exits_apart(cfg):
    """D10: the flat sweep's exits made "traded 4" of a day with 2 entries. H13: a deferral
    (the feed was down) is not a rejection. G1: failed calls are counted."""
    from asxbot.arena.tally import counts_for

    day = date(2026, 9, 25)
    t = datetime(2026, 9, 25, 11, 0, tzinfo=SYD)
    events(cfg, "arena_orders", [
        (t, {"event": "submitted", "side": "buy", "placed_by": "agent"}),
        (t, {"event": "submitted", "side": "short", "placed_by": "agent"}),
        (t, {"event": "submitted", "side": "buy", "placed_by": "bot"}),
        (t, {"event": "submitted", "side": "sell", "placed_by": "agent"}),
        (t, {"event": "submitted", "side": "cover", "placed_by": "agent"}),
        (t, {"event": "submitted", "side": "sell", "placed_by": "bot"}),
    ])  # fmt: skip
    events(cfg, "arena_screened", [
        (t, {"ids_id": "1", "ok": False, "test": "tick", "v2": True}),
        (t, {"ids_id": "2", "ok": False, "test": "deferred", "v2": True}),
    ])  # fmt: skip
    events(cfg, "arena_agent_calls", [
        (t, {"ok": False, "kind": "usage_limit", "purpose": "day trader x"}),
    ])  # fmt: skip
    c = counts_for(cfg.data_dir, day)
    assert (c.traded, c.orders_bot, c.exits) == (2, 1, 3)
    assert (c.screened, c.by_test, c.deferred) == (1, {"tick": 1}, 1)
    assert (c.agent_unavailable, c.agent_unavailable_first) == (1, "11:00")
    assert c.agent_unavailable_why == "usage limit"
