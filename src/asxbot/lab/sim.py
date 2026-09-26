"""The time machine: one trading day for one variant (PRACTICE_LAB.md 1).

`run_day` hands the variant's playbooks (the frozen blocks with its changes) and, when its
agent book is being run, the lab's agent to the replay engine (arena/replay_ibkr.replay_day),
so every decision - rule bot and agent - goes through the live code and sees only what
existed at that simulated minute. The record keeps each book separately:
  {"day", "gaps", "market_move_pct", "v2": {"books": {"bot": ...}},
   "daytrader": {"books": {"bot": ..., "agent": ...}, "setups": ...}, "agent_stats": ...}
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from asxbot.lab import agentcall, splits
from asxbot.lab.anon import Anon
from asxbot.lab.variants import BASELINE, apply

ADDENDUM_HEAD = "\n\nADDED INSTRUCTIONS FOR THIS PLAYBOOK:\n"
MEMBERSHIP = re.compile(r"^(\s*)S[0-9A-F]{4} is (IN|NOT in) the S&P/ASX 200 according to .*$", re.M)


def playbooks_for(cfg, variant: dict | None):
    from asxbot.arena.levels import load_playbook

    v2pb = load_playbook(cfg, "asx_announcements_v2")
    dtpb = load_playbook(cfg, "asx_daytrader")
    if variant and variant.get("id") != BASELINE and variant.get("overrides"):
        if variant["playbook"] == "asx_daytrader":
            dtpb = apply(dtpb, variant["overrides"])
        else:
            v2pb = apply(v2pb, variant["overrides"])
    return v2pb, dtpb


class LabAgent:
    """Stands in for daytrader.ask_agent: the same packet the live decider gets (anonymised
    before the cutoff), answered by agentcall.ask, the answer's latency added to the clock."""

    def __init__(
        self,
        cfg,
        day: date,
        prompt_addendum: str = "",
        anon: Anon | None = None,
        stop_at: float | None = agentcall.USAGE_STOP,
    ):
        self.cfg = cfg
        self.day = day
        self.anon = anon
        self.model, self.effort = agentcall.model_and_effort(cfg)
        self.system = agentcall.standing_instructions(cfg) + (
            ADDENDUM_HEAD + prompt_addendum if prompt_addendum else ""
        )
        self.stop_at = stop_at
        self.stats = {
            "calls": 0,
            "cached": 0,
            "cost_usd": 0.0,
            "takes": 0,
            "rejects": 0,
            "errors": 0,
        }
        self.decisions: list[dict] = []

    def packet(self, arena, pb, s, t, context, now, data) -> str:
        from asxbot.arena import daytrader
        from asxbot.arena.watch import _membership

        if self.anon is None:
            return daytrader.agent_packet(arena, pb, s, t, context, now, data)
        a = self.anon
        text = daytrader.agent_packet(
            arena,
            pb,
            a.setup(s),
            a.terms(s.ticker, t),
            a.context(s.ticker, context),
            now,
            a.text(s.ticker, data),
        )
        real = _membership(arena, s.ticker)
        where = "IN" if " is IN the " in real else "NOT in"
        text = MEMBERSHIP.sub(
            lambda m: f"{m.group(1)}{a.alias(s.ticker)} is {where} the S&P/ASX 200", text
        )
        return a.dates(text)

    def __call__(self, arena, pb, s, t, context, now, data: str = "") -> dict:
        limit_s = int((pb.raw.get("agent") or {}).get("timeout_s", 60))
        res = agentcall.ask(
            self.packet(arena, pb, s, t, context, now, data),
            model=self.model,
            effort=self.effort,
            system=self.system,
            stop_at=self.stop_at,
        )
        self.stats["calls"] += 1
        self.stats["cached"] += int(bool(res.get("cached")))
        self.stats["cost_usd"] = round(
            self.stats["cost_usd"] + (0 if res.get("cached") else res.get("cost_usd", 0)), 5
        )
        secs = float(res.get("seconds") or 0)
        clock = getattr(arena.broker, "clock", None)
        if hasattr(clock, "t"):
            clock.t = clock.t + timedelta(seconds=min(secs, limit_s + 15))
        if res.get("error"):
            self.stats["errors"] += 1
            return {
                "action": "reject",
                "why": f"lab call failed: {res['error'][:120]}",
                "model": res.get("model", ""),
            }
        if secs > limit_s:
            self.stats["rejects"] += 1
            return {
                "action": "reject",
                "why": f"answered after {secs:.0f}s, past {limit_s}s",
                "model": res["model"],
                "seconds": secs,
            }
        d = res.get("decision") or {}
        action = str(d.get("action", "reject")).lower()
        take = action in ("take", "trade", "confirm")
        self.stats["takes" if take else "rejects"] += 1
        self.decisions.append(
            {
                "key": s.key,
                "bar": s.trigger_bar,
                "action": "take" if take else "reject",
                "why": str(d.get("why", ""))[:200],
            }
        )
        stop = d.get("stop")
        if take and stop not in (None, "") and self.anon is not None:
            try:
                stop = self.anon.unprice(s.ticker, float(stop))
            except (TypeError, ValueError):
                stop = None
        return {
            "action": "take" if take else "reject",
            "stop": stop,
            "why": str(d.get("why", "")),
            "model": res.get("model", ""),
            "seconds": round(secs, 1),
        }


def run_day(
    cfg,
    day: date,
    variant: dict,
    *,
    books: list[str],
    anon: bool,
    codes,
    shorts,
    ann,
    hist,
    universe,
    salt: str = "lab",
    day_number: int = 0,
    allow_locked: bool = False,
    stop_at: float | None = agentcall.USAGE_STOP,
) -> dict:
    from asxbot.arena.replay_ibkr import replay_day

    splits.check_days([day], allow_locked)
    if (
        "agent" in books
        and not anon
        and not splits.is_post_cutoff(day)
        and variant.get("mode") != "contamination"
    ):
        # CLAUDE.md: no AI classification of historical announcements. Before the cutoff the
        # agent sees the day only anonymised (raw only to MEASURE contamination).
        raise ValueError(
            f"{day} is before the models' knowledge cutoff: the agent may see it only anonymised"
        )
    v2pb, dtpb = playbooks_for(cfg, variant)
    agent = None
    if "agent" in books:
        agent = LabAgent(
            cfg,
            day,
            variant.get("agent_prompt") or "",
            Anon(day, salt, day_number) if anon else None,
            stop_at=stop_at,
        )
    rec = replay_day(
        cfg, day, codes, shorts, ann, hist, universe=universe, v2pb=v2pb, dtpb=dtpb, agent=agent
    )
    for key in ("v2", "daytrader"):
        if key in rec:
            part = rec[key]
            agent_book = part.pop("agent_book", None)
            extras = {
                k: part.pop(k)
                for k in ("setups", "news_candidates", "state", "candidates")
                if k in part
            }
            part_books = {"bot": part}
            if agent_book is not None:
                part_books["agent"] = agent_book
            rec[key] = {"books": part_books, **extras}
    rec["variant"] = variant.get("id")
    rec["anon"] = anon
    if agent is not None:
        rec["agent_stats"] = agent.stats
        rec["agent_decisions"] = agent.decisions
    return rec
