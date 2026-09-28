"""The AI team's live paper book (src/asxbot/arena/team, 28 Sep 2026): the simulator's team on
a live clock, its hard limits, its usage stop, restarts, the watcher's thread, the lock, the
evening block and the calibration. Synthetic history only: nothing here is a result."""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, time, timedelta

import pytest

from asxbot.config import load_config
from asxbot.lab.tsim.market import SYD, slot_time

DAYS = None
CODES = ["AAX", "ABX", "ACX", "BAX", "BBX", "SAX"]


@pytest.fixture
def cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )


@pytest.fixture
def world(cfg, tmp_path, monkeypatch):
    """Two weeks of synthetic history in the IBKR cache's layout, the announcements in the live
    collector's files, the lab's folders in tmp, no ASX 200 list fetched."""
    from asxbot.arena.replay_ibkr import COLS, sessions
    from asxbot.arena.team import runner
    from asxbot.lab.tsim.synthetic import make_history

    days = sessions(date(2026, 7, 13), date(2026, 7, 24))
    hist = tmp_path / "hist"
    ann = make_history(hist, days, CODES)
    monkeypatch.setenv("ASXBOT_IBKR_HISTORY", str(hist))
    monkeypatch.setenv("ASXBOT_LAB_LOCAL", str(tmp_path / "lab"))
    monkeypatch.delenv("ASXBOT_TEAM_USAGE_STOP", raising=False)
    monkeypatch.setattr(runner, "shortable", lambda cfg: {"AAX", "ABX"})
    live = cfg.data_dir / "announcements" / "live"
    live.mkdir(parents=True, exist_ok=True)
    ann["release_date"] = ann["release_date"].astype(str)
    for d in days:
        part = ann[ann["release_date"] == d.isoformat()].copy()
        for c in COLS:
            if c not in part.columns:
                part[c] = None
        part[COLS].to_parquet(live / f"{d.isoformat()}.parquet")
    return {"days": days, "hist": hist}


class FakeTeam:
    """The model calls, by role: the decision-maker buys AAX with a bracket at the pre-open and
    sets a call-back; the risk manager approves; the rest say little."""

    def __init__(self, orders=None, veto=False):
        self.seen = []
        self.lock = threading.Lock()
        self.orders = orders
        self.veto = veto

    def __call__(self, prompt, *, system, model, effort, cfg=None, **kw):
        if "ROLE:" not in system:  # the PDF reader (lab/tsim/reader.py)
            with self.lock:
                self.seen.append(("pdf reader", model, False, False))
            return {
                "text": json.dumps(
                    {"category": "other", "direction": "neutral", "materiality": 1, "summary": "x"}
                ),
                "seconds": 2.0,
                "usage": {"output": 5},
            }
        line = system[system.index("ROLE:") :].split("\n")[0]
        role = next(
            r
            for r in (
                "announcement reader",
                "market scanner",
                "strategy specialist",
                "DECISION-MAKER",
                "RISK MANAGER",
                "RESEARCHER",
            )
            if r in line
        )
        with self.lock:
            self.seen.append((role, model, "[PRE-OPEN]" in prompt, "END OF DAY" in prompt))
        if role == "DECISION-MAKER":
            if "END OF DAY" in prompt:
                text = {"journal": "a quiet day", "lessons": ["wait for the open"]}
            elif "[PRE-OPEN]" in prompt:
                text = {
                    "orders": self.orders
                    if self.orders is not None
                    else [
                        {
                            "op": "place",
                            "code": "AAX",
                            "side": "buy",
                            "value_aud": 3000,
                            "type": "market",
                            "attach": {"stop": 0.01, "target": 999.0},
                            "why": "test",
                        }
                    ],
                    "next_wake": "11:00",
                    "note": "plan",
                }
            else:
                text = {"orders": [], "note": "hold"}
        elif role == "RISK MANAGER":
            text = {"verdicts": [{"index": 0, "approve": not self.veto, "why": "t"}]}
        elif role == "market scanner":
            text = {"watchlist": [{"code": "AAX", "why": "x"}], "market": "calm"}
        elif role == "RESEARCHER":
            text = {"lessons": ["r"], "ideas": []}
        else:
            text = {}
        return {"text": json.dumps(text), "seconds": 3.0, "usage": {"output": 10, "input": 5}}


