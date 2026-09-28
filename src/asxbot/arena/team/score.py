"""The team's book, scored the way every arena book is scored (scoreboard.py: P&L after every
cost, green and red days from the daily marks, the worst peak-to-trough drop, round trips won
after all costs, fees), and set side by side with the frozen books for the evening report.

Code writes all of it. The block is added to the report AFTER the frozen agent has written its
part (arena/cli.py cmd_report), so no frozen agent is ever shown the team's results.
"""

from __future__ import annotations

from datetime import date
from html import escape

from asxbot.arena.scoreboard import Score
from asxbot.arena.team.runner import team_root
from asxbot.arena.team.session import ACCOUNT, Book
from asxbot.lab.tsim.broker import Account


def load_account(cfg) -> Account | None:
    st = Book(team_root(cfg)).load()
    return Account.from_dict(st["account"]) if st and st.get("account") else None


def team_score(cfg) -> Score | None:
    acct = load_account(cfg)
    if acct is None:
        return None
    curve = [acct.start_cash] + [float(m["equity"]) for m in acct.marks]
    rets = [(b / a - 1) * 100 if a else 0.0 for a, b in zip(curve[:-1], curve[1:], strict=True)]
    peak, dd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        dd = max(dd, (peak - v) / peak * 100 if peak > 0 else 0.0)
    wins = sum(1 for t in acct.trades if t.net > 0)
    equity = curve[-1]
    orders = list(acct.orders.values())
    return Score(
        account=ACCOUNT,
        playbook="asx_team",
        kind="team",
        level=1,
        starting=round(acct.start_cash, 2),
        equity=round(equity, 2),
        pnl=round(equity - acct.start_cash, 2),
        pnl_pct=round((equity / acct.start_cash - 1) * 100, 2) if acct.start_cash else 0.0,
        days=len(rets),
        green_days=sum(1 for r in rets if r > 0),
        red_days=sum(1 for r in rets if r < 0),
        flat_days=sum(1 for r in rets if r == 0),
        avg_day_pct=round(sum(rets) / len(rets), 3) if rets else 0.0,
        worst_day_pct=round(min(rets), 2) if rets else 0.0,
        best_day_pct=round(max(rets), 2) if rets else 0.0,
        max_drawdown_pct=round(dd, 2),
        trades=len(acct.trades),
        wins=wins,
        win_rate_pct=round(wins / len(acct.trades) * 100, 1) if acct.trades else 0.0,
        open_positions=len(acct.positions),
        fees_paid=round(acct.fees, 2),
        borrow_paid=round(acct.borrow, 2),
        orders_placed=len(orders),
        orders_filled=sum(1 for o in orders if o.status == "filled"),
        orders_expired=sum(1 for o in orders if o.status == "expired"),
        pending_fills=sum(1 for o in orders if o.working),
    )


def team_day(cfg, day: date) -> dict | None:
    return Book(team_root(cfg)).read_day(day.isoformat())


def _row(name: str, today, total, g, r, trades, won, dd, fees) -> str:
    t = "-" if today is None else f"{today:+,.0f}"
    return (
        f"{name[:18]:18s} {t:>7s} {total:>+8,.0f} {f'{g}/{r}':>5s} "
        f"{f'{won}/{trades}':>6s} {dd:>5.1f} {fees:>6,.0f}"
    )


