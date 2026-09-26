"""Rules-only traders: plain code, no model calls, on the same market, broker and costs as the AI
trader. They are the yardsticks (CLOUD_BRIEF A, C-rules, D) and the strategy search's cheap
candidates. Each is a FAMILY with parameters; a strategy is {"family": ..., "params": {...}}.

Families
  orb      stocks in play, opening-range breakout (yardstick A and its D variants)
  drift    announcement drift held for days, by headline type (plain-code classifier)
  gap_fade fade an opening gap without news back toward the previous close
  vwap_rev buy a stretched intraday drop below VWAP in a liquid stock, exit at VWAP / close
  hod_mom  momentum: a new high of day on heavy relative volume after the first hour
Every family: sized by risk (a share of equity per trade, the stop distance), a cost filter
(skip if the expected move is under k x the round trip's cost), optional regime filter
(index direction), optional cap at a share of the stock's usual closing-auction volume, long
only unless the stock is on the borrowable list, and flat at the close unless it holds for days.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time

import numpy as np

from asxbot.announcements.model import classify_headline
from asxbot.lab.tsim.engine import Decision, Trader
from asxbot.lab.tsim.market import CONTINUOUS_END_SLOT, slot_of, slot_time
from asxbot.lab.tsim.tools import TraderView

COMMON = {
    "risk_pct": 1.0,  # of equity per trade, stop distance x shares
    "max_trades": 3,  # per day
    "cost_multiple": 3.0,  # skip if expected move < this x round-trip cost
    "regime": "none",  # none | index_day (index up for longs, down for shorts) | first30
    "close_volume_cap": 0.0,  # 0 = off; else max share of the usual closing-auction volume
    "allow_short": True,
    "last_entry": "11:30",
    "exit": "close",  # close (the closing auction) | trail
    "trail_pct": 0.0,  # with exit=trail: a trailing stop this % from the best price
}


@dataclass
class Plan:
    code: str
    side: str  # buy | short
    entry: float | None  # a stop-entry level; None = market
    stop: float
    reason: str
    target: float | None = None


class RuleTrader(Trader):
    every_minute = True
    kind = "rules"
    family = "base"
    defaults: dict = {}

    def __init__(self, params: dict | None = None, name: str | None = None):
        self.p = {**COMMON, **self.defaults, **(params or {})}
        self.name = name or f"rules:{self.family}"
        self.taken: set[str] = set()
        self.state: dict = {}

    # ---------------------------------------------------------------- helpers
    def equity(self, v: TraderView) -> float:
        return v.b.acct.equity(v.m.last)

    def atr_pct(self, v: TraderView, code: str, n: int = 14) -> float | None:
        s = v.m.summaries.before(code, v.m.day, n) if v.m.summaries else None
        if s is None or len(s) < 5:
            return None
        return float(((s["high"] - s["low"]) / s["close"]).mean() * 100)

    def round_trip_cost(self, v: TraderView, code: str, price: float, qty: int) -> float:
        """Dollars: two half-spreads, two commissions (the primary broker)."""
        to = v.m.turnover(code)
        half = v.b.costs.half_spread(price, to)
        return 2 * half * qty + 2 * v.b.costs.broker.commission(price * qty)

    def regime_ok(self, v: TraderView, side: str) -> bool:
        r = self.p["regime"]
        if r == "none":
            return True
        m = v.m
        if m.index is None:
            return False
        if m.n_visible == 0:
            # before the open (drift enters in the opening auction): the index's trend -
            # yesterday's close against its 10-day average
            s = m.summaries.before("^AXJO", m.day, 10) if m.summaries is not None else None
            if s is None or len(s) < 5:
                return False
            up = float(s["close"].iloc[-1]) > float(s["close"].mean())
            return up if side == "buy" else not up
        if r == "index_day":
            mv = m.index_move_pct()
            return mv is not None and ((mv > 0) if side == "buy" else (mv < 0))
        if r == "first30":
            nv = m.n_visible
            if nv < 30:
                return False
            idx = m.index
            first = np.flatnonzero(idx.v[:30] > 0)
            if not len(first):
                return False
            o, c = idx.o[first[0]], idx.c[first[-1]]
            return (c > o) if side == "buy" else (c < o)
        return True

    def size(self, v: TraderView, plan: Plan) -> int:
        ref = plan.entry or v.m.last(plan.code)
        if not ref:
            return 0
        risk_ps = abs(ref - plan.stop)
        if risk_ps <= 0:
            return 0
        qty = math.floor(self.equity(v) * self.p["risk_pct"] / 100 / risk_ps)
        # what the account can carry at 1x its leverage, less what is already out
        acct = v.b.acct
        room = (
            acct.equity(v.m.last) * acct.leverage
            - acct.gross(v.m.last)
            - (v.b.working_open_value())
        )
        room -= getattr(self, "_reserved", 0.0)  # entries already sized in this same call
        qty = min(qty, math.floor(max(0.0, room) * 0.98 / ref))
        cap = self.p["close_volume_cap"]
        if cap and v.m.summaries is not None:
            s = v.m.summaries.before(plan.code, v.m.day, 14)
            if s is not None and len(s):
                qty = min(qty, math.floor(cap * float(s["close_auction_volume"].median())))
        return max(0, qty)

    def economic(self, v: TraderView, plan: Plan, qty: int) -> bool:
        ref = plan.entry or v.m.last(plan.code)
        atr = self.atr_pct(v, plan.code)
        if not ref or atr is None or qty <= 0:
            return False
        expected = ref * atr / 100 * qty
        return expected >= self.p["cost_multiple"] * self.round_trip_cost(v, plan.code, ref, qty)

    def enter(self, v: TraderView, plan: Plan) -> dict | None:
        if plan.side == "short" and (not self.p["allow_short"] or plan.code not in v.b.shortable):
            return None
        if not self.regime_ok(v, "buy" if plan.side == "buy" else "short"):
            return None
        qty = self.size(v, plan)
        if not self.economic(v, plan, qty):
            return None
        self._reserved = getattr(self, "_reserved", 0.0) + qty * (plan.entry or v.m.last(plan.code))
        self.taken.add(plan.code)
        attach = {"stop": plan.stop, "tif": "day"}
        if self.p["exit"] == "trail" and self.p["trail_pct"]:
            attach["trail_pct"] = self.p["trail_pct"]
        if plan.target:
            attach["target"] = plan.target
        a = {"op": "place", "code": plan.code, "side": plan.side, "qty": qty,
             "type": "stop" if plan.entry else "market", "stop": plan.entry,
             "tif": "day", "attach": attach, "why": plan.reason}  # fmt: skip
        return a

    def flatten_at_close(self, v: TraderView, now_slot: int) -> list[dict]:
        """At 15:58 cancel entries and send every intraday position to the closing auction."""
        if now_slot != CONTINUOUS_END_SLOT - 2 or self.p.get("hold_days"):
            return []
        out = [{"op": "cancel", "id": o.id} for o in v.b.acct.working() if not o.reduce_only]
        for code, p in v.b.acct.positions.items():
            for o in v.b.acct.working(code):
                if o.reduce_only and o.type == "limit":
                    out.append({"op": "cancel", "id": o.id})
            out.append(
                {
                    "op": "place",
                    "code": code,
                    "side": "sell" if p.qty > 0 else "cover",
                    "qty": abs(p.qty),
                    "type": "moc",
                    "tif": "day",
                    "why": "flat at close",
                }
            )
        return out

    def past_last_entry(self, v: TraderView) -> bool:
        hh, mm = self.p["last_entry"].split(":")
        return v.m.now.time() >= time(int(hh), int(mm))

    def wake(self, v, reasons, ctx):
        self._reserved = 0.0
        slot = ctx.get("slot", 0)
        acts = self.flatten_at_close(v, slot)
        if not acts:
            acts = self.on_bar(v, slot) or []
        return Decision(actions=acts) if acts else None

    def pre_open(self, v, ctx):
        self.taken = set()
        self.state = {k: val for k, val in self.state.items() if k.startswith("keep_")}
        return None

    def on_bar(self, v: TraderView, slot: int) -> list[dict]:
        return []


class ORB(RuleTrader):
    """Yardstick A: stocks in play, opening-range breakout."""

    family = "orb"
    defaults = {"window": 15, "min_gap_pct": 2.0, "need_catalyst": True, "min_open_rvol": 0.0,
                "rank_at": "10:25", "min_turnover_aud": 500_000.0}  # fmt: skip

    def on_bar(self, v, slot):
        p = self.p
        acts = []
        if "picked" not in self.state:
            hh, mm = p["rank_at"].split(":")
            if v.m.now.time() < time(int(hh), int(mm)):
                return []
            if p["regime"] == "first30" and v.m.n_visible < 31:
                return []  # the first 30 minutes' direction is not known before 10:30
            news = v.news_codes()
            cands = []
            for code in v.m.bars:
                if (v.m.turnover(code) or 0) < p["min_turnover_aud"]:
                    continue
                gap = v.gap_pct(code)
                catalyst = code in news or (gap is not None and abs(gap) >= p["min_gap_pct"])
                if p["need_catalyst"] and not catalyst:
                    continue
                rv = v.opening_rvol(code, int(p["window"]))
                if rv is None or rv < p["min_open_rvol"]:
                    continue
                cands.append((rv, code))
            cands.sort(reverse=True)
            self.state["picked"] = [c for _, c in cands[: int(p["max_trades"])]]
            self.state["ranges"] = {}
            for code in self.state["picked"]:
                b = v.m.bars[code]
                w = slice(b.open_slot, b.open_slot + int(p["window"]))
                if not (b.v[w] > 0).any():
                    continue
                hi, lo = float(np.nanmax(b.h[w])), float(np.nanmin(b.l[w]))
                tr = np.flatnonzero(b.v[w] > 0)
                o, c = float(b.o[w][tr[0]]), float(b.c[w][tr[-1]])
                side = "buy" if c >= o else "short"
                self.state["ranges"][code] = (hi, lo, side)
                from asxbot.lab.tsim.costs import tick_size

                t = tick_size(hi)
                plan = (
                    Plan(code, "buy", hi + t, lo, f"ORB long: range {lo:.3f}-{hi:.3f}")
                    if side == "buy"
                    else Plan(code, "short", lo - t, hi, f"ORB short: range {lo:.3f}-{hi:.3f}")
                )
                a = self.enter(v, plan)
                if a:
                    acts.append(a)
            return acts
        if self.past_last_entry(v) and not self.state.get("expired"):
            self.state["expired"] = True
            return [
                {"op": "cancel", "id": o.id}
                for o in v.b.acct.working()
                if not o.reduce_only and o.type == "stop" and o.filled == 0
            ]
        return []


class GapFade(RuleTrader):
    """Fade a no-news opening gap toward the previous close."""

    family = "gap_fade"
    defaults = {"min_gap_pct": 3.0, "max_gap_pct": 12.0, "stop_mult": 0.5, "enter_after": 5,
                "min_turnover_aud": 1_000_000.0}  # fmt: skip

    def on_bar(self, v, slot):
        if self.state.get("done") or self.past_last_entry(v):
            return []
        news = v.news_codes()
        acts = []
        ready = False
        for code, b in v.m.bars.items():
            if slot != b.open_slot + int(self.p["enter_after"]):
                continue
            ready = True
            if code in news or code in self.taken or (v.m.turnover(code) or 0) < self.p[
                    "min_turnover_aud"]:  # fmt: skip
                continue
            gap = v.gap_pct(code)
            prev = v.m.prev_close(code)
            last = v.m.last(code)
            if gap is None or not prev or not last or not (
                    self.p["min_gap_pct"] <= abs(gap) <= self.p["max_gap_pct"]):  # fmt: skip
                continue
            dist = abs(last - prev)
            if gap > 0:
                plan = Plan(
                    code,
                    "short",
                    None,
                    last + dist * self.p["stop_mult"],
                    f"fade gap up {gap:.1f}%",
                    target=prev,
                )
            else:
                plan = Plan(
                    code,
                    "buy",
                    None,
                    last - dist * self.p["stop_mult"],
                    f"fade gap down {gap:.1f}%",
                    target=prev,
                )
            if len(self.taken) >= self.p["max_trades"]:
                break
            a = self.enter(v, plan)
            if a:
                acts.append(a)
        if ready and slot > 12:
            self.state["done"] = True
        return acts


class VwapRev(RuleTrader):
    """A liquid stock stretched well below VWAP with no news: buy, target VWAP."""

    family = "vwap_rev"
    defaults = {"stretch_pct": 2.5, "stop_pct": 1.5, "from": "10:30", "last_entry": "15:00",
                "min_turnover_aud": 5_000_000.0}  # fmt: skip

    def on_bar(self, v, slot):
        hh, mm = self.p["from"].split(":")
        if v.m.now.time() < time(int(hh), int(mm)) or self.past_last_entry(v):
            return []
        if len(self.taken) >= self.p["max_trades"]:
            return []
        news = v.news_codes()
        from asxbot.lab.tsim.tools import _vwap

        acts = []
        for code, b in v.m.bars.items():
            if code in self.taken or code in news or b.v[slot] <= 0:
                continue
            if (v.m.turnover(code) or 0) < self.p["min_turnover_aud"]:
                continue
            vw = _vwap(b, slot + 1)
            last = float(b.c[slot])
            if not vw or (vw - last) / vw * 100 < self.p["stretch_pct"]:
                continue
            plan = Plan(
                code,
                "buy",
                None,
                last * (1 - self.p["stop_pct"] / 100),
                f"{(vw - last) / vw * 100:.1f}% below VWAP",
                target=vw,
            )
            a = self.enter(v, plan)
            if a:
                acts.append(a)
            if len(self.taken) >= self.p["max_trades"]:
                break
        return acts


class HodMomentum(RuleTrader):
    """After the first hour: a new high of day on heavy relative volume; stop under the
    session's last pullback low (the low of the last `lookback` minutes)."""

    family = "hod_mom"
    defaults = {"from": "11:00", "min_rvol": 2.0, "min_chg_pct": 2.0, "lookback": 30,
                "last_entry": "14:30", "min_turnover_aud": 2_000_000.0}  # fmt: skip

    def on_bar(self, v, slot):
        hh, mm = self.p["from"].split(":")
        if v.m.now.time() < time(int(hh), int(mm)) or self.past_last_entry(v):
            return []
        if len(self.taken) >= self.p["max_trades"]:
            return []
        acts = []
        for code, b in v.m.bars.items():
            if code in self.taken or b.v[slot] <= 0:
                continue
            if (v.m.turnover(code) or 0) < self.p["min_turnover_aud"]:
                continue
            prior = b.h[:slot]
            if not np.isfinite(prior).any() or b.h[slot] <= np.nanmax(prior):
                continue
            chg = v.change_pct(code)
            rv = v.rvol(code)
            if chg is None or rv is None or chg < self.p["min_chg_pct"] or rv < self.p["min_rvol"]:
                continue
            lb = int(self.p["lookback"])
            low = float(np.nanmin(b.l[max(0, slot - lb) : slot + 1]))
            plan = Plan(code, "buy", None, low, f"new high of day, +{chg:.1f}%, rvol {rv:.1f}")
            a = self.enter(v, plan)
            if a:
                acts.append(a)
            if len(self.taken) >= self.p["max_trades"]:
                break
        return acts