def replay(cfg, world, root, ask, day=None, **kw):
    from asxbot.arena.team.runner import replay_session

    day = day or world["days"][5]
    return replay_session(cfg, day, root, codes=CODES, ask=ask, **kw)


# ------------------------------------------------------------------ a whole day
def test_a_day_runs_the_simulators_team_through_the_live_driver(cfg, world, tmp_path):
    ask = FakeTeam()
    s = replay(cfg, world, tmp_path / "book", ask)
    res = s.run()
    assert res["how"] == "done"
    roles = {r for r, *_ in ask.seen}
    assert {
        "market scanner",
        "strategy specialist",
        "DECISION-MAKER",
        "RISK MANAGER",
        "RESEARCHER",
    } <= roles
    top = [m for r, m, *_ in ask.seen if r in ("DECISION-MAKER", "RISK MANAGER", "RESEARCHER")]
    assert top and all(m == "claude-opus-5-5" for m in top)
    staff = [m for r, m, *_ in ask.seen if r in ("market scanner", "strategy specialist")]
    assert staff and all(m == "claude-sonnet-5" for m in staff)
    assert any(pre for _, _, pre, _ in ask.seen) and any(eod for *_, eod in ask.seen)
    assert res["orders_placed"] >= 1 and res["fills"] >= 1
    # the call-back at 11:00 woke it again
    assert any(w.get("phase") == "session" for w in res["wake_log"])
    rec = json.loads((tmp_path / "book" / "days" / f"{res['day']}.json").read_text())
    assert rec["pnl"] == res["pnl"] and rec["universe"] == sorted(CODES)
    # the book never touches the frozen books' accounts
    assert not (cfg.data_dir / "arena" / "accounts").exists()
    # the team's own lessons carry to its next day
    from asxbot.lab.tsim import live as tlive

    assert tlive.lessons_before(tmp_path / "book", "2099-01-01")


def test_a_decision_reaches_the_broker_only_after_its_thinking(cfg, world, tmp_path):
    """What the team decides at T after thinking L seconds fills only from bars starting after
    T + L - the simulator's rule, kept on the live driver."""
    ask = FakeTeam()
    s = replay(cfg, world, tmp_path / "book", ask)
    s.run()
    (o,) = [
        o
        for o in s.broker.acct.orders.values()
        if o.by == "team" and o.code == "AAX" and o.parent is None
    ]
    sub = datetime.fromisoformat(o.submitted_at)
    assert sub > datetime.combine(s.day, time(9, 40), tzinfo=SYD)
    assert all(datetime.fromisoformat(f["at"]) >= sub for f in o.fills)


# ------------------------------------------------------------------ the hard limits
def test_the_kill_switch_refuses_entries_and_closes_the_book(cfg, world, tmp_path):
    from asxbot.arena.team.limits import set_kill

    root = tmp_path / "book"
    ask = FakeTeam()
    s = replay(cfg, world, root, ask, day=world["days"][4])
    s.run()
    assert s.broker.acct.positions  # AAX held (its stop is far away, its target too)
    set_kill(root, "test")
    ask2 = FakeTeam()
    s2 = replay(cfg, world, root, ask2, day=world["days"][5], state=None)
    res = s2.run()
    assert not ask2.seen  # nobody asked
    assert not s2.broker.acct.positions  # closed at market by code
    assert res["orders_by_code"] >= 1 and "kill switch" in res["killed"]


