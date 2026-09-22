"""Calling the two OpenClaw trader agents, and recording which model actually answered.

ARENA.md: "OpenClaw's per-spawn model override has open bug reports where it is silently
ignored, so don't rely on it. A setup test confirms which model produced each step, and
every decision logs its model name."

So the model is set on the agent itself (`openclaw agents add --model`), never per call,
and the model recorded here is the one OpenClaw reports it actually ran
(`meta.executionTrace.winnerModel`) - not the one we asked for, and never the agent's own
claim about itself.

Code hands the reader's summary to the decider. The agents never call each other.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from asxbot.log import EventLog, get_logger

log = get_logger("asxbot.arena.agents")

READER = "trader-reader"
DECIDER = "trader-decider"


class AgentCallFailed(RuntimeError):
    pass


@dataclass
class AgentReply:
    agent: str
    text: str
    model: str  # what OpenClaw reports it actually ran
    requested_model: str
    run_id: str
    duration_ms: int
    ok: bool
    raw: dict

    @property
    def model_matches(self) -> bool:
        return bool(self.model) and self.model.replace("anthropic/", "") == (
            self.requested_model.replace("anthropic/", "")
        )


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
    raise AgentCallFailed(f"no JSON in the openclaw reply: {stdout[:400]}")


def call_agent(
    agent: str,
    message: str,
    *,
    expect_model: str = "",
    timeout_s: int = 600,
    data_dir: Path | None = None,
    purpose: str = "",
) -> AgentReply:
    """Run one turn of an OpenClaw agent and return its text plus the model that ran it."""
    fd, path = tempfile.mkstemp(suffix=".md", prefix=f"{agent}-", text=True)
    os.close(fd)
    Path(path).write_text(message, encoding="utf-8")
    cmd = [
        _openclaw_bin(), "agent", "--agent", agent, "--message-file", path,
        "--json", "--timeout", str(timeout_s),
    ]  # fmt: skip
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s + 60, encoding="utf-8"
        )
    except subprocess.TimeoutExpired as e:
        raise AgentCallFailed(f"{agent} timed out after {timeout_s}s") from e
    finally:
        Path(path).unlink(missing_ok=True)

    if proc.returncode != 0 and not proc.stdout.strip():
        raise AgentCallFailed(f"{agent} exited {proc.returncode}: {proc.stderr[:400]}")
    body = _extract_json(proc.stdout)
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
        run_id=str(body.get("runId") or ""),
        duration_ms=int(meta.get("durationMs") or 0),
        ok=str(body.get("status", "")) == "ok",
        raw=body,
    )
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
                "run_id": reply.run_id, "duration_ms": reply.duration_ms, "ok": reply.ok,
                "chars_in": len(message), "chars_out": len(reply.text),
            },  # fmt: skip
        )
    log.info(
        "%s replied in %.1fs on %s (%d chars)",
        agent, reply.duration_ms / 1000.0, reply.model or "unknown model", len(reply.text),
    )  # fmt: skip
    return reply


def parse_verdict(text: str) -> tuple[bool, str]:
    """The reader ends with a TRADE_WORTHY line. Anything unclear is treated as 'no'.

    A missing or malformed verdict must never become a trade: the safe failure is to pass.
    """
    verdict = None
    for line in reversed(text.splitlines()):
        s = line.strip().upper().replace("*", "").replace("#", "").strip()
        if s.startswith("TRADE_WORTHY"):
            value = s.split(":", 1)[-1].strip() if ":" in s else s.replace("TRADE_WORTHY", "")
            verdict = value.strip()
            break
    if verdict is None:
        return False, "no TRADE_WORTHY line in the reader's summary; treated as no"
    if verdict.startswith("YES"):
        return True, "reader says trade-worthy"
    return False, f"reader says {verdict.lower() or 'no'}"


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
