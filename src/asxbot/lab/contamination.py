"""Does the model know the past? Measured, not assumed (PRACTICE_LAB.md 2).

The frozen agent answers the same setups raw and anonymised on a sample of VALIDATE days (after
the cutoff: the difference A is the anonymisation's own effect) and of TUNE days (before: the
difference R is knowledge plus that effect). C = R - A estimates what the model knows, with a
bootstrap interval over days; the share of setups where the raw and anonymised decisions agree
is reported too. Raw pre-cutoff answers are made only here and never score a variant.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from asxbot.lab import runner, splits, store

SAMPLE_DAYS = 6
EVERY_DAYS = 30
VARIANT = {
    "id": "contamination",
    "playbook": "asx_daytrader",
    "overrides": {},
    "agent_prompt": "",
    "books": ["bot", "agent"],
    "mode": "contamination",
}


def sample(window: str, n: int = SAMPLE_DAYS, seed: int = 26) -> list:
    days = splits.sessions_in(window)
    rnd = random.Random(seed)
    return sorted(rnd.sample(days, min(n, len(days))))


def daily(window: str, anon: bool, days) -> dict:
    return runner.cards(VARIANT["id"], window, "asx_daytrader", ["agent"], anon=anon, days=days)[
        "agent"
    ]["daily"]


def decisions(window: str, anon: bool, days) -> dict:
    """(day, setup key, trigger bar) -> the agent's action, from the day records."""
    want = {d.isoformat() for d in days}
    out = {}
    for r in store.load_days(VARIANT["id"], window, anon):
        if r["day"] not in want:
            continue
        for d in r.get("agent_decisions") or []:
            out[(r["day"], d["key"], d["bar"][-5:])] = d["action"]
    return out


def agreement(window: str, days) -> float | None:
    raw, anon = decisions(window, False, days), decisions(window, True, days)
    common = set(raw) & set(anon)
    return round(sum(raw[k] == anon[k] for k in common) / len(common) * 100, 1) if common else None


def bootstrap(diffs: list[float], n: int = 2000, seed: int = 7) -> tuple[float, float]:
    if not diffs:
        return (0.0, 0.0)
    rnd = random.Random(seed)
    means = sorted(sum(rnd.choice(diffs) for _ in diffs) / len(diffs) for _ in range(n))
    return (round(means[int(0.05 * n)], 2), round(means[int(0.95 * n)], 2))


def maybe_run(cfg, deadline) -> str:
    path = store.lab_data(cfg) / "contamination.json"
    last = store.read_json(path, {}) or {}
    if last.get("at") and datetime.fromisoformat(last["at"]) > datetime.now() - timedelta(
        days=EVERY_DAYS
    ):
        return ""
    plan = {"validate": sample("validate"), "tune": sample("tune")}
    for window, days in plan.items():
        for anon in (False, True):
            r = runner.run_days(
                cfg, VARIANT, window, days, ["bot", "agent"], anon=anon, deadline=deadline
            )
            if r == "usage":
                return "contamination: held at the weekly usage stop"
            if r != "done":
                return f"contamination: {window} {'anon' if anon else 'raw'} {r}"
    res = {}
    for window, days in plan.items():
        raw, anon = daily(window, False, days), daily(window, True, days)
        common = sorted(set(raw) & set(anon))
        diffs = [raw[d] - anon[d] for d in common]
        res[window] = {
            "days": common,
            "raw_pnl": round(sum(raw[d] for d in common), 2),
            "anon_pnl": round(sum(anon[d] for d in common), 2),
            "mean_daily_diff": round(sum(diffs) / len(diffs), 2) if diffs else 0.0,
            "interval_90": bootstrap(diffs),
            "decisions_agree_pct": agreement(window, days),
        }
    a = res["validate"]["mean_daily_diff"]
    r_ = res["tune"]["mean_daily_diff"]
    out = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "windows": res,
        "anonymisation_effect_A": a,
        "raw_advantage_before_cutoff_R": r_,
        "contamination_estimate_C": round(r_ - a, 2),
        "plain": _plain(a, r_, res),
    }
    store.write_json(path, out)
    return "contamination measured: " + out["plain"]


def _plain(a: float, r: float, res: dict) -> str:
    c = r - a
    lo, hi = res["tune"]["interval_90"]
    if lo > 0 and c > 0:
        return (
            f"the agent does better on real names before its cutoff by about ${c:,.0f} a day "
            "beyond what "
            "anonymising changes on its own - it likely remembers some of the past; only "
            "post-cutoff results count"
        )
    return (
        f"no sign the agent remembers these days: raw vs anonymised differs by ${r:,.0f}/day "
        "before the cutoff "
        f"and ${a:,.0f}/day after it (90% interval before: {lo:+,.0f} to {hi:+,.0f})"
    )
