"""The daily scorecard in the evening report (Rick's brief, 25 Sep 2026): per playbook and
account, P&L after fees today and in total, green/red days ("day N of 10"), the worst drop,
the best trade's share of the profit, prices per decision - and a partial day named with
its reason (25 Sep for v2)."""

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from asxbot.arena import report as R
from asxbot.arena.accounts import ArenaOrder
from asxbot.arena.broker import ArenaBroker
from asxbot.arena.levels import load_playbook
from asxbot.arena.minutes import MinuteBars
from asxbot.arena.runtime import Arena
from asxbot.arena.scoreboard import score
from asxbot.backtest.costs import CostModel
from asxbot.config import load_config

SYD = ZoneInfo("Australia/Sydney")
DAY = date(2026, 9, 25)


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


def test_25_september_is_a_partial_day_for_v2_with_the_reason(cfg):
    v2 = load_playbook(cfg, "asx_announcements_v2")
    dt = load_playbook(cfg, "asx_daytrader")
    assert R.partial_day(v2, DAY).startswith("data failure")
    assert R.partial_day(dt, DAY) == "" and R.partial_day(v2, date(2026, 9, 28)) == ""
    assert R.test_day(v2, DAY).startswith("day 1 of 10 (v2) - PARTIAL DAY (data failure")
    assert R.test_day(dt, DAY) == "day 1 of 10 (v1)"


def _closed(acct, oid, ticker, realised, commission):
    acct.orders[oid] = ArenaOrder(
        order_id=oid, account=acct.name, ticker=ticker, side="sell", qty=100, limit=1.0,
        decided_at="2026-09-25T11:00:00+10:00", status="filled", filled_qty=100,
        realised=realised, commission=commission,
    )  # fmt: skip


def test_the_scorecard_counts_pnl_after_fees_days_drop_and_the_best_trades_share(cfg, arena):
    pb = load_playbook(cfg, "asx_announcements_v2")
    bot = arena.account(pb, "bot")
    bot.cash = 20_194.42  # +194.42 after fees today (NWL +250 - fees, HLS -30)
    bot.fees_paid = 26.40
    bot.realised_pnl = 220.82
    _closed(bot, "ARN-1", "NWL", 250.0, 13.20)
    _closed(bot, "ARN-2", "HLS", -29.18, 13.20)
    arena.store.save(bot)
    arena.broker.mark_to_market(bot, DAY)
    scores = [score(arena.store, a, arena.broker.prices(a, DAY))
              for a in (arena.account(pb, "agent"), bot)]  # fmt: skip
    rows = R.scorecard_facts(arena, DAY, scores)
    row = next(r for r in rows if r["key"] == "asx_announcements_v2")
    assert row["partial"].startswith("data failure") and "PARTIAL DAY" in row["test"]
    b = next(a for a in row["accounts"] if a["kind"] == "bot")
    assert b["today_after_fees"] == pytest.approx(194.42)
    assert b["total_after_fees"] == pytest.approx(194.42) and b["fees_total"] == 26.40
    assert b["green_days"] == 1 and b["red_days"] == 0 and b["max_drawdown_pct"] == 0.0
    assert b["trades"] == 2 and b["wins_after_fees"] == 1
    assert b["best_trade"] == 250.0 and b["best_trade_share_pct"] == 100.0
    a = next(x for x in row["accounts"] if x["kind"] == "agent")
    assert a["today_after_fees"] == 0.0 and a["best_trade_share_pct"] is None

    by = {"live data (IBKR)": 1, "no usable prices (x)": 7}
    facts = {"scorecard": rows,
             "playbooks": [{"key": "asx_announcements_v2", "data_by_decision": by}]}  # fmt: skip
    lines = R.scorecard_lines(facts)
    text = "\n".join(lines)
    assert "PARTIAL DAY: data failure" in text
    assert "BOT: today +194.42 after fees, total +194.42 (fees 26.40); green/red days 1/0" in text
    assert "100% from the best trade (+250.00)" in text
    assert "prices per decision: 1 on live data (IBKR), 7 on no usable prices (x)" in text


def test_the_plain_report_and_the_agents_brief_carry_the_scorecard(cfg, arena, monkeypatch):
    monkeypatch.setattr(R, "asx200_provenance", lambda *a, **k: {"list": "x", "as_of": None,
                                                                   "members": 0})  # fmt: skip
    monkeypatch.setattr(R, "archive_status", lambda *a, **k: "n/a")
    facts = R.gather(arena, DAY)
    assert facts["scorecard"] and facts["scorecard"][0]["test"]
    text = R.render_plain(facts)
    assert "<b>Scorecard</b>" in text and "PARTIAL DAY: data failure" in text
    brief = R.agent_brief(facts)
    assert "SCORECARD" in brief and "PARTIAL DAY" in brief and '"scorecard"' in brief


def test_scorecard_lines_need_nothing_but_the_facts():
    assert R.scorecard_lines({}) == []
    row = {"key": "k", "title": "T", "test": "day 2 of 10 (v1)", "partial": "", "accounts": [
        {"kind": "bot", "name": "b", "today_after_fees": -5.5, "total_after_fees": 10.0,
         "fees_total": 1.0, "green_days": 1, "red_days": 1, "worst_day_pct": -0.03,
         "max_drawdown_pct": 0.03, "trades": 1, "wins_after_fees": 0, "best_trade": None,
         "best_trade_share_pct": None}]}  # fmt: skip
    lines = R.scorecard_lines({"scorecard": [row], "playbooks": []})
    assert lines[0] == "<b>T</b> - day 2 of 10 (v1)" and "gross profit -" in lines[1]
    assert SimpleNamespace  # the module is imported for the fixtures above
