"""Every variant the lab tries: registered before it runs, its definition never edited, counted.

A variant is a frozen playbook plus changes: dotted config paths with new values (only the
whitelist below, within bounds) and, for the day trader's agent, text added to its
instructions. Its definition hash is stored; `load` refuses a file whose definition changed
(WINNER.md: nothing is tuned after its results are seen - a change is a NEW variant).
Stages: registered -> screened | screen_failed -> validated | validate_failed -> shadow ->
final -> winner | retired.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

PLAYBOOKS = ("asx_daytrader", "asx_announcements_v2")
BASELINE = "baseline"
MAX_PARAMS = 6
MAX_PROMPT_CHARS = 600

# What a variant may change, per playbook: a regex over the dotted path -> (kind, lo, hi).
# kind: num (a number within [lo, hi]), time ("HH:MM"), window (["HH:MM","HH:MM"]), bool,
# stricter (a number no higher than the frozen value - risk and size may only go down).
_SETUP_NUM = (
    r"setups\.(gap_and_go|opening_range_breakout|vwap_reclaim|halt_resumption)\."
    r"(gap_pct_vs_index|range_minutes|min_rvol|bar_volume_multiple|bar_volume_lookback|"
    r"min_move_vs_index_pct|vwap_slope_bars|pullback_lookback|min_bars_below|stop_lookback|"
    r"min_gap_minutes|prior_activity_share|resumption_range_minutes|within_minutes)"
)
ALLOWED = {
    "asx_daytrader": [
        (_SETUP_NUM, ("num", 0.05, 120)),
        (
            r"setups\.(gap_and_go|opening_range_breakout|vwap_reclaim|halt_resumption)\.window",
            ("window",),
        ),
        (
            r"setups\.(gap_and_go|opening_range_breakout|vwap_reclaim|halt_resumption)\.enabled",
            ("bool",),
        ),
        (r"setups\.halt_resumption\.late_first_trade", ("time",)),
        (r"entry\.limit_slack_pct", ("num", 0.1, 2.0)),
        (r"entry\.good_for_minutes", ("num", 1, 30)),
        (r"entry\.min_r_over_costs", ("num", 1.0, 6.0)),
        (r"manage\.(breakeven_at_r|half_at_r|trail_at_r|trail_distance_r)", ("num", 0.25, 6.0)),
        (r"max_entries_per_stock_per_day", ("num", 1, 3)),
        (r"max_signal_age_bars", ("num", 1, 10)),
        (r"last_entry_time", ("time",)),
        (r"scan\.(start|end)", ("time",)),
        (r"scan\.top_n", ("num", 3, 40)),
        (r"risk_per_trade_pct", ("stricter",)),
        (r"universe\.max_order_share_of_turnover", ("stricter",)),
        (r"universe\.max_tick_pct", ("num", 0.1, 1.0)),
    ],
    "asx_announcements_v2": [
        (r"yardstick\.(move_vs_index_pct|volume_multiple)", ("num", 0.5, 10.0)),
        (r"yardstick\.(measure_at|exit_at|latest_decision_time)", ("time",)),
        (r"yardstick\.entry_limit_slack_pct", ("num", 0.1, 3.0)),
        (r"yardstick\.size_aud", ("stricter",)),
        (r"screen\.max_tick_pct", ("num", 0.2, 3.0)),
        (r"screen\.max_order_share_of_turnover", ("stricter",)),
        (r"reaction\.(after_minutes|before_minutes)", ("num", 1, 120)),
        (r"reaction\.(wake_move_vs_index_pct|wake_volume_multiple)", ("num", 0.2, 10.0)),
        (r"last_entry_time", ("time",)),
    ],
}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MARKET = ("10:00", "16:00")


class BadVariant(ValueError):
    pass


def _get(d: dict, path: str):
    cur = d
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _set(d: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = d
    for k in parts[:-1]:
        cur = cur.setdefault(k, {})
    cur[parts[-1]] = value


def check(playbook: str, overrides: dict, agent_prompt: str, base_raw: dict) -> list[str]:
    """Why this variant is not allowed ([] = allowed)."""
    why = []
    if playbook not in PLAYBOOKS:
        return [f"unknown playbook {playbook!r}"]
    if len(overrides) > MAX_PARAMS:
        why.append(f"{len(overrides)} changed parameters (at most {MAX_PARAMS})")
    if len(agent_prompt or "") > MAX_PROMPT_CHARS:
        why.append(
            f"{len(agent_prompt)} characters of agent instructions (at most {MAX_PROMPT_CHARS})"
        )
    if agent_prompt and playbook != "asx_daytrader":
        why.append("agent instructions are only for the day trader (v2's agent is phase 2)")
    for path, value in overrides.items():
        rule = next((r for pat, r in ALLOWED[playbook] if re.fullmatch(pat, path)), None)
        if rule is None:
            why.append(f"{path} may not be changed by a variant")
            continue
        kind = rule[0]
        base = _get(base_raw, path)
        if kind == "num":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                why.append(f"{path} must be a number")
            elif not (rule[1] <= value <= rule[2]):
                why.append(f"{path}={value} is outside {rule[1]}..{rule[2]}")
        elif kind == "stricter":
            if (
                not isinstance(value, (int, float))
                or base is None
                or not (0 < value <= float(base))
            ):
                why.append(f"{path}={value} must be above 0 and at most the frozen {base}")
        elif kind == "time":
            if not (
                isinstance(value, str) and TIME_RE.match(value) and MARKET[0] <= value <= MARKET[1]
            ):
                why.append(f"{path}={value!r} must be a market time HH:MM")
        elif kind == "window":
            ok = (
                isinstance(value, list)
                and len(value) == 2
                and all(isinstance(x, str) and TIME_RE.match(x) for x in value)
                and MARKET[0] <= value[0] < value[1] <= MARKET[1]
            )
            if not ok:
                why.append(f'{path}={value!r} must be ["HH:MM", "HH:MM"] inside market hours')
        elif kind == "bool" and not isinstance(value, bool):
            why.append(f"{path} must be true or false")
    return why


def apply(pb, overrides: dict):
    """The playbook with the variant's changes (a copy; the frozen block is never touched)."""
    raw = copy.deepcopy(pb.raw)
    for path, value in overrides.items():
        _set(raw, path, value)
    return dataclasses.replace(pb, raw=raw)


