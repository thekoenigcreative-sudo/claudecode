"""Scoring a run: the numbers WINNER.md judges on, after all costs, under EVERY configured
broker's commission (commission is additive per order and changes no fill, so a run is scored
under each schedule from its orders' filled values; the primary broker's is what the account
was actually charged)."""

from __future__ import annotations

import math

import numpy as np

from asxbot.lab.tsim.costs import CostModel

SURVIVORSHIP = ("Survivorship: the universe is the stocks in today's history cache (today's "
                "index lists applied to past days) - stocks that later dropped out are missing, "
                "which flatters results.")  # fmt: skip


def score(out: dict, cfg=None) -> dict:
    acct = out["account"]
    days = out["days"]
    costs = CostModel.from_config(cfg)
    trades = acct["trades"] + list((acct.get("open_trades") or {}).values())
    closed = acct["trades"]
    orders = acct["orders"]
    marks = acct["marks"]
    start = float(acct["start_cash"])
    eq = [start] + [m["equity"] for m in marks]
    daily = np.diff(eq) if len(eq) > 1 else np.array([])
    peak, dd = start, 0.0
    for e in eq:
        peak = max(peak, e)
        dd = max(dd, peak - e)
    net_primary = eq[-1] - start
    by_broker = {}
    prim_fees = sum(o["commission"] for o in orders.values())
    for name, fees in costs.fees.items():
        alt = sum(fees.commission(o["value"]) for o in orders.values() if o["value"] > 0)
        by_broker[name] = round(net_primary + prim_fees - alt, 2)
    nets = sorted((t["gross"] - t["fees"] - t["borrow"]) for t in closed)
    wins = [x for x in nets if x > 0]
    top3 = sum(sorted(nets)[-3:]) if nets else 0.0
    gross_profit = sum(wins)
    moves = [(d.get("market_move_pct"), p) for d, p in zip(days, daily, strict=False)]
    up = [p for mv, p in moves if mv is not None and mv > 0]
    down = [p for mv, p in moves if mv is not None and mv < 0]
    sd = float(np.std(daily, ddof=1)) if len(daily) > 1 else 0.0
    t = float(np.mean(daily) / (sd / math.sqrt(len(daily)))) if sd > 0 else 0.0
    usage = {}
    for d in days:
        for k, v in (d.get("usage") or {}).items():
            usage[k] = usage.get(k, 0) + v
    n_days = max(1, len(days))
    stuck = sum(1 for d in days if d.get("stuck_at_close"))
    return {
        "days": len(days),
        "net": round(net_primary, 2),
        "net_by_broker": by_broker,
        "fees": round(prim_fees, 2),
        "borrow": round(float(acct.get("borrow", 0)), 2),
        "trades": len(closed),
        "open_at_end": len(acct.get("open_trades") or {}),
        "win_rate": round(len(wins) / len(nets), 3) if nets else None,
        "avg_trade": round(sum(nets) / len(nets), 2) if nets else None,
        "without_best3": round(sum(nets) - top3, 2) if nets else 0.0,
        "best3_share_of_gross_profit": round(top3 / gross_profit, 3) if gross_profit > 0 else None,
        "up_days": {"n": len(up), "pnl": round(float(sum(up)), 2)},
        "down_days": {"n": len(down), "pnl": round(float(sum(down)), 2)},
        "max_drawdown": round(dd, 2),
        "max_drawdown_pct": round(dd / start * 100, 2),
        "worst_day": round(float(min(daily)), 2) if len(daily) else 0.0,
        "green_days": int((daily > 0).sum()),
        "red_days": int((daily < 0).sum()),
        "t_stat": round(t, 3),
        "tokens": usage,
        "tokens_per_day": {k: round(v / n_days) for k, v in usage.items()
                           if k in ("input", "cache_write", "cache_read", "output")},  # fmt: skip
        "think_s_per_day": round(sum(d.get("think_s", 0) for d in days) / n_days, 1),
        "calls_per_day": round(sum(d.get("calls", 0) for d in days) / n_days, 1),
        "wall_s_per_day": round(sum(d.get("wall_s", 0) for d in days) / n_days, 2),
        "partial_news_days": sum(1 for d in days if d.get("news_coverage") != "full"),
        "stuck_days": stuck,
        "n_trades_all": len(trades),
        "daily": {d["day"]: round(float(p), 2) for d, p in zip(days, daily, strict=False)},
    }


def line(name: str, s: dict) -> str:
    b = " / ".join(f"{k} ${v:,.0f}" for k, v in s["net_by_broker"].items())
    return (f"| {name} | {s['days']} | {s['trades']} | ${s['net']:,.0f} | {b} | "
            f"{'' if s['win_rate'] is None else f'{s['win_rate']:.0%}'} | "
            f"${s['without_best3']:,.0f} | {s['max_drawdown_pct']:.1f}% | {s['t_stat']:.2f} |")


HEADER = ("| strategy | days | trades | net (primary) | net by broker | win rate | without best 3 "
          "| max DD | t |\n|---|---|---|---|---|---|---|---|---|")  # fmt: skip
