"""Checks the system runs against itself, every watcher cycle.

On 23 September a morning was lost to work that reported itself as working: PDFs that
were terms pages, a screen rejecting most of the day on one arithmetic quirk, a lost edit
that killed every re-look. Every one of those was visible in a file or a log line that
nobody read at the time. These checks read them.

All arithmetic and file checks. No judgement, no model, nothing that needs interpreting:

  pdf_not_pdf        a file under data/announcements/pdf whose first bytes are not %PDF
  pdf_fetch_failed   an announcement_pdf_failures event in the last hour, with its
                     reason: a fetch that failed, or a reader called with no document
  screen_dominated   one screen test rejecting more than 80% of the day's announcements
  agent_mismatch     an agent call whose model or thinking level was not what config asks
  agent_unavailable  agent calls that got no answer in the last hour, by kind (usage_limit,
                     timeout, error) and since when; the plan's usage limit is told once
                     when it starts and once when calls are answered again (26 Sep 2026)
  stuck_pending      a pending_fill order older than the resolve window plus an hour
  fill_before_order  a filled order whose fill bar does not start strictly after the order
                     was recorded (or, for a stop or target, after it began resting)
  errors_logged      an ERROR line in the watcher's own log (arena_watch.log, 26 Sep 2026;
                     it was the shared asxbot.log) in the last hour; repeats of one kind of
                     error within 30 minutes are one finding
  log_silent         the watcher logged something 5+ minutes after the last line in
                     arena_watch.log (or the launcher's arena_warmup.log): the file is not
                     being written. Google Drive did exactly this at 08:14 on 24 Sep 2026
  short_universe     the ASX 200 list is short, stale, or not a constituent list at all
  console_launcher   the venv's pythonw.exe, which every scheduled task starts, is a console
                     program, so each start flashes a window (scripts/install_gui_launcher.py)
  live_data          with data.live_provider: ibkr, IBKR prices are not available (IB
                     Gateway down, logged out, cut off from IBKR, or sending delayed data),
                     so new entries are paused, or the status has gone stale in market hours.
                     While the connection doctor has an episode open, this and
                     gateway_supervisor are recorded but not sent: the doctor and the
                     Gateway supervisor tell Rick (26 Sep 2026)

A failure is loud: CRITICAL in the log, an `arena_selfcheck` event, an alerts flag file,
and a Telegram message. The same failure is not repeated more often than
`arena.selfcheck.repeat_minutes`, so a standing fault does not become its own noise.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot.arena.agents import same_model
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
    items: list = field(default_factory=list)  # the individual findings, for deduping

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
# path -> (size, mtime_ns, finding or "") for every saved PDF already looked at.
_PDF_SEEN: dict[str, tuple[int, int, str]] = {}


def _pdf_entries(root: Path):
    """Every *.pdf under root, with the stat the directory listing already carries (on
    Windows os.scandir needs no extra call per file)."""
    try:
        it = os.scandir(root)
    except OSError:
        return
    with it:
        for e in it:
            if e.is_dir(follow_symlinks=False):
                yield from _pdf_entries(Path(e.path))
            elif e.name.lower().endswith(".pdf"):
                yield e


def check_pdfs_are_pdfs(cfg) -> Check:
    """Any saved announcement document whose first bytes are not %PDF.

    Reads five bytes of each file, once (26 Sep 2026). It used to read every stored PDF
    whole, every cycle: 250 MB a minute on 26 Sep, heading for 1 GB by the end of the test.
    A file is read again only if its size or modified time changes; a finding stands until
    the file is replaced or deleted."""
    bad = []
    root = cfg.data_dir / "announcements" / "pdf"
    present = set()
    for e in sorted(_pdf_entries(root), key=lambda e: e.path):
        present.add(e.path)
        p = Path(e.path)
        try:
            st = e.stat()
        except OSError as err:  # noqa: PERF203 - a file we cannot read is itself the finding
            bad.append(f"{p.name} unreadable: {err}")
            continue
        seen = _PDF_SEEN.get(e.path)
        if seen is None or seen[:2] != (st.st_size, st.st_mtime_ns):
            try:
                with open(p, "rb") as fh:
                    head = fh.read(5)
            except OSError as err:
                bad.append(f"{p.name} unreadable: {err}")  # not remembered: read next time
                continue
            finding = "" if head.startswith(b"%PDF") else \
                f"{p.parent.name}/{p.name} ({st.st_size} bytes)"  # fmt: skip
            seen = (st.st_size, st.st_mtime_ns, finding)
            _PDF_SEEN[e.path] = seen
        if seen[2]:
            bad.append(seen[2])
    for gone in [k for k in _PDF_SEEN if k.startswith(str(root)) and k not in present]:
        _PDF_SEEN.pop(gone, None)
    if not bad:
        return Check("pdf_not_pdf", True, "every saved announcement document is a PDF")
    return Check(
        "pdf_not_pdf",
        False,
        f"{len(bad)} saved 'PDF' file(s) are not PDFs: {', '.join(bad[:5])}"
        + (" ..." if len(bad) > 5 else ""),
        count=len(bad),
        facts={"files": bad[:20]},
        items=bad,
    )


def check_pdf_fetches(cfg, now: datetime) -> Check:
    """PDF fetches that failed in the last hour. The agents judge headlines without one."""
    fails = recent_events(cfg.data_dir, "announcement_pdf_failures", now - timedelta(hours=1))
    if not fails:
        return Check("pdf_fetch_failed", True, "no PDF fetch failed in the last hour")
    from collections import Counter

    def why(f: dict) -> str:
        # Older events carry only the exception text; newer ones a short reason (#8).
        return str(f.get("reason") or f.get("error") or "no reason recorded")[:80]

    items = sorted({f"{f.get('code')} {f.get('ids_id')} ({why(f)})" for f in fails})
    reasons = Counter(why(f) for f in fails)
    by_reason = "; ".join(f"{r} x{n}" for r, n in reasons.most_common(4))
    return Check(
        "pdf_fetch_failed",
        False,
        f"{len(fails)} PDF fetch(es) failed in the last hour - {by_reason} - "
        f"({', '.join(items[:4])}); those agents are judging headlines with no document",
        count=len(fails),
        items=items,
    )


def check_screen_not_dominated(cfg, now: datetime) -> Check:
    """One screen test rejecting most of the day.

    Not "how often did it fire" but "what share of everything did this one test stop".
    The 1% tick limit rejected 32 of 47 on the first morning because it was arithmetically
    a 50-cent price floor, and a count alone never said so.
    """
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # A deferral (the live feed was down; the announcement is screened again once it is
    # back) is not a verdict of any screen test (26 Sep 2026): left out of both counts.
    rows = [
        r
        for r in recent_events(cfg.data_dir, "arena_screened", day_start)
        if not r.get("is_test") and str(r.get("test") or "") != "deferred"
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
    Only calls from the last hour are judged, so a call logged under an earlier, since
    changed setting stops counting an hour later and the event log is never edited.
    """
    calls = recent_events(cfg.data_dir, "arena_agent_calls", now - timedelta(hours=1))
    bad = []
    for c in calls:
        agent = str(c.get("agent", "?"))
        want_model, want_think = expected.get(agent, ("", ""))
        ran, think = str(c.get("model_ran", "")), str(c.get("thinking", ""))
        if want_model and ran and not same_model(ran, want_model):
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
        items=sorted(set(bad)),
    )


