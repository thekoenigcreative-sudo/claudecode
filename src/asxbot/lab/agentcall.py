"""The lab's calls to the decider's model (PRACTICE_LAB.md 1): `claude -p` directly - never
OpenClaw, so the live agents' sessions and the shared gateway are untouched - with the
decider's standing instructions (docs/agents/trader-decider.AGENTS.md) as the system prompt,
the decider's model and effort from config.yaml, no tools. Every answer is cached by
(model, effort, instructions, packet): a re-run, or another variant that produces the same
packet, costs nothing. Before a call that isn't cached, Claude usage is checked against Rick's
weekly stop (70%): agent practice stops there like every build.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from asxbot import proc
from asxbot.lab import store

USAGE_STOP = 0.70  # Rick, 25 Sep 23:56: builds stop at 70% weekly usage; lab agent calls too


class UsageStop(RuntimeError):
    """Weekly Claude usage is at Rick's stop, or Claude refused the call for a limit."""


def claude_exe() -> str:
    exe = (
        Path(os.environ.get("APPDATA", ""))
        / "npm"
        / "node_modules"
        / "@anthropic-ai"
        / "claude-code"
        / "bin"
        / "claude.exe"
    )
    if os.environ.get("ASXBOT_LAB_CLAUDE"):
        return os.environ["ASXBOT_LAB_CLAUDE"]
    return str(exe) if exe.exists() else (shutil.which("claude") or "claude")


def model_and_effort(cfg) -> tuple[str, str]:
    from asxbot.arena.agents import expected_effort, expected_model

    m = str(expected_model(cfg, "decider") or "claude-opus-5-5").split("/")[-1]
    e = str(expected_effort(cfg, "decider") or "high")
    return m, e


def standing_instructions(cfg) -> str:
    p = Path(cfg.root) / "docs" / "agents" / "trader-decider.AGENTS.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def weekly_usage() -> tuple[float | None, str]:
    """The newest reading of the plan's weekly meter: the lab's own, or the Foreman's feed."""
    best = (None, "")
    for p in (
        store.lab_local() / "usage.json",
        Path(os.environ.get("USERPROFILE", str(Path.home())))
        / ".foreman"
        / "usage"
        / "latest.json",
    ):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("seven_day") is not None and d.get("at", "") > best[1]:
            best = (float(d["seven_day"]), d["at"])
    return best


def _record_usage(ev: dict) -> None:
    info = ev.get("rate_limit_info") or {}
    win = info.get("unifiedWindows") or {}
    wk = (win.get("seven_day") or {}).get("utilization")
    if wk is None:
        return
    d = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "seven_day": wk,
        "five_hour": (win.get("five_hour") or {}).get("utilization"),
        "status": info.get("status"),
    }
    p = store.lab_local() / "usage.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d), encoding="utf-8")


def key(model: str, effort: str, system: str, packet: str) -> str:
    return hashlib.sha256("\x1f".join((model, effort, system, packet)).encode()).hexdigest()


def cache_path(k: str) -> Path:
    return store.lab_local() / "agent_cache" / k[:2] / f"{k}.json"


def ask(
    packet: str,
    *,
    model: str,
    effort: str,
    system: str,
    timeout_s: int = 240,
    stop_at: float | None = USAGE_STOP,
) -> dict:
    """{'text', 'decision', 'seconds', 'cost_usd', 'model', 'cached'}; raises UsageStop."""
    from asxbot.arena.agents import parse_decision

    k = key(model, effort, system, packet)
    cp = cache_path(k)
    if cp.exists():
        return {**json.loads(cp.read_text(encoding="utf-8")), "cached": True}
    wk, _ = weekly_usage()
    if stop_at is not None and wk is not None and wk >= stop_at:
        raise UsageStop(f"weekly Claude usage {wk:.0%} is at Rick's stop ({stop_at:.0%})")
    store.lab_local().mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(system)
        sysfile = f.name
    started = time.monotonic()
    try:
        exe = claude_exe()
        cmd = [sys.executable, exe] if exe.endswith(".py") else [exe]  # tests use a stand-in script
        r = proc.run(
            [
                *cmd,
                "-p",
                "--model",
                model,
                "--effort",
                effort,
                "--system-prompt-file",
                sysfile,
                "--tools",
                "",
                "--output-format",
                "stream-json",
                "--verbose",
                "--no-session-persistence",
                "--setting-sources",
                "",
                # no MCP connectors or skills: 431 tokens of overhead instead of 61k
                "--strict-mcp-config",
                "--disable-slash-commands",
            ],
            input=packet,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            cwd=str(store.lab_local()),
        )
    except proc.subprocess.TimeoutExpired:
        return {
            "text": "",
            "decision": {"action": "reject"},
            "seconds": float(timeout_s),
            "cost_usd": 0.0,
            "model": model,
            "cached": False,
            "error": "timeout",
        }
    finally:
        os.unlink(sysfile)
    wall = time.monotonic() - started
    text, cost, api_s, err = "", 0.0, None, ""
    for line in (r.stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "rate_limit_event":
            _record_usage(ev)
            if (ev.get("rate_limit_info") or {}).get("status") == "rejected":
                raise UsageStop("Claude refused the call: plan limit reached")
        elif ev.get("type") == "result":
            text = ev.get("result") or ""
            cost = float(ev.get("total_cost_usd") or 0)
            api_s = (ev.get("duration_ms") or 0) / 1000.0 or None
            if ev.get("is_error"):
                err = text[:300]
    low = (text + (r.stderr or "")).lower()
    if "hit your" in low and "limit" in low:
        raise UsageStop(text[:200] or "plan limit reached")
    if err or r.returncode != 0:
        return {
            "text": text,
            "decision": {"action": "reject"},
            "seconds": wall,
            "cost_usd": cost,
            "model": model,
            "cached": False,
            "error": err or f"exit {r.returncode}: {(r.stderr or '')[-200:]}",
        }
    out = {
        "text": text,
        "decision": parse_decision(text) if "action" in text else {"action": "reject"},
        "seconds": round(api_s or wall, 1),
        "cost_usd": round(cost, 5),
        "model": model,
        "at": datetime.now().isoformat(timespec="seconds"),
    }
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps(out), encoding="utf-8")
    return {**out, "cached": False}
