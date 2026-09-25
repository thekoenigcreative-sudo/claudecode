"""The IBKR history fetch (scripts/ibkr_fetch_history.py): chunks are whole sessions,
newest first; a request asks only for the span of a chunk still missing; a session IBKR
answered nothing for is remembered as empty; an empty answer is retried once; a job that
raises does not kill a worker; PRN is cached as PRN_. No network: a fake gateway."""

import importlib.util
import json
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
SYD = ZoneInfo("Australia/Sydney")

spec = importlib.util.spec_from_file_location(
    "ibkr_fetch_history", REPO / "scripts" / "ibkr_fetch_history.py"
)
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

# 35 weekdays ending Friday 25 Sep 2026 (no holiday in the range)
SESSIONS = [d for d in (date(2026, 8, 7) + timedelta(days=i) for i in range(50))
            if d.weekday() < 5][-35:]  # fmt: skip


def frame(days, bars=3, vol=100.0):
    idx = []
    for d in days:
        for m in range(bars):
            idx.append(datetime(d.year, d.month, d.day, 10, m, tzinfo=SYD))
    idx = pd.DatetimeIndex(idx)
    return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": vol},
                        index=idx)  # fmt: skip


def test_chunks_are_whole_sessions_newest_chunk_first_each_oldest_first():
    groups = M.chunks_by_sessions(SESSIONS, 30)
    assert [len(g) for g in groups] == [30, 5]
    assert groups[0] == SESSIONS[5:] and groups[1] == SESSIONS[:5]
    assert groups[0][-1] == date(2026, 9, 25) and groups[1][0] == SESSIONS[0]
    assert M.chunks_by_sessions([], 30) == []
    assert [len(g) for g in M.chunks_by_sessions(SESSIONS, 14)] == [14, 14, 7]


def test_a_span_asks_only_for_the_sessions_not_on_disk(tmp_path):
    root = tmp_path / "hist"
    group = SESSIONS[:5]
    assert M.span_to_fetch(root, "AAA", group) == (group[0], group[-1], 5)
    for d in group[1:]:
        M.write_parquet_atomic(frame([d]), M.cache_path(root, "AAA", d))
    # only the earliest is missing (the first version's boundary hole): one session
    assert M.span_to_fetch(root, "AAA", group) == (group[0], group[0], 1)
    M.write_parquet_atomic(frame([group[0]]), M.cache_path(root, "AAA", group[0]))
    assert M.span_to_fetch(root, "AAA", group) is None
    # a hole in the middle: that session only; known empty counts as present
    M.cache_path(root, "AAA", group[2]).unlink()
    assert M.span_to_fetch(root, "AAA", group) == (group[2], group[2], 1)
    M.mark_empty(root, "AAA", [group[2]])
    assert M.span_to_fetch(root, "AAA", group) is None
    assert M.load_empty(root, "AAA") == {group[2]}
    # first and last missing: the whole chunk in one request
    M.cache_path(root, "AAA", group[0]).unlink()
    M.cache_path(root, "AAA", group[4]).unlink()
    assert M.span_to_fetch(root, "AAA", group) == (group[0], group[4], 5)


def test_reserved_windows_names_get_the_cache_suffix(tmp_path):
    assert M.cache_path(tmp_path, "PRN", SESSIONS[0]).parent.name == "PRN_"
    assert M.cache_path(tmp_path, "^AXJO", SESSIONS[0]).parent.name == "^AXJO"
    assert M.cache_path(tmp_path, "bhp", SESSIONS[0]).parent.name == "BHP"


class FakeGateway:
    """history_sync(code, "n D", end): the n sessions before `end` from SESSIONS, as IBKR
    would answer, with one code that answers nothing the first time, one that raises, and
    one stock that did not trade on one session inside the span."""

    def __init__(self):
        self.unknown_codes = {"ZZZ"}
        self.calls = []
        self.lock = threading.Lock()
        self.reconnects = 0

    def history_sync(self, code, duration, end):
        with self.lock:
            self.calls.append((code, duration, end.date()))
            n_before = sum(1 for c, _, _ in self.calls[:-1] if c == code)
        n = int(duration.split()[0])
        last = end.date() - timedelta(days=1)
        days = [d for d in SESSIONS if d <= last][-n:]
        if code == "BBB" and n_before == 0:
            return None  # a timeout the first time
        if code == "CCC":
            raise RuntimeError("boom")
        if code == "AAA" and len(days) > 2:
            days = [d for d in days if d != days[1]]  # did not trade that day
        return frame(days)