class Drift(RuleTrader):
    """Announcement drift, rules version: a price-sensitive announcement of the chosen types
    that moved the stock at least `min_react_pct` on its day is bought (or shorted) at the
    next day's opening auction and held `hold_days` sessions, exiting at the closing auction."""

    family = "drift"
    every_minute = False
    defaults = {"types": ["results", "guidance", "contract", "acquisition", "exploration"],
                "min_react_pct": 4.0, "hold_days": 5, "stop_pct": 8.0, "direction": "with",
                "max_trades": 3, "min_turnover_aud": 500_000.0}  # fmt: skip

    def __init__(self, params=None, name=None):
        super().__init__(params, name)
        self.state = {"keep_queue": [], "keep_held": {}}

    def pre_open(self, v, ctx):
        self._reserved = 0.0
        acts = []
        held = self.state["keep_held"]
        # exits due today: sessions counted in `held`
        for code in list(held):
            held[code] += 1
            if held[code] >= int(self.p["hold_days"]):
                p = v.b.acct.positions.get(code)
                if p:
                    acts.append(
                        {
                            "op": "place",
                            "code": code,
                            "side": "sell" if p.qty > 0 else "cover",
                            "qty": abs(p.qty),
                            "type": "moc",
                            "why": f"drift exit after {held[code]} days",
                        }
                    )
                del held[code]
        for code, side, why in self.state["keep_queue"][: int(self.p["max_trades"])]:
            last = v.m.prev_close(code)
            if not last:
                continue
            stop = last * (1 - self.p["stop_pct"] / 100) if side == "buy" else last * (
                1 + self.p["stop_pct"] / 100)  # fmt: skip
            plan = Plan(code, side, None, stop, why)
            a = self.enter(v, plan)
            if a:
                a["attach"]["tif"] = "gtc"
                acts.append(a)
                held[code] = 0
        self.state["keep_queue"] = []
        return Decision(actions=acts) if acts else None

    def wake(self, v, reasons, ctx):
        return None

    def after_close(self, v, ctx):
        news = v.news(sensitive_only=True, n=60)
        q = []
        seen = set()
        for n in news:
            code = v.anon.real(n["code"])
            if not code or code in seen or code in self.state["keep_held"]:
                continue
            if n["type"] not in self.p["types"]:
                continue
            if (v.m.turnover(code) or 0) < self.p["min_turnover_aud"]:
                continue
            chg = v.change_pct(code)
            if chg is None or abs(chg) < self.p["min_react_pct"]:
                continue
            seen.add(code)
            up = chg > 0
            if self.p["direction"] == "against":
                up = not up
            q.append(
                (abs(chg), code, "buy" if up else "short", f"{n['type']} {chg:+.1f}% on the day")
            )
        q.sort(reverse=True)
        self.state["keep_queue"] = [(c, s, w) for _, c, s, w in q]
        return None


