"""THE STRATEGY SEARCH THAT NEVER STOPS (CLOUD_BRIEF; Rick 26 Sep: "we need a day trading bot
that keeps coming up with strategies until it finds a profitable one").

Each tick (every night and weekend, inside the usage budget):
  1. PROPOSE: Claude Opus 5.5 (high) reads the research log - every idea tried, its results and
     why it failed, the yardsticks, the families and their parameters, what has not been tried -
     and proposes the next ideas, each with the reason it might work. Rules-only ideas are cheap
     (plain code); an idea for the AI trader's own approach is an instruction addendum.
     Duplicates of anything tried are refused (ideas are never repeated blindly). When a family
     keeps failing the packet says so and the proposer must widen (other holding periods, other
     signals, the announcement flow, sector moves) - or ask for a new family (logged as needing
     a build).
  2. PRACTICE: tested on a 20-day practice sample that rotates with the idea's number (nothing
     is tuned on the same days over and over).
  3. CHECK: promising ones are confirmed on the check set.
  4. SEALED: survivors (finalists) get the sealed block ONCE; then 10+ days of shadow trading
     on new live days (shadow_day, each evening on Rick's PC).
  5. Repeat. After a winner it keeps looking for better, or for a second uncorrelated one.

GUARD AGAINST LUCK: every idea is counted (N). The practice bar is t >= 0.5 x sqrt(2 ln N), the
check bar t >= sqrt(2 ln N) (WINNER.md's validation bar), both after all costs; the sealed bar
is WINNER.md's (t >= 1.65 + 0.5 ln F, F = finalists ever scored on the current sealed block).
Nothing is ever retuned on the sealed block. The sealed block wears out: once `seal_uses` (3)
finalists have been scored on it, a fresh block of the newest trading days is sealed and the old
one is retired to the check set (splits.py).

Judged against the yardsticks (A, its D variants, rules drift) and the OLD RULES (the frozen
day trader's replay, reports/replay_ibkr_*.json) on the same days.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from datetime import date, datetime
from pathlib import Path

from asxbot.lab import store
from asxbot.lab.tsim import llm
from asxbot.lab.tsim import splits as S
from asxbot.lab.tsim.report import SURVIVORSHIP, score

YARDSTICKS = [
    {"name": "A: stocks in play ORB", "kind": "rules", "family": "orb", "params": {}},
    {"name": "A trail: ORB, 3% trailing stop", "kind": "rules", "family": "orb",
     "params": {"exit": "trail", "trail_pct": 3.0}},
    {"name": "A+D regime: ORB, index direction", "kind": "rules", "family": "orb",
     "params": {"regime": "index_day"}},
    {"name": "A+D first30: ORB, first-30-min direction", "kind": "rules", "family": "orb",
     "params": {"regime": "first30"}},
    {"name": "A+D closing volume cap 20%", "kind": "rules", "family": "orb",
     "params": {"close_volume_cap": 0.2}},
    {"name": "C rules: announcement drift 5 days", "kind": "rules", "family": "drift",
     "params": {}},
    {"name": "C rules + D regime: drift, index direction", "kind": "rules", "family": "drift",
     "params": {"regime": "index_day"}},
]  # fmt: skip

SAMPLE_DAYS = 20
PROPOSE_PER_TICK = 3
PROPOSER_SYSTEM = """You are the research lead of a small ASX day-trading lab. You propose the NEXT
strategy ideas to test, drawn from the research log (every idea tried, its results after all costs
and why it failed), known market effects, and what has not been tried. Aim for ideas with a real
reason to have an edge after costs (IBKR 0.088% min $6.60 per order, the spread, slippage). Prefer
widening the search over tweaking a failed idea. Be concrete. Reply with ONE JSON object:
{"ideas": [{"kind": "rules", "family": "<one of the families>", "params": {...only keys listed for
that family...}, "reason": "why it might work, in one or two sentences"} or {"kind": "ai",
"addendum": "instructions (<= 600 characters) that change the AI trader's own approach",
"reason": "..."} or {"kind": "new_family", "describe": "a setup the code cannot express yet",
"reason": "..."}]} with at most 3 ideas."""


# --------------------------------------------------------------------------- the registry
def lab_dir(cfg) -> Path:
    return store.lab_data(cfg) / "tsim"


def ideas_path(cfg) -> Path:
    return lab_dir(cfg) / "ideas.jsonl"


def load_ideas(cfg) -> list[dict]:
    p = ideas_path(cfg)
    if not p.exists():
        return []
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    latest: dict[str, dict] = {}
    for r in rows:  # append-only log: the last record of an id is its state
        latest[r["id"]] = r
    return sorted(latest.values(), key=lambda r: r["n"])


def save_idea(cfg, idea: dict) -> None:
    p = ideas_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(idea, default=str) + "\n")


def fingerprint(spec: dict) -> str:
    keep = {k: spec.get(k) for k in ("kind", "family", "params", "addendum", "describe")}
    return hashlib.sha1(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:12]


def register(cfg, spec: dict, reason: str, proposer: str, parent: str | None = None) -> dict | None:
    """Count an idea (N) before it runs. Refuses an exact repeat."""
    ideas = load_ideas(cfg)
    fp = fingerprint(spec)
    if any(i["fp"] == fp for i in ideas):
        return None
    if spec.get("kind") == "rules":
        from asxbot.lab.tsim.rules import FAMILIES, param_space

        if spec.get("family") not in FAMILIES:
            return None
        allowed = param_space()[spec["family"]]
        bad = [k for k in (spec.get("params") or {}) if k not in allowed]
        if bad:
            return None
    if spec.get("kind") == "ai" and len(str(spec.get("addendum", ""))) > 600:
        return None
    n = len(ideas) + 1
    idea = {"id": f"I{n:04d}", "n": n, "fp": fp, "at": datetime.now().isoformat(timespec="seconds"),
            "proposer": proposer, "parent": parent, "spec": spec, "reason": reason[:500],
            "stage": "queued" if spec.get("kind") != "new_family" else "needs_build",
            "results": {}, "verdict": "", "why": ""}  # fmt: skip
    save_idea(cfg, idea)
    return idea


def update(cfg, idea: dict, **kw) -> dict:
    idea = {**idea, **kw}
    save_idea(cfg, idea)
    return idea


# --------------------------------------------------------------------------- bars
def n_tried(cfg) -> int:
    return sum(1 for i in load_ideas(cfg) if i["stage"] != "needs_build")


def practice_bar(n: int) -> float:
    return 0.5 * math.sqrt(2 * math.log(max(2, n)))


def check_bar(n: int) -> float:
    return max(1.0, math.sqrt(2 * math.log(max(2, n))))


def sample(days: list[date], n: int, k: int = SAMPLE_DAYS) -> list[date]:
    """A practice sample that rotates with the idea's number: a block of `k` CONSECUTIVE
    sessions (26 Sep: scattered days let a position held overnight jump weeks of unsimulated
    market, and multi-day strategies could not be judged)."""
    days = sorted(days)
    if len(days) <= k:
        return days
    start = random.Random(f"practice-{n}").randrange(0, len(days) - k + 1)
    return days[start : start + k]


# --------------------------------------------------------------------------- old rules
def old_rules_daily(root: Path) -> dict[str, float]:
    """The frozen day trader's replay (its rule bot), daily P&L after its costs."""
    out = {}
    for p in sorted((root / "reports").glob("replay_ibkr_*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for r in d.get("days") or []:
            if (r.get("daytrader") or {}).get("pnl") is not None:
                out[r["day"]] = float(r["daytrader"]["pnl"])
    return out


# --------------------------------------------------------------------------- evaluation
def evaluate(cfg, spec: dict, days: list[date], run_id: str, inputs=None) -> dict:
    from asxbot.lab.tsim import run as R

    inputs = inputs or R.prepare_inputs(cfg, days)
    # the days are part of the run's name: a re-test on other days never resumes an old account
    run_id = (
        f"{run_id}_{hashlib.sha1(','.join(d.isoformat() for d in days).encode()).hexdigest()[:6]}"
    )
    out = R.run(run_id, {k: v for k, v in spec.items() if k != "name"}, days, inputs, cfg=cfg,
                resume=True)  # fmt: skip
    s = score(out, cfg)
    old = old_rules_daily(Path(cfg.root))
    s["old_rules"] = round(sum(old.get(d.isoformat(), 0.0) for d in days), 2)
    s["old_rules_days"] = sum(1 for d in days if d.isoformat() in old)
    s["days_list"] = [d.isoformat() for d in days]
    return s


def yardstick_scores(cfg, days: list[date], tag: str, inputs=None, workers: int = 3) -> dict:
    """Every yardstick over `days`, in parallel worker processes (plain code, no model)."""
    from asxbot.lab.tsim import run as R

    inputs = inputs or R.prepare_inputs(cfg, days)
    jobs = [(f"yard_{fingerprint(y)}_{tag}", {k: v for k, v in y.items() if k != "name"}, days)
            for y in YARDSTICKS]  # fmt: skip
    got = {r["run_id"]: r["score"] for r in R.run_many(jobs, inputs, workers=workers)}
    old = old_rules_daily(Path(cfg.root))
    out = {}
    for y, (rid, _, _) in zip(YARDSTICKS, jobs, strict=True):
        s = got[rid]
        s["old_rules"] = round(sum(old.get(d.isoformat(), 0.0) for d in days), 2)
        s["old_rules_days"] = sum(1 for d in days if d.isoformat() in old)
        out[y["name"]] = s
    return out


def gate(stage: str, s: dict, n: int, best_yard: float) -> tuple[bool, str]:
    need_t = practice_bar(n) if stage == "practice" else check_bar(n)
    min_trades = 15 if stage == "practice" else 20
    why = []
    if s["net"] <= 0:
        why.append(f"lost ${-s['net']:,.0f} after costs")
    if s["trades"] < min_trades:
        why.append(f"{s['trades']} trades (< {min_trades})")
    if s["t_stat"] < need_t:
        why.append(f"t {s['t_stat']:.2f} < bar {need_t:.2f} (N={n})")
    if s["net"] <= best_yard:
        why.append(f"did not beat the best yardstick (${best_yard:,.0f})")
    if s["net"] <= s.get("old_rules", 0):
        why.append(f"did not beat the old rules (${s.get('old_rules', 0):,.0f})")
    if stage == "check" and s["worst_day"] < -600:
        why.append(f"worst day ${s['worst_day']:,.0f} (< -3%)")
    return (not why), "; ".join(why)


# --------------------------------------------------------------------------- proposing
def research_packet(cfg) -> str:
    from asxbot.lab.tsim.rules import param_space

    ideas = load_ideas(cfg)
    fam_fail: dict[str, int] = {}
    for i in ideas[-12:]:
        f = i["spec"].get("family") or i["spec"].get("kind")
        if i["verdict"] == "failed":
            fam_fail[f] = fam_fail.get(f, 0) + 1
    exhausted = [f for f, k in fam_fail.items() if k >= 5]
    lines = [
        f"IDEAS TRIED SO FAR: {n_tried(cfg)} (the bar rises with every one).",
        "FAMILIES AND THEIR PARAMETERS (defaults): " + json.dumps(param_space()),
        "FAMILY NOTES: orb = stocks in play opening-range breakout; gap_fade = fade a no-news "
        "opening gap; vwap_rev = buy a stretch below VWAP; hod_mom = new high of day on heavy "
        "volume; drift = hold days after a price-sensitive announcement (headline types: "
        "results, guidance, acquisition, contract, exploration, clinical, ...); close_strength = "
        "buy strong closers in the closing auction, sell in the next opening auction (or fade "
        "them); pullback = first pullback to VWAP in a strong stock; index_revert = fade a "
        "liquid stock's no-news divergence from the index.",
    ]
    yard = store.read_json(lab_dir(cfg) / "yardsticks.json", {}) or {}
    if yard:
        lines.append("YARDSTICKS (practice window): " + json.dumps(
            {k: {"net": v["net"], "trades": v["trades"], "t": v["t_stat"]} for k, v in
             yard.get("practice", {}).items()}))  # fmt: skip
    if exhausted:
        lines.append(
            "EXHAUSTED (5+ recent failures): "
            + ", ".join(exhausted)
            + " - widen the search: other families, holding periods, signals."
        )
    waiting_ai = sum(1 for i in ideas if i["stage"] == "queued" and i["spec"].get("kind") == "ai")
    lines.append(f"AI-TRADER IDEAS WAITING: {waiting_ai} (each costs ~10 simulated AI days; "
                 "at most "
                 f"{AI_IDEAS_PER_NIGHT} is screened a night) - prefer rules ideas, which are "
                 "nearly free, unless an AI idea is clearly better.")  # fmt: skip
    rp = lab_dir(cfg) / "refused.jsonl"
    if rp.exists():
        refused = [json.loads(x) for x in rp.read_text(encoding="utf-8").splitlines()[-8:]]
        lines.append(
            "YOUR LAST REFUSED PROPOSALS (not tested - fix or change them): "
            + json.dumps([{"spec": r["spec"], "why": r["why"]} for r in refused])
        )
    lines.append("RECENT IDEAS (newest last):")
    for i in ideas[-40:]:
        r = i["results"].get("practice") or {}
        lines.append(json.dumps({"id": i["id"], "spec": i["spec"], "stage": i["stage"],
                                 "verdict": i["verdict"], "why": i["why"][:200],
                                 "practice": {k: r.get(k) for k in ("net", "trades", "t_stat",
                                                                    "win_rate")}}))  # fmt: skip
    return "\n".join(lines)


def propose(cfg, ask=None) -> list[dict]:
    ask = ask or llm.ask
    res = ask(research_packet(cfg) + "\n\nPropose the next ideas.", system=PROPOSER_SYSTEM,
              model=llm.DECIDER[0], effort=llm.DECIDER[1], cfg=cfg, use_cache=False)  # fmt: skip
    d = llm.parse_json(res.get("text", "")) or {}
    out = []
    for x in (d.get("ideas") or [])[:PROPOSE_PER_TICK]:
        if not isinstance(x, dict):
            continue
        spec = {k: x[k] for k in ("kind", "family", "params", "addendum", "describe") if k in x}
        idea = register(cfg, spec, str(x.get("reason", "")), "opus")
        if idea:
            out.append(idea)
        else:
            _refused(cfg, spec)
    return out


def _refused(cfg, spec: dict) -> None:
    """A proposal register() refused (an exact repeat, an unknown family or parameter, an
    addendum over 600 characters): kept so the proposer is told, instead of silently lost."""
    from asxbot.lab.tsim.rules import FAMILIES, param_space

    why = "an exact repeat of an idea already tried"
    if spec.get("kind") == "rules":
        fam = spec.get("family")
        if fam not in FAMILIES:
            why = f"unknown family {fam!r}"
        else:
            bad = [k for k in (spec.get("params") or {}) if k not in param_space()[fam]]
            if bad:
                why = f"unknown parameters for {fam}: {bad}"
    elif spec.get("kind") == "ai" and len(str(spec.get("addendum", ""))) > 600:
        why = "addendum over 600 characters"
    p = lab_dir(cfg) / "refused.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "spec": spec,
                            "why": why}) + "\n")  # fmt: skip


