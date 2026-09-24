"""The day trader (config.yaml `asx_daytrader`, version 1, frozen 2026-09-24 before it ran).

Rick, 24 Sep: "why can't it be doing the work of a day trader". So instead of waiting for
announcements, code scans the liquid market every cycle, finds four setups by exact rule,
and hands each one to the agent to confirm or reject with the context a trader checks. The
rule bot takes every setup mechanically. Code sizes every trade (risk <= 0.5% of the
account), enforces the stop, manages the trade (breakeven at +1R, half off and a trail at
+2R - in the broker, bar by bar) and is flat by the 15:50 sweep.

Everything here is plain code except the one agent call per setup. The setups are pure
functions of the bars a decision may see (intraday.visible), so the live scanner and the
plumbing replay run the same code.

DATA HONESTY: on Yahoo's delayed bars a setup is seen ~20 minutes after its trigger bar and
filled at the first bar after the decision. Every report says "delayed data - rehearsal
until IBKR live prices".
"""

from __future__ import annotations

import json
import math
import time as time_mod
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as time_cls
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from asxbot.arena import notify
from asxbot.arena.agents import DECIDER, AgentCallFailed, call_agent
from asxbot.arena.broker import OPENING_SIDES
from asxbot.arena.intraday import MarketView, continuous, rvol_at, vwap
from asxbot.arena.levels import Playbook
from asxbot.arena.liquid import SizeRule, liquid_universe, size_rule
from asxbot.arena.orders import ArenaOrderRefused, arena_place_order
from asxbot.io import write_text_atomic
from asxbot.live.scanner import round_to_tick
from asxbot.log import EventLog, get_logger

log = get_logger("asxbot.arena.daytrader")
SYD = ZoneInfo("Australia/Sydney")
OPEN = time_cls(10, 0)
SETUPS = ("gap_and_go", "opening_range_breakout", "vwap_reclaim", "halt_resumption")


@dataclass
class Setup:
    ticker: str
    setup: str
    side: str  # buy | short
    trigger_bar: str  # ISO minute of the bar that triggered it
    last: float  # that bar's close
    stop: float  # the setup's invalidation level
    rvol: float | None
    why: str
    context: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.ticker}|{self.setup}|{self.side}"

    def to_dict(self) -> dict:
        return asdict(self)


def _t(s: str) -> time_cls:
    return time_cls.fromisoformat(str(s))


def _in(ts, window) -> bool:
    return _t(window[0]) <= ts.time() <= _t(window[1])


def _close_at(df: pd.DataFrame, ts) -> float | None:
    part = df[df.index <= ts]
    return None if not len(part) else float(part["close"].iloc[-1])


# --------------------------------------------------------------------------
# the four setups, exactly as config.yaml writes them. Each looks at bar i of `bars` (the
# day's continuous bars a decision may see) and returns a Setup if bar i triggers it.
# --------------------------------------------------------------------------
@dataclass
class Ctx:
    """What every setup needs about one stock on one day."""

    ticker: str
    day: date
    bars: pd.DataFrame  # continuous bars, visible
    index: pd.DataFrame  # the index's continuous bars, visible
    prev_close: float
    index_prev_close: float
    usual: pd.Series | None
    shortable: bool
    news: bool  # price-sensitive or reinstatement announcement today
    conf: dict  # config setups block
    _vwap: pd.Series | None = None
    _resumed: list | None = None

    @property
    def vw(self) -> pd.Series:
        if self._vwap is None:
            self._vwap = vwap(self.bars)
        return self._vwap

    def rvol(self, ts) -> float | None:
        return rvol_at(self.bars, self.usual, ts)

    def move_vs_index(self, ts) -> float | None:
        px, ix = _close_at(self.bars, ts), _close_at(self.index, ts)
        if px is None or ix is None:
            return None
        return ((px / self.prev_close - 1) - (ix / self.index_prev_close - 1)) * 100


def gap_and_go(c: Ctx, i: int) -> Setup | None:
    p = c.conf["gap_and_go"]
    ts = c.bars.index[i]
    if not _in(ts, p["window"]):
        return None
    day_open = datetime.combine(c.day, OPEN, tzinfo=SYD)
    rng_end = day_open + timedelta(minutes=int(p["range_minutes"]))
    opening = c.bars[(c.bars.index >= day_open) & (c.bars.index < rng_end)]
    if not len(opening) or not len(c.index):
        return None
    iopen_row = c.index[c.index.index >= day_open]
    if not len(iopen_row):
        return None
    gap = (
        (float(opening["open"].iloc[0]) / c.prev_close - 1)
        - (float(iopen_row["open"].iloc[0]) / c.index_prev_close - 1)
    ) * 100
    need = float(p["gap_pct_vs_index"])
    bar = c.bars.iloc[i]
    so_far = c.bars.iloc[: i + 1]
    rv = c.rvol(ts)
    if rv is None or rv < float(p["min_rvol"]):
        return None
    hi, lo = float(opening["high"].max()), float(opening["low"].min())
    if gap >= need and bar["close"] > hi and float(so_far["low"].min()) >= c.prev_close:
        return Setup(
            c.ticker,
            "gap_and_go",
            "buy",
            ts.isoformat(timespec="minutes"),
            float(bar["close"]),
            lo,
            rv,
            f"gap {gap:+.1f}% vs index, closed {bar['close']:.4g} above the "
            f"{p['range_minutes']}-min high {hi:.4g}, gap unfilled, RVOL {rv:.1f}",
        )
    if (
        gap <= -need
        and c.shortable
        and bar["close"] < lo
        and float(so_far["high"].max()) <= c.prev_close
    ):
        return Setup(
            c.ticker,
            "gap_and_go",
            "short",
            ts.isoformat(timespec="minutes"),
            float(bar["close"]),
            hi,
            rv,
            f"gap {gap:+.1f}% vs index, closed {bar['close']:.4g} below the "
            f"{p['range_minutes']}-min low {lo:.4g}, gap unfilled, RVOL {rv:.1f}",
        )
    return None


