"""The replay over IBKR 1-minute history (arena/replay_ibkr.py): a synthetic history cache
with two sessions, one stock that breaks out with volume on day 2 and a v2 news stock that
jumps 5% on 5x volume, run through the live rule bots and the scorecard. No network."""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from asxbot.arena import replay_ibkr as RI
from asxbot.config import load_config
from asxbot.io import write_parquet_atomic

SYD = ZoneInfo("Australia/Sydney")
PRIOR = [date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 17),
         date(2026, 9, 18)]  # fmt: skip
DAY = date(2026, 9, 21)  # the Monday after: replayed
CODES = ["BRK", "NWS", "QUI"]


def at(h, m, day):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=SYD)


def session(day, px=10.0, vol=2000.0, bump=None, closing=None):
    """A whole session 10:00-15:59 plus the 16:10 closing auction; `bump`
    {(h, m): (o, h, l, c, v)} overrides minutes."""
    rows = {}
    t = at(10, 0, day)
    while t.time() < datetime.min.replace(hour=16).time():
        rows[(t.hour, t.minute)] = (px, px, px, px, vol)
        t += timedelta(minutes=1)
    rows[(16, 10)] = (closing or px,) * 4 + (vol * 10,)
    if bump:
        rows.update(bump)
    idx = pd.DatetimeIndex([at(h, m, day) for (h, m) in rows])
    cols = ["open", "high", "low", "close", "volume"]
    return pd.DataFrame(list(rows.values()), index=idx, columns=cols).astype(float)


@pytest.fixture
def cfg(config_file, tmp_path):
    return load_config(
        config_file(data={"provider": "yfinance", "dir": str(tmp_path / "data")}),
        env_file=tmp_path / "none.env",
    )


@pytest.fixture
def hist(tmp_path, monkeypatch, cfg):
    root = tmp_path / "hist"
    monkeypatch.setattr(RI, "history_root", lambda: root)
    # prior sessions for every code and the index (flat, 2,000 a minute)
    for d in PRIOR:
        for c in CODES:
            write_parquet_atomic(session(d), root / c / f"{d.isoformat()}.parquet")
        write_parquet_atomic(session(d, 8000.0, 0.0), root / "^AXJO" / f"{d.isoformat()}.parquet")
    # the replayed day: the index flat; BRK opens flat, breaks its 10:00-10:29 range at
    # 10:35 on 5x volume and runs; NWS (with news) gaps +5% on 5x volume; QUI is quiet
    write_parquet_atomic(session(DAY, 8000.0, 0.0), root / "^AXJO" / f"{DAY.isoformat()}.parquet")
    # twice the usual volume all morning (RVOL 2), the first close above 10.02 at 10:35 on
    # 5x the prior ten bars, then a run to +2R and beyond
    brk = {(10, m): (10.0, 10.02, 9.98, 10.0, 4000.0) for m in range(0, 35)}
    brk[(10, 35)] = (10.0, 10.12, 10.0, 10.1, 20000.0)
    for m in range(36, 60):  # a steady climb, so the bot's limit (1% through) fills
        px = round(10.1 + 0.02 * (m - 35), 2)
        brk[(10, m)] = (px - 0.01, px + 0.01, px - 0.02, px, 8000.0)
    day_file = f"{DAY.isoformat()}.parquet"
    write_parquet_atomic(session(DAY, 10.5, 2000.0, bump=brk), root / "BRK" / day_file)
    nws = {(10, m): (10.5, 10.52, 10.48, 10.5, 10000.0) for m in range(0, 60)}
    write_parquet_atomic(session(DAY, 10.5, 10000.0, bump=nws), root / "NWS" / day_file)
    write_parquet_atomic(session(DAY), root / "QUI" / day_file)
    # daily prices up to the day before, so the size rule and the tick limit pass
    daily = cfg.data_dir / "prices" / "yfinance"
    daily.mkdir(parents=True, exist_ok=True)
    days = pd.bdate_range("2026-06-01", "2026-09-18")
    for c in CODES:
        df = pd.DataFrame({"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
                           "volume": 2_000_000.0}, index=days)  # fmt: skip
        df.index.name = "date"
        write_parquet_atomic(df, daily / f"{c}.parquet")
    return root


def _ann():
    rows = [{"code": "NWS", "released_at": pd.Timestamp("2026-09-21 08:30"),
             "release_date": "2026-09-21", "headline": "Record result", "type": "results",
             "price_sensitive": True, "pre_open": True, "ids_id": "A1", "pdf_url": "",
             "pages": 1, "size": "1KB"}]  # fmt: skip
    return pd.DataFrame(rows)[RI.COLS]


