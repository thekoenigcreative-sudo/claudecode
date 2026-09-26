"""The real chaos test of the persistent connection, against the live IB Gateway (25 Sep 2026).

Read-only, client id 45 (the watcher is 41, the check 42, the supervisor's probe 43), with
frozen data when the market is closed. It never touches Gateway's process: the one case it
cannot stage for real - Gateway killed - is the fakes' (tests/test_ibkr_live.py), and the
socket drop here is the same event as ib_async sees when Gateway dies.

    C:\\venvs\\asx-bot\\Scripts\\python.exe scripts\\ibkr_live_chaos.py

Steps, each PASS or FAIL, one line each, and a JSON record in reports\\ibkr_chaos_<stamp>.json:
  1. connect and hold: the heartbeat answers;
  2. subscribe real-time bars for three contracts (the index among them) and a quote;
  3. a burst of paced history requests all answered, no pacing violation;
  4. an unknown code is excluded, not retried forever;
  5. the socket dropped from our side: the connection comes back by itself, every
     subscription is re-requested (new request ids), the catch-up history answered;
  6. a frozen quote comes through (proves the real-time subscription out of hours).
Exit 0 only if every step passed.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from asxbot.config import load_config, repo_root  # noqa: E402
from asxbot.ibkr.gateway import settings_from_config  # noqa: E402
from asxbot.ibkr.live import LiveGateway  # noqa: E402
from asxbot.log import setup_logging  # noqa: E402

SYD = ZoneInfo("Australia/Sydney")
CODES = ["BHP", "CBA", "^AXJO"]
BURST = ["BHP", "CBA", "CSL", "NAB", "WBC", "ANZ", "WES", "MQG", "WDS", "FMG", "RIO", "TLS"]


def wait(cond, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.2)
    return cond()


def subscriptions(gw: LiveGateway) -> dict[str, int]:
    """symbol -> request id, straight from ib_async's own registry (the artefact)."""
    ib = gw.ib
    if ib is None:
        return {}
    out = {}
    for req_id, sub in list(getattr(ib.wrapper, "reqId2Subscriber", {}).items()):
        c = getattr(sub, "contract", None)
        if c is not None:
            out[c.symbol] = int(req_id)
    return out


def main() -> int:
    cfg = load_config()
    setup_logging(cfg.logs_dir)
    s = dataclasses.replace(settings_from_config(cfg), client_id=45)
    now = datetime.now(SYD)
    rec: dict = {"at": now.isoformat(timespec="seconds"), "steps": []}
    ok_all = True

    def step(name: str, ok: bool, detail: str) -> None:
        nonlocal ok_all
        ok_all &= ok
        rec["steps"].append({"step": name, "ok": ok, "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'}: {name} - {detail}")

    gw = LiveGateway(s, history_dir=Path(cfg.data_dir) / "arena" / "ibkr_chaos_history")
    gw.start()
    try:
        # 1. connect and hold
        ready = gw.wait_ready(30)
        step("connect", ready, f"ready={ready} health={gw.health.last_error or 'ok'} "
                               f"link {'up' if gw.health.server_ok else 'down'}")
        if not ready:
            return 1
        time.sleep(max(2.0, float(s.heartbeat_s) * 0 + 2.0))
        hb = gw.heartbeat_age_s()
        step("heartbeat", hb is not None and hb < s.heartbeat_s + s.heartbeat_timeout_s,
             f"last answer {hb:.1f}s ago" if hb is not None else "no answer yet")

        # 2. subscriptions
        gw.set_streaming(CODES)
        got = wait(lambda: all(gw.streaming(c) for c in CODES), 20)
        subs1 = subscriptions(gw)
        step("subscribe", got and len(subs1) >= 3,
             f"streaming {sorted(c for c in CODES if gw.streaming(c))}; ib_async registry {subs1}")

        # 3. a paced burst of history
        n0 = gw.history_answered
        for c in BURST:
            gw.queue_history(c, "prior", now.date(), 2)
        done = wait(lambda: all(gw.history_done(c, now.date()) for c in BURST), 90)
        step("history burst", done and gw.health.pacing_hits == 0,
             f"{gw.history_answered - n0} answered of {len(BURST)} in the queue, pacing hits "
             f"{gw.health.pacing_hits}, given up {sorted(gw.hist_given_up)}")

        # 4. an unknown code
        gw.queue_history("ZZZZ", "prior", now.date(), 2)
        excluded = wait(lambda: "ZZZZ" in gw.unknown_codes
                        or ("ZZZZ", now.date()) in gw.hist_given_up, 60)  # fmt: skip
        step("unknown code excluded", excluded,
             f"unknown={sorted(gw.unknown_codes)} given_up={sorted(gw.hist_given_up)}")

        # 5. the socket dropped
        before = gw.reconnects
        dropped = gw.simulate_socket_drop()
        back = wait(lambda: gw.reconnects > before and gw.ready, 60)
        resub = wait(lambda: all(gw.streaming(c) for c in CODES), 30)
        subs2 = subscriptions(gw)
        new_ids = all(subs2.get(k) != v for k, v in subs1.items()) and len(subs2) >= 3
        catch = wait(lambda: gw.bars_today("BHP", now.date()) is not None
                     or now.weekday() >= 5, 60)
        step("socket drop -> reconnect", dropped and back and resub and new_ids and catch,
             f"reconnects {gw.reconnects}, ready {gw.ready}, subscriptions re-requested "
             f"{subs2} (before {subs1}), catch-up bars for BHP "
             f"{'held' if gw.bars_today('BHP', now.date()) is not None else 'none (weekend?)'}")

        # 6. a quote
        q = gw.quote("BHP")
        step("frozen quote", q is not None and q.get("market_data_type") in (1, 2),
             f"{q}" if q else "none")
    finally:
        rec["status"] = gw.status()
        gw.stop(10)
    out = repo_root() / "reports" / f"ibkr_chaos_{now:%Y%m%d_%H%M}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
    print(f"{'ALL PASSED' if ok_all else 'FAILED'}; record: {out}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
