"""Announcements v2 in the watcher: the decider's v2 packet, the reaction look, the v2 rule
bot's day, and the flat sweep before the close. The rules are config.yaml
`asx_announcements_v2` (frozen 2026-09-24, before it ran); the arithmetic is reaction_v2.py.

The decider's question in v2 (Rick, 24 Sep): "over the rest of today, is the expected move in
this direction bigger than about 0.4% after costs, and where is the stop?" - not "is the news
mispriced". The honesty rules are v1's: the text is untrusted, the index list is
authoritative, the case against is argued, and passing is always allowed.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from datetime import time as time_cls
from zoneinfo import ZoneInfo

from asxbot.announcements.model import Announcement
from asxbot.arena import notify
from asxbot.arena.accounts import Account
from asxbot.arena.agents import DECIDER, READER, AgentCallFailed, call_agent, parse_decision
from asxbot.arena.broker import OPENING_SIDES
from asxbot.arena.intraday import MarketView
from asxbot.arena.levels import Playbook
from asxbot.arena.liquid import size_rule
from asxbot.arena.orders import ArenaOrderRefused, arena_place_order
from asxbot.arena.reaction_v2 import (
    bot_order_terms,
    load_bot_state,
    load_queue,
    look_status,
    reaction,
    round_trip_cost_pct,
    save_bot_state,
    save_queue,
    v2_bot_candidates,
    v2_bot_signal,
    wakes,
)
from asxbot.live.scanner import round_to_tick
from asxbot.log import EventLog, get_logger

log = get_logger("asxbot.arena.v2")
DELAYED = "delayed data - rehearsal until IBKR live prices"
SYD = ZoneInfo("Australia/Sydney")


# --------------------------------------------------------------------------
# the decider's v2 packet
# --------------------------------------------------------------------------
def decider_packet_v2(
    arena,
    pb: Playbook,
    acct: Account,
    code: str,
    news: list[dict],
    ctx: dict,
    summaries: list[str],
    now: datetime,
    stage: str,
) -> str:
    """`stage` is "pre_open" (the market has not traded the news) or "reaction"."""
    from asxbot.arena.watch import UNTRUSTED, _membership, _minutes_waiting

    lvl = pb.level
    prices = arena.broker.prices(acct)
    equity = acct.equity(prices)
    loss_today = arena.broker.day_loss_pct(acct)
    min_net = float((pb.raw.get("decider") or {}).get("min_expected_net_move_pct", 0.4))
    max_pos = lvl.max_position_aud or equity * 0.4
    positions = [
        {
            "ticker": t,
            "qty": p.qty,
            "avg_cost": round(p.avg_cost, 4),
            "stop": p.stop,
            "target": p.target,
            "opened": p.opened_at,
        }
        for t, p in acct.positions.items()
    ]
    pending = [
        {
            "ticker": o.ticker,
            "side": o.side,
            "qty": o.qty,
            "limit": o.limit,
            "filled_so_far": o.filled_qty,
            "minutes_waiting": _minutes_waiting(o.decided_at, now),
        }
        for o in acct.orders.values()
        if o.status == "pending_fill"
    ]
    summary_block = "\n\n---\n\n".join(summaries) if summaries else "(no reader summary)"
    news_lines = "\n".join(
        f"  {n['released']} Sydney  {n['headline']}  ({n.get('link', '')})" for n in news
    )
    if stage == "pre_open":
        when = (
            "PRE-OPEN LOOK. The market has not traded this news yet. An order recorded before "
            "09:59 joins the opening auction and fills at its single price (at most "
            f"{float(getattr(arena.broker, 'auction_volume_share', 0.2)):.0%} of its estimated "
            "volume there). You will get ONE more look once the market has traded the news "
            "for 10 minutes (the reaction look), whatever you decide now."
        )
    else:
        when = (
            "REACTION LOOK - your one look at this stock today. The market has traded this "
            "news; the reaction below is measured from 1-minute bars (move against the ASX "
            "200, volume against the stock's usual volume over the same minutes of the day). "
            f"Its newest bar is {ctx['reaction'].get('as_of_bar', '?')}. The bars are DELAYED "
            "(~20 minutes on the free feed): your order fills at the first bar after it is "
            "recorded, so the price you get is later than the last price you see."
        )
    return f"""You are trader-decider, announcements playbook VERSION 2: trade the reaction.

