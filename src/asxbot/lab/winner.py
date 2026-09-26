"""WINNER.md in code (fixed 26 Sep 2026 before the search). tests/test_lab.py fails if the numbers
here and in WINNER.md disagree. `check` returns every criterion with pass/fail and the figure;
`message` is the ONE Trader message Rick gets when all pass."""

from __future__ import annotations

import math

from asxbot.arena.replay_ibkr import START_CASH
from asxbot.lab.score import versus

CRITERIA = {
    "locked_positive": 0.0,  # 1
    "shadow_min_days": 10,  # 2
    "shadow_min_trades": 15,
    "locked_min_trades": 40,  # 3
    "top_n": 3,  # 4
    "top_share_max_pct": 50.0,
    "shadow_top_check_min_trades": 30,
    "updown_min_days": 5,  # 5
    "max_drawdown_pct": 10.0,  # 7
    "worst_day_pct": -3.0,
    "t_base": 1.65,  # 9
    "t_per_ln_final": 0.5,
    "max_params": 6,  # 11
    "max_prompt_chars": 600,
}


def t_required(n_finals: int) -> float:
    return CRITERIA["t_base"] + CRITERIA["t_per_ln_final"] * math.log(max(1, n_finals))


def check(
    v: dict,
    locked: dict,
    locked_base: dict,
    shadow: dict,
    shadow_base: dict,
    n_finals: int,
    anon_flip: bool | None,
) -> list[tuple[str, bool, str]]:
    C = CRITERIA
    worst = C["worst_day_pct"] / 100 * START_CASH
    out = []

    def add(name, ok, fig):
        out.append((name, bool(ok), fig))

    add(
        "1 positive after costs on the locked test",
        locked["pnl_after_fees"] > C["locked_positive"],
        f"{locked['pnl_after_fees']:+,.2f}",
    )
    add(
        "2 positive in shadow trading",
        shadow["days"] >= C["shadow_min_days"]
        and shadow["trades"] >= C["shadow_min_trades"]
        and shadow["pnl_after_fees"] > 0,
        f"{shadow['pnl_after_fees']:+,.2f} over {shadow['days']} days, {shadow['trades']} trades",
    )
    add(
        "3 enough trades (locked)",
        locked["trades"] >= C["locked_min_trades"],
        f"{locked['trades']} trades",
    )
    share = locked.get("top3_share_of_gross_pct")
    ok4 = locked["pnl_without_top3"] > 0 and share is not None and share <= C["top_share_max_pct"]
    if shadow["trades"] >= C["shadow_top_check_min_trades"]:
        ok4 = ok4 and shadow["pnl_without_top3"] > 0
    add(
        "4 not carried by its best 3 trades",
        ok4,
        f"without them {locked['pnl_without_top3']:+,.2f}; they are {share}% of gross profit",
    )
    up, down = locked["up_days"], locked["down_days"]
    add(
        "5 holds on up and down market days",
        up["n"] >= C["updown_min_days"]
        and down["n"] >= C["updown_min_days"]
        and up["pnl"] >= 0
        and down["pnl"] >= 0,
        f"up {up['n']} days {up['pnl']:+,.2f}, down {down['n']} days {down['pnl']:+,.2f}",
    )
    e1, e2 = versus(locked, locked_base), versus(shadow, shadow_base)
    add("6 beats the frozen rule bot", e1 > 0 and e2 > 0, f"locked {e1:+,.2f}, shadow {e2:+,.2f}")
    ok7 = (
        locked["max_drawdown_pct"] <= C["max_drawdown_pct"]
        and locked["worst_day"] >= worst
        and shadow.get("worst_day", 0) >= worst
    )
    add(
        "7 reasonable worst drawdown",
        ok7,
        f"drawdown {locked['max_drawdown_pct']:.1f}%, worst day {locked['worst_day']:+,.2f} "
        "(shadow "
        f"{shadow.get('worst_day', 0):+,.2f})",
    )
    add(
        "8 more green days than red",
        locked["green_days"] > locked["red_days"],
        f"{locked['green_days']} green / {locked['red_days']} red",
    )
    need = t_required(n_finals)
    add(
        "9 not luck from trying many things",
        locked["t_stat"] >= need,
        f"t {locked['t_stat']:.2f}, needs {need:.2f} ({n_finals} final candidate(s) so far)",
    )
    add(
        "10 not knowledge of the past",
        anon_flip is not True,
        "post-cutoff days only; "
        + (
            "anonymising did not flip it"
            if anon_flip is False
            else "rule bot (no model)"
            if anon_flip is None
            else "anonymising FLIPPED it"
        ),
    )
    add(
        "11 simple enough",
        len(v.get("overrides") or {}) <= C["max_params"]
        and len(v.get("agent_prompt") or "") <= C["max_prompt_chars"],
        f"{len(v.get('overrides') or {})} parameters, {len(v.get('agent_prompt') or '')} prompt "
        "characters",
    )
    return out


def is_winner(results: list[tuple[str, bool, str]]) -> bool:
    return bool(results) and all(ok for _, ok, _ in results)


def message(v: dict, book: str, results: list, locked: dict, shadow: dict) -> str:
    what = "the day trader" if v["playbook"] == "asx_daytrader" else "announcements v2"
    who = "the rule bot" if book == "bot" else "the AI agent"
    changes = (
        ", ".join(f"{k} {val}" for k, val in (v.get("overrides") or {}).items())
        or "no rule changes"
    )
    lines = [
        f"Practice Lab: a candidate has met every WINNER.md test - {what}, {who}, variant "
        f"{v['id']} ({changes}).",
        f"Locked test (Aug 17-Sep 25, run once): {locked['pnl_after_fees']:+,.0f} after costs on "
        f"{locked['trades']} trades, "
        f"{locked['green_days']} green / {locked['red_days']} red days, worst drawdown "
        f"{locked['max_drawdown_pct']:.1f}%.",
        f"Shadow trading since promotion: {shadow['pnl_after_fees']:+,.0f} over {shadow['days']} "
        f"days, {shadow['trades']} trades.",
    ]
    lines += [f"- {name}: {fig}" for name, ok, fig in results]
    lines.append(
        "All paper money so far. Trial it with real money? (Nothing happens without your yes; "
        "it would start one ladder level below, with your approval on every order.)"
    )
    return "\n".join(lines)
