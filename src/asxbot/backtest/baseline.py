"""Baseline (frozen 2026-09-22): cross-sectional momentum, 12-1 month lookback, top 4 equal
weight, monthly rebalance at the first session's open, held only while the ASX 200 closed
above its 200-day SMA on the previous session; otherwise cash. Same capital, position count
and costs as the strategy. Eligibility uses the same turnover floor.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from asxbot.backtest.costs import CostModel
from asxbot.backtest.engine import Result
from asxbot.backtest.signals import Panels
from asxbot.data.benchmark import regime_on


def run_momentum(
    p: Panels,
    costs: CostModel,
    starting_capital: float,
    top_n: int,
    lookback_months: int,
    skip_months: int,
    regime_sma_days: int,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    min_order_aud: float = 500.0,
) -> Result:
    dates = p.dates
    close = p.close
    lb = int(lookback_months * 21)
    sk = int(skip_months * 21)
    # momentum score at t uses closes up to t-1 (rebalance happens at t's open)
    score = (close.shift(1 + sk) / close.shift(1 + lb) - 1).where(p.eligible)
    regime = regime_on(p.index_close.dropna(), regime_sma_days).reindex(dates).fillna(False)
    month = pd.Series(dates.month, index=dates)
    first_of_month = month != month.shift(1)

    open_np = p.open.to_numpy()
    close_np = p.close.to_numpy()
    adv_np = p.adv_dollar.to_numpy()
    col = {t: j for j, t in enumerate(close.columns)}
    cash = float(starting_capital)
    holdings: dict[str, dict] = {}
    trades: list[dict] = []
    equity = np.full(len(dates), np.nan)
    exposure = np.full(len(dates), np.nan)
    lo = 0 if start is None else int(dates.searchsorted(pd.Timestamp(start)))
    hi = len(dates) - 1 if end is None else int(dates.searchsorted(pd.Timestamp(end), "right") - 1)

    def sell(t: str, i: int, reason: str) -> None:
        nonlocal cash
        h = holdings.pop(t)
        j = col[t]
        px = open_np[i, j] if np.isfinite(open_np[i, j]) else close_np[i, j]
        if not np.isfinite(px):
            holdings[t] = h
            return
        fill = costs.sell_price(px, px * h["qty"], adv_np[i, j])
        fee = costs.brokerage(fill * h["qty"])
        cash += fill * h["qty"] - fee
        pnl = fill * h["qty"] - fee - h["value"] - h["fee"]
        trades.append(
            {
                "ticker": t, "entry_date": h["date"], "exit_date": dates[i], "qty": h["qty"],
                "entry_px": h["px"], "exit_px": fill, "entry_value": h["value"],
                "costs": h["fee"] + fee, "pnl": pnl,
                "ret_pct": pnl / (h["value"] + h["fee"]) * 100,
                "hold_sessions": i - h["i"], "reason": reason,
            }
        )  # fmt: skip

    for i in range(lo, hi + 1):
        d = dates[i]
        if first_of_month.iloc[i]:
            if not regime.iloc[i]:
                for t in list(holdings):
                    sell(t, i, "regime_off")
            else:
                s = score.iloc[i].dropna()
                s = s[np.isfinite(open_np[i, [col[t] for t in s.index]])]
                target = list(s.nlargest(top_n).index) if len(s) else []
                for t in list(holdings):
                    if t not in target:
                        sell(t, i, "rebalance")
                mark = cash + sum(h["qty"] * close_np[i - 1, col[t]] for t, h in holdings.items())
                size = mark / top_n
                for t in target:
                    if t in holdings:
                        continue
                    j = col[t]
                    px = open_np[i, j]
                    fill = costs.buy_price(px, size, adv_np[i, j])
                    qty = int(math.floor(min(size, cash) / fill))
                    if qty <= 0 or fill * qty < min_order_aud:
                        continue
                    value = fill * qty
                    fee = costs.brokerage(value)
                    if value + fee > cash:
                        qty -= 1
                        if qty <= 0:
                            continue
                        value, fee = fill * qty, costs.brokerage(fill * qty)
                    cash -= value + fee
                    holdings[t] = {
                        "qty": qty,
                        "px": fill,
                        "value": value,
                        "fee": fee,
                        "date": d,
                        "i": i,
                    }
        invested = 0.0
        for t, h in holdings.items():
            c = close_np[i, col[t]]
            if not np.isfinite(c):
                k = i
                while k > 0 and not np.isfinite(close_np[k, col[t]]):
                    k -= 1
                c = close_np[k, col[t]]
            invested += c * h["qty"]
        equity[i] = cash + invested
        exposure[i] = invested / equity[i] if equity[i] > 0 else 0.0
    for t in list(holdings):
        sell(t, hi, "end")
    return Result(
        name="momentum_baseline",
        trades=pd.DataFrame(trades),
        equity=pd.Series(equity, index=dates).dropna(),
        exposure=pd.Series(exposure, index=dates).dropna(),
    )
