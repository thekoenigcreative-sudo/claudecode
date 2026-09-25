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
from asxbot.arena.intraday import MarketView, entries_allowed
from asxbot.arena.levels import Playbook
from asxbot.arena.liquid import size_rule
from asxbot.arena.orders import ArenaOrderRefused, arena_place_order
from asxbot.arena.reaction_v2 import (
    bot_order_terms,
    last_visible,
    load_bot_state,
    load_queue,
    look_status,
    reaction,
    round_trip_cost_pct,
    save_bot_state,
    save_queue,
    v2_bot_candidates,
    v2_bot_measure,
    waits_for_data,
    wakes,
)
from asxbot.live.scanner import round_to_tick
from asxbot.log import EventLog, get_logger

log = get_logger("asxbot.arena.v2")
DELAYED = "delayed data - rehearsal until IBKR live prices"
SYD = ZoneInfo("Australia/Sydney")
_PAUSE_SAID: dict[str, str] = {}


def _pause_note(what: str, why: str, now: datetime) -> None:
    """Say once, per reason, that a playbook is paused on the feed (not every cycle)."""
    if _PAUSE_SAID.get(what) != why:
        _PAUSE_SAID[what] = why
        log.error("%s paused: live feed down (%s); no entries until it is back", what, why)


# --------------------------------------------------------------------------
# the decider's v2 packet
# --------------------------------------------------------------------------
def _bar_age_line(rx: dict, now: datetime) -> str:
    """Which prices the reaction is on, and how old its newest bar is at `now`."""
    label = rx.get("data_label") or DELAYED
    as_of = rx.get("as_of_bar")
    try:
        start = datetime.combine(now.astimezone(SYD).date(), time_cls.fromisoformat(str(as_of)),
                                 tzinfo=SYD)  # fmt: skip
    except (TypeError, ValueError):
        return f"The prices are {label}."
    age = max(0, round((now - start - timedelta(minutes=1)).total_seconds() / 60))
    return (f"Its newest bar is the {as_of} minute, which ended {age} minute"
            f"{'' if age == 1 else 's'} before this decision; the prices are {label}.")  # fmt: skip


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
    # 26 Sep 2026 (review G5): the heading below named "Sonnet 5" whatever model the reader
    # call reported; it names the agent only (the model is in each reader record).
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
        # 26 Sep 2026 (review G5), a factual correction: this said "The bars are DELAYED
        # (~20 minutes on the free feed)" whatever the feed was. On 25 Sep the bars were live
        # IBKR and the same packet said so a few lines lower. It now says which prices they
        # are and how old the newest bar is.
        when = (
            "REACTION LOOK - your one look at this stock today. The market has traded this "
            "news; the reaction below is measured from 1-minute bars (move against the ASX "
            "200, volume against the stock's usual volume over the same minutes of the day). "
            f"{_bar_age_line(ctx['reaction'], now)} Your order fills at the first bar after "
            "it is recorded, so the price you get is later than the last price you see."
        )
    return f"""You are trader-decider, announcements playbook VERSION 2: trade the reaction.

This is the FAKE-MONEY arena. Every limit below is enforced in code; an order outside them is
refused. Data: {ctx["reaction"].get("data_label", DELAYED)}.

{UNTRUSTED}

{when}

YOU ARE DECIDING AT: {now:%Y-%m-%d %H:%M} Sydney

THE NEWS ({code}, price sensitive)
{news_lines}

READER'S SUMMARY (trader-reader)
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
    """The reader's summaries of these announcements, whenever it wrote them (the ASX's ids
    are unique; news after yesterday's close was read yesterday evening). The latest per id."""
    by_id: dict[str, str] = {}
    for r in EventLog(data_dir).read("arena_decisions"):
        if r.get("stage") != "reader" or str(r.get("ids_id")) not in ids or r.get("is_test"):
            continue
        if r.get("summary"):
            by_id[str(r["ids_id"])] = str(r["summary"])
    return [by_id[i] for i in ids if i in by_id]


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
    # 26 Sep 2026 (review G7), three fixes in how the block is read, no rule changed:
    #  * the order is for the stock the decider was asked about, or none: the ticker was
    #    taken from the block, so the decider (or text injected into it) could trade a
    #    stock that never passed the v2 screen or its one-look rule;
    #  * the side is normalised BEFORE the prices are rounded ("BUY" rounded the limit down
    #    and the stop up, the wrong way for a long);
    #  * the target is read inside the try: a target like "about 1.2" raised after the
    #    decision was recorded as a trade, and no order and no reason were recorded.
    asked = code.strip().upper()
    named = str(d.get("ticker") or asked).strip().upper()
    if named != asked:
        why = f"the decision names {named}, not {asked}, the stock it was asked about"
        if alert:
            alert.refused("agent", asked, str(d.get("side")), d.get("qty", "?"), why)
        return {"refused": why}
    try:
        side = str(d.get("side", "buy")).strip().lower()
        qty = int(d["qty"])
        limit = round_to_tick(float(d["limit"]), up=side == "buy")
        stop = round_to_tick(float(d["stop"]), up=side != "buy")
        raw_target = d.get("target")
        if raw_target is None or str(raw_target).strip().lower() in ("", "null", "none"):
            target = None
        else:
            target = float(raw_target)
    except (KeyError, TypeError, ValueError) as e:
        return {"refused": f"the decision block was not usable: {e!r}"}
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
            ticker=asked,
            side=side,
            qty=qty,
            limit=limit,
            stop=stop,
            target=target,
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
# 26 Sep 2026 (review, agent unavailable): a look whose decider call failed is queued again,
# and tried no sooner than this after the failure, until its window closes.
AGENT_RETRY = timedelta(minutes=2)
# 26 Sep 2026 (review C13): a look still "looking" this long after it started was cut off by
# a crash or a restart (the watcher runs one look at a time; the decider's limit is 240 s).
LOOKING_STALE = timedelta(minutes=10)
_WAIT_WARNED: set[tuple[date, str, str]] = set()


