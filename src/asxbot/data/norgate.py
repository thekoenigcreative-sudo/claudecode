"""Norgate Data adapter (Australian Stocks, Platinum). Ready to enable; not set up yet.

Needs the Norgate Data Updater app running on this Windows PC and
    uv pip install -e ".[norgate]"
Until then every call raises ProviderUnavailable with that instruction.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import pandas as pd

from asxbot.data.base import COLUMNS, PriceProvider, ProviderUnavailable

SETUP_HINT = (
    "Norgate is not available on this PC. Install the Norgate Data Updater, subscribe to "
    "Australian Stocks (Platinum), then run: uv pip install -e \".[norgate]\""
)


def _lib():
    try:
        import norgatedata  # type: ignore

        return norgatedata
    except ImportError as e:
        raise ProviderUnavailable(SETUP_HINT) from e


class NorgateProvider(PriceProvider):
    name = "norgate"
    label = "Norgate Platinum (includes delisted stocks)"
    survivorship_safe = True

    def __init__(self, trial: bool = False):
        # The free trial has ~2 years of history: results are a dry run, not a go/no-go.
        if trial:
            self.label = "Norgate TRIAL (2y history) - dry run, not a go/no-go"
            self.survivorship_safe = False

    def daily(self, ticker: str, start: date | str, end: date | str | None = None) -> pd.DataFrame:
        nd = _lib()
        symbol = ticker if ticker.endswith(".AU") else f"{ticker}.AU"
        try:
            df = nd.price_timeseries(
                symbol,
                stock_price_adjustment_setting=nd.StockPriceAdjustmentType.TOTALRETURN,
                padding_setting=nd.PaddingType.NONE,
                start_date=str(start),
                end_date=str(end) if end else None,
                timeseriesformat="pandas-dataframe",
            )
        except Exception as e:  # noqa: BLE001
            raise ProviderUnavailable(f"norgatedata call failed: {e}") from e
        if df is None or len(df) == 0:
            return pd.DataFrame(columns=COLUMNS)
        df = df.rename(columns=str.lower)
        return df[[c for c in COLUMNS if c in df.columns]]

    def daily_many(
        self, tickers: Iterable[str], start: date | str, end: date | str | None = None
    ) -> dict[str, pd.DataFrame]:
        return {t: self.daily(t, start, end) for t in tickers}

    def index_members(self, index_code: str, as_of: date) -> list[str] | None:
        """Point-in-time membership via Norgate's constituent time series.

        index_code: e.g. "S&P/ASX 300". Returns codes without the .AU suffix.
        """
        nd = _lib()
        members: list[str] = []
        for symbol in nd.database_symbols("Australian Equities"):
            ts = nd.index_constituent_timeseries(
                symbol, index_code, timeseriesformat="pandas-dataframe",
                start_date=str(as_of), end_date=str(as_of),
            )
            if ts is not None and len(ts) and int(ts.iloc[-1]["Index Constituent"]) == 1:
                members.append(symbol.replace(".AU", ""))
        return members
