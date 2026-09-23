"""The aggression ladder (ARENA.md), read from config.yaml `arena.levels`.

A playbook sits at exactly one level. The level sets what the code will allow: risk per
trade, open positions, leverage, and the daily loss limit. The agent cannot change these;
only a human editing config.yaml can.
"""

from __future__ import annotations

from dataclasses import dataclass

from asxbot.config import Config


@dataclass(frozen=True)
class Level:
    number: int
    name: str
    aim: str
    risk_per_trade_pct: float
    max_open_positions: int
    leverage_asx: float
    leverage_crypto: float
    daily_loss_limit_pct: float
    holding: str
    wake_sensitivity: str

    def leverage(self, market: str) -> float:
        return self.leverage_crypto if market == "crypto" else self.leverage_asx


def load_level(cfg: Config, number: int) -> Level:
    # YAML parses the level keys as integers; accept either spelling.
    levels = cfg.get("arena.levels") or {}
    raw = levels.get(int(number), levels.get(str(number)))
    if raw is None:
        raise KeyError(f"arena.levels.{number} is not defined in config.yaml")
    return Level(
        number=int(number),
        name=str(raw["name"]),
        aim=str(raw["aim"]),
        risk_per_trade_pct=float(raw["risk_per_trade_pct"]),
        max_open_positions=int(raw["max_open_positions"]),
        leverage_asx=float(raw["leverage_asx"]),
        leverage_crypto=float(raw["leverage_crypto"]),
        daily_loss_limit_pct=float(raw["daily_loss_limit_pct"]),
        holding=str(raw["holding"]),
        wake_sensitivity=str(raw["wake_sensitivity"]),
    )


@dataclass(frozen=True)
class Playbook:
    key: str
    arena_id: int
    title: str
    market: str
    level: Level
    enabled: bool
    status: str
    raw: dict

    @property
    def agent_account(self) -> str:
        return f"{self.key}__agent"

    @property
    def bot_account(self) -> str:
        return f"{self.key}__bot"

    @property
    def holding(self) -> str:
        """The playbook's holding period, which may override its level's.

        Added 2026-09-23: the agent was on the level's intraday default while its own
        yardstick held 10 sessions, so the two were not running the same race. A playbook
        may now state its own holding; everything else still comes from the level.
        """
        return str(self.raw.get("holding", self.level.holding))

    @property
    def hold_sessions(self) -> int:
        """How many sessions a position may be held before code closes it. 0 = no limit."""
        return int(self.raw.get("hold_sessions", 0))

    def guidance(self, name: str, default=None):
        return self.raw.get("guidance", {}).get(name, default)

    def yardstick(self) -> dict:
        return dict(self.raw.get("yardstick", {}))


def load_playbook(cfg: Config, key: str) -> Playbook:
    raw = (cfg.get("arena.playbooks") or {}).get(key)
    if raw is None:
        raise KeyError(f"arena.playbooks.{key} is not defined in config.yaml")
    return Playbook(
        key=key,
        arena_id=int(raw.get("arena_id", 0)),
        title=str(raw.get("title", key)),
        market=str(raw.get("market", "asx")),
        level=load_level(cfg, int(raw.get("level", 1))),
        enabled=bool(raw.get("enabled", False)),
        status=str(raw.get("status", "build")),
        raw=dict(raw),
    )


def all_playbooks(cfg: Config) -> list[Playbook]:
    return [load_playbook(cfg, k) for k in (cfg.get("arena.playbooks") or {})]


def active_playbooks(cfg: Config) -> list[Playbook]:
    return [p for p in all_playbooks(cfg) if p.enabled]