This is the FAKE-MONEY arena. Every limit below is enforced in code; an order outside them is
refused. Data: {ctx["reaction"].get("data_label", DELAYED)}.

{UNTRUSTED}

{when}

YOU ARE DECIDING AT: {now:%Y-%m-%d %H:%M} Sydney

THE NEWS ({code}, price sensitive)
{news_lines}

READER'S SUMMARY (trader-reader, Sonnet 5)
BEGIN READER SUMMARY
{summary_block}
END READER SUMMARY

COMPANY DOSSIER
{json.dumps(ctx["dossier"], indent=2, default=str)}

INDEX MEMBERSHIP - authoritative and dated
  {_membership(arena, code)}
  Do not overrule it from memory. A doubt goes only in "flag_for_claude".

THE REACTION (1-minute bars, plain code)
{json.dumps(ctx["reaction"], indent=2, default=str)}

THE OPENING AUCTION
{json.dumps(ctx.get("auction") or {"available": False}, indent=2, default=str)}

COSTS for a ${max_pos:,.0f} position in {code}: about {ctx.get("round_trip_cost_pct", "?")}% for
the round trip (IBKR brokerage both ways, $6.60 minimum; slippage both ways, scaled to liquidity).

YOUR ACCOUNT ({acct.name}, level {lvl.number} - {lvl.name}, aim: {lvl.aim})
  equity {equity:,.2f}   cash {acct.cash:,.2f}   today {loss_today:+.2f}%
  open positions: {json.dumps(positions, default=str)}
  orders waiting to fill: {json.dumps(pending, default=str)}

