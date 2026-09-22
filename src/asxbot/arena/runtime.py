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
        from asxbot.live.quotes import YFinanceQuotes

        return YFinanceQuotes(self.cfg.get("backtest.index_ticker"))

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

    def now(self) -> datetime:
        return datetime.now(SYD)


def build_arena(cfg: Config, quotes=None) -> Arena:
    from asxbot.data.factory import get_store
    from asxbot.data.universe import asx200_codes, build_universes

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
    )
    a, b = build_universes(cfg.data_dir, cfg.get("collector.user_agent"))
    universe = set(a.codes) | set(b.codes)
    shorts = asx200_codes(cfg.data_dir, cfg.get("collector.user_agent"))
    return Arena(cfg, broker, broker.store, universe, shorts)


def make_bot(arena: Arena, pb: Playbook, quotes=None):
    """The rule-based yardstick for a playbook."""
    if pb.key == "asx_announcements":
        from asxbot.arena.bots.announcement_drift import AnnouncementDriftBot

        return AnnouncementDriftBot(
            arena.cfg,
            pb,
            arena.broker,
            quotes or arena.quote_provider(),
            arena.daily_lookup(),
            arena.universe,
        )
    raise KeyError(f"no yardstick bot built for playbook {pb.key!r} yet")
