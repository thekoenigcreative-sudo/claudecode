"""Re-price ARN-000003 under the volume-aware fill rule, once. TRACKER.md #25.

What happened. A1M's take-profit (target 0.90, honoured from 15:56 on 23 Sep) was reached in
the 15:57 bar, and the broker sold all 3,000 shares in that bar at 0.917 less slippage
(0.9161). One share traded in the 15:57 bar. Since 2026-09-24 no bar fills more than
`arena.fill.max_volume_share` (20%) of the shares it traded, and the rest carries to later
bars at their prices (broker.work).

How the honest fill is found. A1M's day is replayed through the new broker: the position as
it stood before the exit (3,000 bought at ARN-000002's fill, 10:41; stop 0.755; target 0.90
honoured from 15:56, when the watcher first saw it) is worked bar by bar from its entry to
the close, in a scratch book that is never saved, against the cached minute bars of 23 Sep
and nothing else (read-only: the file must already hold the 16:10 closing auction). Whatever
the replay produces is the corrected ARN-000003: the target exit, the bars it filled in, the
price, the brokerage (one minimum per order per day). If the replay does anything else - a
stop, a second order, a remainder still working at the close - it refuses: the book is not
the one this was written for.

What this changes, in the one account holding the order, and nothing else:
  * ARN-000003: decided_at (16:19:45, when the code recorded it; the old code stamped the
    trigger minute), data_as_of (the 15:57 bar that reached the target), rests_from (15:56),
    order_type, trigger_price, fills, worked_through, avg_price, commission, realised,
    fill_minute, fill_basis, message;
  * cash, fees_paid and realised_pnl, by the differences;
  * the 23 Sep mark and any later one: cash and equity by the cash difference; realised_day
    and fees_day on 23 Sep.

It refuses, writing nothing, if any arena process or scheduled arena task is running; if it
has already been applied; if ARN-000002 has not been corrected first; if the order, the
position or the costs are not what this was written for; or if the cached minute bars of
23 Sep are missing or incomplete.

Before writing it copies the account and marks files to data/arena/backups/. After writing
it reads both back, proves no other field moved, reloads the account through AccountStore,
and appends a fill_corrected event to events/arena_accounts.jsonl.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot.arena.accounts import Account, AccountStore, Position
from asxbot.arena.broker import ArenaBroker
from asxbot.arena.capital import (
    TASKS,
    Refused,
    arena_processes,
    only_these_changed,
    process_listing,
    task_status,
)
from asxbot.arena.minutes import MinuteBars
from asxbot.config import Config, load_config
from asxbot.io import write_text_atomic
from asxbot.log import EventLog, setup_logging

SYD = ZoneInfo("Australia/Sydney")
ORDER_ID = "ARN-000003"
ENTRY_ID = "ARN-000002"
TICKER = "A1M"
DAY = date(2026, 9, 23)
EVENT = "fill_corrected"
# data/logs/asxbot.log, 23 Sep: 16:19:45,485 "arena asx_announcements__agent ARN-000003 FILLED
# sell A1M 3000 @ 0.9161" - the moment the code recorded it. 15:56:55 "target 0.9000 honoured
# from 2026-09-23T15:56+10:00".
RECORDED_AT = datetime(2026, 9, 23, 16, 19, 45, tzinfo=SYD)
LOG_LINE = re.compile(
    r"^2026-09-23 (16:19:45),\d+ INFO asxbot\.arena\.broker: arena \S+ ARN-000003 FILLED sell "
    r"A1M 3000 @ 0\.9161"
)
TARGET_FROM = "2026-09-23T15:56+10:00"
# The order as the volume-blind code wrote it (after ARN-000002's correction restated its
# realised P&L).
ORIGINAL = {
    "side": "sell", "qty": 3000, "status": "filled", "filled_qty": 3000,
    "avg_price": 0.9161, "fill_minute": "2026-09-23T15:57+10:00", "placed_by": "code",
    "decided_at": "2026-09-23T15:57:00+10:00",
}  # fmt: skip
ENTRY_FILL_MINUTE = "2026-09-23T10:41+10:00"  # ARN-000002 after its correction
STOP = 0.755
TARGET = 0.90
# The whole day is replayed with the clock here: after the close, so every bar is final.
REPLAY_NOW = datetime(2026, 9, 23, 23, 59, tzinfo=SYD)


class CachedBars(MinuteBars):
    """The minute store, read-only: the cached file or nothing. No request, no write."""

    def fetch(self, code, day, force=False):
        return self.cached(code, day)


@dataclass
class Correction:
    account: str
    account_path: Path
    marks_path: Path
    raw: dict
    marks: list[dict]
    new_raw: dict = field(default_factory=dict)
    new_marks: list[dict] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    record: dict = field(default_factory=dict)


def _find(store: AccountStore) -> tuple[str, Path, dict]:
    for name in store.names():
        path = store.path(name)
        raw = json.loads(path.read_text(encoding="utf-8"))
        if ORDER_ID in raw.get("orders", {}):
            return name, path, raw
    raise Refused(f"no account holds {ORDER_ID}")


def already_applied(events: EventLog, ro: dict) -> str | None:
    for r in events.read("arena_accounts"):
        if r.get("event") == EVENT and r.get("order_id") == ORDER_ID:
            return f"a {EVENT} event for {ORDER_ID} is already in events/arena_accounts.jsonl"
    if ro.get("fills") or ro.get("order_type") or ro.get("data_as_of"):
        return f"{ORDER_ID} already carries fills/order_type/data_as_of"
    return None


def log_confirms(log_dir: Path) -> str:
    for name in ("asxbot.log", "asxbot.log.2026-09-23"):
        p = log_dir / name
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if LOG_LINE.match(line):
                return f"confirmed by {p.name}: {ORDER_ID} recorded at 16:19:45"
    return "the log line was not found (rotated?); using 16:19:45 as recorded in TRACKER #25"


def replay(cfg: Config, broker: ArenaBroker, entry: dict, level: int):
    """A1M's day under the new broker, in a scratch book. Returns the exit order. The
    scratch broker's own log lines are kept out of the real log: they describe no real book."""
    quiet = logging.getLogger("asxbot.arena.broker")
    level_before = quiet.level
    quiet.setLevel(logging.WARNING)
    try:
        return _replay(cfg, broker, entry, level)
    finally:
        quiet.setLevel(level_before)


