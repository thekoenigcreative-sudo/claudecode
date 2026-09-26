"""The research loop's hand-off (PRACTICE_LAB.md 4).

`build_packet` writes a folder the research session works in (the Foreman queues ONE Claude
Code session there, in Rick's build queue): BRIEF.md (the job), packet.md (the evidence) and
rules.json (what a variant may change, within which bounds). The session writes
proposals.json; `ingest` checks every proposal against the whitelist and registers the good
ones - the tick then screens, validates and promotes them.

What the session may see: everything about the TUNE window (the frozen bots' trades, losers,
setups), the live arena's own days, and every earlier variant's stage - but only pass/fail
for VALIDATE (numbers from it would let proposals fit the validation days) and nothing at all
from the LOCKED TEST.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from asxbot.lab import splits, store, variants
from asxbot.lab.variants import Registry

MAX_PROPOSALS = 5
SETUP_RE = re.compile(r"^\[(\w+)\]")

BRIEF = """You are the Practice Lab's research analyst for Rick's ASX day trader (fake money).
Work only in this folder: {folder}
Do NOT edit any repository, config.yaml, the live bots, or anything outside this folder. Never
touch Jarvis/OpenClaw, never place orders, never read secrets. Headless: nobody can answer.

1. Read packet.md (the evidence: the frozen rule bots on the TUNE window {tune}, their losing
   trades, results by setup, the live arena's days, every variant tried so far) and rules.json
   (exactly what a variant may change, and the bounds).
2. Diagnose WHY the losing trades lost (entry timing, stop placement, setup quality, costs,
   market direction, liquidity) using the numbers given. Say what you can't tell from them.
3. Propose at most {n} NEW variants that you expect to improve profit AFTER COSTS on unseen
   days - not to fit these days. Prefer ONE or two changes each (simpler variants rank first and
   more than {maxp} changes are refused), each with a mechanism you can state. Don't repeat a
   variant already tried (packet.md lists them). Agent instructions ("agent_prompt", at most
   {maxc} characters) change what the AI day trader is told; only use them for a judgement the
   rules can't express.
4. Write proposals.json in this folder, a JSON list, nothing else in it:
   [{{"playbook": "asx_daytrader" | "asx_announcements_v2",
      "overrides": {{"<dotted.path>": <value>, ...}},
      "agent_prompt": "",
      "why": "<the diagnosis and the expected effect, 1-3 sentences>"}}]
5. Write findings.md: your diagnosis in plain English (under 25 lines), which Rick may read.
End with a two-line summary.
"""


def _setup_of(trade: dict) -> str:
    m = SETUP_RE.match(trade.get("reason") or "")
    return m.group(1) if m else "v2" if not trade.get("reason") else "other"


def tune_evidence(playbook_key: str = "daytrader") -> dict:
    recs = store.load_days("baseline", "tune")
    by_setup = defaultdict(lambda: {"trades": 0, "wins": 0, "net": 0.0, "r": []})
    losers = []
    days = []
    for r in recs:
        if r.get("gaps"):
            continue
        for key in ("daytrader", "v2"):
            b = ((r.get(key) or {}).get("books") or {}).get("bot")
            if not b:
                continue
            if key == "daytrader":
                days.append((r["day"], b["pnl"], r.get("market_move_pct")))
            for t in b["trades"]:
                s = by_setup[f"{key}:{_setup_of(t)}"]
                s["trades"] += 1
                s["wins"] += int(t["net"] > 0)
                s["net"] += t["net"]
                if t.get("r") is not None:
                    s["r"].append(t["r"])
                if t["net"] < 0:
                    losers.append(
                        {
                            "day": r["day"],
                            "book": key,
                            **t,
                            "market_move_pct": r.get("market_move_pct"),
                        }
                    )
    table = {
        k: {
            "trades": v["trades"],
            "win_rate_pct": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else None,
            "net": round(v["net"], 2),
            "avg_r": round(sum(v["r"]) / len(v["r"]), 3) if v["r"] else None,
        }
        for k, v in by_setup.items()
    }
    losers.sort(key=lambda t: t["net"])
    return {"by_setup": table, "worst_losers": losers[:25], "days": days, "n_days": len(recs)}


def live_days(cfg, n: int = 10) -> list[str]:
    """The live arena's recent day-trader trades (post-cutoff, real decisions), from its events."""
    p = Path(cfg.data_dir) / "events" / "arena_fills.jsonl"
    if not p.exists():
        return []
    rows = p.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
    out = []
    for line in rows:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if "daytrader" in json.dumps(e):
            out.append(json.dumps(e)[:300])
    return out[-n * 5 :]


def rules(cfg) -> dict:
    from asxbot.arena.levels import load_playbook

    out = {}
    for key in variants.PLAYBOOKS:
        pb = load_playbook(cfg, key)
        allowed = []
        for pat, rule in variants.ALLOWED[key]:
            allowed.append({"path_regex": pat, "kind": rule[0], "bounds": list(rule[1:])})
        frozen = {
            k: pb.raw.get(k)
            for k in (
                "setups",
                "entry",
                "manage",
                "scan",
                "max_signal_age_bars",
                "max_entries_per_stock_per_day",
                "last_entry_time",
                "risk_per_trade_pct",
                "universe",
                "yardstick",
                "screen",
                "reaction",
            )
            if k in pb.raw
        }
        out[key] = {"allowed": allowed, "frozen_values": frozen}
    out["limits"] = {
        "max_changed_parameters": variants.MAX_PARAMS,
        "max_prompt_chars": variants.MAX_PROMPT_CHARS,
    }
    return out


def build_packet(cfg, reg: Registry | None = None) -> Path:
    reg = reg or Registry(store.lab_data(cfg))
    folder = store.lab_local() / "research" / datetime.now().strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    ev = tune_evidence()
    tried = []
    for v in reg.all():
        res = v.get("results") or {}
        tune = {
            b: r["card"]["pnl_after_fees"]
            for b, r in (res.get("tune") or {}).get("books", {}).items()
        }
        val = {
            b: ("passed" if r["passed"] else "failed")
            for b, r in (res.get("validate") or {}).get("books", {}).items()
        }
        tried.append(
            {
                "id": v["id"],
                "playbook": v["playbook"],
                "overrides": v["overrides"],
                "agent_prompt": v.get("agent_prompt", ""),
                "stage": v["stage"],
                "tune_pnl": tune,
                "validate": val,
                "why": v.get("why", ""),
            }
        )
    w = splits.WINDOWS
    lines = [
        f"# Practice Lab research packet, {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"TUNE window {w['tune'].first} to {w['tune'].last}: {ev['n_days']} days simulated for "
        "the frozen rule bots "
        "(IBKR 1-minute bars, live costs and fills, $20,000 books).",
        "",
        "## Results by setup (frozen rule bots, TUNE)",
        "",
        "| Book:setup | Trades | Win rate | Net after costs | Avg R |",
        "|---|---:|---:|---:|---:|",
    ]
    for k, s in sorted(ev["by_setup"].items()):
        lines.append(
            f"| {k} | {s['trades']} | {s['win_rate_pct']}% | {s['net']:+,.2f} | {s['avg_r']} |"
        )
    lines += ["", "## The 25 worst losing trades (TUNE)", ""]
    for t in ev["worst_losers"]:
        lines.append(
            f"- {t['day']} {t['book']} {t['side']} {t['ticker']} qty {t['qty']} entry "
            f"{t['entry']} stop {t['stop']} "
            f"filled {t['first_fill']}: net {t['net']:+,.2f} ({t.get('r')}R), exits "
            f"{t['exits']}, market "
            f"{t.get('market_move_pct')}% - {t['reason'][:100]}"
        )
    lines += ["", "## Day by day (day trader rule bot, TUNE): day, P&L, index move %", ""]
    lines += [f"- {d} {p:+,.2f} {m}" for d, p, m in ev["days"]]
    lines += ["", "## Variants tried so far (VALIDATE shown only as passed/failed)", ""]
    lines += [f"- {json.dumps(t)}" for t in tried] or ["- none yet"]
    live = live_days(cfg)
    if live:
        lines += ["", "## Live arena day-trader fills (most recent)", ""] + [f"- {x}" for x in live]
    (folder / "packet.md").write_text("\n".join(lines), encoding="utf-8")
    (folder / "rules.json").write_text(
        json.dumps(rules(cfg), indent=1, default=str), encoding="utf-8"
    )
    (folder / "BRIEF.md").write_text(
        BRIEF.format(
            folder=folder,
            tune=f"{w['tune'].first} to {w['tune'].last}",
            n=MAX_PROPOSALS,
            maxp=variants.MAX_PARAMS,
            maxc=variants.MAX_PROMPT_CHARS,
        ),
        encoding="utf-8",
    )
    return folder


def ingest(cfg, folder: Path, reg: Registry | None = None) -> dict:
    from asxbot.arena.levels import load_playbook

    reg = reg or Registry(store.lab_data(cfg))
    folder = Path(folder)
    out = {"registered": [], "refused": []}
    try:
        props = json.loads((folder / "proposals.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        out["refused"].append({"error": f"no readable proposals.json: {e}"})
        props = []
    for p in (props if isinstance(props, list) else [])[:MAX_PROPOSALS]:
        try:
            pb = load_playbook(cfg, p.get("playbook", ""))
            v = reg.register(
                p["playbook"],
                dict(p.get("overrides") or {}),
                agent_prompt=p.get("agent_prompt") or "",
                why=str(p.get("why", ""))[:600],
                proposed_by=f"research {folder.name}",
                base_raw=pb.raw,
            )
            out["registered"].append(v["id"])
        except (KeyError, variants.BadVariant, TypeError) as e:
            out["refused"].append({"proposal": p, "why": str(e)})
    store.write_json(
        folder / "ingested.json", {**out, "at": datetime.now().isoformat(timespec="seconds")}
    )
    return out