def _when(text) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=SYD)


def _after_cutoff(pb: Playbook, t: datetime) -> bool:
    """Past the playbook's last entry time (arena_place_order refuses an entry then)."""
    cut = pb.last_entry_time
    return cut is not None and t.astimezone(SYD).time() > cut


def _close(item: dict, status: str, why: str, exposed: bool = False) -> None:
    """Record a final status for a look that was not made. A failed agent call or missing
    data that kept it waiting until then is named first: that, not the window, is why."""
    if exposed:
        held = "the agent held it or had an entry working through the window; "
        item.update(status="held", why=held + why)
        return
    lead = ""
    if item.get("agent_failed"):
        lead = f"agent unavailable ({item['agent_failed'].get('kind', 'error')}); "
    elif item.get("waiting"):
        lead = f"no data ({item['waiting']}); "
    item.update(status=status, why=lead + why)


def _prepare(view: MarketView, codes: list[str]) -> None:
    """Queue the prior sessions of these stocks on a feed that keeps its own (IBKR), so the
    previous close and usual volume are in hand when they are needed (review C1)."""
    fn = getattr(view, "prepare", None)
    if fn is None or not codes:
        return
    try:
        fn(list(codes))
    except Exception as e:  # noqa: BLE001 - asking early must never stop the looks
        log.warning("could not queue prior sessions for %d stock(s): %s", len(codes), e)


def _reaction_decision(data_dir, code: str, day: date, since: datetime | None) -> dict | None:
    """The decider's reaction-look answer on `code` recorded on `day` (at or after `since`)."""
    for r in reversed(EventLog(data_dir).read("arena_decisions")):
        if r.get("ticker") != code or r.get("v2") != "reaction" or r.get("stage") != "decider":
            continue
        t = _when(r.get("ts"))
        if t is None or t.astimezone(SYD).date() != day:
            continue
        if since is None or t >= since - timedelta(minutes=1):
            return r
    return None


