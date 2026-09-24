"""One place that wires the arena together, so the CLI, the bots and the agents' tools all
build the same broker, the same accounts and the same universes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from asxbot.arena.accounts import Account, AccountStore
from asxbot.arena.broker import ArenaBroker
from asxbot.arena.levels import Playbook, active_playbooks, all_playbooks, load_playbook
from asxbot.arena.minutes import MinuteBars
from asxbot.backtest.costs import CostModel
from asxbot.config import Config
from asxbot.log import get_logger

log = get_logger("asxbot.arena.runtime")
SYD = ZoneInfo("Australia/Sydney")


@dataclass
class Arena:
    cfg: Config
    broker: ArenaBroker
    store: AccountStore
    universe: set[str]
    short_universe: set[str]
    # Set by the watcher to its poller's PDF fetch, so an announcement that reaches the
    # reader with no document on disk gets one then (TRACKER #8). None outside the watcher.
    fetch_pdf: object = None

    # -- playbooks and accounts ---------------------------------------------
    def playbook(self, key: str) -> Playbook:
        return load_playbook(self.cfg, key)

    def playbooks(self, only_enabled: bool = True) -> list[Playbook]:
        return active_playbooks(self.cfg) if only_enabled else all_playbooks(self.cfg)

    def account(self, pb: Playbook, kind: str) -> Account:
        name = pb.agent_account if kind == "agent" else pb.bot_account
        return self.store.open(
            name, pb.key, kind, pb.level.number, float(self.cfg.get("arena.starting_aud", 10000))
        )

    def both_accounts(self, pb: Playbook) -> tuple[Account, Account]:
        return self.account(pb, "agent"), self.account(pb, "bot")

    def quote_provider(self):
        """Yahoo's delayed quotes, or with `data.live_provider: ibkr` IBKR's real-time quote
        whenever IB Gateway is ready and Yahoo's otherwise (each Quote's source says which)."""
        from asxbot.arena.intraday import live_provider
        from asxbot.live.quotes import YFinanceQuotes

        yahoo = YFinanceQuotes(self.cfg.get("backtest.index_ticker"))
        if live_provider(self.cfg) != "ibkr":
            return yahoo
        from asxbot.ibkr.feed import quote_provider

        return quote_provider(self.cfg, yahoo)

    def daily_lookup(self):
        from asxbot.data.factory import get_store

        store = get_store(self.cfg)
        start = self.cfg.get("data.price_history_start")

        def daily(ticker: str):
            try:
                return store.get(ticker, start, max_age_days=3)
            except Exception:  # noqa: BLE001
                return None

        return daily

    def daily_fresh(self):
        """Daily bars straight from the provider, never from or into the cache.

        The yardstick confirms on the session that has only just closed, so a cached copy
        up to three days old will not do, and the backtest's cached history must not change
        underneath it either.
        """
        from asxbot.data.base import COLUMNS, normalise
        from asxbot.data.factory import get_provider

        provider = get_provider(self.cfg)

        def fetch(tickers: list[str], start) -> dict:
            import pandas as pd

            raw = provider.daily_many(tickers, start)
            return {
                t: normalise(df) if df is not None and len(df) else pd.DataFrame(columns=COLUMNS)
                for t, df in raw.items()
            }

        return fetch

    def now(self) -> datetime:
        return datetime.now(SYD)


def build_arena(cfg: Config, quotes=None) -> Arena:
    from asxbot.data.universe import asx200_codes, build_universes

    broker = arena_broker(cfg)
    from asxbot.arena.notify import build_notifier

    broker.notifier = build_notifier(cfg)
    a, b = build_universes(cfg.data_dir, cfg.get("collector.user_agent"))
    universe = set(a.codes) | set(b.codes)
    shorts = asx200_codes(cfg.data_dir, cfg.get("collector.user_agent"))
    return Arena(cfg, broker, broker.store, universe, shorts)


def arena_broker(cfg: Config) -> ArenaBroker:
    """The arena's broker as the watcher builds it - costs, minute bars, turnover lookup -
    with no notifier, universes or quotes. scripts/correct_fill_arn000002.py prices with it."""
    from asxbot.data.factory import get_store

    if cfg.broker != "sim":
        raise RuntimeError(
            f"the arena is fake money only, but broker mode is {cfg.broker!r}. Refusing to start."
        )
    data_store = get_store(cfg)
    start = cfg.get("data.price_history_start")

    def adv(ticker: str):
        """Average daily dollar turnover, for scaling slippage to liquidity."""
        try:
            d = data_store.get(ticker, start, max_age_days=5)
        except Exception:  # noqa: BLE001
            return None
        if d is None or len(d) < 20:
            return None
        return float((d["close"] * d["volume"]).tail(20).mean())

    def median_turnover(ticker: str):
        """Median daily dollar turnover over 20 sessions: the size-aware liquidity rule."""
        from asxbot.live.quotes import median_turnover_20d

        try:
            d = data_store.get(ticker, start, max_age_days=5)
        except Exception:  # noqa: BLE001
            return None
        return median_turnover_20d(d)

    fill = cfg.get("arena.fill") or {}
    minutes = MinuteBars(cfg.data_dir, str(fill.get("minute_price", "close")))
    broker = ArenaBroker(
        cfg.data_dir,
        CostModel.from_config(cfg),
        minutes,
        adv,
        short_borrow_pct_annual=float(cfg.get("arena.costs.short_borrow_pct_annual", 3.0)),
        resolve_after_minutes=int(fill.get("resolve_after_minutes", 22)),
        max_wait_minutes=int(fill.get("max_wait_minutes", 390)),
        max_volume_share=float(fill.get("max_volume_share", 0.20)),
        settle_minutes=int(fill.get("settle_minutes", 0)),
        opening_auction=str(fill.get("opening_auction", "daily_open")),
        auction_volume_share=float(fill.get("auction_volume_share", 0.20)),
        auction_wait_minutes=int(fill.get("auction_wait_minutes", 30)),
    )
    broker.median_turnover = median_turnover
    return broker


def make_bot(arena: Arena, pb: Playbook, quotes=None):
    """The rule-based yardstick for a playbook."""
    if pb.key == "asx_announcements":
        from asxbot.arena.bots.announcement_drift import AnnouncementDriftBot

        return AnnouncementDriftBot(
            arena.cfg, pb, arena.broker, arena.universe, arena.daily_fresh()
        )
    raise KeyError(f"no yardstick bot built for playbook {pb.key!r} yet")
