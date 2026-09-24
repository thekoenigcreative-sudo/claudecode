"""Announcements v2: trade the reaction, not the news (config.yaml asx_announcements_v2,
frozen 2026-09-24 before it ran).

What is here, all plain code:
  * the v2 screen - halt notices and coarse ticks out (as v1), the size-aware turnover rule
    in place of v1's fixed $250,000 floor, and "no live quote" out with the ticker written
    down for the IBKR data fix;
  * the reaction, measured from minute bars: the move against the ASX 200 and the volume
    against the stock's usual volume over the same minutes. v1 read the day's volume from
    a quote field that always returned nothing (24 Sep day review); v2 never uses it;
  * the reaction-look queue: every stock with price-sensitive news today gets ONE look once
    the market has traded it for 10 minutes, before it has traded it for 40;
  * the rule bot (yardstick) for v2, and the flat sweep before the close.

The agent's part - the reader and the decider's reaction packet - is in watch.py.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as time_cls
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from asxbot.announcements.model import Announcement
from asxbot.arena.intraday import (
    MarketView,
    continuous,
    counted_volume,
    minute_of_session,
    vwap,
)
from asxbot.arena.liquid import size_rule
from asxbot.arena.tradability import Screen, is_halt, tick_pct
from asxbot.io import write_text_atomic
from asxbot.live.quotes import median_turnover_20d
from asxbot.live.scanner import asx_tick, round_to_tick
from asxbot.log import EventLog, get_logger

log = get_logger("asxbot.arena.reaction_v2")
SYD = ZoneInfo("Australia/Sydney")
OPEN = time_cls(10, 0)
PREV_CLOSE_TIME = time_cls(16, 10)


# --------------------------------------------------------------------------
# the v2 screen
# --------------------------------------------------------------------------
def screen_v2(a: Announcement, quote, daily, pb, max_order_aud: float) -> Screen:
    """The v2 screen, in plain code, before any model call."""
    conf = pb.raw.get("screen") or {}
    if conf.get("halt_headlines", True) and is_halt(a):
        return Screen(False, f"the announcement is a halt or suspension ({a.headline})", "halted")
    if quote is None:
        return Screen(False, "no live quote, so an entry and a stop cannot be priced", "no_quote")
    tp = tick_pct(quote.last)
    max_tick = float(conf.get("max_tick_pct", 3.0))
    if tp > max_tick:
        return Screen(
            False,
            f"one tick ({asx_tick(quote.last):.3f}) is {tp:.2f}% of the {quote.last:.3f} price, "
            f"above the {max_tick}% limit",
            "tick",
            tick_pct=tp,
        )
    rule = size_rule(pb)
    turnover = median_turnover_20d(daily, rule.window if rule else 20)
    if turnover is None:
        return Screen(False, "not enough daily history to measure turnover", "no_history")
    if rule is not None and max_order_aud > rule.max_order(turnover) + 1e-6:
        return Screen(
            False,
            f"a ${max_order_aud:,.0f} order is more than {rule.share:.0%} of its median daily "
            f"turnover ${turnover:,.0f} (it needs ${rule.floor(max_order_aud):,.0f})",
            "turnover",
            turnover=turnover,
            tick_pct=tp,
        )
    return Screen(
        True,
        f"tradeable: ${turnover:,.0f} median turnover, tick {tp:.2f}% of price",
        turnover=turnover,
        tick_pct=tp,
    )


def log_no_quote(data_dir: Path, day: date, ticker: str, ids_id: str, headline: str) -> None:
    """Rick's brief: keep "no live quote" out, but write the ticker down for the IBKR fix."""
    p = Path(data_dir) / "arena" / "no_quote" / f"{day.isoformat()}.json"
    rows = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    rows.append(
        {
            "ticker": ticker,
            "ids_id": ids_id,
            "headline": headline,
            "at": datetime.now(SYD).isoformat(timespec="seconds"),
        }
    )
    write_text_atomic(json.dumps(rows, indent=2), p)


# --------------------------------------------------------------------------
# the reaction, from minute bars
# --------------------------------------------------------------------------
def _close_at(df: pd.DataFrame, ts: datetime) -> float | None:
    part = df[df.index <= ts]
    return None if not len(part) else float(part["close"].iloc[-1])


