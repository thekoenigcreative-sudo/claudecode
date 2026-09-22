"""`asxbot arena ...` - the arena's command line.

These are also the trader agents' tools: everything the agents can do to the arena, they do
through one of these commands, and every one of them is plain code.
"""

from __future__ import annotations

import argparse
import json
import pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

from asxbot.config import load_config
from asxbot.log import setup_logging

SYD = ZoneInfo("Australia/Sydney")


def _arena():
    from asxbot.arena.runtime import build_arena

    cfg = load_config()
    log = setup_logging(cfg.data_dir)
    return cfg, log, build_arena(cfg)


def _pb(arena, key: str | None):
    pbs = arena.playbooks()
    if key:
        return arena.playbook(key)
    if not pbs:
        raise SystemExit("no playbook is enabled in config.yaml")
    return pbs[0]


def cmd_status(args) -> int:
    from asxbot.arena.scoreboard import score

    cfg, log, arena = _arena()
    print(f"arena: FAKE money, broker mode {cfg.broker}, data: {cfg.data_label()}")
    for pb in arena.playbooks(only_enabled=False):
        lvl = pb.level
        print(
            f"\nplaybook {pb.key} ({pb.title})  "
            f"{'ENABLED' if pb.enabled else 'disabled'}  status={pb.status}"
        )
        print(
            f"  level {lvl.number} ({lvl.name}): risk/trade {lvl.risk_per_trade_pct}%, "
            f"max {lvl.max_open_positions} positions, leverage {lvl.leverage(pb.market)}x, "
            f"daily loss limit {lvl.daily_loss_limit_pct}%, holding {lvl.holding}"
        )
        if pb.raw.get("warmup_start"):
            print(f"  warm-up starts: {pb.raw['warmup_start']}")
        for kind in ("agent", "bot"):
            acct = arena.account(pb, kind)
            s = score(arena.store, acct, arena.broker.prices(acct))
            print(
                f"  {kind:5s} {acct.name:28s} equity {s.equity:>10,.2f}  "
                f"P&L {s.pnl:>+9,.2f}  positions {s.open_positions}  "
                f"pending fills {s.pending_fills}"
            )
    return 0


def cmd_scoreboard(args) -> int:
    from asxbot.arena.scoreboard import agent_vs_bot, format_table, score

    cfg, log, arena = _arena()
    scores = []
    for pb in arena.playbooks():
        for kind in ("agent", "bot"):
            acct = arena.account(pb, kind)
            scores.append(score(arena.store, acct, arena.broker.prices(acct)))
    if args.json:
        print(json.dumps([s.to_dict() for s in scores], indent=2))
        return 0
    print(format_table(scores))
    print()
    for line in agent_vs_bot(scores):
        print(line)
    for s in scores:
        ok, reasons = s.passes_level_test()
        if s.days >= 1:
            print(
                f"{s.account}: level {s.level} test {'PASS' if ok else 'FAIL'}"
                + ("" if ok else " - " + "; ".join(reasons))
            )
    return 0


def cmd_positions(args) -> int:
    cfg, log, arena = _arena()
    for pb in arena.playbooks():
        for kind in ("agent", "bot"):
            acct = arena.account(pb, kind)
            if args.account and acct.name != args.account:
                continue
            prices = arena.broker.prices(acct)
            print(
                f"{acct.name}: cash {acct.cash:,.2f}  equity {acct.equity(prices):,.2f}  "
                f"today {arena.broker.day_loss_pct(acct):+.2f}%"
            )
            for t, p in acct.positions.items():
                px = prices.get(t, p.avg_cost)
                print(
                    f"  {t} {p.qty:+d} @ {p.avg_cost:.4f}  now {px:.4f}  "
                    f"open P&L {(px - p.avg_cost) * p.qty:+,.2f}  stop {p.stop}  "
                    f"opened {p.opened_at} by {p.opened_by}"
                )
            pend = [o for o in acct.orders.values() if o.status == "pending_fill"]
            for o in pend:
                print(
                    f"  PENDING FILL {o.order_id} {o.side} {o.qty} {o.ticker} @ {o.limit} "
                    f"(decided {o.decision_at})"
                )
            if not acct.positions and not pend:
                print("  no positions, no pending orders")
    return 0


