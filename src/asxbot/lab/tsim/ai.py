"""The AI trader: Claude Opus 5.5 at high effort decides everything a trader decides - what to
look at, what to trade, how, how much, when to get out - through the simulation's tools.

Each time it is woken (engine.py) it gets a compact packet (the time, why it was woken, its
account, orders and alerts, the market, today's notes, lessons from its journal) and answers
with JSON: either "look" (tool requests, answered and sent back in the same wake, up to
`max_looks` times) or orders, alerts and a note. Every call's real response time is added to
the simulated clock, and while it is still thinking the market moves on (its looks see bars
that completed while it thought). Tokens are counted per call.

Nothing numeric about trading style is fixed here: no trade counts, risk percentages, setups
or entry rules. The only limits are the simulation's (the account, costs, no future data)
and the lab's usage budget (`max_calls_per_day`, a budget guard: past it the trader is not
woken again that day and its resting orders and brackets keep working).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from asxbot.lab.tsim import llm
from asxbot.lab.tsim.engine import Decision, Trader
from asxbot.lab.tsim.journal import Journal
from asxbot.lab.tsim.tools import TraderView

PROMPT = Path(__file__).parent / "prompts" / "trader.md"
LOOK_TOOLS = ("scan", "quote", "chart", "daily", "news", "read", "index", "account", "journal")


class AITrader(Trader):
    kind = "ai"
    every_minute = False

    def __init__(self, *, journal: Journal, model: str = llm.DECIDER[0],
                 effort: str = llm.DECIDER[1], max_looks: int = 3, max_calls_per_day: int = 60,
                 addendum: str = "", cfg=None, ask=None, log_dir: Path | None = None,
                 name: str = "ai"):  # fmt: skip
        self.name = name
        self.journal = journal
        self.model, self.effort = model, effort
        self.max_looks = int(max_looks)
        self.max_calls = int(max_calls_per_day)
        self.system = PROMPT.read_text(encoding="utf-8") + (
            "\n\nADDED INSTRUCTIONS (the lab's current version of you):\n" + addendum
            if addendum else "")  # fmt: skip
        self.cfg = cfg
        self.ask = ask or llm.ask
        self.log_dir = Path(log_dir) if log_dir else None
        self.calls_today = 0
        self.notes: list[str] = []
        self.day = ""

    # ------------------------------------------------------------------ packet
    def _header(self, v: TraderView, phase: str, reasons: list[dict]) -> str:
        acct = v.account()
        fees = v.b.costs.broker
        parts = [
            f"NOW: {v.when()}  [{phase}]",
            f"BROKERAGE: {fees.pct:g}% of value, minimum ${fees.minimum:.2f} per order (GST incl.)",
            f"CALLS LEFT TODAY: {self.max_calls - self.calls_today}; "
            f"LOOKS PER WAKE: {self.max_looks}",
        ]
        if reasons:
            parts.append("WOKEN BY:\n" + "\n".join("- " + self._reason(v, r) for r in reasons[:25]))
        parts.append("ACCOUNT: " + json.dumps(acct, separators=(",", ":")))
        parts.append("YOUR ALERTS: " + json.dumps(self._alerts_view, separators=(",", ":")))
        idx = v.m.index_move_pct()
        parts.append(f"INDEX TODAY: {'n/a' if idx is None else f'{idx:+.2f}%'}")
        if self.notes:
            parts.append("YOUR NOTES TODAY:\n" + "\n".join(self.notes[-8:]))
        lessons = self.journal.lessons(v.m.day.isoformat())
        if lessons:
            parts.append("LESSONS FROM YOUR JOURNAL (earlier days):\n" + "\n".join(
                "- " + v.anon.text(x) for x in lessons))  # fmt: skip
        return "\n".join(parts)

    def _reason(self, v: TraderView, r: dict) -> str:
        a = v.anon
        r = dict(r)
        if r.get("code"):
            code = r["code"]
            r["code"] = a.alias(code)
            for k in ("price", "level", "from", "avg_price"):
                if r.get(k) is not None:
                    r[k] = a.price(code, r[k])
            for k in ("qty", "filled", "position_after"):
                if r.get(k) is not None:
                    r[k] = a.shares_to_shown(code, r[k])
        r.pop("side_real", None)
        return json.dumps({k: val for k, val in r.items() if val not in (None, "")},
                          separators=(",", ":"))  # fmt: skip

    def _preopen_extra(self, v: TraderView) -> str:
        overnight = v.news(sensitive_only=True, n=40)
        movers = []
        s = v.m.summaries
        if s is not None:
            for code in v.m.codes():
                t = s.before(code, v.m.day, 2)
                if t is not None and len(t) == 2 and t["close"].iloc[0]:
                    ch = (t["close"].iloc[1] / t["close"].iloc[0] - 1) * 100
                    movers.append((ch, code, float(t["turnover"].iloc[1])))
        movers.sort()
        fmt = lambda xs: [{"code": v.anon.alias(c), "chg_pct": round(ch, 1),  # noqa: E731
                           "turnover": round(to)} for ch, c, to in xs]  # fmt: skip
        return "\n".join([
            "OVERNIGHT/EARLY PRICE-SENSITIVE ANNOUNCEMENTS: " + json.dumps(overnight),
            "YESTERDAY'S BIGGEST GAINERS: " + json.dumps(fmt(movers[::-1][:10])),
            "YESTERDAY'S BIGGEST LOSERS: " + json.dumps(fmt(movers[:10])),
            "OVERSEAS MARKETS: not available in the simulation's data.",
            f"UNIVERSE: {len(v.m.codes())} stocks with trades today (use the scanner).",
        ])  # fmt: skip

    # ------------------------------------------------------------------ looks
    def _look(self, v: TraderView, reqs: list) -> list:
        out = []
        for q in (reqs or [])[:6]:
            q = q if isinstance(q, dict) else {}
            tool = str(q.get("tool", ""))
            try:
                code = v._code(str(q.get("code", ""))) if q.get("code") else None
                if q.get("code") and code is None:
                    raise ValueError(f"unknown code {q.get('code')!r}")
                if tool == "scan":
                    r = v.scan(str(q.get("kind", "gainers")), int(q.get("n", 15)),
                               float(q.get("min_turnover", 0) or 0))  # fmt: skip
                elif tool == "quote":
                    r = v.quote(code)
                elif tool == "chart":
                    r = v.chart(code, int(q.get("minutes", 60)), int(q.get("bar", 1)))
                elif tool == "daily":
                    r = v.daily(code, int(q.get("n", 20)))
                elif tool == "news":
                    r = v.news(code, bool(q.get("sensitive_only", False)), int(q.get("n", 20)))
                elif tool == "read":
                    r = v.read(str(q.get("id")))
                elif tool == "index":
                    r = v.index()
                elif tool == "account":
                    r = v.account()
                elif tool == "journal":
                    r = [
                        {
                            "day": v.anon.text(str(e.get("label", ""))),
                            "journal": v.anon.text(str(e.get("journal", ""))),
                            "lessons": e.get("lessons"),
                        }
                        for e in self.journal.entries(v.m.day.isoformat())[-int(q.get("n", 3)) :]
                    ]
                else:
                    raise ValueError(f"tools: {', '.join(LOOK_TOOLS)}")
            except (ValueError, TypeError, KeyError) as e:
                r = {"error": v.anon.text(str(e))[:200]}
            out.append({"request": q, "result": r})
        return out

    # ------------------------------------------------------------------ one wake
    def _session(self, v: TraderView, head: str, final_key: str = "orders") -> Decision:
        d = Decision()
        if self.calls_today >= self.max_calls:
            d.note = "(not woken: the day's call budget is spent)"
            return d
        transcript = [head]
        t0 = v.m.now
        reply = None
        for round_no in range(self.max_looks + 1):
            if self.calls_today >= self.max_calls:
                break
            last = round_no == self.max_looks
            prompt = "\n\n".join(transcript) + (
                "\n\nThis is your last reply for this wake: act now (no more looks)."
                if last else "")  # fmt: skip
            try:
                res = self.ask(prompt, system=self.system, model=self.model, effort=self.effort,
                               cfg=self.cfg)  # fmt: skip
            except llm.UsageStop as e:
                d.note = f"(budget stop: {e})"
                self.calls_today = self.max_calls
                break
            self.calls_today += 1
            d.calls += 1
            d.latency_s += float(res.get("seconds") or 0)
            for k, val in (res.get("usage") or {}).items():
                d.usage[k] = d.usage.get(k, 0) + val
            d.usage["cached_calls"] = d.usage.get("cached_calls", 0) + int(bool(res.get("cached")))
            self._log(v, prompt, res)
            reply = llm.parse_json(res.get("text", "")) if not res.get("error") else None
            if reply is None:
                d.note = f"(unreadable reply{': ' + res['error'][:80] if res.get('error') else ''})"
                break
            if "look" in reply and not last and not any(k in reply for k in ("orders", "journal")):
                saved = v.m.now
                v.m.now = t0 + timedelta(seconds=d.latency_s)  # the market moved while it thought
                looked = self._look(v, reply.get("look"))
                v.m.now = saved
                transcript.append("YOUR REPLY:\n" + json.dumps(reply, separators=(",", ":")))
                transcript.append(
                    f"LOOK RESULTS (as of {(t0 + timedelta(seconds=d.latency_s)):%H:%M:%S}):\n"
                    + json.dumps(looked, separators=(",", ":"), default=str)
                )
                continue
            break
        if reply:
            d.actions = [a for a in reply.get("orders") or [] if isinstance(a, dict)]
            d.alerts = [a for a in reply.get("alerts") or [] if isinstance(a, dict)]
            d.clear_alerts = [str(x) for x in reply.get("clear_alerts") or []]
            d.next_wake = reply.get("next_wake") or None
            note = str(reply.get("note") or "")
            if note:
                self.notes.append(f"{(t0 + timedelta(seconds=d.latency_s)):%H:%M} {note[:400]}")
                d.note = note
            if final_key == "journal":
                d.journal = json.dumps(
                    {
                        "journal": str(reply.get("journal", ""))[:4000],
                        "lessons": [str(x)[:300] for x in (reply.get("lessons") or [])][:10],
                    }
                )
        return d

    def _log(self, v: TraderView, prompt: str, res: dict) -> None:
        if self.log_dir is None:
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with (self.log_dir / f"{v.m.day.isoformat()}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": v.m.now.isoformat(timespec="seconds"), "prompt": prompt,
                                "reply": res.get("text", ""), "seconds": res.get("seconds"),
                                "usage": res.get("usage"), "cached": res.get("cached"),
                                "error": res.get("error")}) + "\n")  # fmt: skip

    # ------------------------------------------------------------------ Trader
    _alerts_view: list = []

    def bind_alerts(self, book, anon) -> None:
        self._book, self._anon = book, anon

    def _refresh_alerts(self) -> None:
        self._alerts_view = self._book.view(self._anon) if getattr(self, "_book", None) else []

    def pre_open(self, v, ctx):
        self.day = v.m.day.isoformat()
        self.calls_today = 0
        self.notes = []
        self._refresh_alerts()
        head = self._header(v, "PRE-OPEN", []) + "\n" + self._preopen_extra(v) + (
            "\n\nPrepare for the day: decide what to watch and set your alerts; send any orders "
            "for the opening auctions.")  # fmt: skip
        return self._session(v, head)

    def wake(self, v, reasons, ctx):
        self._refresh_alerts()
        return self._session(v, self._header(v, "SESSION", reasons))

    def after_close(self, v, ctx):
        self._refresh_alerts()
        acct = v.b.acct
        today = v.m.day.isoformat()
        fills = [
            {"order": o.id, "code": v.anon.alias(o.code), "side": o.side, "type": o.type,
             "filled": v.anon.shares_to_shown(o.code, o.filled),
             "avg": v.anon.price(o.code, o.avg_price), "why": o.why}
            for o in acct.orders.values()
            if o.filled and any(f["at"][:10] == today for f in o.fills)
        ]  # fmt: skip
        mark_prev = acct.marks[-1]["equity"] if acct.marks else acct.start_cash
        head = self._header(v, "END OF DAY", []) + "\nTODAY'S FILLS: " + json.dumps(fills) + (
            f"\nEQUITY NOW vs LAST CLOSE: {acct.equity(v.m.last):,.2f} vs {mark_prev:,.2f}"
            "\n\nWrite your journal for today (JSON with journal and lessons). You may also leave "
            "GTC orders and alerts for tomorrow.")  # fmt: skip
        self.calls_today = min(
            self.calls_today, self.max_calls - 1
        )  # the journal always gets a call
        d = self._session(v, head, final_key="journal")
        if d.journal:
            e = json.loads(d.journal)
            self.journal.write(today, {**e, "label": v.anon.date(v.m.day)})
        return d


_ = datetime