def test_the_closing_volume_cap_cuts_an_entry(cfg, world, tmp_path):
    from asxbot.arena.team.limits import Limits

    s = replay(cfg, world, tmp_path / "book", FakeTeam())
    s.m.now = slot_time(s.day, 60)
    cap = Limits({}, tmp_path / "book", None).closing_cap(s.m, "AAX")
    assert cap and cap > 0
    lim = s.limits
    sent, why = lim.check(
        s.view,
        {"op": "place", "code": "AAX", "side": "buy", "qty": cap * 3, "type": "market"},
        s.m.now,
    )
    assert sent["qty"] == cap and "closing-volume cap" in why
    # an exit is never cut or refused
    sent, why = lim.check(s.view, {"op": "cancel", "id": "O1"}, s.m.now)
    assert sent and not why


def test_the_daily_loss_limit_and_ricks_pause_refuse_entries_not_exits(cfg, world, tmp_path):
    from asxbot.arena.pause import set_pause

    s = replay(cfg, world, tmp_path / "book", FakeTeam())
    s.m.now = slot_time(s.day, 60)
    s.limits.start_equity = s.broker.acct.equity(s.m.last) / 0.8  # down 20% today
    sent, why = s.limits.check(
        s.view, {"op": "place", "code": "AAX", "side": "buy", "qty": 10}, s.m.now
    )
    assert sent is None and "daily loss limit" in why
    s.limits.start_equity = s.broker.acct.equity(s.m.last)
    set_pause(cfg.data_dir, s.m.now, "Rick", "stop for today")
    sent, why = s.limits.check(
        s.view, {"op": "place", "code": "AAX", "side": "buy", "qty": 10}, s.m.now
    )
    assert sent is None and "paused" in why
    s.broker.acct.positions["AAX"] = type("P", (), {"qty": 100})()
    sent, why = s.limits.check(
        s.view, {"op": "place", "code": "AAX", "side": "sell", "qty": 100}, s.m.now
    )
    assert sent is not None and not why  # an exit


def test_a_stale_live_feed_refuses_entries_without_asking_ibkr_for_anything(cfg):
    from asxbot.arena.team.limits import feed_fresh

    class GW:
        s = type("S", (), {"max_data_age_s": 90})()
        asked = []

        def streaming(self, c):
            return c == "BHP"

        def data_age_s(self, c):
            return 200.0

        def today_fetched_at(self, c, d):
            return None

        def queue_history(self, *a):
            self.asked.append(a)

    class Feed:
        primary = type("P", (), {"gw": GW()})()

        def entries_allowed(self, now, codes):
            assert codes == []  # never names a stock: that would queue a poll
            return True, ""

    now = datetime(2026, 9, 30, 11, 0, tzinfo=SYD)
    ok, why = feed_fresh(Feed(), "BHP", now)
    assert not ok and "stale" in why
    ok, why = feed_fresh(Feed(), "ZIP", now)
    assert not ok and "not streamed" in why
    assert GW.asked == []


# ------------------------------------------------------------------ usage
def test_the_usage_stop_and_the_foremans_pause(cfg, tmp_path, monkeypatch, foreman_home):
    from asxbot.arena.team.limits import usage_budget

    monkeypatch.setenv("ASXBOT_LAB_LOCAL", str(tmp_path / "lab"))
    monkeypatch.delenv("ASXBOT_TEAM_USAGE_STOP", raising=False)
    (tmp_path / "lab").mkdir()
    now = datetime.now(SYD).isoformat(timespec="seconds")
    (tmp_path / "lab" / "usage.json").write_text(json.dumps({"at": now, "seven_day": 0.69}))
    b = usage_budget({"usage_stop": 0.70})
    assert b(None)[0]
    (tmp_path / "lab" / "usage.json").write_text(json.dumps({"at": now, "seven_day": 0.70}))
    ok, why = b(None)
    assert not ok and "70%" in why
    monkeypatch.setenv("ASXBOT_TEAM_USAGE_STOP", "0.93")  # a build's own dry run
    assert b(None)[0]
    foreman_home.mkdir(parents=True, exist_ok=True)
    (foreman_home / "claude_pause.json").write_text(
        json.dumps({"paused": True, "why": "limit", "updated": now})
    )
    ok, why = b(None)
    assert not ok and "paused" in why