class CloseStrength(RuleTrader):
    """The overnight effect: stocks that close strong (near the high of the day, up on heavy
    volume) are bought in the closing auction and sold in the next opening auction - or, with
    direction "against", the strongest are shorted for the gap back. Stop: a stop-market from
    the open at `stop_pct`."""

    family = "close_strength"
    defaults = {"min_chg_pct": 3.0, "min_rvol": 2.0, "near_high_pct": 1.0, "direction": "with",
                "hold_days": 1, "stop_pct": 3.0, "max_trades": 3,
                "min_turnover_aud": 2_000_000.0,
                "pick": "strong"}  # strong | weak (I0088: weak no-news closers)  # fmt: skip

    def __init__(self, params=None, name=None):
        super().__init__(params, name)
        self.state = {"keep_held": {}}

    def pre_open(self, v, ctx):
        self._reserved = 0.0
        acts = []
        held = self.state["keep_held"]
        for code in list(held):
            held[code] += 1
            p = v.b.acct.positions.get(code)
            if p is None:
                del held[code]
                continue
            if held[code] >= int(self.p["hold_days"]):
                # the opening auction: a market order sent before 09:59 joins it
                acts.append({"op": "place", "code": code, "side": "sell" if p.qty > 0 else "cover",
                             "qty": abs(p.qty), "type": "market",
                             "why": "close_strength: out in the opening auction"})  # fmt: skip
                del held[code]
        return Decision(actions=acts) if acts else None

    def on_bar(self, v, slot):
        if slot != CONTINUOUS_END_SLOT - 3:  # 15:57: choose, send to the closing auction
            return []
        news = v.news_codes()
        cands = []
        for code, b in v.m.bars.items():
            if (v.m.turnover(code) or 0) < self.p["min_turnover_aud"] or not (b.v[:slot] > 0).any():
                continue
            chg, rv = v.change_pct(code), v.rvol(code)
            weak = self.p["pick"] == "weak"
            if chg is None or rv is None or rv < self.p["min_rvol"]:
                continue
            if (-chg if weak else chg) < self.p["min_chg_pct"] or (weak and code in news):
                continue
            last = float(b.c[slot]) if b.v[slot] > 0 else v.m.last(code)
            ext = (float(np.nanmin(b.l[: slot + 1])) if weak
                   else float(np.nanmax(b.h[: slot + 1])))  # fmt: skip
            if not last or abs(last - ext) / ext * 100 > self.p["near_high_pct"]:
                continue
            cands.append((rv, code, last, code in news))
        cands.sort(reverse=True)
        acts = []
        for _rv, code, last, _n in cands[: int(self.p["max_trades"])]:
            side = "buy" if self.p["direction"] == "with" else "short"
            stop = last * (1 - self.p["stop_pct"] / 100) if side == "buy" else last * (
                1 + self.p["stop_pct"] / 100)  # fmt: skip
            plan = Plan(
                code, side, None, stop, f"closed {self.p['pick']} ({v.change_pct(code):+.1f}%)"
            )
            a = self.enter(v, plan)
            if a:
                a["type"] = "moc"
                a["attach"] = {"stop": stop, "tif": "gtc"}
                acts.append(a)
                self.state["keep_held"][code] = 0
        return acts


