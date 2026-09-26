"""The Practice Lab (PRACTICE_LAB.md, WINNER.md): the time machine with a variant and the lab's
agent, anonymisation, the locked test's seal, the variant whitelist, the gates and WINNER.md.
No network, no model: the agent's answers come from a stand-in."""

import json
import re
from datetime import date, datetime
from pathlib import Path

import pytest

import test_replay_ibkr as TR
from asxbot.lab import agentcall, anon, runner, score, sim, splits, store, variants, winner
from asxbot.lab.variants import Registry
from test_replay_ibkr import CODES, DAY, _ann

cfg = TR.cfg  # the replay tests' fixtures
hist = TR.hist

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def lab_local(tmp_path, monkeypatch):
    d = tmp_path / "lab-local"
    monkeypatch.setenv("ASXBOT_LAB_LOCAL", str(d))
    return d


# --- WINNER.md is the code ---------------------------------------------------------------------


def test_winner_md_and_the_code_agree():
    text = " ".join((ROOT / "WINNER.md").read_text(encoding="utf-8").split())  # line breaks aside
    c = winner.CRITERIA
    for needle in (
        f"at least **{c['shadow_min_days']} shadow trading days**",
        f"at least **{c['shadow_min_trades']} shadow trades**",
        f"at least **{c['locked_min_trades']} trades**",
        f"best **{c['top_n']}** trades",
        f"at most **{c['top_share_max_pct']:.0f}%**",
        "at least 5 days of each",
        f"at most **{c['max_drawdown_pct']:.0f}%**",
        "**-3%**",
        f"**{c['t_base']} + {c['t_per_ln_final']} x ln(F)**",
        f"at most **{c['max_params']}** changed parameters",
        f"at most **{c['max_prompt_chars']}**",
    ):
        assert needle in text, needle
    assert (
        c["max_params"] == variants.MAX_PARAMS
        and c["max_prompt_chars"] == variants.MAX_PROMPT_CHARS
    )
    for name, w in splits.WINDOWS.items():
        if w.last:
            assert f"{w.first} to {w.last}" in text, name
    assert "sqrt(2 x ln(N))" in text and splits.CUTOFF.isoformat() in text


# --- the locked test's seal ---------------------------------------------------------------------


def test_the_locked_test_is_sealed(tmp_path):
    with pytest.raises(splits.LockedTestSealed):
        splits.check_days([date(2026, 9, 1)])
    splits.check_days([date(2026, 7, 15), date(2026, 5, 4)])
    v = {"id": "dt-0001", "stage": "shadow"}
    with pytest.raises(splits.LockedTestSealed):
        splits.unseal(tmp_path, v, "early")
    v["stage"] = "final"
    assert splits.unseal(tmp_path, v, "final") == 1
    with pytest.raises(splits.LockedTestSealed):
        splits.unseal(tmp_path, v, "again")
    assert splits.unseal(tmp_path, {"id": "dt-0002", "stage": "final"}, "final") == 2
    assert [r["variant"] for r in splits.locked_runs(tmp_path)] == ["dt-0001", "dt-0002"]


def test_windows_are_fixed_and_the_tune_window_is_before_the_cutoff():
    w = splits.WINDOWS
    assert w["tune"].last < splits.CUTOFF <= w["validate"].first
    assert w["validate"].last < w["locked"].first and w["locked"].last < w["shadow"].first
    assert (
        splits.window_of(date(2026, 8, 20)) == "locked"
        and splits.window_of(date(2026, 10, 1)) == "shadow"
    )


# --- variants ------------------------------------------------------------------------------------

BASE = {
    "risk_per_trade_pct": 0.5,
    "universe": {"max_order_share_of_turnover": 0.05},
    "yardstick": {"size_aud": 5000},
}