def reaction(
    view: MarketView, code: str, now: datetime, since: datetime, base: float | None = None
) -> dict:
    """What the market has done with the stock from `since` (the minute the news reached the
    market) to the newest bar a decision at `now` may see. Every number from minute bars."""
    bars = continuous(view.bars(code, now))
    idx = continuous(view.bars(view.index, now))
    prev = view.prev_close(code)
    iprev = view.prev_close(view.index)
    out: dict = {"available": False, "data_label": view.label}
    if prev is None or iprev is None:
        out["why"] = "no previous close in the minute cache"
        return out
    after = bars[bars.index >= since]
    if not len(after) or not len(idx):
        out["why"] = "no trade since the news reached the market"
        return out
    last_ts = after.index.max()
    last = float(after["close"].iloc[-1])
    base = base if base is not None else prev
    # The index over the same span: from the base's time (the previous close, or the last
    # trade before the news) to the stock's newest bar.
    idx_last = _close_at(idx, last_ts)
    if since.time() <= OPEN or base == prev:
        idx_base = iprev
    else:
        idx_base = _close_at(idx, since - timedelta(minutes=1)) or iprev
    if idx_last is None:
        out["why"] = "no index bar yet"
        return out
    move = (last / base - 1) * 100
    idx_move = (idx_last / idx_base - 1) * 100
    usual = view.usual(code)
    vol = counted_volume(after)  # never the 10:00 bar (intraday.VOLUME_FROM)
    vol_mult = None
    if usual is not None:
        m1, m0 = minute_of_session(last_ts), minute_of_session(after.index.min()) - 1
        if 0 <= m1 < len(usual):
            base_vol = float(usual.iloc[m1]) - (float(usual.iloc[m0]) if m0 >= 0 else 0.0)
            vol_mult = vol / base_vol if base_vol > 0 else None
    day_bars = bars[bars.index.time >= OPEN]
    vw = vwap(day_bars) if len(day_bars) else None
    first = day_bars.iloc[0] if len(day_bars) else None
    out.update(
        {
            "available": True,
            "as_of_bar": last_ts.strftime("%H:%M"),
            "minutes_traded_since_news": int(len(after)),
            "previous_close": round(prev, 4),
            "reaction_base": round(base, 4),
            "last": round(last, 4),
            "move_pct": round(move, 2),
            "index_move_pct": round(idx_move, 2),
            "move_vs_index_pct": round(move - idx_move, 2),
            "volume_since_news": int(vol),
            "volume_vs_usual_same_minutes": None if vol_mult is None else round(vol_mult, 2),
            "high_since_news": round(float(after["high"].max()), 4),
            "low_since_news": round(float(after["low"].min()), 4),
            "day_open_first_minute": None if first is None else round(float(first["open"]), 4),
            "vwap": None if vw is None or not len(vw) else round(float(vw.iloc[-1]), 4),
        }
    )
    out["bars"] = [
        f"{ts:%H:%M} o{r.open:.4g} h{r.high:.4g} l{r.low:.4g} c{r.close:.4g} v{int(r.volume)}"
        for ts, r in after.head(30).iterrows()
    ]
    return out


def wakes(r: dict, pb) -> tuple[bool, str]:
    """Is the reaction worth a decider call? config reaction.wake_* (written before running)."""
    conf = pb.raw.get("reaction") or {}
    mv = float(conf.get("wake_move_vs_index_pct", 1.0))
    vm = float(conf.get("wake_volume_multiple", 2.0))
    if not r.get("available"):
        return False, r.get("why", "no reaction data")
    move = abs(float(r["move_vs_index_pct"]))
    vol = r.get("volume_vs_usual_same_minutes")
    if move >= mv:
        return True, f"moved {r['move_vs_index_pct']:+.2f}% against the ASX 200"
    if vol is not None and vol >= vm:
        return True, f"traded {vol:.1f}x its usual volume over the same minutes"
    v = "unknown" if vol is None else f"{vol:.1f}x"
    return False, (
        f"quiet: {r['move_vs_index_pct']:+.2f}% against the index (< {mv}%), volume {v} (< {vm}x)"
    )


# --------------------------------------------------------------------------
# the reaction-look queue: one look per stock per day
# --------------------------------------------------------------------------
def _queue_path(data_dir: Path, day: date) -> Path:
    return Path(data_dir) / "arena" / "reaction" / f"{day.isoformat()}.json"


