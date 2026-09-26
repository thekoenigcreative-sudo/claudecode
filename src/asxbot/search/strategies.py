"""The rule families the search tries. Each takes an idea's parameters and a list of sessions
and returns per-day books in the lab's record shape (search/book.py). Plain code only.

Families (CLOUD_BRIEF_SEARCH.md 1-2):
  orb_inplay   A. stocks in play, opening-range breakout (yardstick A)
  drift        B. announcement drift held 2-10 days, by announcement type (yardstick B)
  gap_fade     no-news opening gaps fading back towards the previous close
  late_trend   the day's strongest in-play movers held from 15:30 into the closing auction
  reversal     buying the day's biggest no-news losers in the closing auction, sold later
  breakout20   a close above the 20-session high on heavy volume, held for days
  day2_orb     the day after a big news reaction, an opening-range break in its direction
Regime filter and closing-volume cap (yardstick C) are parameters every family accepts:
  regime       None | "index_ma" (the index's previous close above its 20-session average
               for longs, below for shorts) | "first30" (the index's move from its open to
               10:30 in the trade's direction; entries wait until 10:31)
  close_cap    None or a share: an entry is never larger than that share of the stock's
               median closing-auction volume over the 14 sessions before (so the exit at
               the close can be absorbed - the replay had 9 positions stuck at the close)
Every family is long only unless `shorts` is set, and then shorts only in the dated ASX 200
list on disk (none on disk: long only).
"""

from __future__ import annotations

from datetime import date, time

import numpy as np
import pandas as pd

from asxbot.search import sim
from asxbot.search.costs import Costs, tick
from asxbot.search.data import CONT_END, CONT_START, GRID_START, Market, minute_index

START_CASH = 20_000.0


class Ctx:
    """What every family needs for one run: the market, the costs, the day table by day."""

    def __init__(self, market: Market, costs: Costs):
        self.m = market
        self.costs = costs
        d = market.daily.copy()
        d["next_open"] = d.groupby("code")["open"].shift(-1)  # used ONLY to price a stuck exit
        self.by_day = {k: g.set_index("code") for k, g in d.groupby("day")}
        self.sessions = sorted(self.by_day)
        self.pos = {k: i for i, k in enumerate(self.sessions)}
        self.shortable = market.shortable()
        ann = market.announcements(date(2000, 1, 1), date(2100, 1, 1))
        if len(ann):
            ann = ann.assign(day0=day0_of(ann["released_at"], self.sessions))
        self.ann = ann

    def rows(self, d: date) -> pd.DataFrame | None:
        return self.by_day.get(pd.Timestamp(d))

    def prev_session(self, d: date) -> pd.Timestamp | None:
        i = self.pos.get(pd.Timestamp(d))
        return self.sessions[i - 1] if i else None

    def news(self, since: pd.Timestamp, until: pd.Timestamp, ps_only: bool = True) -> pd.DataFrame:
        a = self.ann
        if not len(a):
            return a
        a = a[(a["released_at"] >= since) & (a["released_at"] < until)]
        return a[a["price_sensitive"]] if ps_only else a


def day0_of(released: pd.Series, sessions: list[pd.Timestamp]) -> pd.Series:
    """The session whose trading first saw each announcement: its release day if that is a
    session and it came before 16:00 (the end of continuous trading), else the next session
    in the data (NaT past the data's end)."""
    s = np.array(sessions, dtype="datetime64[ns]")
    day = released.dt.normalize().to_numpy(dtype="datetime64[ns]")
    late = (released.dt.hour >= 16).to_numpy()
    i = np.searchsorted(s, day, side="left")
    same = (i < len(s)) & (s[np.minimum(i, len(s) - 1)] == day) & ~late
    j = np.where(same, i, np.searchsorted(s, day, side="right"))
    out = np.where(j < len(s), s[np.minimum(j, len(s) - 1)], np.datetime64("NaT", "ns"))
    return pd.Series(pd.to_datetime(out), index=released.index)


