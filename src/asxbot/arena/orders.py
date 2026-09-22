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
    ASX 200, the allowed universe, and the runaway guards (orders per day, order value).

Exits are never blocked. A daily loss limit that stopped you closing a losing position
would be a risk control that increases risk.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from asxbot.arena.accounts import Account
from asxbot.arena.broker import CLOSING_SIDES, OPENING_SIDES, ArenaBroker
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
    target: float | None = None,
    reason: str = "",
    model: str = "",
    placed_by: str = "agent",
    hold: str = "intraday",
    universe: set[str] | None = None,
    short_universe: set[str] | None = None,
    now: datetime | None = None,
):
    now = now or datetime.now(SYD)
    ticker = ticker.upper().strip()
    side = side.lower().strip()
    events = EventLog(cfg.data_dir)
    req = {
        "account": acct.name, "playbook": playbook.key, "level": playbook.level.number,
        "ticker": ticker, "side": side, "qty": qty, "limit": limit, "stop": stop,
        "placed_by": placed_by, "model": model,
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
    hrs = guards.get("allowed_hours", {})
    start, end = hrs.get("start", "07:00"), hrs.get("end", "19:30")
    local = now.astimezone(SYD)
    if not (time.fromisoformat(start) <= local.time() <= time.fromisoformat(end)):
        raise refuse(f"outside arena hours {start}-{end} Sydney (now {local:%H:%M})")

    orders_today = sum(
        1 for o in acct.orders.values() if o.decision_at[:10] == local.date().isoformat()
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
            f"order value {value:,.2f} is below the minimum parcel "
            f"{float(guards.get('min_order_aud', 500)):,.2f}"
        )

    pos = acct.positions.get(ticker)
    lvl = playbook.level

    # --- closing trades: checked for sanity, never blocked by risk limits ----
    if side in CLOSING_SIDES:
        if pos is None:
            raise refuse(f"no position in {ticker} to {side}")
        if side == "sell" and pos.qty < qty:
            raise refuse(f"holding {pos.qty} of {ticker}, cannot sell {qty}")
        if side == "cover" and -pos.qty < qty:
            raise refuse(f"short {abs(pos.qty)} of {ticker}, cannot cover {qty}")
    else:
        # --- opening trades: the full set of limits -------------------------
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
        elif len(acct.positions) >= lvl.max_open_positions:
            raise refuse(
                f"already at the level {lvl.number} limit of {lvl.max_open_positions} "
                f"open positions"
            )

        if stop is None:
            raise refuse("an opening trade needs a stop: risk per trade cannot be measured")
        if side == "buy" and stop >= limit:
            raise refuse(f"a long's stop ({stop}) must be below the entry limit ({limit})")
        if side == "short" and stop <= limit:
            raise refuse(f"a short's stop ({stop}) must be above the entry limit ({limit})")

        prices = broker.prices(acct)
        equity = acct.equity(prices)
        if equity <= 0:
            raise refuse(f"account equity is {equity:,.2f}; no new positions")

        risk = abs(limit - stop) * qty
        risk_cap = equity * lvl.risk_per_trade_pct / 100.0
        if risk > risk_cap + 1e-6:
            raise refuse(
                f"risk {risk:,.2f} (distance to stop x size) exceeds the level {lvl.number} "
                f"cap of {lvl.risk_per_trade_pct}% of equity ({risk_cap:,.2f})"
            )

        max_pct = playbook.guidance("max_position_pct_of_equity")
        if max_pct is not None and value > equity * float(max_pct) / 100.0 + 1e-6:
            raise refuse(
                f"order value {value:,.2f} exceeds {max_pct}% of equity "
                f"({equity * float(max_pct) / 100.0:,.2f}) for this playbook"
            )

        leverage = lvl.leverage(playbook.market)
        exposure = acct.gross_exposure(prices) + value
        if exposure > equity * leverage + 1e-6:
            raise refuse(
                f"gross exposure {exposure:,.2f} would exceed {leverage}x equity "
                f"({equity * leverage:,.2f}) at level {lvl.number}"
            )

        loss_pct = broker.day_loss_pct(acct, now)
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
        decision_at=now,
        stop=stop,
        target=target,
        reason=reason,
        model=model,
        placed_by=placed_by,
        hold=hold,
    )
    events.append("arena_orders", {**req, "outcome": "accepted", "order_id": o.order_id})
    return o