def test_a_silenced_team_is_not_asked_and_its_positions_close_at_the_close(cfg, world, tmp_path):
    root = tmp_path / "book"
    s = replay(cfg, world, root, FakeTeam(), day=world["days"][4])
    s.run()
    assert s.broker.acct.positions
    ask = FakeTeam()
    s2 = replay(cfg, world, root, ask, day=world["days"][5], state=None)
    s2.budget = lambda cfg: (False, "weekly Claude usage 71% is at the team's stop (70%)")
    res = s2.run()
    assert not ask.seen and "70%" in res["silenced"]
    assert not s2.broker.acct.positions  # sold in the closing auction by code
    assert any(o.type == "moc" and o.by == "code" for o in s2.broker.acct.orders.values())


# ------------------------------------------------------------------ restarts
def test_a_restart_resumes_the_same_day_from_the_book(cfg, world, tmp_path):
    from asxbot.arena.team.session import VirtualClock

    class StopAtNoon(VirtualClock):
        def __init__(self, t, stop):
            super().__init__(t)
            self.stop = stop

        def wait_until(self, t, stop):
            if t.time() >= time(12, 0):
                return False
            return super().wait_until(t, stop)

    root = tmp_path / "book"
    ask = FakeTeam()
    s = replay(cfg, world, root, ask)
    s.clock = StopAtNoon(s.clock.t, None)
    res = s.run()
    assert res["how"] == "stopped"
    pre = sum(1 for r, _, p, _ in ask.seen if r == "DECISION-MAKER" and p)
    assert pre == 1
    positions = {c: p.qty for c, p in s.broker.acct.positions.items()}
    ask2 = FakeTeam()
    s2 = replay(cfg, world, root, ask2, state=None)
    assert {c: p.qty for c, p in s2.broker.acct.positions.items()} == positions
    assert s2.st["worked_slot"] > 100
    res2 = s2.run()
    assert res2["how"] == "done"
    assert not any(p for r, _, p, _ in ask2.seen if r == "DECISION-MAKER")  # no second pre-open


# ------------------------------------------------------------------ the live market
def test_the_live_market_fills_the_simulators_grid_as_minutes_arrive(cfg, world):
    from asxbot.arena.team.market import LiveMarket, build_summaries
    from asxbot.lab.tsim.market import History

    day = world["days"][5]
    hist = History(world["hist"])
    held = {"AAX": 0}

    def source(code, d):  # AAX arrives minute by minute; the rest not at all
        df = hist.load(code, d)
        if code != "AAX" or df is None:
            return None
        return df.iloc[: held["AAX"]]

    m = LiveMarket(day, CODES, build_summaries(CODES, day), set(), source)
    assert set(m.bars) == set(CODES) and m.index is None
    m.now = slot_time(day, 30)
    assert m.last("AAX") == m.prev_close("AAX")  # nothing in yet: the previous close
    held["AAX"] = 20
    assert m.ingest() == 1
    assert m.bars["AAX"].v[:15].sum() > 0 and m.last("AAX") != m.prev_close("AAX")
    assert m.ingest() == 0  # unchanged: not re-gridded
    assert m.has_minute("AAX", 10) and not m.has_minute("AAX", 40)