def liquid(rows: pd.DataFrame, max_value: float, min_price: float = 0.05) -> pd.DataFrame:
    """The day trader's liquid universe rule: median turnover (20 sessions before) at least
    20x the largest order, a tick at most 1% of the price, and a real price."""
    r = rows[(rows["adv_turnover"] >= 20 * max_value) & (rows["prev_close"] >= min_price)]
    ticks = r["prev_close"].map(tick) / r["prev_close"]
    return r[ticks <= 0.01]


def regime_ok(ctx: Ctx, d: date, sign: int, how: str | None) -> bool:
    if not how:
        return True
    ix = ctx.m.index
    ts = pd.Timestamp(d)
    if not len(ix) or ts not in ix.index:
        return False  # no index that day: missing data waits, it never decides
    if how == "index_ma":
        above = ix.at[ts, "prev_close_above_ma20"]
        if pd.isna(ix.at[ts, "ma20"]):
            return False
        return bool(above) if sign > 0 else not bool(above)
    if how == "first30":
        o, p = ix.at[ts, "open"], ix.at[ts, "px_1030"]
        if not (np.isfinite(o) and np.isfinite(p)):
            return False
        return (p > o) if sign > 0 else (p < o)
    raise ValueError(f"unknown regime {how!r}")


def cap_by_close(qty: int, row, share: float | None) -> int:
    if not share:
        return qty
    med = row.get("auction_vol_med14")
    if med is None or not np.isfinite(med) or med <= 0:
        return 0  # no closing auction history: the exit could not be sized, so no entry
    return min(qty, int(share * med))


def _mk(code, side, d, fill, stop, costs, row, **tags) -> sim.Trade:
    eff, fee = sim.enter(side, fill, costs, row["adv_turnover"], row["vol20"])
    return sim.Trade(code, side, d.isoformat(), fill.qty, fill.px, eff, stop,
                     entry_i=fill.last_i, fees=fee, tags=tags)  # fmt: skip


# ---------------------------------------------------------------------------------------
# A. stocks in play, opening-range breakout
# ---------------------------------------------------------------------------------------
ORB_DEFAULTS = {
    "window": 5,  # minutes of the opening range, after the stock's own opening minute
    "top_k": 3,  # trade at most this many stocks a day (highest opening RVOL first)
    "min_rvol": 1.0,
    "gap_min": 0.02,  # a catalyst: price-sensitive news since the last close, or this gap
    "catalyst": "news_or_gap",  # news_or_gap | news | gap | none
    "exit": "close",  # close | trail
    "trail_r": 1.0,
    "trail_after_r": 1.0,
    "risk_pct": 1.0,
    "max_value": 5000.0,
    "min_r_over_cost": 3.0,
    "shorts": False,
    "regime": None,
    "close_cap": None,
    "last_entry": "15:00",
    "slack": 0.01,
    "good_for": 5,
}


