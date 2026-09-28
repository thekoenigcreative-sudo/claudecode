"""The simulated team, set up to trade a LIVE paper book (28 Sep 2026, Rick: "i'll want to
actually test the agent team like the other trader on a live environment").

The live driver is src/asxbot/arena/team (the watcher runs it beside the frozen 10-day test).
This module is the simulator's side of it, so the team that trades live is the SAME TeamTrader
(team.py) - same prompts, same stages, same models - that was built and tested here:

  * `make_team` builds it from config.yaml `arena.team` (models, wake budget, cadence) with its
    own diary of lessons in the live book's folder (journal.Journal: lessons carry forward day
    to day, in date order, as in a simulated run);
  * `dump_day` / `restore_day` save and restore what it holds during a day (wakes used, the
    watchlist, the readers' notes, the specialists' last proposals), so a watcher restart
    mid-session resumes the same day rather than starting it again;
  * `day_entry` is the team's own after-close entry for a day (the decision-maker's text, its
    lessons and the researcher's), for the evening report and the arena's per-book files.

Nothing here reaches a broker or IBKR (tests/test_tsim.py holds tsim to that).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from asxbot.lab.tsim.journal import Journal
from asxbot.lab.tsim.team import OPUS, SONNET, SONNET_LOW, TeamTrader

_DAY_FIELDS = ("wakes", "watchlist", "market_note", "read", "notes", "day_log", "last_props")


def _pair(conf: dict, key: str, default: tuple[str, str]) -> tuple[str, str]:
    v = (conf.get("models") or {}).get(key) or {}
    return str(v.get("model") or default[0]).split("/")[-1], str(v.get("effort") or default[1])


def make_team(cfg, root: Path, *, ask, conf: dict | None = None,
              register_ideas: bool = True) -> TeamTrader:  # fmt: skip
    """The team, as the simulator runs it, with the live book's settings."""
    conf = dict(conf or {})
    root = Path(root)
    return TeamTrader(
        journal=Journal(root), cfg=cfg, ask=ask, log_dir=root / "calls",
        max_wakes_per_day=int(conf.get("max_wakes_per_day", 18)),
        scan_every_min=int(conf.get("scan_every_min", 10)),
        max_readers=int(conf.get("max_readers", 12)),
        name="team",
        decider=_pair(conf, "decider", OPUS), risk=_pair(conf, "risk", OPUS),
        staff=_pair(conf, "staff", SONNET), reader=_pair(conf, "reader", SONNET_LOW),
        register_ideas=register_ideas,
        specialists_every_min=conf.get("specialists_every_min", 20),
    )  # fmt: skip


def models(team: TeamTrader) -> dict:
    return {k: f"{m} {e}" for k, (m, e) in team.models.items()}


def dump_day(team: TeamTrader) -> dict:
    d = {k: getattr(team, k) for k in _DAY_FIELDS}
    for k in ("scanned_at", "props_at"):
        t = getattr(team, k)
        d[k] = t.isoformat() if t is not None else None
    d["props_for"] = list(team.props_for)
    return d


def restore_day(team: TeamTrader, d: dict | None) -> None:
    team._reset_day()
    if not d:
        return
    for k in _DAY_FIELDS:
        if k in d:
            setattr(team, k, d[k])
    for k in ("scanned_at", "props_at"):
        setattr(team, k, datetime.fromisoformat(d[k]) if d.get(k) else None)
    team.props_for = tuple(d.get("props_for") or ())


def day_entry(root: Path, day: str) -> dict | None:
    """The team's own after-close entry for `day` ({"journal", "lessons", "ideas", ...})."""
    for e in Journal(Path(root)).entries():
        if e.get("day") == day:
            return e
    return None


def lessons_before(root: Path, day: str) -> list[str]:
    return Journal(Path(root)).lessons(day)


def copy_entries(src: Path, dst: Path, before: str) -> int:
    """Carry the live book's entries before `before` into a replay's folder, so a replay of a
    live day starts with the same lessons the live team had that morning."""
    n = 0
    for e in Journal(Path(src)).entries(before):
        Journal(Path(dst)).write(str(e.get("day")), {k: v for k, v in e.items() if k != "day"})
        n += 1
    return n


__all__ = ["copy_entries", "day_entry", "dump_day", "lessons_before", "make_team", "models",
           "restore_day"]  # fmt: skip
