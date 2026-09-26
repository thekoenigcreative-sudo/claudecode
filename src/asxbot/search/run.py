"""THE SEARCH LOOP (CLOUD_BRIEF_SEARCH.md 2-4).

For each registered idea, in order (search/ideas.py):
  practice  run it on the lab's TUNE window (the practice split); the lab's screen gate
            (lab/score.gate "screen": P&L after costs > 0, 20+ trades, better than the frozen
            rule bot on the same days);
  check     survivors run on VALIDATE (the check set); the lab's validation gate with
            N = EVERY idea tried so far (so each try raises the bar sqrt(2 ln N)), plus: still
            positive with spread and impact doubled, and not carried by its best 3 trades;
  sealed    each finalist gets the LOCKED TEST once (lab/splits.unseal records it in the lab's
            own locked_runs.jsonl, so F - final candidates ever run - is shared with the lab)
            and is judged on WINNER.md's locked-test criteria. Shadow trading (criterion 2) is
            not possible here: a pass says "ready for shadow", never "winner".
Every idea's result is appended to data/search/tried.jsonl (never rewritten) and the research
log (reports/research_log.md) and the report are rebuilt from it, losers included.
"""

from __future__ import annotations

import json
import time as wall
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from asxbot.lab import splits
from asxbot.lab.score import gate, validation_bar, versus
from asxbot.lab.winner import CRITERIA, t_required
from asxbot.search import book
from asxbot.search.costs import BROKERS, Costs
from asxbot.search.data import Market
from asxbot.search.ideas import all_ideas, complexity
from asxbot.search.strategies import FAMILIES, START_CASH, Ctx

# which frozen rule bot an idea is compared with ("beats the old rules", WINNER.md 6)
BASELINE_OF = {"intraday": "daytrader", "multiday": "v2"}
NEWS_FAMILIES = {"drift", "day2_orb"}


def baseline_for(idea: dict) -> str:
    if idea["family"] in NEWS_FAMILIES:
        return "v2"
    return BASELINE_OF[FAMILIES[idea["family"]][2]]