def orb_inplay(ctx: Ctx, days: list[date], P: dict) -> list[dict]:
    P = {**ORB_DEFAULTS, **P}
    w = int(P["window"])
    out = []
    for d in days:
        book = []
        rows, p = ctx.rows(d), ctx.m.panel(d)
        if rows is None or p is None:
            out.append(None)
            continue
        # The range is the stock's first `w` continuous minutes (10:00 on for nearly every
        # stock in the IBKR bars); the decision is taken once every candidate's range has
        # closed, allowing a stock to open up to 2 minutes late. Orders rest from 2 bars later.
        D = minute_index(time(10, 0)) + w - 1 + 2
        from_i = D + sim.LATENCY
        if P["regime"] == "first30":
            from_i = max(from_i, minute_index(time(10, 31)) + sim.LATENCY)
        last_i = minute_index(time.fromisoformat(P["last_entry"]))
        r = liquid(rows, P["max_value"])
        r = r[(r["first_idx"] >= 0) & (r["first_idx"] + w - 1 <= D) & (r[f"ow{w}_avg14"] > 0)]
        r = r.assign(rvol=r[f"ow{w}"] / r[f"ow{w}_avg14"])
        r = r[r["rvol"] >= P["min_rvol"]]
        prev = ctx.prev_session(d)
        since = (prev + pd.Timedelta(hours=16)) if prev is not None else None
        until = pd.Timestamp(d) + pd.Timedelta(minutes=GRID_START + 1 + D)
        news_codes = set(ctx.news(since, until)["code"]) if since is not None else set()
        cat = P["catalyst"]
        has_news = r.index.isin(news_codes)
        has_gap = r["gap"].abs() >= P["gap_min"]
        keep = {"news_or_gap": has_news | has_gap, "news": has_news, "gap": has_gap,
                "none": np.ones(len(r), bool)}[cat]  # fmt: skip
        r = r[keep].sort_values("rvol", ascending=False).head(int(P["top_k"]))
        for code, row in r.iterrows():
            i = p.row.get(code)
            if i is None:
                continue
            f = int(row["first_idx"])
            seg = slice(f, f + w)
            hi, lo = np.nanmax(p.h[i, seg]), np.nanmin(p.lo[i, seg])
            closes = p.c[i, seg][np.isfinite(p.c[i, seg])]
            if not np.isfinite(hi) or not np.isfinite(lo) or not len(closes) or hi <= lo:
                continue
            o0 = float(p.o[i, f])
            sign = 1 if closes[-1] > o0 else -1 if closes[-1] < o0 else 0
            if sign == 0:
                continue
            if sign < 0 and not (P["shorts"] and code in ctx.shortable):
                continue
            if not regime_ok(ctx, d, sign, P["regime"]):
                continue
            side = "buy" if sign > 0 else "short"
            level = sim.level_above(hi) if sign > 0 else sim.level_below(lo)
            stop = sim.level_below(lo) if sign > 0 else sim.level_above(hi)
            qty = sim.size_by_risk(START_CASH, P["risk_pct"], level, stop, P["max_value"],
                                   row["adv_turnover"])  # fmt: skip
            qty = cap_by_close(qty, row, P["close_cap"])
            if qty <= 0:
                continue
            rt = ctx.costs.round_trip(level, qty, row["adv_turnover"], row["vol20"])
            if qty * abs(level - stop) < P["min_r_over_cost"] * rt:
                continue
            fill = sim.stop_entry(p, i, level, side, from_i, last_i, qty, P["slack"],
                                  P["good_for"])  # fmt: skip
            if fill is None:
                continue
            t = _mk(code, side, d, fill, stop, ctx.costs, row, rvol=round(float(row["rvol"]), 2))
            trail = P["exit"] == "trail"
            sim.manage_and_exit(p, i, t, ctx.costs, row["adv_turnover"], row["vol20"],
                                stop=stop, trail_r=P["trail_r"] if trail else None,
                                trail_after_r=P["trail_after_r"],
                                next_open=_f(row.get("next_open")))  # fmt: skip
            book.append(t)
        out.append(book)
    return out


# ---------------------------------------------------------------------------------------
# B. announcement drift, held for days
# ---------------------------------------------------------------------------------------
DRIFT_DEFAULTS = {
    "buckets": ["results", "guidance_up", "contract", "drilling", "takeover"],
    "ps_only": True,
    "react_min": 0.03,  # day-0 move against the index, measured at 16:00 (before the auction)
    "react_max": 0.25,  # beyond this it is a re-rating/takeover jump, not a drift candidate
    "hold": 5,  # sessions after day 0; out in that session's closing auction
    "entry": "close0",  # close0 (day 0's closing auction) | open1 (the next session's open)
    "stop_pct": None,  # a stop this far under the entry, on the daily low (open if gapped)
    "max_positions": 5,
    "per_position": 4000.0,
    "min_turnover": 500_000.0,
    "regime": None,
    "close_cap": 0.2,
    "direction": "with",  # with (buy after a rise) | against (buy after a fall: overreaction)
}


