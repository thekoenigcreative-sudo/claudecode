"""The end-of-day trading journal (Rick, 26 Sep 2026: "do the traders know to write down what
works etc"): one file per book per day, the agents' entries in ONE call each with their own
model, the rule bots' facts from code, three lines in the evening report - and the frozen
test rule: nothing that trades ever reads it."""

import json
import sys
from argparse import Namespace
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from asxbot.arena import cli as C
from asxbot.arena import journal as J
from asxbot.arena import report as R
from asxbot.arena.accounts import ArenaOrder
from asxbot.arena.broker import ArenaBroker
from asxbot.arena.minutes import MinuteBars
from asxbot.arena.runtime import Arena
from asxbot.backtest.costs import CostModel
from asxbot.config import load_config

SYD = ZoneInfo("Australia/Sydney")
DAY = date(2026, 9, 25)
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )


class TArena(Arena):
    def daily_lookup(self):
        return lambda t: None


@pytest.fixture
def arena(cfg):
    evening = datetime(2026, 9, 25, 19, 30, tzinfo=SYD)
    broker = ArenaBroker(cfg.data_dir, CostModel.from_config(cfg), MinuteBars(cfg.data_dir),
                         lambda t: None, clock=lambda: evening)  # fmt: skip
    return TArena(cfg, broker, broker.store, set(), set())


@pytest.fixture(autouse=True)
def instructions(monkeypatch):
    """The decider's standing instructions, without reading the live OpenClaw workspace."""
    monkeypatch.setattr(J, "standing_instructions",
                        lambda cfg: ("# trader-decider\nStanding instructions.", "test"))


def bars(rows, day=DAY) -> pd.DataFrame:
    """rows: (HH:MM, open, high, low, close, volume)."""
    idx = pd.DatetimeIndex(
        [pd.Timestamp(f"{day.isoformat()} {r[0]}", tz=SYD) for r in rows], name="Datetime")
    return pd.DataFrame([r[1:] for r in rows], index=idx,
                        columns=["open", "high", "low", "close", "volume"])  # fmt: skip


def write_bars(data_dir, code, rows, day=DAY):
    p = Path(data_dir) / "arena" / "minutes" / code.lstrip("^") / f"{day.isoformat()}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    bars(rows, day).to_parquet(p)


def at(hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 9, 25, h, m, tzinfo=SYD)


# --------------------------------------------------------------------------
# what the price did after a setup
# --------------------------------------------------------------------------
def test_a_short_that_reached_1r_before_its_stop():
    df = bars([("11:29", 4.535, 4.54, 4.52, 4.53, 900), ("11:30", 4.53, 4.53, 4.50, 4.51, 800),
               ("11:31", 4.51, 4.57, 4.51, 4.56, 700), ("15:49", 4.47, 4.48, 4.46, 4.47, 500),
               ("15:55", 4.40, 4.40, 4.40, 4.40, 500)])  # fmt: skip
    p = J.price_path(df, DAY, at("11:29"), "short", 4.535, 4.565)
    assert p["first"] == "+1R" and p["plus_1r"] == "11:30" and p["stop_touched"] == "11:31"
    assert p["plus_2r"] == "15:49"  # 4.46 is 2.5R below; the 15:55 bar is after the flat time
    assert p["r_at_1550"] == pytest.approx((4.535 - 4.47) / 0.03, abs=0.01)
    assert "before costs, no trade management" in p["line"]


def test_a_bar_that_touched_both_counts_the_stop_first_and_dead_bars_touch_nothing():
    df = bars([("11:00", 10.0, 10.0, 9.0, 10.0, 0),  # no trade: its low touches nothing
               ("11:01", 10.0, 10.3, 9.85, 10.1, 100)])  # fmt: skip
    p = J.price_path(df, DAY, at("11:00"), "buy", 10.0, 9.9)
    assert p["first"] == "stop" and p["stop_touched"] == "11:01" == p["plus_1r"]
    assert p["from"] == "11:01"


