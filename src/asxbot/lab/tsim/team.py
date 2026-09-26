"""The AI trader as a TEAM of agents working in parallel (Rick, 26 Sep: "build the AI trader as a
team of agents working in parallel, not one"), on the same simulator, tools and costs as the
single AI trader (ai.py), so the two can be measured against each other on the same days.

Each time the team is woken (the same alerts and wake-ups as the single trader):
  1. READERS - one per new announcement, all at once (Sonnet 5): the facts, direction and
     materiality of each price-sensitive announcement since the last wake. Before the models'
     knowledge cutoff they see only the disguised headline and the price reaction (CLAUDE.md:
     no AI classification of historical announcements unless anonymised).
  2. SCANNER (Sonnet 5): the plain-code scans (gainers, losers, gaps, relative volume, opening
     relative volume, news) and the readers' notes -> a watchlist of the stocks in play, with
     why. Re-run at most every `scan_every_min` minutes, or on news and scanner alerts.
  3. SPECIALISTS - one per strategy family, all at once (Sonnet 5): opening range, momentum,
     mean reversion, news/catalyst, multi-day. Each looks at the watchlist (quotes, charts, daily
     bars, news) and the positions, and proposes trades in its own style with entry, stop,
     target, size and confidence - or nothing.
  4. DECISION-MAKER (Opus 5.5, high): chooses among the proposals (or its own ideas), manages
     positions, sets the team's alerts and call-backs. It alone places orders.
  5. RISK MANAGER (Opus 5.5, high): reviews every order that adds risk and may VETO it or cut its
     size; code enforces the verdict. Exits, cancels and stop changes are never vetoed.
After the close the decision-maker writes the journal and the RESEARCHER (Opus 5.5, high) reads
the day - proposals taken and passed, vetoes, fills, results - and writes lessons for tomorrow
and new strategy ideas, which go into the never-ending search's registry to be tested
(search.register, proposer "team-researcher").

Stages run in order; the agents inside a stage run in parallel threads, so the clock advances by
the slowest agent of each stage. Tokens are counted per model and per role.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from asxbot.lab.tsim import llm
from asxbot.lab.tsim.engine import Decision, Trader
from asxbot.lab.tsim.journal import Journal
from asxbot.lab.tsim.tools import TraderView

OPUS = ("claude-opus-5-5", "high")
SONNET = ("claude-sonnet-5", "medium")
SONNET_LOW = ("claude-sonnet-5", "low")

SHARED = """You are part of a small team trading ASX shares in a SIMULATION over real stored
market history (it plays out minute by minute; you see only what existed at the current moment;
every result is judged after brokerage, the spread and slippage). Codes, names and dates may be
disguised (aliases like S1A2B, "Day 7"); trade what you see and never guess a real company or
date. Reply with ONE JSON object and nothing after it."""

READER = (
    SHARED
    + """