WHAT THE CODE WILL ALLOW
  - a position of at most ${max_pos:,.0f}; at most {lvl.max_open_positions} open and
    {lvl.max_new_positions_per_day} new today; our order under 5% of the stock's median
    daily turnover
  - risk (|entry - stop| x shares) at most {pb.risk_per_trade_pct}% of equity
    = {equity * pb.risk_per_trade_pct / 100:,.2f}
  - long anything in the universe; SHORT only ASX 200 members
  - every trade needs a stop; code enforces the stop and any target from the minute after
    your fill (a stop fills at the stop or the bar's open if it gapped through, less slippage)
  - INTRADAY ONLY: code closes every position at 15:50 without asking; no new positions
    after 15:40. There is no overnight hold in v2.

THE QUESTION
  Over the rest of today (to the 15:50 close-out), is the expected move in this direction
  bigger than about {min_net}% AFTER COSTS - and where is the stop?
  Not "is the news mispriced". Work through it and show it:
  1. WHAT THE MARKET HAS DONE: the move against the index, the volume against usual, the
     auction price against the previous close, where it trades against VWAP and its range.
  2. DOES THE REACTION FIT THE NEWS: too little, about right, or already overdone?
  3. BASE RATE: how do reactions like this usually behave over the rest of a day -
     continue, stall, or fade? Start there, then adjust.
  4. THE STOP: the price that proves you wrong (for example beyond the reaction's low or
     high). Is it close enough for the risk budget and far enough not to be noise?
  5. COSTS: the gross move must beat the round trip above plus {min_net}%.
  6. THE CASE AGAINST: argue it before you trade.
  7. Your expected move over the rest of today, gross and after costs, and your confidence.

End with one JSON block and nothing after it:

{{"action": "trade" | "pass",
  "side": "buy" | "short",
  "ticker": "{code}",
  "qty": <whole shares>,
  "limit": <limit price>,
  "stop": <stop price>,
  "target": <take-profit price - code closes the whole position there - or null>,
  "confidence_pct": <0-100>,
  "expected_move_rest_of_day_pct": <gross, in the trade's direction>,
  "expected_net_after_costs_pct": <after the round-trip costs>,
  "why": "<two or three sentences: the thesis and the strongest argument against>",
  "flag_for_claude": "<optional: a dated fact in this packet you believe is wrong>"}}

If you pass, {{"action": "pass", "why": "..."}} is enough.

THE BAR: trade when your expected move after costs is at least {min_net}% and you can name
the stop - positive expected value after costs, not certainty. Code refuses a trade whose
own stated "expected_net_after_costs_pct" is below {min_net}. Size by confidence within the
limits. Never trade to look busy; do not privately apply a higher bar than this - the ladder
steps the playbook down if daily profit does not hold, and that test needs your honest call.
"""


def auction_info(arena, code: str, day: date) -> dict:
    """The opening auction's price (Yahoo's daily open; TRACKER #28/#34: checked, not
    confirmed) against the previous close, if the feed has it."""
    try:
        row = arena.broker.minutes.fetch_daily_row(code, day)
    except Exception:  # noqa: BLE001
        row = None
    if not row or not row.get("open"):
        return {"available": False, "why": "Yahoo has no daily bar with an open yet"}
    return {
        "available": True,
        "price": round(float(row["open"]), 4),
        "basis": "Yahoo's daily open (checked, not confirmed against the ASX)",
    }


def reader_summaries(data_dir, day: date, ids: list[str]) -> list[str]:
    out = []
    for r in EventLog(data_dir).read("arena_decisions"):
        if r.get("stage") != "reader" or str(r.get("ids_id")) not in ids:
            continue
        if str(r.get("ts", ""))[:10] != day.isoformat():
            continue
        if r.get("summary"):
            out.append(str(r["summary"]))
    return out


def _has_exposure(acct: Account, code: str) -> bool:
    if code in acct.positions:
        return True
    return any(
        o.working and o.ticker == code and o.side in OPENING_SIDES for o in acct.orders.values()
    )


def good_till(pb: Playbook, when: datetime) -> datetime | None:
    """The last-entry minute of the session an order recorded at `when` works in (an order
    recorded after the close works in the next session)."""
    from asxbot.arena.broker import _session_day

    t = pb.last_entry_time
    return None if t is None else datetime.combine(_session_day(when), t, tzinfo=SYD)


def place_decision(
    arena,
    pb: Playbook,
    acct: Account,
    code: str,
    d: dict,
    model: str,
    seen_at: datetime,
    stage: str,
) -> dict:
    """The decider's v2 block through the hard limits. Returns what happened."""
    alert = notify.get(arena)
    min_net = float((pb.raw.get("decider") or {}).get("min_expected_net_move_pct", 0.4))
    try:
        net = float(d.get("expected_net_after_costs_pct"))
    except (TypeError, ValueError):
        net = None
    if net is None or net < min_net:
        why = (
            "the decider's own expected move after costs "
            f"({d.get('expected_net_after_costs_pct')}) is below the {min_net}% bar it was given"
        )
        if alert:
            alert.refused("agent", code, str(d.get("side")), d.get("qty", "?"), why)
        return {"refused": why}
    try:
        side = str(d.get("side", "buy"))
        qty = int(d["qty"])
        limit = round_to_tick(float(d["limit"]), up=side == "buy")
        stop = round_to_tick(float(d["stop"]), up=side != "buy")
    except (KeyError, TypeError, ValueError) as e:
        return {"refused": f"the decision block was not usable: {e}"}
    reason = (
        f"[v2 {stage}] {d.get('why', '')} [confidence {d.get('confidence_pct')}%, "
        f"expected {d.get('expected_move_rest_of_day_pct')}% gross, {net}% net]"
    )
    try:
        o = arena_place_order(
            arena.cfg,
            arena.broker,
            acct,
            pb,
            ticker=str(d.get("ticker", code)),
            side=side,
            qty=qty,
            limit=limit,
            stop=stop,
            target=float(d["target"]) if d.get("target") not in (None, "") else None,
            reason=reason,
            model=model,
            placed_by="agent",
            hold="intraday",
            universe=arena.universe,
            short_universe=arena.short_universe,
            now=seen_at,
            good_till=good_till(pb, seen_at),
        )
        if alert:
            alert.decided(o, d.get("confidence_pct"))
        return {
            "order_id": o.order_id,
            "ticker": o.ticker,
            "side": o.side,
            "qty": o.qty,
            "limit": o.limit,
            "stop": o.stop,
        }
    except ArenaOrderRefused as e:
        if alert:
            alert.refused("agent", code, side, qty, str(e))
        return {"refused": str(e)}