def test_a_late_minute_is_worked_at_the_price_it_had(cfg, world, tmp_path):
    """A stock without a streaming line comes in with its next poll: its stop fills at the
    bar that reached it, not at the moment the bar arrived."""
    from asxbot.arena.team.market import LiveMarket, build_summaries
    from asxbot.arena.team.session import TeamSession
    from asxbot.lab.tsim.market import History

    day = world["days"][5]
    hist = History(world["hist"])
    upto = {"n": 0}

    def source(code, d):
        df = hist.load(code, d)
        return None if df is None else df.iloc[: upto["n"]]

    m = LiveMarket(day, CODES, build_summaries(CODES, day), set(), source)

    class Clock:
        live = True
        t = datetime.combine(day, time(8, 0), tzinfo=SYD)

        def now(self):
            return self.t

    clock = Clock()
    s = TeamSession(
        cfg,
        day,
        tmp_path / "book",
        market=m,
        clock=clock,
        ask=FakeTeam(),
        budget=None,
        conf={},
        feed=None,
        state={},
        max_wait_s=7200,
    )
    full = hist.load("AAX", day)
    lo = float(full["low"].iloc[1:60].min())
    # a resting sell stop just above the day's early low, from a long bought at the open
    upto["n"] = 1
    m.ingest()
    m.now = slot_time(day, 1)
    s.broker.place("AAX", "buy", 100, "market", at=slot_time(day, 0) - timedelta(minutes=30))
    s.broker.work_slot(0)
    stop_px = round(lo * 1.001, 3)
    o = s.broker.place(
        "AAX", "sell", 100, "stop", stop=stop_px, at=slot_time(day, 0) + timedelta(seconds=30)
    )
    # minutes 1..59 are worked while AAX has no bars in yet: deferred
    for slot in range(1, 60):
        clock.t = slot_time(day, slot + 1) + timedelta(seconds=20)
        s._work_deferred(slot)
        for code in {x.code for x in s.broker.acct.working()}:
            if code not in s.deferred and not m.has_minute(code, slot):
                s.deferred[code] = slot
        s.broker.work_slot(slot)
    assert o.working and s.deferred.get("AAX") == 1
    upto["n"] = 61  # the poll brings them in
    m.ingest()
    s._work_deferred(60)
    assert not o.working and o.filled == 100
    hit = next(i for i in range(1, 60) if full["low"].iloc[i] <= o.stop)
    assert o.fills[0]["at"][11:16] == slot_time(day, hit).strftime("%H:%M")


# ------------------------------------------------------------------ the watcher's side
def test_the_watchers_tick_starts_one_thread_from_the_first_day(cfg, monkeypatch):
    from asxbot.arena.team import runner

    started = []
    monkeypatch.setattr(runner, "_run", lambda *a: started.append(a))
    monkeypatch.setattr(
        runner,
        "_T",
        {
            "thread": None,
            "stop": None,
            "done": None,
            "day": None,
            "restarts": 0,
            "last_start": 0.0,
            "said_gave_up": False,
        },
    )

    class View:
        feed = object()

    import asxbot.arena.watch as W

    monkeypatch.setattr(W, "day_view", lambda arena, now: View())
    arena = type("A", (), {"cfg": cfg, "universe": {"BHP"}})()
    conf = dict(cfg.raw["arena"]["team"])
    assert conf["enabled"] and conf["start"] == "2026-09-30"
    assert runner.tick(arena, datetime(2026, 9, 29, 10, 0, tzinfo=SYD)).startswith("starts")
    assert runner.tick(arena, datetime(2026, 10, 3, 10, 0, tzinfo=SYD)) == "not a trading day"
    assert runner.tick(arena, datetime(2026, 9, 30, 8, 0, tzinfo=SYD)) == "outside its hours"
    assert runner.tick(arena, datetime(2026, 9, 30, 9, 5, tzinfo=SYD)) == "started"
    runner._T["thread"].join(5)
    assert len(started) == 1
    assert runner.tick(arena, datetime(2026, 9, 30, 9, 6, tzinfo=SYD)) == "waiting to restart"


def test_the_watcher_calls_the_team_every_cycle_guarded():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "asxbot" / "arena" / "watch.py").read_text(
        encoding="utf-8"
    )
    loop = src[src.index("def _watch(") : src.index("def _poll_and_handle(")]
    assert '_guard("the AI team", _team_tick' in loop