def test_a_day_is_replayed_through_both_rule_bots_and_scored(cfg, hist):
    r = RI.replay_day(cfg, DAY, CODES, {"BRK", "NWS", "QUI"}, _ann())
    assert not r["gaps"] and r["stocks_with_bars"] == 3 and r["dt_universe"] == 3
    assert r["market_move_pct"] == pytest.approx(0.0)
    dt = r["daytrader"]
    assert dt["setups"]["found"] >= 1 and dt["setups"]["bot_orders"] >= 1
    brk = [t for t in dt["trades"] if t["ticker"] == "BRK"]
    assert brk, dt
    assert brk[0]["side"] == "buy" and brk[0]["net"] > 0 and brk[0]["r"] > 0
    assert brk[0]["first_fill"] >= "10:36"  # filled at the bar after the trigger, never before
    assert dt["open_positions"] == {}  # flat at the close
    v2 = r["v2"]
    assert v2["state"]["status"] == "done" and v2["candidates"] == 1
    assert [t["ticker"] for t in v2["trades"]] == ["NWS"] and v2["trades"][0]["side"] == "buy"
    assert v2["open_positions"] == {}


def test_a_day_without_index_bars_is_a_gap_not_a_quiet_day(cfg, hist):
    (hist / "^AXJO" / f"{DAY.isoformat()}.parquet").unlink()
    r = RI.replay_day(cfg, DAY, CODES, set(), _ann())
    assert r["gaps"] and "index" in r["gaps"][0] and "daytrader" not in r


def test_the_scorecard_and_the_report(cfg, hist, tmp_path, monkeypatch):
    r = RI.replay_day(cfg, DAY, CODES, {"BRK", "NWS", "QUI"}, _ann())
    days = [r, {"day": "2026-09-22", "gaps": ["only 3 of 300 stocks have bars"]}]
    s = RI.scorecard(days, "daytrader")
    assert s["days"] == 1 and s["trades"] >= 1 and s["green_days"] == 1 and s["red_days"] == 0
    assert s["pnl_after_fees"] == pytest.approx(r["daytrader"]["pnl"])
    assert s["end_equity"] == pytest.approx(RI.START_CASH + r["daytrader"]["pnl"])
    assert s["up_days"]["n"] == 0 and s["down_days"]["n"] == 1  # a flat index counts as down
    md = RI.render(days, DAY, date(2026, 9, 22), 3, {"avg_dt_universe": 3})
    assert md.startswith("# Replay") and "REPLAY - not live" in md
    assert "Data gaps" in md and "2026-09-22" in md and "day trader v1" in md
    assert "small sample" in md


def test_run_writes_the_report_and_the_json(cfg, hist, tmp_path, monkeypatch):
    import asxbot.data.universe as U

    monkeypatch.setattr(U, "build_universes", lambda *a, **k: (
        type("U", (), {"codes": CODES})(), type("U", (), {"codes": []})()))  # fmt: skip
    monkeypatch.setattr(U, "asx200_codes", lambda *a, **k: set(CODES))
    monkeypatch.setattr(RI, "announcements", lambda *a, **k: _ann())
    md = RI.run(cfg, DAY, DAY, out_dir=tmp_path / "reports")
    assert md.exists()
    js = json.loads(Path(str(md)[:-3] + ".json").read_text(encoding="utf-8"))
    assert js["label"] == RI.LABEL and js["daytrader"]["trades"] >= 1


def test_daily_prices_are_cut_off_at_the_day(cfg, hist):
    d = RI._daily_until(cfg, "BRK", DAY)
    assert d.index.max() < pd.Timestamp(DAY)
    assert RI._daily_until(cfg, "NOPE", DAY) is None


def test_announcements_come_from_the_archive_and_the_live_files(cfg):
    hist = cfg.data_dir / "announcements" / "history"
    live = cfg.data_dir / "announcements" / "live"
    hist.mkdir(parents=True)
    live.mkdir(parents=True)
    a = _ann()
    write_parquet_atomic(a, hist / "NWS.parquet")
    b = a.copy()
    b["ids_id"] = "A2"
    b["released_at"] = pd.Timestamp("2026-09-22 09:00")
    b["release_date"] = "2026-09-22"
    write_parquet_atomic(b, live / "2026-09-22.parquet")
    write_parquet_atomic(a, live / "2026-09-21.parquet")  # the same id twice: once
    got = RI.announcements(cfg, date(2026, 9, 20), date(2026, 9, 22))
    assert sorted(got["ids_id"]) == ["A1", "A2"]
    assert RI.announcements(cfg, date(2026, 1, 1), date(2026, 1, 2)).empty
