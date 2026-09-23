"""Parquet cache of daily bars, one file per ticker, written atomically.

data/prices/<provider>/<TICKER>.parquet   plus   data/prices/<provider>/_meta.json
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from asxbot.data.base import COLUMNS, PriceProvider, normalise
from asxbot.io import safe_stem, write_parquet_atomic, write_text_atomic
from asxbot.log import get_logger

log = get_logger("asxbot.data")


class PriceStore:
    def __init__(self, data_dir: Path, provider: PriceProvider):
        self.provider = provider
        self.dir = Path(data_dir) / "prices" / provider.name
        self.dir.mkdir(parents=True, exist_ok=True)
        self._meta_path = self.dir / "_meta.json"
        self._meta: dict[str, str] = self._load_meta()

    # -- meta --------------------------------------------------------------
    def _load_meta(self) -> dict[str, str]:
        if self._meta_path.exists():
            try:
                return json.loads(self._meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log.warning("corrupt %s; starting a fresh meta file", self._meta_path)
        return {}

    def _save_meta(self) -> None:
        write_text_atomic(json.dumps(self._meta, indent=0, sort_keys=True), self._meta_path)

    def fetched_on(self, ticker: str) -> date | None:
        s = self._meta.get(ticker)
        return date.fromisoformat(s) if s else None

    def path(self, ticker: str) -> Path:
        return self.dir / f"{safe_stem(ticker)}.parquet"

    # -- read --------------------------------------------------------------
    def load(self, ticker: str) -> pd.DataFrame | None:
        p = self.path(ticker)
        if not p.exists():
            return None
        try:
            df = pd.read_parquet(p)
        except Exception as e:  # noqa: BLE001 - a corrupt sync artefact, refetch
            log.warning("unreadable cache %s (%s); will refetch", p, e)
            return None
        return df[COLUMNS]

    def cached_tickers(self) -> list[str]:
        return sorted(
            p.stem.rstrip("_") for p in self.dir.glob("*.parquet") if not p.stem.startswith("_")
        )

    # -- fetch -------------------------------------------------------------
    def get(
        self,
        ticker: str,
        start: date | str,
        end: date | str | None = None,
        max_age_days: int = 1,
        refresh: bool = False,
    ) -> pd.DataFrame:
        df = None if refresh else self.load(ticker)
        fetched = self.fetched_on(ticker)
        stale = fetched is None or (date.today() - fetched).days > max_age_days
        if df is None or stale:
            raw = self.provider.daily(ticker, start, end)
            df = normalise(raw) if len(raw) else pd.DataFrame(columns=COLUMNS)
            self._put(ticker, df)
        return df

    def get_many(
        self,
        tickers: list[str],
        start: date | str,
        end: date | str | None = None,
        max_age_days: int = 1,
        refresh: bool = False,
    ) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        need: list[str] = []
        for t in tickers:
            df = None if refresh else self.load(t)
            fetched = self.fetched_on(t)
            stale = fetched is None or (date.today() - fetched).days > max_age_days
            if df is None or stale:
                need.append(t)
            else:
                out[t] = df
        if need:
            log.info("%s: fetching %d tickers (%d cached)", self.provider.name, len(need), len(out))
            fresh = self.provider.daily_many(need, start, end)
            for t in need:
                df = fresh.get(t)
                df = normalise(df) if df is not None and len(df) else pd.DataFrame(columns=COLUMNS)
                self._put(t, df, save_meta=False)
                out[t] = df
            self._save_meta()
        return out

    def _put(self, ticker: str, df: pd.DataFrame, save_meta: bool = True) -> None:
        if df.index.name != "date":
            df.index.name = "date"
        write_parquet_atomic(df, self.path(ticker))
        self._meta[ticker] = datetime.now().date().isoformat()
        if save_meta:
            self._save_meta()

    # -- panels ------------------------------------------------------------
    @staticmethod
    def panel(frames: dict[str, pd.DataFrame], field: str) -> pd.DataFrame:
        """Wide frame: dates x tickers for one field. Missing = NaN."""
        cols = {t: df[field] for t, df in frames.items() if len(df)}
        if not cols:
            return pd.DataFrame()
        return pd.DataFrame(cols).sort_index()
