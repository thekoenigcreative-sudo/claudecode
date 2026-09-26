"""Fetch IBKR's 1-minute history for the replay (25 Sep 2026): the ASX 300 list and the
index, as many months back as asked, through the persistent connection's paced history
queue (client 46, read-only), a few requests at a time, cached on local disk
(%LOCALAPPDATA%\\asx-bot\\ibkr\\history\\<code>\\<day>.parquet - the same cache the live feed
reads its prior sessions from). Resumable: only the sessions not on disk are asked for.

    C:\\venvs\\asx-bot\\Scripts\\python.exe scripts\\ibkr_fetch_history.py --months 6
    C:\\venvs\\asx-bot\\Scripts\\python.exe scripts\\ibkr_fetch_history.py --months 6 --status

Runs on its own as the scheduled task "ASXBot IBKR History Fetch" (daily 17:30 Sydney,
through scripts/ibkr_fetch_history.pyw and its release shim): never starts 07:00-17:00 on a
trading day, stops itself at 07:00 on one, runs the weekend through; once the window is on
disk a run tops up the newest sessions and ends in minutes.

The requests (rewritten 26 Sep 2026, after the first night's run; LEARNINGS #29):
  * the window's sessions (the exchange calendar) are grouped newest first into chunks of
    CHUNK_SESSIONS, and each request asks IBKR for exactly the span of a chunk's sessions
    that are not on disk yet: "n D" is n trading days to IBKR, ending the day after the last
    session wanted. The first version asked "14 D" for 20-calendar-day chunks, which hold 15
    sessions when no holiday falls in them, so the earliest session of every chunk (7 Sep,
    17 Aug, ...) was never fetched, and every chunk was fetched again on every run;
  * bigger requests are cheaper. Measured against the live Gateway on the night of 25 Sep:
    14 sessions of one stock took 6-42 s, 28 took 15 s, 60 took 67 s - IBKR's cost is mostly
    per request - so 30 sessions a request, and a long timeout rather than a cancelled query;
  * a request that comes back empty (a timeout, a cancelled query, a stock with nothing in
    the span) is asked once more at the end of the run. A session IBKR did not return from
    inside a span it did answer (a stock that did not trade that day) is written to
    <code>/_empty.json and not asked for again;
  * a code IBKR has no contract for is skipped; nothing a job raises can kill a worker
    (the first night's PRN - a Windows device name - killed one: the cache folder is now
    PRN_, as every cache here names it, asxbot.io.safe_stem).

Pacing: IBKR's 1-minute bars have no hard limit, only a soft one ("too much too quickly can
lead to throttling"), so this keeps the gateway's own rules anyway: at most 600 requests in
any ten minutes, at least 0.25 s apart, a pacing message pauses everything for 30 s.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from asxbot.config import load_config  # noqa: E402
from asxbot.ibkr.gateway import end_of, settings_from_config  # noqa: E402
from asxbot.ibkr.live import LiveGateway  # noqa: E402
from asxbot.io import safe_stem, write_parquet_atomic, write_text_atomic  # noqa: E402
from asxbot.localdir import asx_local  # noqa: E402
from asxbot.log import get_logger, setup_logging  # noqa: E402

SYD = ZoneInfo("Australia/Sydney")
log = get_logger("asxbot.ibkr.fetch")
CHUNK_SESSIONS = 30  # sessions a request covers at most ("30 D" is thirty sessions to IBKR)
WORKERS = 3  # in flight at once: three gave 3.2 requests a minute against 2.3 for one
# A 60-session request took 67 s on 25 Sep; the first version's 60 s timeout cancelled
# queries that were about to answer (33 of 470 requests), and each cancelled one cost the
# whole request again.
REQUEST_TIMEOUT_S = 300.0
NO_FETCH_HOURS = (7, 17)  # never on a trading day between these hours: the watcher's feed
EMPTY_FILE = "_empty.json"
# The v2 playbook screens every price-sensitive announcement in the arena's universe (the
# ASX 300 and the small-cap list), not only the ASX 300. The archive holds the ASX 300's
# announcements only; the live collector's files (data/announcements/live, from 22 Sep 2026)
# hold every company's. So the news stocks outside the list are fetched for the sessions
# around their live-collector days: their news day and the prior sessions v2 and the day
# trader measure "usual volume" against.
NEWS_PRIOR_SESSIONS = 7
READY_WAIT_S = 30 * 60.0  # how long a job waits for a gateway that is not ready


def sessions_between(first: date, last: date) -> list[date]:
    """ASX sessions from `first` to `last` inclusive, oldest first (the exchange calendar)."""
    import exchange_calendars as xc
    import pandas as pd

    cal = xc.get_calendar("XASX")
    return [t.date() for t in cal.sessions_in_range(pd.Timestamp(first), pd.Timestamp(last))]


def code_dir(root: Path, code: str) -> Path:
    return root / safe_stem(code.upper())


def cache_path(root: Path, code: str, day: date) -> Path:
    return code_dir(root, code) / f"{day.isoformat()}.parquet"


def load_empty(root: Path, code: str) -> set[date]:
    """Sessions IBKR answered a span for and returned no bars on: the stock did not trade."""
    p = code_dir(root, code) / EMPTY_FILE
    if not p.exists():
        return set()
    try:
        return {date.fromisoformat(d) for d in json.loads(p.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return set()


def mark_empty(root: Path, code: str, days) -> None:
    have = load_empty(root, code) | set(days)
    body = json.dumps(sorted(d.isoformat() for d in have))
    write_text_atomic(body, code_dir(root, code) / EMPTY_FILE)


def chunks_by_sessions(sessions: list[date], n: int = CHUNK_SESSIONS) -> list[list[date]]:
    """The sessions (oldest first) grouped into chunks of at most n, NEWEST CHUNK FIRST and
    each chunk oldest-first, so the recent window fills in for the whole list before
    anything older is asked for."""
    n = max(1, int(n))
    out = []
    end = len(sessions)
    while end > 0:
        start = max(0, end - n)
        out.append(list(sessions[start:end]))
        end = start
    return out


def span_to_fetch(root: Path, code: str, group: list[date]) -> tuple[date, date, int] | None:
    """The sessions of `group` still to ask for: (first missing, last missing, sessions in
    between inclusive), or None when every session is on disk or known to be empty."""
    empty = load_empty(root, code)
    missing = [d for d in group if d not in empty and not cache_path(root, code, d).exists()]
    if not missing:
        return None
    first, last = missing[0], missing[-1]
    return first, last, sum(1 for d in group if first <= d <= last)


def coverage(root: Path, codes: list[str], sessions: list[date]) -> dict:
    """What is on disk for the window: per code, sessions held, known empty, missing; the
    days missing for most codes; how many codes are complete."""
    per_code = {}
    missing_by_day: dict[date, int] = {}
    for c in codes:
        empty = load_empty(root, c)
        held = [d for d in sessions if cache_path(root, c, d).exists()]
        missing = [d for d in sessions if d not in empty and not cache_path(root, c, d).exists()]
        for d in missing:
            missing_by_day[d] = missing_by_day.get(d, 0) + 1
        per_code[c] = {"held": len(held), "empty": len(empty & set(sessions)),
                       "missing": len(missing),
                       "first": held[0].isoformat() if held else None}  # fmt: skip
    complete = sorted(c for c, v in per_code.items() if v["missing"] == 0)
    worst_days = sorted(missing_by_day.items(), key=lambda kv: (-kv[1], kv[0]))[:15]
    return {
        "codes": len(codes), "sessions": len(sessions),
        "stock_days_held": sum(v["held"] for v in per_code.values()),
        "stock_days_empty": sum(v["empty"] for v in per_code.values()),
        "stock_days_missing": sum(v["missing"] for v in per_code.values()),
        "codes_complete": len(complete),
        "codes_partial": sorted((c, v["missing"]) for c, v in per_code.items() if v["missing"]),
        "days_missing_for_most": [(d.isoformat(), n) for d, n in worst_days],
        "per_code": per_code,
    }  # fmt: skip


def news_codes(
    data_dir: Path, sessions: list[date], listed: set[str]
) -> tuple[list[str], list[date]]:
    """The live collector's price-sensitive codes not in `listed`, and the window of sessions
    they need: from NEWS_PRIOR_SESSIONS before the first live day to the window's end."""
    import pandas as pd

    live = Path(data_dir) / "announcements" / "live"
    codes: set[str] = set()
    days: list[date] = []
    for p in sorted(live.glob("*.parquet")) if live.exists() else []:
        try:
            d = date.fromisoformat(p.stem)
            df = pd.read_parquet(p, columns=["code", "price_sensitive"])
        except (ValueError, OSError):
            continue
        ps = df[df["price_sensitive"].fillna(False).astype(bool)]
        got = {str(c).upper() for c in ps["code"]} - listed
        if got:
            codes |= got
            days.append(d)
    if not codes or not days:
        return [], []
    first = min(days)
    before = [s for s in sessions if s < first][-NEWS_PRIOR_SESSIONS:]
    window = before + [s for s in sessions if s >= first]
    return sorted(codes), window