class Pullback(RuleTrader):
    """The first pullback in a stock in play: up at least `min_chg_pct` on heavy relative volume,
    it dips to VWAP (within `band_pct`) and a bar closes back above the prior bar's high - buy,
    stop under the pullback's low, target the high of the day, flat at the close."""

    family = "pullback"
    defaults = {"from": "10:45", "last_entry": "14:30", "min_chg_pct": 3.0, "min_rvol": 2.0,
                "band_pct": 0.5, "lookback": 15, "min_turnover_aud": 2_000_000.0}  # fmt: skip

    def on_bar(self, v, slot):
        hh, mm = self.p["from"].split(":")
        if v.m.now.time() < time(int(hh), int(mm)) or self.past_last_entry(v):
            return []
        if len(self.taken) >= self.p["max_trades"] or slot < 2:
            return []
        from asxbot.lab.tsim.tools import _vwap

        acts = []
        for code, b in v.m.bars.items():
            if code in self.taken or b.v[slot] <= 0 or b.v[slot - 1] <= 0:
                continue
            if (v.m.turnover(code) or 0) < self.p["min_turnover_aud"]:
                continue
            chg, rv = v.change_pct(code), v.rvol(code)
            if chg is None or rv is None or chg < self.p["min_chg_pct"] or rv < self.p["min_rvol"]:
                continue
            vw = _vwap(b, slot + 1)
            lb = int(self.p["lookback"])
            low = float(np.nanmin(b.l[max(0, slot - lb) : slot + 1]))
            if not vw or low > vw * (1 + self.p["band_pct"] / 100):
                continue  # no dip to VWAP in the lookback
            if not (b.c[slot] > vw and b.c[slot] > b.h[slot - 1]):
                continue  # no turn back up yet
            hod = float(np.nanmax(b.h[: slot + 1]))
            if hod <= b.c[slot]:
                continue
            plan = Plan(code, "buy", None, low, f"pullback to VWAP in a +{chg:.1f}% stock",
                        target=hod)  # fmt: skip
            a = self.enter(v, plan)
            if a:
                acts.append(a)
            if len(self.taken) >= self.p["max_trades"]:
                break
        return acts