# --------------------------------------------------------------------------- the tick
def ensure_yardsticks(cfg, inputs_for) -> dict:
    p = lab_dir(cfg) / "yardsticks.json"
    y = store.read_json(p, {}) or {}
    sp = S.current(cfg)
    if "practice" not in y:
        days = S.days(cfg, "practice")
        y["practice"] = yardstick_scores(cfg, days, "practice", inputs_for(days))
        store.write_json(p, y)
    if "check" not in y or y.get("check_block") != sp["check"]:
        days = S.days(cfg, "check")
        y["check"] = yardstick_scores(cfg, days, "check", inputs_for(days))
        y["check_block"] = sp["check"]
        store.write_json(p, y)
    return y


def step(cfg, idea: dict, inputs_for, yard: dict) -> dict:
    """Move one idea one stage: practice -> check -> finalist."""
    n = max(1, n_tried(cfg))
    kind = idea["spec"].get("kind")
    if kind == "ai":
        ok, why = llm.budget_ok(cfg)
        if not ok:
            return update(cfg, idea, why=f"waiting for budget: {why}")
    if idea["stage"] == "queued":
        days = sample(S.days(cfg, "practice"), idea["n"], 10 if kind == "ai" else SAMPLE_DAYS)
        s = evaluate(cfg, idea["spec"], days, f"{idea['id']}_practice", inputs_for(days))
        best = _best_yard_on(yard, days, cfg, inputs_for)
        ok, why = gate("practice", s, n, best)
        idea["results"]["practice"] = s
        return update(cfg, idea, stage="check" if ok else "done", verdict="" if ok else "failed",
                      why=why or "passed practice")  # fmt: skip
    if idea["stage"] == "check":
        days = S.days(cfg, "check")
        s = evaluate(cfg, idea["spec"], days, f"{idea['id']}_check", inputs_for(days))
        best = max((v["net"] for v in yard.get("check", {}).values()), default=0.0)
        ok, why = gate("check", s, n, best)
        idea["results"]["check"] = s
        idea = update(cfg, idea, stage="finalist" if ok else "done",
                      verdict="finalist" if ok else "failed",
                      why=why or "passed the check set: a finalist for the sealed block",
                      )  # fmt: skip
        if ok and kind == "rules":
            # SPEND AI WHERE IT PAYS: a rules idea that holds up is handed to the AI trader
            desc = json.dumps(
                {"family": idea["spec"]["family"], "params": idea["spec"].get("params")}
            )
            register(cfg, {"kind": "ai", "addendum": (
                f"A setup that tested well rules-only after costs: {desc[:400]}. Use it, adapt it "
                "or ignore it as you judge.")[:600]}, f"AI trader on {idea['id']}", "search",
                parent=idea["id"])  # fmt: skip
        return idea
    return idea