AGENT_KINDS = ("usage_limit", "timeout", "error")


def check_agent_unavailable(cfg, now: datetime) -> Check:
    """Agent calls that got no answer in the last hour (arena_agent_calls records with ok
    false, 26 Sep 2026), by why - usage_limit (the Claude plan refused), timeout or error -
    and since when. A reader or decider that cannot be reached is not a pass; the decisions
    it would have made are simply not being made."""
    calls = recent_events(cfg.data_dir, "arena_agent_calls", now - timedelta(hours=1))
    failed = [c for c in calls if c.get("ok") is False]
    if not failed:
        return Check("agent_unavailable", True,
                     f"{len(calls)} agent call(s) in the last hour, none unanswered")  # fmt: skip
    kinds: dict[str, list] = {}
    for c in failed:
        kind = str(c.get("kind") or "error")
        kinds.setdefault(kind if kind in AGENT_KINDS else "error", []).append(c)
    parts = []
    for kind in AGENT_KINDS:
        rows = kinds.get(kind)
        if rows:
            agents = ", ".join(sorted({str(r.get("agent", "?")) for r in rows}))
            parts.append(f"{kind} x{len(rows)} since {rows[0]['syd']:%H:%M} "
                         f"(last {rows[-1]['syd']:%H:%M}; {agents})")  # fmt: skip
    return Check(
        "agent_unavailable", False,
        f"{len(failed)} agent call(s) got no answer in the last hour: " + "; ".join(parts),
        count=len(failed),
        facts={"kinds": {k: len(v) for k, v in kinds.items()}},
        items=sorted(kinds),  # a new KIND is news; another call failing the same way is not
    )