def cmd_quote(args) -> int:
    from asxbot.arena.watch import live_reaction

    cfg, log, arena = _arena()
    print(json.dumps(live_reaction(arena, args.ticker.upper(), datetime.now(SYD)), indent=2))
    return 0


def cmd_dossier(args) -> int:
    from asxbot.arena.watch import dossier

    cfg, log, arena = _arena()
    print(json.dumps(dossier(arena, args.ticker.upper()), indent=2, default=str))
    return 0


def cmd_place_order(args) -> int:
    from asxbot.arena.orders import ArenaOrderRefused, arena_place_order

    cfg, log, arena = _arena()
    pb = _pb(arena, args.playbook)
    acct = arena.account(pb, args.by)
    try:
        o = arena_place_order(
            cfg, arena.broker, acct, pb,
            ticker=args.ticker, side=args.side, qty=args.qty, limit=args.limit, stop=args.stop,
            target=args.target, reason=args.reason, model=args.model, placed_by=args.by,
            universe=arena.universe, short_universe=arena.short_universe,
        )  # fmt: skip
    except ArenaOrderRefused as e:
        print(f"REFUSED: {e}")
        return 3
    print(
        f"order_id={o.order_id} status={o.status} {o.side} {o.ticker} {o.qty} @ {o.limit} "
        f"stop={o.stop}\n{o.message}"
    )
    return 0


def cmd_close(args) -> int:
    from asxbot.arena.orders import ArenaOrderRefused, arena_place_order

    cfg, log, arena = _arena()
    pb = _pb(arena, args.playbook)
    acct = arena.account(pb, args.by)
    pos = acct.positions.get(args.ticker.upper())
    if pos is None:
        print(f"no position in {args.ticker.upper()} on {acct.name}")
        return 3
    qty = args.qty or abs(pos.qty)
    side = "sell" if pos.qty > 0 else "cover"
    px = arena.broker.minutes.last_price(pos.ticker) or pos.avg_cost
    limit = args.limit or round(px * (0.97 if side == "sell" else 1.03), 3)
    try:
        o = arena_place_order(
            cfg, arena.broker, acct, pb,
            ticker=pos.ticker, side=side, qty=qty, limit=limit,
            reason=args.reason or "closed by hand", model=args.model, placed_by=args.by,
            universe=arena.universe, short_universe=arena.short_universe,
        )  # fmt: skip
    except ArenaOrderRefused as e:
        print(f"REFUSED: {e}")
        return 3
    print(f"order_id={o.order_id} {o.side} {o.ticker} {o.qty} @ {o.limit} - {o.message}")
    return 0


def cmd_resolve(args) -> int:
    cfg, log, arena = _arena()
    now = datetime.now(SYD)
    any_change = False
    for pb in arena.playbooks():
        for kind in ("agent", "bot"):
            acct = arena.account(pb, kind)
            for r in arena.broker.apply_stops(acct, now):
                print(f"{acct.name} STOP {r.order_id}: {r.status} - {r.detail}")
                any_change = True
            for r in arena.broker.resolve_pending(acct, now):
                print(f"{acct.name} {r.order_id}: {r.status} - {r.detail}")
                any_change = True
    if not any_change:
        print("nothing to resolve")
    return 0


def cmd_mark(args) -> int:
    cfg, log, arena = _arena()
    day = datetime.fromisoformat(args.day).date() if args.day else datetime.now(SYD).date()
    for pb in arena.playbooks():
        for kind in ("agent", "bot"):
            acct = arena.account(pb, kind)
            arena.broker.accrue_borrow(acct, day)
            m = arena.broker.mark_to_market(acct, day)
            print(
                f"{acct.name}: equity {m.equity:,.2f} cash {m.cash:,.2f} "
                f"exposure {m.gross_exposure:,.2f} positions {m.positions} "
                f"realised today {m.realised_day:+,.2f} fees today {m.fees_day:,.2f}"
            )
    return 0