def test_no_usable_stop_or_no_bars_is_no_path():
    df = bars([("11:00", 10.0, 10.1, 9.9, 10.0, 100)])
    assert J.price_path(df, DAY, at("11:00"), "buy", 10.0, 10.2) is None  # stop above a long
    assert J.price_path(df, DAY, at("11:00"), "short", 10.0, None) is None
    assert J.price_path(None, DAY, at("11:00"), "buy", 10.0, 9.9) is None


def test_the_index_move_counts_bars_with_no_volume():
    idx = bars([("10:10", 8700, 8700, 8700, 8700, 0), ("15:49", 8656.5, 8660, 8650, 8656.5, 0)])
    assert J.move_after(idx, DAY, at("10:10")) is None  # as a stock: nothing traded
    m = J._index_move(idx, DAY, at("10:10"))
    assert m["to_1550_pct"] == pytest.approx(-0.5)


# --------------------------------------------------------------------------
# a day on the books
# --------------------------------------------------------------------------
def order(acct, oid, ticker, side, qty, price, minute, fee, *, stop=None, reason="",
          by="agent", otype="limit", realised=0.0):  # fmt: skip
    acct.orders[oid] = ArenaOrder(
        order_id=oid, account=acct.name, ticker=ticker, side=side, qty=qty, limit=price,
        decided_at=f"2026-09-25T{minute}:00+10:00", status="filled", filled_qty=qty,
        avg_price=price, commission=fee, fill_minute=f"2026-09-25T{minute}+10:00",
        fills=[{"minute": f"2026-09-25T{minute}+10:00", "qty": qty, "price": price,
                "bar_price": price * (1.001 if side in ("sell", "short") else 0.999)}],
        stop=stop, reason=reason, placed_by=by, order_type=otype, realised=realised,
        model="claude-opus-5-5" if by == "agent" else "none (rule-based bot)",
    )  # fmt: skip


def write_events(data_dir, kind, rows):
    p = Path(data_dir) / "events" / f"{kind}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps({"kind": kind, **r}) + "\n")


