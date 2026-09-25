"""What each screen filter costs: the forward return of everything it threw away.

The screen rejects most announcements before any model sees them, which is the point - but
a rejection count says only how often a rule fired, never what it excluded. This measures
the exclusions: for every rejection in `arena_screened.jsonl`, what the stock actually did
over the next 10 sessions against the index.

It REPORTS. It changes nothing. No threshold moves because of a number in here, and a rule
that looks expensive earns a dated config change made deliberately, never an automatic one.
Read `Verification` in CLAUDE.md before acting on any of it.

Two honest limits, printed with every report:
  * a rejection needs 10 completed sessions after it before it can be scored, so recent
    days are counted as "not yet measurable" rather than quietly dropped;
  * the prices are yfinance, so a delisted stock is simply absent. That biases this
    measurement the same way it biases everything else here - upward.

Since 26 Sep 2026 (H13): each playbook's screen is scored on its own (v1's retired records
no longer mix with v2's), and a v2 `deferred` record - an announcement put off while the
live feed was down, screened again when it was back - is not a rejection and is left out.
The 10-session horizon is v1's holding period; v2 holds intraday, so its rows measure a
horizon it does not trade. Changing the horizon is a change to the measurement and waits
for Rick's OK; the report says so.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot.log import get_logger

log = get_logger("asxbot.arena.filtercost")
SYD = ZoneInfo("Australia/Sydney")
HORIZON = 10  # sessions: v1's holding period (v2 and the day trader hold intraday)
# Screen records that are not verdicts (watch._handle_v2: the feed was down).
NOT_A_VERDICT = ("deferred",)
V1 = "asx_announcements (v1, retired)"
V2 = "asx_announcements_v2"


def playbook_of(r: dict) -> str:
    """Which playbook's screen wrote a record: v2's are marked `v2`, v1's are not."""
    return V2 if r.get("v2") else V1


@dataclass
class TestCost:
    test: str
    rejected: int = 0
    playbook: str = ""
    measured: int = 0
    pending: int = 0  # not enough forward sessions yet
    no_prices: int = 0
    rel_returns: list = field(default_factory=list)  # stock minus index, per cent
    best: tuple = ()  # (ticker, date, rel_return)

    @property
    def median(self) -> float:
        if not self.rel_returns:
            return float("nan")
        s = sorted(self.rel_returns)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    @property
    def mean(self) -> float:
        return sum(self.rel_returns) / len(self.rel_returns) if self.rel_returns else float("nan")

    @property
    def win_rate(self) -> float:
        if not self.rel_returns:
            return float("nan")
        return sum(1 for r in self.rel_returns if r > 0) / len(self.rel_returns) * 100

    @property
    def big_movers(self) -> int:
        """How many rejected stocks went on to beat the index by 10% or more."""
        return sum(1 for r in self.rel_returns if r >= 10.0)


def rejections(data_dir: Path, weeks: int, now: datetime | None = None) -> list[dict]:
    """Every screen rejection in the window, deduplicated by announcement."""
    now = (now or datetime.now(SYD)).astimezone(SYD)
    since = now - timedelta(weeks=weeks)
    p = Path(data_dir) / "events" / "arena_screened.jsonl"
    if not p.exists():
        return []
    seen, out = set(), []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                when = datetime.fromisoformat(r["ts"]).astimezone(SYD)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if when < since or r.get("ok") or r.get("is_test"):
                continue
            if r.get("test") in NOT_A_VERDICT:
                continue  # put off while the feed was down, not screened out (H13)
            key = (str(r.get("ids_id")), str(r.get("test")), playbook_of(r))
            if key in seen:
                continue
            seen.add(key)
            out.append({**r, "when": when})
    return out


def forward_rel_return(daily, index, on: datetime, horizon: int = HORIZON):
    """Stock return minus index return over `horizon` sessions from the rejection.

    None when the stock has no prices, and "pending" when the sessions have not happened
    yet - the two are different facts and are counted separately.
    """
    if daily is None or len(daily) == 0 or index is None or len(index) == 0:
        return None, "no_prices"
    day = on.date()

    def window(frame):
        after = frame[frame.index.date >= day]
        if len(after) == 0:
            return None, "pending"
        if len(after) <= horizon:
            return None, "pending"
        start = float(after["close"].iloc[0])
        end = float(after["close"].iloc[horizon])
        if start <= 0:
            return None, "no_prices"
        return (end / start - 1) * 100, ""

    s, why = window(daily)
    if s is None:
        return None, why
    i, why = window(index)
    if i is None:
        return None, why
    return s - i, ""