# --------------------------------------------------------------------------
# the reaction look
# --------------------------------------------------------------------------
def reaction_looks(arena, pb: Playbook, view: MarketView, now: datetime | None = None) -> list:
    """Every queued stock whose look is due gets it: one decider call each, most interesting
    first. Stocks the agent already holds, or has an entry working in, are skipped."""
    now = now or arena.broker.clock()
    local = now.astimezone(SYD).time()
    if local < time_cls(10, 0) or local > time_cls(16, 40):
        return []  # no trading to look at: do not poll the feed
    cfg = arena.cfg
    day = view.day
    ev = EventLog(cfg.data_dir)
    q = load_queue(cfg.data_dir, day)
    if not q:
        return []
    acct = arena.account(pb, "agent")
    due = []
    for code, item in q.items():
        if code.startswith("_") or item.get("status") != "queued":
            continue
        if _has_exposure(acct, code):
            item.update(status="held", why="the agent already holds it or has an entry working")
            ev.append("v2_reaction", {"ticker": code, **_brief(item)})
            continue
        st = look_status(view, item, now, pb)
        if st.status == "wait":
            continue
        if st.status in ("halted", "missed"):
            item.update(status=st.status, why=st.why)
            ev.append("v2_reaction", {"ticker": code, **_brief(item)})
            continue
        r = reaction(view, code, now, st.ref, st.base)
        ok, why = wakes(r, pb)
        item["reaction"] = {k: v for k, v in r.items() if k != "bars"}
        if not ok:
            item.update(status="quiet", why=why)
            ev.append("v2_reaction", {"ticker": code, **_brief(item)})
            continue
        vol = r.get("volume_vs_usual_same_minutes") or 1.0
        due.append((abs(float(r["move_vs_index_pct"])) * float(vol), code, st, r, why))
    save_queue(cfg.data_dir, day, q)

    out = []
    for _score, code, _st, _r, why in sorted(due, key=lambda x: -x[0]):
        item = q[code]
        fresh = look_status(view, item, arena.broker.clock(), pb)
        if fresh.status != "ready":
            item.update(
                status=fresh.status if fresh.status != "wait" else "missed",
                why=f"by the time its turn came: {fresh.why}",
            )
            save_queue(cfg.data_dir, day, q)
            ev.append("v2_reaction", {"ticker": code, **_brief(item)})
            continue
        item["status"] = "looking"
        save_queue(cfg.data_dir, day, q)
        try:
            res = look(arena, pb, view, code, item, fresh, why)
        except Exception as e:  # noqa: BLE001
            log.exception("reaction look on %s failed: %s", code, e)
            res = {"error": str(e)}
        item.update(status="looked", outcome=res)
        save_queue(cfg.data_dir, day, q)
        ev.append("v2_reaction", {"ticker": code, **_brief(item)})
        out.append({"ticker": code, **res})
    return out


def _brief(item: dict) -> dict:
    return {
        k: item.get(k)
        for k in ("status", "why", "ids", "headlines", "outcome", "reaction")
        if item.get(k) is not None
    }


