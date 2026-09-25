"""Calling the two OpenClaw trader agents, and recording which model actually answered.

ARENA.md: "OpenClaw's per-spawn model override has open bug reports where it is silently
ignored, so don't rely on it. A setup test confirms which model produced each step, and
every decision logs its model name."

So the model is set on the agent itself (`openclaw agents add --model`), never per call,
and the model recorded here is the one OpenClaw reports it actually ran
(`meta.executionTrace.winnerModel`) - not the one we asked for, and never the agent's own
claim about itself.

The effort level is set the same way, on the agent (`thinkingDefault`). Set 2026-09-23 to
reader low, decider high; changed by Rick the evening of 2026-09-23 to reader medium,
decider high, with the decider moved from Opus 5 to Opus 5.5. Every call records what
OpenClaw says it shaped the request with (`meta.requestShaping.thinking`), so a level that
quietly stops applying is visible in the event log rather than being assumed.

Code hands the reader's summary to the decider. The agents never call each other.

Failures (26 Sep 2026). The agents run on Rick's Claude plan, and its weekly limit was at 4%
left late on 25 Sep. Until then a failed call was invisible: only a successful reply was
recorded in `arena_agent_calls`, `reply.ok` was never checked, and every failure read "no
answer within 60s" even when the plan refused in seven seconds. Now every call is recorded,
ok or not; a failure raises AgentCallFailed with a `kind` ('usage_limit', 'timeout' or
'error') and is logged at ERROR; and a reply OpenClaw does not mark ok is a failure.

Sessions (26 Sep 2026). Without a session key OpenClaw resumes ONE conversation per agent,
so since 22 Sep every watcher call has been read on top of every earlier one (~593k cached
tokens a day-trader call, ~879k for the reader). `fresh_session_key` makes a key used once;
config `arena.agents.fresh_session_per_call` turns it on for the watcher and the report.
The Trader chat keeps its own key.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot import proc as hidden
from asxbot.arena.heartbeat import quiet
from asxbot.log import EventLog, get_logger

log = get_logger("asxbot.arena.agents")
SYD = ZoneInfo("Australia/Sydney")

READER = "trader-reader"
DECIDER = "trader-decider"
ROLES = {"reader": READER, "decider": DECIDER}

# What the repo expects each agent to run on is config.yaml `arena.agents.models` and
# `arena.agents.effort` (models moved there from watch.py on 2026-09-24, so a /model change
# in the Trader chat is recorded as a dated strategy change - arena/settings_history.py).
# These are fallbacks only, for a config without the keys: the settings of 23 Sep evening.
FALLBACK_MODELS = {"reader": "anthropic/claude-sonnet-5", "decider": "anthropic/claude-opus-5-5"}
FALLBACK_EFFORT = {"reader": "medium", "decider": "high"}

_fresh: dict[str, tuple[float, dict]] = {}


def agent_settings(cfg) -> dict:
    """config.yaml `arena.agents`, re-read from the file whenever it has changed.

    A /model or /think in the Trader chat changes the OpenClaw agent at once and records
    the new expectation in config.yaml; a watcher that started at 07:30 must hold the next
    call to the new value, not raise a false mismatch alert all day. Falls back to what
    `cfg` was loaded with when there is no file (or it cannot be read).
    """
    path = getattr(cfg, "path", None)
    if path is not None:
        import yaml

        try:
            mtime = Path(path).stat().st_mtime
            hit = _fresh.get(str(path))
            if hit is None or hit[0] != mtime:
                raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
                hit = (mtime, ((raw.get("arena") or {}).get("agents")) or {})
                _fresh[str(path)] = hit
            return hit[1]
        except (OSError, ValueError, AttributeError) as e:
            log.warning("could not re-read %s (%s); using the settings loaded at start", path, e)
    return cfg.get("arena.agents") or {}


def expected_model(cfg, role: str) -> str:
    """The model config.yaml expects `role` (reader | decider) to run on."""
    return str((agent_settings(cfg).get("models") or {}).get(role) or FALLBACK_MODELS[role])


def expected_effort(cfg, role: str) -> str:
    """The effort (thinking) level config.yaml expects `role` to run at."""
    return str((agent_settings(cfg).get("effort") or {}).get(role) or FALLBACK_EFFORT[role])


def fresh_session_key(purpose: str = "", now: datetime | None = None) -> str:
    """A session key used for one call only: `arena-<YYYYMMDD>-<purpose>-<8 hex>` (26 Sep
    2026, G2). Each call then starts a new OpenClaw conversation, so a decision rests on its
    own packet and the agent's standing instructions, not on weeks of earlier calls."""
    day = (now or datetime.now(SYD)).astimezone(SYD).strftime("%Y%m%d")
    slug = re.sub(r"[^a-z0-9]+", "-", purpose.lower()).strip("-")[:40].strip("-") or "call"
    return f"arena-{day}-{slug}-{secrets.token_hex(4)}"


