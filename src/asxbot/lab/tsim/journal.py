"""The simulated trader's own journal: one entry per simulated day, kept inside its run.

Lessons only come from days already simulated in the same run, in date order (CLOUD_BRIEF
"HONEST TESTING"): a run starts with an empty journal unless it is explicitly seeded with
lessons from PRACTICE runs, and a check or sealed run is never seeded from anything but
practice. `Journal.seed` records where any seed came from.
"""

from __future__ import annotations

import json
from pathlib import Path


class Journal:
    def __init__(self, run_dir: Path):
        self.dir = Path(run_dir) / "journal"

    def write(self, day: str, entry: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{day}.json").write_text(json.dumps({"day": day, **entry}, indent=1),
                                               encoding="utf-8")  # fmt: skip

    def entries(self, before: str | None = None) -> list[dict]:
        if not self.dir.exists():
            return []
        out = []
        for p in sorted(self.dir.glob("*.json")):
            if before is not None and p.stem >= before:
                continue
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return out

    def lessons(self, before: str, n: int = 20) -> list[str]:
        seen, out = set(), []
        for e in reversed(self.entries(before)):
            for les in e.get("lessons") or []:
                k = str(les).strip().lower()
                if k and k not in seen:
                    seen.add(k)
                    out.append(str(les).strip())
        return list(reversed(out[:n]))

    def seed(self, lessons: list[str], source: str) -> None:
        self.write("0000-00-00", {"journal": f"seeded from {source}", "lessons": lessons,
                                  "seed_source": source})  # fmt: skip