def drift(ctx: Ctx, days: list[date], P: dict) -> list[dict]:
    """Multi-day holds on the daily table (auction prints in, auction prints out). One
    $20,000 book over the whole window; the day records carry the marked daily P&L."""
    P = {**DRIFT_DEFAULTS, **P}
    dayset = [pd.Timestamp(x) for x in days]
    want = set(dayset)
    signals: dict[pd.Timestamp, list[tuple[str, pd.Series, str]]] = {}
    a = ctx.ann
    if len(a):
        a = a[a["bucket"].isin(P["buckets"])]
        if P["ps_only"]:
            a = a[a["price_sensitive"]]
    for _, n in a.iterrows() if len(a) else []:
        d0 = n["day0"]
        if pd.isna(d0) or d0 not in want:
            continue
        rows = ctx.by_day[d0]
        if n["code"] not in rows.index:
            continue
        row = rows.loc[n["code"]]
        if not np.isfinite(row["prev_close"]) or not np.isfinite(row["last_cont_close"]):
            continue
        if not (row["adv_turnover"] >= P["min_turnover"]):
            continue
        ix_ret = _index_ret(ctx, d0)
        if ix_ret is None:
            continue
        react = row["last_cont_close"] / row["prev_close"] - 1 - ix_ret
        s = 1 if P["direction"] == "with" else -1
        if not (P["react_min"] <= s * react <= P["react_max"]):
            continue
        if not regime_ok(ctx, d0.date(), 1, P["regime"]):
            continue
        signals.setdefault(d0, []).append((n["code"], row, n["bucket"]))
    return _hold_book(ctx, dayset, signals, P, tag="drift")


def _index_ret(ctx: Ctx, d: pd.Timestamp) -> float | None:
    ix = ctx.m.index
    if not len(ix) or d not in ix.index:
        return None
    g = np.asarray(ix.at[d, "grid_close"], dtype=float)
    pc = ix.at[d, "prev_close"]
    ok = np.where(np.isfinite(g[: CONT_END + 1]))[0]
    if not len(ok) or not np.isfinite(pc):
        return None
    return float(g[ok[-1]]) / float(pc) - 1


