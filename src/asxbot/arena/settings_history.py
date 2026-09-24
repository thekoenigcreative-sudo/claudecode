"""Deliberate changes to the trader agents' model or effort level, dated in config.yaml.

Rick's rule: on the Trader, /model and /think are STRATEGY CHANGES. A different model or
effort level changes every decision the arena makes, so it is recorded the way every other
deliberate change is (CLAUDE.md: "date it in config.yaml with the reason and label it a
deliberate change, not a finding"):

  * the new expectation goes into `arena.agents.models.<role>` or
    `arena.agents.effort.<role>` - what the self-check and every agent call hold the agent
    to (agents.expected_model / expected_effort);
  * a dated entry is appended to `arena.agents.history`;
  * config.yaml is edited as TEXT, so every comment in it survives, then re-read to prove
    only those values changed, and committed on its own.

The evening report lists the entries since the previous report (report.settings_changed).
The Trader chat (asxbot.chat) calls `refusal` before a change and `record` after it; if
`record` raises, botctl puts the OpenClaw setting back.
"""

from __future__ import annotations

import copy
import difflib
import json
import os
import re
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from asxbot import proc

SYD = ZoneInfo("Australia/Sydney")
KEYS = {"model": "models", "effort": "effort"}  # setting -> key under arena.agents
WORDS = {"model": "model", "effort": "thinking"}
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/._-]*$")
CO_AUTHOR = "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"


class RecordFailed(RuntimeError):
    pass


# -- git, through the windowless helper ----------------------------------------------
def git(repo: Path, *args: str, timeout: float = 60) -> tuple[int, str, str]:
    try:
        p = proc.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, stdin=proc.DEVNULL,
        )  # fmt: skip
    except proc.TimeoutExpired:
        return 124, "", "git timed out"
    except OSError as e:
        return 127, "", str(e)
    return p.returncode, p.stdout or "", p.stderr or ""


def _same_repo(a: str, b: str) -> bool:
    def norm(p: str) -> str:
        return str(p).replace("/", "\\").rstrip("\\").lower()

    return bool(a) and norm(a) == norm(b)


def refusal(repo: Path, requests: list[dict], role: str, setting: str) -> str | None:
    """Why a model/effort change must wait, in a plain sentence, or None if it can go ahead.

    `requests` are the change-request records (botctl.list_reqs()). Refused while
    config.yaml has uncommitted edits (someone is working on it, and the change must be a
    commit of its own) or while a change job is building in this repo.
    """
    what = f"the {role}'s {WORDS[setting]}"
    rc, out, err = git(repo, "status", "--porcelain", "--", "config.yaml")
    if rc != 0:
        why = (err or out).strip()[:120] or f"git exit {rc}"
        return f"I can't change {what} just now: I couldn't check config.yaml ({why}). " \
            "Nothing has changed."  # fmt: skip
    if out.strip():
        return (f"I can't change {what} just now: config.yaml has edits that aren't "
                "committed yet (someone is working on it), and a model change has to be "
                "recorded there as its own dated entry. Try again once that work is "
                "committed. Nothing has changed.")  # fmt: skip
    busy = [r for r in requests
            if r.get("status") == "building" and _same_repo(r.get("repo", ""), str(repo))]
    if busy:
        from asxbot.botctl import short

        return (f"I can't change {what} while change {short(busy[-1]['id'])} is being "
                "built in the Trader's code. Try again after its report. Nothing has "
                "changed.")  # fmt: skip
    return None


# -- editing config.yaml as text -----------------------------------------------------
def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_key(line: str, indent: int, key: str) -> bool:
    return _indent(line) == indent and re.match(rf"{re.escape(key)}:(\s|$)", line.strip() + " ")


def _find(lines: list[str], start: int, stop: int, indent: int, key: str) -> int:
    for i in range(start, stop):
        if _is_key(lines[i], indent, key):
            return i
    raise RecordFailed(f"config.yaml has no '{key}:' where it was expected")


def _block_end(lines: list[str], i: int, indent: int) -> int:
    """The index after the last line of the mapping that starts at line i."""
    for j in range(i + 1, len(lines)):
        s = lines[j].strip()
        if s and not s.startswith("#") and _indent(lines[j]) <= indent:
            return j
    return len(lines)


def _yaml_str(v: str) -> str:
    return json.dumps(str(v))  # a JSON string is a valid YAML double-quoted scalar


def entry_line(entry: dict, indent: int = 6) -> str:
    body = ", ".join(f"{k}: {_yaml_str(v)}" for k, v in entry.items())
    return " " * indent + "- {" + body + "}"


def edit_text(text: str, role: str, setting: str, new: str, entry: dict) -> str:
    """config.yaml with arena.agents.<models|effort>.<role> set to `new` and `entry`
    appended to arena.agents.history. Every other line is left exactly as it was."""
    if setting not in KEYS:
        raise RecordFailed(f"unknown setting {setting!r}")
    if not _SAFE_VALUE.match(str(new)):
        raise RecordFailed(f"{new!r} is not a value I can write into config.yaml safely")
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    arena = _find(lines, 0, len(lines), 0, "arena")
    agents = _find(lines, arena + 1, _block_end(lines, arena, 0), 2, "agents")
    a_end = _block_end(lines, agents, 2)
    sect = _find(lines, agents + 1, a_end, 4, KEYS[setting])
    row = _find(lines, sect + 1, _block_end(lines, sect, 4), 6, role)
    m = re.match(r"^(\s*[^:]+:\s*)([^#\s][^#]*?)(\s*#.*)?$", lines[row])
    if not m:
        raise RecordFailed(f"config.yaml's {KEYS[setting]}.{role} line is not a plain value")
    lines[row] = m.group(1) + str(new) + (m.group(3) or "")

    hist = _find(lines, agents + 1, a_end, 4, "history")
    head = lines[hist].split("#", 1)
    value = head[0].split(":", 1)[1].strip()
    if value == "[]":
        lines[hist] = "    history:" + (f"  #{head[1]}" if len(head) > 1 else "")
    elif value:
        raise RecordFailed("config.yaml's arena.agents.history is not a block list")
    at = hist + 1
    while at < len(lines) and lines[at].strip() and _indent(lines[at]) >= 6:
        at += 1
    lines.insert(at, entry_line(entry))
    return nl.join(lines) + (nl if text.endswith(("\n", "\r\n")) else "")


