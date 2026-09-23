"""Benchmark and regime series.

Benchmark: ASX 200 total return. On yfinance there is no accumulation index, so the proxy is
STW.AX adjusted close (dividends reinvested) from its 2008 listing, spliced onto the ^AXJO
price index before that (price-only, understates the benchmark). The splice is reported.
Regime: ^AXJO price index vs its 200-day simple moving average.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from asxbot.data.store import PriceStore


@dataclass
class Benchmark:
    total_return: pd.Series  # index level, starts at 1.0
    index_close: pd.Series  # price index close
    index_open: pd.Series  # price index open (for gap-vs-index on the event day)
    note: str


def build_benchmark(
    store: PriceStore, start: str, benchmark_ticker: str, index_ticker: str
) -> Benchmark:
    frames = store.get_many([benchmark_ticker, index_ticker], start)
    etf = frames[benchmark_ticker]["close"].dropna()
    idx = frames[index_ticker]
    idx_close = idx["close"].dropna()
    if etf.empty:
        raise RuntimeError(f"no benchmark data for {benchmark_ticker}")
    etf_start = etf.index.min()
    pre = idx_close[idx_close.index < etf_start]
    if len(pre):
        # scale the price index so it meets the ETF on the ETF's first day
        scale = etf.iloc[0] / idx_close.asof(etf_start)
        spliced = pd.concat([pre * scale, etf])
        note = (
            f"{benchmark_ticker} adjusted close from {etf_start.date()} (total-return proxy); "
            f"{index_ticker} PRICE index before that (understates total return)"
        )
    else:
        spliced = etf
        note = f"{benchmark_ticker} adjusted close (total-return proxy)"
    tr = spliced / spliced.iloc[0]
    tr.name = "benchmark_tr"
    return Benchmark(
        total_return=tr, index_close=idx_close, index_open=idx["open"].dropna(), note=note
    )


def regime_on(index_close: pd.Series, sma_days: int) -> pd.Series:
    """True when the index closed above its SMA on the previous session (no look-ahead)."""
    sma = index_close.rolling(sma_days, min_periods=sma_days).mean()
    return (index_close > sma).shift(1).fillna(False).astype(bool)