def _hold_book(ctx: Ctx, dayset, signals, P, tag: str) -> list[dict]:
    """Open each signal (entry at day 0's closing auction, or the next session's open), hold
    `hold` sessions, out in the closing auction; a stop on the daily low if set. Auction fills
    capped at AUCTION_SHARE of the day's auction volume (and `close_cap` of its median)."""
    from asxbot.search.costs import AUCTION_SHARE

    costs = ctx.costs
    daily_pnl = {d: 0.0 for d in dayset}
    exits: dict[pd.Timestamp, list[sim.Trade]] = {d: [] for d in dayset}
    fees_by_day = {d: 0.0 for d in dayset}
    stuck_by_day: dict[pd.Timestamp, list] = {d: [] for d in dayset}
    open_pos: list[dict] = []
    sessions = ctx.sessions
    for d in dayset:
        rows = ctx.by_day.get(d)
        if rows is None:
            continue
        # 1) exits due today, stops, and marks for what is held
        still = []
        for pos in open_pos:
            code, t = pos["code"], pos["trade"]
            if code not in rows.index:
                still.append(pos)  # no trade today: held, marked flat
                continue
            row = rows.loc[code]
            pos["age"] += 1 if d > pos["d_in"] else 0
            stop = pos.get("stop")
            out_px, why = None, ""
            if stop is not None and np.isfinite(row["low"]) and row["low"] <= stop:
                out_px, why = min(stop, row["open"]), "stop"
            elif pos["age"] >= P["hold"]:
                out_px, why = row["close"], "time"
            if out_px is not None and np.isfinite(out_px):
                q = t.qty
                if why == "time":
                    cap = int(AUCTION_SHARE * row["auction_vol"]) if row["has_auction"] else 0
                    if cap < q:
                        t.stuck = q - cap
                        nxt = row.get("next_open")
                        leftover_px = (_f(nxt) or out_px * (1 - sim.STUCK_PENALTY))
                        out_px = (cap * out_px + (q - cap) * leftover_px) / q
                        why += "+stuck"
                        stuck_by_day[d].append({"ticker": code, "qty": t.stuck,
                                                "value": round(t.stuck * out_px, 2)})  # fmt: skip
                sim.close_out(t, out_px * q, q, -1, why, costs, row["adv_turnover"], row["vol20"])
                t.exit_day = d.date().isoformat()
                daily_pnl[d] += (t.exit - pos["mark"]) * q - (t.fees - pos["fees_booked"])
                fees_by_day[d] += t.fees - pos["fees_booked"]
                exits[d].append(t)
            else:
                if np.isfinite(row["close"]):
                    daily_pnl[d] += (row["close"] - pos["mark"]) * t.qty
                    pos["mark"] = row["close"]
                still.append(pos)
        open_pos = still
        # 2) entries: signals of today (close0) or of the session before (open1)
        if P["entry"] == "close0":
            todays = signals.get(d, [])
        else:
            i = ctx.pos.get(d)
            todays = signals.get(sessions[i - 1], []) if i else []
        held = {p["code"] for p in open_pos}
        room = int(P["max_positions"]) - len(open_pos)
        todays = sorted(todays, key=lambda x: -x[1]["adv_turnover"])
        for code, _sig, bucket in todays:
            if room <= 0 or code in held or code not in rows.index:
                continue
            row = rows.loc[code]
            px = row["close"] if P["entry"] == "close0" else row["open"]  # auctions both
            if not np.isfinite(px) or px <= 0:
                continue
            q = int(P["per_position"] / px)
            q = min(q, int(0.05 * row["adv_turnover"] / px)) if np.isfinite(
                row["adv_turnover"]) else q  # fmt: skip
            q = cap_by_close(q, row, P["close_cap"])
            if P["entry"] == "close0":
                q = min(q, int(AUCTION_SHARE * row["auction_vol"])) if row["has_auction"] else 0
            elif np.isfinite(row["auc_open_px"]):  # the opening auction: 20% of its volume
                q = min(q, int(AUCTION_SHARE * row["auc_open_vol"]))
            else:
                q = 0  # no opening auction that day: no entry (missing data never decides)
            if q <= 0:
                continue
            fill = sim.Fill(q, float(px), -1, -1)
            t = _mk(code, "buy", d.date(), fill, None, costs, row, bucket=bucket, kind=tag)
            stop = px * (1 - P["stop_pct"]) if P.get("stop_pct") else None
            t.stop0 = stop
            mark = row["close"] if np.isfinite(row["close"]) else px
            daily_pnl[d] += (mark - t.entry) * q - t.fees
            fees_by_day[d] += t.fees
            open_pos.append({"code": code, "trade": t, "d_in": d, "age": 0, "mark": mark,
                             "fees_booked": t.fees, "stop": stop})  # fmt: skip
            held.add(code)
            room -= 1
    # positions still open at the window's end: closed at their last mark, less the exit costs
    last = dayset[-1] if dayset else None
    for pos in open_pos:
        t = pos["trade"]
        rows = ctx.by_day.get(last)
        row = rows.loc[pos["code"]] if rows is not None and pos["code"] in rows.index else None
        adv = row["adv_turnover"] if row is not None else None
        v20 = row["vol20"] if row is not None else None
        sim.close_out(t, pos["mark"] * t.qty, t.qty, -1, "window end", costs, adv, v20)
        t.exit_day = last.date().isoformat()
        daily_pnl[last] += (t.exit - pos["mark"]) * t.qty - (t.fees - pos["fees_booked"])
        fees_by_day[last] += t.fees - pos["fees_booked"]
        exits[last].append(t)
    return [
        {"pnl": daily_pnl[d], "fees": fees_by_day[d], "trades": exits[d],
         "stuck": stuck_by_day[d], "multi_day": True}  # fmt: skip
        for d in dayset
    ]