def usage_limit_state(cfg, now: datetime, hours: int = 6) -> dict:
    """The Claude plan's usage limit from the calls themselves: {"since": first refusal of
    the current run, "active": no call has succeeded since the last refusal, "back": the
    first success after it}."""
    calls = sorted(recent_events(cfg.data_dir, "arena_agent_calls", now - timedelta(hours=hours)),
                   key=lambda r: r["syd"])  # fmt: skip
    last_limit = None
    for i, c in enumerate(calls):
        if c.get("ok") is False and str(c.get("kind")) == "usage_limit":
            last_limit = i
    if last_limit is None:
        return {}
    first = last_limit
    while first > 0 and not calls[first - 1].get("ok"):
        first -= 1  # back through the run of failures to where it started
    since = next(c["syd"] for c in calls[first:] if str(c.get("kind")) == "usage_limit")
    back = next((c["syd"] for c in calls[last_limit + 1:] if c.get("ok")), None)
    return {"since": since, "active": back is None, "back": back}


def _could_first_fill(decided: datetime) -> datetime:
    """When an order could first fill: at its decision, or at the next session's open if it
    was decided outside one. An order placed at 07:30 cannot fill before 10:00, and is not
    late at 09:00 - the yardstick places every entry that way."""
    from datetime import time

    from asxbot.announcements.live import is_trading_day

    local = decided.astimezone(SYD)
    d = local.date()
    if is_trading_day(d) and local.time() < time(10, 0):
        return datetime.combine(d, time(10, 0), tzinfo=SYD)
    if is_trading_day(d) and local.time() <= time(16, 10):
        return decided
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return datetime.combine(d, time(10, 0), tzinfo=SYD)


def check_pending_orders(arena, pb, now: datetime) -> Check:
    """A pending_fill order still waiting the resolve window plus an hour after it could
    first have filled is stuck."""
    limit = timedelta(minutes=arena.broker.resolve_after_minutes) + timedelta(hours=1)
    stuck = []
    for acct in _books(arena, pb):
        for o in acct.orders.values():
            if o.status != "pending_fill":
                continue
            decided = datetime.fromisoformat(o.decided_at)
            if decided.tzinfo is None:
                decided = decided.replace(tzinfo=SYD)
            # An order that is being worked bar by bar (a large order in a thin stock fills
            # across many, since 2026-09-24) is stuck only if the bars stopped coming.
            # A stop or target exit still short at the close carries into the next session;
            # from the bar after 16:10 it next could fill at the next open, not "now".
            if o.worked_through:
                after = datetime.fromisoformat(o.worked_through) + timedelta(minutes=1)
                decided = max(decided, after)
            age = now - _could_first_fill(decided)
            if age > limit:
                stuck.append(
                    f"{o.order_id} {o.side} {o.ticker} decided {decided:%d %b %H:%M}, "
                    f"{age.total_seconds() / 3600:.1f}h after it could first fill"
                )
    if not stuck:
        return Check("stuck_pending", True, "no pending order is past its resolve window")
    return Check(
        "stuck_pending",
        False,
        f"{len(stuck)} order(s) still pending long after the fill should have resolved: "
        + "; ".join(stuck[:4]),
        count=len(stuck),
        items=stuck,
    )


def check_fills_after_orders(arena, pb) -> Check:
    """Every fill must come from a bar that starts strictly after its order existed.

    ARN-000002 on 23 Sep was decided at 10:37:41 and filled at the 10:29 bar, and nothing
    noticed for hours. This reads the books and says so the next cycle. Orders written before
    decided_at and data_as_of existed carry neither honestly, so only orders with data_as_of
    are checked; a legacy order corrected by a script gets it and is checked from then on.
    """
    bad = []
    for acct in _books(arena, pb):
        for o in acct.orders.values():
            if not o.filled_qty or not o.data_as_of or not o.fill_minute:
                continue  # part-filled orders count too: every slice must be after the order
            fill = min(
                [datetime.fromisoformat(f["minute"]) for f in o.fills]
                + [datetime.fromisoformat(o.fill_minute)]
            )
            floor = datetime.fromisoformat(o.rests_from or o.decided_at)
            if fill <= floor:
                what = "began resting" if o.rests_from else "was recorded"
                bad.append(
                    f"{o.order_id} {o.side} {o.ticker} filled at the {fill:%d %b %H:%M} bar; "
                    f"the order {what} at {floor:%d %b %H:%M:%S}"
                )
    if not bad:
        return Check("fill_before_order", True, "every fill is from a bar after its order")
    return Check(
        "fill_before_order",
        False,
        f"{len(bad)} fill(s) priced from before the order existed: " + "; ".join(bad[:4]),
        count=len(bad),
        items=bad,
    )


def _books(arena, pb) -> list:
    """Both books of every enabled playbook (from 2026-09-24 the watcher runs more than
    one); just `pb`'s for a test double without playbooks."""
    try:
        pbs = [pb, *[p for p in arena.playbooks() if p.key != pb.key]]
    except Exception:  # noqa: BLE001
        pbs = [pb]
    return [arena.account(p, kind) for p in pbs for kind in ("agent", "bot")]