def fresh_sessions_on(cfg) -> bool:
    """config.yaml `arena.agents.fresh_session_per_call` (default off in code). Re-read like
    the models, so a change in config.yaml applies to the next call without a restart."""
    if cfg is None:
        return False
    try:
        return bool(agent_settings(cfg).get("fresh_session_per_call", False))
    except Exception:  # noqa: BLE001 - a settings read must never stop a call
        return False


def session_for(cfg, purpose: str = "") -> str | None:
    """The session key a watcher or report call should use: a fresh one when config says
    so, otherwise None (OpenClaw's one shared session per agent, as before)."""
    return fresh_session_key(purpose) if fresh_sessions_on(cfg) else None


# What a refused call says when the plan's limit is the reason. Only ever matched against a
# FAILED call's text, or a short reply with no decision in it: a real answer that mentions a
# limit order is not a usage limit.
_USAGE_LIMIT = re.compile(
    r"\b(session|usage|weekly|daily|monthly|hourly|5[- ]?hour|opus|plan)\s+limit"
    r"|\brate[ _-]?limit|\b429\b|too many requests|limit (?:reached|exceeded|hit)"
    r"|hit your (?:\w+ )?limit|out of (?:extra )?usage|quota",
    re.IGNORECASE,
)
_TIMEOUT = re.compile(r"timed? ?out|timeout|deadline exceeded", re.IGNORECASE)


def classify_failure(text: str) -> str:
    """'usage_limit', 'timeout' or 'error': why a call failed, from what it said."""
    if _USAGE_LIMIT.search(text or ""):
        return "usage_limit"
    if _TIMEOUT.search(text or ""):
        return "timeout"
    return "error"


FAILURE_WORDS = {
    "usage_limit": "usage limit",
    "timeout": "no answer in time",
    "error": "error",
}


class AgentCallFailed(RuntimeError):
    """A call to an OpenClaw agent that produced no usable answer. `kind` says why:
    'usage_limit' (the plan refused), 'timeout' or 'error'. Callers record it as "agent
    unavailable (<kind>)", never as the agent's own rejection."""

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind if kind in FAILURE_WORDS else "error"


@dataclass
class AgentReply:
    agent: str
    text: str
    model: str  # what OpenClaw reports it actually ran
    requested_model: str
    thinking: str  # the effort level OpenClaw reports it shaped the request with
    run_id: str
    duration_ms: int
    ok: bool
    raw: dict

    @property
    def model_matches(self) -> bool:
        return same_model(self.model, self.requested_model)


def same_model(ran: str, want: str) -> bool:
    """Exactly the same model, once the `anthropic/` provider prefix is dropped from each.

    Exact on purpose: `claude-opus-5` and `claude-opus-5-5` share a prefix, and a
    prefix or substring match would pass either one for the other.
    """
    ran, want = ran.strip(), want.strip()
    return bool(ran) and ran.removeprefix("anthropic/") == want.removeprefix("anthropic/")


def _openclaw_bin() -> str:
    for name in ("openclaw.cmd", "openclaw"):
        found = shutil.which(name)
        if found:
            return found
    raise AgentCallFailed("the openclaw CLI is not on PATH")


def _extract_json(stdout: str) -> dict:
    """OpenClaw prints plugin warnings before the JSON body, so find where the JSON starts."""
    start = stdout.find("{")
    while start != -1:
        try:
            return json.loads(stdout[start:])
        except json.JSONDecodeError:
            start = stdout.find("{", start + 1)
    raise AgentCallFailed(
        f"no JSON in the openclaw reply: {stdout[:400]}", classify_failure(stdout)
    )


def _kill_tree(p) -> None:
    """Stop a child and everything it started. `openclaw.cmd` is cmd.exe running node: killing
    only cmd.exe (what subprocess.run does on a timeout) leaves node holding the pipes, and
    the wait for its output then has no end (26 Sep 2026, E6)."""
    if os.name == "nt":
        try:
            hidden.run(
                ["taskkill", "/T", "/F", "/PID", str(p.pid)],
                capture_output=True, text=True, timeout=15,
            )  # fmt: skip
        except (OSError, hidden.TimeoutExpired) as e:
            log.error("taskkill of %s's process tree failed: %s", p.pid, e)
    try:
        p.kill()
    except OSError:
        pass


