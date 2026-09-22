"""Command-line entry point. Subcommands are added stage by stage."""

from __future__ import annotations

import argparse
import sys

from asxbot import __version__
from asxbot.config import ConfigError, load_config
from asxbot.log import setup_logging


def cmd_check(args: argparse.Namespace) -> int:
    cfg = load_config()
    log = setup_logging(cfg.data_dir)
    log.info("asxbot %s  broker=%s  provider=%s", __version__, cfg.broker, cfg.get("data.provider"))
    log.info(
        "capital=%s max_positions=%s position_size=%.2f",
        cfg.get("capital.starting_aud"),
        cfg.get("capital.max_positions"),
        cfg.position_size_aud,
    )
    log.info("data label: %s", cfg.data_label())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="asxbot")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="load config, print settings, verify broker guard").set_defaults(
        fn=cmd_check
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.fn(args))
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