def look(arena, pb: Playbook, view: MarketView, code: str, item: dict, st, why: str) -> dict:
    from asxbot.arena.watch import (
        DECIDER_MODEL,
        READER_MODEL,
        announcement_by_id,
        dossier,
        pdf_text_why,
        reader_packet,
    )

    cfg = arena.cfg
    day = view.day
    ev = EventLog(cfg.data_dir)
    seen_at = arena.broker.clock()
    r = reaction(view, code, seen_at, st.ref, st.base)
    ctx = {
        "dossier": dossier(arena, code),
        "reaction": r,
        "auction": auction_info(arena, code, day),
    }
    adv = arena.broker.adv_lookup(code) if arena.broker.adv_lookup else None
    size = pb.level.max_position_aud or 5000.0
    ctx["round_trip_cost_pct"] = round_trip_cost_pct(arena.broker.costs, size, adv)

    summaries = reader_summaries(cfg.data_dir, day, item["ids"])
    news = []
    for ids in item["ids"]:
        a = announcement_by_id(cfg, day, ids) or _from_other_day(cfg, day, ids)
        if a is None:
            continue
        news.append(
            {
                "released": f"{a.released_at:%Y-%m-%d %H:%M}",
                "headline": a.headline,
                "link": a.pdf_url,
                "a": a,
            }
        )
    if not summaries and news:
        a = news[0]["a"]
        text, _ = pdf_text_why(cfg.data_dir, a)
        try:
            reply = call_agent(
                READER,
                reader_packet(arena, a, {**ctx, "text": text}),
                expect_model=READER_MODEL,
                data_dir=cfg.data_dir,
                purpose=f"read {a.code} {a.ids_id} (v2 reaction look)",
            )
            summaries = [reply.text]
            ev.append(
                "arena_decisions",
                {
                    "stage": "reader",
                    "ticker": code,
                    "ids_id": a.ids_id,
                    "model": reply.model,
                    "summary": reply.text,
                    "v2": "reaction",
                },
            )
        except AgentCallFailed as e:
            log.error("reader failed on the reaction look for %s: %s", code, e)
    acct = arena.account(pb, "agent")
    packet = decider_packet_v2(
        arena,
        pb,
        acct,
        code,
        [{k: v for k, v in n.items() if k != "a"} for n in news],
        ctx,
        summaries,
        seen_at,
        "reaction",
    )
    log.info("REACTION LOOK %s: %s", code, why)
    try:
        reply = call_agent(
            DECIDER,
            packet,
            expect_model=DECIDER_MODEL,
            data_dir=cfg.data_dir,
            purpose=f"v2 reaction look {code}",
            timeout_s=240,
        )
    except AgentCallFailed as e:
        ev.append(
            "arena_decisions",
            {"ticker": code, "outcome": "decider_failed", "why": str(e), "v2": "reaction"},
        )
        return {"error": f"decider failed: {e}"}
    d = parse_decision(reply.text)
    ev.append(
        "arena_decisions",
        {
            "stage": "decider",
            "ticker": code,
            "ids_id": item["ids"][0],
            "model": reply.model,
            "model_expected": DECIDER_MODEL,
            "decision": d,
            "reply": reply.text,
            "v2": "reaction",
            "relook": True,
        },
    )
    if str(d.get("flag_for_claude") or "").strip():
        ev.append(
            "arena_flags",
            {"ticker": code, "stage": "v2 reaction", "flag": str(d["flag_for_claude"])},
        )
    if str(d.get("action", "pass")).lower() != "trade":
        alert = notify.get(arena)
        if alert:
            alert.passed(
                code,
                "; ".join(item["headlines"])[:120],
                str(d.get("why", "")) or "no reason given",
                seen_at,
            )
        return {"decision": "pass", "why": str(d.get("why", ""))}
    return {
        "decision": "trade",
        **place_decision(arena, pb, acct, code, d, reply.model, seen_at, "reaction"),
    }


def _from_other_day(cfg, day: date, ids: str):
    from asxbot.arena.watch import announcement_by_id, previous_session

    return announcement_by_id(cfg, previous_session(day), ids)


