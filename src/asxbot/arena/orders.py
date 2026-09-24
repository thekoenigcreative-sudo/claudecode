"""arena_place_order: the only path from an arena agent to the arena broker.

Plain code. Hard limits. No AI in the loop. The agent chooses what to trade; this function
decides whether the trade is allowed, and the broker decides whether it filled.

It mirrors asxbot.broker.orders.place_order, with two differences:
  * it refuses to run unless the broker mode is `sim` - the arena is fake money only;
  * approvals are off, because it is fake money (ARENA.md). The level's limits still apply,
    and they are enforced here, in code, exactly as they are for real money.

Limits enforced here:
    fake money only, playbook enabled, limit orders only, allowed hours, the level's
    max open positions, the level's risk-per-trade cap (so every opening trade needs a
    stop), the level's leverage cap, the level's daily loss limit, shorts only in the
    ASX 200, the allowed universe, one opening order per ticker while one is waiting to
    fill, and the runaway guards (orders per day, order value). An opening order waiting to
    fill counts as the position it becomes: toward max open positions, and at its limit
    value toward the leverage cap.

Exits are never blocked. A daily loss limit that stopped you closing a losing position
would be a risk control that increases risk.

Time: every check here, and the order itself, uses the broker's clock at the moment of the
call - the decision time. The caller's `now` is only the time its picture of the market was
taken, and is recorded as the order's `data_as_of`. Until 2026-09-23 the caller's `now` was
the decision time, and the watcher passed the start of its cycle: an order decided after
eight minutes of model calls was stamped, and filled, eight minutes before it existed.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from asxbot.arena.accounts import Account
from asxbot.arena.broker import CLOSING_SIDES, OPENING_SIDES, SESSION_CLOSE, ArenaBroker
from asxbot.arena.hours import order_window
from asxbot.arena.levels import Playbook
from asxbot.config import Config
from asxbot.log import EventLog, get_logger

log = get_logger("asxbot.arena.orders")
SYD = ZoneInfo("Australia/Sydney")


class ArenaOrderRefused(RuntimeError):
    """A limit check failed. Nothing reached the broker."""


def arena_place_order(
    cfg: Config,
    broker: ArenaBroker,
    acct: Account,
    playbook: Playbook,
    *,
    ticker: str,
    side: str,
    qty: int,
    limit: float,
    stop: float | None = None,
    stop_pct: float | None = None,
    target: float | None = None,
    reason: str = "",
    model: str = "",
    placed_by: str = "agent",
    hold: str = "intraday",
    universe: set[str] | None = None,
    short_universe: set[str] | None = None,
    now: datetime | None = None,
    good_till: datetime | None = None,
    manage: dict | None = None,
):
    decided = broker.clock()
    data_as_of = now or decided
    ticker = ticker.upper().strip()
    side = side.lower().strip()
    events = EventLog(cfg.data_dir)
    req = {
        "account": acct.name, "playbook": playbook.key, "level": playbook.level.number,
        "ticker": ticker, "side": side, "qty": qty, "limit": limit, "stop": stop,
        "target": target, "placed_by": placed_by, "model": model,
    }  # fmt: skip

    def refuse(why: str) -> ArenaOrderRefused:
        events.append("arena_orders", {**req, "outcome": "refused", "reason": why})
        log.warning("arena_place_order REFUSED %s: %s", req, why)
        return ArenaOrderRefused(why)

    # --- fake money only ----------------------------------------------------
    if cfg.broker != "sim":
        raise refuse(
            f"the arena is fake money only, but broker mode is {cfg.broker!r}. "
            "Real money goes through place_order, with approval on every call."
        )
    if not playbook.enabled:
        raise refuse(f"playbook {playbook.key} is not enabled")

    # --- shape --------------------------------------------------------------
    if side not in OPENING_SIDES + CLOSING_SIDES:
        raise refuse(f"bad side {side!r}; expected one of {OPENING_SIDES + CLOSING_SIDES}")
    if not isinstance(qty, int) or qty <= 0:
        raise refuse("qty must be a positive whole number")
    if not (limit > 0):
        raise refuse("limit must be positive")

    guards = cfg.get("arena.guards") or {}
    local = decided.astimezone(SYD)

    orders_today = sum(
        1 for o in acct.orders.values() if o.decided_at[:10] == local.date().isoformat()
    )
    if orders_today >= int(guards.get("max_orders_per_day", 40)):
        raise refuse(f"runaway guard: already {orders_today} orders today on {acct.name}")

    value = qty * limit
    if value > float(guards.get("max_order_value_aud", 8000)) + 1e-6:
        raise refuse(
            f"order value {value:,.2f} exceeds the guard "
            f"{float(guards.get('max_order_value_aud', 8000)):,.2f}"
        )
    if value < float(guards.get("min_order_aud", 500)) - 1e-6:
        raise refuse(
            f"order value {value:,.2f} is below the minimum order "
            f"{float(guards.get('min_order_aud', 500)):,.2f}"
        )

    pos = acct.positions.get(ticker)
    lvl = playbook.level
    # Opening orders still waiting to fill. acct.positions holds only FILLED positions, so
    # without counting these, orders in flight could together fill past every limit below.
    in_flight = [
        o for o in acct.orders.values()
        if o.status == "pending_fill" and o.side in OPENING_SIDES
    ]  # fmt: skip

    # --- closing trades: checked for sanity, never blocked by risk limits ----
    # The hours check below is deliberately NOT applied to exits. Blocking an exit is a
    # risk control that increases risk, and it cannot create a phantom fill anyway: fills
    # come from real traded minutes, so an exit submitted after the close simply rests and
    # fills at the next minute the stock actually trades.
    if side in CLOSING_SIDES:
        if pos is None:
            raise refuse(f"no position in {ticker} to {side}")
        if side == "sell" and pos.qty < qty:
            raise refuse(f"holding {pos.qty} of {ticker}, cannot sell {qty}")
        if side == "cover" and -pos.qty < qty:
            raise refuse(f"short {abs(pos.qty)} of {ticker}, cannot cover {qty}")
        # Shares already working to close - a stop or target exit still filling, or an
        # earlier exit order - are spoken for. Fills are volume-limited since 2026-09-24, so
        # an exit can take several bars, and a second one on top would oversell.
        working = acct.closing_qty_working(ticker)
        if working and abs(pos.qty) - working < qty:
            raise refuse(
                f"{working} of the {abs(pos.qty)} {ticker} held are already working to close; "
                f"at most {max(0, abs(pos.qty) - working)} more can be"
            )
    else:
        # --- opening trades: the full set of limits -------------------------
        start, end = order_window(cfg, local.date())  # follows Sydney daylight saving
        if not (start <= local.time() <= end):
            raise refuse(
                f"outside arena hours {start:%H:%M}-{end:%H:%M} Sydney (now {local:%H:%M})"
            )
        last_entry = playbook.last_entry_time
        # Only late in the session: an order recorded after the 16:10 close is for the next
        # session's opening auction, and an intraday playbook may place it (a pre-open look).
        if last_entry is not None and last_entry < local.time() < SESSION_CLOSE:
            raise refuse(
                f"no new positions after {last_entry:%H:%M} in this intraday playbook: a later "
                "fill could not be seen by the pre-close sweep on the delayed feed"
            )
        if universe is not None and ticker not in universe:
            raise refuse(f"{ticker} is not in the allowed universe")
        if side == "short":
            if playbook.market == "asx":
                if short_universe is None or ticker not in short_universe:
                    raise refuse(
                        f"{ticker} is not in the ASX 200: shorts are allowed only in the "
                        "realistic borrowable set"
                    )
            elif bool(cfg.get("arena.shorts.crypto_requires_futures", True)):
                raise refuse("crypto shorts need a futures market; not built yet")
        # A second opening order in a ticker whose first is still waiting could fill into a
        # double-sized position.
        waiting = [o for o in in_flight if o.ticker == ticker]
        if waiting:
            w = waiting[0]
            raise refuse(
                f"{ticker} already has an opening order waiting to fill ({w.order_id}: "
                f"{w.side} {w.qty} @ {w.limit}, decided {w.decided_at}). One opening order "
                "per ticker until it fills or expires; exits are unaffected."
            )
        if pos is not None and (pos.qty > 0) != (side == "buy"):
            raise refuse(
                f"already {'long' if pos.qty > 0 else 'short'} {ticker}; "
                f"close it before going the other way"
            )
        if pos is not None:
            # ARENA.md: no playbook may add to a losing position.
            prices = broker.prices(acct)
            px = prices.get(ticker, pos.avg_cost)
            losing = (px < pos.avg_cost) if pos.qty > 0 else (px > pos.avg_cost)
            if losing:
                raise refuse(
                    f"{ticker} is currently a loss ({px:.3f} against {pos.avg_cost:.3f}); "
                    "adding to a losing position is not allowed"
                )
        else:
            # An opening order in a ticker already held adds to that position; one in a new
            # ticker becomes a position of its own when it fills.
            becoming = {o.ticker for o in in_flight if o.ticker not in acct.positions}
            if len(acct.positions) + len(becoming) >= lvl.max_open_positions:
                raise refuse(
                    f"already at the level {lvl.number} limit of {lvl.max_open_positions} "
                    f"open positions ({len(acct.positions)} held, {len(becoming)} more "
                    "waiting to fill)"
                )

        if stop is None:
            raise refuse("an opening trade needs a stop: risk per trade cannot be measured")
        if side == "buy" and stop >= limit:
            raise refuse(f"a long's stop ({stop}) must be below the entry limit ({limit})")
        if side == "short" and stop <= limit:
            raise refuse(f"a short's stop ({stop}) must be above the entry limit ({limit})")
        # The target is a take-profit the broker acts on. One on the wrong side of the entry
        # would be reached in the next minute and close the trade for two lots of brokerage.
        if target is not None and side == "buy" and target <= limit:
            raise refuse(f"a long's target ({target}) must be above the entry limit ({limit})")
        if target is not None and side == "short" and target >= limit:
            raise refuse(f"a short's target ({target}) must be below the entry limit ({limit})")

        prices = broker.prices(acct)
        equity = acct.equity(prices)
        if equity <= 0:
            raise refuse(f"account equity is {equity:,.2f}; no new positions")

        risk = abs(limit - stop) * qty
        risk_pct = playbook.risk_per_trade_pct
        risk_cap = equity * risk_pct / 100.0
        if risk > risk_cap + 1e-6:
            raise refuse(
                f"risk {risk:,.2f} (distance to stop x size) exceeds the "
                + (f"level {lvl.number}" if risk_pct == lvl.risk_per_trade_pct else "playbook's")
                + f" cap of {risk_pct}% of equity ({risk_cap:,.2f})"
            )

        # Level caps added 2026-09-24 (Rick's brief): dollars per position, new a day.
        if lvl.max_position_aud is not None:
            held = abs(pos.qty) * pos.avg_cost if pos is not None else 0.0
            if held + value > lvl.max_position_aud + 1e-6:
                raise refuse(
                    f"position value {held + value:,.2f} would exceed the level {lvl.number} "
                    f"cap of {lvl.max_position_aud:,.2f} per position"
                )
        if lvl.max_new_positions_per_day is not None:
            opened_today = sum(
                1 for o in acct.orders.values()
                if o.side in OPENING_SIDES and o.placed_by != "code"
                and o.decided_at[:10] == local.date().isoformat()
                and (o.filled_qty > 0 or o.working)
            )  # fmt: skip
            if opened_today >= lvl.max_new_positions_per_day:
                raise refuse(
                    f"already {opened_today} new positions today; the level {lvl.number} limit "
                    f"is {lvl.max_new_positions_per_day} a day"
                )

        # The size-aware liquidity rule (announcements v2, the day trader): our order must be
        # under a set share of the stock's median daily dollar turnover.
        from asxbot.arena.liquid import size_rule

        rule = size_rule(playbook)
        lookup = getattr(broker, "median_turnover", None)
        if rule is not None and lookup is not None:
            turnover = lookup(ticker)
            if not turnover:
                raise refuse(f"no turnover history for {ticker}: the size rule cannot be checked")
            if value > rule.max_order(turnover) + 1e-6:
                raise refuse(
                    f"order value {value:,.2f} is more than {rule.share:.0%} of {ticker}'s median "
                    f"daily turnover ({turnover:,.0f}): at most {rule.max_order(turnover):,.2f}"
                )

        max_pct = playbook.guidance("max_position_pct_of_equity")
        if max_pct is not None and value > equity * float(max_pct) / 100.0 + 1e-6:
            raise refuse(
                f"order value {value:,.2f} exceeds {max_pct}% of equity "
                f"({equity * float(max_pct) / 100.0:,.2f}) for this playbook"
            )

        leverage = lvl.leverage(playbook.market)
        # What is still to fill; a part-filled order's filled shares are already positions.
        waiting_value = sum(o.remaining * o.limit for o in in_flight)
        exposure = acct.gross_exposure(prices) + waiting_value + value
        if exposure > equity * leverage + 1e-6:
            raise refuse(
                f"gross exposure {exposure:,.2f} would exceed {leverage}x equity "
                f"({equity * leverage:,.2f}) at level {lvl.number}"
                + (f", counting {waiting_value:,.2f} waiting to fill" if waiting_value else "")
            )

        loss_pct = broker.day_loss_pct(acct, decided)
        if loss_pct <= -lvl.daily_loss_limit_pct:
            raise refuse(
                f"daily loss limit hit: {loss_pct:+.2f}% today against the level "
                f"{lvl.number} limit of {lvl.daily_loss_limit_pct}%. No new positions until "
                "tomorrow; exits are still allowed."
            )

    # --- to the broker; its order id is the only proof ----------------------
    o = broker.submit(
        acct,
        ticker=ticker,
        side=side,
        qty=qty,
        limit=limit,
        data_as_of=data_as_of,
        stop=stop,
        stop_pct=stop_pct,
        target=target,
        reason=reason,
        model=model,
        placed_by=placed_by,
        hold=hold,
        good_till=good_till,
        manage=manage,
    )
    events.append("arena_orders", {**req, "outcome": "accepted", "order_id": o.order_id})
    return o