def test_the_lock_counts_the_teams_book(cfg, world, tmp_path):
    from asxbot.arena.lock import open_exposure
    from asxbot.arena.team.runner import team_root

    s = replay(cfg, world, team_root(cfg), FakeTeam(), day=world["days"][4])
    s.run()
    assert s.broker.acct.positions
    ex = open_exposure(cfg.data_dir)
    assert any(x.startswith("asx_team: ") and "AAX" in x for x in ex)


# ------------------------------------------------------------------ evening and calibration
def test_calibration_a_reproduces_the_book_on_the_same_bars(cfg, world, tmp_path):
    """Check A sends the day's own actions to the simulator's broker at their moments: on the
    very bars the book traded on, every order fills the same and the P&L is the same."""
    from asxbot.arena.team.calibrate import same_decisions
    from asxbot.arena.team.runner import team_root

    s = replay(cfg, world, team_root(cfg), FakeTeam())
    res = s.run()
    a = same_decisions(cfg, s.day)
    assert a["ran"] and a["orders"] >= 1 and a["filled_the_same"] == a["orders"]
    assert a["median_sim_worse_bps"] == 0.0
    assert a["sim_pnl"] == pytest.approx(res["pnl"], abs=0.01)


def test_the_evening_block_sets_the_team_beside_the_frozen_books(cfg, world, tmp_path):
    from asxbot.arena.team.runner import team_root
    from asxbot.arena.team.score import report_block, team_score

    s = replay(cfg, world, team_root(cfg), FakeTeam())
    s.run()
    sc = team_score(cfg)
    assert sc.days == 1 and sc.kind == "team"
    facts = {
        "scorecard": [
            {
                "key": "asx_daytrader",
                "accounts": [
                    {
                        "kind": "agent",
                        "today_after_fees": -3.0,
                        "total_after_fees": 10.0,
                        "green_days": 1,
                        "red_days": 0,
                        "trades": 2,
                        "wins_after_fees": 1,
                        "max_drawdown_pct": 0.5,
                        "fees_total": 20.0,
                    }
                ],
            }
        ]
    }
    raw = dict(cfg.raw)
    raw["arena"] = {**raw["arena"], "team": {**raw["arena"]["team"], "start": "2026-07-01"}}
    cfg2 = type(cfg)(raw=raw, root=cfg.root, env=cfg.env, path=cfg.path)
    text = report_block(cfg2, facts, s.day)
    assert "AI team" in text and "day trader agent" in text and "wait for the open" in text


def test_the_report_adds_the_team_only_after_the_frozen_agent_has_written():
    from pathlib import Path

    cli = (Path(__file__).resolve().parents[1] / "src" / "asxbot" / "arena" / "cli.py").read_text(
        encoding="utf-8"
    )
    report = cli[cli.index("def cmd_report") : cli.index("def cmd_journal")]
    assert report.index("call_agent(") < report.index("team_block(")
    assert report.index("written by: {written_by}") < report.index("team_block(")
    brief = (
        Path(__file__).resolve().parents[1] / "src" / "asxbot" / "arena" / "report.py"
    ).read_text(encoding="utf-8")
    assert "arena.team" not in brief and "asx_team" not in brief


def test_the_team_never_reads_the_frozen_books_notes():
    """The team package never names the arena's per-book files (tests/test_journal.py keeps the
    word out of it altogether); its own lessons are the simulator's (lab/tsim/live.py)."""
    from pathlib import Path

    base = Path(__file__).resolve().parents[1] / "src" / "asxbot" / "arena" / "team"
    for p in base.glob("*.py"):
        t = p.read_text(encoding="utf-8").lower()
        assert "journal" not in t, p
        assert "trader-decider" not in t, p


def test_nothing_in_the_team_can_reach_a_real_broker():
    from pathlib import Path

    base = Path(__file__).resolve().parents[1] / "src" / "asxbot" / "arena" / "team"
    for p in base.glob("*.py"):
        t = p.read_text(encoding="utf-8")
        assert "place_order" not in t and "ib_async" not in t and "asxbot.broker" not in t, p
