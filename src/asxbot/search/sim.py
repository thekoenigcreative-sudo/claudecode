"""Fills and exits on one stock's minute bars, with the replay's discipline:

  * a decision made on bar i (its close) is placed after that bar closed; the earliest bar it
    can fill on is i + LATENCY (the replay decides 20 s past a minute on the bars before it
    and fills on the first bar that starts after the decision: two bars after the signal bar);
  * a RESTING order (a stop-entry above the range, a stop below it) placed before a bar
    starts can fill in that bar: at its level, or at the bar's open if the bar opened through
    it (a gap fills worse, never better);
  * a fill is at most MAX_BAR_SHARE of that bar's volume; the rest keeps working on the next
    bars (at their close) until it is done or its time is up;
  * a stop cannot fire in the bar the position opened in (LEARNINGS #8);
  * the closing auction takes at most AUCTION_SHARE of its own volume; whatever is left is
    STUCK and sold at the next session's first traded minute (or, with no next session in the
    data, at the close less 2%) - counted and reported, never hidden;
  * every fill pays the spread and impact (costs.Costs), every order its brokerage.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from asxbot.search.costs import AUCTION_SHARE, MAX_BAR_SHARE, Costs, round_to_tick, tick
from asxbot.search.data import AUCTION_FROM, CONT_END, DayPanel

LATENCY = 2
STUCK_PENALTY = 0.02


@dataclass
class Fill:
    qty: int
    px: float  # the printed price paid/received, before costs (volume-weighted)
    first_i: int
    last_i: int


@dataclass
class Trade:
    code: str
    side: str  # "buy" (long) or "short"
    day: str
    qty: int
    entry_raw: float
    entry: float  # after spread and impact
    stop0: float | None
    exit_raw: float = math.nan
    exit: float = math.nan
    entry_i: int = -1
    exit_i: int = -1
    exit_why: str = ""
    fees: float = 0.0
    stuck: int = 0
    exit_day: str = ""
    tags: dict = field(default_factory=dict)

    @property
    def sign(self) -> int:
        return 1 if self.side == "buy" else -1

    @property
    def risk(self) -> float | None:
        if self.stop0 is None:
            return None
        return abs(self.entry_raw - self.stop0) * self.qty

    @property
    def gross(self) -> float:
        return self.sign * (self.exit - self.entry) * self.qty

    @property
    def net(self) -> float:
        return self.gross - self.fees

    def record(self) -> dict:
        risk = self.risk
        return {
            "ticker": self.code,
            "side": self.side,
            "qty": int(self.qty),
            "entry": round(float(self.entry_raw), 4),
            "exit": round(float(self.exit_raw), 4),
            "stop": None if self.stop0 is None else round(float(self.stop0), 4),
            "risk": None if risk is None else round(float(risk), 2),
            "day": self.day,
            "exit_day": self.exit_day or self.day,
            "entry_i": self.entry_i,
            "exit_i": self.exit_i,
            "exit_why": self.exit_why,
            "gross": round(float(self.gross), 2),
            "fees": round(float(self.fees), 2),
            "net": round(float(self.net), 2),
            "r": None if not risk else round(float(self.net / risk), 3),
            "stuck": int(self.stuck),
            **{k: v for k, v in self.tags.items() if isinstance(v, (int, float, str, bool))},
        }


def work_fill(p: DayPanel, r: int, first_i: int, qty: int, last_i: int,
              limit: float | None = None, side: str = "buy",
              first_px: float | None = None) -> Fill | None:  # fmt: skip
    """Fill up to `qty` from bar first_i to last_i: the first bar at `first_px` (a resting
    order's level or the gap-through open) or its close, later bars at their close; each bar
    at most MAX_BAR_SHARE of its volume; a bar whose price is past `limit` is skipped."""
    left, got, cost, fi, li = qty, 0, 0.0, -1, -1
    last_i = min(last_i, CONT_END)
    for i in range(first_i, last_i + 1):
        v = p.v[r, i]
        c = p.c[r, i]
        if not (v > 0) or not np.isfinite(c):
            continue
        px = float(first_px) if (fi < 0 and first_px is not None and i == first_i) else float(c)
        if limit is not None and ((side == "buy" and px > limit) or (side != "buy" and px < limit)):
            continue
        take = min(left, int(v * MAX_BAR_SHARE))
        if take <= 0:
            continue
        got += take
        cost += take * px
        left -= take
        fi = i if fi < 0 else fi
        li = i
        if left <= 0:
            break
    if got <= 0:
        return None
    return Fill(got, cost / got, fi, li)


def stop_entry(p: DayPanel, r: int, level: float, side: str, from_i: int, until_i: int,
               qty: int, slack: float = 0.01, good_for: int = 5) -> Fill | None:  # fmt: skip
    """A resting stop-limit entry at `level` (a buy stop above, a sell stop below), active
    from bar from_i. Triggers on the first bar that trades through it; fills from that bar
    for `good_for` bars, never beyond level x (1 +/- slack)."""
    until_i = min(until_i, CONT_END)
    lim = level * (1 + slack) if side == "buy" else level * (1 - slack)
    for i in range(from_i, until_i + 1):
        if not (p.v[r, i] > 0):
            continue
        hi, lo, op = float(p.h[r, i]), float(p.lo[r, i]), float(p.o[r, i])
        if side == "buy" and hi >= level:
            px = max(level, op)
        elif side != "buy" and lo <= level:
            px = min(level, op)
        else:
            continue
        return work_fill(p, r, i, qty, i + good_for, lim, side, first_px=px)
    return None


def enter(tr_side: str, fill: Fill, costs: Costs, turnover, vol20) -> tuple[float, float]:
    """(price after costs, brokerage) for an entry fill."""
    value = fill.qty * fill.px
    frac = costs.side_frac(fill.px, value, turnover, vol20)
    px = fill.px * (1 + frac) if tr_side == "buy" else fill.px * (1 - frac)
    return px, costs.broker.fee(value)


def manage_and_exit(p: DayPanel, r: int, t: Trade, costs: Costs, turnover, vol20, *,
                    stop: float | None, exit_at: int | None = None,
                    trail_r: float | None = None, trail_after_r: float = 0.0,
                    breakeven_r: float | None = None, target_r: float | None = None,
                    next_open: float | None = None, hold_overnight: bool = False
                    ) -> Trade:  # fmt: skip
    """Run the position bar by bar from the bar after it filled: stop (level, or the open if
    the bar gapped through), optional breakeven / trailing stop / target (all measured in R of
    the stop it opened with), and a time exit at bar `exit_at` (None: the closing auction).
    With hold_overnight the position is left open (the caller marks and carries it)."""
    sign = t.sign
    R = abs(t.entry_raw - t.stop0) if t.stop0 is not None else None
    best = t.entry_raw
    cur_stop = stop
    left = t.qty
    proceeds, done_qty, last_i, why = 0.0, 0, t.entry_i, ""
    end = CONT_END if exit_at is None else min(exit_at, CONT_END)
    i = t.entry_i + 1
    while i <= end and left > 0:
        if not (p.v[r, i] > 0):
            i += 1
            continue
        op, hi, lo, c = (float(p.o[r, i]), float(p.h[r, i]), float(p.lo[r, i]),
                         float(p.c[r, i]))  # fmt: skip
        hit_px = None
        if cur_stop is not None:
            if sign > 0 and lo <= cur_stop:
                hit_px = min(cur_stop, op)
            elif sign < 0 and hi >= cur_stop:
                hit_px = max(cur_stop, op)
        if hit_px is None and target_r and R:
            tgt = t.entry_raw + sign * target_r * R
            if (sign > 0 and hi >= tgt) or (sign < 0 and lo <= tgt):
                hit_px = max(tgt, op) if sign > 0 else min(tgt, op)
                why = "target"
        if hit_px is not None:
            f = work_fill(p, r, i, left, CONT_END, None, "sell", first_px=hit_px)
            why = why or "stop"
            if f is not None:
                proceeds += f.qty * f.px
                done_qty += f.qty
                left -= f.qty
                last_i = f.last_i
            break
        # the bar closed: move the stop for the next bar (never loosen)
        best = max(best, c) if sign > 0 else min(best, c)
        if R and cur_stop is not None:
            gain_r = sign * (best - t.entry_raw) / R
            new = cur_stop
            if breakeven_r is not None and gain_r >= breakeven_r:
                new = _tighter(sign, new, t.entry_raw)
            if trail_r is not None and gain_r >= trail_after_r:
                new = _tighter(sign, new, best - sign * trail_r * R)
            cur_stop = new
        i += 1
    if left > 0 and exit_at is not None and exit_at < CONT_END:
        f = work_fill(p, r, exit_at + LATENCY, left, CONT_END, None, "sell")
        if f is not None:
            proceeds += f.qty * f.px
            done_qty += f.qty
            left -= f.qty
            last_i = f.last_i
            why = why or "time"
    if left > 0 and hold_overnight:
        t.tags["open_qty"] = left
        t.tags["proceeds_so_far"] = proceeds
        t.tags["done_so_far"] = done_qty
        return t
    if left > 0:
        # the closing auction
        auc_v = float(p.v[r, AUCTION_FROM:].sum())
        auc_px = _auction_px(p, r)
        take = min(left, int(auc_v * AUCTION_SHARE)) if auc_px is not None else 0
        if take > 0:
            proceeds += take * auc_px
            done_qty += take
            left -= take
            last_i = AUCTION_FROM
            why = why or "close"
        if left > 0:
            last_px = _last_px(p, r)
            px = next_open if next_open else (last_px or t.entry_raw) * (1 - sign * STUCK_PENALTY)
            proceeds += left * px
            done_qty += left
            t.stuck = left
            why = (why + "+" if why else "") + "stuck"
            left = 0
    return close_out(t, proceeds, done_qty, last_i, why, costs, turnover, vol20)


def close_out(t: Trade, proceeds: float, qty: int, last_i: int, why: str, costs: Costs,
              turnover, vol20) -> Trade:  # fmt: skip
    raw = proceeds / qty if qty else t.entry_raw
    value = qty * raw
    frac = costs.side_frac(raw, value, turnover, vol20)
    t.exit_raw = raw
    t.exit = raw * (1 - frac) if t.sign > 0 else raw * (1 + frac)
    t.fees += costs.broker.fee(value)
    t.exit_i = last_i
    t.exit_why = why
    return t


def _tighter(sign: int, cur: float, new: float) -> float:
    return max(cur, new) if sign > 0 else min(cur, new)


def _auction_px(p: DayPanel, r: int) -> float | None:
    v = p.v[r, AUCTION_FROM:]
    if not (v > 0).any():
        return None
    j = AUCTION_FROM + int(np.where(v > 0)[0][-1])
    return float(p.c[r, j])


def _last_px(p: DayPanel, r: int) -> float | None:
    ok = np.where(p.v[r] > 0)[0]
    return float(p.c[r, ok[-1]]) if len(ok) else None


def close_px(p: DayPanel, r: int) -> float | None:
    """The day's close: the auction print, else the last traded minute."""
    return _auction_px(p, r) or _last_px(p, r)


def size_by_risk(equity: float, risk_pct: float, entry: float, stop: float, max_value: float,
                 turnover: float | None, max_adv_share: float = 0.05) -> int:  # fmt: skip
    per = abs(entry - stop)
    if per <= 0 or entry <= 0:
        return 0
    q = equity * risk_pct / 100.0 / per
    q = min(q, max_value / entry)
    if turnover and turnover > 0:
        q = min(q, max_adv_share * turnover / entry)
    return int(q)


def level_above(px: float) -> float:
    return round_to_tick(px + tick(px), up=True)


def level_below(px: float) -> float:
    return round_to_tick(px - tick(px), up=False)


__all__ = ["Fill", "LATENCY", "Trade", "close_out", "close_px", "enter", "level_above",
           "level_below", "manage_and_exit", "size_by_risk", "stop_entry", "work_fill"]  # fmt: skip