def test_the_whitelist_and_bounds():
    ok = {
        "manage.trail_distance_r": 1.5,
        "setups.vwap_reclaim.window": ["11:00", "15:00"],
        "setups.halt_resumption.enabled": False,
        "risk_per_trade_pct": 0.3,
    }
    assert variants.check("asx_daytrader", ok, "", BASE) == []
    bad = variants.check(
        "asx_daytrader",
        {
            "risk_per_trade_pct": 0.8,
            "costs.brokerage_pct": 0.01,
            "entry.limit_slack_pct": 9,
            "last_entry_time": "16:30",
            "level": 3,
        },
        "",
        BASE,
    )
    assert any("risk_per_trade_pct" in x for x in bad) and any(
        "costs.brokerage_pct" in x for x in bad
    )
    assert any("limit_slack_pct" in x for x in bad) and any("last_entry_time" in x for x in bad)
    assert any("level may not" in x for x in bad)
    assert variants.check(
        "asx_daytrader",
        {"setups.gap_and_go.min_rvol": 2.0, "a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "f": 6},
        "",
        BASE,
    )[0].startswith("7 changed")
    assert "characters" in variants.check("asx_daytrader", {}, "x" * 601, BASE)[0]
    assert variants.check("asx_announcements_v2", {}, "be bolder", BASE)  # v2's agent is phase 2


def test_registry_counts_refuses_duplicates_and_catches_edits(tmp_path):
    reg = Registry(tmp_path)
    v = reg.register(
        "asx_daytrader", {"manage.trail_distance_r": 1.5}, why="looser trail", base_raw=BASE
    )
    assert v["id"] == "dt-0001" and v["stage"] == "registered" and v["books"] == ["bot"]
    with pytest.raises(variants.BadVariant):
        reg.register("asx_daytrader", {"manage.trail_distance_r": 1.5}, base_raw=BASE)
    a = reg.register(
        "asx_daytrader", {}, agent_prompt="Reject vwap reclaims into the close.", base_raw=BASE
    )
    assert a["books"] == ["bot", "agent"]
    p = tmp_path / "variants" / "dt-0001.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["overrides"]["manage.trail_distance_r"] = 2.0  # tuned after seeing a result: refused
    p.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(variants.BadVariant):
        reg.load("dt-0001")
    lines = (tmp_path / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_apply_never_touches_the_frozen_block(cfg):
    from asxbot.arena.levels import load_playbook

    pb = load_playbook(cfg, "asx_daytrader")
    before = json.dumps(pb.raw, sort_keys=True, default=str)
    pv = variants.apply(pb, {"manage.trail_distance_r": 1.5})
    assert pv.raw["manage"]["trail_distance_r"] == 1.5
    assert (
        json.dumps(load_playbook(cfg, "asx_daytrader").raw, sort_keys=True, default=str) == before
    )
    assert json.dumps(pb.raw, sort_keys=True, default=str) == before


# --- anonymisation -------------------------------------------------------------------------------


def test_anonymised_numbers_names_and_dates():
    a = anon.Anon(date(2026, 5, 12), "salt", 31)
    assert (
        a.alias("BHP") == a.alias("bhp")
        and a.alias("BHP") != a.alias("RIO")
        and re.fullmatch(r"S[0-9A-F]{4}", a.alias("BHP"))
    )
    f = a.factor("BHP")
    assert 0.5 <= f <= 2.0 and a.factor("BHP") != anon.Anon(date(2026, 5, 13), "salt", 32).factor(
        "BHP"
    )
    assert a.unprice("BHP", a.price("BHP", 45.12)) == pytest.approx(45.12)
    t = a.text(
        "BHP",
        "gap +3.2% vs index, first close (45.12) above the 15-min high 45.00 on 2.1x, RVOL 2.4; "
        "BHP at 10:31 on 2026-05-12",
    )
    assert "+3.2%" in t and "2.1x" in t and "RVOL 2.4" in t and "10:31" in t
    assert "45.12" not in t and "BHP" not in t and "2026-05-12" not in t and "Day 31" in t
    assert (
        a.headline("BHP Group Quarterly Activities Report") == "[name] Quarterly Activities Report"
    )
    assert (
        a.news_row("Mon 11 May 16:30 [price sensitive] Northern Star Resources half year results")
        == "the previous day 16:30 [price sensitive] [name] half year results"
    )
    terms = {"qty": 1000, "limit": 10.0, "stop": 9.8, "risk": 200.0, "value": 10000.0, "last": 9.95}
    t2 = a.terms("QUI", terms)
    assert t2["value"] == 10000.0 and t2["risk"] == 200.0
    assert t2["qty"] * t2["limit"] == pytest.approx(10000.0, rel=0.01)


# --- the lab's model calls ----------------------------------------------------------------------

FAKE = r"""import json, os, sys
packet = sys.stdin.read()
with open(os.environ["FAKE_OUT"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "packet": packet}) + "\n")
mode = os.environ.get("FAKE_MODE", "take")
win = {"seven_day": {"utilization": 0.4}, "five_hour": {"utilization": 0.1}}
ev = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": win}}
print(json.dumps(ev))
if mode == "limit":
    msg = "You've hit your weekly limit \u00b7 resets Sep 29, 3pm (Australia/Sydney)"
    print(json.dumps({"type": "result", "is_error": True, "result": msg}))
    sys.exit(1)
answer = 'Looks fine.\n{"action": "%s", "stop": null, "why": "clean break"}' % mode
print(json.dumps({"type": "result", "is_error": False, "duration_ms": 12000,
                  "total_cost_usd": 0.03, "result": answer}))
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    exe = tmp_path / "fake_claude.py"
    exe.write_text(FAKE, encoding="utf-8")
    out = tmp_path / "calls.jsonl"
    monkeypatch.setenv("ASXBOT_LAB_CLAUDE", str(exe))
    monkeypatch.setenv("FAKE_OUT", str(out))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # no real Foreman usage feed
    return out


def test_ask_uses_no_tools_no_connectors_caches_and_records_usage(fake_claude):
    r = agentcall.ask("THE PACKET", model="claude-opus-5-5", effort="high", system="standing")
    assert r["decision"]["action"] == "take" and r["seconds"] == 12.0 and not r["cached"]
    call = json.loads(fake_claude.read_text().splitlines()[0])
    a = call["argv"]
    for flag in (
        "--tools",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
    ):
        assert flag in a
    assert a[a.index("--model") + 1] == "claude-opus-5-5" and call["packet"] == "THE PACKET"
    assert agentcall.ask("THE PACKET", model="claude-opus-5-5", effort="high", system="standing")[
        "cached"
    ]
    assert len(fake_claude.read_text().splitlines()) == 1  # the second answer came from the cache
    assert agentcall.weekly_usage()[0] == 0.4


def test_ask_stops_at_ricks_weekly_stop_and_on_a_limit(fake_claude, monkeypatch):
    store.write_json(
        store.lab_local() / "usage.json", {"at": "2099-01-01T00:00:00", "seven_day": 0.71}
    )
    with pytest.raises(agentcall.UsageStop):
        agentcall.ask("P2", model="m", effort="e", system="s")
    store.write_json(
        store.lab_local() / "usage.json", {"at": "2099-01-01T00:00:00", "seven_day": 0.2}
    )
    monkeypatch.setenv("FAKE_MODE", "limit")
    with pytest.raises(agentcall.UsageStop):
        agentcall.ask("P3", model="m", effort="e", system="s")


# --- the time machine with a variant and the agent ------------------------------------------------


def _inputs(cfg, hist):
    from asxbot.arena import replay_ibkr as RI

    return dict(
        codes=CODES,
        shorts={"BRK", "NWS", "QUI"},
        ann=_ann(),
        hist=RI.HistoryBars(hist),
        universe=set(CODES),
    )


def test_a_day_with_the_agent_in_the_loop(cfg, hist, monkeypatch):
    packets = []

    def fake_ask(packet, **kw):
        packets.append(packet)
        return {
            "text": "",
            "decision": {"action": "take", "stop": None, "why": "ok"},
            "seconds": 20.0,
            "cost_usd": 0.02,
            "model": kw["model"],
            "cached": False,
        }

    monkeypatch.setattr(agentcall, "ask", fake_ask)
    v = {"id": "baseline", "playbook": "asx_daytrader", "overrides": {}, "agent_prompt": ""}
    rec = sim.run_day(
        cfg, DAY, v, books=["bot", "agent"], anon=False, allow_locked=True, **_inputs(cfg, hist)
    )
    books = rec["daytrader"]["books"]
    assert [t["ticker"] for t in books["bot"]["trades"]] == ["BRK"]
    agent_trades = [t for t in books["agent"]["trades"] if t["ticker"] == "BRK"]
    assert agent_trades and agent_trades[0]["side"] == "buy"
    assert rec["agent_stats"]["calls"] >= 1 and rec["agent_decisions"][0]["action"] == "take"
    assert packets and "BRK" in packets[0] and "YOU ARE DECIDING AT: 2026-09-21" in packets[0]
    # the answer took 20 s: the agent's order is decided 20 s after the scan saw the setup
    assert agent_trades[0]["decided"] > books["bot"]["trades"][0]["decided"]


def test_before_the_cutoff_the_agent_sees_only_an_anonymised_packet(cfg, hist, monkeypatch):
    packets = []
    monkeypatch.setattr(
        agentcall,
        "ask",
        lambda packet, **kw: (
            packets.append(packet)
            or {"decision": {"action": "reject"}, "seconds": 5.0, "model": "m", "cached": True}
        ),
    )
    v = {"id": "baseline", "playbook": "asx_daytrader", "overrides": {}, "agent_prompt": ""}
    with pytest.raises(ValueError):  # raw pre-cutoff days are refused outright
        sim.run_day(
            cfg, date(2026, 5, 12), v, books=["bot", "agent"], anon=False, **_inputs(cfg, hist)
        )
    rec = sim.run_day(
        cfg,
        DAY,
        v,
        books=["bot", "agent"],
        anon=True,
        allow_locked=True,
        day_number=7,
        **_inputs(cfg, hist),
    )
    assert packets
    p = packets[0]
    assert (
        "BRK" not in p
        and "2026-09-21" not in p
        and "Day 7" in p
        and re.search(r"\bS[0-9A-F]{4}\b", p)
    )
    assert "S&P/ASX 200 according to" not in p  # the list's date is gone too
    assert rec["anon"] is True


def test_a_variant_changes_what_the_bot_does(cfg, hist):
    v = {
        "id": "dt-0009",
        "playbook": "asx_daytrader",
        "overrides": {"setups.opening_range_breakout.enabled": False},
        "agent_prompt": "",
    }
    rec = sim.run_day(
        cfg, DAY, v, books=["bot"], anon=False, allow_locked=True, **_inputs(cfg, hist)
    )
    setups = rec["daytrader"]["setups"]["by_setup"]
    assert "opening_range_breakout" not in setups
    base = sim.run_day(
        cfg,
        DAY,
        {"id": "baseline", "playbook": "asx_daytrader", "overrides": {}},
        books=["bot"],
        anon=False,
        allow_locked=True,
        **_inputs(cfg, hist),
    )
    assert base["daytrader"]["setups"]["by_setup"].get("opening_range_breakout")


def test_the_live_replay_is_unchanged_without_a_variant(cfg, hist):
    from asxbot.arena import replay_ibkr as RI

    r = RI.replay_day(cfg, DAY, CODES, {"BRK", "NWS", "QUI"}, _ann())
    assert "agent_book" not in r["daytrader"] and r["daytrader"]["trades"]


# --- scores, gates and WINNER.md ---------------------------------------------------------------


def _days(pnls, trades_per_day=3, move=0.5, start=1):
    out = []
    for i, p in enumerate(pnls):
        each = p / trades_per_day
        out.append(
            {
                "day": f"2026-07-{start + i:02d}",
                "market_move_pct": move if i % 2 else -move,
                "daytrader": {
                    "books": {
                        "bot": {
                            "pnl": p,
                            "fees": 20.0,
                            "trades": [
                                {
                                    "net": each,
                                    "r": 0.5 if each > 0 else -0.5,
                                    "ticker": "X",
                                    "reason": "[vwap_reclaim] r",
                                }
                                for _ in range(trades_per_day)
                            ],
                            "stuck_at_close": [],
                        }
                    }
                },
            }
        )
    return out


def test_card_and_the_validation_gate():
    good = score.card(_days([60, 80, -20, 90, 70, 50, 40, -10, 65, 55]), "asx_daytrader", "bot")
    base = score.card(_days([10, -5, 0, 5, -10, 0, 5, 0, -5, 0]), "asx_daytrader", "bot")
    assert good["trades"] == 30 and good["t_stat"] > 3 and good["pnl_after_fees"] == 480
    ok, why = score.gate("validate", good, base, n_validated=3)
    assert ok, why
    ok, why = score.gate(
        "validate", good, base, n_validated=10**9
    )  # so many tries the bar is out of reach
    assert not ok and "t-statistic" in why[-1]
    bad = score.card(_days([-60, 10, -20]), "asx_daytrader", "bot")
    ok, why = score.gate("screen", bad, base)
    assert not ok and any("P&L" in w for w in why) and any("trades" in w for w in why)
    assert score.validation_bar(1) == 1.0 and score.validation_bar(10) == pytest.approx(
        2.146, abs=0.01
    )


def test_winner_needs_every_test():
    locked = score.card(
        _days([80, 60, -20, 90, 70, 50, 40, -10, 65, 55, 75, 60, 45, 30], trades_per_day=3),
        "asx_daytrader",
        "bot",
    )
    base = score.card(_days([5] * 14), "asx_daytrader", "bot")
    shadow = score.card(
        _days([50, 40, 30, -10, 20, 35, 25, 45, 15, 30], trades_per_day=2, start=1),
        "asx_daytrader",
        "bot",
    )
    sbase = score.card(_days([0] * 10, start=1), "asx_daytrader", "bot")
    v = {"overrides": {"manage.trail_distance_r": 1.5}, "agent_prompt": ""}
    res = winner.check(v, locked, base, shadow, sbase, n_finals=1, anon_flip=None)
    assert winner.is_winner(res), [r for r in res if not r[1]]
    assert (
        "trial it with real money?"
        in winner.message(
            {"id": "dt-0001", "playbook": "asx_daytrader", **v}, "bot", res, locked, shadow
        ).lower()
    )
    few = score.card(_days([80, 60], trades_per_day=3), "asx_daytrader", "bot")
    assert not winner.is_winner(winner.check(v, few, base, shadow, sbase, 1, None))
    assert not winner.is_winner(winner.check(v, locked, base, shadow, sbase, 1, anon_flip=True))
    assert not winner.is_winner(
        winner.check(v, locked, locked, shadow, sbase, 1, None)
    )  # doesn't beat the bot
    assert winner.t_required(1) == pytest.approx(1.65) and winner.t_required(10) > 2.7


# --- when it runs, and research ----------------------------------------------------------------


def test_never_in_the_watchers_hours():
    from zoneinfo import ZoneInfo

    syd = ZoneInfo("Australia/Sydney")
    assert not runner.market_quiet(datetime(2026, 9, 28, 10, 0, tzinfo=syd))[0]  # Monday
    assert not runner.market_quiet(datetime(2026, 9, 28, 6, 50, tzinfo=syd))[0]
    assert runner.market_quiet(datetime(2026, 9, 28, 19, 40, tzinfo=syd))[0]
    assert runner.market_quiet(datetime(2026, 9, 26, 14, 0, tzinfo=syd))[0]  # Saturday
    assert runner.stop_time(datetime(2026, 9, 26, 14, 0, tzinfo=syd)) == datetime(
        2026, 9, 28, 6, 45, tzinfo=syd
    )


def test_ingest_registers_good_proposals_and_says_why_others_were_refused(cfg, tmp_path):
    folder = tmp_path / "research"
    folder.mkdir()
    (folder / "proposals.json").write_text(
        json.dumps(
            [
                {
                    "playbook": "asx_daytrader",
                    "overrides": {"manage.trail_distance_r": 1.5},
                    "why": "losers gave back +1R",
                },
                {
                    "playbook": "asx_daytrader",
                    "overrides": {"costs.brokerage_pct": 0.0},
                    "why": "cheaper",
                },
                {
                    "playbook": "asx_daytrader",
                    "overrides": {},
                    "agent_prompt": "Skip vwap reclaims after 15:00.",
                    "why": "late fades",
                },
            ]
        ),
        encoding="utf-8",
    )
    from asxbot.lab import research

    reg = Registry(store.lab_data(cfg))
    out = research.ingest(cfg, folder, reg)
    assert out["registered"] == ["dt-0001", "dt-0002"] and len(out["refused"]) == 1
    assert "may not be changed" in out["refused"][0]["why"]
    assert reg.load("dt-0002")["books"] == ["bot", "agent"]
