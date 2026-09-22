"""Live quote provider behind a small interface. yfinance now (delayed ~20 min on the ASX,
plumbing only); IBKR real-time later behind the same interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from asxbot.log import get_logger

log = get_logger("asxbot.live.quotes")


@dataclass
class Quote:
    ticker: str
    last: float
    open: float
    prev_close: float
    volume_today: float
    as_of: datetime
    source: str
    delayed: bool = True


class QuoteProvider(ABC):
    name = "base"

    @abstractmethod
    def quote(self, ticker: str) -> Quote | None: ...

    @abstractmethod
    def index_quote(self) -> Quote | None: ...


class YFinanceQuotes(QuoteProvider):
    name = "yfinance"

    def __init__(self, index_ticker: str = "^AXJO"):
        self.index_ticker = index_ticker

    def _q(self, symbol: str, ticker: str) -> Quote | None:
        import yfinance as yf

        try:
            t = yf.Ticker(symbol)
            fi = t.fast_info
            last = float(fi["last_price"])
            prev = float(fi["previous_close"])
            op = float(fi.get("open") or last)
            vol = float(fi.get("last_volume") or 0.0)
        except Exception as e:  # noqa: BLE001
            log.warning("quote failed for %s: %s", symbol, e)
            return None
        if not (last > 0 and prev > 0):
            return None
        return Quote(ticker, last, op, prev, vol, datetime.now(), "yfinance (delayed)", True)

    def quote(self, ticker: str) -> Quote | None:
        return self._q(ticker if ticker.endswith(".AX") else f"{ticker}.AX", ticker)

    def index_quote(self) -> Quote | None:
        return self._q(self.index_ticker, self.index_ticker)


class StaticQuotes(QuoteProvider):
    """For tests and the sim dry run: quotes supplied by hand."""

    name = "static"

    def __init__(self, quotes: dict[str, Quote], index: Quote | None = None):
        self.quotes = quotes
        self.index = index

    def quote(self, ticker: str) -> Quote | None:
        return self.quotes.get(ticker)

    def index_quote(self) -> Quote | None:
        return self.index


def avg_volume_20d(daily: pd.DataFrame, window: int = 20) -> float | None:
    if daily is None or len(daily) < window:
        return None
    return float(daily["volume"].tail(window).mean())


def median_turnover_20d(daily: pd.DataFrame, window: int = 20) -> float | None:
    if daily is None or len(daily) < window:
        return None
    return float((daily["close"] * daily["volume"]).tail(window).median())