def load_queue(data_dir: Path, day: date) -> dict:
    p = _queue_path(data_dir, day)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_queue(data_dir: Path, day: date, q: dict) -> None:
    write_text_atomic(json.dumps(q, indent=2, default=str), _queue_path(data_dir, day))


def enqueue(data_dir: Path, day: date, a: Announcement, screen_why: str = "") -> bool:
    """Queue a stock for its reaction look. A second announcement for the same stock joins
    the first; the stock still gets one look. Returns True if the stock is new today."""
    q = load_queue(data_dir, day)
    code = a.code.upper()
    item = q.get(code)
    new = item is None
    if new:
        item = {
            "ticker": code,
            "status": "queued",
            "ids": [],
            "headlines": [],
            "first_release": a.released_at.isoformat(),
            "screen": screen_why,
        }
    if a.ids_id not in item["ids"]:
        item["ids"].append(a.ids_id)
        item["headlines"].append(a.headline)
    if a.released_at.isoformat() < item["first_release"]:
        item["first_release"] = a.released_at.isoformat()
    q[code] = item
    save_queue(data_dir, day, q)
    return new


def news_reaches_market(release: datetime, day: date) -> datetime:
    """The minute the news can first be traded on: the open for news before 10:00 (after the
    previous close), else the release minute."""
    rel = release if release.tzinfo else release.replace(tzinfo=SYD)
    rel = rel.astimezone(SYD)
    open_ = datetime.combine(day, OPEN, tzinfo=SYD)
    return max(rel.replace(second=0, microsecond=0), open_)


@dataclass
class LookReady:
    ticker: str
    status: str  # ready | wait | halted | missed
    why: str
    ref: datetime | None = None
    base: float | None = None


def look_status(view: MarketView, item: dict, now: datetime, pb) -> LookReady:
    """Is this stock's reaction look due? Uses only bars a decision at `now` may see."""
    conf = pb.raw.get("reaction") or {}
    after = int(conf.get("after_minutes", 10))
    before = int(conf.get("before_minutes", 40))
    halt_min = int((pb.raw.get("screen") or {}).get("halted_if_no_trade_minutes", 10))
    code = item["ticker"]
    t0 = news_reaches_market(datetime.fromisoformat(item["first_release"]), view.day)
    data_time = view.data_time(now)
    if data_time is None or data_time < t0 + timedelta(minutes=after - 1):
        return LookReady(code, "wait", "the feed has not reached the look yet")
    bars = continuous(view.bars(code, now))
    since = bars[bars.index >= t0]
    if not len(since):
        if data_time >= t0 + timedelta(minutes=halt_min):
            return LookReady(
                code,
                "halted",
                f"no trade in the {halt_min} minutes after the news reached the "
                f"market at {t0:%H:%M} (halted, paused or not trading)",
            )
        return LookReady(code, "wait", "no trade yet since the news")
    ref = since.index.min().to_pydatetime()
    newest = since.index.max().to_pydatetime()
    if newest < ref + timedelta(minutes=after - 1):
        if data_time >= ref + timedelta(minutes=before):
            return LookReady(code, "missed", "the stock traded too thinly to show 10 minutes")
        return LookReady(code, "wait", "fewer than 10 minutes of trading visible")
    if newest >= ref + timedelta(minutes=before):
        return LookReady(
            code,
            "missed",
            f"first reached with bars to {newest:%H:%M}, past the {before}-minute "
            f"window after {ref:%H:%M}",
        )
    base = None
    if t0.time() > OPEN:
        pre = bars[bars.index < t0]
        base = float(pre["close"].iloc[-1]) if len(pre) else None
    return LookReady(code, "ready", f"{len(since)} minutes of trading since {ref:%H:%M}", ref, base)


def round_trip_cost_pct(costs, value_aud: float, adv: float | None) -> float:
    """Brokerage both ways plus slippage both ways, as a % of the position."""
    if value_aud <= 0:
        return 0.0
    brokerage = 2 * costs.brokerage(value_aud) / value_aud * 100
    slip = 2 * costs.slippage_pct(value_aud, adv) * 100
    return round(brokerage + slip, 3)


# --------------------------------------------------------------------------
# the rule bot for v2 (the yardstick), no model
# --------------------------------------------------------------------------
@dataclass
class BotSignal:
    ticker: str
    side: str  # buy | short
    move_vs_index_pct: float
    volume_multiple: float
    last: float
    stop: float
    why: str


