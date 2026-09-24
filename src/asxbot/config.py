"""Load config.yaml and .env. The only place settings are read from disk.

Live mode is refused unless BOTH config.yaml says `broker: live` AND the
environment has LIVE_TRADING_CONFIRMED=yes. Both are set by the human, never by code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

VALID_BROKERS = ("sim", "paper", "live")


class ConfigError(RuntimeError):
    pass


def repo_root() -> Path:
    """The repo root: directory containing config.yaml and SPEC.md, walking up from here."""
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "config.yaml").exists() and (parent / "SPEC.md").exists():
            return parent
    return Path.cwd()


@dataclass(frozen=True)
class Config:
    raw: dict[str, Any]
    root: Path
    env: dict[str, str] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, dotted: str, default: Any = None) -> Any:
        """cfg.get("capital.max_positions")"""
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def broker(self) -> str:
        return str(self.raw.get("broker", "sim"))

    @property
    def data_dir(self) -> Path:
        d = Path(self.get("data.dir", "data"))
        return d if d.is_absolute() else self.root / d

    @property
    def logs_dir(self) -> Path:
        """Log files: a local folder outside Google Drive (asxbot.log.logs_dir)."""
        from asxbot.log import logs_dir

        return logs_dir()

    @property
    def position_size_aud(self) -> float:
        return float(self.get("capital.starting_aud")) / int(self.get("capital.max_positions"))

    def data_label(self) -> str:
        """Label every result with its data source's honesty level."""
        provider = self.get("data.provider", "yfinance")
        if provider == "yfinance":
            return self.get("backtest.data_label_yfinance", "plumbing test - not a go/no-go")
        return f"data: {provider}"


def _validate(raw: dict[str, Any], env: dict[str, str]) -> None:
    broker = raw.get("broker", "sim")
    if broker not in VALID_BROKERS:
        raise ConfigError(f"broker must be one of {VALID_BROKERS}, got {broker!r}")
    if broker == "live" and env.get("LIVE_TRADING_CONFIRMED", "").strip().lower() != "yes":
        raise ConfigError(
            "broker: live in config.yaml but LIVE_TRADING_CONFIRMED=yes is not set in .env. "
            "Refusing to start."
        )
    cap = raw.get("capital", {})
    if float(cap.get("starting_aud", 0)) <= 0 or int(cap.get("max_positions", 0)) <= 0:
        raise ConfigError("capital.starting_aud and capital.max_positions must be positive")
    lim = raw.get("limits", {})
    size = float(cap["starting_aud"]) / int(cap["max_positions"])
    if float(lim.get("max_position_aud", 0)) < size:
        raise ConfigError(
            f"limits.max_position_aud ({lim.get('max_position_aud')}) is below the equal-weight "
            f"position size ({size:.2f}); every order would be refused"
        )
    if int(lim.get("max_open_positions", 0)) > int(cap["max_positions"]):
        raise ConfigError("limits.max_open_positions cannot exceed capital.max_positions")
    for section in ("strategy", "baseline", "costs", "universe"):
        if "frozen_on" not in raw.get(section, {}):
            raise ConfigError(f"{section}.frozen_on missing: parameters must be dated when fixed")


def load_config(path: str | Path | None = None, env_file: str | Path | None = None) -> Config:
    root = repo_root()
    cfg_path = Path(path) if path else root / "config.yaml"
    if not cfg_path.exists():
        raise ConfigError(f"config not found: {cfg_path}")
    env_path = Path(env_file) if env_file else root / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
    with open(cfg_path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    env = {
        k: v
        for k, v in os.environ.items()
        if k.startswith(("LIVE_", "IB_", "COLLECTOR_", "TELEGRAM_"))
    }
    _validate(raw, env)
    # data dir is always resolved against the real repo root, even for a temp config copy
    return Config(raw=raw, root=root, env=env)
