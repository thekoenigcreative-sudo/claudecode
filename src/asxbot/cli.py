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
    import pandas as pd

    ld = pd.to_datetime(directory["listing_date"], errors="coerce")
    listing = {
        code: (d.date() if pd.notna(d) else None)
        for code, d in zip(directory["code"], ld, strict=True)
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


def cmd_backtest(args: argparse.Namespace) -> int:
    from asxbot.backtest.run import run_phase1

    cfg = load_config()
    log = setup_logging(cfg.data_dir)
    out = run_phase1(cfg, universes=args.universe or None)
    log.info("done: %s", out)
    return 0


def _alert_banner(cfg, log) -> None:
    from asxbot.alerts import Alerts

    for key, msg in Alerts(cfg.data_dir).active():
        log.warning("ACTIVE ALERT %s: %s", key, msg.replace("\n", " | "))


def _universe_set(cfg) -> set[str]:
    from asxbot.data.universe import build_universes

    a, b = build_universes(cfg.data_dir, cfg.get("collector.user_agent"))
    allowed = cfg.get("limits.allowed_universe", ["asx300", "small"])
    out: set[str] = set()
    if "asx300" in allowed:
        out |= set(a.codes)
    if "small" in allowed:
        out |= set(b.codes)
    return out


def _scanner(cfg, quotes=None):
    from asxbot.backtest.costs import CostModel
    from asxbot.data.factory import get_store
    from asxbot.live.quotes import YFinanceQuotes
    from asxbot.live.scanner import Scanner

    store = get_store(cfg)
    start = cfg.get("data.price_history_start")

    def daily(ticker):
        try:
            return store.get(ticker, start, max_age_days=3)
        except Exception:  # noqa: BLE001
            return None

    return Scanner(
        cfg.data_dir,
        quotes or YFinanceQuotes(cfg.get("backtest.index_ticker")),
        daily,
        float(cfg.get("strategy.gap_pct_vs_index")),
        float(cfg.get("strategy.volume_multiple")),
        float(cfg.get("universe.turnover_floor_aud")),
        float(cfg.get("strategy.stop_loss_pct")),
        cfg.position_size_aud,
        CostModel.from_config(cfg),
        _universe_set(cfg),
    )


def _broker(cfg):
    from asxbot.backtest.costs import CostModel
    from asxbot.broker.sim import SimBroker
    from asxbot.data.factory import get_store
    from asxbot.live.quotes import YFinanceQuotes

    if cfg.broker == "sim":
        store = get_store(cfg)
        quotes = YFinanceQuotes(cfg.get("backtest.index_ticker"))
        start = cfg.get("data.price_history_start")

        def ref(ticker):
            q = quotes.quote(ticker)
            try:
                d = store.get(ticker, start, max_age_days=3)
            except Exception:  # noqa: BLE001
                d = None
            adv = (
                float((d["close"] * d["volume"]).tail(20).mean())
                if d is not None and len(d) >= 20
                else None
            )
            if q is not None:
                return q.last, adv
            if d is not None and len(d):
                return float(d["close"].iloc[-1]), adv
            return None

        return SimBroker(
            cfg.data_dir, float(cfg.get("capital.starting_aud")), CostModel.from_config(cfg), ref
        )
    from asxbot.broker.ibkr import IBKRBroker

    return IBKRBroker(cfg)


def cmd_scan(args: argparse.Namespace) -> int:
    from asxbot.announcements.live import LivePoller

    cfg = load_config()
    log = setup_logging(cfg.data_dir)
    _alert_banner(cfg, log)
    client, alerts = _collector(cfg)
    sc = _scanner(cfg)
    poller = LivePoller(cfg.data_dir, client, alerts, sc.universe, fetch_pdfs=False)
    try:
        poller.poll_once()
    except Exception as e:  # noqa: BLE001
        log.error("collector unavailable (%s); scanning what is already recorded today", e)
    import pandas as pd

    from asxbot.announcements.model import Announcement

    day = poller._day_path(pd.Timestamp.now(tz="Australia/Sydney").date())
    if not day.exists():
        log.info("no announcements recorded today")
        return 0
    df = pd.read_parquet(day)
    items = [
        Announcement(
            r.code,
            r.released_at.to_pydatetime(),
            r.headline,
            bool(r.price_sensitive),
            str(r.ids_id),
            r.pdf_url,
            r.pages,
            r.size,
        )  # fmt: skip
        for r in df.itertuples()
    ]
    props = sc.scan(items)
    log.info("scan: %d announcements today, %d new proposals", len(items), len(props))
    for p in props:
        print(
            f"{p.id} BUY {p.ticker} qty {p.qty} limit {p.entry_limit} stop {p.stop} "
            f"risk ${p.dollar_risk}"
        )
        print(f"  {p.reasoning}")
        print(f"  {p.announcement_link}")
    return 0


def cmd_proposals(args: argparse.Namespace) -> int:
    cfg = load_config()
    log = setup_logging(cfg.data_dir)
    _alert_banner(cfg, log)
    sc = _scanner(cfg)
    for p in sc.list(None if args.all else "pending"):
        print(
            f"{p.id} [{p.status}] BUY {p.ticker} qty {p.qty} limit {p.entry_limit} stop {p.stop} "
            f"value ${p.value_aud} risk ${p.dollar_risk} order={p.order_id}"
        )
        if args.verbose:
            print(f"  {p.reasoning}\n  {p.announcement_link}")
    return 0


def cmd_place_order(args: argparse.Namespace) -> int:
    from asxbot.broker.orders import Limits, OrderRefused, place_order

    cfg = load_config()
    log = setup_logging(cfg.data_dir)
    _alert_banner(cfg, log)
    broker = _broker(cfg)
    limits = Limits.from_config(cfg, _universe_set(cfg))
    try:
        res = place_order(
            cfg, broker, limits, proposal_id=args.id, ticker=args.ticker, side=args.side,
            qty=args.qty, limit=args.limit,
        )  # fmt: skip
    except OrderRefused as e:
        print(f"REFUSED: {e}")
        return 3
    print(
        f"broker={broker.mode} order_id={res.order_id} status={res.status} "
        f"filled={res.filled_qty} avg_price={res.avg_price} commission={res.commission} "
        f"{res.message}"
    )
    return 0


def cmd_positions(args: argparse.Namespace) -> int:
    cfg = load_config()
    log = setup_logging(cfg.data_dir)
    _alert_banner(cfg, log)
    broker = _broker(cfg)
    print(f"broker={broker.mode} cash={broker.cash():.2f}")
    for p in broker.positions():
        print(f"  {p.ticker} qty {p.qty} avg_cost {p.avg_cost:.3f}")
    for o in broker.open_orders():
        print(f"  open order {o.order_id} {o.side} {o.ticker} {o.qty}@{o.limit}")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    from asxbot.alerts import Alerts
    from asxbot.broker.reconcile import reconcile
    from asxbot.log import EventLog

    cfg = load_config()
    setup_logging(cfg.data_dir)
    mm = reconcile(_broker(cfg), EventLog(cfg.data_dir), Alerts(cfg.data_dir))
    for m in mm:
        print(f"MISMATCH {m.ticker}: broker {m.broker_qty} vs log {m.log_qty}")
    print("reconciliation:", "MISMATCH" if mm else "clean")
    return 1 if mm else 0


def cmd_daily_report(args: argparse.Namespace) -> int:
    from asxbot.alerts import Alerts
    from asxbot.announcements.history import status
    from asxbot.broker.reconcile import reconcile
    from asxbot.log import EventLog

    cfg = load_config()
    setup_logging(cfg.data_dir)
    ev = EventLog(cfg.data_dir)
    broker = _broker(cfg)
    alerts = Alerts(cfg.data_dir)
    today = __import__("datetime").date.today().isoformat()
    print(f"# asxbot daily report {today}  (broker mode: {broker.mode}; data: {cfg.data_label()})")
    for key, msg in alerts.active():
        print(f"ALERT {key}: {msg.splitlines()[-1]}")
    print(f"cash {broker.cash():.2f}")
    for p in broker.positions():
        print(f"position {p.ticker} qty {p.qty} avg_cost {p.avg_cost:.3f}")

    def todays(kind: str) -> int:
        return sum(1 for r in ev.read(kind) if r.get("ts", "")[:10] == today)

    print(
        f"today: {todays('announcements')} announcements seen, {todays('signals')} checked, "
        f"{todays('proposals')} proposals, {todays('orders')} order calls, "
        f"{todays('fills')} fills"
    )
    mm = reconcile(broker, ev, alerts)
    print("reconciliation:", "MISMATCH " + str([m.__dict__ for m in mm]) if mm else "clean")
    print(f"announcement archive: {status(cfg.data_dir)}")
    return 0


def cmd_dryrun(args: argparse.Namespace) -> int:
    """End-to-end in sim with a FAKE announcement and a FAKE quote. Nothing touches the
    network except reading cached daily bars. Exercises collector record -> scanner ->
    proposal -> place_order -> sim fill -> log -> reconciliation."""
    from datetime import datetime

    from asxbot.alerts import Alerts
    from asxbot.announcements.model import Announcement
    from asxbot.backtest.costs import CostModel
    from asxbot.broker.orders import Limits, OrderRefused, place_order
    from asxbot.broker.reconcile import reconcile
    from asxbot.broker.sim import SimBroker
    from asxbot.live.quotes import Quote, StaticQuotes
    from asxbot.log import EventLog

    cfg = load_config()
    log = setup_logging(cfg.data_dir)
    if cfg.broker != "sim":
        print("dryrun only runs with broker: sim")
        return 2
    ticker = args.ticker.upper()
    prev = float(args.prev_close)
    last = round(prev * 1.12, 3)
    sc0 = _scanner(cfg)
    daily = sc0.daily_lookup(ticker)
    avg_vol = float(daily["volume"].tail(20).mean()) if daily is not None and len(daily) else 1e6
    fake_vol = 10.0 * avg_vol  # a clear volume surprise, whatever the stock's normal turnover
    quotes = StaticQuotes(
        {ticker: Quote(ticker, last, last, prev, fake_vol, datetime.now(), "DRYRUN fake")},
        Quote("^AXJO", 7000.0, 7000.0, 7000.0, 0.0, datetime.now(), "DRYRUN fake"),
    )
    sc = _scanner(cfg, quotes)
    ev = EventLog(cfg.data_dir)
    fake = Announcement(
        ticker, datetime.now().replace(second=0, microsecond=0), "DRYRUN: fake price-sensitive "
        "announcement", True, f"dry{datetime.now():%H%M%S}", "https://example.invalid/dryrun",
    )  # fmt: skip
    ev.append("announcements", {**fake.to_dict(), "in_universe": True, "dryrun": True})
    print(f"1. fake announcement recorded for {ticker}: {fake.headline}")
    props = sc.scan([fake])
    if not props:
        print("2. scanner produced no proposal (see data/events/signals.jsonl for the reason)")
        return 1
    p = props[0]
    print(f"2. proposal {p.id}: BUY {p.ticker} qty {p.qty} limit {p.entry_limit} stop {p.stop}")
    print("3. approval: in production this is YOUR approval on Telegram via OpenClaw exec "
          "approvals. The dry run continues only because broker mode is sim.")  # fmt: skip
    broker = SimBroker(
        cfg.data_dir, float(cfg.get("capital.starting_aud")), CostModel.from_config(cfg),
        lambda t: (last, 2_000_000.0),
    )  # fmt: skip
    limits = Limits.from_config(cfg, _universe_set(cfg) | {ticker})
    try:
        res = place_order(
            cfg, broker, limits, proposal_id=p.id, ticker=ticker, side="buy", qty=p.qty,
            limit=p.entry_limit,
        )  # fmt: skip
    except OrderRefused as e:
        print(f"4. place_order REFUSED: {e}")
        return 1
    print(f"4. broker says: order_id={res.order_id} status={res.status} filled={res.filled_qty} "
          f"avg_price={res.avg_price} commission={res.commission:.2f}")  # fmt: skip
    mm = reconcile(broker, ev, Alerts(cfg.data_dir))
    print(f"5. reconciliation: {'MISMATCH ' + str(mm) if mm else 'clean'}")
    if args.unwind and res.filled_qty:
        res2 = place_order(
            cfg, broker, limits, proposal_id=None, ticker=ticker, side="sell", qty=res.filled_qty,
            limit=round(last * 0.98, 3),
        )  # fmt: skip
        print(f"6. unwind: order_id={res2.order_id} status={res2.status} filled={res2.filled_qty}")
        mm = reconcile(broker, ev, Alerts(cfg.data_dir))
        print(f"7. reconciliation: {'MISMATCH ' + str(mm) if mm else 'clean'}")
    log.info("dryrun complete")
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
    bt = sub.add_parser("backtest", help="run the Phase 1 backtest and write reports/phase1.md")
    bt.add_argument("--universe", nargs="*", choices=["asx300", "small"])
    bt.set_defaults(fn=cmd_backtest)
    sub.add_parser("scan", help="check today's announcements and write proposals").set_defaults(
        fn=cmd_scan
    )
    pr = sub.add_parser("proposals", help="list proposals")
    pr.add_argument("--all", action="store_true", help="include non-pending")
    pr.add_argument("-v", "--verbose", action="store_true")
    pr.set_defaults(fn=cmd_proposals)
    po = sub.add_parser("place_order", help="send ONE limit order to the broker (needs approval)")
    po.add_argument("--id", required=False, help="proposal id (required for buys)")
    po.add_argument("--ticker", required=True)
    po.add_argument("--qty", required=True, type=int)
    po.add_argument("--limit", required=True, type=float)
    po.add_argument("--side", choices=["buy", "sell"], default="buy")
    po.set_defaults(fn=cmd_place_order)
    sub.add_parser("positions", help="broker positions and cash").set_defaults(fn=cmd_positions)
    sub.add_parser("reconcile", help="broker positions vs fills log").set_defaults(fn=cmd_reconcile)
    sub.add_parser("daily_report", help="one-screen daily summary").set_defaults(
        fn=cmd_daily_report
    )
    dr = sub.add_parser("dryrun", help="sim-only end-to-end run with a fake announcement")
    dr.add_argument("--ticker", default="BHP")
    dr.add_argument("--prev-close", type=float, default=40.0)
    dr.add_argument("--unwind", action="store_true", help="also sell the position back")
    dr.set_defaults(fn=cmd_dryrun)
    from asxbot.arena.cli import add_parsers as _arena_parsers

    _arena_parsers(sub)
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