# --------------------------------------------------------------------------
# the v2 rule bot's day
# --------------------------------------------------------------------------
def v2_bot_cycle(
    arena, pb: Playbook, view: MarketView, now: datetime | None = None, ann=None
) -> list[dict]:
    """Once a day, when the 10:29 bar is final: the rule, as frozen, on every stock with
    price-sensitive news since the previous close. Recorded in data/arena/v2bot/<day>.json."""
    from asxbot.arena.watch import previous_session, seen_announcements

    now = (now or arena.broker.clock()).astimezone(SYD)
    cfg = arena.cfg
    day = view.day
    params = pb.yardstick()
    state = load_bot_state(cfg.data_dir, day)
    if state.get("status") in ("done", "missed"):
        return []
    measure = time_cls.fromisoformat(str(params.get("measure_at", "10:30")))
    if now.time() < measure:
        return []
    latest = time_cls.fromisoformat(str(params.get("latest_decision_time", "11:15")))
    data_time = view.data_time(now)
    if now.time() > latest:
        state.update(
            status="missed",
            why=(
                f"the 10:29 bar was not final (feed at {data_time:%H:%M})"
                if data_time
                else "no index bars"
            )
            + f" by {latest:%H:%M}; nothing is traded late",
        )
        save_bot_state(cfg.data_dir, day, state)
        log.warning("v2 rule bot missed %s: %s", day, state["why"])
        return []
    last_bar = datetime.combine(day, measure, tzinfo=SYD) - timedelta(minutes=1)
    if data_time is None or data_time < last_bar:
        state.update(status="waiting", why="the feed has not reached 10:29")
        save_bot_state(cfg.data_dir, day, state)
        return []

    prev = previous_session(day)
    if ann is None:
        ann = seen_announcements(cfg, pb, prev - timedelta(days=1), day)
    codes = v2_bot_candidates(ann, day, prev, arena.universe, measure)
    rule = size_rule(pb)
    daily = arena.daily_lookup()
    from asxbot.arena.reaction_v2 import tick_pct

    signals, seen = [], []
    for code in codes:
        d = daily(code)
        from asxbot.live.quotes import median_turnover_20d

        turnover = median_turnover_20d(d)
        size = float(params.get("size_aud", 5000))
        if turnover is None or (rule and size > rule.max_order(turnover) + 1e-6):
            seen.append({"ticker": code, "screened": f"turnover {turnover}"})
            continue
        sig, why = v2_bot_signal(view, code, now, params, code in arena.short_universe)
        max_tick = float((pb.raw.get("screen") or {}).get("max_tick_pct", 3.0))
        if sig is not None and tick_pct(sig.last) > max_tick:
            seen.append({"ticker": code, "screened": f"tick {tick_pct(sig.last):.2f}%"})
            continue
        seen.append({"ticker": code, "signal": sig is not None, "why": why})
        if sig is not None:
            signals.append((sig, turnover))
    signals.sort(key=lambda x: -x[0].volume_multiple)
    acct = arena.account(pb, "bot")
    alert = notify.get(arena)
    orders = []
    for sig, turnover in signals:
        qty, limit, stop = bot_order_terms(
            sig, params, float(params.get("size_aud", 5000)), turnover, rule.share if rule else None
        )
        try:
            o = arena_place_order(
                cfg,
                arena.broker,
                acct,
                pb,
                ticker=sig.ticker,
                side=sig.side,
                qty=qty,
                limit=limit,
                stop=stop,
                reason=f"v2 rule: {sig.why}",
                model="none (rule-based bot)",
                placed_by="bot",
                hold="intraday",
                universe=arena.universe,
                short_universe=arena.short_universe,
                now=now,
                good_till=good_till(pb, now),
            )
            orders.append(
                {
                    "ticker": o.ticker,
                    "order_id": o.order_id,
                    "side": o.side,
                    "qty": o.qty,
                    "limit": o.limit,
                    "stop": o.stop,
                }
            )
            if alert:
                alert.decided(o)
        except ArenaOrderRefused as e:
            orders.append({"ticker": sig.ticker, "refused": str(e)})
            if alert:
                alert.refused("bot", sig.ticker, sig.side, qty, str(e))
    state.update(
        status="done",
        decided_at=arena.broker.clock().isoformat(timespec="seconds"),
        feed_at=data_time.isoformat(timespec="minutes"),
        candidates=seen,
        orders=orders,
    )
    save_bot_state(cfg.data_dir, day, state)
    EventLog(cfg.data_dir).append("v2_bot", {"day": day.isoformat(), **state})
    log.info(
        "v2 rule bot: %d candidates, %d signals, %d orders",
        len(codes),
        len(signals),
        sum(1 for o in orders if "order_id" in o),
    )
    return orders