@pytest.fixture
def day_on_the_books(cfg, arena):
    """25 Sep in miniature: the day trader's agent took REG and rejected CSC, could not be
    asked about one setup and never saw a stale one; its bot took NWL. v2's agent passed HLS
    before the open and MXT at its reaction look; one stock was never looked at; its bot's
    rule fired on NWL."""
    d = cfg.data_dir
    dt, v2 = arena.playbook("asx_daytrader"), arena.playbook("asx_announcements_v2")
    ag, bot = arena.account(dt, "agent"), arena.account(dt, "bot")
    order(ag, "ARN-10", "REG", "short", 1111, 4.5165, "11:31", 6.60, stop=4.57,
          reason="[vwap_reclaim] agent confirmed: REG shows stock-specific weakness")
    order(ag, "ARN-12", "REG", "cover", 1111, 4.4749, "15:51", 6.60, by="agent",
          reason="flat at the close: ASX day trader holds intraday only", realised=46.17)
    order(bot, "ARN-6", "NWL", "short", 208, 17.6223, "11:21", 6.60, stop=18.07, by="bot",
          reason="[opening_range_breakout] rule: closed 17.77 below the 30-min low 17.8")
    order(bot, "ARN-14", "NWL", "cover", 208, 16.8954, "15:51", 13.20, by="bot",
          reason="flat at the close", realised=151.20)
    for a in (ag, bot):
        arena.store.save(a)
    write_events(d, "arena_orders", [
        {"ts": "2026-09-25T05:49:30+00:00", "account": ag.name, "ticker": "REG",
         "event": "stop_moved", "from": 4.57, "to": 4.5165, "bar": "2026-09-25T15:49+10:00",
         "r_gained": 1.06}])  # fmt: skip
    signals = [
        {"at": "2026-09-25T11:27:59+10:00", "ticker": "REG", "setup": "vwap_reclaim",
         "side": "short", "last": 4.535, "stop": 4.565, "rvol": 1.33, "why": "closed below VWAP",
         "bot": {"skipped": "the account is full (3 open or working)"},
         "agent": {"order_id": "ARN-10", "seconds": 23.7}},
        {"at": "2026-09-25T11:31:10+10:00", "ticker": "CSC", "setup": "vwap_reclaim",
         "side": "short", "last": 14.43, "stop": 14.46, "rvol": 0.7, "why": "closed below VWAP",
         "bot": {"skipped": "the account is full (3 open or working)"},
         "agent": {"rejected": "CSC's weakness is materials-sector beta", "seconds": 20.0}},
        {"at": "2026-09-25T12:00:00+10:00", "ticker": "MIN", "setup": "vwap_reclaim",
         "side": "short", "last": 52.72, "stop": 52.83, "rvol": 1.0, "why": "x",
         "agent": {"unavailable": "usage_limit", "rejected": "agent unavailable (usage limit)"}},
        {"at": "2026-09-25T10:53:55+10:00", "ticker": "EOS", "setup": "opening_range_breakout",
         "side": "buy", "last": 11.32, "stop": 10.99, "rvol": 2.4, "why": "x", "bot": None,
         "agent": None, "skipped": "stale: the trigger bar is 6 bars old"},
        {"at": "2026-09-25T11:20:47+10:00", "ticker": "NWL", "setup": "opening_range_breakout",
         "side": "short", "last": 17.77, "stop": 18.07, "rvol": 3.0, "why": "closed below",
         "bot": {"order_id": "ARN-6"}, "agent": {"skipped": "stale by its turn: 5 bars old"}},
    ]  # fmt: skip
    p = d / "arena" / "daytrader" / "2026-09-25.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"signals": signals, "universe": ["REG", "CSC"]}), encoding="utf-8")
    write_bars(d, "CSC", [("11:32", 14.43, 14.44, 14.40, 14.42, 500),
                          ("11:44", 14.44, 14.47, 14.43, 14.46, 500)])  # fmt: skip
    write_bars(d, "REG", [("11:29", 4.53, 4.54, 4.50, 4.51, 900)])
    write_bars(d, "HLS", [("10:01", 0.405, 0.42, 0.40, 0.41, 5000),
                          ("15:49", 0.377, 0.38, 0.375, 0.377, 5000)])  # fmt: skip
    write_bars(d, "^AXJO", [("10:00", 8700, 8700, 8700, 8700, 0),
                            ("15:49", 8665, 8665, 8665, 8665, 0)])  # fmt: skip
    q = {"_seeded": [], "MXT": {"ticker": "MXT", "status": "looked", "headlines": ["Distribution"],
                                "reaction": {"as_of_bar": "10:16", "move_vs_index_pct": 0.47},
                                "outcome": {"decision": "pass", "why": "no reaction to trade"}},
         "PEN": {"ticker": "PEN", "status": "quiet", "headlines": ["Impairment"],
                 "why": "no previous close in the minute cache"},
         "HLS": {"ticker": "HLS", "status": "quiet", "headlines": ["Agilex sale"],
                 "why": "no previous close in the minute cache"}}  # fmt: skip
    p = d / "arena" / "reaction" / "2026-09-25.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(q), encoding="utf-8")
    write_events(d, "arena_decisions", [
        # worked twice (a watcher restart): the later record is the look
        {"ts": "2026-09-24T22:40:00+00:00", "stage": "decider", "v2": "pre_open",
         "ticker": "HLS", "ids_id": "03143799", "decision": {"action": "pass", "why": "first"}},
        {"ts": "2026-09-24T22:52:55+00:00", "stage": "decider", "v2": "pre_open",
         "ticker": "HLS", "ids_id": "03143799",
         "decision": {"action": "pass", "why": "the reaction look will show"}},
    ])  # fmt: skip
    vb, vbot = arena.account(v2, "agent"), arena.account(v2, "bot")
    order(vbot, "ARN-4", "NWL", "short", 285, 17.8656, "10:54", 6.60, stop=18.34, by="bot",
          reason="v2 rule: -3.15% vs the ASX 200 at 10:30 on 6.1x")
    order(vbot, "ARN-18", "NWL", "cover", 285, 16.987, "15:56", 6.60, by="bot",
          reason="flat at the close", realised=250.41)
    arena.store.save(vb)
    arena.store.save(vbot)
    p = d / "arena" / "v2bot" / "2026-09-25.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"status": "done", "decided_at": "2026-09-25T10:53:55+10:00",
                             "candidates": [{"ticker": "NWL", "signal": True, "why": "-3.15%"},
                                            {"ticker": "BMN", "signal": False, "why": "2.0x"},
                                            {"ticker": "AMD", "screened": "turnover 75895"}]}),
                 encoding="utf-8")  # fmt: skip
    return arena