class IndexRevert(RuleTrader):
    """A liquid stock that has moved far from the index today with no news (the residual move,
    stock minus index, beyond `min_resid_pct`) is faded toward the index: long a laggard, short a
    leader where shortable. Stop at `stop_mult` x the residual beyond entry; target VWAP."""

    family = "index_revert"
    defaults = {"from": "11:00", "last_entry": "15:00", "min_resid_pct": 3.0, "stop_mult": 0.5,
                "min_turnover_aud": 10_000_000.0, "no_news": True,
                "direction": "revert"}  # revert | follow (I0081: WITH the divergence)  # fmt: skip

    def on_bar(self, v, slot):
        hh, mm = self.p["from"].split(":")
        if v.m.now.time() < time(int(hh), int(mm)) or self.past_last_entry(v):
            return []
        if len(self.taken) >= self.p["max_trades"]:
            return []
        idx = v.m.index_move_pct()
        if idx is None:
            return []
        news = v.news_codes() if self.p["no_news"] else {}
        from asxbot.lab.tsim.tools import _vwap

        acts = []
        for code, b in v.m.bars.items():
            if code in self.taken or code in news or b.v[slot] <= 0:
                continue
            if (v.m.turnover(code) or 0) < self.p["min_turnover_aud"]:
                continue
            chg = v.change_pct(code)
            if chg is None:
                continue
            resid = chg - idx
            if abs(resid) < self.p["min_resid_pct"]:
                continue
            last = float(b.c[slot])
            dist = last * abs(resid) / 100 * self.p["stop_mult"]
            vw = _vwap(b, slot + 1)
            revert = self.p["direction"] == "revert"
            if (resid < 0) == revert:
                plan = Plan(code, "buy", None, last - dist, f"{resid:+.1f}% vs the index",
                            target=vw if revert and vw and vw > last else None)  # fmt: skip
            else:
                plan = Plan(code, "short", None, last + dist, f"{resid:+.1f}% vs the index",
                            target=vw if revert and vw and vw < last else None)  # fmt: skip
            a = self.enter(v, plan)
            if a:
                acts.append(a)
            if len(self.taken) >= self.p["max_trades"]:
                break
        return acts