def _best_yard_on(yard: dict, days: list[date], cfg, inputs_for) -> float:
    """The best yardstick on exactly these days: each yardstick ran once over the whole
    practice window; its daily P&L on the sample's days is summed."""
    keys = [d.isoformat() for d in days]
    return max((sum(v.get("daily", {}).get(k, 0.0) for k in keys)
                for v in yard.get("practice", {}).values()), default=0.0)  # fmt: skip


def sealed_run(cfg, idea: dict, inputs_for) -> dict:
    """A finalist's ONE run on the sealed block."""
    sp = S.current(cfg)
    if idea["id"] in sp["uses"] and "sealed" not in idea["results"]:
        # its one run was cut short (the tick's end or the budget): the same run resumes from
        # its last whole day; it is still one look (27 Sep)
        F = sp["uses"].index(idea["id"]) + 1
    else:
        F = S.use_seal(cfg, idea["id"])
    days = S.days(cfg, "sealed", allow_sealed=True)
    s = evaluate(cfg, idea["spec"], days, f"{idea['id']}_sealed", inputs_for(days))
    from asxbot.lab.winner import t_required

    need = t_required(F)
    fails = []
    if s["net"] <= 0:
        fails.append("not positive after costs")
    if s["trades"] < 40:
        fails.append(f"{s['trades']} trades < 40")
    if s["without_best3"] <= 0 or (s["best3_share_of_gross_profit"] or 1) > 0.5:
        fails.append("carried by its best 3 trades")
    if s["up_days"]["n"] < 5 or s["down_days"]["n"] < 5 or s["up_days"]["pnl"] < 0 or (
            s["down_days"]["pnl"] < 0):  # fmt: skip
        fails.append("not OK on both up and down days")
    if s["max_drawdown_pct"] > 10 or s["worst_day"] < -600:
        fails.append("drawdown or worst day too deep")
    if s["green_days"] <= s["red_days"]:
        fails.append("not more green days than red")
    if s["t_stat"] < need:
        fails.append(f"t {s['t_stat']:.2f} < {need:.2f} (F={F})")
    if s["net"] <= s.get("old_rules", 0):
        fails.append("did not beat the old rules")
    idea["results"]["sealed"] = {**s, "block": sp["sealed"], "F": F}
    passed = not fails
    return update(cfg, idea, stage="shadow" if passed else "retired",
                  verdict="sealed-pass: shadow trading next" if passed else "failed sealed",
                  why="; ".join(fails) or "passed the sealed block")  # fmt: skip


