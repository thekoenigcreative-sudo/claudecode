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


def _arena(log_file: str | None = None, cfg=None):
    from asxbot.arena.runtime import build_arena

    cfg = cfg or load_config()
    log = setup_logging(cfg.logs_dir, **({"filename": log_file} if log_file else {}))
    return cfg, log, build_arena(cfg)


def _event(cfg, kind: str, rec: dict) -> None:
    """A record of a guarded command, in data/events (best effort)."""
    from asxbot.log import EventLog

    try:
        EventLog(cfg.data_dir).append(kind, rec)
    except OSError as e:
        print(f"(could not record the {kind} event: {e})")


def _testing(arena) -> list:
    """Every enabled playbook in its frozen test (status: test)."""
    return [p for p in arena.playbooks() if str(p.status) == "test"]


def _hand_order_refused(cfg, arena, pb, args, what: str) -> str:
    """Why a hand-placed order (arena place-order / close) is refused, or "" (26 Sep 2026).

    `--by bot` is never allowed: the bot's book is the frozen rule bot's alone, and an order
    placed there by hand is a trade the yardstick never made. In a playbook's test, an order
    by hand in the agent's book needs --force and a written reason, and is recorded; the
    watcher's own orders do not come through here."""
    if args.by == "bot":
        return ("REFUSED: --by bot places an order in the rule bot's book, which only the "
                "frozen rule itself trades. Nothing was placed.")  # fmt: skip
    if str(pb.status) == "test":
        reason = (getattr(args, "reason", "") or "").strip()
        if not (getattr(args, "force", False) and reason):
            return (f"REFUSED: {pb.key} is in its frozen test; an order by hand ({what}) "
                    'needs --force --reason "why" (it is recorded). Nothing was placed.')
        _event(cfg, "arena_hand_orders", {"playbook": pb.key, "what": what, "by": args.by,
                                          "ticker": str(args.ticker).upper(),
                                          "why": reason})  # fmt: skip
    return ""


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
    refused = _hand_order_refused(cfg, arena, pb, args, f"place-order {args.side}")
    if refused:
        print(refused)
        return 3
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
    refused = _hand_order_refused(cfg, arena, pb, args, "close")
    if refused:
        print(refused)
        return 3
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
    from asxbot.arena.agents import (
        DECIDER,
        AgentCallFailed,
        call_agent,
        expected_model,
        fresh_sessions_on,
    )
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
                fresh_session=fresh_sessions_on(cfg),
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


EXIT_ALREADY_WATCHING = 4


def cmd_watch(args) -> int:
    from asxbot.arena.watch import WatcherAlreadyRunning, watch
    from asxbot.log import WATCH_LOG

    # The watcher logs to its own file (26 Sep 2026): its self-checks read only its lines.
    cfg, log, arena = _arena(log_file=WATCH_LOG)
    if args.playbook:
        pb, others = _pb(arena, args.playbook), ()
    else:
        pb, others = watch_playbooks(arena)
    if not pb.enabled:
        print(f"playbook {pb.key} is not enabled in config.yaml")
        return 2
    log.info("watching: %s", ", ".join(p.key for p in (pb, *others)))
    try:
        watch(arena, pb, once=args.once, interval_s=args.interval, until=args.until,
              others=others)  # fmt: skip
    except WatcherAlreadyRunning as e:
        log.error("REFUSING TO START: %s", e)
        print(f"REFUSED: {e}")
        return EXIT_ALREADY_WATCHING
    return 0


def cmd_fake(args) -> int:
    """Prove the whole chain with a fake announcement. Nothing here touches asx.com.au."""
    from asxbot.announcements.model import Announcement
    from asxbot.arena.watch import handle_announcement
    from asxbot.config import Config
    from asxbot.live.quotes import Quote, StaticQuotes

    cfg = load_config()
    if args.data_dir:
        # A scratch arena (26 Sep 2026): its own books, events and queue; no Telegram, and
        # Yahoo's quotes rather than a second connection to IB Gateway beside the watcher's.
        raw = {**cfg.raw, "data": {**(cfg.raw.get("data") or {}), "dir": str(args.data_dir),
                                   "live_provider": "yfinance"}}  # fmt: skip
        arena_raw = dict(raw.get("arena") or {})
        arena_raw["alerts"] = {**(arena_raw.get("alerts") or {}), "telegram": False}
        raw["arena"] = arena_raw
        cfg = Config(raw, cfg.root, cfg.env, None)
        print(f"scratch arena in {cfg.data_dir}: nothing here touches the live books")
    cfg, log, arena = _arena(cfg=cfg)
    if not args.data_dir and _testing(arena):
        # 26 Sep 2026: a fake announcement run on the live data wrote into the frozen test -
        # queue entries, reader and decider records, orders in the test books, Telegram.
        names = ", ".join(p.key for p in _testing(arena))
        print(f"REFUSED: {names} is in its frozen test, and a fake announcement would write "
              "into its books, queue and records. Use --data-dir <a scratch folder>.")
        return 3
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