def _run_bounded(cmd: list[str], wait: int) -> tuple[int, str, str]:
    """(returncode, stdout, stderr) of `cmd`, or TimeoutExpired after `wait` seconds with the
    whole process tree stopped and at most 10 more seconds spent collecting what it wrote."""
    p = hidden.popen(
        cmd, stdout=hidden.PIPE, stderr=hidden.PIPE, stdin=hidden.DEVNULL, text=True,
        encoding="utf-8", errors="replace",
    )  # fmt: skip
    try:
        out, err = p.communicate(timeout=wait)
    except hidden.TimeoutExpired:
        _kill_tree(p)
        try:
            p.communicate(timeout=10)
        except (hidden.TimeoutExpired, OSError, ValueError):
            for pipe in (p.stdout, p.stderr):
                try:
                    pipe.close()
                except (OSError, AttributeError):
                    pass
        raise
    return p.returncode, out or "", err or ""


def _body_failure(body: dict, text: str) -> str:
    """What a reply OpenClaw did not mark ok says about why, for the record."""
    bits = [f"status {body.get('status')!r}" if body.get("status") else "no status"]
    for k in ("error", "message", "reason", "summary"):
        v = body.get(k) or ((body.get("result") or {}).get(k))
        if v:
            bits.append(str(v if not isinstance(v, dict) else v.get("message") or v)[:300])
    if text:
        bits.append(text[:300])
    return "; ".join(bits)


def _looks_like_limit_notice(text: str) -> bool:
    """A reply marked ok that is only the plan's limit notice ("You've hit your weekly limit
    - resets ..."): short, no decision block in it, and in the words of a usage limit."""
    t = (text or "").strip()
    return bool(t) and len(t) < 400 and "{" not in t and classify_failure(t) == "usage_limit"


def call_agent(
    agent: str,
    message: str,
    *,
    expect_model: str = "",
    timeout_s: int = 600,
    data_dir: Path | None = None,
    purpose: str = "",
    process_timeout_s: int | None = None,
    session_key: str | None = None,
    fresh_session: bool = False,
) -> AgentReply:
    """Run one turn of an OpenClaw agent and return its text plus the model that ran it.

    `session_key` keeps a conversation going across calls (the Trader chat). Without one
    each call is OpenClaw's default session for the agent, as the watcher has always used,
    unless `fresh_session` asks for a new session of its own (fresh_session_key; callers
    pass `fresh_session=fresh_sessions_on(cfg)`).

    Raises AgentCallFailed, with its `kind`, whenever there is no usable answer: the CLI is
    missing, the process runs past its time, OpenClaw exits with nothing, its reply is not
    marked ok, or the reply is only the plan's limit notice. Every call, failed or not, is
    one `arena_agent_calls` record when `data_dir` is given.
    """
    if fresh_session and not session_key:
        session_key = fresh_session_key(purpose)
    started = time.monotonic()
    # The process is given a minute beyond the agent's own timeout, unless the caller needs
    # a hard answer time (the day trader: 60 s, plus start-up).
    wait = int(process_timeout_s) if process_timeout_s is not None else timeout_s + 60

    def failed(kind: str, reason: str, run_id: str = "") -> AgentCallFailed:
        took = int((time.monotonic() - started) * 1000)
        log.error(
            "AGENT UNAVAILABLE (%s): %s for %s after %.1fs: %s",
            FAILURE_WORDS.get(kind, kind), agent, purpose or "a call", took / 1000.0, reason,
        )  # fmt: skip
        if data_dir is not None:
            try:
                EventLog(data_dir).append(
                    "arena_agent_calls",
                    {
                        "agent": agent, "purpose": purpose, "ok": False, "kind": kind,
                        "reason": reason[:500], "model_expected": expect_model,
                        "run_id": run_id, "duration_ms": took, "chars_in": len(message),
                        "session": session_key or "",
                    },
                )  # fmt: skip
            except OSError as e:
                log.error("could not record the failed %s call: %s", agent, e)
        return AgentCallFailed(f"{FAILURE_WORDS.get(kind, kind)}: {reason}", kind)

    try:
        exe = _openclaw_bin()
    except AgentCallFailed as e:
        raise failed("error", str(e)) from e
    fd, path = tempfile.mkstemp(suffix=".md", prefix=f"{agent}-", text=True)
    os.close(fd)
    Path(path).write_text(message, encoding="utf-8")
    cmd = [
        exe, "agent", "--agent", agent, "--message-file", path,
        "--json", "--timeout", str(timeout_s),
    ]  # fmt: skip
    if session_key:
        cmd[4:4] = ["--session-key", session_key]
    try:
        # A model call can legitimately run to its timeout with nothing logged; say so, so
        # the watchdog does not report a quiet log as a dead watcher.
        with quiet(wait + 60, f"waiting on {agent}"):
            code, stdout, stderr = _run_bounded(cmd, wait)
    except hidden.TimeoutExpired as e:
        raise failed(
            "timeout", f"{agent} gave no answer within {wait}s; its process tree was stopped"
        ) from e
    except OSError as e:
        raise failed("error", f"{agent} could not be started: {e}") from e
    finally:
        Path(path).unlink(missing_ok=True)

    if code != 0 and not stdout.strip():
        why = f"{agent} exited {code}: {stderr[:400]}"
        raise failed(classify_failure(stderr), why)
    try:
        body = _extract_json(stdout)
    except AgentCallFailed as e:
        raise failed(classify_failure(stdout + "\n" + stderr), str(e)) from e
    meta = ((body.get("result") or {}).get("meta")) or {}
    trace = meta.get("executionTrace") or {}
    payloads = ((body.get("result") or {}).get("payloads")) or []
    text = ""
    for p in payloads:
        if p.get("text"):
            text += p["text"]
    if not text:
        text = meta.get("finalAssistantVisibleText") or ""

    reply = AgentReply(
        agent=agent,
        text=text.strip(),
        model=str(trace.get("winnerModel") or (meta.get("agentMeta") or {}).get("model") or ""),
        requested_model=expect_model,
        thinking=str((meta.get("requestShaping") or {}).get("thinking") or ""),
        run_id=str(body.get("runId") or ""),
        duration_ms=int(meta.get("durationMs") or 0),
        ok=str(body.get("status", "")) == "ok",
        raw=body,
    )
    # A reply OpenClaw did not mark ok is not an answer, whatever text came with it; nor is
    # a reply that is only the plan's limit notice (26 Sep 2026, G1).
    if not reply.ok:
        why = _body_failure(body, reply.text)
        raise failed(classify_failure(why + "\n" + stderr), why, reply.run_id)
    if _looks_like_limit_notice(reply.text):
        raise failed("usage_limit", f"the reply was the plan's limit notice: {reply.text[:300]}",
                     reply.run_id)  # fmt: skip
    if expect_model and not reply.model_matches:
        log.warning(
            "MODEL MISMATCH: %s was configured as %s but OpenClaw ran %s",
            agent, expect_model, reply.model or "(unreported)",
        )  # fmt: skip
    if data_dir is not None:
        EventLog(data_dir).append(
            "arena_agent_calls",
            {
                "agent": agent, "purpose": purpose, "model_ran": reply.model,
                "model_expected": expect_model, "model_matches": reply.model_matches,
                "thinking": reply.thinking,
                "run_id": reply.run_id, "duration_ms": reply.duration_ms, "ok": reply.ok,
                "chars_in": len(message), "chars_out": len(reply.text),
                "session": session_key or "",
            },  # fmt: skip
        )
    log.info(
        "%s replied in %.1fs on %s at %s effort (%d chars)",
        agent, reply.duration_ms / 1000.0, reply.model or "unknown model",
        reply.thinking or "unreported", len(reply.text),
    )  # fmt: skip
    return reply