LIVE_STATUS_STALE_MIN = 15


def check_live_data(cfg, now: datetime) -> Check:
    """IBKR live prices, when config asks for them (ibkr/feed.py writes the status)."""
    provider = str(cfg.get("data.live_provider", "yfinance"))
    if provider != "ibkr":
        return Check(
            "live_data", True,
            f"live prices off by config (data.live_provider: {provider}): Yahoo, ~20 min delayed",
        )  # fmt: skip
    from asxbot.ibkr.feed import market_hours, read_status

    st = read_status(cfg.data_dir)
    if not st.get("at"):
        return Check("live_data", True, "no live-data status yet: the feed starts with the "
                     "watcher's first intraday cycle")  # fmt: skip
    age = (now - datetime.fromisoformat(st["at"]).astimezone(SYD)).total_seconds() / 60.0
    gwh = st.get("gateway") or {}
    down = bool(gwh.get("refused") or not gwh.get("connected") or not gwh.get("server_ok"))
    fresh = age <= LIVE_STATUS_STALE_MIN
    facts = {
        "provider_in_use": st.get("provider_in_use"), "status_age_min": round(age, 1),
        "login_needed": down and fresh and st.get("provider_in_use") != "ibkr",
        "gateway": gwh,
    }  # fmt: skip
    if fresh and st.get("provider_in_use") != "ibkr":
        why = st.get("why") or gwh.get("last_error") or "IBKR unavailable"
        # Worded 26 Sep 2026: nothing decides on Yahoo any more; entries wait for IBKR.
        return Check(
            "live_data", False,
            f"IBKR prices are not available ({why}); new entries are paused, exits keep "
            "working",
            facts=facts, items=[why],
        )  # fmt: skip
    if market_hours(now) and not fresh:
        return Check(
            "live_data", False,
            f"the live-data status has not been updated for {age:.0f} minutes in market hours",
            facts=facts, items=[f"stale since {st['at']}"],
        )  # fmt: skip
    if fresh and st.get("entries") == "paused" and market_hours(now):
        why = st.get("paused_why") or "stale data"
        facts["paused"] = True
        return Check(
            "live_data", False,
            f"entries paused on the live feed ({why}); exits keep working",
            facts=facts, items=[why],
        )  # fmt: skip
    kind = gwh.get("market_data") or "unknown"
    extra = ""
    if gwh.get("streaming") is not None:
        extra = (f", {gwh.get('streaming')} streaming of {gwh.get('line_limit')} lines, "
                 f"heartbeat {gwh.get('heartbeat_age_s')}s ago, {gwh.get('reconnects')} "
                 "reconnect(s)")  # fmt: skip
    return Check("live_data", True, f"prices from {st.get('provider_in_use')} ({kind} at the "
                 f"last quote, {st['at'][11:16]}{extra})", facts=facts)  # fmt: skip


SUPERVISOR_STALE_MIN = 6


def check_gateway_supervisor(now: datetime) -> Check:
    """The IB Gateway supervisor (ibkr/supervisor.py, a task every 2 minutes) and what it
    is waiting on. Fails when the task has not checked for a while in the hours it should,
    or when Gateway is in an outage, and says when that outage needs Rick (the phone
    approval, or a login on the PC). Telling Rick is the supervisor's job, not this check's
    (26 Sep 2026; see report)."""
    from asxbot.ibkr.supervisor import SUPERVISOR_STATE, login_window, read_json

    st = read_json(SUPERVISOR_STATE)
    if not st:
        return Check("gateway_supervisor", True, "no supervisor state yet")
    try:
        last = datetime.fromisoformat(str(st.get("last_check")))
        if last.tzinfo is None:
            last = last.replace(tzinfo=SYD)
    except (TypeError, ValueError):
        last = None
    age = None if last is None else (now - last).total_seconds() / 60.0
    outage = st.get("outage") or {}
    result = str(st.get("last_result") or "")
    facts = {"last_check": st.get("last_check"), "age_min": None if age is None else round(age, 1),
             "outage": outage or None, "result": result[:200]}  # fmt: skip
    if login_window(now) and (age is None or age > SUPERVISOR_STALE_MIN):
        return Check(
            "gateway_supervisor", False,
            f"the IB Gateway supervisor task has not checked for "
            f"{'ever' if age is None else f'{age:.0f} minutes'}: is the task 'ASXBot IB Gateway "
            "Supervisor' running?",
            facts=facts, items=["supervisor silent"],
        )  # fmt: skip
    if outage:
        notice = str(outage.get("notice") or "")
        needs = notice in ("phone", "manual", "waiting")
        facts["login_needed"] = needs
        what = {"phone": "your approval on the phone", "manual": "your login on the PC",
                "waiting": "your login on the PC"}.get(notice, "nothing from you yet")  # fmt: skip
        return Check(
            "gateway_supervisor", False,
            f"IB Gateway outage since {str(outage.get('since', ''))[11:16]}: {result[:120]}; "
            f"it needs {what}",
            facts=facts, items=[f"outage since {outage.get('since')}"],
        )  # fmt: skip
    return Check("gateway_supervisor", True,
                 f"Gateway healthy per the supervisor ({str(st.get('last_check', ''))[11:16]})",
                 facts=facts)  # fmt: skip