def _replay(cfg: Config, broker: ArenaBroker, entry: dict, level: int):
    with tempfile.TemporaryDirectory() as tmp:
        scratch = ArenaBroker(
            Path(tmp), broker.costs, CachedBars(cfg.data_dir, broker.minutes.price_field),
            broker.adv_lookup, resolve_after_minutes=broker.resolve_after_minutes,
            clock=lambda: RECORDED_AT, max_volume_share=broker.max_volume_share,
            settle_minutes=broker.settle_minutes,
        )  # fmt: skip
        acct = Account(
            name="scratch__agent", playbook="asx_announcements", kind="agent", level=level,
            starting_cash=0.0, cash=0.0,
        )  # fmt: skip
        acct.positions[TICKER] = Position(
            ticker=TICKER, qty=int(entry["filled_qty"]), avg_cost=float(entry["avg_price"]),
            opened_at=ENTRY_FILL_MINUTE, stop=STOP, target=TARGET, opened_by="agent",
            model=entry.get("model", ""), hold=entry.get("hold", "intraday"),
            target_from=TARGET_FROM, target_past_when_armed=True,
        )  # fmt: skip
        scratch.work(acct, REPLAY_NOW)
        orders = list(acct.orders.values())
        if len(orders) != 1 or orders[0].order_type != "target":
            raise Refused(
                "the replay did not produce exactly one target exit: "
                + "; ".join(
                    f"{o.order_type} {o.side} {o.filled_qty}/{o.qty} {o.status}" for o in orders
                )  # fmt: skip
            )
        o = orders[0]
        if o.status != "filled" or TICKER in acct.positions:
            raise Refused(f"the replayed exit is {o.status}, {o.filled_qty} of {o.qty}")
        if any(f["minute"][:10] != DAY.isoformat() for f in o.fills):
            raise Refused("the replayed exit fills on another day")
        return o


