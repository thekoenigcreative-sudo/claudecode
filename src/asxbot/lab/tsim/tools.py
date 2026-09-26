"""What a trader sees and can do inside the simulation (CLOUD_BRIEF 3): a scanner (movers,
gaps, unusual volume, news), any stock's chart and daily history up to now, a quote with a
modelled bid/ask, the announcement feed, its own account, orders and journal - and its orders.

Everything goes through `TraderView`, which (a) shows only what existed at the simulated now
(market.py) and (b) disguises it when the run is before the models' knowledge cutoff (anon.py),
translating the trader's codes, prices and share counts back before the broker sees them.
Nothing is pre-selected by rules: the scanner lists what the trader asks it for.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from asxbot.announcements.model import classify_headline
from asxbot.lab.tsim.broker import OrderRejected, SimBroker
from asxbot.lab.tsim.market import INDEX, SLOTS, Market, slot_time

SCAN_KINDS = ("gainers", "losers", "gaps_up", "gaps_down", "rvol", "opening_rvol", "turnover",
              "news", "range")  # fmt: skip


class TraderView:
    def __init__(self, market: Market, broker: SimBroker, anon, journal=None, reader=None):
        self.m = market
        self.b = broker
        self.anon = anon
        self.journal = journal  # tsim.journal.Journal or None
        self.reader = reader  # callable(announcement row) -> dict | None (post-cutoff only)
        self._usual: dict[str, np.ndarray | None] = {}

    # ------------------------------------------------------------ helpers
    @property
    def now(self) -> datetime:
        return self.m.now

    def when(self) -> str:
        return f"{self.anon.date(self.m.day)} {self.m.now:%H:%M:%S}"

    def _code(self, shown: str) -> str | None:
        c = self.anon.real(shown)
        return c if c and (c in self.m.bars or c == INDEX or self.m.prev_close(c)) else None

    def usual_cum(self, code: str) -> np.ndarray | None:
        """The usual cumulative volume through each slot (14-day average)."""
        if code not in self._usual:
            s = self.m.summaries
            if s is None:
                self._usual[code] = None
            else:
                from asxbot.lab.tsim.summaries import CHECKPOINTS

                t = s.before(code, self.m.day, 14)
                if t is None or len(t) < 5:
                    self._usual[code] = None
                else:
                    pts = [0, *CHECKPOINTS]
                    vals = [0.0, *[float(t[f"cv{k}"].mean()) for k in CHECKPOINTS]]
                    self._usual[code] = np.interp(np.arange(1, SLOTS + 1), pts, vals)
        return self._usual[code]

    def rvol(self, code: str) -> float | None:
        n = self.m.n_visible
        b = self.m.bars.get(code)
        u = self.usual_cum(code)
        if b is None or u is None or n <= 0 or u[n - 1] <= 0:
            return None
        return float(b.v[:n].sum() / u[n - 1])

    def opening_rvol(self, code: str, window: int = 15) -> float | None:
        """Volume in the first `window` minutes after the stock's own opening auction vs its
        14-day average for the same window; None until the window is complete."""
        b = self.m.bars.get(code)
        if b is None or self.m.summaries is None:
            return None
        end = b.open_slot + 1 + window
        if self.m.n_visible < end:
            return None
        usual = self.m.summaries.usual_open_window(code, self.m.day, window)
        if not usual:
            return None
        return float(b.v[b.open_slot + 1 : end].sum() / usual)

    def change_pct(self, code: str) -> float | None:
        last, prev = self.m.last(code), self.m.prev_close(code)
        return None if not last or not prev else (last / prev - 1) * 100

    def gap_pct(self, code: str) -> float | None:
        b = self.m.bars.get(code)
        prev = self.m.prev_close(code)
        if b is None or not prev or b.auction_price is None or self.m.n_visible <= b.open_slot:
            return None
        return (b.auction_price / prev - 1) * 100

    def news_codes(self) -> dict[str, int]:
        """Codes with a price-sensitive announcement since the last close (visible now)."""
        from datetime import time as t_

        from asxbot.arena.intraday import prior_sessions

        prev = prior_sessions(self.m.day, 1)
        since = datetime.combine(prev[0] if prev else self.m.day, t_(16, 10), tzinfo=self.m.now.tzinfo)
        a = self.m.visible_announcements(since)
        if not len(a):
            return {}
        a = a[a["price_sensitive"].fillna(False).astype(bool)]
        return a.groupby(a["code"].str.upper()).size().to_dict()

    # ------------------------------------------------------------ the scanner
    def scan(self, kind: str = "gainers", n: int = 15, min_turnover: float = 0.0) -> list[dict]:
        if kind not in SCAN_KINDS:
            raise ValueError(f"scanner kinds: {', '.join(SCAN_KINDS)}")
        news = self.news_codes()
        rows = []
        for code in self.m.codes():
            to = self.m.turnover(code) or 0.0
            if to < min_turnover:
                continue
            chg = self.change_pct(code)
            if kind in ("gainers", "losers", "range") and chg is None:
                continue
            if kind == "gainers":
                key = chg
            elif kind == "losers":
                key = -chg
            elif kind in ("gaps_up", "gaps_down"):
                g = self.gap_pct(code)
                if g is None:
                    continue
                key = g if kind == "gaps_up" else -g
            elif kind == "rvol":
                key = self.rvol(code)
            elif kind == "opening_rvol":
                key = self.opening_rvol(code)
            elif kind == "turnover":
                b = self.m.bars[code]
                nv = self.m.n_visible
                key = float(np.nansum(b.c[:nv] * b.v[:nv])) if nv else None
            elif kind == "news":
                key = news.get(code)
            else:  # range: today's high-low as % of the previous close
                b = self.m.bars[code]
                nv = self.m.n_visible
                prev = self.m.prev_close(code)
                if not nv or not prev or not (b.v[:nv] > 0).any():
                    continue
                key = (np.nanmax(b.h[:nv]) - np.nanmin(b.l[:nv])) / prev * 100
            if key is None or key != key:
                continue
            rows.append((key, code))
        rows.sort(reverse=True)
        return [self.quote(c, brief=True) for _, c in rows[: max(1, min(int(n), 50))]]

    # ------------------------------------------------------------ one stock
    def quote(self, code: str, brief: bool = False) -> dict:
        a = self.anon
        b = self.m.bars.get(code)
        nv = self.m.n_visible
        last = self.m.last(code)
        prev = self.m.prev_close(code)
        to = self.m.turnover(code)
        half = self.b.costs.half_spread(last, to) if last else None
        d = {
            "code": a.alias(code),
            "last": a.price(code, last),
            "chg_pct": _r(self.change_pct(code)),
            "gap_pct": _r(self.gap_pct(code)),
            "rvol": _r(self.rvol(code)),
            "news_today": self.news_codes().get(code, 0),
        }
        if brief:
            return d
        vol = float(b.v[:nv].sum()) if b is not None and nv else 0.0
        trad = b is not None and nv and (b.v[:nv] > 0).any()
        d.update({
            "prev_close": a.price(code, prev),
            "open": a.price(code, b.auction_price) if b is not None and nv > b.open_slot else None,
            "high": a.price(code, float(np.nanmax(b.h[:nv]))) if trad else None,
            "low": a.price(code, float(np.nanmin(b.l[:nv]))) if trad else None,
            "volume": a.volume(code, vol),
            "vwap": a.price(code, _vwap(b, nv)) if trad else None,
            "bid_est": a.price(code, last - half) if half else None,
            "ask_est": a.price(code, last + half) if half else None,
            "spread_note": "estimated from price and liquidity tier (bars carry no bid/ask)",
            "opening_rvol_15m": _r(self.opening_rvol(code)),
            "median_daily_turnover_aud": round(to) if to else None,
            "shortable": code in self.b.shortable,
            "opens_at": slot_time(self.m.day, b.open_slot).strftime("%H:%M") if b is not None
            else None,
        })  # fmt: skip
        return d

    def chart(self, code: str, minutes: int = 60, bar: int = 1) -> dict:
        """Today's bars up to now (the last `minutes`), resampled to `bar` minutes."""
        a = self.anon
        b = self.m.bars.get(code)
        nv = self.m.n_visible
        if b is None or nv == 0:
            return {"code": a.alias(code), "bars": [], "note": "no trades yet today"}
        df = b.frame(self.m.day, nv)
        if bar > 1 and len(df):
            df = df.resample(f"{int(bar)}min", label="left", closed="left").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
            ).dropna()
        df = df[df.index >= df.index[-1] - pd.Timedelta(minutes=max(1, int(minutes)))] if len(
            df) else df  # fmt: skip
        rows = [
            [t.strftime("%H:%M"), a.price(code, r.open), a.price(code, r.high),
             a.price(code, r.low), a.price(code, r.close), a.volume(code, r.volume)]
            for t, r in df.iterrows()
        ]  # fmt: skip
        return {"code": a.alias(code), "cols": ["time", "o", "h", "l", "c", "v"], "bars": rows[-120:]}

    def daily(self, code: str, n: int = 20) -> dict:
        a = self.anon
        s = self.m.summaries.before(code, self.m.day, max(1, min(int(n), 120))) if (
            self.m.summaries) else None  # fmt: skip
        if s is None or not len(s):
            return {"code": a.alias(code), "days": []}
        rows = []
        for i, r in enumerate(s.itertuples()):
            rows.append([f"-{len(s) - i}", a.price(code, r.open), a.price(code, r.high),
                         a.price(code, r.low), a.price(code, r.close), a.volume(code, r.volume)])
        return {"code": a.alias(code), "cols": ["sessions_ago", "o", "h", "l", "c", "v"],
                "days": rows}  # fmt: skip

    def index(self) -> dict:
        m = self.m
        if m.index is None:
            return {"note": "no index bars today"}
        nv = m.n_visible
        df = m.index.frame(m.day, nv)
        df = df.resample("15min").agg({"close": "last"}).dropna() if len(df) else df
        return {
            "move_pct": _r(m.index_move_pct()),
            "closes_15m": [[t.strftime("%H:%M"), round(float(c) * self.anon.factor(INDEX), 1)]
                           for t, c in df["close"].items()],
        }  # fmt: skip

    # ------------------------------------------------------------ news
    def news(self, code: str | None = None, sensitive_only: bool = False, n: int = 25,
             since: datetime | None = None) -> list[dict]:  # fmt: skip
        a = self.anon
        df = self.m.visible_announcements(since)
        if not len(df):
            return []
        if code:
            df = df[df["code"].str.upper() == code]
        if sensitive_only:
            df = df[df["price_sensitive"].fillna(False).astype(bool)]
        out = []
        for r in df.tail(max(1, min(int(n), 60))).itertuples():
            ts = pd.Timestamp(r.released_at)
            when = ("today " if ts.date() == self.m.day else "earlier ") + ts.strftime("%H:%M")
            out.append({
                "id": str(r.ids_id), "code": a.alias(r.code), "when": when,
                "sensitive": bool(r.price_sensitive), "headline": a.headline(r.headline),
                "type": classify_headline(r.headline),
            })  # fmt: skip
        return out

    def read(self, ann_id: str) -> dict:
        df = self.m.visible_announcements()
        row = df[df["ids_id"].astype(str) == str(ann_id)] if len(df) else df
        if not len(row):
            return {"error": "no such announcement visible now"}
        r = row.iloc[-1]
        base = {"id": str(ann_id), "code": self.anon.alias(r["code"]),
                "headline": self.anon.headline(r["headline"]),
                "type": classify_headline(r["headline"])}  # fmt: skip
        summary = self.reader(r) if self.reader is not None else None
        if summary:
            base["reader"] = summary
        else:
            base["note"] = "only the headline is available in the simulation for this item"
        return base

    # ------------------------------------------------------------ the account
    def account(self) -> dict:
        a, acct = self.anon, self.b.acct
        pos = []
        for code, p in acct.positions.items():
            last = self.m.last(code)
            pos.append({
                "code": a.alias(code), "qty": a.shares_to_shown(code, p.qty),
                "avg": a.price(code, p.avg), "last": a.price(code, last),
                "unrealised": round((last - p.avg) * p.qty, 2) if last else None,
                "value": round(abs(p.qty) * (last or p.avg), 2), "opened": p.opened_at[:16],
            })  # fmt: skip
        today = self.m.day.isoformat()
        closed = [t for t in acct.trades if t.closed_at[:10] == today]
        return {
            "cash": round(acct.cash, 2),
            "equity": round(acct.equity(self.m.last), 2),
            "start_of_run": acct.start_cash,
            "buying_power_note": f"gross positions + working orders <= equity x {acct.leverage:g}",
            "positions": pos,
            "working_orders": [self.order_view(o) for o in acct.working()],
            "closed_today": [{"code": a.alias(t.code), "direction": t.direction,
                              "net": round(t.net, 2)} for t in closed],  # fmt: skip
            "fees_paid_run": round(acct.fees, 2),
        }

    def order_view(self, o) -> dict:
        a = self.anon
        d = {"id": o.id, "code": a.alias(o.code), "side": o.side, "type": o.type,
             "qty": a.shares_to_shown(o.code, o.qty), "filled": a.shares_to_shown(o.code, o.filled),
             "tif": o.tif, "status": o.status}  # fmt: skip
        for k in ("limit", "stop"):
            if getattr(o, k):
                d[k] = a.price(o.code, getattr(o, k))
        if o.trail_pct:
            d["trail_pct"] = o.trail_pct
        if o.avg_price:
            d["avg_price"] = a.price(o.code, o.avg_price)
        if o.reduce_only:
            d["reduce_only"] = True
        return d

    # ------------------------------------------------------------ orders
    def do(self, action: dict, at: datetime, by: str) -> dict:
        """One order action from the trader, in its own (possibly disguised) terms."""
        op = str(action.get("op", "place")).lower()
        a = self.anon
        try:
            if op == "cancel":
                o = self.b.cancel(str(action["id"]), at)
                return {"ok": True, "id": o.id, "status": o.status}
            if op == "modify":
                o = self.b.acct.orders.get(str(action.get("id")))
                if o is None:
                    raise OrderRejected(f"no order {action.get('id')}")
                ch = {k: a.unprice(o.code, action[k]) for k in ("limit", "stop") if
                      action.get(k) is not None}  # fmt: skip
                if action.get("trail_pct") is not None:
                    ch["trail_pct"] = float(action["trail_pct"])
                if action.get("qty") is not None:
                    ch["qty"] = a.shares_to_real(o.code, int(action["qty"]))
                o = self.b.modify(o.id, at, **ch)
                return {"ok": True, "id": o.id, "status": o.status}
            code = self._code(str(action.get("code", "")))
            if code is None:
                raise OrderRejected(f"unknown code {action.get('code')!r}")
            typ = str(action.get("type", "market")).lower()
            price_ref = self.m.last(code)
            if action.get("qty") is not None:
                qty = a.shares_to_real(code, int(action["qty"]))
            elif action.get("value_aud") is not None:
                ref = a.unprice(code, action.get("limit")) or price_ref
                qty = int(float(action["value_aud"]) // ref) if ref else 0
            else:
                raise OrderRejected("give qty (shares) or value_aud")
            att = {}
            for k in ("stop", "target"):
                if action.get("attach", {}).get(k) is not None:
                    att[k] = a.unprice(code, action["attach"][k])
            if (action.get("attach") or {}).get("trail_pct") is not None:
                att["trail_pct"] = float(action["attach"]["trail_pct"])
            if (action.get("attach") or {}).get("tif"):
                att["tif"] = str(action["attach"]["tif"])
            o = self.b.place(
                code, str(action.get("side", "buy")), qty, typ, at=at,
                limit=a.unprice(code, action.get("limit")), stop=a.unprice(code, action.get("stop")),
                trail_pct=action.get("trail_pct"), tif=str(action.get("tif", "day")),
                attach=att, by=by, why=str(action.get("why", "")),
            )  # fmt: skip
            return {"ok": True, "id": o.id, "code": a.alias(code),
                    "qty": a.shares_to_shown(code, o.qty), "status": o.status}  # fmt: skip
        except (OrderRejected, KeyError, ValueError, TypeError) as e:
            return {"ok": False, "error": a.text(str(e))[:300], "action": action}


def _r(x, n: int = 2):
    return None if x is None or x != x else round(float(x), n)


def _vwap(b, nv) -> float | None:
    v = b.v[:nv]
    if v.sum() <= 0:
        return None
    tp = (np.nan_to_num(b.h[:nv]) + np.nan_to_num(b.l[:nv]) + np.nan_to_num(b.c[:nv])) / 3
    return float((tp * v).sum() / v.sum())