# Who speaks for an IBKR outage (26 Sep 2026). Until then this module sent its own lines:
# "IB Gateway needs you to log in again ... the arena is on delayed Yahoo prices until you
# do" for ANY down state - wrong on both counts since entries pause instead of falling back
# - and "IB Gateway is back". Now the Gateway supervisor tells Rick when his phone or login
# is needed and when Gateway is back, and the connection doctor (ibkr/doctor.py, inside the
# watcher) tells him about every other connection problem. While the doctor is ticking and
# has an episode open, these two checks' failures are recorded (alerts file, event, log)
# and not sent. If the doctor's state is stale or missing, they are sent as any check is.
IBKR_CHECKS = ("live_data", "gateway_supervisor")
DOCTOR_FRESH = timedelta(minutes=5)


def doctor_speaking(now: datetime, path: Path | None = None) -> str:
    """The open episode's cause if the connection doctor is ticking (its last tick under
    5 minutes old) and has one open, else "" (it is not there to speak for the outage)."""
    try:
        if path is None:
            from asxbot.ibkr.doctor import state_path

            path = state_path()
        st = json.loads(Path(path).read_text(encoding="utf-8"))
        at = datetime.fromisoformat(str((st.get("last") or {}).get("at")))
    except Exception:  # noqa: BLE001 - missing or unreadable: the doctor is not speaking
        return ""
    if at.tzinfo is None:
        at = at.replace(tzinfo=SYD)
    episode = st.get("episode")
    if not episode or abs(now - at) > DOCTOR_FRESH:
        return ""
    return str(episode.get("cause") or "an open episode")


def check_short_universe(cfg) -> Check:
    """The set the arena allows shorts in: is it the index, is it full, is it current?

    This is the fault class the other checks miss. A short universe that silently shrinks
    refuses trades and never errors: on 23 September the "ASX 200" was the top 200 by
    market cap, which held 175 of the 200 real constituents, and the decider was refused a
    short in TUA - a genuine member - twice in one minute.
    """
    from asxbot.data.universe import ASX200_MAX_AGE_DAYS, ASX200_MIN, asx200_status

    u = asx200_status(cfg.data_dir, cfg.get("collector.user_agent"))
    facts = {"codes": len(u.codes), "source": u.source, "age_days": u.age_days}
    if not u.is_index_list:
        return Check(
            "short_universe", False,
            f"the short universe is not a constituent list: {u.source}, {len(u.codes)} codes. "
            "Shorts in real index members are being refused. "
            "Run: asxbot universe asx200 --refresh",
            count=len(u.codes), facts=facts,
        )  # fmt: skip
    if u.too_small:
        return Check(
            "short_universe", False,
            f"the ASX 200 list has only {len(u.codes)} codes (fewer than {ASX200_MIN}); "
            f"source {u.source}. Every missing member is a short refused without an error",
            count=len(u.codes), facts=facts,
        )  # fmt: skip
    if u.stale:
        age = "never dated" if u.age_days is None else f"{u.age_days} days old"
        return Check(
            "short_universe", False,
            f"the ASX 200 list is {age} (limit {ASX200_MAX_AGE_DAYS} days) and has not "
            f"refreshed; source {u.source}",
            count=len(u.codes), facts=facts,
        )  # fmt: skip
    return Check(
        "short_universe", True,
        f"{len(u.codes)} ASX 200 codes, {u.age_days} day(s) old, from {u.source}",
        facts=facts,
    )  # fmt: skip


ERROR_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ (ERROR|CRITICAL) (\S+): (.*)")

# The checks' own voices. Without this the error check feeds on itself: raising an alert
# logs CRITICAL, the next cycle counts that line as an error, raises another, and the count
# climbs on its own - 8, 10, 12 - with nothing actually wrong. Seen within a minute of
# turning these on.
SELF_LOGGERS = ("asxbot.alerts", "asxbot.arena.selfcheck")