def opening_range_breakout(c: Ctx, i: int) -> Setup | None:
    p = c.conf["opening_range_breakout"]
    ts = c.bars.index[i]
    if not _in(ts, p["window"]):
        return None
    day_open = datetime.combine(c.day, OPEN, tzinfo=SYD)
    rng = c.bars[
        (c.bars.index >= day_open)
        & (c.bars.index < day_open + timedelta(minutes=int(p["range_minutes"])))
    ]
    if not len(rng):
        return None
    look = int(p["bar_volume_lookback"])
    before = c.bars.iloc[max(0, i - look) : i]
    if len(before) < max(3, look // 2):
        return None
    bar = c.bars.iloc[i]
    avg = float(before["volume"].mean())
    if avg <= 0 or float(bar["volume"]) < float(p["bar_volume_multiple"]) * avg:
        return None
    rv = c.rvol(ts)
    if rv is None or rv < float(p["min_rvol"]):
        return None
    hi, lo = float(rng["high"].max()), float(rng["low"].min())
    mid = (hi + lo) / 2
    vol_x = float(bar["volume"]) / avg
    if bar["close"] > hi and mid < bar["close"]:
        return Setup(
            c.ticker,
            "opening_range_breakout",
            "buy",
            ts.isoformat(timespec="minutes"),
            float(bar["close"]),
            mid,
            rv,
            f"closed {bar['close']:.4g} above the 30-min high {hi:.4g} on {vol_x:.1f}x "
            f"the prior {look} bars' volume, RVOL {rv:.1f}",
        )
    if bar["close"] < lo and c.shortable and mid > bar["close"]:
        return Setup(
            c.ticker,
            "opening_range_breakout",
            "short",
            ts.isoformat(timespec="minutes"),
            float(bar["close"]),
            mid,
            rv,
            f"closed {bar['close']:.4g} below the 30-min low {lo:.4g} on {vol_x:.1f}x "
            f"the prior {look} bars' volume, RVOL {rv:.1f}",
        )
    return None


def vwap_reclaim(c: Ctx, i: int) -> Setup | None:
    p = c.conf["vwap_reclaim"]
    ts = c.bars.index[i]
    if not _in(ts, p["window"]):
        return None
    slope, lookback = int(p["vwap_slope_bars"]), int(p["pullback_lookback"])
    if i - 1 - slope < 0 or i < lookback:
        return None
    vw, closes = c.vw, c.bars["close"]
    prev_ts = c.bars.index[i - 1]
    move = c.move_vs_index(prev_ts)
    if move is None:
        return None
    window = range(i - lookback, i)
    below = sum(1 for j in window if closes.iloc[j] < vw.iloc[j])
    above = sum(1 for j in window if closes.iloc[j] > vw.iloc[j])
    vol_before = c.bars["volume"].iloc[max(0, i - int(p["bar_volume_lookback"])) : i]
    if not len(vol_before) or float(c.bars["volume"].iloc[i]) < float(vol_before.mean()):
        return None
    n = int(p["stop_lookback"])
    recent = c.bars.iloc[max(0, i - n + 1) : i + 1]
    need_move, need_bars = float(p["min_move_vs_index_pct"]), int(p["min_bars_below"])
    rv = c.rvol(ts)
    bar = c.bars.iloc[i]
    if (
        move >= need_move
        and vw.iloc[i - 1] > vw.iloc[i - 1 - slope]
        and below >= need_bars
        and closes.iloc[i - 1] < vw.iloc[i - 1]
        and closes.iloc[i] > vw.iloc[i]
    ):
        return Setup(
            c.ticker,
            "vwap_reclaim",
            "buy",
            ts.isoformat(timespec="minutes"),
            float(bar["close"]),
            float(recent["low"].min()),
            rv,
            f"up {move:+.1f}% vs index, VWAP rising, {below} of the last {lookback} "
            f"bars below VWAP, closed back above it at {bar['close']:.4g} "
            f"(VWAP {vw.iloc[i]:.4g})",
        )
    if (
        c.shortable
        and move <= -need_move
        and vw.iloc[i - 1] < vw.iloc[i - 1 - slope]
        and above >= need_bars
        and closes.iloc[i - 1] > vw.iloc[i - 1]
        and closes.iloc[i] < vw.iloc[i]
    ):
        return Setup(
            c.ticker,
            "vwap_reclaim",
            "short",
            ts.isoformat(timespec="minutes"),
            float(bar["close"]),
            float(recent["high"].max()),
            rv,
            f"down {move:+.1f}% vs index, VWAP falling, {above} of the last {lookback} "
            f"bars above VWAP, closed back below it at {bar['close']:.4g} "
            f"(VWAP {vw.iloc[i]:.4g})",
        )
    return None


def resumptions(c: Ctx) -> list[tuple]:
    """Halts found in the day's bars: (resumed_at, price before, time before). Worked out
    once per stock per scan."""
    if c._resumed is not None:
        return c._resumed
    p = c.conf["halt_resumption"]
    idx = c.bars.index
    out = []
    gap = timedelta(minutes=int(p["min_gap_minutes"]) + 1)  # 10 untraded minutes between
    lo_t, hi_t = time_cls(10, 5), time_cls(15, 30)
    if len(idx) > 1:
        steps = idx[1:] - idx[:-1]
        for k in (steps >= gap).nonzero()[0]:
            a, b = idx[k], idx[k + 1]
            if not (lo_t <= a.time() < hi_t):
                continue
            n_prior = idx.searchsorted(a, side="right") - idx.searchsorted(
                a - timedelta(minutes=30), side="right"
            )
            if n_prior < float(p["prior_activity_share"]) * 30:
                continue
            out.append((b, float(c.bars["close"].iloc[k]), a))
    if len(idx) and idx[0].time() >= _t(p["late_first_trade"]) and c.news:
        out.append((idx[0], c.prev_close, None))
    c._resumed = out
    return out


def halt_resumption(c: Ctx, i: int) -> Setup | None:
    p = c.conf["halt_resumption"]
    ts = c.bars.index[i]
    rng_m, within = int(p["resumption_range_minutes"]), int(p["within_minutes"])
    for resumed, before_px, before_ts in resumptions(c):
        if not (resumed + timedelta(minutes=rng_m) <= ts <= resumed + timedelta(minutes=within)):
            continue
        rng = c.bars[
            (c.bars.index >= resumed) & (c.bars.index < resumed + timedelta(minutes=rng_m))
        ]
        if not len(rng):
            continue
        end_px = float(rng["close"].iloc[-1])
        ix_end = _close_at(c.index, rng.index[-1])
        ix_before = c.index_prev_close if before_ts is None else _close_at(c.index, before_ts)
        if ix_end is None or not ix_before:
            continue
        move = ((end_px / before_px - 1) - (ix_end / ix_before - 1)) * 100
        need = float(p["min_move_vs_index_pct"])
        bar = c.bars.iloc[i]
        hi, lo = float(rng["high"].max()), float(rng["low"].min())
        # only the first bar past the range after it formed
        after = c.bars[(c.bars.index >= resumed + timedelta(minutes=rng_m)) & (c.bars.index < ts)]
        if move >= need and bar["close"] > hi and not (after["close"] > hi).any():
            return Setup(
                c.ticker,
                "halt_resumption",
                "buy",
                ts.isoformat(timespec="minutes"),
                float(bar["close"]),
                lo,
                c.rvol(ts),
                f"resumed {resumed:%H:%M} {move:+.1f}% vs index; closed {bar['close']:.4g} "
                f"above the resumption range high {hi:.4g}",
            )
        if move <= -need and c.shortable and bar["close"] < lo and not (after["close"] < lo).any():
            return Setup(
                c.ticker,
                "halt_resumption",
                "short",
                ts.isoformat(timespec="minutes"),
                float(bar["close"]),
                hi,
                c.rvol(ts),
                f"resumed {resumed:%H:%M} {move:+.1f}% vs index; closed {bar['close']:.4g} "
                f"below the resumption range low {lo:.4g}",
            )
    return None


DETECTORS = {
    "gap_and_go": gap_and_go,
    "opening_range_breakout": opening_range_breakout,
    "vwap_reclaim": vwap_reclaim,
    "halt_resumption": halt_resumption,
}


def detect(c: Ctx, since: datetime | None, fired: set[str], scan_end: time_cls) -> list[Setup]:
    """Setups triggered by bars after `since`, first trigger of each (stock, setup, side)
    only. Bars after the scan's end time are not triggers."""
    out = []
    for i, ts in enumerate(c.bars.index):
        if since is not None and ts <= since:
            continue
        if ts.time() > scan_end:
            break
        for fn in DETECTORS.values():
            s = fn(c, i)
            if s is not None and s.key not in fired:
                fired.add(s.key)
                out.append(s)
    return out


# --------------------------------------------------------------------------
# the day's state
# --------------------------------------------------------------------------
def _state_path(data_dir: Path, day: date) -> Path:
    return Path(data_dir) / "arena" / "daytrader" / f"{day.isoformat()}.json"


def load_state(data_dir: Path, day: date) -> dict:
    p = _state_path(data_dir, day)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"fired": [], "last_eval": {}, "signals": [], "universe": []}


