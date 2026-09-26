"""Scoring a variant's book over a window, and the gates before shadow trading (WINNER.md).

Built on the replay's scorecard (arena/replay_ibkr.scorecard: trades, win rate, R, P&L after
fees, green/red days, drawdown on the chained $20,000 books, top-3 share, up/down days), plus
what the gates need: the daily-P&L t-statistic, P&L without the best three trades, the worst
day, and the comparison with the frozen rule bot on the same days.
"""

from __future__ import annotations

import math

from asxbot.arena.replay_ibkr import START_CASH, scorecard

MIN_TRADES_GATE = 20
WORST_DAY = -0.03 * START_CASH


def book_days(days: list[dict], playbook: str, book: str) -> list[dict]:
    """Per-day records reshaped so replay_ibkr.scorecard reads one book of one playbook."""
    key = "v2" if playbook == "asx_announcements_v2" else "daytrader"
    out = []
    for d in days:
        if d.get("gaps") or key not in d:
            continue
        b = (d[key].get("books") or {}).get(book)
        if b is None:
            continue
        out.append({"day": d["day"], "market_move_pct": d.get("market_move_pct"), "book": b})
    return out


def t_stat(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    if var <= 0:
        return 0.0 if m == 0 else math.copysign(99.0, m)
    return m / math.sqrt(var / n)


def card(days: list[dict], playbook: str, book: str) -> dict:
    rows = book_days(days, playbook, book)
    s = scorecard(rows, "book")
    daily = [float(r["book"]["pnl"]) for r in rows]
    trades = sorted((float(t["net"]) for r in rows for t in r["book"]["trades"]), reverse=True)
    gross_profit = sum(x for x in trades if x > 0)
    top3 = sum(x for x in trades[:3] if x > 0)
    s.pop("curve", None)
    s.update(
        {
            "t_stat": round(t_stat(daily), 3),
            "pnl_without_top3": round(sum(daily) - sum(trades[:3]), 2) if trades else 0.0,
            "top3_share_of_gross_pct": round(top3 / gross_profit * 100, 1)
            if gross_profit > 0
            else None,
            "daily": {r["day"]: round(float(r["book"]["pnl"]), 2) for r in rows},
        }
    )
    return s


def versus(a: dict, b: dict) -> float:
    """a's P&L minus b's over the days both have."""
    common = set(a["daily"]) & set(b["daily"])
    return round(sum(a["daily"][d] for d in common) - sum(b["daily"][d] for d in common), 2)


def validation_bar(n_validated: int) -> float:
    """sqrt(2 ln N): about the largest t-statistic N useless variants would show by luck."""
    return max(1.0, math.sqrt(2 * math.log(max(n_validated, 1)))) if n_validated > 1 else 1.0


def gate(stage: str, s: dict, baseline: dict, n_validated: int = 1) -> tuple[bool, list[str]]:
    """The screen (TUNE) or validation (VALIDATE) gate. Returns (passed, reasons)."""
    why = []
    if s["pnl_after_fees"] <= 0:
        why.append(f"P&L after costs {s['pnl_after_fees']:+,.0f}")
    if s["trades"] < MIN_TRADES_GATE:
        why.append(f"{s['trades']} trades (fewer than {MIN_TRADES_GATE})")
    edge = versus(s, baseline)
    if edge <= 0:
        why.append(f"no better than the frozen rule bot ({edge:+,.0f})")
    if stage == "validate":
        if s["worst_day"] < WORST_DAY:
            why.append(f"worst day {s['worst_day']:+,.0f} (below -3%)")
        bar = validation_bar(n_validated)
        if s["t_stat"] < bar:
            why.append(
                f"t-statistic {s['t_stat']:.2f} below the bar {bar:.2f} for {n_validated} "
                "variants tried"
            )
    return (not why), (
        why
        or [
            f"passed: {s['pnl_after_fees']:+,.0f} on {s['trades']} trades, "
            f"{edge:+,.0f} better than the frozen rule bot, t {s['t_stat']:.2f}"
        ]
    )
