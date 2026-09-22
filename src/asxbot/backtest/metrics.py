"""Performance metrics on an equity curve and a trade list."""

from __future__ import annotations

import numpy as np
import pandas as pd


def cagr(equity: pd.Series) -> float:
    if len(equity) < 2 or equity.iloc[0] <= 0:
        return float("nan")
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    if years <= 0:
        return float("nan")
    return (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return float("nan")
    peak = equity.cummax()
    return float((equity / peak - 1).min())


def yearly_returns(equity: pd.Series) -> pd.Series:
    if equity.empty:
        return pd.Series(dtype=float)
    ye = equity.groupby(equity.index.year).last()
    first = equity.iloc[0]
    prev = ye.shift(1)
    prev.iloc[0] = first
    return (ye / prev - 1) * 100


def summarise(
    equity: pd.Series, trades: pd.DataFrame, exposure: pd.Series, small_sample: int = 30
) -> dict:
    n = len(trades)
    wins = int((trades["pnl"] > 0).sum()) if n else 0
    traded_value = float(trades["entry_value"].sum() * 2) if n else 0.0
    years = (equity.index[-1] - equity.index[0]).days / 365.25 if len(equity) > 1 else float("nan")
    avg_eq = float(equity.mean()) if len(equity) else float("nan")
    return {
        "trades": n,
        "win_rate_pct": (wins / n * 100) if n else float("nan"),
        "avg_trade_pct": float(trades["ret_pct"].mean()) if n else float("nan"),
        "median_trade_pct": float(trades["ret_pct"].median()) if n else float("nan"),
        "avg_pnl_aud": float(trades["pnl"].mean()) if n else float("nan"),
        "total_pnl_aud": float(trades["pnl"].sum()) if n else 0.0,
        "total_costs_aud": float(trades["costs"].sum()) if n else 0.0,
        "cagr_pct": cagr(equity) * 100,
        "max_dd_pct": max_drawdown(equity) * 100,
        "turnover_x_per_year": (traded_value / avg_eq / years) if n and years > 0 else 0.0,
        "exposure_pct": float(exposure.mean() * 100) if len(exposure) else float("nan"),
        "final_equity": float(equity.iloc[-1]) if len(equity) else float("nan"),
        "small_sample": n < small_sample,
        "years": years,
    }


def split_is_oos(
    trades: pd.DataFrame, oos_start: pd.Timestamp
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if trades.empty:
        return trades, trades
    is_ = trades[trades["entry_date"] < oos_start]
    oos = trades[trades["entry_date"] >= oos_start]
    return is_, oos


def trade_stats(trades: pd.DataFrame) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "win_rate_pct": np.nan, "avg_trade_pct": np.nan, "total_pnl_aud": 0.0}
    return {
        "trades": n,
        "win_rate_pct": float((trades["pnl"] > 0).mean() * 100),
        "avg_trade_pct": float(trades["ret_pct"].mean()),
        "total_pnl_aud": float(trades["pnl"].sum()),
    }
