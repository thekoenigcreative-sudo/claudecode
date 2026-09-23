"""Provider interface. Every provider returns the same frame shape:

    index:   DatetimeIndex, tz-naive calendar dates, name "date", ascending, unique
    columns: open, high, low, close, volume  (floats; OHLC adjusted for splits AND dividends,
             i.e. a total-return basis, so day-to-day ratios are true returns)

Tickers are plain ASX codes ("BHP"); the provider adds its own suffix.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import date

import pandas as pd

COLUMNS = ["open", "high", "low", "close", "volume"]


class ProviderUnavailable(RuntimeError):
    """The provider's backing library or service is not set up on this PC."""


def normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce any provider output into the standard shape. Drops rows with no close."""
    out = df.copy()
    out.columns = [str(c).lower().replace(" ", "_") for c in out.columns]
    if "adj_close" in out.columns and "close" not in out.columns:
        out = out.rename(columns={"adj_close": "close"})
    missing = [c for c in COLUMNS if c not in out.columns]
    if missing:
        raise ValueError(f"provider frame missing columns {missing}; has {list(out.columns)}")
    out = out[COLUMNS].astype("float64")
    idx = pd.DatetimeIndex(pd.to_datetime(out.index))
    if idx.tz is not None:
        idx = idx.tz_convert("Australia/Sydney").tz_localize(None)
    out.index = idx.normalize()
    out.index.name = "date"
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out = out.dropna(subset=["close"])
    return out


class PriceProvider(ABC):
    name: str = "base"
    #: Honesty label attached to every result that comes from this provider.
    label: str = ""
    #: True only when the provider includes delisted stocks (survivorship-safe).
    survivorship_safe: bool = False

    @abstractmethod
    def daily(self, ticker: str, start: date | str, end: date | str | None = None) -> pd.DataFrame:
        """Daily bars for one ticker in the standard shape. Empty frame if unknown."""

    def daily_many(
        self, tickers: Iterable[str], start: date | str, end: date | str | None = None
    ) -> dict[str, pd.DataFrame]:
        """Default: loop. Providers with batch APIs override this."""
        return {t: self.daily(t, start, end) for t in tickers}

    def index_members(self, index_code: str, as_of: date) -> list[str] | None:
        """Point-in-time index membership, or None when the provider cannot supply it."""
        return None