AI_IDEAS_PER_NIGHT = 1


def _order(cfg, todo: list[dict]) -> list[dict]:
    """SPEND AI WHERE IT PAYS: rules ideas first (plain code, almost free), checks before new
    screens; an idea for the AI trader itself costs ~10 simulated AI days, so at most
    `tradesim.search.ai_ideas_per_night` of them are screened per night (Sydney date), and only
    when no rules idea is waiting."""
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo

    conf = ((cfg.get("tradesim") or {}).get("search") or {}) if cfg is not None else {}
    cap = int(conf.get("ai_ideas_per_night", AI_IDEAS_PER_NIGHT))
    night = dt.now(ZoneInfo("Australia/Sydney")).strftime("%Y-%m-%d")
    p = lab_dir(cfg) / "ai_screens.json"
    done = store.read_json(p, {}) or {}
    rules = [i for i in todo if i["spec"].get("kind") != "ai"]
    ai = [i for i in todo if i["spec"].get("kind") == "ai"]
    rules.sort(key=lambda i: (i["stage"] != "check", i["n"]))
    ai.sort(key=lambda i: (i["stage"] != "check", i["n"]))
    if rules or done.get(night, 0) >= cap:
        return rules
    if ai:
        done[night] = done.get(night, 0) + 1
        store.write_json(p, done)
    return ai[:1]


