"""Daily reconciliation: broker positions vs positions implied by the fills log."""

from __future__ import annotations

from dataclasses import dataclass

from asxbot.alerts import Alerts
from asxbot.broker.base import Broker
from asxbot.log import EventLog, get_logger

log = get_logger("asxbot.broker.reconcile")
RECONCILE_MISMATCH = "reconcile_mismatch"


@dataclass
class Mismatch:
    ticker: str
    broker_qty: int
    log_qty: int


def positions_from_log(events: EventLog) -> dict[str, int]:
    out: dict[str, int] = {}
    for f in events.read("fills"):
        q = int(f["qty"]) * (1 if f["side"] == "buy" else -1)
        out[f["ticker"]] = out.get(f["ticker"], 0) + q
    return {t: q for t, q in out.items() if q != 0}


def reconcile(broker: Broker, events: EventLog, alerts: Alerts) -> list[Mismatch]:
    b = {p.ticker: p.qty for p in broker.positions()}
    lg = positions_from_log(events)
    out = []
    for t in sorted(set(b) | set(lg)):
        if b.get(t, 0) != lg.get(t, 0):
            out.append(Mismatch(t, b.get(t, 0), lg.get(t, 0)))
    rec = {"broker": b, "log": lg, "mismatches": [m.__dict__ for m in out]}
    events.append("reconciliation", rec)
    if out:
        alerts.raise_alert(
            RECONCILE_MISMATCH,
            "; ".join(f"{m.ticker}: broker {m.broker_qty} vs log {m.log_qty}" for m in out),
        )
    else:
        alerts.clear(RECONCILE_MISMATCH)
        log.info("reconciliation clean: %d positions", len(b))
    return out
