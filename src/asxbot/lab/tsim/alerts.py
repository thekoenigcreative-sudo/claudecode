"""Wake-ups a trader sets for itself (CLOUD_BRIEF 6): like a trader with alerts, the AI is not
called every minute. Plain code watches, and wakes it when:
  * price_above / price_below: a stock trades at or through a level;
  * time: the clock reaches HH:MM (and `next_wake`, the trader's own "call me back at");
  * volume: a stock's relative volume (today's cumulative vs its usual by this time) >= x;
  * news: an announcement for a code, or any price-sensitive announcement (optionally
    of given types) is released;
  * scanner: any stock newly meets the trader's own thresholds (move % and/or relative volume
    and/or opening-window relative volume, with a turnover floor) - once per stock per day;
  * its own fills and order changes (always), and a position moving by a set % (move).
Every threshold is the trader's own choice; nothing here is a trading rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time

import numpy as np

KINDS = ("price_above", "price_below", "time", "volume", "news", "scanner", "move")


@dataclass
class AlertBook:
    alerts: dict = field(default_factory=dict)  # id -> spec
    next_id: int = 1
    fired_scanner: set = field(default_factory=set)  # (alert id, code) already reported today
    news_seen: set = field(default_factory=set)
    last_price: dict = field(default_factory=dict)

    def add(self, spec: dict, anon) -> dict:
        kind = str(spec.get("type", "")).lower()
        if kind not in KINDS:
            return {"ok": False, "error": f"alert type must be one of {', '.join(KINDS)}"}
        s = {"type": kind}
        if kind in ("price_above", "price_below", "volume", "move"):
            code = anon.real(str(spec.get("code", "")))
            if not code:
                return {"ok": False, "error": f"unknown code {spec.get('code')!r}"}
            s["code"] = code
        if kind in ("price_above", "price_below"):
            s["level"] = anon.unprice(s["code"], float(spec["level"]))
        if kind == "time":
            try:
                hh, mm = str(spec.get("at", "")).split(":")[:2]
                s["at"] = time(int(hh), int(mm)).isoformat()
            except ValueError:
                return {"ok": False, "error": "time alerts need at: 'HH:MM'"}
        if kind == "volume":
            s["rvol"] = float(spec.get("rvol", 2.0))
        if kind == "move":
            s["pct"] = abs(float(spec.get("pct", 2.0)))
            s["from"] = None
        if kind == "news":
            if spec.get("code"):
                code = anon.real(str(spec["code"]))
                if not code:
                    return {"ok": False, "error": f"unknown code {spec.get('code')!r}"}
                s["code"] = code
            s["sensitive_only"] = bool(spec.get("sensitive_only", not spec.get("code")))
            s["types"] = [str(t) for t in spec.get("types") or []]
        if kind == "scanner":
            for k in ("min_move_pct", "min_rvol", "min_opening_rvol", "min_turnover_aud",
                      "min_gap_pct"):  # fmt: skip
                if spec.get(k) is not None:
                    s[k] = float(spec[k])
            if len(s) == 1:
                return {"ok": False, "error": "a scanner alert needs at least one threshold"}
        s["note"] = str(spec.get("note", ""))[:120]
        s["repeat"] = bool(spec.get("repeat", kind in ("scanner", "news")))
        aid = f"A{self.next_id}"
        self.next_id += 1
        self.alerts[aid] = s
        return {"ok": True, "id": aid}

    def clear(self, ids) -> None:
        for i in ids or []:
            if i == "all":
                self.alerts.clear()
            self.alerts.pop(str(i), None)

    def new_day(self) -> None:
        self.fired_scanner.clear()
        # time alerts are for a day; the rest stay until they fire or are cleared
        self.alerts = {k: v for k, v in self.alerts.items() if v["type"] != "time"}
        for v in self.alerts.values():
            if v["type"] == "move":
                v["from"] = None

    def view(self, anon) -> list[dict]:
        out = []
        for aid, s in self.alerts.items():
            d = {"id": aid, **{k: v for k, v in s.items() if k not in ("from",)}}
            if "code" in d:
                d["code"] = anon.alias(d["code"])
            if "level" in d:
                d["level"] = anon.price(s["code"], d["level"])
            out.append(d)
        return out

    # ------------------------------------------------------------------
    def check(self, view, slot: int) -> list[dict]:
        """Everything that fired with the bar in `slot` (now visible)."""
        m = view.m
        fired: list[dict] = []
        now: datetime = m.now
        for aid, s in list(self.alerts.items()):
            k = s["type"]
            hit = None
            if k in ("price_above", "price_below", "move", "volume"):
                b = m.bars.get(s["code"])
                if b is None or b.v[slot] <= 0:
                    continue
                if k == "price_above" and b.h[slot] >= s["level"]:
                    hit = {"price": float(b.h[slot])}
                elif k == "price_below" and b.l[slot] <= s["level"]:
                    hit = {"price": float(b.l[slot])}
                elif k == "move":
                    if s["from"] is None:
                        s["from"] = float(b.c[slot])
                    elif abs(b.c[slot] / s["from"] - 1) * 100 >= s["pct"]:
                        hit = {"from": s["from"], "price": float(b.c[slot])}
                        s["from"] = float(b.c[slot])
                elif k == "volume":
                    rv = view.rvol(s["code"])
                    if rv is not None and rv >= s["rvol"]:
                        hit = {"rvol": round(rv, 2)}
            elif k == "time":
                if now.time() >= time.fromisoformat(s["at"]):
                    hit = {"at": s["at"]}
            elif k == "scanner":
                for code in self._scan(view, s, slot):
                    if (aid, code) not in self.fired_scanner:
                        self.fired_scanner.add((aid, code))
                        fired.append(
                            {"alert": aid, "type": k, "code": code, **self._why(view, code)}
                        )
                continue
            if hit is not None:
                fired.append({"alert": aid, "type": k, "code": s.get("code"), **hit})
                if not s["repeat"]:
                    self.alerts.pop(aid, None)
        # news: any newly released announcement matching a news alert
        news_alerts = [(aid, s) for aid, s in self.alerts.items() if s["type"] == "news"]
        if news_alerts:
            a = m.visible_announcements()
            if len(a):
                fresh = a[~a["ids_id"].astype(str).isin(self.news_seen)]
                for r in fresh.itertuples():
                    self.news_seen.add(str(r.ids_id))
                    for aid, s in news_alerts:
                        if s.get("code") and s["code"] != str(r.code).upper():
                            continue
                        if s.get("sensitive_only") and not bool(r.price_sensitive):
                            continue
                        if s.get("types"):
                            from asxbot.announcements.model import classify_headline

                            if classify_headline(r.headline) not in s["types"]:
                                continue
                        fired.append({"alert": aid, "type": "news", "code": str(r.code).upper(),
                                      "id": str(r.ids_id)})  # fmt: skip
                        break
        return fired

    def mark_news_seen(self, view) -> None:
        a = view.m.visible_announcements()
        if len(a):
            self.news_seen.update(a["ids_id"].astype(str))

    @staticmethod
    def _why(view, code) -> dict:
        return {"chg_pct": _r(view.change_pct(code)), "rvol": _r(view.rvol(code))}

    @staticmethod
    def _scan(view, s: dict, slot: int) -> list[str]:
        m = view.m
        out = []
        for code, b in m.bars.items():
            if b.v[slot] <= 0:
                continue
            if s.get("min_turnover_aud") and (m.turnover(code) or 0) < s["min_turnover_aud"]:
                continue
            ok = True
            if s.get("min_move_pct") is not None:
                c = view.change_pct(code)
                ok &= c is not None and abs(c) >= s["min_move_pct"]
            if ok and s.get("min_gap_pct") is not None:
                g = view.gap_pct(code)
                ok &= g is not None and abs(g) >= s["min_gap_pct"]
            if ok and s.get("min_rvol") is not None:
                r = view.rvol(code)
                ok &= r is not None and r >= s["min_rvol"]
            if ok and s.get("min_opening_rvol") is not None:
                r = view.opening_rvol(code)
                ok &= r is not None and r >= s["min_opening_rvol"]
            if ok:
                out.append(code)
        return out


def _r(x):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), 2)