def cmd_replay_ibkr(args) -> int:
    """The replay of the frozen rule bots over IBKR 1-minute history (replay_ibkr.py)."""
    from datetime import date as date_cls
    from pathlib import Path as _P

    from asxbot.arena import replay_ibkr as RI

    cfg = load_config()
    setup_logging(cfg.logs_dir)
    codes = [c.strip().upper() for c in args.codes.split(",")] if args.codes else None
    if args.worker:
        days = [date_cls.fromisoformat(d) for d in args.days.split(",")]
        return RI.worker_main(cfg, days, _P(args.json), codes or [])
    first, last = date_cls.fromisoformat(args.from_day), date_cls.fromisoformat(args.to_day)
    md = RI.run(cfg, first, last, workers=int(args.workers), codes=codes)
    print(f"written: {md}")
    print(RI.LABEL)
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
    """Exit 0 if the evening routine should run now: in today's slot, or late the same
    evening when today's report has not gone (a start the PC missed at 19:30; hours.
    evening_due, 26 Sep 2026). The scheduled task fires at both 19:30 and 20:30; the wrong
    one for today exits here and does nothing."""
    from asxbot.arena.hours import evening_due, is_dst
    from asxbot.arena.report import last_report_sent

    cfg = load_config()
    setup_logging(cfg.logs_dir)
    now = datetime.now(SYD)
    due, why = evening_due(cfg, now, last_report_sent(cfg))
    print(f"{why} (daylight saving {'ON' if is_dst(now.date()) else 'off'})")
    return 0 if due else 1


def cmd_preclose(args) -> int:
    """Ask the decider about each open position and close it unless it writes a reason."""
    from asxbot.arena.watch import sweep_before_close

    cfg, log, arena = _arena()
    pb = _pb(arena, args.playbook)
    if pb.flat_at_close:
        # 26 Sep 2026: this is v1's hold-or-close sweep. Run on a flat-at-close playbook (v2,
        # the day trader) it asked the decider about the live test book, at any hour, and
        # could hold a position the playbook's own rule closes by code from 15:50.
        print(f"REFUSED: {pb.key} is flat at the close; the watcher closes it by code from "
              "the pre-close sweep time (v2_flow.flatten). Nothing was asked or placed.")
        return 3
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
            f"{c.test} ({c.playbook or '?'}) {c.measured}/{c.rejected} median {c.median:+.2f}%"
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
    """Wipe arena accounts back to their opening balance. Fake money only, never live.

    Refused (26 Sep 2026) inside the market-hours lock, and for any account of a playbook
    in its frozen test (status: test): `reset --yes` deleted the test's books, marks and
    the day's handled list, and the record of the test with them. Every reset and every
    refusal is an `arena_reset` event."""
    from asxbot.arena.lock import in_market_lock

    cfg, log, arena = _arena()
    if not args.yes:
        print("this deletes every arena account, mark and trade record. Re-run with --yes.")
        return 2
    now = datetime.now(SYD)
    protected = [n for p in arena.playbooks(only_enabled=False) if str(p.status) == "test"
                 for n in (p.agent_account, p.bot_account)]  # fmt: skip
    hit = [n for n in protected if not args.account or args.account in n]
    why = ""
    if in_market_lock(now, cfg=cfg):
        why = f"{now:%H:%M} Sydney is inside the market-hours lock on a trading day"
    elif hit:
        why = f"it would delete the frozen test's books: {', '.join(hit)}"
    if why:
        _event(cfg, "arena_reset", {"refused": why, "account": args.account or "(all)"})
        print(f"REFUSED: {why}. Nothing was removed.")
        return 3
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
    _event(cfg, "arena_reset", {"removed": removed, "account": args.account or "(all)"})
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
    pl.add_argument("--by", choices=["agent", "bot"], default="agent",
                    help="bot is refused: only the frozen rule trades the bot's book")
    pl.add_argument("--force", action="store_true",
                    help="allow an order by hand in a playbook's test (needs --reason)")
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
    cl.add_argument("--by", choices=["agent", "bot"], default="agent",
                    help="bot is refused: only the frozen rule trades the bot's book")
    cl.add_argument("--force", action="store_true",
                    help="allow a close by hand in a playbook's test (needs --reason)")
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
        "--until", help='stop at this Sydney time, HH:MM, or "auto" for just after '
        "announcements end, with one last poll (19:31, or 20:31 on daylight saving)"
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
    fk.add_argument("--data-dir", help="a scratch data folder; required while a playbook is "
                    "in its test (the live books are never written)")  # fmt: skip
    fk.set_defaults(fn=cmd_fake)

    hr = a.add_parser("hours", help="today's announcement window (daylight-saving aware)")
    hr.add_argument("--day")
    hr.set_defaults(fn=cmd_hours)

    ri = a.add_parser(
        "replay-ibkr",
        help="REPLAY the frozen v2 and day-trader rule bots over IBKR 1-minute history "
        "(scripts/ibkr_fetch_history.py fetches it); a scorecard per playbook, labelled",
    )
    ri.add_argument("--from", dest="from_day", help="first day, YYYY-MM-DD")
    ri.add_argument("--to", dest="to_day", help="last day, YYYY-MM-DD")
    ri.add_argument("--workers", type=int, default=1, help="child processes to split days over")
    ri.add_argument("--codes", help="comma-separated codes (default: today's ASX 300 list)")
    ri.add_argument("--worker", action="store_true", help="(internal) one slice of days")
    ri.add_argument("--days", help="(internal) comma-separated days for --worker")
    ri.add_argument("--json", help="(internal) where a worker writes its results")
    ri.set_defaults(fn=cmd_replay_ibkr)

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