def side_by_side(cfg, facts: dict, day: date) -> list[str]:
    """One table: the team and every frozen book, the same measures (today and total after
    every cost, green/red days, round trips won after costs, worst drop %, fees)."""
    s = team_score(cfg)
    d = team_day(cfg, day)
    head = f"{'book':18s} {'today':>7s} {'total':>8s} {'G/R':>5s} {'won':>6s} {'drop':>5s} {'fees':>6s}"
    rows = [head]
    if s is not None:
        rows.append(
            _row(
                "AI team",
                d["pnl"] if d else None,
                s.pnl,
                s.green_days,
                s.red_days,
                s.trades,
                s.wins,
                s.max_drawdown_pct,
                s.fees_paid + s.borrow_paid,
            )
        )
    for row in facts.get("scorecard") or []:
        short = (
            "day trader"
            if "daytrader" in row["key"]
            else "announcements v2"
            if "v2" in row["key"]
            else row["key"]
        )
        for a in row["accounts"]:
            who = f"{short} {'agent' if a['kind'] == 'agent' else 'rule bot'}"
            rows.append(
                _row(
                    who,
                    a["today_after_fees"],
                    a["total_after_fees"],
                    a["green_days"],
                    a["red_days"],
                    a["trades"],
                    a["wins_after_fees"],
                    a["max_drawdown_pct"],
                    a["fees_total"],
                )
            )
    return rows


def report_block(cfg, facts: dict, day: date) -> str:
    """The team's part of the evening report (HTML)."""
    from asxbot.announcements.live import is_trading_day
    from asxbot.arena.team.limits import conf_of

    conf = conf_of(cfg)
    if not conf.get("enabled"):
        return ""
    start = date.fromisoformat(str(conf.get("start"))) if conf.get("start") else None
    if start and day < start:
        return ""
    d = team_day(cfg, day)
    head = "<b>AI team</b> (paper: its own A$20,000 book, beside the frozen test; code wrote this)"
    lines = [head]
    if not is_trading_day(day):
        return ""
    if d is None:
        lines.append(
            "- It did not trade today: no day was recorded for its book (asxbot.log says why)."
        )
    else:
        won = sum(1 for t in d["trades"] if t["net"] > 0)
        u = d.get("usage") or {}
        parts = ("input", "cache_write", "cache_read", "output")
        opus = int(sum(u.get(f"opus:{k}", 0) for k in parts))
        sonnet = int(sum(u.get(f"sonnet:{k}", 0) for k in parts))
        tok = f"Opus {opus:,} / Sonnet {sonnet:,} tokens"
        lines.append(
            f"- Today {d['pnl']:+,.2f} after all costs (fees {d['fees_today']:,.2f}); "
            f"{len(d['trades'])} round trip{'s' if len(d['trades']) != 1 else ''} closed, {won} "
            f"won after costs; {d['orders_placed']} orders placed; woken {d['wakes']} times, "
            f"{d['calls']} model calls ({tok})."
        )
        for t in d["trades"][:8]:
            lines.append(f"    {escape(t['code'])} {t['direction']}: {t['net']:+,.2f} after costs")
        if d.get("carried"):
            lines.append(
                "- Held overnight: "
                + ", ".join(f"{escape(c)} {v['qty']:+d}" for c, v in d["carried"].items())
            )
        if d.get("refused_by_limits"):
            lines.append(
                f"- The hard limits refused {len(d['refused_by_limits'])}: "
                + "; ".join(escape(x["why"][:90]) for x in d["refused_by_limits"][:3])
            )
        if d.get("silenced"):
            lines.append(f"- Not asked at times: {escape(d['silenced'][:160])}")
        if d.get("loss_hit"):
            lines.append(
                "- The daily loss limit was hit: no new positions for the rest of the day."
            )
        if d.get("killed"):
            lines.append(f"- KILL SWITCH: {escape(d['killed'])}")
        les = lesson(cfg, day)
        if les:
            lines.append(f"- Its lesson: {escape(les[:240])}")
    lines.append("<pre>" + "\n".join(escape(x) for x in side_by_side(cfg, facts, day)) + "</pre>")
    from asxbot.arena.team.calibrate import report_lines

    lines += [escape(x) for x in report_lines(cfg, day)]
    return "\n".join(lines)


def lesson(cfg, day: date) -> str:
    from asxbot.lab.tsim import live as tlive

    e = tlive.day_entry(team_root(cfg), day.isoformat()) or {}
    les = e.get("lessons") or []
    return str(les[0]) if les else ""


__all__ = ["load_account", "report_block", "side_by_side", "team_day", "team_score"]