def save_state(data_dir: Path, day: date, state: dict) -> None:
    write_text_atomic(json.dumps(state, indent=1, default=str), _state_path(data_dir, day))


def build_universe(arena, pb: Playbook) -> tuple[list[str], dict]:
    """The liquid universe: ASX 300 names passing the size-aware rule and the tick limit,
    from cached daily bars (no network)."""
    from asxbot.data.factory import get_store
    from asxbot.data.universe import build_universes

    conf = pb.raw.get("universe") or {}
    a, _ = build_universes(arena.cfg.data_dir, arena.cfg.get("collector.user_agent"))
    store = get_store(arena.cfg)

    def daily(code):
        try:
            return store.load(code)
        except Exception:  # noqa: BLE001
            return None

    rule = size_rule(pb) or SizeRule(0.05, 20)
    order = pb.level.max_position_aud or 5000.0
    return liquid_universe(list(a.codes), daily, rule, order, float(conf.get("max_tick_pct", 1.0)))


def news_today(data_dir: Path, day: date) -> set[str]:
    """Codes with a price-sensitive announcement (or a reinstatement) since the previous
    session's close."""
    from asxbot.arena.intraday import prior_sessions

    frames = []
    prev = prior_sessions(day, 1)
    for d in ([prev[0]] if prev else []) + [day]:
        p = Path(data_dir) / "announcements" / "live" / f"{d.isoformat()}.parquet"
        if p.exists():
            frames.append(pd.read_parquet(p))
    if not frames:
        return set()
    df = pd.concat(frames)
    start = datetime.combine(prev[0], time_cls(16, 10)) if prev else datetime.combine(day, OPEN)
    df = df[df["released_at"] >= start]
    keep = df["price_sensitive"].astype(bool) | df["headline"].str.contains(
        "reinstat", case=False, na=False
    )
    return {str(c).upper() for c in df.loc[keep, "code"]}


