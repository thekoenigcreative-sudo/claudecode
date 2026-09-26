"""CALIBRATE BEFORE TRUSTING (CLOUD_BRIEF 5): the live 10-day paper test is the ground truth.

Two checks per live trading day, reported plainly by `asxbot lab sim fidelity`:

1. FILLS. Every order the live arena actually placed that day (data/arena/accounts/*.json) is
   sent to this simulator's broker at the moment it was recorded live (`decided_at`), with the
   same side, size and limit; the live broker's resting exits (stops and take-profit targets)
   are placed at their levels from the moment they began resting. The simulated fills (minute,
   price, shares) and each book's P&L after costs are set against the live ones. Differences
   come from the fill model (live: the CLOSE of the first bar after the decision plus 0.10%
   slippage; here: the OPEN of that bar plus half a modelled spread and impact), the volume
   cap, and the costs - which is what this check is for.

2. DECISIONS. The frozen rule bots are replayed through the live code on the same day by the
   Practice Lab's time machine (arena/replay_ibkr.replay_day, rule bots only: the agent's own
   choices on a past day are the lab's `--agent` calibration) and their trades set against the
   live bots' trades: same stocks, same sides, same minutes?

A calibration run is of the FROZEN LIVE configuration on the live days, not of any candidate
strategy; it tunes nothing and scores nothing, so it does not count as a look at the sealed
test (it is recorded in calibration.jsonl beside the lab's locked-run log).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from asxbot.lab.tsim.broker import Account, OrderRejected, SimBroker
from asxbot.lab.tsim.costs import CostModel
from asxbot.lab.tsim.market import SLOTS, Market, slot_time
from asxbot.lab.tsim.run import Inputs

ENTRY_SIDES = {"buy": "buy", "short": "short", "sell": "sell", "cover": "cover"}


def live_orders(data_dir: Path, day: date) -> dict[str, list[dict]]:
    out = {}
    for p in sorted((Path(data_dir) / "arena" / "accounts").glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        orders = [o for o in (d.get("orders") or {}).values()
                  if str(o.get("decided_at", ""))[:10] == day.isoformat()
                  or str(o.get("fill_minute") or "")[:10] == day.isoformat()]  # fmt: skip
        if orders:
            out[p.stem] = sorted(orders, key=lambda o: o.get("rests_from") or o["decided_at"])
    return out


def _when(o: dict) -> datetime:
    """When the order reaches the simulated broker: its live recording time; for the exits the
    live broker raised (stops, targets), the start of the minute BEFORE their live fill - their
    levels moved with trade management (breakeven, trailing) and the record keeps only the
    last, so this checks the fill at the same trigger, not when the level was set."""
    from datetime import timedelta

    if o.get("order_type") in ("stop", "target") and o.get("fill_minute"):
        return datetime.fromisoformat(o["fill_minute"]) - timedelta(seconds=30)
    return datetime.fromisoformat(o["decided_at"])


def replay_orders(day: date, orders: list[dict], inputs: Inputs, cfg=None,
                  live_style: bool = False) -> dict:  # fmt: skip
    """The live orders through the simulator's broker. live_style: the live broker's price
    basis (bar close, flat 0.10% slippage, no spread) instead of this simulator's."""
    m = Market(day, inputs.history(), sorted({o["ticker"] for o in orders}), None,
               inputs.summaries(), set(inputs.shortable))  # fmt: skip
    costs = CostModel.from_config(cfg)
    acct = Account("fidelity", 20_000.0, 20_000.0, leverage=10.0)
    b = SimBroker(acct, costs, 0.20, set(inputs.shortable) | {o["ticker"] for o in orders})
    b.market = m
    if live_style:
        _live_style(b)
    mapping, errors = {}, []
    queue = sorted(orders, key=_when)
    for slot in range(SLOTS):
        end = slot_time(day, slot + 1)
        while queue and _when(queue[0]) < end:
            o = queue.pop(0)
            at = _when(o)
            m.now = at
            typ = o.get("order_type") or "limit"
            try:
                if typ == "stop":
                    so = b.place(o["ticker"], o["side"], int(o["qty"]), "stop", at=at,
                                 stop=float(o["limit"]), tif="gtc", reduce_only=True)  # fmt: skip
                elif typ == "target":
                    so = b.place(o["ticker"], o["side"], int(o["qty"]), "limit", at=at,
                                 limit=float(o["limit"]), tif="gtc", reduce_only=True)  # fmt: skip
                    so.resting = True  # a target rests: only a trade through it fills it
                else:
                    so = b.place(o["ticker"], o["side"], int(o["qty"]), "limit", at=at,
                                 limit=float(o["limit"]), tif="day",
                                 reduce_only=o["side"] in ("sell", "cover"),
                                 good_till=o.get("good_till") or None)  # fmt: skip
                mapping[o["order_id"]] = so.id
            except OrderRejected as e:
                errors.append({"order": o["order_id"], "error": str(e)})
        for f in b.work_slot(slot):
            # the live arena broker: once a stop takes a position out, the entry's unfilled
            # remainder is cancelled ("a stop took over")
            fo = acct.orders[f.order_id]
            if fo.reduce_only and fo.type == "stop":
                for w in acct.working(fo.code):
                    if not w.reduce_only:
                        b.cancel(w.id, slot_time(day, slot + 1), "a stop took over (live rule)")
    rows = []
    for o in orders:
        so = acct.orders.get(mapping.get(o["order_id"], ""))
        live_px = o.get("avg_price")
        sim_px = so.avg_price if so else None
        rows.append({
            "order": o["order_id"], "code": o["ticker"], "side": o["side"],
            "type": o.get("order_type") or "limit", "qty": o["qty"],
            "decided": o["decided_at"][11:19],
            "live_filled": o.get("filled_qty") or 0, "sim_filled": so.filled if so else 0,
            "live_minute": (o.get("fill_minute") or "")[11:16],
            "sim_minute": so.fills[0]["at"][11:16] if so and so.fills else "",
            "live_price": live_px, "sim_price": round(sim_px, 4) if sim_px else None,
            "diff_bps": round((sim_px / live_px - 1) * 1e4, 1) if sim_px and live_px else None,
            "live_commission": o.get("commission"), "sim_commission": so.commission if so else 0,
        })  # fmt: skip
    closes = {c: m.last(c) for c in m.bars}
    m.now = slot_time(day, SLOTS)
    return {"rows": rows, "errors": errors,
            "sim_equity_change": round(acct.equity(lambda c: closes.get(c)) - 20_000.0, 2),
            "sim_fees": round(acct.fees, 2), "open_positions": {c: p.qty for c, p in
                                                             acct.positions.items()}}  # fmt: skip