# ---------------------------------------------------------------------------------------
# widening: other families
# ---------------------------------------------------------------------------------------
GAP_FADE_DEFAULTS = {
    "gap_min": 0.03,  # a gap DOWN at least this big (long only), with no news since the close
    "entry_at": "10:30",
    "stop_under_low_ticks": 1,  # stop: a tick under the day's low so far
    "risk_pct": 1.0,
    "max_value": 5000.0,
    "top_k": 3,
    "min_r_over_cost": 2.0,
    "regime": None,
    "close_cap": None,
    "need_green_from_low": True,  # the price at entry is above the low so far (turning)
}


def gap_fade(ctx: Ctx, days: list[date], P: dict) -> list:
    P = {**GAP_FADE_DEFAULTS, **P}
    out = []
    ei = minute_index(time.fromisoformat(P["entry_at"]))
    for d in days:
        rows, p = ctx.rows(d), ctx.m.panel(d)
        if rows is None or p is None:
            out.append(None)
            continue
        book = []
        r = liquid(rows, P["max_value"])
        r = r[(r["gap"] <= -P["gap_min"]) & (r["first_idx"] >= 0) & (r["first_idx"] < ei)]
        prev = ctx.prev_session(d)
        if prev is not None:
            since = prev + pd.Timedelta(hours=16)
            until = pd.Timestamp(d) + pd.Timedelta(minutes=GRID_START + 1 + ei)
            r = r[~r.index.isin(set(ctx.news(since, until, ps_only=False)["code"]))]
        r = r.sort_values("gap").head(int(P["top_k"]))
        if not regime_ok(ctx, d, 1, P["regime"]):
            r = r.iloc[0:0]
        for code, row in r.iterrows():
            i = p.row.get(code)
            if i is None:
                continue
            seen = slice(CONT_START, ei + 1)
            lo = np.nanmin(p.lo[i, seen])
            cs = p.c[i, seen][np.isfinite(p.c[i, seen])]
            if not len(cs) or not np.isfinite(lo):
                continue
            last = float(cs[-1])
            if P["need_green_from_low"] and last <= lo:
                continue
            stop = sim.level_below(lo)
            for _ in range(int(P["stop_under_low_ticks"]) - 1):
                stop = sim.level_below(stop)
            qty = sim.size_by_risk(START_CASH, P["risk_pct"], last, stop, P["max_value"],
                                   row["adv_turnover"])  # fmt: skip
            qty = cap_by_close(qty, row, P["close_cap"])
            if qty <= 0:
                continue
            rt = ctx.costs.round_trip(last, qty, row["adv_turnover"], row["vol20"])
            if qty * abs(last - stop) < P["min_r_over_cost"] * rt:
                continue
            fill = sim.work_fill(p, i, ei + sim.LATENCY, qty, ei + sim.LATENCY + 5,
                                 last * 1.01, "buy")  # fmt: skip
            if fill is None:
                continue
            t = _mk(code, "buy", d, fill, stop, ctx.costs, row, gap=round(float(row["gap"]), 4))
            sim.manage_and_exit(p, i, t, ctx.costs, row["adv_turnover"], row["vol20"],
                                stop=stop, next_open=_f(row.get("next_open")))  # fmt: skip
            book.append(t)
        out.append(book)
    return out


LATE_TREND_DEFAULTS = {
    "at": "15:30",
    "min_move_vs_index": 0.03,  # up at least this much against the index since the prior close
    "min_rvol_day": 1.5,  # volume so far against the median full day (20 sessions before)
    "top_k": 3,
    "max_value": 5000.0,
    "stop_pct": 0.015,
    "catalyst": "any",  # any | news
    "regime": None,
    "close_cap": 0.2,
}