# --------------------------------------------------------------------------
# one scan
# --------------------------------------------------------------------------
def scan(
    view: MarketView,
    pb: Playbook,
    codes: list[str],
    now: datetime,
    state: dict,
    shortable: set[str],
    news: set[str],
) -> tuple[list[Setup], dict]:
    """Evaluate every code's new bars. Returns (fresh setups, the scan's top lists)."""
    conf = pb.raw.get("setups") or {}
    scan_conf = pb.raw.get("scan") or {}
    end = _t(scan_conf.get("end", "15:45"))
    max_age = int(pb.raw.get("max_signal_age_bars", 5))
    index = continuous(view.bars(view.index, now, fetch=False))
    iprev = view.prev_close(view.index)
    fired = set(state.get("fired", []))
    rows, found = [], []
    if iprev is None or not len(index):
        return [], {}
    for code in codes:
        bars = continuous(view.bars(code, now, fetch=False))
        if not len(bars):
            continue
        prev = view.prev_close(code)
        if prev is None:
            continue
        c = Ctx(
            code,
            view.day,
            bars,
            index,
            prev,
            iprev,
            view.usual(code),
            code in shortable,
            code in news,
            conf,
        )
        last_eval = state["last_eval"].get(code)
        since = datetime.fromisoformat(last_eval) if last_eval else None
        new = detect(c, since, fired, end)
        state["last_eval"][code] = bars.index.max().isoformat(timespec="minutes")
        for s in new:
            pos = bars.index.get_loc(pd.Timestamp(s.trigger_bar))
            age = len(bars) - 1 - int(pos)
            s.context = {"age_bars": age}
            if age > max_age:
                s.context["stale"] = True
            found.append(s)
        ts = bars.index.max()
        mv = c.move_vs_index(ts)
        rows.append(
            {
                "ticker": code,
                "bar": ts.strftime("%H:%M"),
                "move_vs_index": mv,
                "rvol": c.rvol(ts),
                "new_high": bool(
                    len(bars) > 1 and bars["high"].iloc[-1] >= bars["high"].iloc[:-1].max()
                ),
                "new_low": bool(
                    len(bars) > 1 and bars["low"].iloc[-1] <= bars["low"].iloc[:-1].min()
                ),
                "resumed": bool(resumptions(c)),
                "news": code in news,
            }
        )
    state["fired"] = sorted(fired)
    top = int(scan_conf.get("top_n", 10))
    df = pd.DataFrame(rows)
    summary: dict = {"stocks": len(rows)}
    if len(df):
        df = df.dropna(subset=["move_vs_index"])
        summary["up"] = _top(df.sort_values("move_vs_index", ascending=False), top)
        summary["down"] = _top(df.sort_values("move_vs_index"), top)
        summary["rvol"] = _top(df.dropna(subset=["rvol"]).sort_values("rvol", ascending=False), top)
        summary["new_highs"] = int(df["new_high"].sum())
        summary["new_lows"] = int(df["new_low"].sum())
        summary["resumed"] = sorted(df.loc[df["resumed"], "ticker"].tolist())
        summary["news"] = sorted(df.loc[df["news"], "ticker"].tolist())
    return found, summary


