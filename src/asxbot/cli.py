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


def cmd_universe(args: argparse.Namespace) -> int:
    from asxbot.data.universe import build_universes

    cfg = load_config()
    log = setup_logging(cfg.data_dir)
    a, b = build_universes(cfg.data_dir, cfg.get("collector.user_agent"))
    for u in (a, b):
        log.info("%s: %d codes as of %s  source: %s", u.name, len(u.codes), u.as_of, u.source)
        log.info("  first: %s", " ".join(u.codes[:12]))
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    from asxbot.data.benchmark import build_benchmark
    from asxbot.data.factory import get_store
    from asxbot.data.universe import build_universes

    cfg = load_config()
    log = setup_logging(cfg.data_dir)
    store = get_store(cfg)
    start = cfg.get("data.price_history_start")
    log.info("provider=%s  label=%s", store.provider.name, store.provider.label)
    bm = build_benchmark(
        store, start, cfg.get("backtest.benchmark_ticker"), cfg.get("backtest.index_ticker")
    )
    log.info(
        "benchmark: %s .. %s (%d days)  %s",
        bm.total_return.index.min().date(),
        bm.total_return.index.max().date(),
        len(bm.total_return),
        bm.note,
    )
    a, b = build_universes(cfg.data_dir, cfg.get("collector.user_agent"))
    wanted = {"asx300": a.codes, "small": b.codes, "all": a.codes + b.codes}[args.universe]
    if args.limit:
        wanted = wanted[: args.limit]
    frames = store.get_many(wanted, start, refresh=args.refresh)
    n_ok = sum(1 for f in frames.values() if len(f))
    log.info("fetched %d tickers, %d with data, %d empty", len(frames), n_ok, len(frames) - n_ok)
    log.info("RESULT LABEL: %s", store.provider.label)
    return 0


def cmd_data_status(args: argparse.Namespace) -> int:
    from asxbot.data.factory import get_store

    cfg = load_config()
    log = setup_logging(cfg.data_dir)
    store = get_store(cfg)
    ts = store.cached_tickers()
    log.info("%s cache: %d tickers in %s", store.provider.name, len(ts), store.dir)
    return 0


def _collector(cfg):
    from asxbot.alerts import Alerts
    from asxbot.announcements.http import PacedClient

    client = PacedClient(
        cfg.data_dir / "announcements" / "cache",
        cfg.get("collector.user_agent"),
        pause_s=float(cfg.get("collector.request_pause_s", 3.0)),
        max_retries=int(cfg.get("collector.max_retries", 4)),
        backoff_base_s=float(cfg.get("collector.backoff_base_s", 10)),
    )
    return client, Alerts(cfg.data_dir)


def cmd_ann_history(args: argparse.Namespace) -> int:
    from asxbot.announcements.history import HistoryArchive
    from asxbot.data.universe import build_universes, fetch_directory

    cfg = load_config()
    log = setup_logging(cfg.data_dir)
    client, alerts = _collector(cfg)
    a, b = build_universes(cfg.data_dir, cfg.get("collector.user_agent"))
    codes = {"asx300": a.codes, "small": b.codes, "all": a.codes + b.codes}[args.universe]
    if args.codes:
        codes = [c.upper() for c in args.codes]
    directory = fetch_directory(cfg.data_dir, cfg.get("collector.user_agent"))
    listing = {
        r.code: (r.listing_date.date() if hasattr(r.listing_date, "date") else None)
        for r in directory.itertuples()
    }
    arc = HistoryArchive(cfg.data_dir, client, alerts)
    log.info(
        "history archive: %d codes, budget=%s requests", len(codes), args.max_requests or "none"
    )
    stats = arc.run(codes, listing, max_requests=args.max_requests or None)
    log.info("history archive: %s (requests made: %d)", stats, client.requests_made)
    return 0


def cmd_ann_poll(args: argparse.Namespace) -> int:
    from asxbot.announcements.live import LivePoller
    from asxbot.data.universe import build_universes

    cfg = load_config()
    log = setup_logging(cfg.data_dir)
    client, alerts = _collector(cfg)
    a, b = build_universes(cfg.data_dir, cfg.get("collector.user_agent"))
    poller = LivePoller(
        cfg.data_dir, client, alerts, set(a.codes) | set(b.codes), fetch_pdfs=not args.no_pdf
    )
    hours = (cfg.get("collector.hours.start"), cfg.get("collector.hours.end"))
    if args.now:
        new = poller.poll_once()
        for x in new[:20]:
            log.info(
                "  %s %s %s %s",
                x.released_at,
                x.code,
                "*" if x.price_sensitive else " ",
                x.headline,
            )
        return 0
    poller.run(float(cfg.get("collector.poll_interval_s", 60)), hours, once=args.once)
    return 0


def cmd_ann_status(args: argparse.Namespace) -> int:
    from asxbot.alerts import Alerts
    from asxbot.announcements.history import status

    cfg = load_config()
    log = setup_logging(cfg.data_dir)
    log.info("history: %s", status(cfg.data_dir))
    for key, msg in Alerts(cfg.data_dir).active():
        log.warning("ACTIVE ALERT %s: %s", key, msg.replace("\n", " | "))
    return 0


def cmd_alerts_clear(args: argparse.Namespace) -> int:
    from asxbot.alerts import Alerts

    cfg = load_config()
    setup_logging(cfg.data_dir)
    Alerts(cfg.data_dir).clear(args.key)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="asxbot")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="load config, print settings, verify broker guard").set_defaults(
        fn=cmd_check
    )
    sub.add_parser("universe", help="build and print the two universes").set_defaults(
        fn=cmd_universe
    )
    f = sub.add_parser("fetch", help="download/refresh daily prices into data/prices")
    f.add_argument("--universe", choices=["asx300", "small", "all"], default="asx300")
    f.add_argument("--limit", type=int, default=0, help="only the first N tickers (testing)")
    f.add_argument("--refresh", action="store_true", help="ignore cache age, re-download")
    f.set_defaults(fn=cmd_fetch)
    sub.add_parser("data-status", help="what is in the price cache").set_defaults(
        fn=cmd_data_status
    )
    ann = sub.add_parser("announcements", help="asx.com.au collector").add_subparsers(
        dest="sub", required=True
    )
    h = ann.add_parser("history", help="build/resume the announcement archive (Ctrl-C to stop)")
    h.add_argument("--universe", choices=["asx300", "small", "all"], default="all")
    h.add_argument("--codes", nargs="*", help="only these codes (testing)")
    h.add_argument("--max-requests", type=int, default=0, help="stop after N requests")
    h.set_defaults(fn=cmd_ann_history)
    pl = ann.add_parser("poll", help="live poller for today's announcements")
    pl.add_argument("--once", action="store_true", help="one cycle, then exit")
    pl.add_argument("--now", action="store_true", help="fetch once regardless of hours/day")
    pl.add_argument("--no-pdf", action="store_true")
    pl.set_defaults(fn=cmd_ann_poll)
    ann.add_parser("status", help="archive progress and active alerts").set_defaults(
        fn=cmd_ann_status
    )
    ac = sub.add_parser("alerts-clear", help="clear an alert flag by hand")
    ac.add_argument("key")
    ac.set_defaults(fn=cmd_alerts_clear)
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