def _yes_no(text: str, key: str) -> str | None:
    """The value of a KEY: YES/NO line, read from the end. None when there is no such line."""
    for line in reversed(text.splitlines()):
        s = line.strip().upper().replace("*", "").replace("#", "").strip()
        if s.startswith(key):
            value = s.split(":", 1)[-1].strip() if ":" in s else s.replace(key, "")
            return value.strip()
    return None


def parse_verdict(text: str) -> tuple[bool, str]:
    """The reader ends with a TRADE_WORTHY line. Anything unclear is treated as 'no'.

    A missing or malformed verdict must never become a trade: the safe failure is to pass.
    """
    verdict = _yes_no(text, "TRADE_WORTHY")
    if verdict is None:
        return False, "no TRADE_WORTHY line in the reader's summary; treated as no"
    if verdict.startswith("YES"):
        return True, "reader says trade-worthy"
    return False, f"reader says {verdict.lower() or 'no'}"


def parse_can_size(text: str) -> tuple[bool, str]:
    """The reader's second gate: can a position be sized here and exited at sensible cost?

    Same safe failure as TRADE_WORTHY - missing or unclear means no. The two gates are
    separate because they fail for different reasons: real news on a stock nobody can
    trade is still not a trade, and the decider should not be paid to find that out.
    """
    verdict = _yes_no(text, "CAN_SIZE_AND_EXIT")
    if verdict is None:
        return False, "no CAN_SIZE_AND_EXIT line in the reader's summary; treated as no"
    if verdict.startswith("YES"):
        return True, "reader says it can be sized and exited"
    return False, f"reader says it cannot be sized or exited: {verdict.lower() or 'no'}"


def parse_decision(text: str) -> dict:
    """The decider ends with a DECISION block in JSON. Unparseable means no trade."""
    start = text.rfind("{")
    while start != -1:
        try:
            d = json.loads(text[start:].strip().rstrip("`").strip())
            if isinstance(d, dict) and "action" in d:
                return d
        except json.JSONDecodeError:
            pass
        start = text.rfind("{", 0, start)
    return {"action": "pass", "why": "no readable DECISION block; treated as a pass"}