def _top(df: pd.DataFrame, n: int) -> list:
    out = []
    for r in df.head(n).itertuples():
        rv = "" if r.rvol is None or r.rvol != r.rvol else f" {r.rvol:.1f}x"
        out.append(f"{r.ticker} {r.move_vs_index:+.1f}%{rv}")
    return out


# --------------------------------------------------------------------------
# sizing, eligibility, the orders
# --------------------------------------------------------------------------
def entries_today(acct, code: str, day: date) -> list:
    return [
        o
        for o in acct.orders.values()
        if o.ticker == code
        and o.side in OPENING_SIDES
        and o.placed_by != "code"
        and o.decided_at[:10] == day.isoformat()
        and (o.filled_qty > 0 or o.working)
    ]


def eligible(acct, pb: Playbook, code: str, day: date) -> tuple[bool, str]:
    """One re-entry per stock per day, never while a position or entry is working."""
    if code in acct.positions:
        return False, f"already holding {code}"
    if any(o.working and o.ticker == code for o in acct.orders.values()):
        return False, f"an order in {code} is still working"
    n = len(entries_today(acct, code, day))
    cap = int(pb.raw.get("max_entries_per_stock_per_day", 2))
    if n >= cap:
        return False, f"{n} entries in {code} today already (one re-entry a day at most)"
    lvl = pb.level
    becoming = {
        o.ticker
        for o in acct.orders.values()
        if o.working and o.side in OPENING_SIDES and o.ticker not in acct.positions
    }
    if len(acct.positions) + len(becoming) >= lvl.max_open_positions:
        return False, f"the account is full ({lvl.max_open_positions} open or working)"
    if lvl.max_new_positions_per_day is not None:
        opened = sum(
            1
            for o in acct.orders.values()
            if o.side in OPENING_SIDES
            and o.placed_by != "code"
            and o.decided_at[:10] == day.isoformat()
            and (o.filled_qty > 0 or o.working)
        )
        if opened >= lvl.max_new_positions_per_day:
            return False, f"{opened} new positions today already (the limit)"
    return True, ""


def terms(arena, pb: Playbook, acct, s: Setup, stop: float | None = None) -> dict | None:
    """(qty, limit, stop) by the rules: a limit through the last price, risk <= the
    playbook's share of equity, <= the level's max position, <= 5% of turnover."""
    entry = pb.raw.get("entry") or {}
    slack = float(entry.get("limit_slack_pct", 1.0)) / 100.0
    buy = s.side == "buy"
    limit = round_to_tick(s.last * (1 + slack if buy else 1 - slack), up=buy)
    stop = round_to_tick(float(stop if stop is not None else s.stop), up=not buy)
    per_share = abs(limit - stop)
    if per_share <= 0 or (buy and stop >= limit) or (not buy and stop <= limit):
        return None
    equity = acct.equity(arena.broker.prices(acct))
    budget = equity * pb.risk_per_trade_pct / 100.0
    caps = [budget / per_share, (pb.level.max_position_aud or 5000.0) / limit]
    rule = size_rule(pb)
    lookup = getattr(arena.broker, "median_turnover", None)
    if rule is not None and lookup is not None:
        t = lookup(s.ticker)
        if t:
            caps.append(rule.max_order(t) / limit)
    qty = int(math.floor(min(caps)))
    min_order = float((arena.cfg.get("arena.guards") or {}).get("min_order_aud", 500))
    if qty <= 0 or qty * limit < min_order:
        return None
    return {
        "qty": qty,
        "limit": limit,
        "stop": stop,
        "risk": round(per_share * qty, 2),
        "value": round(qty * limit, 2),
    }


def place(
    arena, pb: Playbook, acct, s: Setup, t: dict, kind: str, model: str, why: str, seen_at: datetime
) -> dict:
    entry = pb.raw.get("entry") or {}
    good = arena.broker.clock() + timedelta(minutes=int(entry.get("good_for_minutes", 10)))
    alert = notify.get(arena)
    try:
        o = arena_place_order(
            arena.cfg,
            arena.broker,
            acct,
            pb,
            ticker=s.ticker,
            side=s.side,
            qty=t["qty"],
            limit=t["limit"],
            stop=t["stop"],
            reason=f"[{s.setup}] {why}",
            model=model,
            placed_by=kind,
            hold="intraday",
            universe=arena.universe,
            short_universe=arena.short_universe,
            now=seen_at,
            good_till=good,
            manage=dict(pb.raw.get("manage") or {}),
        )
        if alert:
            alert.decided(o)
        return {"order_id": o.order_id, **t}
    except ArenaOrderRefused as e:
        if alert:
            alert.refused(kind, s.ticker, s.side, t["qty"], str(e))
        return {"refused": str(e)}


