"""One trading day of the team's paper book, minute by minute (28 Sep 2026).

The simulator's clock rules (lab/tsim/engine.py run_day), kept exactly, on either clock:

  * LIVE (the watcher's thread): real time. The team is asked at 09:40 (pre-open), then only
    when something it asked to be woken for happens (its own alerts, fills, news, call-backs),
    and once after the close (16:20: the decision-maker's notes and lessons, the researcher's
    ideas). While it thinks, the market moves on; what it decides reaches the broker when the
    call returns and fills only from bars that START after that moment.
  * VIRTUAL (the dry run, the calibration replays): the same code on stored history, the
    clock advanced by each call's thinking time, as the simulator does.

Each minute, once its bar is delivered (20 s after it closes): decisions that reached the
broker before it are applied (through the hard limits, limits.py), the broker works every
order against the bar (the simulator's broker: spread, impact, the 20%-of-volume cap, stops
through gaps, auctions), plain code checks the team's alerts, and the team is woken if
anything it asked for happened and it is not still thinking.

Live, a stock the watcher's feed has no line for arrives only with its next poll (~6 minutes).
Its minutes are worked when they arrive, at the prices they had (`deferred`), so a stop is
filled at the bar that reached it, however late the feed brought that bar in.

The book lives in data/arena/team/ (state.json after every minute that changed it, days/,
actions/, calls/, status.json); a watcher restart resumes the same day from it.
"""

from __future__ import annotations

import json
import threading
import time as wall
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from pathlib import Path

from asxbot.arena.team.limits import Limits, killed
from asxbot.io import write_text_atomic
from asxbot.lab.tsim import live as tlive
from asxbot.lab.tsim import llm
from asxbot.lab.tsim.alerts import AlertBook
from asxbot.lab.tsim.anon import NoAnon
from asxbot.lab.tsim.broker import Account, SimBroker
from asxbot.lab.tsim.costs import CostModel
from asxbot.lab.tsim.engine import Decision
from asxbot.lab.tsim.market import (
    CLOSE_AUCTION_SLOT,
    CONTINUOUS_END_SLOT,
    FEED_LAG,
    SLOTS,
    SYD,
    slot_time,
)
from asxbot.lab.tsim.tools import TraderView
from asxbot.log import get_logger

log = get_logger("asxbot.arena.team")

ACCOUNT = "asx_team"
PREOPEN = time(9, 40)
AFTER_CLOSE = time(16, 20)
ORDER_EVENTS = ("fill", "cancelled", "cancelled_partial", "expired", "expired_partial", "filled")


# ------------------------------------------------------------------ clocks
class WallClock:
    live = True

    def now(self) -> datetime:
        return datetime.now(SYD)

    def wait_until(self, t: datetime, stop: threading.Event) -> bool:
        """Sleep until `t`; False if asked to stop first."""
        while True:
            left = (t - self.now()).total_seconds()
            if left <= 0:
                return True
            if stop.wait(min(left, 5.0)):
                return False


class VirtualClock:
    live = False

    def __init__(self, t: datetime):
        self.t = t

    def now(self) -> datetime:
        return self.t

    def wait_until(self, t: datetime, stop: threading.Event) -> bool:
        if stop.is_set():
            return False
        self.t = max(self.t, t)
        return True


