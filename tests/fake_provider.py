"""Deterministic in-memory provider for tests. No network."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import numpy as np
import pandas as pd

from asxbot.data.base import PriceProvider


def synthetic_bars(
    n: int = 300, seed: int = 0, start: str = "2020-01-01", price: float = 10.0
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n, name="date")
    rets = rng.normal(0.0002, 0.02, n)
    close = price * np.cumprod(1 + rets)
    open_ = close * (1 + rng.normal(0, 0.005, n))
    high = np.maximum(open_, close) * (1 + abs(rng.normal(0, 0.005, n)))
    low = np.minimum(open_, close) * (1 - abs(rng.normal(0, 0.005, n)))
    vol = rng.integers(50_000, 200_000, n).astype(float)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx
    )


class FakeProvider(PriceProvider):
    name = "fake"
    label = "synthetic test data"

    def __init__(self, frames: dict[str, pd.DataFrame] | None = None):
        self.frames = frames or {}
        self.calls: list[list[str]] = []

    def daily(self, ticker: str, start: date | str, end: date | str | None = None) -> pd.DataFrame:
        self.calls.append([ticker])
        return self.frames.get(ticker, pd.DataFrame())

    def daily_many(
        self, tickers: Iterable[str], start: date | str, end: date | str | None = None
    ) -> dict[str, pd.DataFrame]:
        tickers = list(tickers)
        self.calls.append(tickers)
        return {t: self.frames.get(t, pd.DataFrame()) for t in tickers}
