"""The research log (reports/research_log.md) and the search report
(reports/cloud_rules_search_<date>.md), rebuilt whole from data/search/tried.jsonl: every idea,
its reason, its result at each stage and why it stopped - losers first-class."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from asxbot.io import write_text_atomic
from asxbot.lab.score import validation_bar
from asxbot.search import LABEL, SURVIVORSHIP
from asxbot.search.costs import BROKERS, SPREAD_TIERS
from asxbot.search.ideas import all_ideas


def _money(x) -> str:
    return "-" if x is None else f"{x:+,.0f}"


def _row(r: dict) -> str:
    pr, ck = r.get("practice") or {}, r.get("check") or {}
    lk = r.get("locked") or {}
    return (f"| {r['id']} | {r['family']} | {_params(r['params'])} | "
            f"{pr.get('trades', '-')} | {_money(pr.get('pnl'))} | "
            f"{_money(pr.get('vs_baseline'))} | "
            f"{ck.get('trades', '-')} | {_money(ck.get('pnl'))} | "
            f"{'-' if ck.get('t_stat') is None else format(ck['t_stat'], '.2f')} | "
            f"{_money(lk.get('pnl')) if lk else '-'} | {r['verdict']} |")  # fmt: skip


def _params(p: dict) -> str:
    if not p:
        return "defaults"
    return ", ".join(f"{k}={v}" for k, v in p.items()).replace("|", "/")


def latest(tried: list[dict]) -> list[dict]:
    """The last record per idea (a sealed record supersedes its finalist record)."""
    by = {}
    for r in tried:
        by[r["id"]] = r
    return list(by.values())


def research_log(tried: list[dict], data_note: str) -> str:
    rows = latest(tried)
    n = len(rows)
    lines = [
        "# Research log - the rules-only strategy search",
        "",
        f"*{LABEL}*",
        "",
        f"*{SURVIVORSHIP}*",
        "",
        f"Rebuilt {datetime.now():%Y-%m-%d %H:%M} from data/search/tried.jsonl. "
        f"Ideas tried: **{n}**. The check-set bar now stands at t >= "
        f"{validation_bar(max(n, 1)):.2f} (sqrt(2 ln N), N = every idea tried).",
        "",
        data_note,
        "",
    ]
    if not rows:
        lines += ["No idea has been run yet.", ""]
    for r in rows:
        pr, ck = r.get("practice") or {}, r.get("check") or {}
        lines += [
            f"## {r['id']} - {r['family']} ({_params(r['params'])})",
            "",
            f"- Idea #{r.get('n_at_try')} (wave {r.get('wave')}, parent {r.get('parent') or '-'}"
            f"), tried {r.get('tried_at')}.",
            f"- Why it might work: {r['reason']}",
            f"- Compared with: the frozen {r['baseline']} rule bot on the same days.",
        ]
        if pr:
            lines.append(
                f"- Practice (TUNE): {pr['trades']} trades, {_money(pr['pnl'])} after costs, "
                f"win rate {pr.get('win_rate_pct')}%, t {pr.get('t_stat')}, worst day "
                f"{_money(pr.get('worst_day'))}, frozen bot same days "
                f"{_money(pr.get('baseline_pnl_same_days'))}, stuck at close "
                f"{pr.get('stuck_at_close')}.")  # fmt: skip
        if ck:
            lines.append(
                f"- Check (VALIDATE): {ck['trades']} trades, {_money(ck['pnl'])}, t "
                f"{ck.get('t_stat')} against a bar of {ck.get('bar')}; 2x spread/impact "
                f"{_money((r.get('check_2x') or {}).get('pnl'))}; cheapest API broker "
                f"{_money((r.get('check_cheapest_broker') or {}).get('pnl'))}.")  # fmt: skip
        if r.get("locked"):
            lk = r["locked"]
            lines.append(f"- SEALED test (run once, F={r.get('F')}): {lk['trades']} trades, "
                         f"{_money(lk['pnl'])}.")  # fmt: skip
            for name, ok, fig in r.get("locked_checks") or []:
                lines.append(f"  - {'PASS' if ok else 'FAIL'} {name}: {fig}")
        lines += [f"- Verdict: **{r['verdict']}** - {'; '.join(r.get('why') or [])}", ""]
    return "\n".join(lines)


def report(tried: list[dict], meta: dict) -> str:
    rows = latest(tried)
    n = len(rows)
    stages = (("check", "finalist", "sealed"), ("finalist", "sealed"), ("sealed",))
    reached = {s: sum(1 for r in rows if r.get("stage") in s) for s in stages}
    passed = [r for r in rows if r.get("stage") == "sealed"
              and all(ok for _, ok, _ in r.get("locked_checks") or [])]  # fmt: skip
    fams = sorted({r["family"] for r in rows})
    lines = [
        f"# Rules-only strategy search - {meta.get('date')}",
        "",
        f"*{LABEL}*",
        "",
        f"**{SURVIVORSHIP}**",
        "",
        "## In one paragraph",
        "",
        meta.get("summary", ""),
        "",
        "## The data",
        "",
        meta.get("data_note", ""),
        "",
        "## Costs used (fixed before any idea ran)",
        "",
        *[f"- Brokerage, {b.name}: {b.pct}% of value, min A${b.minimum:.2f} per order. "
          f"Source: {b.source}" for b in BROKERS.values()],
        "- Spread: a half-spread paid on every fill, by the stock's median daily turnover "
        "(20 sessions before): "
        + ", ".join(f">= ${f / 1e6:,.0f}m: {p}%" for f, p in SPREAD_TIERS)
        + " - and never less than one full tick each way.",
        "- Impact: 0.5 x daily volatility x sqrt(order value / daily turnover), capped at 2% "
        "a side. Fills capped at 20% of each minute's volume and 20% of the closing auction; "
        "what the auction cannot take is sold at the next open and counted as stuck.",
        "- Every idea that reached the check set was also run with spread and impact doubled, "
        "and with the cheapest API broker's brokerage.",
        "",
        "## Scoreboard",
        "",
        f"Ideas tried: **{n}** across {len(fams)} families ({', '.join(fams) or '-'}). "
        f"Reached the check set: {reached[('check', 'finalist', 'sealed')]}. Finalists: "
        f"{reached[('finalist', 'sealed')]}. Given the sealed test: {reached[('sealed',)]}. "
        f"Passed it: **{len(passed)}**.",
        "",
        "| Idea | Family | Changes | Practice trades | Practice P&L | vs frozen bot | "
        "Check trades | Check P&L | Check t | Sealed P&L | Verdict |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
        *[_row(r) for r in rows],
        "",
        "## What passed",
        "",
    ]
    if passed:
        for r in passed:
            lines.append(f"- {r['id']} ({r['family']}, {_params(r['params'])}): "
                         f"{r['verdict']}.")  # fmt: skip
    else:
        lines.append("Nothing passed. No strategy in this search has shown an edge after costs "
                     "that survived the practice split, the check set and the sealed test.")
    lines += ["", "## For the AI trader (the other cloud session)", "", meta.get("next", ""), ""]
    lines += ["## Every idea, in full", "", "See reports/research_log.md.", ""]
    return "\n".join(lines)


def write(repo: Path, tried: list[dict], meta: dict) -> tuple[Path, Path]:
    reports = Path(repo) / "reports"
    log = reports / "research_log.md"
    write_text_atomic(research_log(tried, meta.get("data_note", "")), log)
    rep = reports / f"cloud_rules_search_{meta['date'].replace('-', '')}.md"
    write_text_atomic(report(tried, meta), rep)
    js = reports / f"cloud_rules_search_{meta['date'].replace('-', '')}.json"
    write_text_atomic(json.dumps({"label": LABEL, "survivorship": SURVIVORSHIP, "meta": meta,
                                  "ideas_registered": len(all_ideas()),
                                  "results": latest(tried)}, indent=1, default=str),
                      js)  # fmt: skip
    return log, rep


__all__ = ["report", "research_log", "write"]