class FakeModel:
    """Stands in for the one `claude -p` call: counts calls, keeps what it was asked."""

    def __init__(self, ok=True, kind=""):
        self.calls, self.ok, self.kind = [], ok, kind

    def __call__(self, prompt, system, model, effort, timeout_s=600):
        self.calls.append({"prompt": prompt, "system": system, "model": model, "effort": effort})
        n = len(self.calls)
        text = (f"SETUPS\n- took REG, skipped CSC\nTRADES\n...\nLESSON: lesson number {n}"
                if self.ok else "")  # fmt: skip
        return {"ok": self.ok, "kind": self.kind, "reason": "" if self.ok else "limit reached",
                "text": text, "seconds": 41.2, "model_ran": "claude-opus-5-5",
                "input_tokens": 9000, "output_tokens": 700, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "cost_usd_list": 0.19, "weekly_usage": 0.41,
                "five_hour_usage": 0.1, "limit_status": "allowed", "is_error": not self.ok,
                "api_ms": 40000}  # fmt: skip


def test_the_day_trader_agents_setups_and_trades_from_its_side(cfg, day_on_the_books):
    arena = day_on_the_books
    f = J.book_facts(cfg, arena, arena.playbook("asx_daytrader"), "agent", DAY)
    (reg,) = f["trades"]
    assert reg["ticker"] == "REG" and reg["side"] == "short" and reg["fees"] == 13.20
    assert reg["risk_aud"] == pytest.approx((4.57 - 4.5165) * 1111, abs=0.01)
    assert reg["r_net"] == pytest.approx(reg["net"] / reg["risk_aud"], abs=0.01)
    assert reg["entry_reason"].startswith("[vwap_reclaim] agent confirmed")
    # costs are brokerage AND slippage, each shown (25 Sep's first journal: slippage hidden in
    # the prices made the agent call its correct ~$23 round trip a mistake)
    slip = 1111 * 4.5165 * 0.001 + 1111 * 4.4749 * 0.001
    assert reg["slippage"] == pytest.approx(slip, abs=0.02) and reg["slippage_known"]
    assert reg["costs_all_in"] == pytest.approx(13.20 + slip, abs=0.02)
    assert reg["entry_limit"] == 4.5165 and reg["decided"] == "11:31"
    # a stop moved by code is never the agent's (it wrote "I tightened the stop" on 25 Sep)
    (moved,) = reg["management"]
    assert moved.startswith("code moved the stop 4.57 -> 4.516 at 15:49")
    assert "not the agent" in moved
    text = "\n".join(J.facts_lines(f))
    assert "brokerage 13.20 + slippage" in text and "(in the fill prices)" in text
    # the day trader's limit and size are code's; the agent only confirms (25 Sep: it called
    # the rule's 1% limit its own error)
    assert reg["limit_by"].startswith("set by code: the frozen rule's 1.0% through")
    assert "the agent only confirmed" in text
    assert J.limit_setter(arena.playbook("asx_announcements_v2"), "agent").startswith(
        "the agent's own limit")
    assert J.limit_setter(arena.playbook("asx_daytrader"), "bot").startswith("set by the frozen")
    took, skipped = f["setups_decided"]
    assert took["ticker"] == "REG" and took["decision"] == "took"
    assert took["why"] == "REG shows stock-specific weakness"  # its own words, prefix dropped
    assert skipped["ticker"] == "CSC" and skipped["decision"] == "skipped"
    assert skipped["after"]["first"] == "stop" and skipped["after"]["stop_touched"] == "11:44"
    # never the agent's to decide: counted, never called a rejection
    nd = f["setups_not_decided"]
    assert nd["agent unavailable (usage_limit)"] == 1
    assert nd["stale: the trigger bar is N bars old"] == 1
    assert nd["stale by its turn: N bars old"] == 1
    assert f["decisions"] == 2
    assert f["yardstick"]["trades"][0]["ticker"] == "NWL"
    assert f["result"]["round_trips_closed"] == 1 and f["result"]["wins_after_fees"] == 1