def check_edit(before: str, after: str, role: str, setting: str, new: str, entry: dict) -> None:
    """Prove the edit did exactly what it says: the YAML still loads, the only values that
    differ are the one setting and the new history entry, and no other line changed."""
    try:
        old_d = yaml.safe_load(before) or {}
        new_d = yaml.safe_load(after) or {}
    except yaml.YAMLError as e:
        raise RecordFailed(f"config.yaml would no longer load: {e}") from e
    want = copy.deepcopy(old_d)
    ag = want.setdefault("arena", {}).setdefault("agents", {})
    ag.setdefault(KEYS[setting], {})[role] = new
    ag["history"] = list(ag.get("history") or []) + [dict(entry)]
    if new_d != want:
        raise RecordFailed("the edited config.yaml does not say what was intended")
    removed = [d[2:] for d in difflib.ndiff(before.splitlines(), after.splitlines())
               if d.startswith("- ")]  # fmt: skip
    for line in removed:
        if not (_is_key(line, 6, role) or _is_key(line, 4, "history")):
            raise RecordFailed(f"the edit would change an unrelated line: {line.strip()[:80]}")


def _write_exact(path: Path, text: str) -> None:
    """Write text byte for byte (no newline translation), atomically."""
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def record(repo: Path, role: str, setting: str, old: str, new: str, by: str,
           when: datetime | None = None, note: str = "") -> dict:  # fmt: skip
    """Write the change into config.yaml and commit it. Returns the history entry, with the
    commit's short sha under "commit". Raises RecordFailed (config.yaml put back) on any
    doubt."""
    repo = Path(repo)
    path = repo / "config.yaml"
    when = (when or datetime.now(SYD)).astimezone(SYD)
    entry = {
        "date": when.date().isoformat(), "time": when.strftime("%H:%M"), "role": role,
        "setting": setting, "old": str(old), "new": str(new), "by": by,
        "note": note or "deliberate change by Rick, not a finding",
    }  # fmt: skip
    with open(path, encoding="utf-8", newline="") as fh:
        before = fh.read()
    after = edit_text(before, role, setting, new, entry)
    check_edit(before, after, role, setting, new, entry)
    _write_exact(path, after)
    msg = (
        f"Strategy change by Rick: {role} {WORDS[setting]} {old} -> {new}\n\n"
        f"{by}, {entry['date']} {entry['time']} Sydney. Recorded in config.yaml "
        f"arena.agents.{KEYS[setting]}.{role} and arena.agents.history: a deliberate change, "
        "not a finding. The OpenClaw agent was changed at the same time.\n\n" + CO_AUTHOR
    )
    rc, out, err = git(repo, "commit", "-m", msg, "--", "config.yaml")
    if rc != 0:
        _write_exact(path, before)
        raise RecordFailed(f"git commit failed: {(err or out).strip()[:200]}")
    rc, sha, _ = git(repo, "rev-parse", "--short", "HEAD")
    return {**entry, "commit": sha.strip() if rc == 0 else "?"}


# -- reading it back (the evening report) --------------------------------------------
def entries(agents: dict) -> list[dict]:
    """arena.agents.history, skipping anything that is not a dated entry."""
    out = []
    for e in agents.get("history") or []:
        if isinstance(e, dict) and e.get("date") and e.get("role") and e.get("setting"):
            out.append(e)
    return out


def entry_time(e: dict) -> datetime:
    t = time.fromisoformat(str(e.get("time") or "00:00"))
    return datetime.combine(date.fromisoformat(str(e["date"])), t, tzinfo=SYD)


def since(agents: dict, start: datetime, end: datetime) -> list[dict]:
    return [e for e in entries(agents) if start <= entry_time(e) < end]


def _clock(t: datetime) -> str:
    return f"{t.hour % 12 or 12}:{t.minute:02d}{'am' if t.hour < 12 else 'pm'}"


def describe(e: dict, day: date | None = None) -> str:
    """'decider model Opus 5.5 -> Sonnet 5 (Rick, 10:15pm)'."""
    from asxbot.botctl import pretty_model

    t = entry_time(e)
    show = pretty_model if e["setting"] == "model" else str
    who = str(e.get("by") or "Rick").split(",")[0].strip()
    when = _clock(t) if day is None or t.date() == day else f"{t:%a} {_clock(t)}"
    return (f"{e['role']} {WORDS.get(e['setting'], e['setting'])} {show(e.get('old'))} -> "
            f"{show(e.get('new'))} ({who}, {when})")  # fmt: skip


def report_window(last_report: datetime | None, day: date) -> tuple[datetime, datetime]:
    """From the previous report (or the start of `day`) to the end of `day`."""
    start = datetime.combine(day, time(0), tzinfo=SYD)
    end = start + timedelta(days=1)
    if last_report is not None and last_report < end:
        start = min(start, last_report.astimezone(SYD))
    return start, end