class Search:
    def __init__(self, market: Market, repo: Path, replay_json: Path | None = None,
                 lab_data: Path | None = None, out_tag: str | None = None):  # fmt: skip
        self.m = market
        self.repo = Path(repo)
        self.dir = market.data / "search"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tried_path = self.dir / "tried.jsonl"
        self.lab_data = lab_data or (market.data / "lab")
        self.replay_json = replay_json or _latest_replay(self.repo / "reports")
        self.tag = out_tag or datetime.now().strftime("%Y%m%d")
        self._ctx: dict[str, Ctx] = {}
        self.sessions = (sorted(d.date() for d in pd.to_datetime(market.daily["day"].unique()))
                         if len(market.daily) else [])  # fmt: skip
        self.win = {w: [d for d in self.sessions if splits.window_of(d) == w]
                    for w in ("tune", "validate", "locked")}  # fmt: skip
        self.base = {}
        if self.replay_json and Path(self.replay_json).exists():
            for which in ("daytrader", "v2"):
                self.base[which] = book.baseline_records(self.replay_json, which)

    def ctx(self, broker: str = "ibkr", mult: float = 1.0) -> Ctx:
        key = f"{broker}:{mult}"
        if key not in self._ctx:
            self._ctx[key] = Ctx(self.m, Costs(BROKERS[broker], mult))
        return self._ctx[key]

    # ------------------------------------------------------------------ bookkeeping
    def tried(self) -> list[dict]:
        if not self.tried_path.exists():
            return []
        return [json.loads(x) for x in self.tried_path.read_text(encoding="utf-8").splitlines()
                if x.strip()]  # fmt: skip

    def _append(self, rec: dict) -> None:
        with self.tried_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    # ------------------------------------------------------------------ one run
    def run_idea(self, idea: dict, days: list[date], broker: str = "ibkr",
                 mult: float = 1.0, allow_locked: bool = False
                 ) -> tuple[dict, list[dict]]:  # fmt: skip
        splits.check_days(days, allow_locked)
        fn, _defaults, kind = FAMILIES[idea["family"]]
        ctx = self.ctx(broker, mult)
        raw = fn(ctx, days, dict(idea.get("params") or {}))
        recs = book.records(ctx, days, raw, kind)
        played = [r for r in recs if not r["gaps"]]
        s = book.score(played)
        s["gaps"] = len(recs) - len(played)
        s["stuck_at_close"] = sum(len(r["daytrader"]["books"]["bot"]["stuck_at_close"])
                                  for r in played)  # fmt: skip
        return s, played

    def base_card(self, idea: dict, days: list[date]) -> dict:
        recs = book.in_window(self.base.get(baseline_for(idea), []), days)
        return book.score(recs) if recs else {"daily": {}, "pnl_after_fees": 0.0, "trades": 0}

    # ------------------------------------------------------------------ the loop
    def run(self, only: list[str] | None = None, progress=print) -> list[dict]:
        done = {r["id"] for r in self.tried()}
        n = len(done)
        for idea in all_ideas(self.m.data):
            if idea["id"] in done or (only and idea["id"] not in only):
                continue
            n += 1
            t0 = wall.time()
            rec = {"id": idea["id"], "family": idea["family"], "wave": idea.get("wave"),
                   "parent": idea.get("parent"), "params": idea.get("params") or {},
                   "reason": idea["reason"], "n_at_try": n, "tried_at":
                   datetime.now().isoformat(timespec="seconds"), "stage": "practice",
                   "baseline": baseline_for(idea)}  # fmt: skip
            if not self.win["tune"]:
                rec.update(verdict="not run", why=["no practice (TUNE) sessions in the data"])
                self._append(rec)
                continue
            s, _ = self.run_idea(idea, self.win["tune"])
            b = self.base_card(idea, self.win["tune"])
            ok, why = gate("screen", s, b)
            rec["practice"] = _slim(s, b)
            rec["why"] = why
            if ok and complexity(idea) > CRITERIA["max_params"]:
                ok, why = False, [f"{complexity(idea)} changed parameters (over 6)"]
            if ok:
                rec["stage"] = "check"
                s2, _ = self.run_idea(idea, self.win["validate"])
                b2 = self.base_card(idea, self.win["validate"])
                ok2, why2 = gate("validate", s2, b2, n_validated=n)
                stress, _ = self.run_idea(idea, self.win["validate"], mult=2.0)
                cheap, _ = self.run_idea(idea, self.win["validate"], broker="cheapest_api")
                rec["check"] = _slim(s2, b2)
                rec["check"]["bar"] = round(validation_bar(n), 3)
                rec["check_2x"] = _slim(stress, b2)
                rec["check_cheapest_broker"] = _slim(cheap, b2)
                extra = []
                if stress["pnl_after_fees"] <= 0:
                    extra.append("loses with spread and impact doubled "
                                 f"({stress['pnl_after_fees']:+,.0f})")  # fmt: skip
                if s2["pnl_without_top3"] <= 0:
                    extra.append("carried by its best 3 trades (without them "
                                 f"{s2['pnl_without_top3']:+,.0f})")  # fmt: skip
                if extra:
                    ok2 = False
                    why2 = [w for w in why2 if not w.startswith("passed")] + extra
                rec["why"] = why2
                if ok2:
                    rec["stage"] = "finalist"
            rec["verdict"] = {"practice": "failed practice", "check": "failed check",
                              "finalist": "finalist"}[rec["stage"]]  # fmt: skip
            rec["seconds"] = round(wall.time() - t0, 1)
            self._append(rec)
            if progress:
                pr = rec.get("practice") or {}
                progress(f"{idea['id']} {idea['family']}: {rec['verdict']} - "
                         f"practice {pr.get('trades')} trades {pr.get('pnl', 0):+,.0f}; "
                         f"{'; '.join(rec['why'])[:160]}")  # fmt: skip
        return self.tried()

    def sealed(self, progress=print) -> list[dict]:
        """Each finalist not yet sealed-tested gets its ONE locked-test run."""
        out = []
        tried = self.tried()
        sealed_ids = {r["id"] for r in tried if r.get("stage") == "sealed"}
        finals = [r for r in tried if r.get("stage") == "finalist" and r["id"] not in sealed_ids]
        finals.sort(key=lambda r: (len(r["params"]), -(r["check"]["t_stat"] or 0)))
        ideas = {i["id"]: i for i in all_ideas(self.m.data)}
        for r in finals:
            idea = ideas[r["id"]]
            F = splits.unseal(self.lab_data, {"id": f"search-{idea['id']}", "stage": "final"},
                              "rules-only search finalist (CLOUD_BRIEF_SEARCH.md)")  # fmt: skip
            s, _ = self.run_idea(idea, self.win["locked"], allow_locked=True)
            b = self.base_card(idea, self.win["locked"])
            checks = sealed_checks(idea, s, b, F)
            rec = {**r, "stage": "sealed", "F": F, "locked": _slim(s, b),
                   "locked_checks": checks, "tried_at":
                   datetime.now().isoformat(timespec="seconds")}  # fmt: skip
            passed = all(ok for _, ok, _ in checks)
            rec["verdict"] = ("passed the sealed test - ready for 10 shadow days (WINNER.md 2) "
                              "before Rick can be asked" if passed else "failed the sealed test")
            self._append(rec)
            out.append(rec)
            if progress:
                progress(f"{idea['id']} SEALED: {rec['verdict']}")
        return out