def tick(cfg, max_minutes: float = 50.0, propose_ok: bool = True, ask=None) -> str:
    """One unit of the never-ending search. Returns what it did."""
    import time as wall

    from asxbot.lab.tsim import run as R

    t0 = wall.monotonic()
    cache: dict = {}

    def inputs_for(days):
        k = (min(days), max(days))
        if k not in cache:
            cache[k] = R.prepare_inputs(cfg, days)
        return cache[k]

    did = []
    S.rotate_if_worn(cfg)
    yard = ensure_yardsticks(cfg, inputs_for)
    R.STOP_AT = t0 + max_minutes * 60  # a run stops between days at the tick's end
    try:
        while wall.monotonic() - t0 < max_minutes * 60:
            try:
                if not _tick_one(cfg, did, inputs_for, yard, propose_ok, ask):
                    break
            except (llm.UsageStop, R.OutOfTime) as e:
                # the idea keeps its stage and its run resumes from its last whole day
                did.append(f"paused: {str(e)[:160]}")
                break
    finally:
        R.STOP_AT = None
    write_log(cfg)
    return "; ".join(did) or "nothing to do"


def _tick_one(cfg, did: list, inputs_for, yard: dict, propose_ok: bool, ask) -> bool:
    """One move of the search; False when there is nothing more to do this tick."""
    ideas = load_ideas(cfg)
    todo = _order(cfg, [i for i in ideas if i["stage"] in ("queued", "check")])
    fin = [i for i in ideas if i["stage"] == "finalist"]
    if fin:
        i = sealed_run(cfg, fin[0], inputs_for)
        did.append(f"{i['id']} sealed: {i['verdict']}")
        return True
    if not todo:
        if not propose_ok:
            return False
        try:
            new = propose(cfg, ask)
        except llm.UsageStop as e:
            did.append(f"stopped: {e}")
            return False
        if not new:
            did.append("the proposer gave nothing new")
            return False
        did.append("proposed " + ", ".join(i["id"] for i in new))
        return True
    i = step(cfg, todo[0], inputs_for, yard)
    did.append(f"{i['id']} -> {i['stage']} ({i['why'][:80]})")
    return not i["why"].startswith("waiting for budget")


