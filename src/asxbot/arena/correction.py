"""Re-price ARN-000002 under the fixed fill rule, once. TRACKER.md #24.

What happened. The watcher took one `now` at the top of its cycle and passed it to every
order placed during that cycle. On 23 Sep the cycle that began at 10:29:46 worked eight
re-looks, each a Sonnet and an Opus call. A1M was the eighth: its decider replied at
10:37:41 and ARN-000002 (buy 3,000 @ 0.84 limit) was recorded then, but stamped 10:29:46 -
and the fill rule took the bar containing that minute, so it filled at the close of the
10:29 bar (0.830 + 0.101% slippage = 0.8308), eight minutes before the decision existed.

The honest fill, under the fixed rule, is the first traded bar that starts strictly after
10:37:41, at its close, with the same slippage and brokerage model. Minute data: no trades
10:38-10:40, then 10:41 closing at 0.835.

What this changes, in the one account holding the order, and nothing else:
  * ARN-000002: decided_at (10:37:41), data_as_of (10:36:02, when the re-look that led to it
    began and read its quote), avg_price, commission, fill_minute, fill_basis, message;
  * the A1M position's avg_cost and opened_at, if it is still open;
  * the realised P&L of each later A1M sell, and the account's realised_pnl;
  * cash and fees_paid, by the difference in what the buy cost;
  * every daily mark from 23 Sep on: cash and equity by the cash difference (an open
    position's market value does not depend on what it cost), realised_day and fees_day on
    the day they moved.

It refuses, writing nothing, if:
  * any arena process is running, or either scheduled arena task reports itself Running;
  * it has already been applied (a fill_corrected event for the order, or the order no longer
    has its original stamp and fill);
  * the book is not the one this was written for: another A1M buy, a sell that filled before
    the corrected entry, a position that no longer matches, a stop_pct to re-derive;
  * the slippage model no longer reproduces the original 0.8308 from the 10:29 bar;
  * the minute data holds no bar after 10:37:41 that meets the 0.84 limit.

Before writing it copies the account and marks files to data/arena/backups/. After writing
it reads both back, proves no other field moved, reloads the account through AccountStore,
and appends a fill_corrected event to events/arena_accounts.jsonl.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot.arena.accounts import AccountStore, ArenaOrder, _order
from asxbot.arena.capital import (
    TASKS,
    Refused,
    arena_processes,
    only_these_changed,
    process_listing,
    task_status,
)
from asxbot.config import Config, load_config
from asxbot.io import write_text_atomic
from asxbot.log import EventLog, setup_logging

SYD = ZoneInfo("Australia/Sydney")
ORDER_ID = "ARN-000002"
TICKER = "A1M"
# data/logs/asxbot.log, 23 Sep: 10:37:41,674 "arena asx_announcements__agent ARN-000002 buy
# A1M 3000 @ 0.840 (decision 2026-09-23T10:29:46+10:00) -> pending fill" - the moment it was
# recorded. 10:36:02,939 "RE-LOOK A1M 03142703" - the re-look that read its quote.
DECIDED_AT = datetime(2026, 9, 23, 10, 37, 41, tzinfo=SYD)
DATA_AS_OF = datetime(2026, 9, 23, 10, 36, 2, tzinfo=SYD)
LOG_LINE = re.compile(
    r"^2026-09-23 (10:37:41),\d+ INFO asxbot\.arena\.broker: arena \S+ ARN-000002 buy A1M 3000 "
)
# The order as the faulty code wrote it.
ORIGINAL = {
    "stamp": "2026-09-23T10:29:46+10:00",
    "fill_minute": "2026-09-23T10:29+10:00",
    "avg_price": 0.8308,
    "side": "buy",
    "qty": 3000,
    "limit": 0.84,
}
EVENT = "fill_corrected"


@dataclass
class Correction:
    account: str
    account_path: Path
    marks_path: Path
    raw: dict
    marks: list[dict]
    new_raw: dict = field(default_factory=dict)
    new_marks: list[dict] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)  # before/after, for the printout
    record: dict = field(default_factory=dict)  # for the event log


def _find(store: AccountStore) -> tuple[str, Path, dict]:
    for name in store.names():
        path = store.path(name)
        raw = json.loads(path.read_text(encoding="utf-8"))
        if ORDER_ID in raw.get("orders", {}):
            return name, path, raw
    raise Refused(f"no account holds {ORDER_ID}")


def already_applied(events: EventLog, raw_order: dict) -> str | None:
    """Why this has already been applied (or the order is no longer the one it was written
    for), or None."""
    for r in events.read("arena_accounts"):
        if r.get("event") == EVENT and r.get("order_id") == ORDER_ID:
            return f"a {EVENT} event for {ORDER_ID} is already in events/arena_accounts.jsonl"
    stamp = raw_order.get("decision_at", raw_order.get("decided_at"))
    if raw_order.get("data_as_of") or stamp != ORIGINAL["stamp"]:
        return f"{ORDER_ID} is already stamped {stamp}, not {ORIGINAL['stamp']}"
    if raw_order.get("fill_minute") != ORIGINAL["fill_minute"]:
        return f"{ORDER_ID} filled at {raw_order.get('fill_minute')}, not {ORIGINAL['fill_minute']}"
    return None


def log_confirms(log_dir: Path) -> str:
    """What the log says about when ARN-000002 was recorded."""
    for name in ("asxbot.log", "asxbot.log.2026-09-23"):
        p = log_dir / name
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            m = LOG_LINE.match(line)
            if m:
                if m.group(1) != DECIDED_AT.strftime("%H:%M:%S"):
                    raise Refused(f"the log records {ORDER_ID} at {m.group(1)}, not 10:37:41")
                return f"confirmed by {p.name}: recorded at 10:37:41"
    return "the log line was not found (rotated?); using 10:37:41 as recorded in TRACKER #24"


def plan(cfg: Config, broker, store: AccountStore, events: EventLog) -> Correction:
    name, path, raw = _find(store)
    ro = raw["orders"][ORDER_ID]
    why = already_applied(events, ro)
    if why:
        raise Refused(f"already applied, or not the order this was written for: {why}")
    for k in ("side", "qty", "limit"):
        if ro.get(k) != ORIGINAL[k]:
            raise Refused(f"{ORDER_ID} {k} is {ro.get(k)!r}, not {ORIGINAL[k]!r}")
    if ro.get("status") != "filled" or ro.get("ticker") != TICKER:
        raise Refused(f"{ORDER_ID} is {ro.get('status')} {ro.get('ticker')}, not a filled {TICKER}")
    if ro.get("stop_pct") is not None:
        raise Refused(f"{ORDER_ID} has a stop_pct; its stop would need re-deriving")

    order = _order(ro)
    qty = int(order.qty)
    old_minute = datetime.fromisoformat(ORIGINAL["fill_minute"]).astimezone(SYD)

    # The original fill, reproduced from the minute data and today's cost model. If they no
    # longer give 0.8308, the "same slippage" this correction promises cannot be claimed.
    pending = ArenaOrder(**{**order.__dict__, "status": "pending_fill"})
    old_bar = broker.minutes.fetch(TICKER, old_minute.date())
    if old_bar is None or old_minute not in old_bar.index:
        raise Refused(f"the minute data has no {old_minute:%H:%M} bar for {TICKER}")
    adv = broker.adv_lookup(TICKER)
    slip = broker.costs.slippage_pct(qty * order.limit, adv)
    old_raw = float(old_bar.loc[old_minute, broker.minutes.price_field])
    old_px = old_raw * (1 + slip)
    if round(old_px, 4) != ORIGINAL["avg_price"]:
        raise Refused(
            f"the cost model gives {old_raw:.4f} x (1 + {slip * 100:.4f}%) = {old_px:.4f} for the "
            f"original fill, not {ORIGINAL['avg_price']}; the slippage has changed"
        )
    recorded = re.search(r"([\d.]+)% slippage", ro.get("fill_basis") or "")
    if recorded and f"{slip * 100:.3f}" != recorded.group(1):
        raise Refused(f"slippage now {slip * 100:.3f}%, recorded {recorded.group(1)}%")
    old_fee = broker.costs.brokerage(qty * old_px)
    if round(old_fee, 2) != round(float(ro.get("commission", 0.0)), 2):
        raise Refused(
            f"brokerage now {old_fee:.2f} on the original fill, recorded {ro['commission']}"
        )

    # The honest fill: the fixed rule, the same model.
    chosen, new_slip = broker.find_fill(pending, DECIDED_AT)
    if chosen is None:
        raise Refused(f"no bar after {DECIDED_AT:%H:%M:%S} meets the {order.limit} limit yet")
    ts, new_px, new_bar_px = chosen
    if abs(new_slip - slip) > 1e-12:
        raise Refused("the slippage for the corrected fill differs from the original's")
    new_fee = broker.costs.brokerage(qty * new_px)
    d_cost = new_px - old_px  # per share
    d_cash = (qty * old_px + old_fee) - (qty * new_px + new_fee)
    d_fee = new_fee - old_fee

    # Everything downstream of the buy, in this account.
    others = [
        o for oid, o in raw["orders"].items()
        if oid != ORDER_ID and o.get("ticker") == TICKER and o.get("status") == "filled"
    ]  # fmt: skip
    if any(o.get("side") != "sell" for o in others):
        raise Refused(f"another filled {TICKER} order that is not a sell; not written for that")
    closed = 0
    d_realised_by_day: dict[str, float] = {}
    new_orders = dict(raw["orders"])
    for o in others:
        fm = datetime.fromisoformat(o["fill_minute"])
        if fm <= ts:
            raise Refused(
                f"{o['order_id']} sold at {fm:%H:%M}, before the corrected entry {ts:%H:%M}"
            )
        closed += int(o["filled_qty"])
        d = -d_cost * int(o["filled_qty"])
        day = fm.date().isoformat()
        d_realised_by_day[day] = d_realised_by_day.get(day, 0.0) + d
        new_orders[o["order_id"]] = {**o, "realised": round(float(o["realised"]) + d, 2)}
    if closed > qty:
        raise Refused(f"{closed} {TICKER} sold against a {qty} buy")

    new_positions = dict(raw.get("positions", {}))
    pos = new_positions.get(TICKER)
    if closed < qty:
        if pos is None or int(pos["qty"]) != qty - closed or abs(pos["avg_cost"] - old_px) > 1e-6:
            raise Refused(f"the {TICKER} position does not match {qty} bought less {closed} sold")
        new_positions[TICKER] = {
            **pos, "avg_cost": new_px, "opened_at": ts.isoformat(timespec="minutes"),
        }  # fmt: skip
    elif pos is not None:
        raise Refused(f"{TICKER} is fully sold but a position is still on the book")

    new_ro = {k: v for k, v in ro.items() if k != "decision_at"}
    new_ro.update(
        decided_at=DECIDED_AT.isoformat(timespec="seconds"),
        data_as_of=DATA_AS_OF.isoformat(timespec="seconds"),
        avg_price=round(new_px, 4),
        commission=round(new_fee, 2),
        fill_minute=ts.isoformat(timespec="minutes"),
        fill_basis=(
            broker.fill_basis(order, DECIDED_AT, ts, new_bar_px, slip)
            + f" [re-priced by scripts/correct_fill_arn000002.py: first stamped "
            f"{ORIGINAL['stamp'][11:19]} (the start of the watcher's cycle) and filled at "
            f"the 10:29 bar at {ORIGINAL['avg_price']}, before the decision existed]"
        ),
        message="filled; re-priced under the fixed fill rule (TRACKER #24)",
    )
    new_orders[ORDER_ID] = new_ro
    d_realised = sum(d_realised_by_day.values())
    new_raw = {
        **raw,
        "cash": float(raw["cash"]) + d_cash,
        "fees_paid": float(raw["fees_paid"]) + d_fee,
        "realised_pnl": float(raw["realised_pnl"]) + d_realised,
        "orders": new_orders,
        "positions": new_positions,
    }

    mpath = store.marks_path(name)
    marks = [
        json.loads(ln)
        for ln in (mpath.read_text(encoding="utf-8").splitlines() if mpath.exists() else [])
        if ln.strip()
    ]
    fill_day = ts.date().isoformat()
    new_marks = []
    for m in marks:
        if m["day"] < fill_day:
            new_marks.append(m)
            continue
        m2 = {**m, "cash": round(float(m["cash"]) + d_cash, 2),
              "equity": round(float(m["equity"]) + d_cash, 2)}  # fmt: skip
        if m["day"] == fill_day:
            m2["fees_day"] = round(float(m["fees_day"]) + d_fee, 2)
        if m["day"] in d_realised_by_day:
            m2["realised_day"] = round(float(m["realised_day"]) + d_realised_by_day[m["day"]], 2)
        new_marks.append(m2)

    c = Correction(name, path, mpath, raw, marks, new_raw, new_marks)

    def row(label: str, before: str, after: str) -> str:
        return f"  {label:<12} {before:<26} -> {after}"

    c.lines = [
        f"{name} {ORDER_ID} buy {qty} {TICKER} @ {order.limit} limit",
        row("decided_at", ORIGINAL["stamp"], new_ro["decided_at"]),
        row("data_as_of", "(none)", new_ro["data_as_of"]),
        row("fill bar", f"{old_minute:%H:%M} close {old_raw:.4f}",
            f"{ts:%H:%M} close {new_bar_px:.4f}"),
        row("fill price", f"{old_px:.4f} ({slip * 100:.3f}% slip)",
            f"{new_px:.4f} ({slip * 100:.3f}% slip)"),
        row("commission", f"{old_fee:.2f}", f"{new_fee:.2f}"),
        row("cost", f"{qty * old_px + old_fee:,.2f}", f"{qty * new_px + new_fee:,.2f}"),
        row("cash", f"{float(raw['cash']):,.2f}", f"{new_raw['cash']:,.2f} ({d_cash:+,.2f})"),
        row("realised", f"{float(raw['realised_pnl']):,.2f}",
            f"{new_raw['realised_pnl']:,.2f} ({d_realised:+,.2f})"),
        row("fees_paid", f"{float(raw['fees_paid']):,.2f}", f"{new_raw['fees_paid']:,.2f}"),
    ]  # fmt: skip
    for o in others:
        c.lines.append(
            f"  {o['order_id']} {o['side']} {o['filled_qty']} @ {o['avg_price']}: realised "
            f"{o['realised']:,.2f} -> {new_orders[o['order_id']]['realised']:,.2f}"
        )
    if TICKER in new_positions:
        c.lines.append(
            f"  position     avg_cost {pos['avg_cost']:.4f} -> {new_px:.4f}, opened_at "
            f"{pos['opened_at']} -> {new_positions[TICKER]['opened_at']}"
        )
    else:
        c.lines.append(f"  position     none ({TICKER} fully sold); avg_cost enters via realised")
    restated = [m["day"] for m in marks if m["day"] >= fill_day]
    c.lines.append(
        f"  marks        {len(restated)} restated ({', '.join(restated) or 'none yet'}): cash and "
        f"equity {d_cash:+,.2f}"
        + (f", realised_day {d_realised:+,.2f}" if restated and d_realised else "")
    )
    c.record = {
        "account": name, "event": EVENT, "order_id": ORDER_ID, "ticker": TICKER,
        "why": "filled at the 10:29 bar from a stale decision time; decided 10:37:41 "
               "(TRACKER #24)",
        "before": {"decided_at": ORIGINAL["stamp"], "fill_minute": ro["fill_minute"],
                   "avg_price": ro["avg_price"], "commission": ro["commission"]},
        "after": {k: new_ro[k] for k in ("decided_at", "data_as_of", "fill_minute",
                                          "avg_price", "commission")},
        "cash_before": float(raw["cash"]), "cash_after": new_raw["cash"],
        "realised_before": float(raw["realised_pnl"]), "realised_after": new_raw["realised_pnl"],
        "fees_before": float(raw["fees_paid"]), "fees_after": new_raw["fees_paid"],
        "orders_restated": [o["order_id"] for o in others], "marks_restated": restated,
    }  # fmt: skip
    return c


ALLOWED = {"cash", "fees_paid", "realised_pnl", "orders", "positions"}


def run(cfg: Config, now: datetime, dry_run: bool, checks: bool = True, broker=None) -> int:
    log = setup_logging(cfg.logs_dir)
    store = AccountStore(cfg.data_dir)
    events = EventLog(cfg.data_dir)

    blocked = None
    if checks:
        try:
            running = arena_processes(process_listing(), os.getpid())
            if running:
                raise Refused("an arena process is running:\n  " + "\n  ".join(running))
            for task in TASKS:
                if any(s.lower() == "running" for s in task_status(task)):
                    raise Refused(f"the scheduled task {task!r} is running")
        except Refused as e:
            if not dry_run:
                raise
            blocked = str(e)
    print(log_confirms(cfg.logs_dir))

    if broker is None:
        from asxbot.arena.runtime import arena_broker

        broker = arena_broker(cfg)
    c = plan(cfg, broker, store, events)
    print("\n".join(c.lines))
    if dry_run:
        if blocked:
            print(f"dry run: nothing written. A real run now would REFUSE: {blocked}")
            return 2
        print("dry run: nothing written. Checks passed; a real run now would apply this.")
        return 0

    backup = cfg.data_dir / "arena" / "backups" / f"correct_arn000002_{now:%Y%m%d_%H%M%S}"
    backup.mkdir(parents=True, exist_ok=False)
    shutil.copy2(c.account_path, backup / c.account_path.name)
    if c.marks_path.exists():
        shutil.copy2(c.marks_path, backup / c.marks_path.name)

    write_text_atomic(json.dumps(c.new_raw, indent=2), c.account_path)
    if c.new_marks:
        write_text_atomic("\n".join(json.dumps(m) for m in c.new_marks) + "\n", c.marks_path)

    # Read back what is on disk, and prove nothing else moved.
    got = json.loads(c.account_path.read_text(encoding="utf-8"))
    stray = only_these_changed(c.raw, got, ALLOWED)
    changed_orders = {ORDER_ID, *c.record["orders_restated"]}
    stray += [
        f"order {oid}" for oid in set(c.raw["orders"]) | set(got["orders"])
        if oid not in changed_orders and c.raw["orders"].get(oid) != got["orders"].get(oid)
    ]  # fmt: skip
    stray += [
        f"position {t}" for t in set(c.raw["positions"]) | set(got["positions"])
        if t != TICKER and c.raw["positions"].get(t) != got["positions"].get(t)
    ]  # fmt: skip
    got_marks = (
        [json.loads(ln) for ln in c.marks_path.read_text(encoding="utf-8").splitlines() if ln]
        if c.new_marks else []
    )  # fmt: skip
    if got_marks != c.new_marks or got != c.new_raw:
        stray.append("the files do not read back as written")
    if stray:
        raise Refused(f"{c.account} did not read back cleanly ({stray}); restore from {backup}")
    acct = store.open(c.account, got["playbook"], got["kind"], int(got["level"]), 0.0)
    if acct.orders[ORDER_ID].decided_at != DECIDED_AT.isoformat(timespec="seconds"):
        raise Refused(f"{c.account} reloads without the correction; restore from {backup}")

    rec = {**c.record, "backup": str(backup)}
    events.append("arena_accounts", rec)
    log.info("arena fill corrected: %s", rec)
    print(f"done. Backup in {backup}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="run every check, write nothing")
    args = ap.parse_args(argv)
    cfg = load_config()
    try:
        return run(cfg, datetime.now(SYD), args.dry_run)
    except Refused as e:
        print(f"REFUSED - nothing was written: {e}")
        return 2