def cmd_report(args) -> int:
    from asxbot.arena.agents import DECIDER, AgentCallFailed, call_agent
    from asxbot.arena.report import agent_brief, gather, render_plain
    from asxbot.arena.watch import DECIDER_MODEL

    cfg, log, arena = _arena()
    facts = gather(arena)
    text = render_plain(facts)
    written_by = "code"

    if args.agent:
        try:
            reply = call_agent(
                DECIDER, agent_brief(facts), expect_model=DECIDER_MODEL,
                data_dir=cfg.data_dir, purpose="evening report",
            )  # fmt: skip
            if reply.text.strip():
                text = reply.text.strip()
                written_by = reply.model or "agent"
            else:
                log.warning("the agent returned an empty report; sending the code version")
        except AgentCallFailed as e:
            log.error("agent report failed (%s); sending the code version", e)

    text += f"\n\n<i>written by: {written_by}</i>"
    print(text)
    if args.send:
        from asxbot.telegram import TelegramError, load_bot

        try:
            bot = load_bot(cfg)
            ids = bot.send(text)
            print(f"\n[sent to Telegram: {len(ids)} message(s)]")
        except TelegramError as e:
            print(f"\n[Telegram NOT sent: {e}]")
            return 3
    return 0


def cmd_watch(args) -> int:
    from asxbot.arena.watch import watch

    cfg, log, arena = _arena()
    pb = _pb(arena, args.playbook)
    if not pb.enabled:
        print(f"playbook {pb.key} is not enabled in config.yaml")
        return 2
    watch(arena, pb, once=args.once, interval_s=args.interval, until=args.until)
    return 0


def cmd_fake(args) -> int:
    """Prove the whole chain with a fake announcement. Nothing here touches asx.com.au."""
    from asxbot.announcements.model import Announcement
    from asxbot.arena.watch import handle_announcement
    from asxbot.live.quotes import Quote, StaticQuotes

    cfg, log, arena = _arena()
    pb = _pb(arena, args.playbook)
    ticker = args.ticker.upper()
    now = datetime.now(SYD)
    if args.at:
        h, m = (int(x) for x in args.at.split(":"))
        now = now.replace(hour=h, minute=m, second=0, microsecond=0)
        print(
            f"decision time set to {now:%Y-%m-%d %H:%M} Sydney, so the fill comes from "
            f"that minute's REAL bar"
        )

    quotes = None
    if args.prev_close:
        prev = float(args.prev_close)
        last = round(prev * (1 + args.move_pct / 100.0), 3)
        daily = arena.daily_lookup()(ticker)
        avg_vol = (
            float(daily["volume"].tail(20).mean()) if daily is not None and len(daily) >= 20
            else 1e6
        )  # fmt: skip
        quotes = StaticQuotes(
            {ticker: Quote(ticker, last, last, prev, avg_vol * args.vol_mult, now, "FAKE quote")},
            Quote("^AXJO", 8750.0, 8750.0, 8750.0, 0.0, now, "FAKE quote"),
        )
        print(
            f"using a FAKE quote: prev close {prev}, last {last} "
            f"({args.move_pct:+.1f}%), volume {args.vol_mult:.0f}x the 20-day average"
        )

    a = Announcement(
        code=ticker,
        released_at=now.replace(tzinfo=None, second=0, microsecond=0),
        headline=args.headline,
        price_sensitive=True,
        ids_id=f"FAKE{now:%H%M%S}",
        pdf_url="https://example.invalid/fake-announcement",
    )
    print(f"\nFAKE announcement: {a.code} - {a.headline}\n")
    body = ""
    if args.text_file:
        body = pathlib.Path(args.text_file).read_text(encoding="utf-8")
        print(f"supplying {len(body)} characters of FAKE announcement text to the reader")
    result = handle_announcement(
        arena, pb, a, now, quotes=quotes, run_bot=not args.no_bot,
        run_agent=not args.no_agent, text=body, ignore_warmup=True,
    )  # fmt: skip
    print(json.dumps(result, indent=2, default=str))

    # Resolve the fill now, so the whole chain is visible in one run.
    print("\n--- resolving fills from the real minute bars ---")
    real_now = datetime.now(SYD)
    for kind in ("agent", "bot"):
        acct = arena.account(pb, kind)
        for r in arena.broker.resolve_pending(acct, real_now):
            print(f"{acct.name} {r.order_id}: {r.status} - {r.detail}")
        for r in arena.broker.apply_stops(acct, real_now):
            print(f"{acct.name} STOP {r.order_id}: {r.status} - {r.detail}")
        acct = arena.account(pb, kind)
        for t, pos in acct.positions.items():
            print(f"{acct.name} holds {t} {pos.qty:+d} @ {pos.avg_cost:.4f} stop {pos.stop}")
    return 0