GROUP_ERRORS = timedelta(minutes=30)
# (log, error class) -> runs of that class: [first, last, the first line]. Kept in memory,
# so a run keeps its name after its first line leaves the hour the check reads.
_ERROR_RUNS: dict[tuple[str, str], list[list]] = {}


def error_class(logger: str, msg: str) -> str:
    """Lines that differ only in their numbers (ids, times, sizes, status codes) are one
    kind of error."""
    return f"{logger}: {re.sub(r'[0-9]+', '#', msg)[:110]}"


def check_errors_logged(cfg, now: datetime) -> Check:
    """Any ERROR or CRITICAL the WATCHER logged in the last hour.

    An exception that only reaches the log has, in practice, not been reported at all.

    Two changes on 26 Sep 2026. It reads the watcher's own log (asxbot.log.WATCH_LOG): it
    used to read asxbot.log, which every other process writes too, and an 18:57 chaos-test
    line on 25 Sep reached Rick as a SELF-CHECK. And repeats are grouped: each finding is a
    run of one kind of error (error_class) with no gap over 30 minutes, named by its first
    line, so the same 404 at 08:02 and 08:03 is one message, not two. A kind that comes back
    after a quiet half hour is a new run, and said again.
    """
    from asxbot.log import WATCH_LOG

    since = now - timedelta(hours=1)
    path = cfg.logs_dir / WATCH_LOG
    runs_seen: list[list] = []
    count: dict[int, int] = {}
    hits = 0
    for line in tail_lines(path):
        m = ERROR_LINE.match(line)
        if not m:
            continue
        logger = m.group(3)
        if logger in SELF_LOGGERS:
            continue  # an alert about an error is not itself an error
        if logger == "asxbot.arena.agents" and m.group(4).startswith("AGENT UNAVAILABLE"):
            continue  # the agent_unavailable check reports these, by kind
        when = datetime.fromisoformat(m.group(1)).replace(tzinfo=SYD)
        if when < since:
            continue
        hits += 1
        runs = _ERROR_RUNS.setdefault((str(path), error_class(logger, m.group(4))), [])
        run = next((r for r in runs if r[0] <= when <= r[1]), None)
        if run is None and runs and when > runs[-1][1] and when - runs[-1][1] <= GROUP_ERRORS:
            run = runs[-1]
            run[1] = when
        if run is None:
            run = [when, when, f"{m.group(1)[11:]} {logger}: {m.group(4)[:110]}"]
            runs.append(run)
            runs.sort(key=lambda r: r[0])
        if id(run) not in count:
            runs_seen.append(run)
        count[id(run)] = count.get(id(run), 0) + 1
    for key in [k for k, v in _ERROR_RUNS.items() if v and now - v[-1][1] > timedelta(hours=3)]:
        _ERROR_RUNS.pop(key, None)
    if not hits:
        return Check("errors_logged", True, "no ERROR logged in the last hour")
    runs_seen.sort(key=lambda r: r[0])

    def said(r: list) -> str:
        n = count[id(r)]
        return r[2] + (f" (x{n}, the last at {r[1]:%H:%M:%S})" if n > 1 else "")

    return Check(
        "errors_logged",
        False,
        f"{hits} ERROR line(s) in the last hour, {len(runs_seen)} kind(s): "
        + " | ".join(said(r) for r in runs_seen[-3:]),
        count=hits,
        items=[r[2] for r in runs_seen],
    )


LOG_SILENT_AFTER = timedelta(minutes=5)
STAMPED_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ ")


def last_stamp(path: Path) -> datetime | None:
    """The time on the last timestamped line of a log, or None if it has none."""
    for line in reversed(tail_lines(path)):
        m = STAMPED_LINE.match(line)
        if m:
            return datetime.fromisoformat(m.group(1)).replace(tzinfo=SYD)
    return None