def late_trend(ctx: Ctx, days: list[date], P: dict) -> list:
    """Late-day continuation into the close (intraday momentum; closing-auction demand for
    the day's winners)."""
    P = {**LATE_TREND_DEFAULTS, **P}
    ai = minute_index(time.fromisoformat(P["at"]))
    out = []
    for d in days:
        rows, p = ctx.rows(d), ctx.m.panel(d)
        ig = ctx.m.index_close_grid(d)
        if rows is None or p is None or ig is None:
            out.append(None)
            continue
        book = []
        ipc = ctx.m.index.at[pd.Timestamp(d), "prev_close"]
        iok = np.where(np.isfinite(ig[: ai + 1]))[0]
        if not len(iok) or not np.isfinite(ipc) or not regime_ok(ctx, d, 1, P["regime"]):
            out.append(book)
            continue
        ix_move = float(ig[iok[-1]]) / float(ipc) - 1
        r = liquid(rows, P["max_value"])
        cand = []
        for code, row in r.iterrows():
            i = p.row.get(code)
            if i is None or not np.isfinite(row["prev_close"]):
                continue
            cs = p.c[i, : ai + 1]
            ok = np.where(np.isfinite(cs))[0]
            if not len(ok):
                continue
            px = float(cs[ok[-1]])
            move = px / row["prev_close"] - 1 - ix_move
            vol = float(p.v[i, : ai + 1].sum())
            rv = vol / row["vol_med20"] if row["vol_med20"] and row["vol_med20"] > 0 else 0
            if move >= P["min_move_vs_index"] and rv >= P["min_rvol_day"]:
                cand.append((move, code, row, px, i))
        if P["catalyst"] == "news":
            prev = ctx.prev_session(d)
            if prev is not None:
                since = prev + pd.Timedelta(hours=16)
                until = pd.Timestamp(d) + pd.Timedelta(minutes=GRID_START + 1 + ai)
                nc = set(ctx.news(since, until)["code"])
                cand = [c for c in cand if c[1] in nc]
        cand.sort(key=lambda x: -x[0])
        for move, code, row, px, i in cand[: int(P["top_k"])]:
            qty = int(P["max_value"] / px)
            qty = min(qty, int(0.05 * row["adv_turnover"] / px))
            qty = cap_by_close(qty, row, P["close_cap"])
            if qty <= 0:
                continue
            fill = sim.work_fill(p, i, ai + sim.LATENCY, qty, ai + sim.LATENCY + 5, px * 1.01)
            if fill is None:
                continue
            stop = fill.px * (1 - P["stop_pct"])
            t = _mk(code, "buy", d, fill, stop, ctx.costs, row, move=round(move, 4))
            sim.manage_and_exit(p, i, t, ctx.costs, row["adv_turnover"], row["vol20"],
                                stop=stop, next_open=_f(row.get("next_open")))  # fmt: skip
            book.append(t)
        out.append(book)
    return out


REVERSAL_DEFAULTS = {
    "drop_min": 0.04,  # down at least this much against the index at 16:00
    "no_news": True,  # no announcement at all since the prior close
    "hold": 1,  # sessions; out in that session's closing auction (entry "close0")
    "max_positions": 5,
    "per_position": 4000.0,
    "min_turnover": 2_000_000.0,
    "stop_pct": None,
    "regime": None,
    "close_cap": 0.2,
    "entry": "close0",
}


def reversal(ctx: Ctx, days: list[date], P: dict) -> list:
    """Short-term reversal: a liquid stock's big no-news fall tends to partly come back."""
    P = {**REVERSAL_DEFAULTS, **P}
    dayset = [pd.Timestamp(x) for x in days]
    signals = {}
    for d in dayset:
        rows = ctx.by_day.get(d)
        ixr = _index_ret(ctx, d)
        if rows is None or ixr is None or not regime_ok(ctx, d.date(), 1, P["regime"]):
            continue
        r = rows[rows["adv_turnover"] >= P["min_turnover"]]
        mv = r["last_cont_close"] / r["prev_close"] - 1 - ixr
        r = r[mv <= -P["drop_min"]]
        if P["no_news"] and len(r):
            prev = ctx.prev_session(d.date())
            since = (prev + pd.Timedelta(hours=16)) if prev is not None else d
            nc = set(ctx.news(since, d + pd.Timedelta(hours=16), ps_only=False)["code"])
            r = r[~r.index.isin(nc)]
        if len(r):
            order = (r["last_cont_close"] / r["prev_close"]).sort_values().index
            signals[d] = [(c, r.loc[c], "no_news_drop") for c in order]
    return _hold_book(ctx, dayset, signals, {**P, "entry": P["entry"]}, tag="reversal")


