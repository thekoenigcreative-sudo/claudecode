"""Checks the system runs against itself, every watcher cycle.

On 23 September a morning was lost to work that reported itself as working: PDFs that
were terms pages, a screen rejecting most of the day on one arithmetic quirk, a lost edit
that killed every re-look. Every one of those was visible in a file or a log line that
nobody read at the time. These checks read them.

All arithmetic and file checks. No judgement, no model, nothing that needs interpreting:

  pdf_not_pdf        a file under data/announcements/pdf whose first bytes are not %PDF
  pdf_fetch_failed   an announcement_pdf_failures event in the last hour
  screen_dominated   one screen test rejecting more than 80% of the day's announcements
  agent_mismatch     an agent call whose model or thinking level was not what config asks
  stuck_pending      a pending_fill order older than the resolve window plus an hour
  errors_logged      an ERROR line in the last hour

A failure is loud: CRITICAL in the log, an `arena_selfcheck` event, an alerts flag file,
and a Telegram message. The same failure is not repeated more often than
`arena.selfcheck.repeat_minutes`, so a standing fault does not become its own noise.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot.io import write_text_atomic
from asxbot.log import EventLog, get_logger

log = get_logger("asxbot.arena.selfcheck")
SYD = ZoneInfo("Australia/Sydney")

SCREEN_DOMINANCE_PCT = 80.0
SCREEN_MIN_SAMPLE = 10  # below this a percentage says nothing
REPEAT_MINUTES = 60
TAIL_BYTES = 400_000  # how much of an append-only file a check reads back


@dataclass
class Check:
    key: str
    ok: bool
    detail: str
    count: int = 0
    facts: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok


# -- reading back, cheaply --------------------------------------------------
def tail_lines(path: Path, max_bytes: int = TAIL_BYTES) -> list[str]:
    """The last chunk of an append-only file, whole lines only.

    These run every cycle, so they read the end of a file rather than all of it. A check
    that is expensive gets turned off, and a check that is off finds nothing.
    """
    if not path.exists():
        return []
    size = path.stat().st_size
    with open(path, "rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            fh.readline()  # discard the partial first line
        raw = fh.read()
    return raw.decode("utf-8", "replace").splitlines()


def recent_events(data_dir: Path, kind: str, since: datetime) -> list[dict]:
    out = []
    for line in tail_lines(Path(data_dir) / "events" / f"{kind}.jsonl"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            when = datetime.fromisoformat(r["ts"]).astimezone(SYD)
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        if when >= since:
            r["syd"] = when
            out.append(r)
    return out


# -- the checks -------------------------------------------------------------
def check_pdfs_are_pdfs(cfg) -> Check:
    """Any saved announcement document whose first bytes are not %PDF."""
    bad = []
    root = cfg.data_dir / "announcements" / "pdf"
    for p in sorted(root.rglob("*.pdf")) if root.exists() else []:
        try:
            if not p.read_bytes()[:5].startswith(b"%PDF"):
                bad.append(f"{p.parent.name}/{p.name} ({p.stat().st_size} bytes)")
        except OSError as e:  # noqa: PERF203 - a file we cannot read is itself the finding
            bad.append(f"{p.name} unreadable: {e}")
    if not bad:
        return Check("pdf_not_pdf", True, "every saved announcement document is a PDF")
    return Check(
        "pdf_not_pdf",
        False,
        f"{len(bad)} saved 'PDF' file(s) are not PDFs: {', '.join(bad[:5])}"
        + (" ..." if len(bad) > 5 else ""),
        count=len(bad),
        facts={"files": bad[:20]},
    )


def check_pdf_fetches(cfg, now: datetime) -> Check:
    """PDF fetches that failed in the last hour. The agents judge headlines without one."""
    fails = recent_events(cfg.data_dir, "announcement_pdf_failures", now - timedelta(hours=1))
    if not fails:
        return Check("pdf_fetch_failed", True, "no PDF fetch failed in the last hour")
    codes = ", ".join(sorted({str(f.get("code", "?")) for f in fails})[:8])
    return Check(
        "pdf_fetch_failed",
        False,
        f"{len(fails)} PDF fetch(es) failed in the last hour ({codes}); those agents are "
        f"judging headlines with no document",
        count=len(fails),
    )


def check_screen_not_dominated(cfg, now: datetime) -> Check:
    """One screen test rejecting most of the day.

    Not "how often did it fire" but "what share of everything did this one test stop".
    The 1% tick limit rejected 32 of 47 on the first morning because it was arithmetically
    a 50-cent price floor, and a count alone never said so.
    """
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = [
        r
        for r in recent_events(cfg.data_dir, "arena_screened", day_start)
        if not r.get("is_test")
    ]
    total = len({str(r.get("ids_id")) for r in rows})
    if total < SCREEN_MIN_SAMPLE:
        return Check(
            "screen_dominated", True, f"only {total} screened today; too few to judge a share"
        )
    by_test: dict[str, set] = {}
    for r in rows:
        if r.get("ok"):
            continue
        by_test.setdefault(str(r.get("test") or "?"), set()).add(str(r.get("ids_id")))
    worst, ids = "", set()
    for t, s in by_test.items():
        if len(s) > len(ids):
            worst, ids = t, s
    share = len(ids) / total * 100 if total else 0.0
    facts = {"total": total, "by_test": {t: len(s) for t, s in by_test.items()}}
    if share <= SCREEN_DOMINANCE_PCT:
        return Check(
            "screen_dominated",
            True,
            f"no single screen test dominates ({worst or 'none'} is the largest at "
            f"{share:.0f}% of {total})",
            facts=facts,
        )
    return Check(
        "screen_dominated",
        False,
        f"the '{worst}' screen test rejected {len(ids)} of {total} announcements today "
        f"({share:.0f}%). Work out what that test excludes arithmetically before trusting it",
        count=len(ids),
        facts=facts,
    )


def check_agent_calls(cfg, now: datetime, expected: dict[str, tuple[str, str]]) -> Check:
    """Every agent call ran on the model and the effort level config asks for.

    `expected` maps agent -> (model, thinking). A silently downgraded model or a thinking
    level that stopped applying is invisible in the output but changes every decision.
    """
    calls = recent_events(cfg.data_dir, "arena_agent_calls", now - timedelta(hours=1))
    bad = []
    for c in calls:
        agent = str(c.get("agent", "?"))
        want_model, want_think = expected.get(agent, ("", ""))
        ran, think = str(c.get("model_ran", "")), str(c.get("thinking", ""))
        if want_model and ran and ran.replace("anthropic/", "") != want_model.replace(
            "anthropic/", ""
        ):
            bad.append(f"{agent} ran {ran}, config says {want_model}")
        elif want_think and think and think != want_think:
            bad.append(f"{agent} ran at {think} effort, config says {want_think}")
    if not bad:
        return Check("agent_mismatch", True, f"{len(calls)} agent call(s) matched config")
    return Check(
        "agent_mismatch",
        False,
        f"{len(bad)} agent call(s) did not match config: {'; '.join(sorted(set(bad))[:4])}",
        count=len(bad),
    )


def check_pending_orders(arena, pb, now: datetime) -> Check:
    """A pending_fill order older than the resolve window plus an hour is stuck."""
    limit = timedelta(minutes=arena.broker.resolve_after_minutes) + timedelta(hours=1)
    stuck = []
    for kind in ("agent", "bot"):
        acct = arena.account(pb, kind)
        for o in acct.orders.values():
            if o.status != "pending_fill":
                continue
            decided = datetime.fromisoformat(o.decision_at)
            if decided.tzinfo is None:
                decided = decided.replace(tzinfo=SYD)
            age = now - decided
            if age > limit:
                stuck.append(
                    f"{o.order_id} {o.side} {o.ticker} decided {decided:%d %b %H:%M}, "
                    f"{age.total_seconds() / 3600:.1f}h ago"
                )
    if not stuck:
        return Check("stuck_pending", True, "no pending order is past its resolve window")
    return Check(
        "stuck_pending",
        False,
        f"{len(stuck)} order(s) still pending long after the fill should have resolved: "
        + "; ".join(stuck[:4]),
        count=len(stuck),
    )


ERROR_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ (ERROR|CRITICAL) (\S+): (.*)")

# The checks' own voices. Without this the error check feeds on itself: raising an alert
# logs CRITICAL, the next cycle counts that line as an error, raises another, and the count
# climbs on its own - 8, 10, 12 - with nothing actually wrong. Seen within a minute of
# turning these on.
SELF_LOGGERS = ("asxbot.alerts", "asxbot.arena.selfcheck")


def check_errors_logged(cfg, now: datetime) -> Check:
    """Any ERROR or CRITICAL logged in the last hour.

    An exception that only reaches the log has, in practice, not been reported at all.
    """
    since = now - timedelta(hours=1)
    hits = []
    for line in tail_lines(cfg.data_dir / "logs" / "asxbot.log"):
        m = ERROR_LINE.match(line)
        if not m:
            continue
        logger = m.group(3)
        if logger in SELF_LOGGERS:
            continue  # an alert about an error is not itself an error
        when = datetime.fromisoformat(m.group(1)).replace(tzinfo=SYD)
        if when >= since:
            hits.append(f"{m.group(1)[11:]} {logger}: {m.group(4)[:110]}")
    if not hits:
        return Check("errors_logged", True, "no ERROR logged in the last hour")
    return Check(
        "errors_logged",
        False,
        f"{len(hits)} ERROR line(s) in the last hour: " + " | ".join(hits[-3:]),
        count=len(hits),
    )


# -- running them -----------------------------------------------------------
def run_checks(arena, pb, now: datetime | None = None) -> list[Check]:
    """Every check, in one pass. Never raises: a check that breaks is itself a failure."""
    from asxbot.arena.watch import DECIDER_MODEL, READER_MODEL

    now = (now or datetime.now(SYD)).astimezone(SYD)
    cfg = arena.cfg
    effort = cfg.get("arena.agents.effort") or {}
    expected = {
        "trader-reader": (READER_MODEL, str(effort.get("reader", "low"))),
        "trader-decider": (DECIDER_MODEL, str(effort.get("decider", "high"))),
    }
    runners = [
        ("pdf_not_pdf", lambda: check_pdfs_are_pdfs(cfg)),
        ("pdf_fetch_failed", lambda: check_pdf_fetches(cfg, now)),
        ("screen_dominated", lambda: check_screen_not_dominated(cfg, now)),
        ("agent_mismatch", lambda: check_agent_calls(cfg, now, expected)),
        ("stuck_pending", lambda: check_pending_orders(arena, pb, now)),
        ("errors_logged", lambda: check_errors_logged(cfg, now)),
    ]
    out = []
    for key, fn in runners:
        try:
            out.append(fn())
        except Exception as e:  # noqa: BLE001
            out.append(Check(key, False, f"the check itself failed: {type(e).__name__}: {e}"))
    return out


def _state_path(cfg) -> Path:
    return cfg.data_dir / "arena" / "selfcheck_state.json"


def _load_state(cfg) -> dict:
    p = _state_path(cfg)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def report(arena, pb, now: datetime | None = None, force: bool = False) -> list[Check]:
    """Run the checks and make every failure loud. Returns the failures."""
    from asxbot.alerts import Alerts
    from asxbot.arena import notify

    now = (now or datetime.now(SYD)).astimezone(SYD)
    cfg = arena.cfg
    checks = run_checks(arena, pb, now)
    failures = [c for c in checks if not c.ok]
    alerts = Alerts(cfg.data_dir)
    events = EventLog(cfg.data_dir)
    state = _load_state(cfg)
    repeat = timedelta(minutes=int(cfg.get("arena.selfcheck.repeat_minutes", REPEAT_MINUTES)))
    alert = notify.get(arena)

    for c in checks:
        if c.ok:
            if state.pop(c.key, None) is not None:
                alerts.clear(c.key)
                log.info("self-check %s is clear again: %s", c.key, c.detail)
            continue
        last = state.get(c.key)
        due = force or last is None or (now - datetime.fromisoformat(last)) >= repeat
        events.append(
            "arena_selfcheck",
            {"check": c.key, "ok": False, "detail": c.detail, "count": c.count, **c.facts},
        )
        if due:
            # Only shout when it is due. The watcher runs every minute, and a standing
            # fault logged every minute is the log nobody reads all over again.
            log.critical("SELF-CHECK FAILED [%s] %s", c.key, c.detail)
            alerts.raise_alert(c.key, c.detail)
        else:
            log.info("self-check %s still failing (already reported): %s", c.key, c.detail)
        if due and alert:
            alert.send(f"🚨 <b>SELF-CHECK: {c.key}</b>\n{notify.escape_text(c.detail)}")
            state[c.key] = now.isoformat(timespec="seconds")
        elif due:
            state[c.key] = now.isoformat(timespec="seconds")
    write_text_atomic(json.dumps(state, indent=2), _state_path(cfg))
    return failures