def check_log_growing(cfg, hb: dict | None = None, stdout_log: str | None = None) -> Check:
    """The watcher is logging, but its log files are not receiving the lines.

    At 08:14 on 24 Sep 2026 Google Drive cut off the watcher's long-open append handles.
    The watcher kept trading and logging, both asxbot.log and the launcher's copy of its
    output stopped growing, and the heartbeat (rewritten whole) looked healthy - so the
    watchdog saw nothing wrong. This holds each log's last line to the heartbeat's
    `last_record`, the last line the watcher handed its log: 5 minutes apart is a log that
    is not being written.
    """
    from asxbot.arena import heartbeat
    from asxbot.log import WATCH_LOG

    hb = hb if hb is not None else (heartbeat.current() or heartbeat.read(cfg.data_dir))
    if not hb or hb.get("state") != "running":
        return Check("log_silent", True, "no running watcher to hold the logs to")
    record = hb.get("last_record")
    if not record:
        return Check("log_silent", True, "the watcher has logged nothing yet")
    record = datetime.fromisoformat(record).astimezone(SYD)
    stdout_log = stdout_log if stdout_log is not None else os.environ.get("ASXBOT_STDOUT_LOG")
    # The watcher's own log (26 Sep 2026; it was asxbot.log, which other processes write).
    logs = [cfg.logs_dir / WATCH_LOG] + ([Path(stdout_log)] if stdout_log else [])
    stale, facts = [], {"last_record": record.isoformat(timespec="seconds")}
    # A log with no line yet is measured from the watcher's start, not called silent at once.
    started = datetime.fromisoformat(hb.get("started") or record.isoformat()).astimezone(SYD)
    for p in logs:
        last = last_stamp(p)
        facts[p.name] = last.isoformat(timespec="seconds") if last else None
        if record - (last or started) >= LOG_SILENT_AFTER:
            said = f"last line {last:%H:%M:%S}" if last else "no timestamped line at all"
            stale.append(f"{p} ({said})")
    if not stale:
        return Check("log_silent", True, f"the logs are keeping up (last record {record:%H:%M:%S})",
                     facts=facts)  # fmt: skip
    return Check(
        "log_silent",
        False,
        f"the watcher logged at {record:%H:%M:%S} but its log is not being written: "
        + "; ".join(stale)
        + ". It is still trading; only its record is lost. On 24 Sep 2026 a restart of the "
        "watcher through its scheduled task (ASXBot Arena Warmup) brought the log back",
        count=len(stale),
        facts=facts,
        items=stale,
    )


def check_gui_launcher(scripts: Path | None = None) -> Check:
    """The venv's pythonw.exe - what the Warmup, Evening and Watchdog tasks start - has no
    console. uv's own venv launcher does (TRACKER #32); rebuilding the venv with uv brings
    it back, and every task start flashes a window again."""
    from asxbot.proc import pe_subsystem

    exe = (scripts or Path(sys.prefix) / "Scripts") / "pythonw.exe"
    if (sys.platform != "win32" and scripts is None) or not exe.exists():
        return Check("console_launcher", True, f"no {exe} here to check")
    kind = pe_subsystem(exe)
    if kind == "GUI":
        return Check("console_launcher", True, f"{exe} is a GUI program")
    return Check(
        "console_launcher",
        False,
        f"{exe} is a {kind} program, so every scheduled task that starts it flashes a window. "
        "Put CPython's own GUI launcher back: python scripts/install_gui_launcher.py",
    )


# -- running them -----------------------------------------------------------
def expected_agents(cfg) -> dict[str, tuple[str, str]]:
    """agent -> (model, effort) that the agent_mismatch check holds every call to.

    Both from config.yaml: models from `arena.agents.models` (moved there from watch.py's
    constants on 2026-09-24, the same values every call now passes as `expect_model`),
    effort levels from `arena.agents.effort`. Re-read from the file when it changes, so a
    /model or /think in the Trader chat - recorded there as a dated strategy change -
    applies to the next check (agents.agent_settings). The fallbacks are the settings of
    2026-09-23 evening; config normally has all four.
    """
    from asxbot.arena.agents import ROLES, expected_effort, expected_model

    return {agent: (expected_model(cfg, role), expected_effort(cfg, role))
            for role, agent in ROLES.items()}  # fmt: skip