def complexity(v: dict) -> float:
    return len(v.get("overrides") or {}) + len(v.get("agent_prompt") or "") / 100.0


def definition(v: dict) -> dict:
    return {k: v.get(k) for k in ("playbook", "overrides", "agent_prompt")}


def def_hash(v: dict) -> str:
    return hashlib.sha256(json.dumps(definition(v), sort_keys=True).encode()).hexdigest()[:16]


class Registry:
    """data/lab/variants/<id>.json, one per variant, plus runs.jsonl (every run)."""

    def __init__(self, lab_data: Path):
        self.dir = Path(lab_data) / "variants"
        self.lab_data = Path(lab_data)

    def all(self) -> list[dict]:
        if not self.dir.exists():
            return []
        out = [self.load(p.stem) for p in sorted(self.dir.glob("*.json"))]
        return [v for v in out if v]

    def load(self, vid: str) -> dict | None:
        p = self.dir / f"{vid}.json"
        if not p.exists():
            return None
        v = json.loads(p.read_text(encoding="utf-8"))
        if v.get("hash") != def_hash(v):
            raise BadVariant(f"variant {vid}'s definition was edited after it was registered")
        return v

    def save(self, v: dict) -> None:
        from asxbot.io import write_text_atomic

        self.dir.mkdir(parents=True, exist_ok=True)
        write_text_atomic(json.dumps(v, indent=1, default=str), self.dir / f"{v['id']}.json")

    def next_id(self, playbook: str) -> str:
        tag = "dt" if playbook == "asx_daytrader" else "v2"
        n = sum(1 for v in self.all() if v["playbook"] == playbook)
        return f"{tag}-{n + 1:04d}"

    def register(
        self,
        playbook: str,
        overrides: dict,
        *,
        agent_prompt: str = "",
        why: str = "",
        proposed_by: str = "",
        parent: str = BASELINE,
        books: list[str] | None = None,
        base_raw: dict | None = None,
    ) -> dict:
        problems = check(playbook, overrides, agent_prompt, base_raw or {})
        if problems:
            raise BadVariant("; ".join(problems))
        for v in self.all():
            if (
                v["playbook"] == playbook
                and v["overrides"] == overrides
                and (v.get("agent_prompt") or "") == (agent_prompt or "")
            ):
                raise BadVariant(f"the same as {v['id']}")
        if books is None:
            books = ["bot", "agent"] if (agent_prompt and playbook == "asx_daytrader") else ["bot"]
        v = {
            "id": self.next_id(playbook),
            "playbook": playbook,
            "parent": parent,
            "overrides": overrides,
            "agent_prompt": agent_prompt or "",
            "books": books,
            "why": why,
            "proposed_by": proposed_by,
            "created": datetime.now().isoformat(timespec="seconds"),
            "stage": "registered",
            "results": {},
            "history": [],
        }
        v["hash"] = def_hash(v)
        v["complexity"] = complexity(v)
        self.save(v)
        self.log_run({"event": "registered", "variant": v["id"], "why": why, "by": proposed_by})
        return v

    def set_stage(self, v: dict, stage: str, note: str = "") -> dict:
        v["history"].append(
            {
                "at": datetime.now().isoformat(timespec="seconds"),
                "from": v["stage"],
                "to": stage,
                "note": note,
            }
        )
        v["stage"] = stage
        self.save(v)
        return v

    def log_run(self, rec: dict) -> None:
        self.lab_data.mkdir(parents=True, exist_ok=True)
        rec = {"at": datetime.now().isoformat(timespec="seconds"), **rec}
        with (self.lab_data / "runs.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def count_validated(self) -> int:
        """N for the validation bar: every variant ever run on VALIDATE (passed or not)."""
        return sum(1 for v in self.all() if "validate" in (v.get("results") or {}))
