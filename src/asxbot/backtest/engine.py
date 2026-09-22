"""Portfolio simulator for event strategies. Daily bars, long only, whole shares.

Rules (frozen 2026-09-22):
  entry      open of the session after confirmation (main) or the confirmation session's open
             (upper bound), sized at equity / max_positions, adverse slippage + brokerage.
  priority   when more events than free slots: highest vol_mult first.
  time exit  sell at the open `hold_days` sessions after entry.
  stop       close < entry_fill * (1 - stop_pct) -> sell at next open.
  equity     cash + sum(qty * close), marked daily.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from asxbot.backtest.costs import CostModel
from asxbot.backtest.signals import Panels


@dataclass
class Position:
    ticker: str
    qty: int
    entry_date: pd.Timestamp
    entry_px: float  # fill price incl. slippage
    entry_value: float
    entry_cost: float
    stop_px: float
    exit_due: int  # integer session position at which to sell at open
    meta: dict = field(default_factory=dict)
    flagged_stop: bool = False


@dataclass
class Result:
    name: str
    trades: pd.DataFrame
    equity: pd.Series
    exposure: pd.Series  # fraction of equity invested, daily
    label: str = ""
    notes: list[str] = field(default_factory=list)


def simulate(
    p: Panels,
    events: pd.DataFrame,
    costs: CostModel,
    starting_capital: float,
    max_positions: int,
    hold_days: int,
    stop_pct: float,
    entry_lag: int = 1,
    name: str = "strategy",
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    min_order_aud: float = 500.0,
) -> Result:
    dates = p.dates
    if start is not None or end is not None:
        mask = np.ones(len(dates), dtype=bool)
        if start is not None:
            mask &= dates >= pd.Timestamp(start)
        if end is not None:
            mask &= dates <= pd.Timestamp(end)
    else:
        mask = np.ones(len(dates), dtype=bool)
    pos_of = {d: i for i, d in enumerate(dates)}
    open_np = p.open.to_numpy()
    close_np = p.close.to_numpy()
    adv_np = p.adv_dollar.to_numpy()
    elig_np = p.eligible.to_numpy()
    col = {t: j for j, t in enumerate(p.close.columns)}

    # events keyed by the session position on which entry happens
    pending: dict[int, list[dict]] = {}
    for ev in events.to_dict("records"):
        d = pd.Timestamp(ev["date"])
        if d not in pos_of:
            continue
        pending.setdefault(pos_of[d] + entry_lag, []).append(ev)

    cash = float(starting_capital)
    positions: dict[str, Position] = {}
    trades: list[dict] = []
    equity = np.full(len(dates), np.nan)
    exposure = np.full(len(dates), np.nan)

    def close_position(pos: Position, i: int, reason: str) -> None:
        nonlocal cash
        j = col[pos.ticker]
        px = open_np[i, j]
        if not np.isfinite(px):
            # no open today (halt / missing): try close, else carry
            px = close_np[i, j]
            if not np.isfinite(px):
                return
        value = px * pos.qty
        fill = costs.sell_price(px, value, adv_np[i, j])
        proceeds = fill * pos.qty
        fee = costs.brokerage(proceeds)
        cash += proceeds - fee
        pnl = proceeds - fee - pos.entry_value - pos.entry_cost
        trades.append(
            {
                "ticker": pos.ticker,
                "entry_date": pos.entry_date,
                "exit_date": dates[i],
                "qty": pos.qty,
                "entry_px": pos.entry_px,
                "exit_px": fill,
                "entry_value": pos.entry_value,
                "costs": pos.entry_cost
                + fee
                + (pos.entry_px - pos.meta.get("raw_entry", pos.entry_px)) * pos.qty
                + (px - fill) * pos.qty,
                "pnl": pnl,
                "ret_pct": pnl / (pos.entry_value + pos.entry_cost) * 100,
                "hold_sessions": i - pos_of[pos.entry_date],
                "reason": reason,
                **{k: v for k, v in pos.meta.items() if k != "raw_entry"},
            }
        )
        del positions[pos.ticker]

    for i in range(len(dates)):
        if not mask[i]:
            continue
        # 1. exits at the open
        for t in list(positions):
            pos = positions[t]
            if pos.flagged_stop:
                close_position(pos, i, "stop")
            elif i >= pos.exit_due:
                close_position(pos, i, "time")
        # 2. entries at the open
        if i in pending and len(positions) < max_positions:
            mark = cash + sum(
                positions[t].qty * _last_valid(close_np, i - 1, col[t]) for t in positions
            )
            size = mark / max_positions
            for ev in sorted(pending[i], key=lambda e: -e["vol_mult"]):
                if len(positions) >= max_positions:
                    break
                t = ev["ticker"]
                if t in positions or t not in col:
                    continue
                j = col[t]
                px = open_np[i, j]
                if not np.isfinite(px) or px <= 0 or not elig_np[i, j]:
                    continue
                adv = adv_np[i, j]
                fill = costs.buy_price(px, size, adv)
                qty = int(math.floor(min(size, cash) / fill))
                if qty <= 0 or fill * qty < min_order_aud:
                    continue  # below the minimum marketable parcel: no trade
                value = fill * qty
                fee = costs.brokerage(value)
                if value + fee > cash:
                    qty -= 1
                    if qty <= 0:
                        continue
                    value = fill * qty
                    fee = costs.brokerage(value)
                cash -= value + fee
                positions[t] = Position(
                    ticker=t,
                    qty=qty,
                    entry_date=dates[i],
                    entry_px=fill,
                    entry_value=value,
                    entry_cost=fee,
                    stop_px=fill * (1 - stop_pct / 100),
                    exit_due=i + hold_days,
                    meta={
                        "raw_entry": px,
                        "event_date": ev["date"],
                        "gap_rel": ev.get("gap_rel"),
                        "vol_mult": ev.get("vol_mult"),
                        "ann_type": ev.get("ann_type"),
                        "headline": ev.get("headline"),
                    },
                )
        # 3. mark to market at the close; flag stops
        invested = 0.0
        for t, pos in positions.items():
            c = _last_valid(close_np, i, col[t])
            invested += c * pos.qty
            if np.isfinite(close_np[i, col[t]]) and close_np[i, col[t]] < pos.stop_px:
                pos.flagged_stop = True
        equity[i] = cash + invested
        exposure[i] = invested / equity[i] if equity[i] > 0 else 0.0

    # close anything still open at the last session, at its close
    last = int(np.flatnonzero(mask)[-1]) if mask.any() else len(dates) - 1
    for t in list(positions):
        pos = positions[t]
        j = col[t]
        px = _last_valid(close_np, last, j)
        value = px * pos.qty
        fill = costs.sell_price(px, value, adv_np[last, j])
        fee = costs.brokerage(fill * pos.qty)
        pnl = fill * pos.qty - fee - pos.entry_value - pos.entry_cost
        trades.append(
            {
                "ticker": t, "entry_date": pos.entry_date, "exit_date": dates[last],
                "qty": pos.qty, "entry_px": pos.entry_px, "exit_px": fill,
                "entry_value": pos.entry_value, "costs": pos.entry_cost + fee,
                "pnl": pnl, "ret_pct": pnl / (pos.entry_value + pos.entry_cost) * 100,
                "hold_sessions": last - pos_of[pos.entry_date], "reason": "end",
                **{k: v for k, v in pos.meta.items() if k != "raw_entry"},
            }
        )  # fmt: skip
    eq = pd.Series(equity, index=dates, name="equity").dropna()
    ex = pd.Series(exposure, index=dates, name="exposure").dropna()
    tdf = pd.DataFrame(trades)
    return Result(name=name, trades=tdf, equity=eq, exposure=ex)


def _last_valid(arr: np.ndarray, i: int, j: int) -> float:
    k = i
    while k >= 0:
        v = arr[k, j]
        if np.isfinite(v):
            return float(v)
        k -= 1
    return float("nan")