# --------------------------------------------------------------------------
# the agent: confirm or reject, one call per setup
# --------------------------------------------------------------------------
def agent_packet(arena, pb: Playbook, s: Setup, t: dict, context: dict, now: datetime) -> str:
    from asxbot.arena.watch import UNTRUSTED, _membership

    return f"""You are trader-decider, working as a DAY TRADER (playbook "{pb.title}", level
{pb.level.number}, fake money). Code's scanner found the setup below by an exact rule. Confirm
or reject it the way a day trader would, from the context. ANSWER WITHIN 60 SECONDS: no
answer in time is a rejection. Be brief.

{UNTRUSTED}

DATA: delayed data - rehearsal until IBKR live prices. The bars below are ~20 minutes behind
the market; your order fills at the first bar after it is recorded.
YOU ARE DECIDING AT: {now:%Y-%m-%d %H:%M} Sydney

THE SETUP ({s.setup}, {s.side.upper()}) - {s.ticker}
  trigger bar {s.trigger_bar[11:16]}: {s.why}
  the setup's stop (its invalidation): {t["stop"]}   entry limit: {t["limit"]}
  code's size: {t["qty"]:,} shares (${t["value"]:,.0f}), risk ${t["risk"]:,.0f}
  (risk per trade is capped at {pb.risk_per_trade_pct}% of the account)
  code manages the trade: stop to breakeven at +1R, half off at +2R, then a 1R trail;
  everything is closed at 15:50
  {_membership(arena, s.ticker)}

CONTEXT (plain code)
{json.dumps(context, indent=2, default=str)}

Check what a trader checks: the news behind the move (if any), the market's direction, the
stock's industry, how extended it already is (from VWAP, from the open), and whether the stop
is in a sensible place. You may TIGHTEN the stop (move it closer to the entry), never widen
it. Positive expected value after costs is the bar; do not reject to look careful.

End with one JSON block and nothing after it:
{{"action": "take" | "reject", "stop": <optional tighter stop, or null>,
  "why": "<one or two sentences>"}}
"""


def setup_context(
    view: MarketView, s: Setup, now: datetime, industry_of: dict, scan_rows: dict, news_rows: dict
) -> dict:
    bars = continuous(view.bars(s.ticker, now, fetch=False))
    index = continuous(view.bars(view.index, now, fetch=False))
    prev, iprev = view.prev_close(s.ticker), view.prev_close(view.index)
    last = float(bars["close"].iloc[-1]) if len(bars) else s.last
    vw = vwap(bars) if len(bars) else None
    ind = industry_of.get(s.ticker, "")
    peers = [v for k, v in scan_rows.items() if industry_of.get(k) == ind and k != s.ticker]
    out = {
        "last_visible_bar": bars.index.max().strftime("%H:%M") if len(bars) else None,
        "last": last,
        "move_since_prev_close_pct": None if not prev else round((last / prev - 1) * 100, 2),
        "move_since_open_pct": None
        if not len(bars)
        else round((last / float(bars["open"].iloc[0]) - 1) * 100, 2),
        "distance_from_vwap_pct": None
        if vw is None
        else round((last / float(vw.iloc[-1]) - 1) * 100, 2),
        "day_high": None if not len(bars) else float(bars["high"].max()),
        "day_low": None if not len(bars) else float(bars["low"].min()),
        "rvol": None if s.rvol is None else round(s.rvol, 2),
        "market_asx200_since_prev_close_pct": None
        if not (len(index) and iprev)
        else round((float(index["close"].iloc[-1]) / iprev - 1) * 100, 2),
        "market_asx200_since_open_pct": None
        if not len(index)
        else round((float(index["close"].iloc[-1]) / float(index["open"].iloc[0]) - 1) * 100, 2),
        "industry": ind,
        "industry_peers_median_move_vs_index_pct": None
        if not peers
        else round(float(pd.Series(peers).median()), 2),
        "industry_peers_n": len(peers),
        "news_today": news_rows.get(s.ticker, []),
        "last_15_bars": [
            f"{ts:%H:%M} o{r.open:.4g} h{r.high:.4g} l{r.low:.4g} c{r.close:.4g} v{int(r.volume)}"
            for ts, r in bars.tail(15).iterrows()
        ],
    }
    return out


def ask_agent(arena, pb: Playbook, s: Setup, t: dict, context: dict, now: datetime) -> dict:
    """One decider call, answer within the configured seconds, or it is a rejection."""
    from asxbot.arena.agents import parse_decision
    from asxbot.arena.watch import DECIDER_MODEL

    limit_s = int((pb.raw.get("agent") or {}).get("timeout_s", 60))
    started = time_mod.monotonic()
    try:
        reply = call_agent(
            DECIDER,
            agent_packet(arena, pb, s, t, context, now),
            expect_model=DECIDER_MODEL,
            timeout_s=limit_s,
            data_dir=arena.cfg.data_dir,
            purpose=f"day trader {s.setup} {s.ticker}",
            process_timeout_s=limit_s + 15,
        )
    except AgentCallFailed as e:
        return {"action": "reject", "why": f"no answer within {limit_s}s ({e})", "model": ""}
    took = time_mod.monotonic() - started
    d = parse_decision(reply.text) if "action" in reply.text else {"action": "reject"}
    if took > limit_s + 15:
        return {
            "action": "reject",
            "why": f"answered after {took:.0f}s, past {limit_s}s",
            "model": reply.model,
        }
    action = str(d.get("action", "reject")).lower()
    return {
        "action": "take" if action in ("take", "trade", "confirm") else "reject",
        "stop": d.get("stop"),
        "why": str(d.get("why", "")),
        "model": reply.model,
        "seconds": round(took, 1),
    }


