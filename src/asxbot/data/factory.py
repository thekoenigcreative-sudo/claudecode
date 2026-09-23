from __future__ import annotations

from asxbot.config import Config
from asxbot.data.base import PriceProvider
from asxbot.data.store import PriceStore


def get_provider(cfg: Config) -> PriceProvider:
    name = cfg.get("data.provider", "yfinance")
    if name == "yfinance":
        from asxbot.data.yf import YFinanceProvider

        return YFinanceProvider()
    if name == "norgate":
        from asxbot.data.norgate import NorgateProvider

        return NorgateProvider(trial=bool(cfg.get("data.norgate_trial", False)))
    raise ValueError(f"unknown data.provider {name!r}")


def get_store(cfg: Config) -> PriceStore:
    return PriceStore(cfg.data_dir, get_provider(cfg))