def plan(cfg: Config, broker: ArenaBroker, store: AccountStore, events: EventLog) -> Correction:
    name, path, raw = _find(store)
    ro = raw["orders"][ORDER_ID]
    why = already_applied(events, ro)
    if why:
        raise Refused(f"already applied, or not the order this was written for: {why}")
    ro = {**ro}
    if "decision_at" in ro:  # the field's name in books written before 2026-09-24
        ro.setdefault("decided_at", ro.pop("decision_at"))
    for k, v in ORIGINAL.items():
        if ro.get(k) != v:
            raise Refused(f"{ORDER_ID} {k} is {ro.get(k)!r}, not {v!r}")
    if ro.get("ticker") != TICKER or not str(ro.get("reason", "")).startswith("TARGET"):
        raise Refused(f"{ORDER_ID} is not {TICKER}'s target exit")
    entry = raw["orders"].get(ENTRY_ID) or {}
    if entry.get("fill_minute") != ENTRY_FILL_MINUTE or entry.get("status") != "filled":
        raise Refused(
            f"{ENTRY_ID} filled at {entry.get('fill_minute')}, not {ENTRY_FILL_MINUTE}: run "
            "scripts/correct_fill_arn000002.py first"
        )
    others = [oid for oid, o in raw["orders"].items()
              if o.get("ticker") == TICKER and oid not in (ORDER_ID, ENTRY_ID)]  # fmt: skip
    if others or TICKER in raw.get("positions", {}):
        raise Refused(f"the book holds other {TICKER} orders or a position: {others}")

    bars = CachedBars(cfg.data_dir, broker.minutes.price_field).cached(TICKER, DAY)
    if bars is None or not len(bars) or (bars.index.max().hour, bars.index.max().minute) < (16, 10):
        raise Refused(f"the cached {TICKER} minute bars of {DAY} are missing or incomplete")
    trigger_minute = datetime.fromisoformat(ORIGINAL["fill_minute"])
    bar = bars.loc[trigger_minute]
    old_raw = max(TARGET, float(bar["open"]))
    adv = broker.adv_lookup(TICKER)
    slip = broker.costs.slippage_pct(3000 * old_raw, adv)
    old_px = old_raw * (1 - slip)
    if round(old_px, 4) != ORIGINAL["avg_price"]:
        raise Refused(
            f"the cost model gives {old_raw:.4f} x (1 - {slip * 100:.4f}%) = {old_px:.4f} for the "
            f"original fill, not {ORIGINAL['avg_price']}; the slippage has changed"
        )
    recorded = re.search(r"([\d.]+)% slippage", ro.get("fill_basis") or "")
    if recorded and f"{slip * 100:.3f}" != recorded.group(1):
        raise Refused(f"slippage now {slip * 100:.3f}%, recorded {recorded.group(1)}%")
    old_fee = broker.costs.brokerage(3000 * old_px)
    if round(old_fee, 2) != round(float(ro["commission"]), 2):
        raise Refused(f"brokerage now {old_fee:.2f}, recorded {ro['commission']}")

    new = replay(cfg, broker, entry, int(raw.get("level", 1)))
    new_value = sum(f["qty"] * f["price"] for f in new.fills)
    new_avg = new_value / new.filled_qty
    new_fee = float(new.commission)
    d_realised = new_value - 3000 * old_px
    d_fee = new_fee - old_fee
    d_cash = d_realised - d_fee

    new_ro = {**ro}
    for k, v in new.to_dict().items():
        if k in ("order_id", "account"):
            continue
        new_ro[k] = v
    new_ro.update(
        decided_at=RECORDED_AT.isoformat(timespec="seconds"),
        realised=round(float(ro["realised"]) + d_realised, 2),
        fill_basis=(
            new.fill_basis + " [re-priced by scripts/correct_fill_arn000003.py: first filled "
            f"all 3,000 in the 15:57 bar, where 1 share traded, at {ORIGINAL['avg_price']}]"
        ),
        message="filled; re-priced under the volume-aware fill rule (TRACKER #25)",
    )
    new_orders = {**raw["orders"], ORDER_ID: new_ro}
    new_raw = {
        **raw,
        "cash": float(raw["cash"]) + d_cash,
        "fees_paid": float(raw["fees_paid"]) + d_fee,
        "realised_pnl": float(raw["realised_pnl"]) + d_realised,
        "orders": new_orders,
    }

    mpath = store.marks_path(name)
    marks = [
        json.loads(ln)
        for ln in (mpath.read_text(encoding="utf-8").splitlines() if mpath.exists() else [])
        if ln.strip()
    ]
    day = DAY.isoformat()
    new_marks = []
    for m in marks:
        if m["day"] < day:
            new_marks.append(m)
            continue
        m2 = {**m, "cash": round(float(m["cash"]) + d_cash, 2),
              "equity": round(float(m["equity"]) + d_cash, 2)}  # fmt: skip
        if m["day"] == day:
            m2["fees_day"] = round(float(m["fees_day"]) + d_fee, 2)
            m2["realised_day"] = round(float(m["realised_day"]) + d_realised, 2)
        new_marks.append(m2)

    c = Correction(name, path, mpath, raw, marks, new_raw, new_marks)

    def row(label: str, before: str, after: str) -> str:
        return f"  {label:<12} {before:<34} -> {after}"

    slices = ", ".join(f"{f['minute'][11:16]} {f['qty']:,} of {f['bar_volume']:,.0f} @ "
                       f"{f['bar_price']:.3f}" for f in new.fills)  # fmt: skip
    c.lines = [
        f"{name} {ORDER_ID} sell 3000 {TICKER}, target {TARGET} (honoured from 15:56)",
        row("decided_at", ORIGINAL["decided_at"], new_ro["decided_at"]),
        row("fill bars", f"15:57 (volume {float(bar['volume']):,.0f}): 3,000 @ {old_raw:.3f}",
            slices),
        row("fill price", f"{old_px:.4f} ({slip * 100:.3f}% slip)",
            f"{new_avg:.4f} (average, {slip * 100:.3f}% slip)"),
        row("commission", f"{old_fee:.2f}", f"{new_fee:.2f}"),
        row("proceeds", f"{3000 * old_px - old_fee:,.2f}", f"{new_value - new_fee:,.2f}"),
        row("cash", f"{float(raw['cash']):,.2f}", f"{new_raw['cash']:,.2f} ({d_cash:+,.2f})"),
        row("realised", f"{float(raw['realised_pnl']):,.2f}",
            f"{new_raw['realised_pnl']:,.2f} ({d_realised:+,.2f})"),
        row(f"{ORDER_ID}", f"realised {float(ro['realised']):,.2f}",
            f"realised {new_ro['realised']:,.2f}"),
        row("fees_paid", f"{float(raw['fees_paid']):,.2f}", f"{new_raw['fees_paid']:,.2f}"),
    ]  # fmt: skip
    restated = [m["day"] for m in marks if m["day"] >= day]
    c.lines.append(
        f"  marks        {len(restated)} restated ({', '.join(restated) or 'none'}): cash and "
        f"equity {d_cash:+,.2f}, realised_day {d_realised:+,.2f}, fees_day {d_fee:+,.2f}"
    )
    c.record = {
        "account": name, "event": EVENT, "order_id": ORDER_ID, "ticker": TICKER,
        "why": "3,000 shares filled in a bar where 1 traded; re-priced under the "
               "volume-aware fill rule (TRACKER #25)",
        "before": {k: ro[k] for k in ("decided_at", "fill_minute", "avg_price", "commission",
                                      "realised")},
        "after": {k: new_ro[k] for k in ("decided_at", "fill_minute", "avg_price", "commission",
                                         "realised")} | {"fills": new_ro["fills"]},
        "cash_before": float(raw["cash"]), "cash_after": new_raw["cash"],
        "realised_before": float(raw["realised_pnl"]), "realised_after": new_raw["realised_pnl"],
        "fees_before": float(raw["fees_paid"]), "fees_after": new_raw["fees_paid"],
        "marks_restated": restated,
    }  # fmt: skip
    return c


ALLOWED = {"cash", "fees_paid", "realised_pnl", "orders"}


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

    backup = cfg.data_dir / "arena" / "backups" / f"correct_arn000003_{now:%Y%m%d_%H%M%S}"
    backup.mkdir(parents=True, exist_ok=False)
    shutil.copy2(c.account_path, backup / c.account_path.name)
    if c.marks_path.exists():
        shutil.copy2(c.marks_path, backup / c.marks_path.name)

    write_text_atomic(json.dumps(c.new_raw, indent=2), c.account_path)
    if c.new_marks:
        write_text_atomic("\n".join(json.dumps(m) for m in c.new_marks) + "\n", c.marks_path)

    got = json.loads(c.account_path.read_text(encoding="utf-8"))
    stray = only_these_changed(c.raw, got, ALLOWED)
    stray += [
        f"order {oid}" for oid in set(c.raw["orders"]) | set(got["orders"])
        if oid != ORDER_ID and c.raw["orders"].get(oid) != got["orders"].get(oid)
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
    if acct.orders[ORDER_ID].fills != c.new_raw["orders"][ORDER_ID]["fills"]:
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
