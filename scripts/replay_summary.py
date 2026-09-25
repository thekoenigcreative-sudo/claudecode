"""The replay report Rick reads (26 Sep 2026): the engine's report (arena/replay_ibkr.py)
with the history's coverage in front of it and a per-setup breakdown behind it.

    python scripts\\replay_summary.py <replay_ibkr_*.json> [<fetch report json>]

Writes reports/replay_<YYYYMMDD>.md and .json next to the engine's files. The breakdown by
setup is DESCRIPTIVE: a slice of a result, found after the aggregate, is a hypothesis for a
holdout, never a finding (CLAUDE.md, STRATEGIES.md rule 6).
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SYD = ZoneInfo("Australia/Sydney")


def setup_of(reason: str) -> str:
    r = reason or ""
    if r.startswith("[") and "]" in r:
        return r[1:r.index("]")]
    return "v2 10:30 rule" if "10:30" in r or "rule bot" in r.lower() else "other"


def breakdown(days: list[dict], key: str) -> list[dict]:
    rows: dict = defaultdict(lambda: {"trades": 0, "wins": 0, "net": 0.0, "r": [], "fees": 0.0})
    for d in days:
        for t in (d.get(key) or {}).get("trades", []):
            b = rows[setup_of(t.get("reason", ""))]
            b["trades"] += 1
            b["wins"] += t["net"] > 0
            b["net"] += t["net"]
            b["fees"] += t.get("fees", 0.0)
            if t.get("r") is not None:
                b["r"].append(t["r"])
    out = []
    for name, b in sorted(rows.items(), key=lambda kv: -kv[1]["trades"]):
        out.append({"setup": name, "trades": b["trades"],
                    "win_rate_pct": round(b["wins"] / b["trades"] * 100, 1),
                    "net_after_fees": round(b["net"], 2), "fees": round(b["fees"], 2),
                    "avg_r": round(sum(b["r"]) / len(b["r"]), 3) if b["r"] else None})
    return out


def exits(days: list[dict], key: str) -> dict:
    c: dict = defaultdict(int)
    for d in days:
        for t in (d.get(key) or {}).get("trades", []):
            for x in t.get("exits", []):
                kind = x.split(":")[1].strip().split(" ")[0].lower() if ":" in x else x
                c[{"flat": "flat by the close", "stop": "stop", "half": "half off at +2R"}.get(
                    kind, kind)] += 1
    return dict(sorted(c.items(), key=lambda kv: -kv[1]))


def main() -> int:
    src = Path(sys.argv[1])
    js = json.loads(src.read_text(encoding="utf-8"))
    fetch = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")) if len(sys.argv) > 2 else {}
    days = [d for d in js["days"] if not d.get("gaps")]
    engine_md = src.with_suffix(".md").read_text(encoding="utf-8")
    stamp = datetime.now(SYD)
    out_md = src.parent / f"replay_{stamp:%Y%m%d}.md"
    cov = (fetch.get("coverage") or {}) if fetch else {}
    lines = [
        f"# Replay results - {js['first']} to {js['last']} ({len(days)} sessions replayed)",
        "",
        "*REPLAY, NOT LIVE. The frozen rule bots only - the agent's judgment is not in it. "
        "IBKR 1-minute history. Today's ASX 300 / ASX 200 lists applied to every past day "
        "(survivorship). A replay earns a hypothesis, never a promotion.*",
        "",
        "## The history it ran on",
        "",
    ]
    if cov:
        lines.append(
            f"- {cov.get('codes')} codes x {cov.get('sessions')} sessions: "
            f"{cov.get('stock_days_held'):,} stock-days of 1-minute bars on disk, "
            f"{cov.get('stock_days_empty'):,} known empty (the stock did not trade), "
            f"{cov.get('stock_days_missing'):,} missing; {cov.get('codes_complete')} codes "
            "complete.")  # fmt: skip
        partial = cov.get("codes_partial") or []
        if partial:
            worst = ", ".join(f"{c} ({n})" for c, n in sorted(partial, key=lambda x: -x[1])[:10])
            lines.append(f"- codes with sessions missing (most first): {worst}")
    gapped = [d for d in js["days"] if d.get("gaps")]
    lines.append(f"- sessions not replayed (data gaps): {len(gapped)}"
                 + (": " + "; ".join(f"{d['day']} {', '.join(d['gaps'])}" for d in gapped[:10])
                    if gapped else ""))  # fmt: skip
    lines += ["", "## By setup (descriptive - a slice is a hypothesis, not a finding)", ""]
    extra = {}
    for key, name in (("daytrader", "Day trader rule bot"), ("v2", "v2 rule bot")):
        rows = breakdown(days, key)
        extra[key] = {"by_setup": rows, "exits": exits(days, key)}
        if not rows:
            continue
        lines += [f"**{name}**", "",
                  "| Setup | Trades | Win rate | Avg R | Net after fees | Fees |",
                  "|---|---:|---:|---:|---:|---:|"]  # fmt: skip
        for r in rows:
            ar = "-" if r["avg_r"] is None else f"{r['avg_r']:+.2f}"
            lines.append(f"| {r['setup']} | {r['trades']} | {r['win_rate_pct']:.0f}% | {ar} | "
                         f"{r['net_after_fees']:+,.2f} | {r['fees']:,.2f} |")  # fmt: skip
        ex = extra[key]["exits"]
        lines += ["", "Exits: " + ", ".join(f"{k} {v}" for k, v in ex.items()), ""]
    lines += ["---", "", "# The engine's report", "", engine_md]
    out_md.write_text("\n".join(lines), encoding="utf-8")
    out_js = out_md.with_suffix(".json")
    out_js.write_text(json.dumps({"source": src.name, "coverage": cov, **extra,
                                  "v2": js["v2"], "daytrader": js["daytrader"],
                                  "label": js["label"]}, indent=1, default=str),
                      encoding="utf-8")  # fmt: skip
    print(f"written: {out_md} and {out_js.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