def test_the_rule_bots_facts_have_trades_outcome_r_and_fees(cfg, day_on_the_books):
    arena = day_on_the_books
    f = J.book_facts(cfg, arena, arena.playbook("asx_daytrader"), "bot", DAY)
    (nwl,) = f["trades"]
    assert nwl["net"] == pytest.approx(151.20 - 19.80) and nwl["fees"] == pytest.approx(19.80)
    assert nwl["r_net"] == pytest.approx(nwl["net"] / ((18.07 - 17.6223) * 208), abs=0.01)
    assert [s["decision"] for s in f["setups_decided"]] == ["skipped", "skipped", "took"]
    v = J.book_facts(cfg, arena, arena.playbook("asx_announcements_v2"), "bot", DAY)
    assert v["rule"]["signals"] == [{"ticker": "NWL", "why": "-3.15%"}]
    assert v["rule"]["screened_out"] == 1 and v["trades"][0]["r_net"] is not None


def test_the_v2_agents_looks_and_the_stocks_it_never_saw(cfg, day_on_the_books):
    arena = day_on_the_books
    f = J.book_facts(cfg, arena, arena.playbook("asx_announcements_v2"), "agent", DAY)
    (pre,) = f["pre_open_looks"]
    assert pre["ticker"] == "HLS" and pre["decision"] == "pass" and pre["at"] == "08:52"
    assert pre["why"] == "the reaction look will show"
    assert "(0.41) to 15:50 -8.05%" in pre["after"] and "ASX 200 -0.40%" in pre["after"]
    (look,) = f["reaction_looks"]
    assert look["ticker"] == "MXT" and look["decision"] == "pass"
    assert {x["ticker"] for x in f["never_looked"]} == {"PEN", "HLS"}
    assert f["decisions"] == 2


def test_one_file_per_book_the_frozen_rule_and_one_call_per_agent_per_day(cfg,
                                                                          day_on_the_books):
    arena = day_on_the_books
    model = FakeModel()
    paths = J.run(cfg, arena, DAY, ask=model)
    names = sorted(p.name for p in paths)
    assert names == sorted(["asx_daytrader_v1__agent.md", "asx_daytrader_v1__bot.md",
                            "asx_announcements_v2__agent.md", "asx_announcements_v2__bot.md"])
    assert all(p.parent == cfg.data_dir / "arena" / "journal" / "2026-09-25" for p in paths)
    assert len(model.calls) == 2  # one per agent book, none for the bots
    for c in model.calls:
        assert c["model"] == "anthropic/claude-opus-5-5" and c["effort"] == "high"
        assert "Standing instructions." in c["system"]
        assert "no DECISION block" in c["prompt"] and "LESSON:" in c["prompt"]
        assert "never shown to you or any agent when deciding" in c["prompt"]
    for p in paths:
        text = p.read_text(encoding="utf-8")
        assert "FROZEN TEST RULE: this journal is written only" in text
        assert "never fed back into the agents' prompts or decisions" in text
        assert "(Fri 25 Sep - Thu 08 Oct 2026)" in text
        assert "LOCKED TEST window" in text  # 25 Sep is sealed for the Practice Lab
    ag = (cfg.data_dir / "arena" / "journal" / "2026-09-25" / "asx_daytrader_v1__agent.md")
    data = J.read_file(ag)
    assert data["lesson"].startswith("lesson number") and data["entry"].startswith("SETUPS")
    assert data["call"]["input_tokens"] == 9000 and "entry_text" not in data["call"]
    bot = J.read_file(cfg.data_dir / "arena" / "journal" / "2026-09-25"
                      / "asx_daytrader_v1__bot.md")  # fmt: skip
    assert bot["written_by"] == "code" and bot["call"] is None
    # usage is logged: a started and a done record per agent book
    recs = [json.loads(x) for x in (cfg.data_dir / "events" / "arena_journal_calls.jsonl")
            .read_text(encoding="utf-8").splitlines()]  # fmt: skip
    assert [r["stage"] for r in recs] == ["started", "done", "started", "done"]
    done = recs[1]
    assert done["ok"] and done["model_ran"] == "claude-opus-5-5" and done["model_matches"]
    assert done["cost_usd_list"] == 0.19 and done["weekly_usage"] == 0.41
    assert done["via"].startswith("claude -p") and "not OpenClaw" in done["via"]
    assert all(r["kind"] == "arena_journal_calls" for r in recs)  # never overwritten

    # the evening runs again (a restart): nobody is asked twice, and the entries stay
    J.run(cfg, arena, DAY, ask=model)
    assert len(model.calls) == 2
    assert J.read_file(ag)["entry"].startswith("SETUPS")
    # facts only: no call, the entry kept
    J.run(cfg, arena, DAY, agent=False, ask=model)
    assert len(model.calls) == 2 and J.read_file(ag)["lesson"].startswith("lesson number")
    # asked again on purpose: logged as such
    J.run(cfg, arena, DAY, again=True, ask=model)
    assert len(model.calls) == 4
    recs = [json.loads(x) for x in (cfg.data_dir / "events" / "arena_journal_calls.jsonl")
            .read_text(encoding="utf-8").splitlines()]  # fmt: skip
    assert recs[-1]["again"] is True