# ------------------------------------------------------------------ the book on disk
class Book:
    def __init__(self, root: Path):
        self.root = Path(root)

    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    def day_path(self, day: str) -> Path:
        return self.root / "days" / f"{day}.json"

    def start_path(self, day: str) -> Path:
        return self.root / "start" / f"{day}.json"

    def write_start(self, day: str, d: dict) -> None:
        p = self.start_path(day)
        p.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(json.dumps(d, default=str), p)

    def read_start(self, day: str) -> dict | None:
        p = self.start_path(day)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def actions_path(self, day: str) -> Path:
        return self.root / "actions" / f"{day}.jsonl"

    @property
    def status_path(self) -> Path:
        return self.root / "status.json"

    def load(self) -> dict | None:
        p = self.state_path
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def save(self, st: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        write_text_atomic(json.dumps(st, default=str), self.state_path)

    def write_day(self, day: str, rec: dict) -> None:
        p = self.day_path(day)
        p.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(json.dumps(rec, indent=1, default=str), p)

    def read_day(self, day: str) -> dict | None:
        p = self.day_path(day)
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        except (OSError, ValueError):
            return None

    def append_action(self, day: str, rec: dict) -> None:
        p = self.actions_path(day)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def actions(self, day: str) -> list[dict]:
        p = self.actions_path(day)
        if not p.exists():
            return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def write_status(self, d: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        write_text_atomic(json.dumps(d, indent=1, default=str), self.status_path)


def _alerts_dump(b: AlertBook) -> dict:
    return {
        "alerts": b.alerts,
        "next_id": b.next_id,
        "news_seen": sorted(b.news_seen)[-5000:],
        "fired_scanner": sorted([list(x) for x in b.fired_scanner]),
    }


def _alerts_load(d: dict | None) -> AlertBook:
    b = AlertBook()
    if d:
        b.alerts = d.get("alerts") or {}
        b.next_id = int(d.get("next_id") or 1)
        b.news_seen = set(d.get("news_seen") or [])
        b.fired_scanner = {tuple(x) for x in d.get("fired_scanner") or []}
    return b


def _decision_load(d: dict) -> Decision:
    return Decision(**{k: v for k, v in d.items() if k in Decision.__dataclass_fields__})


def next_session_nights(day: date) -> int:
    from asxbot.arena.replay_ibkr import sessions

    later = [d for d in sessions(day + timedelta(days=1), day + timedelta(days=12)) if d > day]
    return (later[0] - day).days if later else (3 if day.weekday() == 4 else 1)


# ------------------------------------------------------------------ the day
class TeamSession:
    def __init__(
        self,
        cfg,
        day: date,
        root: Path,
        *,
        market,
        clock,
        ask,
        budget,
        conf: dict,
        feed=None,
        pause_dir: Path | None = None,
        register_ideas: bool = True,
        state: dict | None = None,
        codes_fn=None,
        add_codes_fn=None,
        max_wait_s: float = 480.0,
        mode: str = "live",
    ):
        self.cfg, self.day, self.iso = cfg, day, day.isoformat()
        self.root = Path(root)
        self.book = Book(self.root)
        self.conf = conf
        self.m = market
        self.clock = clock
        self.live = bool(clock.live)
        self.mode = mode
        self.budget = budget
        self.codes_fn = codes_fn  # live: the universe as the watcher sees it now
        self.add_codes_fn = add_codes_fn  # live: summaries for codes added during the day
        self.max_wait = timedelta(seconds=max_wait_s)
        st = state if state is not None else (self.book.load() or {})
        start_cash = float(conf.get("start_cash_aud", 20_000))
        acct = (
            Account.from_dict(st["account"])
            if st.get("account")
            else Account(ACCOUNT, start_cash, start_cash, float(conf.get("leverage", 1.0)))
        )
        self.broker = SimBroker(
            acct,
            CostModel.from_config(cfg, conf.get("broker")),
            float(conf.get("max_volume_share", 0.20)),
            market.shortable,
        )
        self.broker.market = market
        self.alerts = _alerts_load(st.get("alerts"))
        self.team = tlive.make_team(
            cfg, self.root, ask=ask, conf=conf, register_ideas=register_ideas
        )
        self.team.gap_after = False
        reader = None
        if cfg is not None and day >= date(2026, 7, 1):
            from asxbot.lab.tsim.reader import cached_reader

            reader = cached_reader(cfg, ask=ask)
        self.view = TraderView(market, self.broker, NoAnon(), None, reader=reader)
        if cfg is not None:
            from asxbot.arena.replay_ibkr import _daily_until

            self.view.daily_store = lambda c, d: _daily_until(cfg, c, d)
        self.team.bind_alerts(self.alerts, NoAnon())
        self.limits = Limits(conf, self.root, pause_dir, feed, live=self.live)
        self.queued: list[dict] = []
        self.pending: list[tuple[datetime, Decision]] = []
        self.busy_until = datetime.combine(day, time(0, 0), tzinfo=SYD)
        self.deferred: dict[str, int] = {}  # code -> the first of its minutes not yet worked
        self.st = st
        self.after_preopen = None
        if st.get("day") != self.iso:
            self._new_day()
        else:
            tlive.restore_day(self.team, st.get("team"))
            self.pending = [
                (datetime.fromisoformat(a), _decision_load(d)) for a, d in st.get("pending") or []
            ]
            self.deferred = {k: int(v) for k, v in (st.get("deferred") or {}).items()}
            if st.get("busy_until"):
                self.busy_until = datetime.fromisoformat(st["busy_until"])
        self.limits.start_equity = float(self.st["start_equity"])
        self.limits.loss_hit = bool(self.st.get("loss_hit"))

    # -------------------------------------------------------------- state
    def _new_day(self) -> None:
        self.alerts.new_day()
        self.team._reset_day()
        acct = self.broker.acct
        start = acct.marks[-1]["equity"] if acct.marks else acct.equity(self.m.prev_close)
        self.st = {
            "day": self.iso,
            "mode": self.mode,
            "worked_slot": -1,
            "preopen_done": False,
            "after_close_done": False,
            "start_equity": round(float(start), 2),
            "wakes": 0,
            "calls": 0,
            "usage": {},
            "think_s": 0.0,
            "fills": 0,
            "refused": [],
            "rejected": [],
            "log": [],
            "silenced": "",
            "kill_done": False,
            "sweep_done": False,
            "loss_hit": False,
            "errors": 0,
            "models": tlive.models(self.team),
            "universe": sorted(self.m.bars),
        }
        # The book as the day starts: what a calibration replay of this day starts from.
        try:
            self.book.write_start(
                self.iso, {"account": acct.to_dict(), "alerts": _alerts_dump(self.alerts)}
            )
        except OSError as e:
            log.error("the team's start-of-day book could not be written: %s", e)

    def _save(self) -> None:
        self.st.update(
            {
                "account": self.broker.acct.to_dict(),
                "alerts": _alerts_dump(self.alerts),
                "team": tlive.dump_day(self.team),
                "deferred": self.deferred,
                "pending": [(a.isoformat(), asdict(d)) for a, d in self.pending],
                "busy_until": self.busy_until.isoformat(),
                "loss_hit": self.limits.loss_hit,
                "universe": sorted(self.m.bars),
            }
        )
        try:
            self.book.save(self.st)
        except OSError as e:  # Drive refused a write; the next minute's save carries it
            log.error("the team's book could not be saved: %s", e)

    def status(self, phase: str) -> dict:
        acct = self.broker.acct
        return {
            "at": datetime.now(SYD).isoformat(timespec="seconds"),
            "day": self.iso,
            "mode": self.mode,
            "phase": phase,
            "clock": self.m.now.isoformat(timespec="seconds"),
            "worked_to": slot_time(self.day, self.st["worked_slot"]).strftime("%H:%M")
            if self.st["worked_slot"] >= 0
            else None,
            "equity": round(acct.equity(self.m.last), 2),
            "cash": round(acct.cash, 2),
            "positions": {c: p.qty for c, p in acct.positions.items()},
            "working": len(acct.working()),
            "wakes": self.st["wakes"],
            "calls": self.st["calls"],
            "silenced": self.st.get("silenced") or "",
            "loss_hit": self.limits.loss_hit,
            "deferred": self.deferred,
            "stocks": len(self.m.bars),
        }

    # -------------------------------------------------------------- the day
    def at(self, t: time) -> datetime:
        return datetime.combine(self.day, t, tzinfo=SYD)

    def run(self, stop: threading.Event | None = None) -> dict:
        stop = stop or threading.Event()
        llm.clear_stop()
        if self.st.get("after_close_done"):
            return self.result("done")
        if not self.st["preopen_done"]:
            pre = self.at(PREOPEN)
            if not self.clock.wait_until(pre, stop):
                return self._stopped()
            self._refresh_universe()
            self._ingest()
            self.m.now = max(self.clock.now(), pre) if self.live else pre
            self.alerts.mark_news_seen(self.view)
            self._housekeeping(-1)
            self._call(self.team.pre_open, {"phase": "pre_open"})
            self.st["preopen_done"] = True
            self._save()
            if self.after_preopen is not None:
                try:
                    self.after_preopen()
                except Exception as e:  # noqa: BLE001
                    log.error("after the pre-open: %s", e)
        for slot in range(self.st["worked_slot"] + 1, SLOTS):
            delivered = slot_time(self.day, slot + 1) + FEED_LAG
            if not self.clock.wait_until(delivered, stop):
                return self._stopped()
            if self.live and slot % 10 == 0:
                self._refresh_universe()
            if self.live and slot >= CLOSE_AUCTION_SLOT:
                self._await_close(slot, delivered, stop)
            self._ingest()
            self._work_deferred(slot)
            self._apply_due(delivered)
            self.m.now = max(delivered, slot_time(self.day, slot))
            if self.live:
                for code in {o.code for o in self.broker.acct.working()}:
                    if code not in self.deferred and not self.m.has_minute(code, slot):
                        self.deferred[code] = slot
            fills = self.broker.work_slot(slot)
            self.st["fills"] += len(fills)
            reasons = self.alerts.check(self.view, slot)
            evs, self.broker.events = self.broker.events, []
            self.queued += [{"type": "order", **e} for e in evs if e["kind"] in ORDER_EVENTS]
            self.queued += reasons
            self.st["worked_slot"] = slot
            self._housekeeping(slot)
            if self.m.now >= self.busy_until and self.queued:
                why, self.queued = self.queued[:], []
                self._call(self.team.wake, why, {"phase": "session", "slot": slot})
            if self.live:
                if evs or reasons or fills or self.pending or slot % 5 == 0:
                    self._save()
                self._status("session")
        end = self.at(AFTER_CLOSE)
        if not self.clock.wait_until(end, stop):
            return self._stopped()
        for at, d in sorted(self.pending, key=lambda p: p[0]):
            self._apply(at, d)
        self.pending.clear()
        self.m.now = end
        self.broker.events = []
        self._call(self.team.after_close, {"phase": "after_close"})
        for at, d in sorted(self.pending, key=lambda p: p[0]):
            self._apply(max(at, end), d)
        self.pending.clear()
        self.broker._expire_day_orders(end)
        self.broker.events = []
        nights = next_session_nights(self.day)
        borrow = self.broker.overnight(nights, lambda c: self.m.last(c))
        acct = self.broker.acct
        eq = acct.equity(self.m.last)
        acct.marks.append(
            {
                "day": self.iso,
                "equity": round(eq, 2),
                "cash": round(acct.cash, 2),
                "borrow": borrow,
                "positions": len(acct.positions),
            }
        )
        self.st["after_close_done"] = True
        rec = self.result("done")
        self.book.write_day(self.iso, rec)
        self._save()
        self._status("closed")
        return rec

    def _stopped(self) -> dict:
        self._save()
        self._status("stopped")
        return self.result("stopped")

    def _status(self, phase: str) -> None:
        try:
            self.book.write_status(self.status(phase))
        except OSError:
            pass

    # -------------------------------------------------------------- market data
    def _refresh_universe(self) -> None:
        if self.codes_fn is None:
            return
        try:
            codes = [c for c in self.codes_fn() if c not in self.m.bars]
        except Exception as e:  # noqa: BLE001 - the universe we have stands
            log.warning("the team could not refresh its universe: %s", e)
            return
        if codes:
            if self.add_codes_fn is not None:
                try:
                    self.add_codes_fn(codes)
                except Exception as e:  # noqa: BLE001
                    log.warning("summaries for %d new stock(s) failed: %s", len(codes), e)
            self.m.add_codes(codes)
            log.info("the team's market now has %d stocks (+%d)", len(self.m.bars), len(codes))

    def _ingest(self) -> None:
        fn = getattr(self.m, "ingest", None)
        if fn is not None:
            fn()

    def _work_deferred(self, slot: int) -> None:
        """Minutes of the team's own stocks the feed brought in late: worked now, in order, at
        the prices they had - or given up (no trade) after `max_wait`."""
        if not self.deferred:
            return
        now = self.clock.now()
        for code, first in list(self.deferred.items()):
            for s in range(first, slot):
                if self.m.has_minute(code, s):
                    b = self.m.bars.get(code)
                    start = slot_time(self.day, s)
                    orders = [
                        o
                        for o in self.broker.acct.working(code)
                        if datetime.fromisoformat(o.submitted_at) < start
                    ]
                    if b is not None and b.v[s] > 0 and orders:
                        saved = self.m.now
                        self.m.now = slot_time(self.day, s + 1) + FEED_LAG
                        fills = self.broker._work_bar(code, orders, s, b)
                        self.m.now = saved
                        self.st["fills"] += len(fills)
                        if fills:
                            self.st.setdefault("late_fills", []).append(
                                {
                                    "code": code,
                                    "minute": start.strftime("%H:%M"),
                                    "worked_at": now.strftime("%H:%M:%S"),
                                    "fills": len(fills),
                                }
                            )
                    self.deferred[code] = s + 1
                elif now - (slot_time(self.day, s + 1) + FEED_LAG) > self.max_wait:
                    self.deferred[code] = s + 1  # never came: a minute with no trade
                else:
                    break
            if self.deferred.get(code, slot) >= slot:
                self.deferred.pop(code, None)

    def _await_close(self, slot: int, delivered: datetime, stop) -> None:
        """Before the closing auction's day-order expiry: the team's late stocks caught up
        (at most `max_wait`; the wait goes through the session's clock)."""
        while self.deferred and self.clock.now() - delivered < self.max_wait:
            self._ingest()
            self._work_deferred(slot)
            if not self.deferred:
                return
            if not self.clock.wait_until(self.clock.now() + timedelta(seconds=5), stop):
                return

    # -------------------------------------------------------------- the team
    def _silenced(self) -> str:
        k, why = killed(self.root)
        if k:
            return why
        if self.budget is not None:
            ok, why = self.budget(self.cfg)
            if not ok:
                return why
        return ""

    def _call(self, fn, *args) -> None:
        why = self._silenced()
        if why:
            if self.st.get("silenced") != why:
                log.warning("the team is not asked: %s", why)
                self.st["log"].append({"at": self.m.now.strftime("%H:%M:%S"), "silenced": why})
            self.st["silenced"] = why
            return
        self.st["silenced"] = ""
        t0 = self.m.now
        started = wall.monotonic()
        try:
            d = fn(self.view, *args)
        except Exception as e:  # noqa: BLE001 - a failed wake must never stop the book
            log.exception("the team's %s failed: %s", getattr(fn, "__name__", "call"), e)
            self.st["errors"] += 1
            d = None
        self.m.now = t0
        stopped = llm.stopped()
        if stopped:
            self.st["silenced"] = stopped
            self.st["log"].append({"at": t0.strftime("%H:%M:%S"), "silenced": stopped})
            llm.clear_stop()
        if d is None:
            return
        self.st["wakes"] += 1
        self.st["calls"] += d.calls
        self.st["think_s"] = round(self.st["think_s"] + d.latency_s, 1)
        for k, v in (d.usage or {}).items():
            if isinstance(v, (int, float)):
                self.st["usage"][k] = round(self.st["usage"].get(k, 0) + v, 6)
        if self.live:
            at = max(self.clock.now(), t0 + timedelta(seconds=1))
        else:
            at = t0 + timedelta(seconds=max(1.0, d.latency_s))
        self.busy_until = at
        self.pending.append((at, d))
        phase = (args[-1] or {}).get("phase", "") if args and isinstance(args[-1], dict) else ""
        self.st["log"].append(
            {
                "at": t0.strftime("%H:%M:%S"),
                "lands": at.strftime("%H:%M:%S"),
                "phase": phase,
                "woken_by": _reasons_brief(args[0]) if phase == "session" else [],
                "calls": d.calls,
                "think_s": round(d.latency_s, 1),
                "wall_s": round(wall.monotonic() - started, 1),
                "orders": len(d.actions),
                "note": (d.note or "")[:400],
            }
        )

    def _apply_due(self, delivered: datetime) -> None:
        due = sorted([p for p in self.pending if p[0] <= delivered], key=lambda p: p[0])
        self.pending = [p for p in self.pending if p[0] > delivered]
        for at, d in due:
            self._apply(at, d)

    def _apply(self, at: datetime, d: Decision) -> None:
        saved = self.m.now
        self.m.now = at
        for a in d.actions:
            sent, why = self.limits.check(self.view, a, at)
            rec = {"at": at.isoformat(timespec="seconds"), "action": a, "sent": sent, "limits": why}
            if sent is None:
                r = {"ok": False, "error": why, "action": a}
                self.st["refused"].append(
                    {"at": at.strftime("%H:%M:%S"), "code": a.get("code"), "why": why[:200]}
                )
            else:
                r = self.view.do(sent, at, "team")
                if why:
                    r = {**r, "note": why}
            rec["result"] = r
            self._append_action(rec)
            if not r.get("ok"):
                self.st["rejected"].append(
                    {
                        "at": at.strftime("%H:%M:%S"),
                        "code": a.get("code"),
                        "error": str(r.get("error"))[:200],
                    }
                )
                self.queued.append({"type": "rejected", **r})
            elif why:
                self.queued.append({"type": "limits", "id": r.get("id"), "note": why})
        for spec in d.alerts:
            r = self.alerts.add(spec, self.view.anon)
            if not r.get("ok"):
                self.queued.append({"type": "alert_refused", **r})
        self.alerts.clear(d.clear_alerts)
        if d.next_wake:
            self.alerts.add(
                {"type": "time", "at": d.next_wake, "note": "call-back"}, self.view.anon
            )
        self.m.now = saved

    def _append_action(self, rec: dict) -> None:
        try:
            self.book.append_action(self.iso, rec)
        except OSError as e:
            log.error("the team's action record could not be written: %s", e)

    # -------------------------------------------------------------- code's own duties
    def _housekeeping(self, slot: int) -> None:
        acct = self.broker.acct
        now = self.m.now
        k, why = killed(self.root)
        if k and not self.st.get("kill_done"):
            for o in list(acct.working()):
                self.broker.cancel(o.id, now, "kill switch")
            for code, p in list(acct.positions.items()):
                self._close(code, p.qty, "market", f"kill switch: {why}", now)
            self.st["kill_done"] = True
            self.st["log"].append({"at": now.strftime("%H:%M:%S"), "kill": why})
            log.warning("team kill switch: working orders cancelled, positions closed at market")
        elif not k and self.st.get("kill_done"):
            self.st["kill_done"] = False
        # Silenced (the usage stop, the Foreman's pause) with positions held and nothing asked
        # to manage them: closed in the closing auction rather than carried unmanaged.
        if (
            slot >= CONTINUOUS_END_SLOT
            and slot < CLOSE_AUCTION_SLOT
            and acct.positions
            and not self.st.get("sweep_done")
            and self._silenced()
        ):
            for code, p in list(acct.positions.items()):
                held_exits = sum(o.remaining for o in acct.working(code) if o.type == "moc")
                if held_exits < abs(p.qty):
                    self._close(
                        code,
                        p.qty,
                        "moc",
                        "the team could not be asked ("
                        + self._silenced()[:120]
                        + "): closed in the closing auction",
                        now,
                    )
            self.st["sweep_done"] = True

    def _close(self, code: str, qty: int, typ: str, why: str, now: datetime) -> None:
        try:
            o = self.broker.place(
                code,
                "sell" if qty > 0 else "cover",
                abs(qty),
                typ,
                at=now,
                reduce_only=True,
                by="code",
                why=why,
            )
            self._append_action(
                {
                    "at": now.isoformat(timespec="seconds"),
                    "by": "code",
                    "action": {
                        "op": "place",
                        "code": code,
                        "type": typ,
                        "side": o.side,
                        "qty": abs(qty),
                    },
                    "result": {"ok": True, "id": o.id},
                    "limits": why,
                }
            )
        except Exception as e:  # noqa: BLE001
            log.error("code could not close %s: %s", code, e)

    # -------------------------------------------------------------- the result
    def result(self, how: str) -> dict:
        acct = self.broker.acct
        eq = acct.equity(self.m.last)
        placed = [o for o in acct.orders.values() if o.submitted_at[:10] == self.iso]
        trades = [t for t in acct.trades if t.closed_at[:10] == self.iso]
        return {
            "day": self.iso,
            "how": how,
            "mode": self.mode,
            "start_equity": self.st["start_equity"],
            "equity_close": round(eq, 2),
            "pnl": round(eq - self.st["start_equity"], 2),
            "wakes": self.st["wakes"],
            "calls": self.st["calls"],
            "usage": self.st["usage"],
            "think_s": self.st["think_s"],
            "fills": self.st["fills"],
            "orders_placed": len([o for o in placed if o.by == "team"]),
            "orders_by_code": len([o for o in placed if o.by == "code"]),
            "refused_by_limits": self.st["refused"],
            "rejected": self.st["rejected"],
            "trades": [
                {
                    "code": t.code,
                    "direction": t.direction,
                    "opened": t.opened_at[:16],
                    "closed": t.closed_at[:16],
                    "net": round(t.net, 2),
                    "fees": round(t.fees, 2),
                    "gross": round(t.gross, 2),
                }
                for t in trades
            ],
            "carried": {c: {"qty": p.qty, "avg": p.avg} for c, p in acct.positions.items()},
            "fees_today": round(sum(o.commission for o in placed), 2),
            "silenced": self.st.get("silenced") or "",
            "loss_hit": self.limits.loss_hit,
            "killed": killed(self.root)[1],
            "errors": self.st.get("errors", 0),
            "late_fills": self.st.get("late_fills", []),
            "models": self.st.get("models"),
            "wake_log": self.st["log"],
            "stocks": len(self.m.bars),
            "universe": sorted(self.m.bars),
        }


def _reasons_brief(reasons) -> list[str]:
    out = []
    for r in (reasons or [])[:12]:
        if isinstance(r, dict):
            out.append(
                " ".join(str(r.get(k)) for k in ("type", "code", "kind", "alert") if r.get(k))
            )
    return out


__all__ = ["ACCOUNT", "Book", "TeamSession", "VirtualClock", "WallClock", "next_session_nights"]
