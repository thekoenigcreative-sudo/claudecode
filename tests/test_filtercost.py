"""What the screen filters threw away. Measured from cached prices; no network.

The point of these tests is that the three outcomes stay distinct: a rejection that can be
scored, one whose 10 sessions have not happened yet, and one with no prices at all. Folding
"not yet" into "no effect" is how a filter gets a clean bill of health it has not earned.
"""

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from asxbot.arena import filtercost as F
from asxbot.config import load_config
from asxbot.log import EventLog

SYD = ZoneInfo("Australia/Sydney")
NOW = datetime(2026, 9, 23, 17, 0, tzinfo=SYD)


@pytest.fixture
def cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )


def _frame(start: str, closes: list[float]):
    idx = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({"close": closes, "volume": [1e6] * len(closes)}, index=idx)


def _reject(cfg, ids_id, ticker, test, at, ok=False, **extra):
    ev = EventLog(cfg.data_dir)
    ev.append(
        "arena_screened",
        {"ids_id": ids_id, "ticker": ticker, "test": test, "ok": ok, **extra},
    )
    p = ev.path("arena_screened")
    lines = p.read_text(encoding="utf-8").splitlines()
    lines[-1] = json.dumps({**json.loads(lines[-1]), "ts": at.astimezone(UTC).isoformat()})
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_the_forward_return_is_measured_against_the_index():
    stock = _frame("2026-09-01", [1.00] + [1.0] * 8 + [1.20, 1.20])  # +20% by session 10
    index = _frame("2026-09-01", [100.0] * 10 + [105.0])  # +5%
    rel, why = F.forward_rel_return(stock, index, datetime(2026, 9, 1, 10, 0, tzinfo=SYD))
    assert why == "" and rel == pytest.approx(15.0, abs=0.01)


def test_a_rejection_without_ten_sessions_yet_is_pending_not_zero():
    stock = _frame("2026-09-21", [1.0, 1.1, 1.2])
    index = _frame("2026-09-21", [100.0, 100.0, 100.0])
    rel, why = F.forward_rel_return(stock, index, datetime(2026, 9, 21, 10, 0, tzinfo=SYD))
    assert rel is None and why == "pending"


def test_a_stock_with_no_prices_is_counted_separately():
    index = _frame("2026-09-01", [100.0] * 12)
    rel, why = F.forward_rel_return(None, index, datetime(2026, 9, 1, 10, 0, tzinfo=SYD))
    assert rel is None and why == "no_prices"


def test_rejections_are_windowed_deduplicated_and_exclude_passes(cfg):
    _reject(cfg, "1", "AAA", "tick", NOW - timedelta(days=2))
    _reject(cfg, "1", "AAA", "tick", NOW - timedelta(days=2))  # same announcement again
    _reject(cfg, "2", "BBB", "", NOW - timedelta(days=1), ok=True)  # passed the screen
    _reject(cfg, "3", "CCC", "turnover", NOW - timedelta(days=40))  # outside the window
    _reject(cfg, "4", "DDD", "tick", NOW - timedelta(days=1), is_test=True)  # a rehearsal
    rows = F.rejections(cfg.data_dir, weeks=4, now=NOW)
    assert [r["ids_id"] for r in rows] == ["1"]


class _FakeArena:
    def __init__(self, cfg, frames):
        self.cfg = cfg
        self.frames = frames

    def daily_lookup(self):
        return lambda t: self.frames.get(t)


def test_each_test_is_scored_on_what_it_excluded(cfg):
    winner = _frame("2026-09-01", [1.00] + [1.0] * 8 + [1.30, 1.30])  # +30%
    loser = _frame("2026-09-01", [1.00] + [1.0] * 8 + [0.80, 0.80])  # -20%
    index = _frame("2026-09-01", [100.0] * 12)
    at = datetime(2026, 9, 1, 10, 0, tzinfo=SYD)
    _reject(cfg, "1", "WIN", "tick", at)
    _reject(cfg, "2", "LOSE", "tick", at)
    _reject(cfg, "3", "LOSE", "turnover", at)
    _reject(cfg, "4", "GONE", "turnover", at)  # no prices at all

    arena = _FakeArena(cfg, {"WIN": winner, "LOSE": loser, "^AXJO": index})
    costs = F.measure(arena, weeks=4, now=NOW)

    tick = costs["tick"]
    assert tick.rejected == 2 and tick.measured == 2
    assert tick.median == pytest.approx(5.0, abs=0.01)  # +30 and -20
    assert tick.win_rate == pytest.approx(50.0) and tick.big_movers == 1
    assert tick.best[0] == "WIN" and tick.best[2] == pytest.approx(30.0, abs=0.01)

    turnover = costs["turnover"]
    assert turnover.rejected == 2 and turnover.measured == 1 and turnover.no_prices == 1

    text = F.render(costs, weeks=4, now=NOW)
    assert "measurement, not an instruction" in text and "| tick |" in text