def v2_bot_candidates(
    ann: pd.DataFrame, day: date, prev_session: date, universe: set[str], measure_at: time_cls
) -> list[str]:
    """Stocks with price-sensitive news released after the previous session's 16:10 close
    and before the measure time today."""
    if ann is None or not len(ann):
        return []
    start = datetime.combine(prev_session, PREV_CLOSE_TIME)
    end = datetime.combine(day, measure_at)
    rows = ann[
        (ann["price_sensitive"].astype(bool))
        & (ann["released_at"] >= start)
        & (ann["released_at"] < end)
    ]
    return sorted({str(c).upper() for c in rows["code"] if str(c).upper() in universe})


def v2_bot_signal(
    view: MarketView, code: str, now: datetime, params: dict, shortable: bool
) -> tuple[BotSignal | None, str]:
    """The v2 rule at 10:30, on bars final by `now`. (None, why) when it does not fire."""
    at = time_cls.fromisoformat(str(params.get("measure_at", "10:30")))
    day = view.day
    end = datetime.combine(day, at, tzinfo=SYD)  # bars 10:00 .. end-1min
    last_bar = end - timedelta(minutes=1)
    idx = view.bars(view.index, now)
    if not len(idx) or idx.index.max() < last_bar:
        return None, "the feed has not reached 10:30 yet"
    bars = continuous(view.bars(code, now))
    window = bars[(bars.index >= datetime.combine(day, OPEN, tzinfo=SYD)) & (bars.index < end)]
    if not len(window):
        return None, "no trade 10:00-10:30"
    prev, iprev = view.prev_close(code), view.prev_close(view.index)
    if prev is None or iprev is None:
        return None, "no previous close in the minute cache"
    usual = view.usual(code)
    if usual is None:
        return None, "no usual-volume baseline (too few prior sessions cached)"
    m = minute_of_session(last_bar)
    base_vol = float(usual.iloc[m]) if 0 <= m < len(usual) else 0.0
    if base_vol <= 0:
        return None, "usual first-30-minute volume is zero"
    vol_mult = counted_volume(window) / base_vol  # 10:01-10:29 (intraday.VOLUME_FROM)
    last = float(window["close"].iloc[-1])
    idx_last = _close_at(idx, last_bar)
    if idx_last is None:
        return None, "no index bar at 10:29"
    move = ((last / prev - 1) - (idx_last / iprev - 1)) * 100
    need_move = float(params.get("move_vs_index_pct", 3.0))
    need_vol = float(params.get("volume_multiple", 3.0))
    base = f"{move:+.2f}% vs the ASX 200 at 10:30 on {vol_mult:.1f}x usual first-30-minute volume"
    if vol_mult < need_vol:
        return None, base + f": volume below {need_vol}x"
    if move >= need_move:
        return BotSignal(code, "buy", move, vol_mult, last, float(window["low"].min()), base), base
    if move <= -need_move:
        if not shortable:
            return None, base + ": would be a short, but not an ASX 200 member"
        return BotSignal(
            code, "short", move, vol_mult, last, float(window["high"].max()), base
        ), base
    return None, base + f": move inside +/-{need_move}%"


def bot_order_terms(
    sig: BotSignal, params: dict, size_aud: float, turnover: float | None, share: float | None
) -> tuple[int, float, float]:
    """(qty, limit, stop) for a v2 bot signal: $5,000 or less, capped by the size rule."""
    slack = float(params.get("entry_limit_slack_pct", 2.0)) / 100.0
    buy = sig.side == "buy"
    limit = round_to_tick(sig.last * (1 + slack if buy else 1 - slack), up=buy)
    value = float(size_aud)
    if turnover and share:
        value = min(value, turnover * share)
    qty = int(math.floor(value / limit)) if limit > 0 else 0
    stop = round_to_tick(sig.stop, up=not buy)
    return qty, limit, stop


def _state_path(data_dir: Path, day: date) -> Path:
    return Path(data_dir) / "arena" / "v2bot" / f"{day.isoformat()}.json"


def load_bot_state(data_dir: Path, day: date) -> dict:
    p = _state_path(data_dir, day)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_bot_state(data_dir: Path, day: date, state: dict) -> None:
    write_text_atomic(json.dumps(state, indent=2, default=str), _state_path(data_dir, day))


def events(data_dir: Path) -> EventLog:
    return EventLog(data_dir)