def _recover_interrupted(arena, pb: Playbook, view: MarketView, q: dict, now: datetime) -> None:
    """26 Sep 2026 (review C13): a crash or restart in the middle of a look left it "looking"
    for good, with no verdict. One still "looking" LOOKING_STALE after it started was cut
    off: if the decider had answered, the look was made ("looked"); if not, it is queued
    again while its window is open, and recorded as missed once it has closed."""
    changed = False
    for code, item in q.items():
        if code.startswith("_") or not isinstance(item, dict) or item.get("status") != "looking":
            continue
        since = _when(item.get("looking_since"))
        if since is not None and now - since < LOOKING_STALE:
            continue
        started = f"{since.astimezone(SYD):%H:%M}" if since else "an unrecorded time"
        cut = f"the look started at {started} was cut off (crash or restart)"
        rec = _reaction_decision(arena.cfg.data_dir, code, view.day, since)
        if rec is not None:
            d = rec.get("decision") or {}
            item.update(status="looked", why=cut + " after the decider answered",
                        outcome={"decision": str(d.get("action", "pass")).lower(),
                                 "why": str(d.get("why", "")), "recovered": cut})  # fmt: skip
        else:
            st = look_status(view, item, now, pb)
            if st.status in ("ready", "wait"):
                item.update(status="queued", why=cut + "; it is looked at again")
            else:
                item.update(status="missed", why=f"{cut} and its window has closed: {st.why}")
        item.pop("looking_since", None)
        EventLog(arena.cfg.data_dir).append("v2_reaction", {"ticker": code, **_brief(item)})
        log.warning("reaction look on %s: %s", code, item["why"])
        changed = True
    if changed:
        save_queue(arena.cfg.data_dir, view.day, q)


