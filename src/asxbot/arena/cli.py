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
    log = setup_logging(cfg.logs_dir)
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


def positions_lines(arena, account: str | None = None) -> list[str]:
    """Open positions and pending fills per arena account, as `arena positions` prints them
    (the Trader chat's /positions sends the same lines). Plain code, fake money."""
    out = []
    for pb in arena.playbooks():
        for kind in ("agent", "bot"):
            acct = arena.account(pb, kind)
            if account and acct.name != account:
                continue
            prices = arena.broker.prices(acct)
            out.append(
                f"{acct.name}: cash {acct.cash:,.2f}  equity {acct.equity(prices):,.2f}  "
                f"today {arena.broker.day_loss_pct(acct):+.2f}%"
            )
            for t, p in acct.positions.items():
                px = prices.get(t, p.avg_cost)
                out.append(
                    f"  {t} {p.qty:+d} @ {p.avg_cost:.4f}  now {px:.4f}  "
                    f"open P&L {(px - p.avg_cost) * p.qty:+,.2f}  stop {p.stop}  "
                    f"target {p.target}  opened {p.opened_at} by {p.opened_by}"
                )
            pend = [o for o in acct.orders.values() if o.status == "pending_fill"]
            for o in pend:
                out.append(
                    f"  PENDING FILL {o.order_id} {o.side} {o.qty} {o.ticker} @ {o.limit} "
                    f"(decided {o.decided_at})"
                )
            if not acct.positions and not pend:
                out.append("  no positions, no pending orders")
    return out


def cmd_positions(args) -> int:
    cfg, log, arena = _arena()
    for line in positions_lines(arena, args.account):
        print(line)
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
            # One pass since 2026-09-24: entries, stops and targets are worked together, so
            # apply_exits reports them all and resolve_pending finds nothing left.
            for r in arena.broker.apply_exits(acct, now):
                print(f"{acct.name} {r.order_id}: {r.status} - {r.detail}")
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
    from asxbot.arena.agents import DECIDER, AgentCallFailed, call_agent, expected_model
    from asxbot.arena.report import agent_brief, gather, mark_report_sent, render_plain

    cfg, log, arena = _arena()
    facts = gather(arena)
    text = render_plain(facts)
    written_by = "code"

    if args.agent:
        try:
            reply = call_agent(
                DECIDER, agent_brief(facts), expect_model=expected_model(cfg, "decider"),
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

    # Deliver FIRST. Printing is a convenience; delivery is the point, and a console that
    # cannot encode what the agent wrote must never stop the report going out.
    sent_note, rc = "", 0
    if args.send:
        from asxbot.telegram import TelegramError, load_bot

        try:
            ids = load_bot(cfg).send(text)
            sent_note = f"[sent to Telegram: {len(ids)} message(s)]"
            mark_report_sent(cfg)  # the next report lists settings changed after this one
        except TelegramError as e:
            sent_note, rc = f"[Telegram NOT sent: {e}]", 3
            log.error("telegram delivery failed: %s", e)

    try:
        print(text)
    except UnicodeEncodeError:  # belt and braces; main() already forces UTF-8
        print(text.encode("utf-8", "replace").decode("utf-8"))
    if sent_note:
        print(sent_note)
    return rc


def watch_playbooks(arena) -> tuple:
    """What the scheduled watcher runs: every enabled playbook in one loop (from
    2026-09-24). The announcements playbook drives the announcement poll; the others (the
    day trader) run beside it."""
    pbs = arena.playbooks()
    ann = [p for p in pbs if p.key.startswith("asx_announcements")]
    if not ann:
        raise SystemExit("no announcements playbook is enabled in config.yaml")
    return ann[0], tuple(p for p in pbs if p is not ann[0])


def cmd_watch(args) -> int:
    from asxbot.arena.watch import watch

    cfg, log, arena = _arena()
    if args.playbook:
        pb, others = _pb(arena, args.playbook), ()
    else:
        pb, others = watch_playbooks(arena)
    if not pb.enabled:
        print(f"playbook {pb.key} is not enabled in config.yaml")
        return 2
    log.info("watching: %s", ", ".join(p.key for p in (pb, *others)))
    watch(arena, pb, once=args.once, interval_s=args.interval, until=args.until, others=others)
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
        run_agent=not args.no_agent, text=body, ignore_warmup=True, test=True,
    )  # fmt: skip
    print(json.dumps(result, indent=2, default=str))

    # Resolve the fill now, so the whole chain is visible in one run.
    print("\n--- resolving fills from the real minute bars ---")
    real_now = datetime.now(SYD)
    for kind in ("agent", "bot"):
        acct = arena.account(pb, kind)
        for r in arena.broker.resolve_pending(acct, real_now):
            print(f"{acct.name} {r.order_id}: {r.status} - {r.detail}")
        for r in arena.broker.apply_exits(acct, real_now):
            print(f"{acct.name} EXIT {r.order_id}: {r.status} - {r.detail}")
        acct = arena.account(pb, kind)
        for t, pos in acct.positions.items():
            print(f"{acct.name} holds {t} {pos.qty:+d} @ {pos.avg_cost:.4f} stop {pos.stop}")
    return 0