# --------------------------------------------------------------------------
# the cycle
# --------------------------------------------------------------------------
_DAY: dict = {}
# After the close, today's whole session is written to the minute cache once: tomorrow's
# previous close and usual volume come from it (the scan keeps today's bars in memory).
EOD_AT = time_cls(16, 40)


def end_of_day(arena, pb: Playbook, view: MarketView, now: datetime) -> dict:
    """Write today's complete sessions for the scan universe, today's news stocks and the
    index to the minute cache. Once a day; recorded in the day's state."""
    from asxbot.arena.intraday import backfill
    from asxbot.arena.reaction_v2 import load_queue

    cfg = arena.cfg
    day = view.day
    state = load_state(cfg.data_dir, day)
    if state.get("eod"):
        return state["eod"]
    codes = set(state.get("universe") or _DAY.get("codes") or [])
    codes |= {c for c in load_queue(cfg.data_dir, day) if not c.startswith("_")}
    codes |= news_today(cfg.data_dir, day)
    index = str(cfg.get("backtest.index_ticker", "^AXJO"))
    try:
        res = backfill(view.minutes, [index, *sorted(codes)], [day], batch=60, pause_s=2.0)
    except Exception as e:  # noqa: BLE001
        log.error("end-of-day minute backfill failed: %s", e)
        return {}
    state["eod"] = {"at": now.isoformat(timespec="seconds"), **res}
    save_state(cfg.data_dir, day, state)
    log.info("end of day: %d sessions written to the minute cache", res.get("written", 0))
    return state["eod"]


def cycle(
    arena,
    pb: Playbook,
    view: MarketView,
    now: datetime | None = None,
    use_agent: bool = True,
    refresh: bool = True,
) -> list[dict]:
    """One scan: refresh the stalest stocks, find setups, bot and agent act on each."""
    now = (now or arena.broker.clock()).astimezone(SYD)
    cfg = arena.cfg
    day = view.day
    scan_conf = pb.raw.get("scan") or {}
    if now.weekday() >= 5:
        return []
    if not view.replay and now.time() >= EOD_AT:
        end_of_day(arena, pb, view, now)
        return []
    if now.time() < _t(scan_conf.get("start", "10:00")):
        _prepare_history(arena, pb, view)
        return []
    ev = EventLog(cfg.data_dir)
    state = load_state(cfg.data_dir, day)
    if _DAY.get("day") != day or _DAY.get("data_dir") != str(cfg.data_dir):
        codes, _why = (
            (state["universe"], None) if state.get("universe") else build_universe(arena, pb)
        )
        state["universe"] = codes
        _DAY.clear()
        _DAY.update(
            day=day,
            data_dir=str(cfg.data_dir),
            codes=codes,
            news=news_today(cfg.data_dir, day),
        )
        log.info("day trader: %d stocks in today's liquid universe", len(codes))
        save_state(cfg.data_dir, day, state)
    codes = _DAY["codes"]
    data_time = view.data_time(now)
    if data_time is None or data_time.time() < OPEN:
        return []
    if data_time.time() > _t(scan_conf.get("end", "15:45")):
        return []  # the scan's window is over in market time: no more requests today
    if refresh and not view.replay:
        _DAY["news"] = news_today(cfg.data_dir, day) if now.minute % 5 == 0 else _DAY["news"]
        view.prepare([view.index, *codes])  # IBKR: prior sessions still missing, if any
        refreshed = view.feed.refresh(codes, now)
        view.mark_fetched(refreshed, now)
    found, summary = scan(view, pb, codes, now, state, set(arena.short_universe), _DAY["news"])
    if summary:
        ev.append(
            "daytrader_scan",
            {
                "at": now.isoformat(timespec="seconds"),
                "feed_at": data_time.isoformat(timespec="minutes"),
                "found": [s.key for s in found],
                **summary,
            },
        )
    rows = {}
    for code in codes:
        b = continuous(view.bars(code, now, fetch=False))
        p = view.prev_close(code)
        if len(b) and p:
            rows[code] = (float(b["close"].iloc[-1]) / p - 1) * 100
    out = []
    order = sorted(found, key=lambda s: -(s.rvol or 0.0))
    last_entry = pb.last_entry_time
    max_age = int(pb.raw.get("max_signal_age_bars", 5))
    scanned_at = arena.broker.clock()
    recs = []
    # The rule bot first, on every setup: it needs no call, so it acts at the scan's moment.
    for s in order:
        rec = {"at": now.isoformat(timespec="seconds"), **s.to_dict(), "data": view.label,
               "bot": None, "agent": None}  # fmt: skip
        if s.context.get("stale"):
            rec["skipped"] = f"stale: the trigger bar is {s.context['age_bars']} bars old"
        elif last_entry is not None and scanned_at.astimezone(SYD).time() > last_entry:
            rec["skipped"] = f"after the last entry time {last_entry:%H:%M}"
        else:
            rec["bot"] = _bot_take(arena, pb, s, now, day)
        recs.append((s, rec))
    # Then the agent, one call per setup, while the setup is still fresh: its trigger bar's
    # age plus the minutes spent on the calls before it must stay within max_signal_age_bars.
    for s, rec in recs:
        if use_agent and not rec.get("skipped"):
            waited = (arena.broker.clock() - scanned_at).total_seconds() / 60.0
            if int(s.context.get("age_bars", 0)) + waited > max_age:
                rec["agent"] = {
                    "skipped": f"stale by its turn: {s.context.get('age_bars', 0)} bars old at "
                    f"the scan, {waited:.1f} minutes of agent calls before it"
                }
            else:
                rec["agent"] = _agent_take(arena, pb, view, s, now, day, rows)
        state["signals"].append(rec)
        ev.append("daytrader_setups", rec)
        out.append(rec)
    save_state(cfg.data_dir, day, state)
    return out