# --------------------------------------------------------------------------- the log
def write_log(cfg) -> Path:
    ideas = load_ideas(cfg)
    n = n_tried(cfg)
    sp = S.current(cfg)
    yard = store.read_json(lab_dir(cfg) / "yardsticks.json", {}) or {}
    wk = llm.week_state()
    L = ["# Research log: the trading simulator's strategy search (lab/tsim)", "",
         f"Updated {datetime.now():%a %d %b %Y %H:%M}. Ideas tried: **{n}**. Practice bar now "
         f"t >= {practice_bar(n):.2f}; check bar t >= {check_bar(n):.2f}; sealed block "
         f"{sp['sealed'][0]} to {sp['sealed'][1]} (used by {len(sp['uses'])} of {sp['seal_uses']} "
         f"finalists). Lab usage this week: {wk['lab_share']:.1%} of the weekly allowance, "
         f"{wk['calls']} calls.", "", f"_{SURVIVORSHIP}_", "",
         "All money is simulated on stored history; every figure is after brokerage, spread and "
         "slippage (IBKR's fees; other brokers in each run's scores).", ""]  # fmt: skip
    if yard.get("practice"):
        L += [
            "## Yardsticks (practice window, rules only)",
            "",
            "| yardstick | days | trades | net | t | old rules same days |",
            "|---|---|---|---|---|---|",
        ]
        for k, v in yard["practice"].items():
            L.append(
                f"| {k} | {v['days']} | {v['trades']} | ${v['net']:,.0f} | {v['t_stat']:.2f} "
                f"| ${v.get('old_rules', 0):,.0f} |"
            )
        L.append("")
    L += [
        "## Every idea",
        "",
        "| id | what | why it might work | practice | check | sealed | verdict |",
        "|---|---|---|---|---|---|---|",
    ]
    for i in ideas:
        sp_ = i["spec"]
        what = (
            f"{sp_.get('family')} {json.dumps(sp_.get('params') or {})}"
            if sp_.get("kind") == "rules"
            else f"AI: {sp_.get('addendum', '')[:120]}"
            if sp_.get("kind") == "ai"
            else f"new family: {sp_.get('describe', '')[:120]}"
        )
        cell = lambda r: "" if not r else f"${r['net']:,.0f}, {r['trades']} tr, t {r['t_stat']:.2f}"  # noqa: E731
        L.append(
            f"| {i['id']} | {what.replace('|', '/')} | {i['reason'][:140].replace('|', '/')} | "
            f"{cell(i['results'].get('practice'))} | {cell(i['results'].get('check'))} | "
            f"{cell(i['results'].get('sealed'))} | {i['verdict'] or i['stage']}: "
            f"{i['why'][:140].replace('|', '/')} |"
        )
    # reports/research_log.md is the rules-only search's (src/asxbot/search): each has its own
    p = Path(cfg.root) / "reports" / "tsim_research_log.md"
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    return p
