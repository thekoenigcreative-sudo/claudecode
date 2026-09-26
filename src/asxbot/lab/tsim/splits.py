"""The search's data splits (CLOUD_BRIEF "HONEST TESTING"): practice, check and the sealed block.

Starting point (the lab's existing windows, WINNER.md): practice = TUNE (26 Mar - 30 Jun, before
the models' knowledge cutoff: the AI trader sees it disguised), check = VALIDATE (1 Jul - 14 Aug),
sealed = the LOCKED TEST (17 Aug - 25 Sep).

The sealed block wears out with use: every finalist scored on it is recorded; once `seal_uses`
(3) have been, the newest `seal_days` (30) trading days in the history that come after it are
sealed instead and the old block is retired into the check set. Days are never moved back into
practice, and a sealed day is never used for tuning or screening (`days` refuses it).
State: data/lab/tsim/seal.json.
"""

from __future__ import annotations

from datetime import date, datetime

from asxbot.lab import store

SEAL_USES = 3
SEAL_DAYS = 30
START = {
    "practice": ["2026-03-26", "2026-06-30"],
    "check": [["2026-07-01", "2026-08-14"]],
    "sealed": ["2026-08-17", "2026-09-25"],
}


class Sealed(RuntimeError):
    pass


def _path(cfg):
    return store.lab_data(cfg) / "tsim" / "seal.json"


def current(cfg) -> dict:
    d = store.read_json(_path(cfg), None)
    if d is None:
        d = {"practice": START["practice"], "check": START["check"], "sealed": START["sealed"],
             "uses": [], "retired": [], "seal_uses": SEAL_USES, "history": []}  # fmt: skip
        store.write_json(_path(cfg), d)
    return d


def _sessions(a: str, b: str) -> list[date]:
    from asxbot.arena.replay_ibkr import sessions

    return sessions(date.fromisoformat(a), date.fromisoformat(b))


def days(cfg, name: str, allow_sealed: bool = False) -> list[date]:
    d = current(cfg)
    if name == "sealed":
        if not allow_sealed:
            raise Sealed("the sealed block is run once per finalist only (search.sealed_run)")
        return _sessions(*d["sealed"])
    if name == "practice":
        return _sessions(*d["practice"])
    if name == "check":
        out = []
        for a, b in d["check"]:
            out += _sessions(a, b)
        return sorted(set(out))
    raise ValueError(name)


def use_seal(cfg, idea_id: str) -> int:
    """Record a finalist's one look at the sealed block; returns F (looks at this block)."""
    d = current(cfg)
    if idea_id in d["uses"]:
        raise Sealed(f"{idea_id} already had its sealed run")
    d["uses"].append(idea_id)
    store.write_json(_path(cfg), d)
    return len(d["uses"])


def rotate_if_worn(cfg, newest: date | None = None) -> bool:
    """Seal a fresh block once the current one has served `seal_uses` finalists and enough new
    trading days exist after it."""
    d = current(cfg)
    if len(d["uses"]) < d.get("seal_uses", SEAL_USES):
        return False
    from asxbot.lab.tsim.market import History
    from asxbot.lab.tsim.run import history_root

    if newest is None:
        idx = History(history_root()).days("^AXJO")
        newest = idx[-1] if idx else None
    if newest is None:
        return False
    after = _sessions((date.fromisoformat(d["sealed"][1])).isoformat(), newest.isoformat())[1:]
    if len(after) < SEAL_DAYS:
        return False
    block = after[-SEAL_DAYS:]
    d["check"].append(d["sealed"])
    d["retired"].append({"block": d["sealed"], "uses": d["uses"]})
    d["history"].append({"at": datetime.now().isoformat(timespec="seconds"),
                         "sealed": [block[0].isoformat(), block[-1].isoformat()]})  # fmt: skip
    d["sealed"] = [block[0].isoformat(), block[-1].isoformat()]
    d["uses"] = []
    store.write_json(_path(cfg), d)
    return True