def _live_style(b: SimBroker) -> None:
    """Price like the live arena broker: the bar's CLOSE, 0.10% + impact slippage, no spread."""
    from asxbot.backtest.costs import CostModel as Live

    live = Live()
    orig = b._price_for

    def price_for(o, slot, op, h, l, c, v, turnover, ao, ac):  # noqa: E741
        ref, basis = orig(o, slot, op, h, l, c, v, turnover, ao, ac)
        if basis in ("market", "marketable") and not ao:
            ref = c
        return ref, basis

    b._price_for = price_for

    def aggressive(ref, buy, qty, bar_volume, turnover, auction=False):
        return live.buy_price(ref, qty * ref, turnover) if buy else live.sell_price(
            ref, qty * ref, turnover)  # fmt: skip

    b.costs = _Wrap(b.costs, aggressive)


class _Wrap:
    def __init__(self, inner, aggressive):
        self._inner = inner
        self.aggressive_price = aggressive

    def __getattr__(self, k):
        return getattr(self._inner, k)


def live_book_pnl(data_dir: Path, name: str, day: date) -> dict:
    d = json.loads((Path(data_dir) / "arena" / "accounts" / f"{name}.json").read_text("utf-8"))
    orders = [
        o for o in d["orders"].values() if str(o.get("decided_at", ""))[:10] == day.isoformat()
    ]
    realised = sum(float(o.get("realised") or 0) for o in orders)
    fees = sum(float(o.get("commission") or 0) for o in orders)
    return {
        "realised": round(realised, 2),
        "fees": round(fees, 2),
        "net": round(realised - fees, 2),
    }