def sealed_checks(idea: dict, s: dict, b: dict, F: int) -> list[tuple[str, bool, str]]:
    """WINNER.md's criteria that a locked-test run alone can answer (1, 3-9, 11). Criterion 2
    (shadow) and the shadow halves of 4, 6 and 7 need forward trading; 10 is automatic for a
    rules-only candidate (no model is asked anything)."""
    C = CRITERIA
    worst = C["worst_day_pct"] / 100 * START_CASH
    share = s.get("top3_share_of_gross_pct")
    up, down = s["up_days"], s["down_days"]
    edge = versus(s, b)
    need = t_required(F)
    return [
        ("1 positive after costs on the locked test", s["pnl_after_fees"] > 0,
         f"{s['pnl_after_fees']:+,.2f}"),
        ("3 at least 40 trades", s["trades"] >= C["locked_min_trades"], f"{s['trades']} trades"),
        ("4 not carried by its best 3 trades",
         s["pnl_without_top3"] > 0 and share is not None and share <= C["top_share_max_pct"],
         f"without them {s['pnl_without_top3']:+,.2f}; {share}% of gross profit"),
        ("5 holds on up and down days",
         up["n"] >= C["updown_min_days"] and down["n"] >= C["updown_min_days"]
         and up["pnl"] >= 0 and down["pnl"] >= 0,
         f"up {up['n']} days {up['pnl']:+,.2f}, down {down['n']} days {down['pnl']:+,.2f}"),
        ("6 beats the frozen rule bot (locked days)", edge > 0, f"{edge:+,.2f}"),
        ("7 worst drawdown <= 10% and no day below -3%",
         s["max_drawdown_pct"] <= C["max_drawdown_pct"] and s["worst_day"] >= worst,
         f"drawdown {s['max_drawdown_pct']:.1f}%, worst day {s['worst_day']:+,.2f}"),
        ("8 more green days than red", s["green_days"] > s["red_days"],
         f"{s['green_days']} green / {s['red_days']} red"),
        ("9 not luck from trying many things", s["t_stat"] >= need,
         f"t {s['t_stat']:.2f}, needs {need:.2f} (F={F})"),
        ("11 simple enough", complexity(idea) <= C["max_params"],
         f"{complexity(idea)} changed parameters"),
    ]  # fmt: skip


def _slim(s: dict, b: dict) -> dict:
    return {
        "days": s.get("days"), "trades": s.get("trades"), "pnl": s.get("pnl_after_fees"),
        "fees": s.get("fees"), "win_rate_pct": s.get("win_rate_pct"), "avg_r": s.get("avg_r"),
        "t_stat": s.get("t_stat"), "worst_day": s.get("worst_day"),
        "max_drawdown_pct": s.get("max_drawdown_pct"), "green_days": s.get("green_days"),
        "red_days": s.get("red_days"), "pnl_without_top3": s.get("pnl_without_top3"),
        "top3_share_of_gross_pct": s.get("top3_share_of_gross_pct"),
        "up_days": s.get("up_days"), "down_days": s.get("down_days"),
        "stuck_at_close": s.get("stuck_at_close"), "gaps": s.get("gaps"),
        "baseline_pnl_same_days": round(sum(b["daily"].get(d, 0.0) for d in s.get("daily", {})),
                                        2),
        "vs_baseline": versus(s, b) if s.get("daily") else 0.0,
    }  # fmt: skip


def _latest_replay(reports: Path) -> Path | None:
    got = sorted(reports.glob("replay_ibkr_*.json"))
    return got[-1] if got else None


__all__ = ["Search", "baseline_for", "sealed_checks"]