def cmd_replay(args) -> int:
    """The plumbing replay of the v2 and day-trader rule bots (never the agent)."""
    from datetime import date as date_cls

    from asxbot.arena.replay import run

    cfg = load_config()
    setup_logging(cfg.logs_dir)
    days = [date_cls.fromisoformat(d) for d in args.days.split(",")]
    delays = [int(x) for x in args.delays.split(",")]
    md = run(cfg, days, delays, cfg.root / "reports")
    print(f"written: {md}")
    print("PLUMBING REPLAY - plumbing test, not a go/no-go")
    return 0


def cmd_hours(args) -> int:
    """Today's announcement window, which moves with Sydney daylight saving."""
    from asxbot.arena.hours import describe

    cfg = load_config()
    setup_logging(cfg.logs_dir)
    day = datetime.fromisoformat(args.day).date() if args.day else None
    print(describe(cfg, day))
    return 0


def cmd_evening_due(args) -> int:
    """Exit 0 if now is today's evening-report slot. The scheduled task fires at both
    19:30 and 20:30; the wrong one for today exits here and does nothing."""
    from asxbot.arena.hours import evening_slot, is_dst, is_evening_slot

    cfg = load_config()
    setup_logging(cfg.logs_dir)
    now = datetime.now(SYD)
    if is_evening_slot(cfg, now):
        print(f"due: today's slot is {evening_slot(cfg, now.date()):%H:%M} Sydney")
        return 0
    print(
        f"not due: today's slot is {evening_slot(cfg, now.date()):%H:%M} Sydney "
        f"(daylight saving {'ON' if is_dst(now.date()) else 'off'}), now {now:%H:%M}"
    )
    return 1


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


def cmd_digest(args) -> int:
    """Send the hourly digest, or the end-of-session summary, right now.

    The watcher sends both by itself; this is for proving the delivery and for catching up
    after a restart. `--print-only` shows the text without sending it.
    """
    from asxbot.arena.notify import build_notifier
    from asxbot.arena.tally import counts_for, session_summary_text

    cfg, log, arena = _arena()
    pb = _pb(arena, args.playbook)
    now = datetime.now(SYD)
    if args.summary:
        text = session_summary_text(arena, pb, now)
        if args.print_only:
            print(text)
            return 0
        n = build_notifier(cfg)
        if args.again:
            state = n._state()
            state["summary_day"] = ""
            n._write_state(state)
        sent = n.session_summary(text, now)
        print("session summary sent" if sent else "today's summary has already been sent")
        return 0

    print(counts_for(cfg.data_dir, now.date()).line())
    if args.print_only:
        return 0
    sent = build_notifier(cfg).flush_passes(now, force=True)
    print(
        "digest sent"
        if sent
        else "nothing to send: nothing happened since the last digest, or today's summary has gone"
    )
    return 0


def cmd_filter_cost(args) -> int:
    """Measure what each screen filter threw away. Reports; never acts."""
    from asxbot.arena.filtercost import measure, render
    from asxbot.io import write_text_atomic

    cfg, log, arena = _arena()
    costs = measure(arena, weeks=args.weeks)
    text = render(costs, args.weeks)
    out = pathlib.Path(args.out) if args.out else (cfg.root / "reports" / "filter_cost.md")
    write_text_atomic(text, out)
    print(text)
    print(f"[written to {out}]")
    if args.send:
        from asxbot.arena.notify import build_notifier

        rows = " · ".join(
            f"{c.test} {c.measured}/{c.rejected} median {c.median:+.2f}%"
            for c in sorted(costs.values(), key=lambda c: -c.rejected)
            if c.measured
        )
        body = rows or "nothing measurable yet"
        build_notifier(cfg).send(
            f"📏 <b>Filter cost, last {args.weeks} week(s)</b>\n{body}\n"
            "<i>measurement only; no threshold moves on this</i>"
        )
    return 0