def decisions_check(cfg, day: date) -> dict:
    """The frozen rule bots through the live code (the lab's time machine) vs the live bots."""
    from datetime import timedelta

    from asxbot.arena import replay_ibkr as R
    from asxbot.data.universe import asx200_codes

    codes, universe = R.arena_universe(cfg)
    shorts = asx200_codes(cfg.data_dir, cfg.get("collector.user_agent"))
    ann = R.announcements(cfg, day - timedelta(days=5), day)
    rec = R.replay_day(cfg, day, codes, shorts, ann, universe=universe)
    out = {"gaps": rec.get("gaps")}
    for key, live_name in (
        ("daytrader", "asx_daytrader_v1__bot"),
        ("v2", "asx_announcements_v2__bot"),
    ):
        part = rec.get(key) or {}
        trades = [
            (t.get("ticker"), t.get("side"), t.get("first_fill"), t.get("net"))
            for t in part.get("trades") or []
        ]
        live = [(o["ticker"], o["side"], (o.get("fill_minute") or "")[11:16])
                for o in live_orders(cfg.data_dir, day).get(live_name, [])
                if o["side"] in ("buy", "short")]  # fmt: skip
        out[key] = {
            "lab_trades": trades,
            "live_entries": live,
            "lab_pnl": part.get("pnl"),
            "live": live_book_pnl(cfg.data_dir, live_name, day),
        }
    return out


def report(cfg, days: list[date], inputs: Inputs, with_decisions: bool = True) -> str:
    lines = [
        "# Fidelity: the simulator against the live 10-day paper test",
        "",
        "The live paper test is the ground truth. Each live order is sent to this "
        "simulator's broker at its live time; fills and costs are compared. "
        "`sim` = this simulator's fill model (next bar's open, half a modelled spread, "
        "impact); `live-style` = the same broker pricing like the live arena (bar close, "
        "0.10% slippage) - the gap between them is the fill model's own effect.",
        "",
    ]
    for d in days:
        books = live_orders(cfg.data_dir, d)
        lines.append(f"## {d:%a %d %b %Y}")
        if not books:
            lines += ["", "No live orders that day.", ""]
            continue
        for name, orders in books.items():
            sim = replay_orders(d, orders, inputs, cfg)
            ls = replay_orders(d, orders, inputs, cfg, live_style=True)
            live = live_book_pnl(cfg.data_dir, name, d)
            lines += ["", f"### {name}", "",
                      f"Live: realised ${live['realised']:,.2f}, brokerage ${live['fees']:,.2f}, "
                      f"net ${live['net']:,.2f}. Simulator: net ${sim['sim_equity_change']:,.2f} "
                      f"(brokerage ${sim['sim_fees']:,.2f}); "
                      f"live-style ${ls['sim_equity_change']:,.2f}."
                      + (f" Left open in the simulator: {sim['open_positions']}."
                         if sim["open_positions"] else ""),
                      "", "| order | stock | side | type | decided | live min | sim min | live px "
                      "| sim px | diff bps | live-style px | live qty | sim qty |",
                      "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
            lsr = {r["order"]: r for r in ls["rows"]}
            for r in sim["rows"]:
                lines.append(
                    f"| {r['order']} | {r['code']} | {r['side']} | {r['type']} | {r['decided']} | "
                    f"{r['live_minute']} | {r['sim_minute']} | {r['live_price']} | "
                    f"{r['sim_price']} | "
                    f"{r['diff_bps']} | {lsr[r['order']]['sim_price']} | {r['live_filled']} | "
                    f"{r['sim_filled']} |"
                )
            if sim["errors"]:
                lines.append(f"\nRefused in the simulator: {sim['errors']}")
        if with_decisions:
            try:
                dc = decisions_check(cfg, d)
                lines += [
                    "",
                    "### Decisions: the frozen rule bots replayed vs live",
                    "",
                    "```",
                    json.dumps(dc, indent=1, default=str)[:6000],
                    "```",
                ]
            except Exception as e:  # noqa: BLE001 - reported, not hidden
                lines += ["", f"Decision replay failed: {type(e).__name__}: {e}"]
        lines.append("")
    return "\n".join(lines)
