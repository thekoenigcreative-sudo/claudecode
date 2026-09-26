"""Model calls for the simulator: `claude -p` directly, never OpenClaw (the lab's rule, as
agentcall.py), no tools, cached by (model, effort, system, prompt), with the TOKENS of every
call recorded (input, cache writes, cache reads, output) so usage per simulated day is measured,
not estimated.

Budget (CLOUD_BRIEF "MODELS AND USAGE"): the lab gets `tradesim.budget.weekly_share` (15%) of
the weekly Claude allowance and never touches the live bots' reserve (30%). Every call's rate
limit event reports the plan's seven-day utilisation; the lab's own share is the sum of the
rises across its calls this week (usage_week.json). A call is refused (UsageStop) when the
lab's share is spent, or when the week is at 1 - reserve (70%), or at Rick's stop
(agentcall.USAGE_STOP), whichever comes first. A cached answer costs nothing and is always
allowed.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot import proc
from asxbot.lab import agentcall, store
from asxbot.lab.agentcall import UsageStop

SYD = ZoneInfo("Australia/Sydney")
DECIDER = ("claude-opus-5-5", "high")  # CLOUD_BRIEF: every trading decision, never Sonnet/Fable/max
READER = ("claude-sonnet-5", "low")  # reads announcements, never decides


def _week_file() -> Path:
    return store.lab_local() / "tsim" / "usage_week.json"


def week_state() -> dict:
    now = datetime.now(SYD)
    monday = (now - timedelta(days=now.weekday())).date().isoformat()
    d = store.read_json(_week_file(), {}) or {}
    if d.get("week") != monday:
        d = {"week": monday, "lab_share": 0.0, "last_seen": None, "calls": 0, "tokens": {}}
    return d


def budget_ok(cfg=None) -> tuple[bool, str]:
    conf = ((cfg.get("tradesim") or {}).get("budget") or {}) if cfg is not None else {}
    # ASXBOT_TSIM_WEEKLY_SHARE: a session's own share (the cloud session on its one-time credit,
    # 26 Sep); the stop at 1 - bots_reserve (70% of the week) holds whatever the share
    share = float(os.environ.get("ASXBOT_TSIM_WEEKLY_SHARE") or conf.get("weekly_share", 0.15))
    reserve = float(conf.get("bots_reserve", 0.30))
    st = week_state()
    if st["lab_share"] >= share:
        return False, f"the lab's {share:.0%} of this week's usage is spent ({st['lab_share']:.1%})"
    wk, _ = agentcall.weekly_usage()
    stop = min(1 - reserve, agentcall.USAGE_STOP or 1.0)
    if wk is not None and wk >= stop:
        return False, f"weekly usage {wk:.0%} is at the stop ({stop:.0%}): the bots' reserve"
    return True, ""


def _account(ev_util: float | None, usage: dict) -> None:
    st = week_state()
    if ev_util is not None:
        if st.get("last_seen") is not None and ev_util >= st["last_seen"]:
            st["lab_share"] = round(st["lab_share"] + (ev_util - st["last_seen"]), 6)
        st["last_seen"] = ev_util
    st["calls"] += 1
    for k, v in usage.items():
        if isinstance(v, (int, float)):
            st["tokens"][k] = st["tokens"].get(k, 0) + v
    store.write_json(_week_file(), st)


def cache_file(k: str) -> Path:
    return store.lab_local() / "tsim" / "llm_cache" / k[:2] / f"{k}.json"


def ask(prompt: str, *, system: str, model: str, effort: str, cfg=None, timeout_s: int = 300,
        use_cache: bool = True) -> dict:  # fmt: skip
    """{'text', 'seconds', 'usage': {input, cache_write, cache_read, output, cost_usd},
    'cached', 'error'?}. Raises UsageStop when the budget is spent."""
    k = hashlib.sha256("\x1f".join((model, effort, system, prompt)).encode()).hexdigest()
    cp = cache_file(k)
    if use_cache and cp.exists():
        d = json.loads(cp.read_text(encoding="utf-8"))
        return {**d, "cached": True}
    ok, why = budget_ok(cfg)
    if not ok:
        raise UsageStop(why)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(system)
        sysfile = f.name
    exe = agentcall.claude_exe()
    cmd = [sys.executable, exe] if exe.endswith(".py") else [exe]
    started = time.monotonic()
    cwd = store.lab_local() / "tsim"
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        r = proc.run(
            [*cmd, "-p", "--model", model, "--effort", effort, "--system-prompt-file", sysfile,
             "--tools", "", "--output-format", "stream-json", "--verbose",
             "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
             "--disable-slash-commands"],
            input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout_s, cwd=str(cwd),
        )  # fmt: skip
    except proc.subprocess.TimeoutExpired:
        return {"text": "", "seconds": float(timeout_s), "usage": {}, "cached": False,
                "error": "timeout"}  # fmt: skip
    finally:
        os.unlink(sysfile)
    wall = time.monotonic() - started
    text, err, util, usage, api_s = "", "", None, {}, None
    for line in (r.stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "rate_limit_event":
            agentcall._record_usage(ev)
            info = ev.get("rate_limit_info") or {}
            util = ((info.get("unifiedWindows") or {}).get("seven_day") or {}).get("utilization")
            if info.get("status") == "rejected":
                raise UsageStop("Claude refused the call: plan limit reached")
        elif ev.get("type") == "result":
            text = ev.get("result") or ""
            u = ev.get("usage") or {}
            usage = {
                "input": int(u.get("input_tokens") or 0),
                "cache_write": int(u.get("cache_creation_input_tokens") or 0),
                "cache_read": int(u.get("cache_read_input_tokens") or 0),
                "output": int(u.get("output_tokens") or 0),
                "cost_usd": float(ev.get("total_cost_usd") or 0),
            }
            api_s = (ev.get("duration_ms") or 0) / 1000.0 or None
            if ev.get("is_error"):
                err = text[:300]
    _account(util, usage)
    low = (text + (r.stderr or "")).lower()
    if "hit your" in low and "limit" in low:
        raise UsageStop(text[:200] or "plan limit reached")
    out = {"text": text, "seconds": round(api_s or wall, 1), "usage": usage, "model": model,
           "effort": effort, "at": datetime.now().isoformat(timespec="seconds")}  # fmt: skip
    if err or r.returncode != 0:
        return {**out, "cached": False,
                "error": err or f"exit {r.returncode}: {(r.stderr or '')[-200:]}"}  # fmt: skip
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps(out), encoding="utf-8")
    return {**out, "cached": False}


DECISION_KEYS = (
    "look",
    "orders",
    "alerts",
    "journal",
    "lessons",
    "watchlist",
    "proposals",
    "verdicts",
    "ideas",
    "codes",
    "category",
    "next_wake",
    "clear_alerts",
)


def parse_json(text: str) -> dict | None:
    """The reply's decision: the LAST top-level JSON object that carries a decision's keys (26
    Sep: a reply held the decision and then an imitation tool acknowledgement,
    {"ok": true, "note": ...}; taking the last object threw the decision away), else the last
    object."""
    s = text.strip()
    objs = []
    depth, start, in_str, esc = 0, None, False, False
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    o = json.loads(s[start : i + 1])
                    if isinstance(o, dict):
                        objs.append(o)
                except ValueError:
                    pass
    for o in reversed(objs):
        if any(k in o for k in DECISION_KEYS):
            return o
    return objs[-1] if objs else None