class IndexMomentumClose(RuleTrader):
    """Intraday momentum (I0085; a published effect): the market's move from the prior close to
    10:30 predicts its last half hour. At `at`, if that early move is at least `threshold_pct`,
    buy (or short) a basket of the largest stocks (`vehicle`, comma-separated; the index ETFs are
    in the history only from 11 Sep 2026) and sell in the closing auction."""

    family = "index_momentum"
    defaults = {"at": "15:30", "threshold_pct": 0.4, "vehicle": "BHP,CBA,CSL,NAB,WBC",
                "stop_pct": 1.0, "risk_pct": 0.2, "cost_multiple": 0.0,
                "max_trades": 5}  # fmt: skip

    def on_bar(self, v, slot):
        hh, mm = self.p["at"].split(":")
        if self.state.get("done") or v.m.now.time() < time(int(hh), int(mm)):
            return []
        self.state["done"] = True
        idx, prev = v.m.index, v.m.prev_close("^AXJO")
        early = idx.last_close(32) if idx is not None else None  # through the 10:30 bar
        if not prev or early is None:
            return []
        sig = (early / prev - 1) * 100
        if abs(sig) < self.p["threshold_pct"]:
            return []
        acts = []
        for code in str(self.p["vehicle"]).upper().split(",")[: int(self.p["max_trades"])]:
            last = v.m.last(code.strip())
            if not last or code.strip() not in v.m.bars:
                continue
            side = "buy" if sig > 0 else "short"
            stop = last * (1 - self.p["stop_pct"] / 100) if side == "buy" else last * (
                1 + self.p["stop_pct"] / 100)  # fmt: skip
            a = self.enter(v, Plan(code.strip(), side, None, stop, f"index {sig:+.2f}% by 10:30"))
            if a:
                acts.append(a)
        return acts


