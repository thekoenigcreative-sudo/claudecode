"""Two traders on the SAME days, side by side (Rick, 26 Sep: "measure the team against a single
agent on the same days"): P&L after all costs per day and in total, trades, win rate, drawdown,
and what each cost to run - calls, thinking time, tokens by model (Opus / Sonnet), dollars.

    asxbot lab sim compare ai_base_practice10 team_practice10
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from asxbot.lab import store
from asxbot.lab.tsim.report import SURVIVORSHIP, score


def load(run_id: str) -> dict:
    from asxbot.lab.tsim.run import tsim_local

    rdir = tsim_local() / "runs" / run_id
    spec = store.read_json(rdir / "spec.json", {}) or {}
    acct = store.read_json(rdir / "account.json", None) or (store.read_json(
        rdir / "state.json", {}) or {}).get("account")  # fmt: skip
    days = [store.read_json(p) for p in sorted((rdir / "days").glob("*.json"))]
    return {"run_id": run_id, "spec": spec, "days": days, "account": acct}


def _money(u: dict, prefix: str = "") -> str:
    k = lambda x: u.get(f"{prefix}{x}", 0)  # noqa: E731
    return (f"{k('input') + k('cache_write') + k('cache_read'):,} in "
            f"({k('cache_read'):,} cached), {k('output'):,} out")  # fmt: skip


def report(cfg, run_a: str, run_b: str, out: Path | None = None) -> Path:
    A, B = load(run_a), load(run_b)
    common = sorted({d["day"] for d in A["days"]} & {d["day"] for d in B["days"]})
    A["days"] = [d for d in A["days"] if d["day"] in common]
    B["days"] = [d for d in B["days"] if d["day"] in common]
    sa, sb = score(A, cfg), score(B, cfg)
    L = [f"# {run_a} vs {run_b}: the same {len(common)} days", "",
         f"Written {datetime.now():%a %d %b %Y %H:%M}. Simulated money on real ASX history "
         "(the lab's trading simulator); every figure after brokerage, spread and slippage. "
         f"Disguised: {A['spec'].get('disguised')} / {B['spec'].get('disguised')}.", "",
         f"_{SURVIVORSHIP}_", "",
         "| | " + run_a + " | " + run_b + " |", "|---|---|---|"]  # fmt: skip
    rows = [
        ("net after costs (IBKR)", lambda s: f"${s['net']:,.2f}"),
        ("  made on the simulated days", lambda s: f"${s['net_days']:,.2f}"),
        ("  across unsimulated gaps (not trading)", lambda s: f"${s['gap_pnl']:,.2f}"),
        ("closed trades", lambda s: s["trades"]),
        ("win rate", lambda s: "-" if s["win_rate"] is None else f"{s['win_rate']:.0%}"),
        ("brokerage", lambda s: f"${s['fees']:,.2f}"),
        ("max drawdown", lambda s: f"{s['max_drawdown_pct']:.2f}%"),
        ("worst day", lambda s: f"${s['worst_day']:,.2f}"),
        ("green / red days", lambda s: f"{s['green_days']} / {s['red_days']}"),
        ("t (daily P&L)", lambda s: f"{s['t_stat']:.2f}"),
        ("model calls per day", lambda s: s["calls_per_day"]),
        ("thinking time per day", lambda s: f"{s['think_s_per_day']:.0f} s"),
        ("tokens (all days)", lambda s: _money(s["tokens"])),
        (
            "  of which Opus",
            lambda s: (
                _money(s["tokens"], "opus:")
                if "opus:output" in s["tokens"]
                else "all (single agent)"
            ),
        ),  # fmt: skip
        (
            "  of which Sonnet",
            lambda s: _money(s["tokens"], "sonnet:") if "sonnet:output" in s["tokens"] else "-",
        ),  # fmt: skip
        ("API-equivalent cost", lambda s: f"${s['tokens'].get('cost_usd', 0):,.2f}"),
    ]
    for name, f in rows:
        L.append(f"| {name} | {f(sa)} | {f(sb)} |")
    L += ["", "## Day by day", "", f"| day | index | {run_a} | {run_b} |", "|---|---|---|---|"]
    da = {d["day"]: d for d in A["days"]}
    db = {d["day"]: d for d in B["days"]}
    for day in common:
        mv = da[day].get("market_move_pct")
        L.append(f"| {day} | {'' if mv is None else f'{mv:+.2f}%'} | ${da[day]['pnl']:,.2f} | "
                 f"${db[day]['pnl']:,.2f} |")  # fmt: skip
    diff = sb["net_days"] - sa["net_days"]
    L += ["", "## Plainly", "",
          f"Over {len(common)} days the {'second' if diff > 0 else 'first'} did better by "
          f"${abs(diff):,.2f} after costs, on what was made on the simulated days themselves. "
          f"{len(common)} days is a small sample: this is a first "
          "reading, not a verdict (WINNER.md's bar needs 40+ trades on the sealed block and 10+ "
          "shadow days)."]  # fmt: skip
    out = out or Path(cfg.root) / "reports" / f"tsim_compare_{run_a}_vs_{run_b}.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out