def cost_key(playbook: str, test: str) -> str:
    """How measure() keys a test: 'tick (asx_announcements_v2)'."""
    return f"{test} ({playbook})"


def measure(arena, weeks: int = 4, now: datetime | None = None) -> dict[str, TestCost]:
    """Group every rejection by the playbook and the test that made it, and score what it
    threw away. Keyed by cost_key(playbook, test)."""
    now = (now or datetime.now(SYD)).astimezone(SYD)
    cfg = arena.cfg
    daily_lookup = arena.daily_lookup()
    index = daily_lookup(str(cfg.get("backtest.index_ticker", "^AXJO")))
    costs: dict[str, TestCost] = defaultdict(lambda: TestCost(test="?"))
    cache: dict[str, object] = {}

    for r in rejections(cfg.data_dir, weeks, now):
        test = str(r.get("test") or "?")
        playbook = playbook_of(r)
        c = costs[cost_key(playbook, test)]
        c.test, c.playbook = test, playbook
        c.rejected += 1
        ticker = str(r.get("ticker", "")).upper()
        if ticker not in cache:
            cache[ticker] = daily_lookup(ticker)
        rel, why = forward_rel_return(cache[ticker], index, r["when"])
        if rel is None:
            if why == "pending":
                c.pending += 1
            else:
                c.no_prices += 1
            continue
        c.measured += 1
        c.rel_returns.append(rel)
        if not c.best or rel > c.best[2]:
            c.best = (ticker, r["when"].date().isoformat(), rel)
    return dict(costs)


def render(costs: dict[str, TestCost], weeks: int, now: datetime | None = None) -> str:
    now = (now or datetime.now(SYD)).astimezone(SYD)
    L = [
        "# What the screen filters cost",
        "",
        f"_Generated {now:%Y-%m-%d %H:%M}. Window: the last {weeks} week(s). "
        f"Horizon: {HORIZON} sessions after the rejection, stock minus index._",
        "",
        "**This is a measurement, not an instruction.** No threshold moves because of a "
        "number here. A rule that looks expensive earns a dated change in `config.yaml`, "
        "made deliberately and labelled as such.",
        "",
        "Prices are yfinance: delisted stocks are absent, so these figures are biased "
        "upward like everything else in this repo.",
        "",
        f"Each playbook's screen is scored on its own. The {HORIZON}-session horizon is v1's "
        "holding period; v2 is flat by the close, so its rows measure a horizon v2 does not "
        "trade (changing it waits for Rick's OK). Announcements deferred while the live "
        "feed was down are not rejections and are not counted.",
        "",
        "| playbook | screen test | rejected | measurable | median rel % | mean rel % | "
        "beat index % | 10%+ movers | biggest one that got away |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]

    def f(x, nd=1):
        return "n/a" if x != x else f"{x:,.{nd}f}"

    for key in sorted(costs, key=lambda k: (costs[k].playbook, -costs[k].rejected)):
        c = costs[key]
        best = (
            f"{c.best[0]} {c.best[1]}: {c.best[2]:+.1f}%" if c.best else "—"
        )
        L.append(
            f"| {c.playbook or '?'} | {c.test} | {c.rejected} | {c.measured} | "
            f"{f(c.median, 2)} | {f(c.mean, 2)} | {f(c.win_rate)} | {c.big_movers} | {best} |"
        )
    total = sum(c.rejected for c in costs.values())
    pending = sum(c.pending for c in costs.values())
    missing = sum(c.no_prices for c in costs.values())
    L += [
        "",
        f"- {total} rejection(s) in the window; {pending} cannot be scored yet "
        f"({HORIZON} sessions have not passed since), and {missing} had no cached prices.",
        "- `median rel %` is the middle outcome, which is the number to read first: a mean "
        "on a handful of small caps is one lottery ticket away from meaningless.",
        "- A filter throwing away winners shows up as a positive median with a real sample "
        "behind it. A filter doing its job shows up as a median at or below zero.",
    ]
    return "\n".join(L) + "\n"
