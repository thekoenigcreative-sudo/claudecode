"""The watcher's side of the team, and the builders of its sessions (28 Sep 2026).

`tick(arena, now)` is called every watcher cycle (guarded: it can never stop the watcher). On
a trading day from `arena.team.start` (the first live day, 30 Sep), from 09:00 it starts ONE
thread that runs the team's day (session.py) and checks it is alive each cycle; a thread that
died is started again after two minutes (at most six times a day), resuming the day from the
book. The thread reads the bars the watcher's feed already holds and asks IBKR for nothing.
The frozen books never wait for it: every model call happens on its own thread.

`live_session` builds the live day; `replay_session` the same day on stored history with a
virtual clock (the dry run on a recorded day, and the calibration replays).
"""

from __future__ import annotations

import functools
import json
import threading
import time as wall
from datetime import date, datetime, time, timedelta
from pathlib import Path

from asxbot.arena.team.limits import conf_of, usage_budget
from asxbot.arena.team.market import AnnouncementFiles, LiveMarket, build_summaries
from asxbot.arena.team.session import TeamSession, VirtualClock, WallClock
from asxbot.lab.tsim import llm
from asxbot.lab.tsim.market import SYD
from asxbot.log import get_logger

log = get_logger("asxbot.arena.team")

START_FROM = time(9, 0)  # the thread builds its summaries before the 09:40 pre-open
LAST_START = time(16, 45)
MAX_RESTARTS = 6
RESTART_GAP_S = 120.0


def team_root(cfg) -> Path:
    return Path(cfg.data_dir) / "arena" / "team"


def shortable(cfg) -> set[str]:
    try:
        from asxbot.data.universe import asx200_codes

        return set(asx200_codes(cfg.data_dir, cfg.get("collector.user_agent")))
    except Exception as e:  # noqa: BLE001 - no list: nothing is shortable
        log.warning("the team has no ASX 200 list (no shorts today): %s", e)
        return set()


def universe(cfg, day: date, allowed: set[str] | None = None, extra=()) -> list[str]:
    """The stocks the team can see on `day`: the day trader's liquid universe (what the
    watcher's feed streams and polls), the day's price-sensitive news stocks in the arena's
    universe (which the feed pins), and anything the book holds."""
    from asxbot.arena.daytrader import load_state, news_today

    codes = {str(c).upper() for c in load_state(cfg.data_dir, day).get("universe") or []}
    news = news_today(cfg.data_dir, day)
    if allowed:
        news &= {c.upper() for c in allowed}
    return sorted(codes | news | {str(c).upper() for c in extra})