def test_fetch_all_writes_marks_empty_retries_and_survives_a_bad_job(tmp_path):
    root = tmp_path / "hist"
    gw = FakeGateway()
    # AAA already has the newest chunk except its earliest session: one small request
    for d in SESSIONS[6:]:
        M.write_parquet_atomic(frame([d]), M.cache_path(root, "AAA", d))
    lines = []
    stats = M.fetch_all(gw, root, SESSIONS, ["AAA", "BBB", "CCC", "PRN", "ZZZ"], chunk=30,
                        workers=2, progress=lines.append, log_every=1)  # fmt: skip
    # AAA: "1 D" for the boundary session, then "5 D" for the old chunk
    aaa = sorted((dur, end) for c, dur, end in gw.calls if c == "AAA")
    assert aaa == [("1 D", SESSIONS[5] + timedelta(days=1)),
                   ("5 D", SESSIONS[4] + timedelta(days=1))]
    assert all(M.cache_path(root, "AAA", d).exists() or d == SESSIONS[1] for d in SESSIONS)
    assert M.load_empty(root, "AAA") == {SESSIONS[1]}  # inside the answered span: no bars
    # BBB: the first answer was empty, so it was asked again after everything else
    bbb = [(dur, end) for c, dur, end in gw.calls if c == "BBB"]
    assert len(bbb) == 3 and stats["retried"] == 1 and stats["failed"] == []
    assert all(M.cache_path(root, "BBB", d).exists() for d in SESSIONS)
    # CCC raised on every job: counted, and the workers went on to finish the queue
    assert len(stats["errors"]) == 2 and all("boom" in e for e in stats["errors"])
    # PRN under PRN_; ZZZ skipped as unknown, never asked
    assert (root / "PRN_" / f"{SESSIONS[-1].isoformat()}.parquet").exists()
    assert not any(c == "ZZZ" for c, _, _ in gw.calls) and stats["unknown"] == ["ZZZ"]
    assert stats["requests"] == len(gw.calls) - 2  # CCC's two raised before counting
    # written by this run only: AAA's boundary session and 4 of its old chunk's 5 (one did
    # not trade), BBB's and PRN's whole window
    assert stats["days_written"] == (1 + 4) + 35 + 35 and stats["no_bars_days"] == 1
    assert lines and "jobs left" in lines[-1]
    cov = M.coverage(root, ["AAA", "BBB", "PRN"], SESSIONS)
    assert cov["codes_complete"] == 3 and cov["stock_days_missing"] == 0
    assert cov["stock_days_empty"] == 1 and cov["per_code"]["AAA"]["held"] == 34


def test_stop_now_leaves_the_rest_for_the_next_run(tmp_path):
    root = tmp_path / "hist"
    gw = FakeGateway()
    stats = M.fetch_all(gw, root, SESSIONS, ["AAA", "BBB"], chunk=30, workers=1,
                        stop_now=lambda: True, progress=lambda s: None)  # fmt: skip
    assert stats["requests"] == 0 and stats["stopped_for_market_hours"] == 4


def test_a_second_run_asks_for_nothing_once_the_window_is_on_disk(tmp_path):
    root = tmp_path / "hist"
    gw = FakeGateway()
    M.fetch_all(gw, root, SESSIONS, ["AAA", "PRN"], chunk=30, workers=1, progress=lambda s: None)
    n = len(gw.calls)
    stats = M.fetch_all(gw, root, SESSIONS, ["AAA", "PRN"], chunk=30, workers=1,
                        progress=lambda s: None)  # fmt: skip
    assert len(gw.calls) == n and stats["requests"] == 0 and stats["skipped"] == 4
    # the fake leaves out the second session of every span it answers: one per chunk
    empty = json.loads((root / "AAA" / M.EMPTY_FILE).read_text())
    assert empty == [SESSIONS[1].isoformat(), SESSIONS[6].isoformat()]


@pytest.mark.parametrize("n", [1, 7])
def test_small_chunks_still_cover_every_session(n):
    groups = M.chunks_by_sessions(SESSIONS, n)
    assert [d for g in reversed(groups) for d in g] == SESSIONS


def test_news_codes_outside_the_list_get_their_live_days_and_the_prior_sessions(tmp_path):
    live = tmp_path / "announcements" / "live"
    live.mkdir(parents=True)
    rows = pd.DataFrame({"code": ["BHP", "XYZ", "abc", "QQQ"],
                         "price_sensitive": [True, True, True, False]})  # fmt: skip
    rows.to_parquet(live / f"{SESSIONS[-2].isoformat()}.parquet")
    (live / "not-a-day.parquet").write_bytes(b"junk")
    codes, window = M.news_codes(tmp_path, SESSIONS, {"BHP"})
    assert codes == ["ABC", "XYZ"]  # BHP is listed, QQQ's news was not price-sensitive
    assert window == SESSIONS[-2 - M.NEWS_PRIOR_SESSIONS:]
    assert M.news_codes(tmp_path / "none", SESSIONS, set()) == ([], [])


class FlakyGateway(FakeGateway):
    """Answers `good` requests; the next dies in flight (the connection goes), and the
    gateway stays not ready after it (an outage that does not end)."""

    def __init__(self, good: int):
        super().__init__()
        self.good = good

    @property
    def ready(self):
        return len(self.calls) <= self.good

    def history_sync(self, code, duration, end):
        if len(self.calls) >= self.good:
            with self.lock:
                self.calls.append((code, duration, end.date()))
            return None  # what LiveGateway answers when the connection goes
        return super().history_sync(code, duration, end)


def test_an_outage_does_not_burn_the_queue_it_stops_and_leaves_it(tmp_path):
    root = tmp_path / "hist"
    gw = FlakyGateway(good=2)
    naps = []
    lines = []
    stats = M.fetch_all(gw, root, SESSIONS, ["AAA", "PRN", "BBB"], chunk=30, workers=1,
                        progress=lines.append, ready_wait_s=60, sleep=naps.append)  # fmt: skip
    # two good requests, one that died in flight (put back, not failed), then the wait
    assert len(gw.calls) == 3 and stats["failed"] == [] and stats["retried"] == 0
    assert sum(naps) >= 60 and stats["stopped_gateway_not_ready"] == 4
    assert any("not ready" in x for x in lines)
    # the next run, with the gateway back, finishes the window
    gw2 = FakeGateway()
    M.fetch_all(gw2, root, SESSIONS, ["AAA", "PRN", "BBB"], chunk=30, workers=1,
                progress=lambda s: None)  # fmt: skip
    cov = M.coverage(root, ["AAA", "PRN", "BBB"], SESSIONS)
    assert cov["stock_days_missing"] == 0
