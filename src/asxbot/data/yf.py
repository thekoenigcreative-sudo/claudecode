"""yfinance adapter. Plumbing only: no delisted stocks, no point-in-time membership.

Every frame from here carries the label in `YFinanceProvider.label`; reports must print it.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import date

import pandas as pd

from asxbot.data.base import COLUMNS, PriceProvider
from asxbot.log import get_logger

log = get_logger("asxbot.data.yf")

LABEL = "plumbing test - not a go/no-go"
INDEX_PREFIX = "^"


def yf_symbol(ticker: str) -> str:
    if ticker.startswith(INDEX_PREFIX) or ticker.endswith(".AX"):
        return ticker
    return f"{ticker}.AX"


class YFinanceProvider(PriceProvider):
    name = "yfinance"
    label = LABEL
    survivorship_safe = False

    def __init__(self, batch_size: int = 150, pause_s: float = 1.0):
        self.batch_size = batch_size
        self.pause_s = pause_s

    def daily(self, ticker: str, start: date | str, end: date | str | None = None) -> pd.DataFrame:
        return self.daily_many([ticker], start, end).get(ticker, pd.DataFrame(columns=COLUMNS))

    def daily_many(
        self, tickers: Iterable[str], start: date | str, end: date | str | None = None
    ) -> dict[str, pd.DataFrame]:
        import yfinance as yf

        tickers = list(dict.fromkeys(tickers))
        out: dict[str, pd.DataFrame] = {}
        for i in range(0, len(tickers), self.batch_size):
            chunk = tickers[i : i + self.batch_size]
            symbols = [yf_symbol(t) for t in chunk]
            raw = yf.download(
                symbols,
                start=str(start),
                end=str(end) if end else None,
                auto_adjust=True,
                group_by="ticker",
                progress=False,
                threads=True,
            )
            for t, s in zip(chunk, symbols, strict=True):
                out[t] = self._extract(raw, s, len(symbols) == 1)
            log.info("yfinance batch %d-%d of %d", i + 1, i + len(chunk), len(tickers))
            if i + self.batch_size < len(tickers):
                time.sleep(self.pause_s)
        return out

    @staticmethod
    def _extract(raw: pd.DataFrame, symbol: str, single: bool) -> pd.DataFrame:
        if raw is None or raw.empty:
            return pd.DataFrame(columns=COLUMNS)
        try:
            df = raw if single and not isinstance(raw.columns, pd.MultiIndex) else raw[symbol]
        except KeyError:
            return pd.DataFrame(columns=COLUMNS)
        df = df.dropna(how="all")
        if df.empty:
            return pd.DataFrame(columns=COLUMNS)
        df = df.rename(columns=str.lower)
        return df[[c for c in COLUMNS if c in df.columns]]