def held_codes(root: Path) -> list[str]:
    try:
        st = json.loads((Path(root) / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    acct = st.get("account") or {}
    held = set(acct.get("positions") or {})
    held |= {o["code"] for o in (acct.get("orders") or {}).values() if o.get("status") == "working"}
    return sorted(held)


def _ask(conf: dict, budget):
    return functools.partial(
        llm.ask, budget=budget, ledger=False, timeout_s=int(conf.get("call_timeout_s", 300))
    )


def live_session(
    cfg, day: date, feed, allowed: set[str] | None = None, after_preopen=None
) -> TeamSession:
    from asxbot.arena.replay_ibkr import history_root
    from asxbot.lab.tsim.market import History

    conf = conf_of(cfg)
    root = team_root(cfg)
    held = held_codes(root)
    codes = universe(cfg, day, allowed, held)
    summ = build_summaries(codes, day)
    m = LiveMarket(
        day,
        codes,
        summ,
        shortable(cfg),
        source=feed.bars_today,
        announcements=AnnouncementFiles(cfg, day),
        covered=getattr(feed, "covered", None),
    )
    budget = usage_budget(conf)

    def add_codes(new):
        summ.build(History(history_root()), list(new), workers=1, until=day)

    s = TeamSession(
        cfg,
        day,
        root,
        market=m,
        clock=WallClock(),
        ask=_ask(conf, budget),
        budget=budget,
        conf=conf,
        feed=feed,
        pause_dir=cfg.data_dir,
        register_ideas=bool(conf.get("register_ideas", True)),
        codes_fn=lambda: universe(cfg, day, allowed, held_codes(root)),
        add_codes_fn=add_codes,
        mode="live",
    )
    s.after_preopen = after_preopen
    return s


def replay_session(
    cfg,
    day: date,
    root: Path,
    *,
    codes: list[str],
    state: dict | None = None,
    max_rise: float | None = 0.04,
    register_ideas: bool = False,
    lessons_from: Path | None = None,
    ask=None,
) -> TeamSession:
    """The team's day on stored history (IBKR's 1-minute history cache, the announcements the
    live collector kept), a virtual clock, a book in `root`. `state`: the account and alerts
    to start from (a live day's start, for the calibration); None: the book already in `root`
    (a fresh A$20,000 in an empty folder - the dry run)."""
    from asxbot.arena.replay_ibkr import announcements, history_root
    from asxbot.lab.tsim import live as tlive
    from asxbot.lab.tsim.market import History, Market
    from asxbot.lab.tsim.run import _ann_window

    conf = conf_of(cfg)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if lessons_from is not None:
        tlive.copy_entries(lessons_from, root, day.isoformat())
    summ = build_summaries(codes, day)
    ann = announcements(cfg, day - timedelta(days=7), day)
    m = Market(
        day, History(history_root()), list(codes), _ann_window(ann, day), summ, shortable(cfg)
    )
    budget = usage_budget(conf, max_rise=max_rise)
    return TeamSession(
        cfg,
        day,
        root,
        market=m,
        clock=VirtualClock(datetime.combine(day, time(8, 0), tzinfo=SYD)),
        ask=ask or _ask(conf, budget),
        budget=budget,
        conf=conf,
        feed=None,
        pause_dir=cfg.data_dir,
        register_ideas=register_ideas,
        state=dict(state) if state is not None else None,
        mode="replay",
    )


# ------------------------------------------------------------------ the watcher's thread
_T: dict = {
    "thread": None,
    "stop": None,
    "done": None,
    "day": None,
    "restarts": 0,
    "last_start": 0.0,
    "said_gave_up": False,
}


def status() -> dict:
    t = _T.get("thread")
    return {
        "alive": bool(t is not None and t.is_alive()),
        "day": str(_T.get("day")),
        "done": str(_T.get("done")),
        "restarts": _T.get("restarts"),
    }


def tick(arena, now: datetime) -> str:
    """Start (or restart) today's team thread when it is due; returns what it did."""
    from asxbot.announcements.live import is_trading_day

    cfg = arena.cfg
    conf = conf_of(cfg)
    if not conf.get("enabled"):
        return "off"
    local = now.astimezone(SYD)
    day = local.date()
    start = conf.get("start")
    if start and day < date.fromisoformat(str(start)):
        return f"starts {start}"
    if local.weekday() >= 5 or not is_trading_day(day):
        return "not a trading day"
    if not (START_FROM <= local.time() <= LAST_START):
        return "outside its hours"
    if _T["day"] != day:
        _T.update(day=day, restarts=0, said_gave_up=False, done=None)
    if _T["done"] == day:
        return "done for today"
    t = _T["thread"]
    if t is not None and t.is_alive():
        return "running"
    if _T["restarts"] >= MAX_RESTARTS:
        if not _T["said_gave_up"]:
            log.error(
                "the team's thread died %d times today; not started again until "
                "tomorrow (its book is in data/arena/team)",
                MAX_RESTARTS,
            )
            _T["said_gave_up"] = True
        return "gave up for today"
    if _T["last_start"] and wall.monotonic() - _T["last_start"] < RESTART_GAP_S:
        return "waiting to restart"
    from asxbot.arena.watch import day_view

    feed = day_view(arena, now).feed
    stop = threading.Event()
    th = threading.Thread(
        target=_run, args=(cfg, day, feed, set(arena.universe), stop), name="ai-team", daemon=True
    )
    _T.update(thread=th, stop=stop, last_start=wall.monotonic())
    if _T["restarts"]:
        log.warning("the team's thread is started again (%d)", _T["restarts"])
    _T["restarts"] += 1
    th.start()
    return "started"


def stop(timeout: float = 10.0) -> None:
    if _T.get("stop") is not None:
        _T["stop"].set()
    t = _T.get("thread")
    if t is not None:
        t.join(timeout)


def _run(cfg, day: date, feed, allowed: set[str], stop: threading.Event) -> None:
    try:
        log.info("the AI team's paper book: today's session starting")
        s = live_session(cfg, day, feed, allowed, after_preopen=lambda: first_day_line(cfg, day))
        res = s.run(stop)
        log.info(
            "the AI team's day: %s, P&L %+.2f, %d wakes, %d calls",
            res["how"],
            res["pnl"],
            res["wakes"],
            res["calls"],
        )
        if res["how"] == "done":
            _T["done"] = day
    except Exception as e:  # noqa: BLE001 - a thread that dies is started again by tick
        log.exception("the AI team's session failed: %s", e)


# ------------------------------------------------------------------ the one line to Rick
def first_day_line(cfg, day: date) -> bool:
    """Rick, 28 Sep: when the team is live, ONE line in the Trader chat - from when, and where
    he sees its results. Sent once, after its first live pre-open."""
    from asxbot.io import write_text_atomic

    p = team_root(cfg) / "announced.json"
    if p.exists():
        return False
    text = (
        f"The AI trading team is trading on paper from {day:%a %d %b}: its own A$20,000 "
        "book beside the frozen test. Its results are in each evening report, under "
        "'AI team'."
    )
    ok = False
    try:
        from asxbot.arena.notify import build_notifier

        ok = build_notifier(cfg).send(text)
    except Exception as e:  # noqa: BLE001 - a message must never stop the team
        log.error("the team's first-day line was not sent: %s", e)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        json.dumps(
            {"at": datetime.now(SYD).isoformat(timespec="seconds"), "sent": bool(ok), "text": text},
            indent=1,
        ),
        p,
    )
    return ok


__all__ = [
    "first_day_line",
    "live_session",
    "replay_session",
    "status",
    "stop",
    "team_root",
    "tick",
    "universe",
]