def fetch_all(
    gw, root: Path, sessions: list[date], codes: list[str], chunk: int = CHUNK_SESSIONS,
    workers: int = WORKERS, stop_now=lambda: False, progress=print, log_every: int = 50,
    ready_wait_s: float = READY_WAIT_S, sleep=time.sleep, oldest_first: bool = False,
) -> dict:  # fmt: skip
    """Every span still missing, newest chunk first across every code, through `workers`
    threads. Returns the run's counts. `gw.history_sync(code, duration, end)` is the only
    call made (None for a timeout, a cancelled query or no bars).

    While the gateway is not ready (`gw.ready` False: disconnected, its link to IBKR down, a
    restart in progress) no job is spent: a job waits up to `ready_wait_s` for it to come
    back, and if it does not, the job goes back on the queue and the run stops, leaving the
    rest to the next run (26 Sep 2026: an empty answer from a dead connection used to count
    as a try, so an outage burned the whole queue in minutes)."""
    groups = chunks_by_sessions(sessions, chunk)
    if oldest_first:
        # A second run beside the scheduled one works the window from the other end, so the
        # two meet in the middle instead of asking for the same spans (26 Sep 2026).
        groups = groups[::-1]
    queue: deque = deque((gi, code, 0) for gi in range(len(groups)) for code in codes)
    total = len(queue)
    stats = {"codes": len(codes), "jobs": total, "requests": 0, "bars": 0, "days_written": 0,
             "no_bars_days": 0, "empty": 0, "retried": 0, "skipped": 0, "failed": [],
             "unknown": [], "errors": []}  # fmt: skip
    lock = threading.Lock()
    started = time.monotonic()
    window_first, window_last = sessions[0], sessions[-1]
    gateway_down = threading.Event()

    def is_ready() -> bool:
        return bool(getattr(gw, "ready", True))

    def wait_ready() -> bool:
        waited = 0.0
        while not is_ready():
            if stop_now() or gateway_down.is_set() or waited >= ready_wait_s:
                return False
            sleep(5.0)
            waited += 5.0
        return True

    def unknown(code: str) -> bool:
        return code in (getattr(gw, "unknown_codes", None) or set())

    def job(gi: int, code: str, tries: int) -> None:
        group = groups[gi]
        span = span_to_fetch(root, code, group)
        if span is None:
            with lock:
                stats["skipped"] += 1
            return
        if unknown(code):
            with lock:
                if code not in stats["unknown"]:
                    stats["unknown"].append(code)
            return
        first, last, n = span
        if not wait_ready():
            with lock:
                queue.appendleft((gi, code, tries))
                if not gateway_down.is_set() and not stop_now():
                    gateway_down.set()
                    progress(f"IB Gateway not ready for {ready_wait_s / 60:.0f} min: stopping "
                             f"with {len(queue)} jobs left for the next run")  # fmt: skip
            return
        df = gw.history_sync(code, f"{n} D", end_of(last + timedelta(days=1)))
        if (df is None or not len(df)) and not is_ready():
            with lock:  # the connection went while this was in flight: not the stock's fault
                queue.appendleft((gi, code, tries))
            return
        with lock:
            stats["requests"] += 1
            done = stats["requests"]
        if df is None or not len(df):
            with lock:
                stats["empty"] += 1
                if unknown(code):
                    if code not in stats["unknown"]:
                        stats["unknown"].append(code)
                elif tries == 0:
                    queue.append((gi, code, 1))  # asked once more, after everything else
                    stats["retried"] += 1
                else:
                    stats["failed"].append(f"{code} {first}..{last}")
        else:
            got = sorted({x for x in df.index.date})
            written = 0
            for d in got:
                if window_first <= d <= window_last:
                    part = df[[x == d for x in df.index.date]]
                    if len(part):
                        write_parquet_atomic(part, cache_path(root, code, d))
                        written += 1
            # Sessions inside the range IBKR answered that it returned nothing for: the
            # stock did not trade. Sessions before the range's first day stay missing (IBKR
            # may count a day we do not) and are asked for again as a shorter span.
            none = [d for d in group if got[0] <= d <= got[-1] and d not in set(got)]
            if none:
                mark_empty(root, code, none)
            with lock:
                stats["bars"] += len(df)
                stats["days_written"] += written
                stats["no_bars_days"] += len(none)
        if done % log_every == 0 or not queue:
            el = time.monotonic() - started
            health = getattr(gw, "health", None)
            progress(
                f"{done} requests ({first}..{last} {code}), {stats['days_written']} stock-days "
                f"written, {stats['empty']} empty, {stats['retried']} retried, "
                f"{stats['skipped']} skipped, {len(queue)} jobs left, {el / 60:.0f} min, "
                f"pacing hits {getattr(health, 'pacing_hits', 0)}, reconnects "
                f"{getattr(gw, 'reconnects', 0)}"
            )

    def worker() -> None:
        while True:
            with lock:
                if not queue or stop_now() or gateway_down.is_set():
                    return
                gi, code, tries = queue.popleft()
            try:
                job(gi, code, tries)
            except Exception as e:  # noqa: BLE001 - one bad job must not kill a worker
                log.exception("history job %s %s failed: %s", code, groups[gi][-1], e)
                with lock:
                    stats["errors"].append(
                        f"{code} {groups[gi][0]}..{groups[gi][-1]}: {type(e).__name__}: {e}"
                    )

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, int(workers)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if queue and stop_now():
        stats["stopped_for_market_hours"] = len(queue)
    elif queue and gateway_down.is_set():
        stats["stopped_gateway_not_ready"] = len(queue)
    stats["minutes"] = round((time.monotonic() - started) / 60, 1)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--end", default=None, help="last day, YYYY-MM-DD (default: today)")
    ap.add_argument("--codes", nargs="*", help="only these codes (testing)")
    ap.add_argument("--status", action="store_true", help="what is on disk; fetch nothing")
    ap.add_argument("--client-id", type=int, default=46)
    ap.add_argument("--chunk", type=int, default=CHUNK_SESSIONS, help="sessions a request")
    ap.add_argument("--workers", type=int, default=WORKERS, help="requests in flight")
    ap.add_argument("--news-only", action="store_true",
                    help="only the news stocks outside the list (their live-collector days)")
    ap.add_argument("--oldest-first", action="store_true",
                    help="work the oldest chunk first (a second run beside the scheduled one)")
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
    extra, extra_sessions = ([], []) if args.codes else news_codes(cfg.data_dir, sessions,
                                                                     set(codes))  # fmt: skip
    cov = coverage(root, codes, sessions)
    print(f"window {first}..{last}: {len(sessions)} sessions, {len(codes)} codes; cache {root}")
    if extra:
        ecov = coverage(root, extra, extra_sessions)
        print(f"news stocks outside the list: {len(extra)} codes, {ecov['stock_days_missing']} "
              f"stock-days missing of {len(extra) * len(extra_sessions)}")  # fmt: skip
    print(f"on disk: {cov['stock_days_held']} stock-days, {cov['stock_days_empty']} known "
          f"empty, {cov['stock_days_missing']} missing; {cov['codes_complete']} of "
          f"{len(codes)} codes complete")  # fmt: skip
    if cov["days_missing_for_most"]:
        print("days missing for most codes: " + ", ".join(
            f"{d} ({n})" for d, n in cov["days_missing_for_most"][:8]))  # fmt: skip
    if args.status:
        thin = sorted(cov["per_code"].items(), key=lambda kv: -kv[1]["missing"])[:20]
        print("thinnest:", ", ".join(f"{c} {v['held']}/{len(sessions)}" for c, v in thin))
        return 0

    now = datetime.now(SYD)
    from asxbot.announcements.live import is_trading_day

    if is_trading_day(now.date()) and NO_FETCH_HOURS[0] <= now.hour < NO_FETCH_HOURS[1]:
        print(f"refusing to fetch at {now:%H:%M} on a trading day: the watcher's live feed "
              "has IBKR to itself between 07:00 and 17:00")  # fmt: skip
        return 2
    s = dataclasses.replace(
        settings_from_config(cfg), client_id=int(args.client_id),
        request_timeout_s=REQUEST_TIMEOUT_S, history_concurrency=max(1, int(args.workers)),
    )  # fmt: skip
    gw = LiveGateway(s, history_dir=root).start()
    if not gw.wait_ready(30):
        print(f"IB Gateway not ready: {gw.health.last_error}")
        return 1

    def stop_now() -> bool:
        """07:00 on a trading day: the watcher's feed has IBKR to itself. A run that
        began the evening before (or on the weekend) stops taking jobs here; the next
        run carries on from what is on disk."""
        t = datetime.now(SYD)
        return is_trading_day(t.date()) and NO_FETCH_HOURS[0] <= t.hour < NO_FETCH_HOURS[1]

    if args.news_only:  # the list's window is left to the scheduled run (26 Sep 2026)
        stats = {"codes": 0, "requests": 0, "news_only": True}
    else:
        stats = fetch_all(gw, root, sessions, codes, chunk=args.chunk, workers=args.workers,
                          stop_now=stop_now, progress=lambda s: print(s, flush=True),
                          oldest_first=args.oldest_first)  # fmt: skip
    if extra and not stop_now():
        print(f"news stocks outside the list: {len(extra)} codes over {len(extra_sessions)} "
              f"sessions ({extra_sessions[0]}..{extra_sessions[-1]})", flush=True)  # fmt: skip
        stats["news_codes"] = fetch_all(
            gw, root, extra_sessions, extra, chunk=args.chunk, workers=args.workers,
            stop_now=stop_now, progress=lambda s: print(s, flush=True))  # fmt: skip
    if stats.get("stopped_gateway_not_ready"):
        print(f"stopped at {datetime.now(SYD):%H:%M}: IB Gateway was not ready for "
              f"{READY_WAIT_S / 60:.0f} minutes; {stats['stopped_gateway_not_ready']} jobs "
              "left for the next run", flush=True)  # fmt: skip
    if stats.get("stopped_for_market_hours"):
        print(f"stopped at {datetime.now(SYD):%H:%M} on a trading day with "
              f"{stats['stopped_for_market_hours']} jobs still to do; the next run (17:30) "
              "carries on", flush=True)  # fmt: skip
    gw.stop(10)
    after = coverage(root, codes, sessions)
    after.pop("per_code")
    stats["window"] = [first.isoformat(), last.isoformat()]
    stats["sessions"] = len(sessions)
    stats["coverage"] = after
    if extra:
        ea = coverage(root, extra, extra_sessions)
        ea.pop("per_code")
        stats["news_coverage"] = ea
    out = REPO / "reports" / f"ibkr_history_fetch_{datetime.now(SYD):%Y%m%d_%H%M}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(stats, indent=2, default=str), encoding="utf-8")
    print(f"done: {json.dumps({k: v for k, v in stats.items() if k != 'coverage'}, default=str)}")
    print(f"coverage: {after['stock_days_held']} stock-days held, {after['stock_days_empty']} "
          f"known empty, {after['stock_days_missing']} missing; {after['codes_complete']} of "
          f"{len(codes)} codes complete; report {out}")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