_INDUSTRY: dict = {}


def industry_of(code: str) -> str | None:
    """The stock's industry group (data/universe/asx_directory.csv), read once."""
    if not _INDUSTRY:
        import csv

        from asxbot.config import load_config

        _INDUSTRY["_loaded"] = ""
        try:
            path = load_config().data_dir / "universe" / "asx_directory.csv"
            with open(path, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    _INDUSTRY[str(r["code"]).upper()] = r.get("industry") or ""
        except Exception:  # noqa: BLE001 - no directory: no peers
            pass
    return _INDUSTRY.get(code.upper()) or None


class Sympathy(RuleTrader):
    """Sympathy moves (I0083): a leader moves hard on its own price-sensitive news; its no-news
    peers in the same industry group that have not moved yet are traded in the leader's
    direction, stop at `stop_pct`, flat at the close."""

    family = "sympathy"
    defaults = {"from": "10:30", "last_entry": "12:30", "leader_min_chg_pct": 6.0,
                "leader_min_rvol": 3.0, "leader_min_turnover_aud": 5_000_000.0,
                "peer_max_move_frac": 0.33, "stop_pct": 2.0,
                "min_turnover_aud": 5_000_000.0}  # fmt: skip

    def on_bar(self, v, slot):
        hh, mm = self.p["from"].split(":")
        if v.m.now.time() < time(int(hh), int(mm)) or self.past_last_entry(v):
            return []
        if len(self.taken) >= self.p["max_trades"]:
            return []
        news = v.news_codes()
        leaders = []
        for code in news:
            to = v.m.turnover(code) or 0
            if code not in v.m.bars or to < self.p["leader_min_turnover_aud"]:
                continue
            chg, rv = v.change_pct(code), v.rvol(code)
            if chg is None or rv is None or abs(chg) < self.p["leader_min_chg_pct"]:
                continue
            if rv < self.p["leader_min_rvol"] or not industry_of(code):
                continue
            leaders.append((abs(chg), code, chg))
        leaders.sort(reverse=True)
        acts = []
        for _a, lead, lchg in leaders:
            ind = industry_of(lead)
            for code, b in v.m.bars.items():
                if code == lead or code in news or code in self.taken or b.v[slot] <= 0:
                    continue
                if industry_of(code) != ind:
                    continue
                if (v.m.turnover(code) or 0) < self.p["min_turnover_aud"]:
                    continue
                chg = v.change_pct(code)
                if chg is None or abs(chg) > abs(lchg) * self.p["peer_max_move_frac"]:
                    continue
                last = float(b.c[slot])
                side = "buy" if lchg > 0 else "short"
                stop = last * (1 - self.p["stop_pct"] / 100) if side == "buy" else last * (
                    1 + self.p["stop_pct"] / 100)  # fmt: skip
                a = self.enter(v, Plan(code, side, None, stop, f"sympathy with {lead}"))
                if a:
                    acts.append(a)
                if len(self.taken) >= self.p["max_trades"]:
                    return acts
        return acts


FAMILIES = {
    c.family: c
    for c in (
        ORB,
        GapFade,
        VwapRev,
        HodMomentum,
        Drift,
        CloseStrength,
        Pullback,
        IndexRevert,
        IndexMomentumClose,
        Sympathy,
    )
}


def make(spec: dict) -> RuleTrader:
    fam = spec.get("family")
    if fam not in FAMILIES:
        raise ValueError(f"unknown family {fam!r}; known: {', '.join(FAMILIES)}")
    return FAMILIES[fam](spec.get("params") or {}, name=spec.get("name"))


def param_space() -> dict:
    """Every family's parameters and defaults (the search's proposer reads this)."""
    return {f: {**COMMON, **c.defaults} for f, c in FAMILIES.items()}


_ = (datetime, field, slot_of, slot_time, classify_headline)
