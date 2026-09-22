"""Render reports/phase1.md from backtest results. Honest by construction: the data label,
sample-size flags, IS/OOS split and the benchmark comparison are always printed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from asxbot.backtest.engine import Result
from asxbot.backtest.metrics import split_is_oos, summarise, trade_stats, yearly_returns


@dataclass
class Block:
    universe: str
    strategy: str
    slippage_x: float
    result: Result
    entry_note: str = ""


@dataclass
class ReportInput:
    data_label: str
    provider_name: str
    survivorship_safe: bool
    benchmark_note: str
    benchmark_equity: pd.Series
    universes: dict[str, str]  # name -> provenance
    universe_sizes: dict[str, int]
    announcement_coverage: str
    params: dict
    oos_start: pd.Timestamp
    blocks: list[Block] = field(default_factory=list)
    baselines: dict[tuple[str, float], Result] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _f(x, nd=1, suffix=""):
    if x is None or (isinstance(x, float) and x != x):
        return "n/a"
    return f"{x:,.{nd}f}{suffix}"


def _summary_table(rows: list[tuple[str, dict]]) -> str:
    head = (
        "| run | trades | win % | avg trade % | median % | CAGR % | max DD % | turnover x/yr | "
        "exposure % | costs $ | final $ | flag |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|\n"
    )
    out = head
    for name, s in rows:
        flag = "SMALL SAMPLE" if s.get("small_sample") else ""
        out += (
            f"| {name} | {s['trades']} | {_f(s['win_rate_pct'])} | {_f(s['avg_trade_pct'], 2)} | "
            f"{_f(s.get('median_trade_pct'), 2)} | {_f(s['cagr_pct'])} | {_f(s['max_dd_pct'])} | "
            f"{_f(s['turnover_x_per_year'])} | {_f(s['exposure_pct'])} | "
            f"{_f(s['total_costs_aud'], 0)} | {_f(s['final_equity'], 0)} | {flag} |\n"
        )
    return out


def _is_oos_table(res: Result, oos_start: pd.Timestamp, small: int) -> str:
    is_, oos = split_is_oos(res.trades, oos_start)
    out = (
        "| period | trades | win % | avg trade % | total P&L $ | flag |\n"
        "|---|---:|---:|---:|---:|---|\n"
    )
    for label, t in (("in-sample", is_), (f"out-of-sample (from {oos_start.date()})", oos)):
        s = trade_stats(t)
        flag = "SMALL SAMPLE" if s["trades"] < small else ""
        out += (
            f"| {label} | {s['trades']} | {_f(s['win_rate_pct'])} | {_f(s['avg_trade_pct'], 2)} | "
            f"{_f(s['total_pnl_aud'], 0)} | {flag} |\n"
        )
    return out


def _yearly_table(named: list[tuple[str, pd.Series]]) -> str:
    df = pd.DataFrame({n: s for n, s in named})
    if df.empty:
        return "_no data_\n"
    out = "| year | " + " | ".join(df.columns) + " |\n|---|" + "---:|" * len(df.columns) + "\n"
    for y, row in df.iterrows():
        out += f"| {y} | " + " | ".join(_f(v) for v in row) + " |\n"
    return out


def _by_type_table(trades: pd.DataFrame) -> str:
    if trades.empty or "ann_type" not in trades or trades["ann_type"].isna().all():
        return ""
    g = trades.groupby(trades["ann_type"].fillna("n/a"))
    out = "| announcement type | trades | win % | avg trade % |\n|---|---:|---:|---:|\n"
    for k, t in g:
        s = trade_stats(t)
        out += f"| {k} | {s['trades']} | {_f(s['win_rate_pct'])} | {_f(s['avg_trade_pct'], 2)} |\n"
    return out


def render(r: ReportInput, small_sample: int) -> str:
    L: list[str] = []
    L.append("# Phase 1 backtest report\n")
    L.append(f"_Generated {datetime.now():%Y-%m-%d %H:%M}._\n")
    L.append(f"## DATA LABEL: **{r.data_label.upper()}**\n")
    L.append(
        f"Price provider: `{r.provider_name}`. Survivorship-safe: **{r.survivorship_safe}**. "
        "Nothing in this report is a go/no-go unless the provider is Norgate Platinum with "
        "delisted stocks included.\n"
    )
    for w in r.warnings:
        L.append(f"> **Warning:** {w}\n")
    L.append("## Setup\n")
    L.append(f"- Benchmark: {r.benchmark_note}")
    for name, prov in r.universes.items():
        L.append(f"- Universe `{name}`: {r.universe_sizes.get(name, 0)} codes. Source: {prov}")
    L.append(f"- Announcement archive coverage: {r.announcement_coverage}")
    L.append(f"- Out-of-sample holdout starts {r.oos_start.date()} (last 3 years).")
    L.append("- Parameters (frozen 2026-09-22, before any data was fetched):")
    for k, v in r.params.items():
        L.append(f"  - {k}: {v}")
    L.append("")

    bench_yr = yearly_returns(r.benchmark_equity)
    bench_sum = summarise(
        r.benchmark_equity,
        pd.DataFrame(columns=["pnl", "ret_pct", "entry_value", "costs"]),
        pd.Series(1.0, index=r.benchmark_equity.index),
        small_sample,
    )

    for uni in r.universes:
        L.append(f"## Universe: {uni}\n")
        blocks = [b for b in r.blocks if b.universe == uni]
        for sx in sorted({b.slippage_x for b in blocks}):
            L.append(f"### Slippage x{sx:g}\n")
            rows = []
            for b in [b for b in blocks if b.slippage_x == sx]:
                s = summarise(b.result.equity, b.result.trades, b.result.exposure, small_sample)
                rows.append((f"{b.strategy}{(' ' + b.entry_note) if b.entry_note else ''}", s))
            base = r.baselines.get((uni, sx))
            if base is not None:
                rows.append(
                    (
                        "momentum baseline",
                        summarise(base.equity, base.trades, base.exposure, small_sample),
                    )
                )
            rows.append(("benchmark (buy & hold)", {**bench_sum, "small_sample": False}))
            L.append(_summary_table(rows))
            for b in [b for b in blocks if b.slippage_x == sx]:
                L.append(f"**{b.strategy} {b.entry_note}** in-sample vs out-of-sample\n")
                L.append(_is_oos_table(b.result, r.oos_start, small_sample))
                bt = _by_type_table(b.result.trades)
                if bt:
                    L.append(
                        f"**{b.strategy}** by announcement type (mechanical headline classes)\n"
                    )
                    L.append(bt)
            L.append("**Results by year (% return on equity)**\n")
            named = [
                (
                    f"{b.strategy}{' ' + b.entry_note if b.entry_note else ''}",
                    yearly_returns(b.result.equity),
                )
                for b in blocks
                if b.slippage_x == sx
            ]
            if base is not None:
                named.append(("baseline", yearly_returns(base.equity)))
            named.append(("benchmark", bench_yr))
            L.append(_yearly_table(named))
    L.append("## Verdict\n")
    L.append(verdict(r, small_sample))
    return "\n".join(L) + "\n"


def verdict(r: ReportInput, small_sample: int) -> str:
    lines = []
    if not r.survivorship_safe:
        lines.append(
            f"**{r.data_label.upper()}.** The price data has no delisted stocks and no "
            "point-in-time index membership, so every number above is biased upward by "
            "survivorship. This run proves the pipeline works end to end; it says nothing "
            "reliable about the edge. Rerun on Norgate Platinum before any decision."
        )
    bench = summarise(
        r.benchmark_equity,
        pd.DataFrame(columns=["pnl", "ret_pct", "entry_value", "costs"]),
        pd.Series(1.0, index=r.benchmark_equity.index),
        small_sample,
    )
    for (uni, sx), base in r.baselines.items():
        bs = summarise(base.equity, base.trades, base.exposure, small_sample)
        if bs["cagr_pct"] > 20:
            lines.append(
                f"- {uni} @ x{sx:g}: the momentum baseline shows CAGR {_f(bs['cagr_pct'])}%, which "
                "is implausible for a 4-stock ASX momentum portfolio. On a universe built from "
                "today's largest companies, momentum simply buys the stocks that went on to "
                "become large. This is what survivorship bias looks like; treat the baseline as "
                "broken until point-in-time membership is available."
            )
    for b in r.blocks:
        s = summarise(b.result.equity, b.result.trades, b.result.exposure, small_sample)
        base = r.baselines.get((b.universe, b.slippage_x))
        bs = summarise(base.equity, base.trades, base.exposure, small_sample) if base else None
        beats_bench = s["cagr_pct"] > bench["cagr_pct"]
        beats_base = bs is None or s["cagr_pct"] > bs["cagr_pct"]
        tag = "SMALL SAMPLE, " if s["small_sample"] else ""
        lines.append(
            f"- {b.universe} / {b.strategy} {b.entry_note} @ x{b.slippage_x:g}: {tag}"
            f"{s['trades']} trades, avg {_f(s['avg_trade_pct'], 2)}% per trade after costs, "
            f"CAGR {_f(s['cagr_pct'])}% vs benchmark {_f(bench['cagr_pct'])}%"
            + (f" and baseline {_f(bs['cagr_pct'])}%" if bs else "")
            + f". Beats benchmark: {beats_bench}. Beats baseline: {beats_base}."
        )
    return "\n".join(lines) + "\n"