def run_checks(arena, pb, now: datetime | None = None) -> list[Check]:
    """Every check, in one pass. Never raises: a check that breaks is itself a failure."""
    now = (now or datetime.now(SYD)).astimezone(SYD)
    cfg = arena.cfg
    expected = expected_agents(cfg)
    runners = [
        ("pdf_not_pdf", lambda: check_pdfs_are_pdfs(cfg)),
        ("pdf_fetch_failed", lambda: check_pdf_fetches(cfg, now)),
        ("screen_dominated", lambda: check_screen_not_dominated(cfg, now)),
        ("agent_mismatch", lambda: check_agent_calls(cfg, now, expected)),
        ("agent_unavailable", lambda: check_agent_unavailable(cfg, now)),
        ("stuck_pending", lambda: check_pending_orders(arena, pb, now)),
        ("fill_before_order", lambda: check_fills_after_orders(arena, pb)),
        ("errors_logged", lambda: check_errors_logged(cfg, now)),
        ("log_silent", lambda: check_log_growing(cfg)),
        ("short_universe", lambda: check_short_universe(cfg)),
        ("console_launcher", check_gui_launcher),
        ("live_data", lambda: check_live_data(cfg, now)),
        ("gateway_supervisor", lambda: check_gateway_supervisor(now)),
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
    state.pop("_ibkr_login_told", None)  # the retired login line's marker (26 Sep 2026)
    doctor = ""
    if any(c.key in IBKR_CHECKS and not c.ok for c in checks):
        doctor = doctor_speaking(now)

    for c in checks:
        if c.ok:
            if state.pop(c.key, None) is not None:
                alerts.clear(c.key)
                log.info("self-check %s is clear again: %s", c.key, c.detail)
            continue
        prior = state.get(c.key) or {}
        if isinstance(prior, str):  # the pre-23-Sep shape: a bare timestamp
            prior = {"at": prior, "items": []}
        last, seen = prior.get("at"), list(prior.get("items", []))

        # Two reasons to speak: the repeat window has passed, or something NEW has
        # appeared. Tracking only "is it failing" meant a genuinely new error arriving
        # inside the hour was silent, hidden behind a fault already reported.
        new_items = [i for i in c.items if i not in seen]
        told_by = ""
        if doctor and c.key in IBKR_CHECKS:
            told_by = f"the connection doctor ({doctor})"
        elif c.key == "agent_unavailable" and set(c.facts.get("kinds") or {}) == {"usage_limit"}:
            told_by = "the usage-limit line (once when it starts, once when calls work again)"
        # A failure held back while the doctor spoke is said as soon as it no longer does.
        unmuted = bool(prior.get("muted")) and not told_by
        overdue = last is None or unmuted or (now - datetime.fromisoformat(last)) >= repeat
        due = force or overdue or bool(new_items)

        events.append(
            "arena_selfcheck",
            {
                "check": c.key, "ok": False, "detail": c.detail, "count": c.count,
                "new_items": new_items[:20], **c.facts,
                **({"not_sent": f"told by {told_by}"} if told_by else {}),
            },  # fmt: skip
        )
        if due:
            # Only shout when it is due. The watcher runs every minute, and a standing
            # fault logged every minute is the log nobody reads all over again.
            log.critical("SELF-CHECK FAILED [%s] %s", c.key, c.detail)
            alerts.raise_alert(c.key, c.detail)
        else:
            log.info("self-check %s still failing (already reported): %s", c.key, c.detail)
        if told_by:
            # Recorded above; Rick hears it from the doctor or the Gateway supervisor.
            if due:
                log.info("self-check %s not sent to Telegram: %s is telling Rick", c.key, told_by)
        elif due and alert:
            body = notify.escape_text(c.detail)
            if new_items and not overdue:
                lines = "\n".join(notify.escape_text(i) for i in new_items[:5])
                body = f"{len(new_items)} NEW since the last report:\n{lines}"
            alert.send(f"🚨 <b>SELF-CHECK: {c.key}</b>\n{body}")
        if due:
            state[c.key] = {
                "at": now.isoformat(timespec="seconds"),
                "items": (seen + new_items)[-200:],
                **({"muted": True} if told_by else {}),
            }
        elif new_items:  # nothing to say, but do not forget what was seen
            state[c.key] = {"at": last, "items": (seen + new_items)[-200:],
                            **({"muted": True} if prior.get("muted") else {})}  # fmt: skip
    _usage_limit_lines(cfg, now, state, alert)
    write_text_atomic(json.dumps(state, indent=2), _state_path(cfg))
    return failures


def _usage_limit_lines(cfg, now: datetime, state: dict, alert) -> None:
    """The Claude plan's usage limit is urgent (26 Sep 2026): one line when it starts
    refusing agent calls, one when a call is answered again - not the hourly repeat."""
    try:
        ul = usage_limit_state(cfg, now)
    except Exception as e:  # noqa: BLE001 - a check must never stop the watcher
        log.warning("could not read the agent calls for the usage limit: %s", e)
        return
    told = state.get("_usage_limit")
    if ul.get("active") and not told:
        text = (f"🛑 <b>AGENT UNAVAILABLE: usage limit</b> - the Claude plan has refused agent "
                f"calls since {ul['since']:%H:%M}. The reader and deciders make no decisions "
                "until it resets; the rule bots, stops, targets and the flat sweep carry on.")
        log.critical("AGENT UNAVAILABLE: usage limit since %s", f"{ul['since']:%H:%M}")
        if alert:
            alert.send(text)
        state["_usage_limit"] = {"since": ul["since"].isoformat(timespec="seconds"),
                                 "told_at": now.isoformat(timespec="seconds")}  # fmt: skip
    elif told and ul.get("back"):
        since = datetime.fromisoformat(told["since"])
        log.info("agent calls answered again at %s", f"{ul['back']:%H:%M}")
        if alert:
            alert.send(f"✅ Agent calls are answered again ({ul['back']:%H:%M}), after the usage "
                       f"limit from {since:%H:%M}.")  # fmt: skip
        state.pop("_usage_limit", None)
