"""The lab's reports (PRACTICE_LAB.md 6): every variant ever tried with its results (STRATEGIES.md:
winners and losers alike), the scoreboard the evening report shows, and the weekly summary.
Locked-test results never appear before a candidate's final run (they are only computed then)."""

from __future__ import annotations

from datetime import datetime, timedelta

from asxbot.lab import runner, shadow, splits, store
from asxbot.lab.variants import Registry

LABEL = (
    "PRACTICE LAB - simulated on real IBKR minute bars through the live code; paper money; "
    "not a verdict until WINNER.md's tests are met"
)


def _pnl(v: dict, window: str, book: str):
    r = ((v.get("results") or {}).get(window) or {}).get("books", {}).get(book)
    return None if not r else r["card"]["pnl_after_fees"]


def _shadow(v: dict, book: str):
    if v["stage"] not in ("shadow", "final", "winner"):
        return None
    sm = shadow.summary(v)
    return {**sm.get(book, {}), "days": sm["days"]} if book in sm else None


def scoreboard(cfg, reg: Registry) -> dict:
    vs = reg.all()
    rows = []
    for v in vs:
        for b in v.get("books") or ["bot"]:
            rows.append(
                {
                    "variant": v["id"],
                    "book": b,
                    "playbook": v["playbook"],
                    "stage": v["stage"],
                    "changes": v.get("overrides"),
                    "prompt": bool(v.get("agent_prompt")),
                    "tune": _pnl(v, "tune", b),
                    "validate": _pnl(v, "validate", b),
                    "shadow": _shadow(v, b),
                    "complexity": v.get("complexity"),
                }
            )
    base = {
        w: runner.baseline_card(w, "asx_daytrader", splits.sessions_in(w))["pnl_after_fees"]
        for w in ("tune", "validate")
        if store.day_files("baseline", w)
    }
    board = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "variants_tried": len(vs),
        "validated": reg.count_validated(),
        "baseline_bot": base,
        "rows": rows,
        "in_shadow": [r for r in rows if r["stage"] == "shadow"],
        "winners": [r for r in rows if r["stage"] == "winner"],
        "contamination": (
            store.read_json(store.lab_data(cfg) / "contamination.json", {}) or {}
        ).get("plain"),
    }
    store.write_json(store.lab_data(cfg) / "scoreboard.json", board)
    return board


def write(cfg, reg: Registry) -> str:
    board = scoreboard(cfg, reg)
    lines = [
        f"# Practice Lab report, {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"*{LABEL}*",
        "",
        f"Variants tried: {board['variants_tried']} (validated: {board['validated']}). "
        f"Frozen day-trader rule bot: TUNE {board['baseline_bot'].get('tune', 'not yet run')}, "
        f"VALIDATE {board['baseline_bot'].get('validate', 'not yet run')} after costs.",
        "",
    ]
    if board["contamination"]:
        lines += [f"Contamination: {board['contamination']}.", ""]
    lines += [
        "| Variant | Book | Stage | Changes | TUNE P&L | VALIDATE P&L | Shadow |",
        "|---|---|---|---|---:|---:|---|",
    ]
    for r in board["rows"]:
        ch = ", ".join(f"{k}={v}" for k, v in (r["changes"] or {}).items()) + (
            " + prompt" if r["prompt"] else ""
        )
        sh = r["shadow"]
        sh_txt = (
            ""
            if not sh
            else f"{sh['pnl']:+,.0f} ({sh['trades']} trades, {sh['vs_frozen_bot']:+,.0f} vs frozen)"
        )
        f = lambda x: "-" if x is None else f"{x:+,.0f}"  # noqa: E731
        lines.append(
            f"| {r['variant']} | {r['book']} | {r['stage']} | {ch or '-'} | {f(r['tune'])} | "
            f"{f(r['validate'])} | {sh_txt} |"
        )
    for v in reg.all():
        lines += [
            "",
            f"## {v['id']} ({v['stage']})",
            f"- proposed by {v.get('proposed_by') or '?'}: {v.get('why') or ''}",
        ]
        for h in v.get("history") or []:
            lines.append(f"- {h['at'][:16]} {h['from']} -> {h['to']}: {h.get('note', '')}")
    text = "\n".join(lines)
    from asxbot.io import write_text_atomic

    out = cfg.root / "reports" / f"lab_{datetime.now():%Y%m%d_%H%M}.md"
    write_text_atomic(text, out)
    return str(out)


def evening_section(cfg) -> list[str]:
    """A few lines for the evening report: practice so far, who is in shadow, the leader."""
    board = store.read_json(store.lab_data(cfg) / "scoreboard.json")
    status = store.read_json(store.lab_data(cfg) / "status.json", {}) or {}
    if not board:
        return (
            ["Practice Lab: warming up (the frozen bots' baseline runs first)."] if status else []
        )
    lines = [
        f"Practice Lab: {board['variants_tried']} variants tried, {len(board['in_shadow'])} in "
        "shadow trading, "
        f"{len(board['winners'])} winner(s)."
    ]
    for r in sorted(board["in_shadow"], key=lambda r: -((r["shadow"] or {}).get("pnl") or 0))[:3]:
        sh = r["shadow"] or {}
        lines.append(
            f"- {r['variant']} ({r['book']}): shadow {sh.get('pnl', 0):+,.0f} over "
            f"{sh.get('days', 0)} days "
            f"vs frozen bot {sh.get('vs_frozen_bot', 0):+,.0f}"
        )
    return lines


def weekly_summary(cfg) -> str:
    reg = Registry(store.lab_data(cfg))
    board = scoreboard(cfg, reg)
    week = [
        x for x in reg.all() if x["created"] >= (datetime.now() - timedelta(days=7)).isoformat()
    ]
    lines = [
        f"Practice Lab, week to {datetime.now():%a %d %b}: {board['variants_tried']} variants "
        "tried so far "
        f"({len(week)} new this week), {len(board['in_shadow'])} in shadow trading, "
        f"{len(board['winners'])} winner(s)."
    ]
    lines += evening_section(cfg)[1:]
    if board["contamination"]:
        lines.append(f"Contamination check: {board['contamination']}.")
    lines.append("All paper money; nothing real happens without your yes.")
    return "\n".join(lines)
