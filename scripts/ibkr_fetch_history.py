"""Fetch IBKR's 1-minute history for the replay (25 Sep 2026): the ASX 300 list and the
index, as many months back as asked, through the persistent connection's paced history
queue (client 46, read-only), one small request at a time, cached on local disk
(%LOCALAPPDATA%\\asx-bot\\ibkr\\history\\<code>\\<day>.parquet - the same cache the live feed
reads its prior sessions from). Resumable: a stock with every session of the window on disk
is skipped, and a chunk already on disk is not asked for again.

    C:\\venvs\\asx-bot\\Scripts\\python.exe scripts\\ibkr_fetch_history.py --months 6
    C:\\venvs\\asx-bot\\Scripts\\python.exe scripts\\ibkr_fetch_history.py --months 6 --status

Runs on its own as the scheduled task "ASXBot IBKR History Fetch" (daily 17:30 Sydney,
through scripts/ibkr_fetch_history.pyw and its release shim): never starts 07:00-17:00 on a
trading day, stops itself at 07:00 on one, runs the weekend through; once the window is on
disk a run tops up the newest sessions and ends in minutes.

Pacing: IBKR's 1-minute bars have no hard limit, only a soft one ("too much too quickly can
lead to throttling"), so this keeps the gateway's own rules anyway: at most 600 requests in
any ten minutes, at least 0.25 s apart, a pacing message pauses everything for 30 s, and
each request is two weeks of one stock (about 3,700 bars, ~0.6 s from IBKR).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from asxbot.config import load_config  # noqa: E402
from asxbot.ibkr.gateway import end_of, settings_from_config  # noqa: E402
from asxbot.ibkr.live import LiveGateway  # noqa: E402
from asxbot.io import write_parquet_atomic  # noqa: E402
from asxbot.localdir import asx_local  # noqa: E402
from asxbot.log import get_logger, setup_logging  # noqa: E402

SYD = ZoneInfo("Australia/Sydney")
log = get_logger("asxbot.ibkr.fetch")
CHUNK_DAYS = 14  # trading days a request covers ("14 D" is fourteen sessions to IBKR)
CHUNK_STEP = 20  # calendar days between chunk ends (about fourteen sessions)
WORKERS = 3
# Recent windows come back in a second; older ones take 1-36 s and IBKR cancels some
# outright when several are in flight (measured 25 Sep 2026). So: a modest timeout, few in
# flight, and the chunks are fetched NEWEST FIRST across every stock, so at any moment the
# cache holds a complete recent window for the whole list and the replay can run on it.
REQUEST_TIMEOUT_S = 60.0
NO_FETCH_HOURS = (7, 17)  # never on a trading day between these hours: the watcher's feed


def sessions_between(first: date, last: date) -> list[date]:
    """ASX sessions from `first` to `last` inclusive, oldest first (the exchange calendar)."""
    import exchange_calendars as xc
    import pandas as pd

    cal = xc.get_calendar("XASX")
    return [t.date() for t in cal.sessions_in_range(pd.Timestamp(first), pd.Timestamp(last))]


def cache_path(root: Path, code: str, day: date) -> Path:
    return root / code.upper() / f"{day.isoformat()}.parquet"


def chunks(first: date, last: date) -> list[tuple[date, date]]:
    """(start, end) pairs of CHUNK_DAYS calendar days covering [first, last]."""
    out = []
    end = last
    while end >= first:
        start = end - timedelta(days=CHUNK_STEP - 1)
        out.append((max(start, first), end))
        end = start - timedelta(days=1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--end", default=None, help="last day, YYYY-MM-DD (default: today)")
    ap.add_argument("--codes", nargs="*", help="only these codes (testing)")
    ap.add_argument("--status", action="store_true", help="what is on disk; fetch nothing")
    ap.add_argument("--client-id", type=int, default=46)
    args = ap.parse_args()

    cfg = load_config()
    setup_logging(cfg.logs_dir)
    from asxbot.data.universe import build_universes

    a, _ = build_universes(cfg.data_dir, cfg.get("collector.user_agent"))
    codes = [c.upper() for c in (args.codes or a.codes)]
    index = str(cfg.get("backtest.index_ticker", "^AXJO"))
    codes = [index, *[c for c in codes if c != index]]
    last = date.fromisoformat(args.end) if args.end else datetime.now(SYD).date()
    first = last - timedelta(days=int(30.5 * args.months))
    sessions = sessions_between(first, last)
    root = asx_local() / "ibkr" / "history"
    root.mkdir(parents=True, exist_ok=True)
    have = {c: sum(1 for d in sessions if cache_path(root, c, d).exists()) for c in codes}
    print(f"window {first}..{last}: {len(sessions)} sessions, {len(codes)} codes; cache {root}")
    complete = [c for c in codes if have[c] >= 0.9 * len(sessions)]
    print(f"already complete (>=90% of sessions on disk): {len(complete)} of {len(codes)}")
    if args.status:
        thin = sorted(codes, key=lambda c: have[c])[:20]
        print("thinnest:", ", ".join(f"{c} {have[c]}" for c in thin))
        return 0

    todo = [c for c in codes if c not in complete]
    now = datetime.now(SYD)
    from asxbot.announcements.live import is_trading_day

    if is_trading_day(now.date()) and NO_FETCH_HOURS[0] <= now.hour < NO_FETCH_HOURS[1]:
        print(f"refusing to fetch at {now:%H:%M} on a trading day: the watcher's live feed "
              "has IBKR to itself between 07:00 and 17:00")  # fmt: skip
        return 2
    s = dataclasses.replace(
        settings_from_config(cfg), client_id=int(args.client_id),
        request_timeout_s=REQUEST_TIMEOUT_S, history_concurrency=WORKERS,
    )  # fmt: skip
    gw = LiveGateway(s, history_dir=root).start()
    if not gw.wait_ready(30):
        print(f"IB Gateway not ready: {gw.health.last_error}")
        return 1
    stats = {"codes": len(todo), "requests": 0, "bars": 0, "days_written": 0, "empty": 0,
             "failed": [], "chunks_done": []}  # fmt: skip
    lock = threading.Lock()
    # newest chunk first, every stock, then the next chunk back: the recent window fills
    # in for the whole list before anything older is asked for
    jobs = [(start, end, code) for start, end in chunks(first, last) for code in todo]
    queue = list(jobs)
    started = time.monotonic()

    def stop_now() -> bool:
        """07:00 on a trading day: the watcher's feed has IBKR to itself. A run that
        began the evening before (or on the weekend) stops taking jobs here; the next
        run carries on from what is on disk."""
        t = datetime.now(SYD)
        return is_trading_day(t.date()) and NO_FETCH_HOURS[0] <= t.hour < NO_FETCH_HOURS[1]

    def worker() -> None:
        while True:
            with lock:
                if not queue or stop_now():
                    return
                start, end, code = queue.pop(0)
            wanted = [d for d in sessions if start <= d <= end]
            if wanted and all(cache_path(root, code, d).exists() for d in wanted):
                continue
            if code in gw.unknown_codes:
                continue
            df = gw.history_sync(code, f"{CHUNK_DAYS} D", end_of(end + timedelta(days=1)))
            written = 0
            with lock:
                stats["requests"] += 1
            if df is None or not len(df):
                with lock:
                    stats["empty"] += 1
                    if code in gw.unknown_codes and code not in stats["failed"]:
                        stats["failed"].append(code)
            else:
                with lock:
                    stats["bars"] += len(df)
                for d in sorted({x for x in df.index.date if first <= x <= last}):
                    part = df[[x == d for x in df.index.date]]
                    if len(part):
                        write_parquet_atomic(part, cache_path(root, code, d))
                        written += 1
            with lock:
                stats["days_written"] += written
                done = len(jobs) - len(queue)
            if done % 50 == 0 or not queue:
                el = time.monotonic() - started
                print(f"{done}/{len(jobs)} requests ({start}..{end}), "
                      f"{stats['days_written']} stock-days written, {stats['empty']} empty, "
                      f"{el / 60:.0f} min, pacing hits {gw.health.pacing_hits}, reconnects "
                      f"{gw.reconnects}", flush=True)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if queue and stop_now():
        print(f"stopped at {datetime.now(SYD):%H:%M} on a trading day with {len(queue)} "
              "requests still to do; the next run (17:30) carries on", flush=True)
        stats["stopped_for_market_hours"] = len(queue)
    gw.stop(10)
    stats["minutes"] = round((time.monotonic() - started) / 60, 1)
    stats["window"] = [first.isoformat(), last.isoformat()]
    stats["sessions"] = len(sessions)
    out = REPO / "reports" / f"ibkr_history_fetch_{datetime.now(SYD):%Y%m%d_%H%M}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    print(f"done: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