def cmd_selfcheck(args) -> int:
    """Run the self-checks now and print them. The watcher runs these every cycle."""
    from asxbot.arena.selfcheck import report, run_checks

    cfg, log, arena = _arena()
    pb = _pb(arena, args.playbook)
    if args.quiet:
        checks = run_checks(arena, pb)
    else:
        report(arena, pb, force=args.force)
        checks = run_checks(arena, pb)
    for c in checks:
        print(f"{'ok   ' if c.ok else 'FAIL '} {c.key:18s} {c.detail}")
    return 0 if all(c.ok for c in checks) else 3


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
    setup_logging(cfg.logs_dir)
    try:
        if args.tg_cmd == "whoami":
            bot = load_bot(cfg)
            me = bot.me()
            print(f"bot @{me.get('username')} ({me.get('first_name')}) token {bot.masked}")
            print(f"chat id: {bot.chat_id or 'NOT PAIRED - run: asxbot telegram pair'}")
        elif args.tg_cmd == "pair":
            print(f"paired with chat {pair(cfg, getattr(args, 'chat_id', None))}")
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
    w.add_argument(
        "--until", help='stop at this Sydney time, HH:MM, or "auto" for just before '
        "announcements end (19:25, or 20:25 on daylight saving)"
    )
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

    hr = a.add_parser("hours", help="today's announcement window (daylight-saving aware)")
    hr.add_argument("--day")
    hr.set_defaults(fn=cmd_hours)

    rp = a.add_parser(
        "replay", help="plumbing replay of the v2 and day-trader rule bots on cached bars"
    )
    rp.add_argument("--days", required=True, help="comma-separated YYYY-MM-DD")
    rp.add_argument("--delays", default="20,0", help="feed delays in minutes, e.g. 20,0")
    rp.set_defaults(fn=cmd_replay)

    a.add_parser(
        "evening-due", help="exit 0 if now is today's evening-report slot"
    ).set_defaults(fn=cmd_evening_due)

    pc = a.add_parser("preclose", help="settle Level 1 positions before the close")
    pc.add_argument("--playbook")
    pc.set_defaults(fn=cmd_preclose)

    dg = a.add_parser("digest", help="send the hourly digest or the end-of-session summary now")
    dg.add_argument("--playbook")
    dg.add_argument("--summary", action="store_true", help="the 16:10 end-of-session message")
    dg.add_argument("--print-only", action="store_true", help="show it, do not send it")
    dg.add_argument("--again", action="store_true", help="resend today's summary")
    dg.set_defaults(fn=cmd_digest)

    fc = a.add_parser("filter-cost", help="what each screen filter threw away (reports only)")
    fc.add_argument("--weeks", type=int, default=4)
    fc.add_argument("--out", help="where to write the report")
    fc.add_argument("--send", action="store_true", help="also send a one-line summary")
    fc.set_defaults(fn=cmd_filter_cost)

    sc = a.add_parser("selfcheck", help="run the system's checks on itself")
    sc.add_argument("--playbook")
    sc.add_argument("--quiet", action="store_true", help="print only; do not alert")
    sc.add_argument("--force", action="store_true", help="alert even if already alerted")
    sc.set_defaults(fn=cmd_selfcheck)

    rs = a.add_parser("reset", help="wipe arena accounts back to their opening balance")
    rs.add_argument("--yes", action="store_true", help="required: this deletes trade records")
    rs.add_argument("--account", help="only accounts whose name contains this")
    rs.set_defaults(fn=cmd_reset)

    tg = sub.add_parser("telegram", help="the trader bot's Telegram link")
    t = tg.add_subparsers(dest="tg_cmd", required=True)
    t.add_parser("whoami", help="which bot, and is a chat paired").set_defaults(fn=cmd_telegram)
    tp = t.add_parser("pair", help="set the chat the evening report goes to")
    tp.add_argument(
        "--chat-id",
        help="set it directly (needed once OpenClaw polls this bot as a channel, because "
        "OpenClaw consumes the updates this would otherwise read)",
    )
    tp.set_defaults(fn=cmd_telegram)
    ts = t.add_parser("send", help="send a message")
    ts.add_argument("text")
    ts.set_defaults(fn=cmd_telegram)


def _unused(_: argparse.Namespace) -> int:  # pragma: no cover
    return 0
