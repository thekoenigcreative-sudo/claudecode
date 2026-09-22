"""Bot interface. A bot sees the same alerts the agent sees and applies a fixed rule.

Bots go through arena_place_order exactly like the agent, so the same limits apply to both
and the comparison is fair.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from asxbot.announcements.model import Announcement
from asxbot.arena.accounts import Account
from asxbot.arena.broker import ArenaBroker
from asxbot.arena.levels import Playbook
from asxbot.config import Config


@dataclass
class BotDecision:
    ticker: str
    side: str
    qty: int
    limit: float
    stop: float
    reason: str


class Bot(ABC):
    """`name` is dated, because a rule change becomes a NEW bot version scored separately."""

    name: str = "bot"
    version: str = "2026-09-22"

    def __init__(self, cfg: Config, playbook: Playbook, broker: ArenaBroker):
        self.cfg = cfg
        self.playbook = playbook
        self.broker = broker
        self.params = playbook.yardstick()

    @property
    def label(self) -> str:
        return f"{self.name} ({self.version})"

    @abstractmethod
    def on_announcement(
        self, acct: Account, a: Announcement, now: datetime
    ) -> tuple[BotDecision | None, str]:
        """Decide from the fixed rule alone. Returns (decision or None, why)."""

    @abstractmethod
    def manage(self, acct: Account, now: datetime) -> list[BotDecision]:
        """Exits the rule calls for: time exits, and anything beyond the code-side stop."""
