"""The lab's data windows (WINNER.md, fixed 26 Sep 2026 before the search) and the seal on the
locked test: nothing runs on it unless the variant is a final candidate, and every run is
recorded - so the number of looks at it is known and charged (WINNER.md criterion 9)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

CUTOFF = date(2026, 7, 1)  # the models' knowledge cutoff (Opus 5.5: June 2026)


@dataclass(frozen=True)
class Window:
    name: str
    first: date
    last: date | None  # None: open-ended (shadow)
    post_cutoff: bool
    sealed: bool = False


WINDOWS = {
    "tune": Window("tune", date(2026, 3, 26), date(2026, 6, 30), post_cutoff=False),
    "validate": Window("validate", date(2026, 7, 1), date(2026, 8, 14), post_cutoff=True),
    "locked": Window("locked", date(2026, 8, 17), date(2026, 9, 25), post_cutoff=True, sealed=True),
    "shadow": Window("shadow", date(2026, 9, 28), None, post_cutoff=True),
}


class LockedTestSealed(RuntimeError):
    """A run on the locked test that is not a final candidate's one run."""


def window_of(day: date) -> str | None:
    for w in WINDOWS.values():
        if day >= w.first and (w.last is None or day <= w.last):
            return w.name
    return None


def sessions_in(name: str, until: date | None = None) -> list[date]:
    """The ASX trading sessions of a window (the shadow window up to `until`)."""
    from asxbot.arena.replay_ibkr import sessions

    w = WINDOWS[name]
    last = w.last or until
    if last is None:
        raise ValueError("the shadow window needs `until`")
    return sessions(w.first, min(last, until) if until else last)


def is_post_cutoff(day: date) -> bool:
    return day >= CUTOFF


def locked_log(lab_data: Path) -> Path:
    return lab_data / "locked_runs.jsonl"


def locked_runs(lab_data: Path) -> list[dict]:
    p = locked_log(lab_data)
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def unseal(lab_data: Path, variant: dict, why: str) -> int:
    """Allow ONE locked-test run for a final candidate; returns F, the number of final
    candidates ever run on the locked test including this one. Refuses a second run of the
    same variant and any variant that is not marked final."""
    if variant.get("stage") != "final":
        raise LockedTestSealed(
            f"variant {variant.get('id')} is not a final candidate (stage "
            f"{variant.get('stage')!r}); the locked test is sealed"
        )
    runs = locked_runs(lab_data)
    if any(r["variant"] == variant["id"] for r in runs):
        raise LockedTestSealed(f"variant {variant['id']} already had its one locked-test run")
    lab_data.mkdir(parents=True, exist_ok=True)
    rec = {"variant": variant["id"], "at": datetime.now().isoformat(timespec="seconds"), "why": why}
    with locked_log(lab_data).open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return len(runs) + 1


def check_days(days: list[date], allow_locked: bool = False) -> None:
    """Refuse any locked-test day unless unsealed for this run."""
    if allow_locked:
        return
    w = WINDOWS["locked"]
    bad = [d for d in days if w.first <= d <= w.last]
    if bad:
        raise LockedTestSealed(f"{len(bad)} locked-test day(s) requested ({bad[0]}...); sealed")