def test_a_paid_call_is_not_lost_when_the_file_never_got_it(cfg, day_on_the_books):
    arena = day_on_the_books
    model = FakeModel()
    J.run(cfg, arena, DAY, ask=model)
    ag = cfg.data_dir / "arena" / "journal" / "2026-09-25" / "asx_daytrader_v1__agent.md"
    ag.unlink()
    J.run(cfg, arena, DAY, ask=model)
    assert len(model.calls) == 2
    assert J.read_file(ag)["entry"].startswith("SETUPS")  # from the call's own record


def test_a_failed_call_is_said_plainly_and_not_retried(cfg, day_on_the_books):
    arena = day_on_the_books
    model = FakeModel(ok=False, kind="usage_limit")
    J.run(cfg, arena, DAY, ask=model)
    ag = cfg.data_dir / "arena" / "journal" / "2026-09-25" / "asx_daytrader_v1__agent.md"
    text = ag.read_text(encoding="utf-8")
    assert "No entry: the call failed (usage_limit)" in text
    J.run(cfg, arena, DAY, ask=model)
    assert len(model.calls) == 2
    lines = J.summary_lines(cfg.data_dir, DAY, arena.playbooks())
    assert any("Day trader agent: no entry: the call failed (usage_limit)" in x for x in lines)
    # not a trading call: the report's "the agent could not be asked" stays at zero
    assert R.agent_unavailable_facts(cfg, DAY)["calls_failed"] == 0


def test_a_book_the_agent_decided_nothing_in_gets_no_call(cfg, arena):
    model = FakeModel()
    J.run(cfg, arena, DAY, ask=model)
    assert model.calls == []
    lines = J.summary_lines(cfg.data_dir, DAY, arena.playbooks())
    assert "Day trader agent: no decisions today - nothing was put to the agent" in lines
    assert lines[-1].startswith("Rule bots: ")


def test_three_lines_for_the_evening_report(cfg, day_on_the_books):
    arena = day_on_the_books
    J.run(cfg, arena, DAY, ask=FakeModel())
    lines = J.summary_lines(cfg.data_dir, DAY, arena.playbooks())
    assert len(lines) == 3
    assert {x.split(":")[0] for x in lines[:2]} == {"Day trader agent", "Announcements v2 agent"}
    assert lines[2].startswith("Rule bots: ")
    assert "day trader 1 trade +131.40 after fees" in lines[2]
    block = J.report_block(cfg.data_dir, DAY, arena.playbooks())
    assert block.startswith("<b>Journal - the day's lessons</b>")
    assert "no agent reads it during the test" in block and block.count("\n- ") == 3
    # a day with no journal says so; a weekend says nothing
    assert "none written" in J.report_block(cfg.data_dir, date(2026, 9, 28), arena.playbooks())
    assert J.report_block(cfg.data_dir, date(2026, 9, 26), arena.playbooks()) == ""
    tg = J.telegram_text(cfg.data_dir, DAY, arena.playbooks())
    assert tg.startswith("📓 <b>Trading journal - Fri 25 Sep</b>") and "Written only" in tg