def _prepare_history(arena, pb: Playbook, view: MarketView) -> None:
    """Before the scan starts, and only for a feed that keeps its own prior sessions (IBKR):
    fetch them for the liquid universe, as far as the pacing allows each cycle, so the first
    scans have "usual volume" from the same source as today's bars. Yahoo's feed reads the
    minute cache instead, so nothing is done for it."""
    if view.replay or view.feed.history_source() is view.minutes:
        return
    if _PRE.get("day") != view.day:
        try:
            codes, _why = build_universe(arena, pb)
        except Exception as e:  # noqa: BLE001
            log.warning("could not list the universe for the pre-open history: %s", e)
            return
        _PRE.clear()
        _PRE.update(day=view.day, codes=[view.index, *codes])
    view.prepare(_PRE["codes"])


_PRE: dict = {}


def _bot_take(arena, pb, s: Setup, now: datetime, day: date) -> dict:
    acct = arena.account(pb, "bot")
    ok, why = eligible(acct, pb, s.ticker, day)
    if not ok:
        return {"skipped": why}
    t = terms(arena, pb, acct, s)
    if t is None:
        return {"skipped": "cannot be sized (stop on the wrong side, or below the minimum order)"}
    return place(arena, pb, acct, s, t, "bot", "none (rule-based bot)", f"rule: {s.why}", now)


def _agent_take(arena, pb, view, s: Setup, now: datetime, day: date, rows: dict) -> dict:
    acct = arena.account(pb, "agent")
    ok, why = eligible(acct, pb, s.ticker, day)
    if not ok:
        return {"skipped": why}
    t = terms(arena, pb, acct, s)
    if t is None:
        return {"skipped": "cannot be sized"}
    if "industry" not in _DAY:
        _DAY["industry"] = _industries(arena)
    ctx = setup_context(view, s, now, _DAY["industry"], rows, _news_rows(arena, day))
    seen_at = arena.broker.clock()
    answer = ask_agent(arena, pb, s, t, ctx, now)
    EventLog(arena.cfg.data_dir).append(
        "arena_decisions",
        {
            "stage": "daytrader",
            "ticker": s.ticker,
            "setup": s.setup,
            "side": s.side,
            "decision": answer,
            "model": answer.get("model", ""),
        },
    )
    if answer["action"] != "take":
        # Not queued for the hourly digest: the scan can find dozens of setups a day and
        # the digest is for passes worth reading (#37). They are counted in the evening
        # report and each is in the event log (arena_decisions, stage daytrader).
        return {"rejected": answer.get("why", ""), "seconds": answer.get("seconds")}
    stop = s.stop
    try:
        new = float(answer.get("stop")) if answer.get("stop") not in (None, "") else None
    except (TypeError, ValueError):
        new = None
    if new is not None:
        tighter = (s.side == "buy" and s.stop < new < s.last) or (
            s.side == "short" and s.last < new < s.stop
        )
        stop = new if tighter else s.stop
    t = terms(arena, pb, acct, s, stop)
    if t is None:
        return {"skipped": "cannot be sized with the agent's stop"}
    res = place(
        arena,
        pb,
        acct,
        s,
        t,
        "agent",
        answer.get("model") or "agent",
        f"agent confirmed: {answer.get('why', '')}",
        seen_at,
    )
    return {**res, "seconds": answer.get("seconds")}


def _industries(arena) -> dict:
    try:
        from asxbot.data.universe import fetch_directory

        d = fetch_directory(arena.cfg.data_dir, arena.cfg.get("collector.user_agent"))
        return {str(r.code).upper(): str(r.industry) for r in d.itertuples()}
    except Exception:  # noqa: BLE001
        return {}


def _news_rows(arena, day: date) -> dict:
    p = Path(arena.cfg.data_dir) / "announcements" / "live" / f"{day.isoformat()}.parquet"
    if not p.exists():
        return {}
    df = pd.read_parquet(p)
    out: dict = {}
    for r in df.itertuples():
        out.setdefault(str(r.code).upper(), []).append(
            f"{r.released_at:%H:%M} {'[price sensitive] ' if r.price_sensitive else ''}{r.headline}"
        )
    return out
