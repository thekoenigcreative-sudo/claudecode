"""The team's hard limits, in plain code: what no model can talk its way past (28 Sep 2026).

Every order action the decision-maker sends goes through `Limits.check` before the broker sees
it. Exits, cancels and stop changes always pass. An action that ADDS exposure (a new or bigger
position, or the part of a sell that would flip a long into a short) is refused, with the
reason told back to the team, when:

  * the KILL SWITCH is on (data/arena/team/kill.json, `asxbot arena team kill`): the book is
    closed at market and the team is not asked anything until it is lifted;
  * the DAILY LOSS LIMIT is hit: equity down `daily_loss_limit_pct` (15%, Level 1's figure,
    ARENA.md) from the day's start - no new position until tomorrow, exits allowed;
  * Rick has paused new entries for the day (arena/pause.py: every book, the same switch);
  * the LIVE FEED is down or stale, or the stock's own bars are stale (live only; read-only
    checks - the team never asks IBKR for a poll, so the frozen books' feed is untouched);
  * and it is CUT to the CLOSING-VOLUME CAP: the position may never exceed `close_volume_cap`
    (20%) of the stock's usual closing-auction volume (its 14-session average), so it can
    always be sold in one closing auction.
The account's own limits (1x leverage, shorts only in the ASX 200) are the broker's.

`usage_budget` is the team's Claude stop: no model call once the plan's weekly usage reaches
`usage_stop` (70%, Rick's stop for every bot), nor while the Foreman has Claude paused. The
team then goes quiet; its resting stops and brackets keep working in code, and anything still
held is closed in the closing auction (session.py) rather than carried unmanaged.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SYD = ZoneInfo("Australia/Sydney")

DEFAULTS = {
    "daily_loss_limit_pct": 15.0,
    "close_volume_cap": 0.20,
    "usage_stop": 0.70,
    "polled_fresh_s": 180.0,
}


def conf_of(cfg) -> dict:
    c = dict((cfg.get("arena") or {}).get("team") or {}) if cfg is not None else {}
    return {**DEFAULTS, **c}


# ------------------------------------------------------------------ the kill switch
def kill_path(root: Path) -> Path:
    return Path(root) / "kill.json"


def killed(root: Path) -> tuple[bool, str]:
    p = kill_path(root)
    if not p.exists():
        return False, ""
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    return (
        True,
        f"kill switch on since {str(d.get('at', '?'))[:16]} ({d.get('why') or 'no reason given'})",
    )


def set_kill(root: Path, why: str, by: str = "Rick") -> Path:
    from asxbot.io import write_text_atomic

    p = kill_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        json.dumps(
            {"at": datetime.now(SYD).isoformat(timespec="seconds"), "by": by, "why": why[:300]},
            indent=1,
        ),
        p,
    )
    return p


def lift_kill(root: Path) -> bool:
    p = kill_path(root)
    if p.exists():
        p.unlink()
        return True
    return False


# ------------------------------------------------------------------ Claude usage
def foreman_paused(now: datetime | None = None) -> tuple[bool, str]:
    home = Path(os.environ.get("FOREMAN_HOME") or Path.home() / ".foreman")
    try:
        d = json.loads((home / "claude_pause.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, ""
    if not d.get("paused"):
        return False, ""
    try:
        at = datetime.fromisoformat(str(d.get("updated")))
        if (now or datetime.now(SYD)) - at > timedelta(minutes=30):
            return False, ""  # a stale file: the Foreman is not saying so now
    except (TypeError, ValueError):
        pass
    return True, f"Claude is paused by the Foreman ({d.get('why') or 'no reason'})"


def weekly_usage() -> float | None:
    """The newest reading of the plan's weekly meter: the one every tsim call records
    (lab_local/usage.json), or the Foreman's feed (FOREMAN_HOME/usage/latest.json)."""
    from asxbot.lab import store

    home = Path(os.environ.get("FOREMAN_HOME") or Path.home() / ".foreman")
    best = (None, "")
    for p in (store.lab_local() / "usage.json", home / "usage" / "latest.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("seven_day") is not None and str(d.get("at", "")) > best[1]:
            best = (float(d["seven_day"]), str(d.get("at", "")))
    return best[0]


def usage_budget(conf: dict, *, max_rise: float | None = None):
    """The budget function for llm.ask: (ok, why). `max_rise` caps what one run may add to the
    week (the dry run and the calibration replays)."""
    start = {"wk": None}

    def check(_cfg=None) -> tuple[bool, str]:
        stop = float(os.environ.get("ASXBOT_TEAM_USAGE_STOP") or conf.get("usage_stop", 0.70))
        wk = weekly_usage()
        if start["wk"] is None:
            start["wk"] = wk
        if wk is not None and wk >= stop:
            return False, f"weekly Claude usage {wk:.0%} is at the team's stop ({stop:.0%})"
        if (
            max_rise is not None
            and wk is not None
            and start["wk"] is not None
            and wk - start["wk"] >= max_rise
        ):
            return False, (
                f"this run has used its share of the week ({wk - start['wk']:.1%} "
                f"of {max_rise:.0%})"
            )
        p, why = foreman_paused()
        if p:
            return False, why
        return True, ""

    return check


# ------------------------------------------------------------------ orders
def feed_fresh(feed, code: str, now: datetime, polled_fresh_s: float = 180.0) -> tuple[bool, str]:
    """May a NEW entry be decided on `code` now, on the watcher's feed? Read-only: the feed's
    own verdict (down, delayed, stale index) with no stock named - which queues nothing - and
    the stock's own freshness from what the feed already holds."""
    if feed is None:
        return True, ""
    fn = getattr(feed, "entries_allowed", None)
    if fn is not None:
        ok, why = fn(now, [])
        if not ok:
            return False, why
    gw = getattr(getattr(feed, "primary", feed), "gw", None)
    if gw is None:
        return True, ""
    code = code.upper()
    local = now.astimezone(SYD)
    try:
        if gw.streaming(code):
            age = gw.data_age_s(code)
            limit = float(getattr(gw.s, "max_data_age_s", 90))
            # judged to 16:00: nothing trades in the closing auction's build-up
            if age is not None and age > limit and local.time() < time(16, 0):
                return False, f"stale: no bar from IBKR for {code} for {age:.0f}s"
            return True, ""
        at = gw.today_fetched_at(code, local.date())
    except Exception as e:  # noqa: BLE001 - an unreadable feed is not a fresh one
        return False, f"the feed could not be read for {code} ({type(e).__name__})"
    age = None if at is None else (local - at).total_seconds()
    if age is None or age > polled_fresh_s:
        return False, (
            f"stale: {code} is not streamed and its bars are "
            + ("not fetched yet" if age is None else f"{age:.0f}s old")
            + " - it is polled about every 6 minutes; try again after its next poll"
        )
    return True, ""


class Limits:
    def __init__(
        self, conf: dict, root: Path, pause_dir: Path | None, feed=None, live: bool = True
    ):
        self.conf = {**DEFAULTS, **conf}
        self.root = Path(root)
        self.pause_dir = pause_dir
        self.feed = feed
        self.live = live
        self.start_equity: float | None = None
        self.loss_hit = False

    def closing_cap(self, market, code: str) -> int | None:
        s = market.summaries.before(code, market.day, 14) if market.summaries else None
        if s is None or len(s) < 3 or "close_auction_volume" not in s:
            return None
        return int(float(self.conf["close_volume_cap"]) * float(s["close_auction_volume"].mean()))

    def entry_block(self, view, code: str, at: datetime) -> str:
        """Why no new exposure may be added now ('' = it may)."""
        k, why = killed(self.root)
        if k:
            return why
        b = view.b
        if self.start_equity:
            eq = b.acct.equity(view.m.last)
            floor = self.start_equity * (1 - float(self.conf["daily_loss_limit_pct"]) / 100)
            if eq <= floor:
                self.loss_hit = True
                return (
                    f"daily loss limit: equity {eq:,.0f} is down "
                    f"{(1 - eq / self.start_equity) * 100:.1f}% today (limit "
                    f"{self.conf['daily_loss_limit_pct']:g}%) - no new position until "
                    "tomorrow; exits are allowed"
                )
        if self.pause_dir is not None:
            from asxbot.arena.pause import paused

            p, why = paused(self.pause_dir, at)
            if p:
                return why
        if self.live and self.feed is not None:
            ok, why = feed_fresh(self.feed, code, at, float(self.conf["polled_fresh_s"]))
            if not ok:
                return why
        return ""

    def check(self, view, action: dict, at: datetime) -> tuple[dict | None, str]:
        """(the action to send - possibly with a smaller quantity - or None, and why)."""
        op = str(action.get("op", "place")).lower()
        if op == "cancel":
            return action, ""
        a = view.anon
        if op == "modify":
            o = view.b.acct.orders.get(str(action.get("id")))
            if o is None or action.get("qty") is None or not view.b.opens(o):
                return action, ""
            new_qty = a.shares_to_real(o.code, int(action["qty"]))
            if new_qty <= o.qty:
                return action, ""
            why = self.entry_block(view, o.code, at)
            return (None, f"refused by the limits: {why}") if why else (action, "")
        code = view._code(str(action.get("code", "")))
        if code is None:
            return action, ""  # the broker refuses an unknown code itself
        side = str(action.get("side", "buy")).lower()
        buy = side in ("buy", "cover")
        held = view.b.acct.positions[code].qty if code in view.b.acct.positions else 0
        qty = self._qty(view, code, action)
        if qty is None:
            return action, ""  # no quantity: the broker says so
        reducing = (buy and held < 0) or (not buy and held > 0)
        adds = qty - abs(held) if reducing else qty
        if adds <= 0:
            return action, ""
        why = self.entry_block(view, code, at)
        if why:
            if reducing:  # close what is held; refuse the flip
                return (
                    {**_with_qty(action, a.shares_to_shown(code, abs(held)))},
                    f"cut to a close of the position held: {why}",
                )
            return None, f"refused by the limits: {why}"
        cap = self.closing_cap(view.m, code)
        if cap is not None:
            same_dir = abs(held) if not reducing else 0
            working = sum(
                o.remaining for o in view.b.acct.working(code) if view.b.opens(o) and o.buy == buy
            )
            room = cap - same_dir - working
            if room <= 0:
                return None, (
                    f"refused by the closing-volume cap: {code} may hold at most "
                    f"{cap:,} shares ({self.conf['close_volume_cap']:.0%} of its usual "
                    f"closing auction); held {same_dir:,}, working {working:,}"
                )
            if adds > room:
                new = (abs(held) if reducing else 0) + room
                return (
                    _with_qty(action, a.shares_to_shown(code, new)),
                    f"cut from {qty:,} to {new:,} shares by the closing-volume cap "
                    f"({cap:,} = {self.conf['close_volume_cap']:.0%} of its usual closing "
                    "auction)",
                )
        return action, ""

    @staticmethod
    def _qty(view, code: str, action: dict) -> int | None:
        a = view.anon
        try:
            if action.get("qty") is not None:
                return a.shares_to_real(code, int(action["qty"]))
            if action.get("value_aud") is not None:
                ref = a.unprice(code, action.get("limit")) or view.m.last(code)
                return int(float(action["value_aud"]) // ref) if ref else None
        except (TypeError, ValueError):
            return None
        return None


def _with_qty(action: dict, qty: int) -> dict:
    out = {k: v for k, v in action.items() if k != "value_aud"}
    out["qty"] = int(qty)
    return out


__all__ = [
    "DEFAULTS",
    "Limits",
    "conf_of",
    "feed_fresh",
    "foreman_paused",
    "kill_path",
    "killed",
    "lift_kill",
    "set_kill",
    "usage_budget",
]