def test_the_practice_lab_reads_them_back(cfg, day_on_the_books):
    arena = day_on_the_books
    J.run(cfg, arena, DAY, ask=FakeModel())
    got = J.load(cfg.data_dir, since=DAY, until=DAY)
    assert len(got) == 4 and all(g["locked_test_day"] for g in got)
    by = {g["account"]: g for g in got}
    assert by["asx_daytrader_v1__bot"]["facts"]["trades"][0]["ticker"] == "NWL"
    assert J.load(cfg.data_dir, since=date(2026, 9, 28)) == []


def test_the_test_window_is_the_10_trading_days(cfg):
    from asxbot.arena.levels import load_playbook

    assert J.test_window(load_playbook(cfg, "asx_daytrader")) == (DAY, date(2026, 10, 8))


# --------------------------------------------------------------------------
# the evening report carries the lines; no agent ever reads them
# --------------------------------------------------------------------------
class Evening(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 25, 19, 35, tzinfo=SYD)


def test_the_report_gets_the_journal_after_the_writer_and_the_agent_never_sees_it(
        cfg, day_on_the_books, monkeypatch, capsys):  # fmt: skip
    arena = day_on_the_books
    J.run(cfg, arena, DAY, ask=FakeModel())
    monkeypatch.setattr(R, "datetime", Evening)
    monkeypatch.setattr(R, "asx200_provenance", lambda *a, **k: {"list": "x", "as_of": None,
                                                                   "members": 0})  # fmt: skip
    monkeypatch.setattr(R, "archive_status", lambda *a, **k: "n/a")
    import logging

    monkeypatch.setattr(C, "_arena", lambda *a, **k: (cfg, logging.getLogger("t"), arena))
    briefs = []

    def fake_call(agent, message, **kw):
        briefs.append(message)
        from asxbot.arena.agents import AgentReply

        return AgentReply(agent, "<b>The report</b>", "claude-opus-5-5", "", "high", "r", 1,
                          True, {})  # fmt: skip

    monkeypatch.setattr("asxbot.arena.agents.call_agent", fake_call)
    assert C.cmd_report(Namespace(agent=True, send=False)) == 0
    out = capsys.readouterr().out
    assert out.index("written by: claude-opus-5-5") < out.index("Journal - the day's lessons")
    assert "lesson number" in out
    (brief,) = briefs
    assert "journal" not in brief.lower() and "lesson number" not in brief


def test_no_trading_code_reads_the_journal():
    """FROZEN TEST RULE, held in code: outside the journal itself, only the CLI (the journal
    command, and the report's code-written lines after the agent has written) and the evening
    launcher name it. Not the watcher, the day trader, v2, the agents, the prompts, the chat,
    nor the agents' standing instructions. The Practice Lab (src/asxbot/lab) is the one reader
    by design: it trades nothing live and never builds a live agent's prompt."""
    allowed = {REPO / "src" / "asxbot" / "arena" / "journal.py",
               REPO / "src" / "asxbot" / "arena" / "cli.py",
               REPO / "scripts" / "arena_evening.pyw"}  # fmt: skip
    offenders = []
    for base, pats in ((REPO / "src", ("*.py",)), (REPO / "scripts", ("*.py", "*.pyw")),
                       (REPO / "docs" / "agents", ("*.md",))):  # fmt: skip
        for pat in pats:
            for p in base.rglob(pat):
                if p in allowed or "__pycache__" in p.parts:
                    continue
                if p.is_relative_to(REPO / "src" / "asxbot" / "lab"):
                    continue
                if "journal" in p.read_text(encoding="utf-8", errors="replace").lower():
                    offenders.append(str(p.relative_to(REPO)))
    assert offenders == []
    cli = (REPO / "src" / "asxbot" / "arena" / "cli.py").read_text(encoding="utf-8")
    report = cli[cli.index("def cmd_report"):cli.index("def cmd_journal")]
    # in the report, the journal comes after the agent's call and its text are settled
    assert report.index("call_agent(") < report.index("report_block(")
    assert report.index("written by: {written_by}") < report.index("report_block(")


