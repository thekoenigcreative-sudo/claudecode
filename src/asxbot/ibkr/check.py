"""`asxbot ibkr check`: is IB Gateway ready to feed the arena live ASX prices?

One command, read-only, safe to run while the watcher is running (it connects with the next
client id, so it never takes the watcher's). It checks, in order, and stops at the first
failure with what to do about it:

  1. Gateway accepts the API connection on the configured port;
  2. Gateway is connected to IBKR (not "connectivity broken", code 2110 / 1100);
  3. a quote for BHP: bid/ask/last and sizes, open, previous close, halt flag, auction
     fields, and its market data type. It asks for real-time (1) from the pre-open to the
     closing auction and frozen (2) otherwise, and reports what it asked for and what came
     back: frozen is the last real-time value, so out of hours it proves the real-time
     subscription (without one IBKR sends nothing, and the check fails);
  4. BHP's 1-minute bars for the latest session, compared bar by bar with Yahoo's cached
     bars for the same day where the cache has them (prices and volumes);
  5. the ASX 200 index (XJO): a quote and its 1-minute bars - every scan measures moves
     against it, so without it IBKR cannot be the feed.

Writes data/arena/ibkr_check.json (no account details) and exits 0 only if all pass.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from asxbot.ibkr.gateway import MARKET_DATA_TYPES, Gateway, settings_from_config
from asxbot.io import write_text_atomic

SYD = ZoneInfo("Australia/Sydney")


def _hours(now: datetime) -> bool:
    from asxbot.ibkr.feed import market_hours

    return market_hours(now)


def compare_with_yahoo(ib_df, y_df) -> dict:
    """IBKR's bars against Yahoo's for the same minutes: how many line up, how often the
    close agrees to the cent, and the median volume ratio (IBKR / Yahoo)."""
    if ib_df is None or y_df is None or not len(ib_df) or not len(y_df):
        return {"compared": 0}
    both = ib_df.join(y_df, how="inner", lsuffix="_ib", rsuffix="_y")
    both = both[(both["volume_ib"] > 0) & (both["volume_y"] > 0)]
    if not len(both):
        return {"compared": 0}
    same_close = (both["close_ib"] - both["close_y"]).abs() < 0.005
    ratio = (both["volume_ib"] / both["volume_y"]).median()
    return {
        "compared": int(len(both)),
        "close_equal_pct": round(100.0 * float(same_close.mean()), 1),
        "median_volume_ratio_ib_over_yahoo": round(float(ratio), 3),
        "ib_minutes": int(len(ib_df)),
        "yahoo_minutes": int(len(y_df)),
    }


def run_check(cfg, out=print, gateway: Gateway | None = None, now: datetime | None = None) -> int:
    now = (now or datetime.now(SYD)).astimezone(SYD)
    s = settings_from_config(cfg)
    s = dataclasses.replace(s, client_id=int(s.client_id) + 1)
    gw = gateway or Gateway(s)
    gw.wall = lambda: now  # the market data type asked for follows the check's clock
    res: dict = {"at": now.isoformat(timespec="seconds"), "port": s.port, "steps": {}}
    in_hours = _hours(now)

    def done(ok: bool, line: str) -> int:
        res["ok"] = ok
        res["result"] = line
        out(("PASS: " if ok else "FAIL: ") + line)
        try:
            write_text_atomic(
                json.dumps(res, indent=2, default=str), cfg.data_dir / "arena" / "ibkr_check.json"
            )
        except OSError:
            pass
        gw.disconnect()
        return 0 if ok else 1

    state = "market hours" if in_hours else "market closed"
    out(f"IB Gateway check, {now:%a %d %b %H:%M} Sydney ({state})")
    # 1-2. connection and IBKR link
    connected = gw.connect()
    res["steps"]["connect"] = gw.health.to_dict()
    if gw.ib is None:
        return done(
            False,
            f"IB Gateway is not accepting connections on 127.0.0.1:{s.port}. Is it running "
            "and logged in? (It needs a fresh login about once a week.)",
        )
    out(f"  connected to Gateway on port {s.port} (client {s.client_id}), read-only")
    if not connected:
        gw.ib.sleep(3)  # a restored link reports itself within a few seconds
    if not gw.ready:
        return done(
            False,
            "Gateway is running but not connected to IBKR ("
            + (gw.health.last_error or "link down")
            + "). Log in to Gateway again, or wait for it to reconnect, then re-run.",
        )
    out("  Gateway is connected to IBKR")

    # 3. BHP quote
    q = gw.quote("BHP")
    res["steps"]["bhp_quote"] = q
    asked = gw.health.requested_data_type
    asked_kind = MARKET_DATA_TYPES.get(asked or 0, "unknown")
    res["requested_market_data"] = asked_kind
    if q is None:
        err = gw.health.last_request_error
        return done(
            False,
            f"no quote for BHP came back (asked for {asked_kind} data"
            + (f"; Gateway said {err}" if err else "")
            + ")",
        )
    mdt = q.get("market_data_type")
    kind = MARKET_DATA_TYPES.get(mdt or 0, "unknown")
    res["received_market_data"] = kind
    out(f"  market data: asked for {asked_kind} ({asked}), got {kind} ({mdt})")
    out(
        f"  BHP quote: {kind} | bid {q['bid']} x {q['bid_size']}  ask {q['ask']} x "
        f"{q['ask_size']}  last {q['last']} x {q['last_size']} | open {q['open']}  prev close "
        f"{q['prev_close']}  volume {q['volume']} | halted {q['halted']} | auction "
        f"{q['auction_price']} / {q['auction_volume']}"
    )
    if in_hours and mdt != 1:
        return done(False, f"BHP's quote is {kind}, not real-time, in market hours")
    if not in_hours and mdt not in (1, 2):
        res["real_time_proven"] = False
        out(f"  note: out of hours the quote is {kind}: real-time is NOT proven until the open")
    else:
        res["real_time_proven"] = True

    # 4. BHP bars
    got = gw.bars({"BHP": ("1 D", None)})
    b = got.get("BHP")
    if b is None or not len(b):
        return done(False, "no 1-minute bars for BHP came back")
    day = b.index.max().date()
    res["steps"]["bhp_bars"] = {
        "day": day.isoformat(), "bars": int(len(b)),
        "first": str(b.index.min()), "last": str(b.index.max()),
        "volume": float(b["volume"].sum()),
    }  # fmt: skip
    out(f"  BHP 1-minute bars: {len(b)} for {day} ({b.index.min():%H:%M}-{b.index.max():%H:%M})")
    from asxbot.arena.minutes import MinuteBars

    y = MinuteBars(cfg.data_dir).cached("BHP", day)
    cmp = compare_with_yahoo(b, y)
    res["steps"]["bhp_vs_yahoo"] = cmp
    if cmp.get("compared"):
        out(
            f"  vs Yahoo's cached bars that day: {cmp['compared']} minutes compared, close "
            f"equal on {cmp['close_equal_pct']}%, median volume ratio IBKR/Yahoo "
            f"{cmp['median_volume_ratio_ib_over_yahoo']}"
        )
    else:
        out("  (no Yahoo bars cached for that day to compare with)")

    # 5. the index
    iq = gw.quote("^AXJO")
    ib_idx = gw.bars({"^AXJO": ("1 D", None)}).get("^AXJO")
    res["steps"]["xjo"] = {
        "quote": iq,
        "bars": 0 if ib_idx is None else int(len(ib_idx)),
    }
    if ib_idx is None or not len(ib_idx):
        return done(
            False,
            "no 1-minute bars for the ASX 200 index (XJO): check the ASX index data "
            "subscription. Without it the scans cannot use IBKR.",
        )
    out(
        f"  ASX 200 (XJO): {len(ib_idx)} bars, quote "
        f"{'none' if iq is None else (iq.get('last') or iq.get('prev_close'))} "
        f"({'none' if iq is None else MARKET_DATA_TYPES.get(iq.get('market_data_type') or 0)})"
    )
    line = "IB Gateway is ready: live ASX quotes and 1-minute bars for stocks and the index"
    if not res.get("real_time_proven"):
        line += " (real-time itself is confirmed only once the market opens)"
    return done(True, line)