ROLE: announcement reader. You read ONE announcement (as much as is available: the headline, its
type, and the stock's price reaction so far) and report facts for the traders. Reply:
{"code": "...", "category": "results|guidance|contract|acquisition|capital_raising|exploration|
clinical|management|other", "direction": "positive|negative|mixed|neutral|unclear",
"materiality": 1-5, "note": "one or two sentences: what it is and why it matters (or not)"}."""
)

SCANNER = (
    SHARED
    + """
ROLE: market scanner. From the scans and the readers' notes, pick the stocks IN PLAY now - the
ones a good day trader would be watching (unusual volume, a catalyst, a clean move, a gap) - at
most 8, best first, and say why in a few words each. Reply:
{"watchlist": [{"code": "...", "why": "..."}], "market": "one line on the market's tone"}."""
)

SPECIALISTS = {
    "opening_range": "opening-range breakouts and breakdowns in stocks in play in the first hour",
    "momentum": "momentum continuation: new highs/lows of day on heavy relative volume, trend days",
    "mean_reversion": "mean reversion: stretched moves back toward VWAP or the prior close, "
    "fading gaps without news",  # fmt: skip
    "news_catalyst": "news-driven moves: reactions to price-sensitive announcements, first "
    "pullbacks after a catalyst, failed reactions",  # fmt: skip
    "multi_day": "multi-day holds (2-10 days): post-announcement drift and strong closes worth "
    "carrying overnight",  # fmt: skip
}
SPECIALIST = (
    SHARED
    + """
ROLE: strategy specialist for {family}: {desc}. Look at the watchlist and positions and propose
trades IN YOUR STYLE ONLY, each with where it is wrong. Propose nothing if nothing fits - that is
common and fine. Reply:
{{"proposals": [{{"code": "...", "side": "buy|short|sell|cover", "type": "market|limit|stop|
stop_limit", "limit": 0.0, "stop": 0.0, "stop_loss": 0.0, "target": 0.0, "value_aud": 0,
"hold": "intraday|days", "confidence": 0.0-1.0, "thesis": "one line"}}], "watch": "optional
one line"}}."""
)

DECIDER = (
    SHARED
    + """
ROLE: the DECISION-MAKER. You alone place orders. Specialists propose; you choose, combine,
size and manage - or do nothing. A risk manager reviews every order that adds risk and may veto
it. Manage open positions (stops, exits, letting winners run). Every call costs time and
budget, so set alerts and a call-back (next_wake) that wake the team when it would act; use
brackets (attach stop/target/trail_pct) so risk is managed between wakes.
Reply: {"orders": [{"op": "place", "code": "...", "side": "buy|sell|short|cover", "qty": 0 or
"value_aud": 0, "type": "market|limit|stop|stop_limit|trailing_stop|moc|loc", "limit": 0.0,
"stop": 0.0, "trail_pct": 0.0, "tif": "day|gtc", "attach": {"stop": 0.0, "target": 0.0,
"trail_pct": 0.0, "tif": "day|gtc"}, "why": "..."}, {"op": "modify", "id": "O00012", "stop":
0.0}, {"op": "cancel", "id": "O00013"}], "alerts": [{"type": "price_above|price_below", "code":
"...", "level": 0.0}, {"type": "time", "at": "HH:MM"}, {"type": "volume", "code": "...",
"rvol": 3}, {"type": "news", "sensitive_only": true}, {"type": "scanner", "min_move_pct": 4,
"min_rvol": 3}], "clear_alerts": ["A3"], "next_wake": "HH:MM", "note": "the plan, briefly"}.
At the END OF DAY reply {"journal": "...", "lessons": ["..."], "orders": [...], "alerts": [...]}.
The account: AUD cash, gross positions + working orders <= equity x leverage, shorts only where
shortable, day orders expire after the closing auction (~16:10), GTC orders carry over."""
)

RISK = (
    SHARED
    + """
ROLE: RISK MANAGER with a veto. You review each order the decision-maker wants to send that ADDS
risk (new or bigger positions). Approve, cut its size, or veto - for concentration, a stop too
far or missing, size too big for the account, a thesis that contradicts the facts, costs that
eat the expected move, or a losing day getting out of hand. You do not trade. Reply:
{"verdicts": [{"index": 0, "approve": true|false, "max_qty": 0 (optional, shares), "why":
"one line"}]} - one verdict per order, in order."""
)

RESEARCHER = (
    SHARED
    + """
ROLE: RESEARCHER, after the close. Read the day - what the specialists proposed, what was taken,
vetoed or skipped, the fills and results - and (1) write lessons for tomorrow's team (short,
specific, testable), (2) propose NEW strategy ideas to test in the lab, in the lab's rules
families where possible. Reply: {"lessons": ["..."], "ideas": [{"kind": "rules", "family":
"orb|gap_fade|vwap_rev|hod_mom|drift|close_strength|pullback|index_revert", "params":
{...}, "reason": "..."} or {"kind": "ai",
"addendum": "<= 600 characters", "reason": "..."} or {"kind": "new_family", "describe":
"...", "reason": "..."}]}. At most 3 ideas; widen rather than tweak."""
)


class TeamTrader(Trader):
    kind = "ai"
    every_minute = False

    def __init__(self, *, journal: Journal, cfg=None, ask=None, log_dir: Path | None = None,
                 max_wakes_per_day: int = 18, scan_every_min: int = 10,
                 max_readers: int = 12, name: str = "team", decider=OPUS, risk=OPUS,
                 staff=SONNET, reader=SONNET_LOW, register_ideas: bool = True,
                 specialists_every_min: int | None = 20):  # fmt: skip
        self.name = name
        self.journal = journal
        self.cfg = cfg
        self.ask = ask or llm.ask
        self.log_dir = Path(log_dir) if log_dir else None
        self.max_wakes = int(max_wakes_per_day)
        self.scan_every = timedelta(minutes=int(scan_every_min))
        self.max_readers = int(max_readers)
        self.models = {"decider": decider, "risk": risk, "scanner": staff, "specialist": staff,
                       "reader": reader, "researcher": decider}  # fmt: skip
        self.register_ideas = register_ideas
        # The cost cut (26 Sep: the first team spent ~85% of its tokens on specialists re-reading
        # the same watchlist every wake): specialists re-run only when the watchlist changed, a
        # watched stock or a position did something, or this many minutes passed; otherwise
        # their last proposals stand. None: every wake (the first team, as compared).
        self.specialists_every = (timedelta(minutes=int(specialists_every_min))
                                  if specialists_every_min else None)  # fmt: skip
        self._reset_day()

    def _reset_day(self):
        self.wakes = 0
        self.watchlist: list[dict] = []
        self.market_note = ""
        self.scanned_at: datetime | None = None
        self.read: dict[str, dict] = {}
        self.notes: list[str] = []
        self.day_log: list[dict] = []
        self.last_props: dict = {}
        self.props_at: datetime | None = None
        self.props_for: tuple = ()

    # ------------------------------------------------------------------ calls
    def _call(self, role: str, system: str, prompt: str, v: TraderView, d: Decision):
        model, effort = self.models[role]
        try:
            res = self.ask(prompt, system=system, model=model, effort=effort, cfg=self.cfg)
        except llm.UsageStop as e:
            return {"_stop": str(e)}, 0.0
        d.calls += 1
        for k, val in (res.get("usage") or {}).items():
            if isinstance(val, (int, float)):
                d.usage[k] = d.usage.get(k, 0) + val
                d.usage[f"{role}:{k}"] = d.usage.get(f"{role}:{k}", 0) + val
                tag = "opus" if "opus" in model else "sonnet"
                d.usage[f"{tag}:{k}"] = d.usage.get(f"{tag}:{k}", 0) + val
        self._log(v, role, prompt, res)
        out = llm.parse_json(res.get("text", "")) if not res.get("error") else None
        return out or {}, float(res.get("seconds") or 0)

    def _parallel(self, jobs, v, d) -> tuple[list, float]:
        """[(role, system, prompt)] at once; returns replies and the slowest agent's time."""
        if not jobs:
            return [], 0.0
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as ex:
            futs = [ex.submit(self._call, r, s, p, v, d) for r, s, p in jobs]
            got = [f.result() for f in futs]
        return [g[0] for g in got], max(g[1] for g in got)

    def _log(self, v, role, prompt, res):
        if self.log_dir is None:
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with (self.log_dir / f"{v.m.day.isoformat()}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": v.m.now.isoformat(timespec="seconds"), "role": role,
                                "prompt": prompt[-6000:], "reply": res.get("text", ""),
                                "seconds": res.get("seconds"), "usage": res.get("usage"),
                                "error": res.get("error")}) + "\n")  # fmt: skip

    # ------------------------------------------------------------------ stages
    def _readers(self, v: TraderView, d: Decision) -> float:
        news = [n for n in v.news(sensitive_only=True, n=60) if n["id"] not in self.read]
        news = news[-self.max_readers :]
        jobs = []
        for n in news:
            code = v.anon.real(n["code"])
            item = {**n, "stock_now": v.quote(code, brief=True) if code else None}
            if code and not isinstance(v.anon, type(None)):
                r = v.read(n["id"]) if v.reader is not None else None
                if r and r.get("reader"):
                    item["reader_summary"] = r["reader"]
            jobs.append(("reader", READER, json.dumps(item, default=str)))
        replies, secs = self._parallel(jobs, v, d)
        for n, r in zip(news, replies, strict=True):
            self.read[n["id"]] = {**r, "code": n["code"], "headline": n["headline"]}
        return secs

    def _scanner(self, v: TraderView, d: Decision, force: bool) -> float:
        if not force and self.scanned_at and v.m.now - self.scanned_at < self.scan_every:
            return 0.0
        scans = {k: v.scan(k, 8, 500_000) for k in ("gainers", "losers", "rvol", "news")}
        if v.m.n_visible > 0:
            scans["gaps"] = v.scan("gaps_up", 5, 500_000) + v.scan("gaps_down", 5, 500_000)
        if v.m.n_visible >= 16:
            scans["opening_rvol"] = v.scan("opening_rvol", 8, 500_000)
        notes = list(self.read.values())[-20:]
        prompt = (f"NOW: {v.when()}\nINDEX TODAY: {v.m.index_move_pct()}\nSCANS: "
                  f"{json.dumps(scans, default=str)}\nREADERS' NOTES: {json.dumps(notes)}\n"
                  f"POSITIONS: {json.dumps(v.account()['positions'])}")  # fmt: skip
        (r,), secs = self._parallel([("scanner", SCANNER, prompt)], v, d)
        wl = [w for w in (r.get("watchlist") or []) if isinstance(w, dict) and w.get("code")]
        # positions stay on the watch
        held = [{"code": p["code"], "why": "held"} for p in v.account()["positions"]]
        seen, out = set(), []
        for w in held + wl:
            if w["code"] not in seen and v._code(str(w["code"])):
                seen.add(w["code"])
                out.append(w)
        self.watchlist = out[:10]
        self.market_note = str(r.get("market", ""))[:200]
        self.scanned_at = v.m.now
        return secs

    def _dossier(self, v: TraderView) -> dict:
        out = {}
        for w in self.watchlist:
            code = v._code(w["code"])
            if not code:
                continue
            news = v.news(code, n=4)
            out[w["code"]] = {
                "why": w.get("why"), "quote": v.quote(code),
                "chart_5m": v.chart(code, 90, 5)["bars"][-18:],
                "daily": v.daily(code, 10)["days"],
                "news": news, "read": [self.read[n["id"]] for n in news if n["id"] in self.read],
            }  # fmt: skip
        return out

    def _specialists(self, v: TraderView, d: Decision, dossier: dict) -> tuple[dict, float]:
        if not dossier and not v.b.acct.positions:
            return {}, 0.0
        acct = v.account()
        base = (f"NOW: {v.when()}\nINDEX TODAY: {v.m.index_move_pct()}\nMARKET: "
                f"{self.market_note}\nBROKERAGE: {v.b.costs.broker.pct:g}% min "
                f"${v.b.costs.broker.minimum:.2f} per order\nACCOUNT: equity {acct['equity']}, "
                f"cash {acct['cash']}\nPOSITIONS: {json.dumps(acct['positions'])}\nWATCHLIST: "
                f"{json.dumps(dossier, default=str)}")  # fmt: skip
        fams = list(SPECIALISTS)
        jobs = [
            ("specialist", SPECIALIST.format(family=f, desc=SPECIALISTS[f]), base) for f in fams
        ]
        replies, secs = self._parallel(jobs, v, d)
        props = {}
        for f, r in zip(fams, replies, strict=True):
            ps = [p for p in (r.get("proposals") or []) if isinstance(p, dict)][:3]
            if ps:
                props[f] = ps
        return props, secs

    def _specialists_due(self, v, reasons, dossier, phase) -> bool:
        if self.specialists_every is None or self.props_at is None or phase != "SESSION":
            return True
        if tuple(sorted(dossier)) != self.props_for:
            return True
        if v.m.now - self.props_at >= self.specialists_every:
            return True
        watched = {v.anon.real(c) for c in dossier} | set(v.b.acct.positions)
        return any(r.get("code") in watched and r.get("type") in (
            "price_above", "price_below", "volume", "move", "news", "scanner", "order")
            for r in reasons)  # fmt: skip

    def _decide(self, v, d, phase, reasons, props, dossier, lessons) -> tuple[dict, float]:
        acct = v.account()
        parts = [f"NOW: {v.when()}  [{phase}]",
                 f"WAKES LEFT TODAY: {self.max_wakes - self.wakes}",
                 f"BROKERAGE: {v.b.costs.broker.pct:g}% of value, "
                 f"min ${v.b.costs.broker.minimum:.2f} per order",
                 "WOKEN BY: " + json.dumps(reasons[:20], default=str),
                 "ACCOUNT: " + json.dumps(acct, separators=(",", ":")),
                 "TEAM ALERTS: " + json.dumps(self._alerts(), separators=(",", ":")),
                 f"INDEX TODAY: {v.m.index_move_pct()}; MARKET: {self.market_note}",
                 "WATCHLIST (scanner): " + json.dumps(dossier, default=str)[:9000],
                 "SPECIALISTS' PROPOSALS: " + json.dumps(props),
                 "NEW ANNOUNCEMENTS READ: "
                 + json.dumps(list(self.read.values())[-12:])]  # fmt: skip
        if getattr(self, "gap_after", False):
            parts.append("SAMPLE NOTE: tomorrow is not simulated - anything still held at "
                         "today's close is sold at the closing price, and working orders are "
                         "cancelled.")  # fmt: skip
        if self.notes:
            parts.append("YOUR NOTES TODAY:\n" + "\n".join(self.notes[-6:]))
        if lessons:
            parts.append("LESSONS (team journal, earlier days):\n" + "\n".join(
                "- " + v.anon.text(x) for x in lessons))  # fmt: skip
        (r,), secs = self._parallel([("decider", DECIDER, "\n".join(parts))], v, d)
        return r, secs

    def _risk(self, v, d, orders) -> tuple[list, float, list]:
        """Vetoes and size cuts on orders that add risk; returns (orders, seconds, vetoes)."""
        adds = []
        for i, o in enumerate(orders):
            if str(o.get("op", "place")) != "place":
                continue
            code = v.anon.real(str(o.get("code", "")))
            held = v.b.acct.positions.get(code).qty if code in v.b.acct.positions else 0
            side = str(o.get("side", "buy"))
            reduces = (side in ("sell",) and held > 0) or (side == "cover" and held < 0)
            if not reduces:
                adds.append(i)
        if not adds:
            return orders, 0.0, []
        acct = v.account()
        prompt = (f"NOW: {v.when()}\nACCOUNT: {json.dumps(acct, separators=(',', ':'))}\n"
                  f"INDEX TODAY: {v.m.index_move_pct()}\nORDERS TO REVIEW: "
                  f"{json.dumps([orders[i] for i in adds])}")  # fmt: skip
        (r,), secs = self._parallel([("risk", RISK, prompt)], v, d)
        verdicts = r.get("verdicts") or []
        keep = set(range(len(orders)))
        vetoes = []
        for k, i in enumerate(adds):
            vd = next((x for x in verdicts if isinstance(x, dict) and x.get("index") == k),
                      verdicts[k] if k < len(verdicts) and isinstance(verdicts[k], dict)
                      else {})  # fmt: skip
            if vd.get("approve") is False:
                keep.discard(i)
                vetoes.append({"order": orders[i], "why": vd.get("why", "")})
            elif vd.get("max_qty"):
                try:
                    q = int(vd["max_qty"])
                    if orders[i].get("qty") is None or int(orders[i]["qty"]) > q:
                        orders[i] = {**orders[i], "qty": q}
                        orders[i].pop("value_aud", None)
                except (TypeError, ValueError):
                    pass
        return [o for j, o in enumerate(orders) if j in keep], secs, vetoes

    # ------------------------------------------------------------------ Trader
    def bind_alerts(self, book, anon) -> None:
        self._book, self._anon = book, anon

    def _alerts(self):
        return self._book.view(self._anon) if getattr(self, "_book", None) else []

    def _session(self, v: TraderView, phase: str, reasons: list, force_scan: bool) -> Decision:
        d = Decision()
        if self.wakes >= self.max_wakes:
            d.note = "(not woken: the day's wake budget is spent)"
            return d
        self.wakes += 1
        t0 = v.m.now
        lat = self._readers(v, d)
        v.m.now = t0 + timedelta(seconds=lat)
        lat += self._scanner(v, d, force_scan)
        v.m.now = t0 + timedelta(seconds=lat)
        dossier = self._dossier(v)
        if self._specialists_due(v, reasons, dossier, phase):
            props, s = self._specialists(v, d, dossier)
            self.last_props, self.props_at = props, v.m.now
            self.props_for = tuple(sorted(dossier))
        else:
            props, s = self.last_props, 0.0
        lat += s
        v.m.now = t0 + timedelta(seconds=lat)
        lessons = self.journal.lessons(v.m.day.isoformat())
        r, s = self._decide(v, d, phase, reasons, props, dossier, lessons)
        lat += s
        orders = [o for o in r.get("orders") or [] if isinstance(o, dict)]
        orders, s, vetoes = self._risk(v, d, orders)
        lat += s
        v.m.now = t0
        d.latency_s = lat
        d.actions = orders
        d.alerts = [a for a in r.get("alerts") or [] if isinstance(a, dict)]
        d.clear_alerts = [str(x) for x in r.get("clear_alerts") or []]
        d.next_wake = r.get("next_wake") or None
        note = str(r.get("note") or "")
        if note:
            self.notes.append(f"{(t0 + timedelta(seconds=lat)):%H:%M} {note[:300]}")
            d.note = note
        if vetoes:
            d.note = (d.note + " | risk vetoed: " + "; ".join(
                f"{x['order'].get('code')} ({x['why'][:80]})" for x in vetoes)
            ).strip(" |")  # fmt: skip
        self.day_log.append({"at": t0.strftime("%H:%M"), "phase": phase,
                             "proposals": props, "orders": orders, "vetoes": vetoes,
                             "note": note[:300]})  # fmt: skip
        return d

    def pre_open(self, v, ctx):
        self._reset_day()
        return self._session(v, "PRE-OPEN", [], force_scan=True)

    def wake(self, v, reasons, ctx):
        force = any(r.get("type") in ("news", "scanner") for r in reasons)
        return self._session(v, "SESSION", reasons, force_scan=force)

    def after_close(self, v, ctx):
        d = Decision()
        today = v.m.day.isoformat()
        acct = v.b.acct
        fills = [{"order": o.id, "code": v.anon.alias(o.code), "side": o.side, "type": o.type,
                  "filled": v.anon.shares_to_shown(o.code, o.filled),
                  "avg": v.anon.price(o.code, o.avg_price), "why": o.why}
                 for o in acct.orders.values()
                 if o.filled and any(f["at"][:10] == today for f in o.fills)]  # fmt: skip
        prev = acct.marks[-1]["equity"] if acct.marks else acct.start_cash
        day = (f"NOW: {v.when()} [END OF DAY]\nEQUITY NOW vs LAST CLOSE: "
               f"{acct.equity(v.m.last):,.2f} vs {prev:,.2f}\nFILLS: {json.dumps(fills)}\n"
               f"ACCOUNT: {json.dumps(v.account(), separators=(',', ':'))}\nTHE DAY'S WAKES: "
               f"{json.dumps(self.day_log, default=str)[:14000]}")  # fmt: skip
        (j, rs), secs = self._parallel([
            ("decider", DECIDER, day + "\n\nWrite the team journal for today (END OF DAY)."),
            ("researcher", RESEARCHER, day)], v, d)  # fmt: skip
        orders = [o for o in j.get("orders") or [] if isinstance(o, dict)]
        orders, rsecs, vetoes = self._risk(v, d, orders)  # tomorrow's orders are reviewed too
        d.latency_s = secs + rsecs
        d.actions = orders
        d.alerts = [a for a in j.get("alerts") or [] if isinstance(a, dict)]
        lessons = [str(x)[:300] for x in (j.get("lessons") or [])][:6] + [
            "(researcher) " + str(x)[:300] for x in (rs.get("lessons") or [])][:4]  # fmt: skip
        entry = {"journal": str(j.get("journal", ""))[:4000], "lessons": lessons,
                 "label": v.anon.date(v.m.day), "ideas": rs.get("ideas") or []}  # fmt: skip
        self.journal.write(today, entry)
        d.journal = json.dumps({"journal": entry["journal"], "lessons": lessons})
        if self.register_ideas and self.cfg is not None:
            from asxbot.lab.tsim import search

            for idea in (rs.get("ideas") or [])[:3]:
                if isinstance(idea, dict):
                    spec = {k: idea[k] for k in ("kind", "family", "params", "addendum",
                                                 "describe") if k in idea}  # fmt: skip
                    search.register(self.cfg, spec, str(idea.get("reason", "")),
                                    "team-researcher")  # fmt: skip
        return d
