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


def _row(r: dict, diag: dict) -> str:
    pr, ck = r.get("practice") or {}, r.get("check") or {}
    lk = r.get("locked") or {}
    cheap = (diag.get("cheapest_broker_practice") or {}).get(r["id"]) or {}
    turn = pr.get("turnover") or 0
    raw = pr.get("raw_pnl")
    raw_pct = f"{raw / turn * 100:+.2f}%" if turn and raw is not None else "-"
    return (f"| {r['id']} | {r['family']} | {_params(r['params'])} | "
            f"{pr.get('trades', '-')} | {raw_pct} | {_money(pr.get('raw_pnl'))} | "
            f"{_money(pr.get('pnl'))} | {_money(cheap.get('pnl'))} | "
            f"{_money(pr.get('vs_baseline'))} | "
            f"{ck.get('trades', '-')} | {_money(ck.get('pnl'))} | "
            f"{'-' if ck.get('t_stat') is None else format(ck['t_stat'], '.2f')} | "
            f"{_money(lk.get('pnl')) if lk else '-'} | {r['verdict']} |")  # fmt: skip


def _params(p: dict) -> str:
    if not p:
        return "defaults"
    return ", ".join(f"{k}={v}" for k, v in p.items()).replace("|", "/")


def latest(tried: list[dict]) -> list[dict]:
    """The last record per idea (a sealed record supersedes its finalist record). A record
    written before 'not run - no data' existed, with no playable practice day, is shown as
    what it was."""
    by = {}
    for r in tried:
        if (r.get("practice") or {}).get("days") == 0 and r.get("verdict") == "failed practice":
            r = {**r, "verdict": "not run - no data",
                 "why": ["no practice session had the data this idea trades"]}  # fmt: skip
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
        "## In short",
        "",
        meta.get("summary", ""),
        "",
        "## The data",
        "",
        meta.get("data_note", ""),
        "",
        *(["## Checks on the engine and the data", "", meta["checks"], ""]
          if meta.get("checks") else []),
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
        "Practice = TUNE (26 Mar-30 Jun), check = VALIDATE (1 Jul-14 Aug), sealed = LOCKED "
        "(17 Aug-25 Sep). Raw = the price move before any cost (what the idea would make if "
        "trading were free), per trade as a share of the money traded, and in dollars.",
        "",
        "| Idea | Family | Changes | Practice trades | Raw move / trade | Practice raw $ | "
        "Practice P&L (IBKR) | Practice P&L (cheapest API broker) | vs frozen bot | "
        "Check trades | Check P&L | Check t | Sealed P&L | Verdict |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        *[_row(r, meta.get("diagnostics") or {}) for r in rows],
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
    info = (meta.get("diagnostics") or {}).get("info_check") or {}
    if info:
        lines += ["", "## Near-misses on the check set (information only - they stay failed)", "",
                  "These made money on practice with 20+ trades but were stopped only because "
                  "they did not beat the frozen rule bot on the same days. Their check-set "
                  "result is shown so the next session knows whether the edge held; it cannot "
                  "promote them.", "",
                  "| Idea | Check trades | Check P&L | Check t | Raw $ |",
                  "|---|---:|---:|---:|---:|"]  # fmt: skip
        for k, v in info.items():
            lines.append(f"| {k} | {v['trades']} | {_money(v['pnl'])} | {v['t_stat']} | "
                         f"{_money(v.get('raw_pnl'))} |")  # fmt: skip
    fb = (meta.get("diagnostics") or {}).get("frozen_bots") or {}
    if fb:
        lines += ["", "## The frozen rule bots on the same windows (the bar to beat)", ""]
        for k, v in fb.items():
            name = {"daytrader": "day trader v1", "v2": "announcements v2"}[k.split(":")[0]]
            lines.append(f"- {name}, {k.split(':')[1]}: {v['trades']} trades, "
                         f"{_money(v['pnl'])} after costs (the arena's cost model).")  # fmt: skip
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