# --------------------------------------------------------------------------
# flat by the close
# --------------------------------------------------------------------------
def flatten(arena, pb: Playbook, now: datetime | None = None, price_of=None) -> list[dict]:
    """Close every position of a flat-at-close playbook: the agent's from the pre-close sweep
    time (15:50), the bot's from its own exit time when its rule names one (v2: 15:55). No
    model is asked. Runs every cycle from then on, so a fill the delayed feed shows late is
    closed as soon as it is seen."""
    from asxbot.arena.watch import preclose_window

    now = (now or arena.broker.clock()).astimezone(SYD)
    sweep_at, _ = preclose_window(arena.cfg)
    bot_at = pb.yardstick().get("exit_at")
    out = []
    for kind, at in (
        ("agent", sweep_at),
        ("bot", time_cls.fromisoformat(str(bot_at)) if bot_at else sweep_at),
    ):
        if now.time() < at:
            continue
        acct = arena.account(pb, kind)
        for ticker, pos in list(acct.positions.items()):
            left = abs(pos.qty) - acct.closing_qty_working(ticker)
            if left <= 0:
                continue
            price = (price_of(ticker) if price_of else None) or (
                arena.broker.minutes.last_price(ticker) or pos.avg_cost
            )
            side = "sell" if pos.qty > 0 else "cover"
            limit = round(price * (0.97 if side == "sell" else 1.03), 3)
            why = (
                f"flat at the close: {pb.title} holds intraday only "
                f"({'the rule exits' if kind == 'bot' and bot_at else 'the sweep runs'} at "
                f"{at:%H:%M})"
            )
            try:
                o = arena_place_order(
                    arena.cfg,
                    arena.broker,
                    acct,
                    pb,
                    ticker=ticker,
                    side=side,
                    qty=left,
                    limit=limit,
                    reason=why,
                    model="code (pre-close sweep: flat)",
                    placed_by=kind,
                    universe=arena.universe,
                    short_universe=arena.short_universe,
                    now=now,
                )
                out.append({"account": acct.name, "ticker": ticker, "order_id": o.order_id})
                log.info("flat: %s closing %s %d with %s", acct.name, ticker, left, o.order_id)
                EventLog(arena.cfg.data_dir).append(
                    "arena_decisions",
                    {
                        "stage": "preclose",
                        "ticker": ticker,
                        "action": "close",
                        "reason": why,
                        "model": "code",
                        "level": pb.level.number,
                        "account": acct.name,
                    },
                )
            except ArenaOrderRefused as e:
                log.error("flat: closing %s in %s was refused: %s", ticker, acct.name, e)
                out.append({"account": acct.name, "ticker": ticker, "refused": str(e)})
    return out


def pre_open_decider(
    arena, pb: Playbook, a: Announcement, ctx: dict, summary: str, now: datetime, seen_at: datetime
) -> dict:
    """The v2 pre-open look: the decider on overnight news, before the auction."""
    from asxbot.arena.watch import DECIDER_MODEL

    cfg = arena.cfg
    ev = EventLog(cfg.data_dir)
    acct = arena.account(pb, "agent")
    size = pb.level.max_position_aud or 5000.0
    adv = arena.broker.adv_lookup(a.code) if arena.broker.adv_lookup else None
    ctx = {**ctx, "round_trip_cost_pct": round_trip_cost_pct(arena.broker.costs, size, adv)}
    news = [
        {"released": f"{a.released_at:%Y-%m-%d %H:%M}", "headline": a.headline, "link": a.pdf_url}
    ]
    packet = decider_packet_v2(arena, pb, acct, a.code, news, ctx, [summary], now, "pre_open")
    try:
        reply = call_agent(
            DECIDER,
            packet,
            expect_model=DECIDER_MODEL,
            data_dir=cfg.data_dir,
            purpose=f"v2 pre-open {a.code} {a.ids_id}",
            timeout_s=240,
        )
    except AgentCallFailed as e:
        ev.append(
            "arena_decisions",
            {"ticker": a.code, "outcome": "decider_failed", "why": str(e), "v2": "pre_open"},
        )
        return {"error": str(e)}
    d = parse_decision(reply.text)
    ev.append(
        "arena_decisions",
        {
            "stage": "decider",
            "ticker": a.code,
            "ids_id": a.ids_id,
            "model": reply.model,
            "model_expected": DECIDER_MODEL,
            "decision": d,
            "reply": reply.text,
            "v2": "pre_open",
        },
    )
    if str(d.get("flag_for_claude") or "").strip():
        ev.append(
            "arena_flags",
            {
                "ticker": a.code,
                "ids_id": a.ids_id,
                "stage": "v2 pre-open",
                "flag": str(d["flag_for_claude"]),
            },
        )
    if str(d.get("action", "pass")).lower() != "trade":
        alert = notify.get(arena)
        if alert:
            alert.passed(
                a.code,
                a.headline,
                (str(d.get("why", "")) or "no reason given")
                + " (pre-open; the reaction look follows)",
                now,
            )
        return {"decision": "pass", "why": str(d.get("why", ""))}
    return {
        "decision": "trade",
        **place_decision(arena, pb, acct, a.code, d, reply.model, seen_at, "pre_open"),
    }