def _data_gap(r: dict, woke: bool) -> str | None:
    """Missing data that stops the reaction being judged, or None. A reaction that is not
    available at all (no previous close, no index bar) is never "quiet"; nor is one with no
    usual volume, unless the move alone wakes the decider (the rule is move OR volume)."""
    if not r.get("available"):
        return str(r.get("why") or "no reaction data")
    if not woke and r.get("volume_why"):
        return str(r["volume_why"])
    return None


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
    _recover_interrupted(arena, pb, view, q, now)
    queued = [
        c
        for c, v in q.items()
        if not c.startswith("_") and isinstance(v, dict) and v.get("status") == "queued"
    ]
    if not queued:
        return []
    acct = arena.account(pb, "agent")
    if _after_cutoff(pb, now):
        # 26 Sep 2026 (review C15): no entry may be made after the last entry time, so no
        # look is made either - no reader or decider call for an order code would refuse.
        # Recorded whatever the feed is doing, so nothing is left queued at the close.
        for code in queued:
            _close(q[code], "missed", f"after the entry cut-off ({pb.last_entry_time:%H:%M})",
                   _has_exposure(acct, code))  # fmt: skip
            ev.append("v2_reaction", {"ticker": code, **_brief(q[code])})
        save_queue(cfg.data_dir, day, q)
        return []
    # 26 Sep 2026 (review C1): a stock with news during the session had its prior sessions
    # first asked for by its own look, which then found no previous close. Asked for as soon
    # as it is queued (a no-op for those already held, on Yahoo and in the replay).
    _prepare(view, queued)
    ok, why = entries_allowed(view, now)
    if not ok:
        _pause_note("v2 reaction looks", why, now)
        return []  # the looks wait; a window that closes meanwhile is recorded as missed
    due = []
    for code in queued:
        item = q[code]
        st = look_status(view, item, now, pb)
        if st.status == "wait":
            continue
        # Held, or an entry working (a pre-open order is still "working" until the delayed
        # feed shows the auction, ~10:22): no look this cycle, but no verdict either - if
        # the order expires or the position is stopped out, the look still comes.
        exposed = _has_exposure(acct, code)
        if st.status in ("no_trade", "missed"):
            _close(item, st.status, st.why, exposed)
            ev.append("v2_reaction", {"ticker": code, **_brief(item)})
            continue
        if exposed:
            continue
        failed = _when((item.get("agent_failed") or {}).get("at"))
        if failed is not None and now - failed < AGENT_RETRY:
            continue
        r = reaction(view, code, now, st.ref, st.base)
        ok, why = wakes(r, pb)
        gap = _data_gap(r, ok)
        if gap:
            # Missing data, not a quiet stock: looked at again next cycle, until its window
            # closes ("missed", naming the data). On 25 Sep 2026 six looks were closed as
            # quiet at 10:16 with no previous close (LEARNINGS #25); 26 Sep 2026 (review
            # C2): the same for no usual volume, which was closed as "quiet, volume unknown".
            item["waiting"] = gap
            if (day, code, gap) not in _WAIT_WARNED:
                _WAIT_WARNED.add((day, code, gap))
                log.warning("reaction look on %s waits: %s (%s)", code, gap, r.get("data_label"))
            continue
        item.pop("waiting", None)
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
        t = arena.broker.clock()
        if _after_cutoff(pb, t):
            _close(item, "missed", f"after the entry cut-off ({pb.last_entry_time:%H:%M}), "
                                   "by the time its turn came")  # fmt: skip
            save_queue(cfg.data_dir, day, q)
            ev.append("v2_reaction", {"ticker": code, **_brief(item)})
            continue
        fresh = look_status(view, item, t, pb)
        if fresh.status == "wait":
            # 26 Sep 2026 (review C10): "wait" is not a verdict. It was recorded as a final
            # "missed ... by the time its turn came"; it stays queued for the next cycle.
            continue
        if fresh.status != "ready":
            _close(item, fresh.status, f"by the time its turn came: {fresh.why}")
            save_queue(cfg.data_dir, day, q)
            ev.append("v2_reaction", {"ticker": code, **_brief(item)})
            continue
        # 26 Sep 2026 (review C10): the feed was checked once before a run of looks that can
        # take minutes each; it is checked again, for this stock, before each one.
        ok, pause_why = entries_allowed(view, t, [code])
        if not ok:
            _pause_note("v2 reaction looks", pause_why, t)
            continue
        item.update(status="looking", looking_since=t.isoformat(timespec="seconds"))
        save_queue(cfg.data_dir, day, q)
        try:
            res = look(arena, pb, view, code, item, fresh, why)
        except Exception as e:  # noqa: BLE001
            log.exception("reaction look on %s failed: %s", code, e)
            res = {"error": str(e)}
            # Not "looked": a failure in code is not a look. Final, so a fault after the
            # decider answered cannot ask it again every cycle.
            item.update(status="failed", why=f"the look failed in code: {e!r}")
        item.pop("looking_since", None)
        if "agent_unavailable" in res:
            # 26 Sep 2026 (review, agent unavailable): a decider call that failed (usage
            # limit, timeout, error) is not a look and not a pass. Queued again, retried
            # after AGENT_RETRY, until its window closes: then "missed: agent unavailable".
            item.update(status="queued", why=f"the decider was unavailable: {res.get('error')}",
                        agent_failed={"kind": res["agent_unavailable"],
                                      "at": arena.broker.clock().isoformat(timespec="seconds"),
                                      "why": res.get("error")})  # fmt: skip
        elif item.get("status") == "looking":
            for k in ("agent_failed", "why"):
                item.pop(k, None)
            item.update(status="looked", outcome=res)
        else:
            item["outcome"] = res
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
    from asxbot.arena.agents import expected_model
    from asxbot.arena.watch import (
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
                expect_model=expected_model(cfg, "reader"),
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
            expect_model=expected_model(cfg, "decider"),
            data_dir=cfg.data_dir,
            purpose=f"v2 reaction look {code}",
            timeout_s=240,
        )
    except AgentCallFailed as e:
        # 26 Sep 2026 (review, agent unavailable): the caller keeps the look queued.
        kind = str(getattr(e, "kind", "error"))
        ev.append(
            "arena_decisions",
            {"ticker": code, "outcome": "decider_failed", "kind": kind, "why": str(e),
             "v2": "reaction"},
        )  # fmt: skip
        return {"agent_unavailable": kind, "error": f"decider failed: {e}"}
    d = parse_decision(reply.text)
    ev.append(
        "arena_decisions",
        {
            "stage": "decider",
            "ticker": code,
            "ids_id": item["ids"][0],
            "model": reply.model,
            "model_expected": expected_model(cfg, "decider"),
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
_BOT_PREPARED: dict = {}


def _prepare_bot(
    arena, pb: Playbook, view: MarketView, now: datetime, ann, prev: date, measure: time_cls
) -> None:
    """26 Sep 2026 (review C1): on IBKR a stock's prior sessions were first asked for by the
    rule itself at 10:30 - most v2 names are outside the day trader's pre-fetched universe -
    and came back too late (25 Sep, 10:39). Before the measure, at most once a minute, they
    are queued for the index and every stock with price-sensitive news since the last close,
    so they are in hand at 10:30. Nothing to do in the replay or on Yahoo (the minute cache
    is the history there)."""
    from asxbot.arena.watch import seen_announcements

    if getattr(view, "replay", False):
        return
    key = (view.day, str(arena.cfg.data_dir))
    last = _BOT_PREPARED.get(key)
    if last is not None and timedelta(0) <= now - last < timedelta(seconds=60):
        return
    _BOT_PREPARED[key] = now
    try:
        if ann is None:
            ann = seen_announcements(arena.cfg, pb, prev - timedelta(days=1), view.day)
        codes = v2_bot_candidates(ann, view.day, prev, arena.universe, measure)
    except Exception as e:  # noqa: BLE001 - asking early must never stop the rule
        log.warning("v2 rule bot: could not list the news stocks to prepare: %s", e)
        return
    _prepare(view, [view.index, *codes])


def _bot_deadline(state: dict, latest: time_cls, now: datetime, data_time) -> None:
    """latest_decision_time has passed. Never measured: the day is missed (as before).
    Measured: every candidate still waiting for its data is recorded "missed: no data" -
    not "no signal" - and nothing is traded late."""
    pending = state.pop("pending", None) or {}
    if state.get("measured_at") is None:
        state.update(
            status="missed",
            why=(
                f"not decided by {latest:%H:%M}: first reached at {now:%H:%M} with the feed "
                + (f"at {data_time:%H:%M}" if data_time else "holding no index bars")
                + ("" if state.get("status") != "waiting" else f", waiting: {state.get('why')}")
                + "; nothing is traded late"
            ),
        )
        return
    cands = list(state.get("candidates") or [])
    for code, why in sorted(pending.items()):
        cands.append({"ticker": code, "missed": "no data",
                      "why": f"missed: no data by {latest:%H:%M} ({why}); nothing is traded "
                             "late"})  # fmt: skip
    state["candidates"] = cands
    if not pending or any("signal" in c for c in cands):
        state.pop("why", None)
        state["status"] = "done"
    else:
        state.update(
            status="missed",
            why=f"no data for any candidate by {latest:%H:%M}: "
            + "; ".join(f"{c} ({w})" for c, w in sorted(pending.items()))
            + "; nothing is traded late",
        )


def v2_bot_cycle(
    arena, pb: Playbook, view: MarketView, now: datetime | None = None, ann=None
) -> list[dict]:
    """Once a day, when the 10:29 bar is final: the rule, as frozen, on every stock with
    price-sensitive news since the previous close. Recorded in data/arena/v2bot/<day>.json.

    26 Sep 2026 (review C1): a candidate whose own data is missing (no previous close, no
    usual volume, no bars from a feed that was not watching it) is not "no signal": it waits,
    and is decided in the cycle its data arrives, until latest_decision_time; then it is
    recorded "missed: no data". Until now one such stock was closed "no signal" and the day
    "done" for good - 25 Sep 10:39, one stock at a time. The measure is the same 10:30 one
    whenever it is made (bars to 10:29); the lag is recorded."""
    from asxbot.arena.reaction_v2 import NO_PREV_CLOSE, tick_pct
    from asxbot.arena.watch import previous_session, seen_announcements
    from asxbot.live.quotes import median_turnover_20d

    now = (now or arena.broker.clock()).astimezone(SYD)
    cfg = arena.cfg
    day = view.day
    params = pb.yardstick()
    state = load_bot_state(cfg.data_dir, day)
    if state.get("status") in ("done", "missed"):
        return []
    measure = time_cls.fromisoformat(str(params.get("measure_at", "10:30")))
    prev = previous_session(day)
    if now.time() < measure:
        _prepare_bot(arena, pb, view, now, ann, prev, measure)
        return []
    latest = time_cls.fromisoformat(str(params.get("latest_decision_time", "11:15")))
    data_time = view.data_time(now)
    if now.time() > latest:
        _bot_deadline(state, latest, now, data_time)
        save_bot_state(cfg.data_dir, day, state)
        if state.get("measured_at") is not None:
            EventLog(cfg.data_dir).append("v2_bot", {"day": day.isoformat(), **state})
        log.warning("v2 rule bot %s at %s: %s", state["status"], f"{latest:%H:%M}",
                    state.get("why") or "candidates without data recorded as missed")  # fmt: skip
        return []
    ok, why = entries_allowed(view, now)
    if not ok:
        # Rick, 25 Sep: no entry decision on anything but live IBKR prices. The rule waits
        # for the feed, until its own deadline records the day as missed with this reason.
        _pause_note("v2 rule bot", why, now)
        state.update(status="waiting", why=f"paused: {why}")
        save_bot_state(cfg.data_dir, day, state)
        return []
    measure_at = datetime.combine(day, measure, tzinfo=SYD)
    last_bar = measure_at - timedelta(minutes=1)
    if data_time is None or data_time < last_bar:
        state.update(status="waiting", why="the feed has not reached 10:29")
        save_bot_state(cfg.data_dir, day, state)
        return []

    if view.prev_close(view.index) is None:
        # Every candidate is measured against the index: without its previous close the rule
        # cannot be run, and "no signal" would be a data failure dressed as a verdict (25 Sep
        # 2026: 13 of 13 candidates). Wait for it, until latest_decision_time.
        state.update(status="waiting", why=f"{NO_PREV_CLOSE} for the index ({view.label})")
        save_bot_state(cfg.data_dir, day, state)
        return []
    if ann is None:
        ann = seen_announcements(cfg, pb, prev - timedelta(days=1), day)
    codes = v2_bot_candidates(ann, day, prev, arena.universe, measure)
    rule = size_rule(pb)
    daily = arena.daily_lookup()
    size = float(params.get("size_aud", 5000))
    max_tick = float((pb.raw.get("screen") or {}).get("max_tick_pct", 3.0))
    lag = round((now - measure_at).total_seconds() / 60, 1)
    seen = list(state.get("candidates") or [])
    decided = {c.get("ticker") for c in seen}
    pending: dict[str, str] = {}
    signals, new = [], 0
    for code in codes:
        if code in decided:
            continue
        turnover = median_turnover_20d(daily(code))
        if turnover is None or (rule and size > rule.max_order(turnover) + 1e-6):
            seen.append({"ticker": code, "screened": f"turnover {turnover}"})
            new += 1
            continue
        sig, why, inputs = v2_bot_measure(view, code, now, params, code in arena.short_universe)
        if sig is None and waits_for_data(why):
            pending[code] = why
            continue
        new += 1
        if sig is not None and tick_pct(sig.last) > max_tick:
            seen.append({"ticker": code, "screened": f"tick {tick_pct(sig.last):.2f}%",
                         "inputs": inputs})  # fmt: skip
            continue
        # 26 Sep 2026 (review C11): the inputs of the measure, so it can be reproduced.
        seen.append({"ticker": code, "signal": sig is not None, "why": why,
                     "decided_at": now.isoformat(timespec="seconds"), "lag_min": lag,
                     "inputs": inputs})  # fmt: skip
        if sig is not None:
            signals.append((sig, turnover))
    signals.sort(key=lambda x: -x[0].volume_multiple)
    acct = arena.account(pb, "bot")
    alert = notify.get(arena)
    orders = list(state.get("orders") or [])
    placed = []
    for sig, turnover in signals:
        # 26 Sep 2026 (review C6): the limit is through the last price visible now, as the
        # rule says - not the 10:29 close whatever the time of the decision.
        px, px_bar = last_visible(view, sig.ticker, now)
        qty, limit, stop = bot_order_terms(
            sig, params, size, turnover, rule.share if rule else None, last=px
        )
        terms = {"limit_from": {"price": sig.last if px is None else px, "bar": px_bar},
                 "decision_lag_min": lag}  # fmt: skip
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
            rec = {
                "ticker": o.ticker,
                "order_id": o.order_id,
                "side": o.side,
                "qty": o.qty,
                "limit": o.limit,
                "stop": o.stop,
                **terms,
            }
            if alert:
                alert.decided(o)
        except ArenaOrderRefused as e:
            rec = {"ticker": sig.ticker, "refused": str(e), **terms}
            if alert:
                alert.refused("bot", sig.ticker, sig.side, qty, str(e))
        orders.append(rec)
        placed.append(rec)
    first = state.get("measured_at") is None
    state.pop("why", None)  # a "waiting" reason from an earlier cycle is not the outcome
    state.update(
        feed_at=data_time.isoformat(timespec="minutes"),
        data=view.label,  # which prices the rule was measured on (IBKR live or Yahoo)
        candidates=seen,
        orders=orders,
    )
    if first:
        state.update(measured_at=now.isoformat(timespec="seconds"), measure_lag_min=lag)
    if pending:
        state.update(
            status="waiting",
            pending=pending,
            why=f"{len(pending)} candidate(s) waiting for their data: "
            + "; ".join(f"{c} ({w})" for c, w in sorted(pending.items())),
        )
    else:
        state.pop("pending", None)
        state.update(status="done", decided_at=arena.broker.clock().isoformat(timespec="seconds"))
    save_bot_state(cfg.data_dir, day, state)
    if first or new or state["status"] == "done":
        EventLog(cfg.data_dir).append("v2_bot", {"day": day.isoformat(), **state})
        log.info(
            "v2 rule bot: %d candidates, %d decided now, %d signals, %d orders, %d waiting "
            "for data",
            len(codes),
            new,
            len(signals),
            sum(1 for o in placed if "order_id" in o),
            len(pending),
        )
    return placed


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
    from asxbot.arena.agents import expected_model

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
            expect_model=expected_model(cfg, "decider"),
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
            "model_expected": expected_model(cfg, "decider"),
            "decision": d,
            "reply": reply.text,
            "data": (ctx.get("reaction") or {}).get("data_label"),
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
_SCREENED: dict = {}


def _arrival_verdicts(data_dir, ids: set[str]) -> dict[str, dict]:
    """The latest v2 screen record for each of these announcement ids (arena_screened),
    whether it was made on arrival (watch._handle_v2) or here. Read again only when the log
    has changed."""
    p = EventLog(data_dir).path("arena_screened")
    try:
        st = p.stat()
    except OSError:
        return {}
    key = (str(p), st.st_mtime_ns, st.st_size)
    if _SCREENED.get("key") != key:
        latest: dict[str, dict] = {}
        for r in EventLog(data_dir).read("arena_screened"):
            if r.get("v2") and r.get("ids_id") is not None:
                latest[str(r["ids_id"])] = r
        _SCREENED.clear()
        _SCREENED.update(key=key, latest=latest)
    return {i: _SCREENED["latest"][i] for i in ids if i in _SCREENED["latest"]}


def seed_queue(
    arena, pb: Playbook, day: date, now: datetime, quotes=None, feed_down: bool = False
) -> list[str]:
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
    before = set(seeded)
    queued_ids = {i for k, v in q.items() if k != "_seeded" for i in v.get("ids", [])}
    todo = [
        r for r in df.itertuples()
        if not (str(r.ids_id) in seeded or str(r.ids_id) in queued_ids
                or is_test_id(str(r.ids_id)) or str(r.code).upper() not in arena.universe)
    ]  # fmt: skip
    if not todo:
        return []
    arrival = _arrival_verdicts(cfg.data_dir, {str(r.ids_id) for r in todo})
    added = []
    qp = quotes or arena.quote_provider()
    size = pb.level.max_position_aud or 5000.0
    for r in todo:
        ids = str(r.ids_id)
        code = str(r.code).upper()
        seeded.add(ids)
        v = arrival.get(ids)
        if v is not None and not v.get("ok") and v.get("test") != "deferred":
            # 26 Sep 2026 (review C8): screened out on arrival - that is its verdict. It
            # was screened again here with a fresh quote (35 at 10:52 on 25 Sep, and some
            # verdicts changed: NXN no_quote -> tick). Only a "deferred" one (no quote while
            # the feed was down) is screened again.
            continue
        a = Announcement(code, r.released_at.to_pydatetime(), str(r.headline), True, ids,
                         str(r.pdf_url))  # fmt: skip
        quote = qp.quote(code)
        if quote is None and feed_down:
            # No quote and the live feed is down: not a verdict. Tried again next cycle
            # (the id is not marked seeded), as watch._handle_v2 defers on arrival.
            seeded.discard(ids)
            continue
        verdict = screen_v2(a, quote, arena.daily_lookup()(code), pb, size)
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
    if seeded != before:
        q = load_queue(cfg.data_dir, day)
        q["_seeded"] = sorted(seeded)
        save_queue(cfg.data_dir, day, q)
    if added:
        log.info("v2: %d stock(s) with news since the last close added to today's reaction "
                 "looks: %s", len(added), ", ".join(sorted(set(added))))  # fmt: skip
    return added