def cmd_preclose(args) -> int:
    """Ask the decider about each open position and close it unless it writes a reason."""
    from asxbot.arena.watch import sweep_before_close

    cfg, log, arena = _arena()
    pb = _pb(arena, args.playbook)
    results = sweep_before_close(arena, pb)
    if not results:
        print("nothing to settle (no open agent positions, or this level is not intraday)")
    for r in results:
        print(f"{r['ticker']}: {r['action']} - {r['reason']}")
    return 0


def cmd_reset(args) -> int:
    """Wipe arena accounts back to their opening balance. Fake money only, never live."""
    cfg, log, arena = _arena()
    if not args.yes:
        print("this deletes every arena account, mark and trade record. Re-run with --yes.")
        return 2
    root = cfg.data_dir / "arena"
    removed = []
    for sub in ("accounts", "marks", "handled"):
        d = root / sub
        if d.exists():
            for f in sorted(d.glob("*")):
                if args.account and args.account not in f.stem:
                    continue
                f.unlink()
                removed.append(f"{sub}/{f.name}")
    for name in removed:
        print(f"removed {name}")
    print(f"{len(removed)} file(s) removed; accounts reopen at their starting balance")
    log.info("arena reset: %d files removed", len(removed))
    return 0


def cmd_telegram(args) -> int:
    from asxbot.telegram import TelegramError, load_bot, pair

    cfg = load_config()
    setup_logging(cfg.data_dir)
    try:
        if args.tg_cmd == "whoami":
            bot = load_bot(cfg)
            me = bot.me()
            print(f"bot @{me.get('username')} ({me.get('first_name')}) token {bot.masked}")
            print(f"chat id: {bot.chat_id or 'NOT PAIRED - run: asxbot telegram pair'}")
        elif args.tg_cmd == "pair":
            print(f"paired with chat {pair(cfg)}")
        elif args.tg_cmd == "send":
            bot = load_bot(cfg)
            print(f"sent message ids {bot.send(args.text)}")
    except TelegramError as e:
        print(f"telegram error: {e}")
        return 3
    return 0