BREAKOUT_DEFAULTS = {
    "vol_mult": 2.0,  # the day's volume against its 20-session median
    "hold": 5,
    "max_positions": 5,
    "per_position": 4000.0,
    "min_turnover": 2_000_000.0,
    "stop_pct": 0.08,
    "regime": None,
    "close_cap": 0.2,
    "entry": "close0",
}


def breakout20(ctx: Ctx, days: list[date], P: dict) -> list:
    """A close (at 16:00, before the auction) above the prior 20-session high on heavy volume:
    momentum held for days."""
    P = {**BREAKOUT_DEFAULTS, **P}
    dayset = [pd.Timestamp(x) for x in days]
    signals = {}
    for d in dayset:
        rows = ctx.by_day.get(d)
        if rows is None or not regime_ok(ctx, d.date(), 1, P["regime"]):
            continue
        r = rows[(rows["adv_turnover"] >= P["min_turnover"]) & rows["high20"].notna()]
        vol_by_16 = r["volume"] - r["auction_vol"]  # the auction is not known at 16:00
        r = r[(r["last_cont_close"] > r["high20"]) & (vol_by_16 >= P["vol_mult"] * r["vol_med20"])]
        if len(r):
            signals[d] = [(c, r.loc[c], "breakout20") for c in r.index]
    return _hold_book(ctx, dayset, signals, P, tag="breakout20")


DAY2_DEFAULTS = {
    **ORB_DEFAULTS,
    "react_min": 0.05,  # yesterday's news reaction against the index, at 16:00
    "buckets": None,  # None: any price-sensitive announcement
    "catalyst": "none",
    "min_rvol": 0.0,
}


def day2_orb(ctx: Ctx, days: list[date], P: dict) -> list:
    """Day after a big price-sensitive reaction: the opening-range break in its direction."""
    P = {**DAY2_DEFAULTS, **P}
    out = []
    for d in days:
        prev = ctx.prev_session(d)
        rows = ctx.rows(d)
        if prev is None or rows is None:
            out.append(None)
            continue
        pr = ctx.by_day[prev]
        ixr = _index_ret(ctx, prev)
        news = ctx.ann[(ctx.ann["day0"] == prev) & ctx.ann["price_sensitive"]] if len(
            ctx.ann) else ctx.ann  # fmt: skip
        if P["buckets"]:
            news = news[news["bucket"].isin(P["buckets"])]
        codes = [c for c in set(news["code"]) if c in pr.index]
        good = set()
        if ixr is not None:
            for c in codes:
                x = pr.loc[c]
                if np.isfinite(x["prev_close"]) and np.isfinite(x["last_cont_close"]):
                    if x["last_cont_close"] / x["prev_close"] - 1 - ixr >= P["react_min"]:
                        good.add(c)
        sub = ctx.by_day[pd.Timestamp(d)]
        ctx.by_day[pd.Timestamp(d)] = sub[sub.index.isin(good)]
        try:
            out += orb_inplay(ctx, [d], {k: v for k, v in P.items() if k in ORB_DEFAULTS})
        finally:
            ctx.by_day[pd.Timestamp(d)] = sub
    return out


def _f(x) -> float | None:
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if np.isfinite(x) and x > 0 else None


FAMILIES = {
    "orb_inplay": (orb_inplay, ORB_DEFAULTS, "intraday"),
    "drift": (drift, DRIFT_DEFAULTS, "multiday"),
    "gap_fade": (gap_fade, GAP_FADE_DEFAULTS, "intraday"),
    "late_trend": (late_trend, LATE_TREND_DEFAULTS, "intraday"),
    "reversal": (reversal, REVERSAL_DEFAULTS, "multiday"),
    "breakout20": (breakout20, BREAKOUT_DEFAULTS, "multiday"),
    "day2_orb": (day2_orb, DAY2_DEFAULTS, "intraday"),
}

__all__ = ["Ctx", "FAMILIES", "START_CASH"]
