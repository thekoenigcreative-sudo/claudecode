"""The connection doctor against the live IB Gateway (26 Sep 2026): the chaos that can be
staged for real without touching Gateway or Rick's login.

Read-only connections on spare client ids (47, 48; the watcher is 41, the check 42, the
supervisor's probe 43, the old chaos script 45, the history fetch 46; the doctor's own spares
44/47/48). Never kills or restarts Gateway, never sends Telegram, never runs the supervisor's
task and writes no restart request: the doctor here has its state in a scratch folder and its
Gateway-level actions replaced by recorders. What it proves on the real thing:

  1. the network probe: the internet and IBKR's servers (from Gateway's jts.ini) answer;
  2. connect and hold on client 47: the doctor ticks HEALTHY;
  3. the socket dropped from our side (what a dead Gateway or network looks like to
     ib_async): the connection comes back by itself and the doctor's episode, if one opened,
     closes "verified";
  4. a client-id clash: a plain connection holds client 48, a LiveGateway on 48 is refused
     (326 / "already in use"), the doctor diagnoses client_id_in_use, switches to spare 44,
     and verifies it connected;
  5. the doctor's reconnect on request: request_reconnect -> back, verified.

    C:\\venvs\\asx-bot\\Scripts\\python.exe scripts\\ibkr_doctor_live_chaos.py

One line per step (PASS/FAIL), and reports\\ibkr_doctor_live_<stamp>.json. Exit 0 only if
every step passed. Not in market hours (the watcher has Gateway to itself then).
"""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from asxbot.config import load_config, repo_root  # noqa: E402
from asxbot.ibkr import doctor as D  # noqa: E402
from asxbot.ibkr.gateway import market_hours, settings_from_config  # noqa: E402
from asxbot.ibkr.live import LiveGateway  # noqa: E402
from asxbot.log import setup_logging  # noqa: E402

SYD = ZoneInfo("Australia/Sydney")


def wait(cond, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.2)
    return cond()


def main() -> int:
    now = datetime.now(SYD)
    if market_hours(now):
        print("refusing: market hours (the watcher has IB Gateway to itself)")
        return 2
    cfg = load_config()
    setup_logging(cfg.logs_dir)
    base = settings_from_config(cfg)
    scratch = Path(tempfile.mkdtemp(prefix="doctor_chaos_"))
    rec: dict = {"at": now.isoformat(timespec="seconds"), "steps": []}
    ok_all = True
    recorded: list = []

    def step(name: str, ok: bool, detail: str) -> None:
        nonlocal ok_all
        ok_all &= ok
        rec["steps"].append({"step": name, "ok": ok, "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'}: {name} - {detail}", flush=True)

    def doctor_for(gw: LiveGateway, name: str) -> D.Doctor:
        a = D.gateway_actions(gw, send=lambda text: recorded.append(("tell", text)) or True)
        a.run_supervisor = lambda: (recorded.append(("supervisor",)) or True, "recorded only")
        a.request_restart = lambda why: recorded.append(("restart", why)) or True
        return D.Doctor(a, None, path=scratch / f"{name}.json")

    def tick(doc: D.Doctor, gw: LiveGateway) -> D.Diagnosis:
        return doc.tick(D.gather(gw, datetime.now(SYD), "^AXJO"))

    # 1. the network
    internet, ibkr = D.net_probe()
    step("network probe", internet and ibkr,
         f"internet {'answers' if internet else 'DOWN'}, IBKR servers "
         f"{D.ibkr_servers()} {'answer' if ibkr else 'UNREACHABLE'}")  # fmt: skip

    # 2-3, 5. client 47: healthy, a socket drop, a reconnect on request
    gw = LiveGateway(dataclasses.replace(base, client_id=47), history_dir=scratch / "h").start()
    try:
        ready = gw.wait_ready(30)
        doc = doctor_for(gw, "c47")
        d = tick(doc, gw)
        step("connect and hold (client 47)", ready and d.cause == D.HEALTHY,
             f"ready {ready}; doctor: {d.cause}")  # fmt: skip
        n0 = gw.reconnects
        gw.simulate_socket_drop()
        wait(lambda: not gw.health.connected, 5)
        mid = tick(doc, gw)
        back = wait(lambda: gw.ready and gw.reconnects > n0, 60)
        d = tick(doc, gw)
        hist = (json.loads((scratch / "c47.json").read_text()).get("history") or [])
        step("socket dropped -> back by itself, doctor verifies", back and d.cause == D.HEALTHY,
             f"while down the doctor said {mid.cause}; reconnects {n0}->{gw.reconnects}; "
             f"now {d.cause}; episodes closed {len(hist)}")  # fmt: skip
        n1 = gw.reconnects
        gw.request_reconnect("live chaos: the doctor's reconnect")
        back = wait(lambda: gw.reconnects > n1 and gw.ready, 60)
        d = tick(doc, gw)
        step("reconnect on request", back and d.cause == D.HEALTHY,
             f"reconnects {n1}->{gw.reconnects}; doctor {d.cause}")  # fmt: skip
    finally:
        gw.stop(10)

    # 4. a client-id clash on 48
    from ib_async import IB

    holder = IB()
    try:
        holder.connect(base.host, int(base.port), clientId=48, timeout=10, readonly=True)
        held = holder.isConnected()
    except Exception as e:  # noqa: BLE001
        held = False
        rec["holder_error"] = repr(e)
    if not held:
        step("client-id clash", False, "could not hold client 48 with a plain connection")
    else:
        gw2 = LiveGateway(dataclasses.replace(base, client_id=48), history_dir=scratch / "h2")
        gw2.start()
        try:
            clash = wait(lambda: gw2.clash_recent() or gw2.connect_failures >= 2, 40)
            doc2 = doctor_for(gw2, "c48")
            d = tick(doc2, gw2)
            first = d.cause
            ok = wait(lambda: gw2.ready, 60)
            d2 = tick(doc2, gw2)
            ep = json.loads((scratch / "c48.json").read_text())
            hist = ep.get("history") or []
            step("client-id clash -> spare id, verified",
                 clash and first == D.CLIENT_ID_IN_USE and ok and gw2.s.client_id != 48
                 and d2.cause == D.HEALTHY,
                 f"326 seen {gw2.clash_recent()}; connect failures {gw2.connect_failures}; "
                 f"doctor said {first}; now client {gw2.s.client_id}, ready {ok}, doctor "
                 f"{d2.cause}; episode closed: {hist[-1] if hist else None}")  # fmt: skip
        finally:
            gw2.stop(10)
            holder.disconnect()

    rec["recorded_actions"] = recorded
    rec["passed"] = ok_all
    out = repo_root() / "reports" / f"ibkr_doctor_live_{datetime.now(SYD):%Y%m%d_%H%M}.json"
    out.write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
    print(f"{'ALL PASSED' if ok_all else 'FAILED'}; record {out}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