def add_parsers(sub) -> None:
    ar = sub.add_parser("arena", help="the fake-money arena (ARENA.md)")
    a = ar.add_subparsers(dest="arena_cmd", required=True)

    a.add_parser("status", help="playbooks, levels and accounts").set_defaults(fn=cmd_status)

    sb = a.add_parser("scoreboard", help="P&L, green/red days, drawdown, agent vs bot")
    sb.add_argument("--json", action="store_true")
    sb.set_defaults(fn=cmd_scoreboard)

    po = a.add_parser("positions", help="open positions and pending fills")
    po.add_argument("--account")
    po.set_defaults(fn=cmd_positions)

    q = a.add_parser("quote", help="the delayed quote and price reaction for one stock")
    q.add_argument("--ticker", required=True)
    q.set_defaults(fn=cmd_quote)

    do = a.add_parser("dossier", help="one-page brief on a stock")
    do.add_argument("--ticker", required=True)
    do.set_defaults(fn=cmd_dossier)

    pl = a.add_parser("place-order", help="send ONE order to the arena broker (fake money)")
    pl.add_argument("--playbook")
    pl.add_argument("--by", choices=["agent", "bot"], default="agent")
    pl.add_argument("--ticker", required=True)
    pl.add_argument("--side", choices=["buy", "sell", "short", "cover"], required=True)
    pl.add_argument("--qty", type=int, required=True)
    pl.add_argument("--limit", type=float, required=True)
    pl.add_argument("--stop", type=float)
    pl.add_argument("--target", type=float)
    pl.add_argument("--reason", default="")
    pl.add_argument("--model", default="")
    pl.set_defaults(fn=cmd_place_order)

    cl = a.add_parser("close", help="close a position")
    cl.add_argument("--playbook")
    cl.add_argument("--by", choices=["agent", "bot"], default="agent")
    cl.add_argument("--ticker", required=True)
    cl.add_argument("--qty", type=int)
    cl.add_argument("--limit", type=float)
    cl.add_argument("--reason", default="")
    cl.add_argument("--model", default="")
    cl.set_defaults(fn=cmd_close)

    a.add_parser("resolve", help="fill pending orders and trigger stops").set_defaults(
        fn=cmd_resolve
    )

    mk = a.add_parser("mark", help="mark every account to market for the day")
    mk.add_argument("--day")
    mk.set_defaults(fn=cmd_mark)

    rp = a.add_parser("report", help="the evening report")
    rp.add_argument("--send", action="store_true", help="deliver it on Telegram")
    rp.add_argument("--agent", action="store_true", help="let the decider write it")
    rp.set_defaults(fn=cmd_report)

    w = a.add_parser("watch", help="poll announcements and run the agents on each one")
    w.add_argument("--playbook")
    w.add_argument("--once", action="store_true")
    w.add_argument("--interval", type=float)
    w.add_argument("--until", help="stop at this Sydney time, HH:MM (for the daily task)")
    w.set_defaults(fn=cmd_watch)

    fk = a.add_parser("fake-announcement", help="prove the chain end to end, no network")
    fk.add_argument("--playbook")
    fk.add_argument("--ticker", required=True)
    fk.add_argument("--headline", default="FAKE: material contract awarded (test)")
    fk.add_argument("--prev-close", type=float, help="use a fake quote built from this close")
    fk.add_argument("--move-pct", type=float, default=12.0)
    fk.add_argument("--vol-mult", type=float, default=8.0)
    fk.add_argument("--at", help="decision time today, HH:MM Sydney (default: now)")
    fk.add_argument("--text-file", help="file holding the fake announcement's text")
    fk.add_argument("--no-bot", action="store_true")
    fk.add_argument("--no-agent", action="store_true")
    fk.set_defaults(fn=cmd_fake)

    pc = a.add_parser("preclose", help="settle Level 1 positions before the close")
    pc.add_argument("--playbook")
    pc.set_defaults(fn=cmd_preclose)

    rs = a.add_parser("reset", help="wipe arena accounts back to their opening balance")
    rs.add_argument("--yes", action="store_true", help="required: this deletes trade records")
    rs.add_argument("--account", help="only accounts whose name contains this")
    rs.set_defaults(fn=cmd_reset)

    tg = sub.add_parser("telegram", help="the trader bot's Telegram link")
    t = tg.add_subparsers(dest="tg_cmd", required=True)
    t.add_parser("whoami", help="which bot, and is a chat paired").set_defaults(fn=cmd_telegram)
    t.add_parser("pair", help="learn the chat id from a message you sent").set_defaults(
        fn=cmd_telegram
    )
    ts = t.add_parser("send", help="send a message")
    ts.add_argument("text")
    ts.set_defaults(fn=cmd_telegram)


def _unused(_: argparse.Namespace) -> int:  # pragma: no cover
    return 0
