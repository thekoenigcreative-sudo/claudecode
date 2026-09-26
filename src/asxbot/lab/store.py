"""Where the lab keeps things.

- lab_local(): %LOCALAPPDATA%\\asx-bot\\lab (outside Google Drive): per-day results, the agent
  answer cache, worker logs, research packets. Bulky, rebuildable.
- lab_data(cfg): <ASXBOT_HOME>\\data\\lab: the registry of variants, runs.jsonl, the locked-test
  log, shadow ledgers, the scoreboard, status.json (read by the Foreman). Small, kept.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def lab_local() -> Path:
    if os.environ.get("ASXBOT_LAB_LOCAL"):
        return Path(os.environ["ASXBOT_LAB_LOCAL"])
    from asxbot.localdir import asx_local

    return asx_local() / "lab"


def lab_data(cfg) -> Path:
    return Path(cfg.data_dir) / "lab"


def results_dir(variant_id: str, window: str, anon: bool = False) -> Path:
    return lab_local() / "results" / variant_id / (window + ("-anon" if anon else ""))


def day_files(variant_id: str, window: str, anon: bool = False) -> dict[str, Path]:
    d = results_dir(variant_id, window, anon)
    return {p.stem: p for p in d.glob("*.json")} if d.exists() else {}


def load_days(variant_id: str, window: str, anon: bool = False) -> list[dict]:
    out = []
    for _, p in sorted(day_files(variant_id, window, anon).items()):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def read_json(p: Path, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(p: Path, data) -> None:
    from asxbot.io import write_text_atomic

    Path(p).parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(json.dumps(data, indent=1, default=str), Path(p))
