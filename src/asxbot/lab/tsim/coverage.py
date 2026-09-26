"""Which simulated days have the full announcement feed (CLOUD_BRIEF "FILL THE NEWS GAP").

The archive (data/announcements/history, one request per code and year) was built for the ASX
300; small caps' announcements exist only from the live collector's first day (22 Sep 2026).
A day without small caps' news would let a trader trade stocks in play without the news that
put them in play, so every day is marked:
  full     every small cap's archive page for that year was fetched on or after the day, or
           the live collector ran that day;
  partial  the ASX 300's news only (catalyst strategies are not judged on these days);
  unknown  no archive or no universe list to tell.
The backfill is the existing collector, plain code, paced and cached:
    asxbot announcements history --universe small
(it resumes where it stopped; CLAUDE.md: if access is refused it stops).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

FULL_SHARE = 0.95  # of small caps covered for a day to count as full


def news_coverage(cfg, days: list[date]) -> dict[date, str]:
    data = Path(cfg.data_dir) / "announcements"
    live_days = set()
    live = data / "live"
    for p in live.glob("*.parquet") if live.exists() else []:
        try:
            live_days.add(date.fromisoformat(p.stem))
        except ValueError:
            continue
    progress = {}
    pp = data / "history" / "_progress.json"
    if pp.exists():
        try:
            progress = json.loads(pp.read_text(encoding="utf-8"))
        except ValueError:
            progress = {}
    small = _small_codes(cfg)
    out = {}
    for d in days:
        if d in live_days:
            out[d] = "full"
            continue
        if not small or not progress:
            out[d] = "unknown" if not progress else "partial"
            continue
        ok = 0
        for c in small:
            s = progress.get(f"{c}:{d.year}")
            if s and (d.year < date.fromisoformat(s[:10]).year or s[:10] >= d.isoformat()):
                ok += 1
        out[d] = "full" if ok / len(small) >= FULL_SHARE else "partial"
    return out


def _small_codes(cfg) -> list[str]:
    p = Path(cfg.data_dir) / "universe"
    for name in ("small_members.csv", "small.csv"):
        f = p / name
        if f.exists():
            import pandas as pd

            return pd.read_csv(f)["code"].astype(str).str.upper().tolist()
    try:
        from asxbot.data.universe import build_universes

        _, b = build_universes(cfg.data_dir, cfg.get("collector.user_agent"))
        return [c.upper() for c in b.codes]
    except Exception:  # noqa: BLE001 - no directory cached and no network: unknown
        return []