# --------------------------------------------------------------------------
# the one call: `claude -p`, no tools, nothing kept
# --------------------------------------------------------------------------
FAKE_CLAUDE = r"""
import json, os, sys
args = sys.argv[1:]
sysfile = args[args.index("--system-prompt-file") + 1]
rec = {"args": args, "cwd": os.getcwd(), "stdin": sys.stdin.read(),
       "system": open(sysfile, encoding="utf-8").read(),
       "claudecode": os.environ.get("CLAUDECODE")}
with open(os.environ["FAKE_CLAUDE_LOG"], "w", encoding="utf-8") as fh:
    json.dump(rec, fh)
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
print(json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-5-5"}))
status = "rejected" if mode == "limit" else "allowed"
print(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": status,
      "unifiedWindows": {"seven_day": {"utilization": 0.42}, "five_hour": {"utilization": 0.1}}}}))
if mode == "limit":
    print(json.dumps({"type": "result", "is_error": True,
                      "result": "You've hit your weekly limit"}))
    sys.exit(1)
print(json.dumps({"type": "result", "is_error": False, "result": "SETUPS\n...\nLESSON: be patient",
                  "total_cost_usd": 0.2134, "duration_ms": 30500, "duration_api_ms": 30000,
                  "usage": {"input_tokens": 8123, "output_tokens": 812,
                            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                  "modelUsage": {"claude-opus-5-5": {"outputTokens": 812}}}))
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    log = tmp_path / "fake_claude.json"
    monkeypatch.setenv("ASXBOT_JOURNAL_CLAUDE", str(script))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("CLAUDECODE", "1")  # a Claude Code shell: the child must not inherit it
    return log


def test_the_call_runs_the_model_with_no_tools_and_keeps_nothing(fake_claude):
    r = J.ask_model("the facts", "the instructions", "anthropic/claude-opus-5-5", "high")
    assert r["ok"] and r["text"].endswith("LESSON: be patient")
    assert r["model_ran"] == "claude-opus-5-5" and r["input_tokens"] == 8123
    assert r["output_tokens"] == 812 and r["cost_usd_list"] == 0.2134
    assert r["weekly_usage"] == 0.42 and r["api_ms"] == 30000
    rec = json.loads(fake_claude.read_text(encoding="utf-8"))
    a = rec["args"]
    assert a[a.index("--model") + 1] == "claude-opus-5-5" and a[a.index("--effort") + 1] == "high"
    assert a[a.index("--tools") + 1] == "" and a[a.index("--setting-sources") + 1] == ""
    for flag in ("-p", "--no-session-persistence", "--strict-mcp-config",
                 "--disable-slash-commands"):  # fmt: skip
        assert flag in a
    assert rec["stdin"] == "the facts" and rec["system"] == "the instructions"
    assert rec["claudecode"] is None
    assert "asxbot-journal-" in rec["cwd"] and not Path(rec["cwd"]).exists()  # scratch, gone


def test_a_refused_call_is_a_usage_limit(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "limit")
    r = J.ask_model("x", "y", "anthropic/claude-opus-5-5", "high")
    assert not r["ok"] and r["kind"] == "usage_limit" and r["weekly_usage"] == 0.42


def test_the_lesson_line_is_read_from_the_end():
    assert J.parse_lesson("a\n**LESSON:** size up on clean breaks\n") == "size up on clean breaks"
    assert J.parse_lesson("no lesson here") == ""


# --------------------------------------------------------------------------
# the command
# --------------------------------------------------------------------------
def test_the_command_refuses_a_day_that_is_not_over(cfg, monkeypatch, capsys):
    class Noon(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 25, 12, 0, tzinfo=SYD)

    monkeypatch.setattr(C, "datetime", Noon)
    monkeypatch.setattr(C, "load_config", lambda *a, **k: cfg)
    args = Namespace(day=None, no_agent=False, again=False, send=False)
    assert C.cmd_journal(args) == 3 and "REFUSED" in capsys.readouterr().out
    assert not (cfg.data_dir / "arena" / "journal").exists()
    args.day = "2026-09-26"  # a Saturday, and in the future at noon on the 25th
    assert C.cmd_journal(args) == 3
    assert sys.modules["asxbot.arena.journal"] is J