# --------------------------------------------------------------------------
# the queue's safety net
# --------------------------------------------------------------------------
def seed_queue(arena, pb: Playbook, day: date, now: datetime, quotes=None) -> list[str]:
    """Queue, through the v2 screen, every price-sensitive announcement in the universe
    released since the previous session's close that is not queued yet - so the agent's
    reaction looks cover the same news the v2 rule bot reads straight from the collector's
    files. It catches news worked before v2 took over (v1's evening of 24 Sep) or lost to a
    restart. Each announcement is seeded once (the queue keeps "_seeded")."""
    import pandas as pd

    from asxbot.announcements.model import Announcement
    from asxbot.arena.reaction_v2 import enqueue, log_no_quote, screen_v2
    from asxbot.arena.watch import is_test_id, previous_session

    cfg = arena.cfg
    prev = previous_session(day)
    frames = []
    for d in (prev, day):
        p = cfg.data_dir / "announcements" / "live" / f"{d.isoformat()}.parquet"
        if p.exists():
            frames.append(pd.read_parquet(p))
    if not frames:
        return []
    df = pd.concat(frames, ignore_index=True).drop_duplicates("ids_id")
    start = datetime.combine(prev, time_cls(16, 10))
    local_now = now.astimezone(SYD).replace(tzinfo=None)
    df = df[
        df["price_sensitive"].astype(bool)
        & (df["released_at"] >= start)
        & (df["released_at"] <= local_now)
    ]
    q = load_queue(cfg.data_dir, day)
    seeded = set(q.get("_seeded", []))
    queued_ids = {i for k, v in q.items() if k != "_seeded" for i in v.get("ids", [])}
    added = []
    qp = quotes or arena.quote_provider()
    size = pb.level.max_position_aud or 5000.0
    for r in df.itertuples():
        ids = str(r.ids_id)
        code = str(r.code).upper()
        if ids in seeded or ids in queued_ids or is_test_id(ids) or code not in arena.universe:
            continue
        seeded.add(ids)
        a = Announcement(code, r.released_at.to_pydatetime(), str(r.headline), True, ids,
                         str(r.pdf_url))  # fmt: skip
        verdict = screen_v2(a, qp.quote(code), arena.daily_lookup()(code), pb, size)
        EventLog(cfg.data_dir).append(
            "arena_screened",
            {"ticker": code, "ids_id": ids, "headline": a.headline, "ok": verdict.ok,
             "test": verdict.test, "why": verdict.why, "v2": True, "seeded": True},
        )  # fmt: skip
        if not verdict.ok:
            if verdict.test == "no_quote":
                log_no_quote(cfg.data_dir, day, code, ids, a.headline)
            continue
        enqueue(cfg.data_dir, day, a, verdict.why)
        added.append(code)
    q = load_queue(cfg.data_dir, day)
    q["_seeded"] = sorted(seeded)
    save_queue(cfg.data_dir, day, q)
    if added:
        log.info("v2: %d stock(s) with news since the last close added to today's reaction "
                 "looks: %s", len(added), ", ".join(sorted(set(added))))  # fmt: skip
    return added
