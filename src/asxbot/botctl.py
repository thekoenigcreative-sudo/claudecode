"""botctl: change requests and OpenClaw's chat commands, for Rick's own Telegram bots.

Master copy: C:\\Users\\Richa\\.cc-jobs\\changes\\botctl.py. Each bot repo keeps an identical
copy in its package (the dispatcher's `sync_botctl.py` copies it and a test in each repo
checks the header), so a deployed release never imports from outside itself.

Two jobs, both handled in code, never by the AI:

1. CHANGE REQUESTS. Rick asks a bot to change itself ("/change stop sending health bets on
   Sundays", or plainly "can you make it ..."). The bot reads the request back as one line
   with [Build it] [Cancel]; only Rick's tap queues it for the dispatcher in
   C:\\Users\\Richa\\.cc-jobs\\changes, which builds, tests, deploys and reports in the same
   chat. Guardrails (OFF_LIMITS) are checked here in code before anything is offered, and
   again by the dispatcher. A change to a safety limit needs a second tap showing
   old -> new. /changes lists them, /undo rolls back this bot's last one.

2. OPENCLAW'S CHAT COMMANDS (docs/tools/slash-commands.md of the installed OpenClaw):
   /stop /queue /model /models /think /reasoning /status /new /reset /compact /steer
   /help /commands /whoami /tasks, with OpenClaw's names, arguments and replies. The
   bots call OpenClaw agents through `openclaw agent`, so these map onto the bot:
   /model and /think change the bot's own agent entries in ~/.openclaw/openclaw.json
   (`openclaw config set` + `openclaw config validate`, the path the agent-models job
   used; nothing else in that file may change or it is restored), /stop aborts the
   tracked agent turns (`chat.abort` on the gateway, then the CLI process), /queue sets
   what a message does while an answer is running. Everything else OpenClaw has is
   listed in /help with the reason it doesn't apply to a bot.

Stdlib only. Nothing here names a particular bot: each bot passes an Adapter.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

BOTCTL_VERSION = "2026-09-24.1"

CHANGES_DIR = Path(os.environ.get("CC_CHANGES_DIR") or r"C:\Users\Richa\.cc-jobs\changes")
OPENCLAW_JSON = Path(
    os.environ.get("OPENCLAW_CONFIG_PATH") or Path.home() / ".openclaw" / "openclaw.json"
)
RICK_ID = "8998104023"

OFFER_HOURS = 24  # an untapped [Build it] expires
QUESTION_MINUTES = 20  # an unanswered question expires
CAP_DEFAULT = 20  # OpenClaw's messages.queue.cap default (and Rick's config)


# --------------------------------------------------------------------------- hooks
# Bots with strict process/file rules replace these with their own sanctioned helpers.


def _default_run(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, stdin=subprocess.DEVNULL, creationflags=flags,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as e:
        return 127, "", str(e)
    return p.returncode, p.stdout or "", p.stderr or ""


def _default_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


RUN: Callable[[list[str], float], tuple[int, str, str]] = _default_run
WRITE: Callable[[Path, str], None] = _default_write


def openclaw_bin() -> str:
    return shutil.which("openclaw.cmd") or shutil.which("openclaw") or "openclaw"


def claude_bin() -> str:
    exe = (Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / "@anthropic-ai"
           / "claude-code" / "bin" / "claude.exe")
    return str(exe) if exe.exists() else (shutil.which("claude.exe") or "claude.exe")


# --------------------------------------------------------------------------- guardrails

# (key, pattern, rule). Checked on Rick's words and on the summary read back to him. A
# match is refused with the rule and a pointer to Claude; a change request can never
# alter these. `money` applies to the trade bot only (the others never trade shares).
OFF_LIMITS: list[tuple[str, re.Pattern[str], str]] = [
    ("money", re.compile(
        r"\b(real[- ]money|actual money|live[- ]trad\w*|trade live|go live with real|"
        r"real (orders?|trades?|account|shares|broker)|my (broker|brokerage|stake|commsec) "
        r"account|broker:?\s*(live|paper)|live_trading\w*|place (real )?orders? (on|with|"
        r"through|at) (the )?(broker|ibkr|stake)|paper[- ]trad\w* account)", re.I),
     "no real-money orders from the trade bot"),
    ("ibkr", re.compile(
        r"\b(ibkr|interactive brokers|ib gateway|tws)\b[^.?!]{0,60}\b(orders?|buy|sell|"
        r"trade|trading|write|read[- ]?write|place|execute|live)\b|\b(orders?|buy|sell|"
        r"place|execute)\b[^.?!]{0,40}\b(ibkr|interactive brokers|ib gateway)\b", re.I),
     "IBKR stays data-only and read-only"),
    ("secrets", re.compile(
        r"\b(passwords?|passcodes?|pass ?phrases?|api[ _-]?keys?|(bot |access |auth )?tokens?"
        r"|secrets?|credentials?|2fa|one[- ]time codes?|otp|pin numbers?|card numbers?|"
        r"cvv|cvc|login details|sign[- ]in details|secrets\.env|\.env\b)", re.I),
     "no credentials, tokens or passwords handled or moved"),
    ("jarvis", re.compile(
        r"\b(jarvis|main agent|sgst|sentinel[- ]times|the glass|editorial agent|"
        r"workspace\b(?! (tidy|clean))|crons?\b|cron jobs?)", re.I),
     "never touch Jarvis or its workspace and crons"),
    ("shared", re.compile(
        r"\b(openclaw|open claw|openclaw\.json|the gateway|gateway config|all (the )?(bots|"
        r"agents)|every (bot|agent)|other (bots?|agents?)|each (bot|agent))\b", re.I),
     "nothing shared with Jarvis or the other bots, only this bot's own settings"),
]

# Other bots, by what Rick calls them. Each adapter drops its own names.
BOT_NAMES = {
    "layman": r"layman|bet[- ]?bot|betting bot",
    "fetch": (r"fetch bot|fetch's|shop[- ]?bot|shopping bot|\bfetch\b(?= (to|should|can|is|"
              r"isn't|does|doesn't|orders?|shops?|buys?|uses?|sends?|replies|answers)\b)"),
    "trader": r"trader|share bot|trading bot|asx[- ]?bot|the arena",
    "etf": r"etf[- ]?(agent|bot|chat)|the etf",
}


def off_limits(bot: str, *texts: str) -> str | None:
    """The rule a request breaks, or None. Pure code, no AI."""
    body = " \n ".join(t for t in texts if t)
    for key, pat, rule in OFF_LIMITS:
        if key == "money" and bot != "trader":
            continue
        if pat.search(body):
            return rule
    for other, pat in BOT_NAMES.items():
        if other != bot and re.search(pat, body, re.I):
            return "never touch other bots' code (each bot only changes itself)"
    return None


# --------------------------------------------------------------------------- adapter


@dataclass
class Agent:
    target: str  # what Rick types: "decider", "scout"
    agent_id: str  # OpenClaw agent id
    role: str  # plain words for /model and /status
    pinned_model: str  # what the repo expects ("default" restores it)
    pinned_thinking: str
    aliases: tuple[str, ...] = ()
    note: str = ""  # e.g. "screenshots always use Opus 5.5 at high (set in code)"


@dataclass
class Limit:
    key: str  # stable id, e.g. "max_liability_per_bet"
    label: str  # "per-bet liability cap"
    words: str  # regex that means this limit in Rick's words
    current: Callable[[], str]  # current value, formatted ("$250")


@dataclass
class Adapter:
    bot: str  # layman | fetch | trader | etf
    name: str  # The Layman
    repo: str
    about: str  # one paragraph: what the bot does (for the change interpreter)
    abilities: str  # what it already does on request with no code change
    agents: list[Agent]  # the last one is the one Rick chats with
    send: Callable[..., object]  # send(text, buttons=None); buttons = [(label, data)]
    home: Path  # bot's own state folder (outside Drive)
    limits: list[Limit] = field(default_factory=list)
    run_bg: Callable[[Callable[[], object], Callable[[object], None]], None] | None = None
    typing: Callable[[], None] | None = None
    new_session: Callable[[str], str] | None = None  # reason "new"|"reset" -> what was cleared
    session_keys: Callable[[], list[str]] | None = None  # persistent keys (for /new /compact)
    before_agent_change: Callable[[Agent, str, str, str], str | None] | None = None
    after_agent_change: Callable[[Agent, str, str, str, str], str | None] | None = None
    clashes: dict[str, str] = field(default_factory=dict)  # /cmd -> the bot's own meaning
    context_note: str = ""  # how the bot's conversation context works (for /new, /compact)
    own_help: Callable[[], str] | None = None  # the bot's own /help, shown first
    status_extra: Callable[[], list[str]] | None = None  # the bot's own /status lines


# --------------------------------------------------------------------------- change store


def _now() -> datetime:
    return datetime.now()


def _stamp(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat(timespec="seconds")


def req_dir() -> Path:
    return CHANGES_DIR / "requests"


def req_path(rid: str) -> Path:
    return req_dir() / f"{rid}.json"


def load_req(rid: str) -> dict | None:
    p = req_path(rid)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_req(rec: dict) -> None:
    WRITE(req_path(rec["id"]), json.dumps(rec, indent=1, ensure_ascii=False))


def log_event(rec: dict, event: str, note: str = "") -> None:
    line = json.dumps({"at": _stamp(), "id": rec["id"], "bot": rec["bot"], "kind":
                       rec.get("kind"), "event": event, "note": note}, ensure_ascii=False)
    path = CHANGES_DIR / "log.jsonl"
    try:
        old = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        old = ""
    WRITE(path, old + line + "\n")


def set_status(rec: dict, status: str, note: str = "") -> dict:
    rec["status"] = status
    rec.setdefault("history", []).append({"at": _stamp(), "status": status, "note": note})
    save_req(rec)
    log_event(rec, status, note)
    return rec


def list_reqs(bot: str | None = None) -> list[dict]:
    out = []
    try:
        files = sorted(req_dir().glob("*.json"))
    except OSError:
        return out
    for p in files:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if bot is None or rec.get("bot") == bot:
            out.append(rec)
    out.sort(key=lambda r: r.get("created", ""))
    return out


def new_id(bot: str) -> str:
    base = f"{bot}-{_now():%Y%m%d-%H%M%S}"
    rid, n = base, 1
    while req_path(rid).exists():
        n += 1
        rid = f"{base}-{n}"
    return rid


def short(rid: str) -> str:
    """layman-20260924-213501 -> #0924-2135 (what Rick sees)."""
    parts = rid.split("-")
    if len(parts) < 3 or len(parts[1]) != 8 or len(parts[2]) != 6:
        return f"#{rid}"
    return f"#{parts[1][4:]}-{parts[2][:4]}" + (f"/{parts[3]}" if len(parts) > 3 else "")


def kick_dispatcher() -> None:
    vbs = CHANGES_DIR / "dispatch.vbs"
    if vbs.exists():
        RUN(["wscript.exe", "//B", str(vbs)], 30)


STATUS_WORDS = {
    "proposed": "waiting for your tap",
    "asking": "waiting for your answer",
    "needs_second_ok": "waiting for your second OK",
    "queued": "queued",
    "waiting": "waiting",
    "building": "being built",
    "done": "live",
    "undone": "undone",
    "failed": "not done",
    "refused": "refused",
    "cancelled": "cancelled",
    "expired": "expired",
}


# --------------------------------------------------------------------------- interpreter

NL_HINT = re.compile(
    r"""(?ix)
    \b(can|could|would|will)\s+you\s+(please\s+)?(make|change|stop|start|remove|set\s+it|
        have\s+it|let\s+me|turn|add\s+a|add\s+an|update\s+(the|your|how))\b
    | \bmake\s+it\s+(so|stop|always|never|only|say|show|send|tell|ask|use)\b
    | \bmake\s+sure\s+you\s+(always|never)\b
    | \b(stop|quit)\s+(doing|sending|showing|telling|asking|giving|posting|mentioning|using|
        saying|putting|including|adding|suggesting|repeating)\b
    | \bfrom\s+now\s+on\b
    | \b(i\s+want|i'd\s+like|i\s+would\s+like|i\s+need)\s+(you|it)\s+to\s+(always|never|stop|
        start|only|show|send|tell|ask|use|include)\b
    | \bchange\s+(the\s+way|how)\s+(you|it)\b
    | \b(add|create|give\s+me)\s+(a|an)\s+(new\s+)?(command|button|setting|option|feature|
        alert|report|check|summary)\b
    | \b(never|always)\s+(send|show|tell|suggest|mention|say|put|include|ask|use|check)\b
    | \bdon'?t\s+ever\b
    """
)


def looks_like_change(text: str) -> bool:
    return bool(text) and not text.startswith("/") and bool(NL_HINT.search(text))


SCHEMA = {
    "type": "object",
    "properties": {
        "is_change": {"type": "boolean"},
        "runtime_hint": {"type": ["string", "null"]},
        "summary": {"type": "string"},
        "question": {"type": ["string", "null"]},
        "limits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"key": {"type": "string"},
                               "new_value": {"type": ["string", "null"]}},
                "required": ["key", "new_value"],
                "additionalProperties": False,
            },
        },
        "off_limits": {"type": ["string", "null"]},
    },
    "required": ["is_change", "runtime_hint", "summary", "question", "limits", "off_limits"],
    "additionalProperties": False,
}

INTERPRET = """You read a change request that Rick sent to one of his own Telegram bots, and
say back, in ONE plain line, exactly what will change. You do not make the change and you
do not chat. Output JSON only, matching the schema.

THE BOT: {name}. {about}

WHAT IT ALREADY DOES ON REQUEST, WITH NO CODE CHANGE: {abilities}

ITS SAFETY LIMITS (key: label, now): {limits}

OFF LIMITS FOR ANY CHANGE REQUEST (these need Claude, not the bot): real-money orders from
the trade bot; IBKR must stay data-only and read-only; handling or moving credentials,
tokens or passwords; anything to do with Jarvis (the main agent), its workspace or crons;
other bots' code; shared OpenClaw settings beyond this bot's own agent entry.

RICK'S WORDS (verbatim): {words}
{answer}
Fill in:
- is_change: true if Rick wants the bot to behave differently from now on (its code,
  wording, rules, schedule or settings). false if it is a one-off task or question for the
  bot to just do now, or something it already does on request (then say how in
  runtime_hint, e.g. "Send /pause").
- summary: one line, at most 25 words, plain English, starting with a verb, saying exactly
  what will change, e.g. "Stop sending health bets on Sundays; other days unchanged." No
  jargon, no file names. If is_change is false, a short line saying what he asked.
- question: {question_rule}
- limits: every safety limit above this would change, by key, with the new value Rick gave
  (null if he didn't give one). Empty if none.
- off_limits: if the request touches anything OFF LIMITS, which rule, in a few words; else
  null.
"""


def interpret(ad: Adapter, words: str, answer: tuple[str, str] | None = None) -> dict:
    limits = "; ".join(f"{lim.key}: {lim.label}, now {_safe(lim.current)}"
                       for lim in ad.limits) or "none"
    if answer:
        extra = (f"\nYOU ALREADY ASKED: {answer[0]}\nRICK ANSWERED: {answer[1]}\n")
        qrule = "null. You have had your one question; make the best reading."
    else:
        extra = ""
        qrule = ("ONE short question, only if the request is genuinely ambiguous (two "
                 "reasonable readings that would build different things, or a safety limit "
                 "with no new value). Otherwise null.")
    prompt = INTERPRET.format(name=ad.name, about=ad.about, abilities=ad.abilities,
                              limits=limits, words=words, answer=extra, question_rule=qrule)
    cmd = [claude_bin(), "-p", prompt, "--model", "claude-sonnet-5", "--tools", "",
           "--safe-mode", "--strict-mcp-config", "--no-session-persistence", "--output-format", "json",
           "--json-schema", json.dumps(SCHEMA, separators=(",", ":"))]
    rc, out, err = RUN(cmd, 180)
    try:
        data = json.loads(out)
        res = data.get("structured_output") or json.loads(data.get("result") or "{}")
        if not isinstance(res, dict) or "summary" not in res:
            raise ValueError("no summary")
    except (ValueError, TypeError) as e:
        raise RuntimeError(f"interpreter failed (exit {rc}): {(err or out)[:200]} {e}") from e
    res["limits"] = [x for x in res.get("limits") or []
                     if any(x.get("key") == lim.key for lim in ad.limits)]
    return res


def _safe(fn: Callable[[], str]) -> str:
    try:
        return str(fn())
    except Exception:  # noqa: BLE001 - a broken reader must not block a change request
        return "unknown"


# --------------------------------------------------------------------------- models


def load_openclaw() -> dict:
    return json.loads(OPENCLAW_JSON.read_text(encoding="utf-8-sig"))


def allowed_models(cfg: dict | None = None) -> list[str]:
    cfg = cfg if cfg is not None else load_openclaw()
    models = list(((cfg.get("agents") or {}).get("defaults") or {}).get("models") or {})
    return sorted(models, key=_model_rank, reverse=True)


def _model_rank(m: str) -> tuple:
    fam = 2 if "opus" in m else 1 if "sonnet" in m else 0
    nums = tuple(int(x) for x in re.findall(r"\d+", m.split("/")[-1]))
    return (nums[:1], fam, nums[1:])


def pretty_model(m: str | None) -> str:
    if not m:
        return "not set"
    base = m.split("/")[-1].replace("claude-", "")
    mt = re.match(r"([a-z]+)-(\d+)(?:-(\d+))?$", base)
    if not mt:
        return base
    fam, a, b = mt.groups()
    return f"{fam.capitalize()} {a}{'.' + b if b else ''}"


def _norm(s: str) -> str:
    s = s.lower().replace("anthropic/", "").replace("claude-", "").replace("claude ", "")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def resolve_model(word: str, allowed: list[str]) -> tuple[str | None, str]:
    """(model id, note). Accepts 3, opus-5-5, "opus 5.5", claude-opus-5-5, anthropic/..."""
    w = word.strip()
    if w.isdigit():
        i = int(w) - 1
        return (allowed[i], "") if 0 <= i < len(allowed) else (None, "no model with that number")
    nw = _norm(w)
    exact = [m for m in allowed if _norm(m) == nw]
    if exact:
        return exact[0], ""
    pref = [m for m in allowed if _norm(m).startswith(nw)]
    if len(pref) == 1:
        return pref[0], ""
    if pref:
        best = sorted(pref, key=_model_rank, reverse=True)[0]
        return best, f"(the newest {nw.capitalize()})"
    return None, "not one of the allowed models"


THINK_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh", "adaptive", "max"]
THINK_ALIASES = {"x-high": "xhigh", "x_high": "xhigh", "extra-high": "xhigh",
                 "extra high": "xhigh", "extra_high": "xhigh", "highest": "high"}
THINK_CLEAR = {"default", "inherit", "clear", "reset", "unpin"}


def agent_entry(cfg: dict, agent_id: str) -> tuple[int, dict]:
    for i, a in enumerate((cfg.get("agents") or {}).get("list") or []):
        if a.get("id") == agent_id:
            return i, a
    raise KeyError(agent_id)


def effective(cfg: dict, agent_id: str) -> tuple[str, str]:
    _, a = agent_entry(cfg, agent_id)
    d = (cfg.get("agents") or {}).get("defaults") or {}
    model = a.get("model") or d.get("model")
    if isinstance(model, dict):
        model = model.get("primary")
    think = a.get("thinkingDefault") or d.get("thinkingDefault") or "the model's default"
    return str(model), str(think)


class ChangeFailed(Exception):
    pass


_OC_LOCK = threading.Lock()


def _diff_paths(a, b, path="") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in set(a) | set(b):
            out += _diff_paths(a.get(k, ...), b.get(k, ...), f"{path}.{k}" if path else k)
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = []
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            out += _diff_paths(x, y, f"{path}[{i}]")
        return out
    return [] if a == b else [path]


def set_agent_field(agent_id: str, fld: str, value: str | None) -> None:
    """Set (or unset) agents.list[<this agent>].<fld> through OpenClaw's own CLI, validate,
    and prove nothing else changed. On any doubt, put the file back exactly as it was."""
    if fld not in ("model", "thinkingDefault"):
        raise ChangeFailed(f"{fld} is not a field a bot may change")
    with _OC_LOCK, _file_lock(CHANGES_DIR / "openclaw-write.lock"):
        before = load_openclaw()
        idx, _ = agent_entry(before, agent_id)
        backup = OPENCLAW_JSON.with_name(
            f"openclaw.json.bak-{_now():%Y%m%d-%H%M%S}-botctl-{agent_id}")
        shutil.copy2(OPENCLAW_JSON, backup)
        path = f"agents.list[{idx}].{fld}"
        oc = openclaw_bin()
        if value is None:
            rc, out, err = RUN([oc, "config", "unset", path], 120)
        else:
            rc, out, err = RUN([oc, "config", "set", path, value], 120)
        problem = None
        if rc != 0:
            problem = f"openclaw config refused it: {(err or out).strip()[:200]}"
        else:
            vrc, vout, verr = RUN([oc, "config", "validate"], 120)
            if vrc != 0 or "valid" not in (vout + verr).lower() or "invalid" in (
                    vout + verr).lower():
                problem = f"openclaw config validate failed: {(verr or vout).strip()[:200]}"
        if problem is None:
            after = load_openclaw()
            j, entry = agent_entry(after, agent_id)
            changed = [p for p in _diff_paths(before, after) if not p.startswith("meta.")]
            want = [f"agents.list[{idx}].{fld}"]
            if j != idx or entry.get(fld) != value or sorted(changed) != want:
                problem = f"unexpected result in openclaw.json ({', '.join(changed) or 'none'})"
        if problem:
            shutil.copy2(backup, OPENCLAW_JSON)
            raise ChangeFailed(problem)


@contextlib.contextmanager
def _file_lock(path: Path, wait: float = 60.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + wait
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > 300:
                    path.unlink()
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                raise ChangeFailed("another settings change is in progress; try again") from None
            time.sleep(0.5)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


# --------------------------------------------------------------------------- turns (/stop)


class Turn:
    def __init__(self, key: str, agent: str, what: str):
        self.key, self.agent, self.what = key, agent, what
        self.started = time.time()
        self.stopped = False

    def full_key(self) -> str:
        return self.key if self.key.startswith("agent:") else f"agent:{self.agent}:{self.key}"


class Stopped(Exception):
    """The turn was stopped by /stop (or /queue interrupt); drop its result."""


class TurnRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._active: dict[int, Turn] = {}

    @contextlib.contextmanager
    def track(self, key: str, agent: str, what: str):
        t = Turn(key, agent, what)
        with self._lock:
            self._active[id(t)] = t
        try:
            yield t
        finally:
            with self._lock:
                self._active.pop(id(t), None)

    def running(self) -> list[Turn]:
        with self._lock:
            return list(self._active.values())

    def stop_all(self) -> list[Turn]:
        turns = self.running()
        for t in turns:
            t.stopped = True
            threading.Thread(target=abort_turn, args=(t,), daemon=True).start()
        return turns


TURNS = TurnRegistry()


# OpenClaw's own /stop is chat.abort on the session. `openclaw gateway call` connects with
# least-privilege scopes, which the gateway refuses for another client's run
# ("unauthorized"), so the call goes through OpenClaw's public plugin SDK client with the
# operator.admin scope (local loopback gateway, its own token). With the claude-cli
# runtime the gateway marks the run aborted and drops its reply; the CLI process is then
# ended so the bot is free at once.
ABORT_JS = r"""
import { pathToFileURL } from "node:url";
const sdk = process.env.APPDATA + "/npm/node_modules/openclaw/dist/plugin-sdk/gateway-runtime.js";
const { callGatewayFromCli } = await import(pathToFileURL(sdk).href);
const res = await callGatewayFromCli("chat.abort", { json: true, timeout: "20000" },
  { sessionKey: process.argv[1] }, { scopes: ["operator.admin"], progress: false });
console.log(JSON.stringify(res));
"""


def node_bin() -> str:
    exe = Path(r"C:\Program Files\nodejs\node.exe")
    return str(exe) if exe.exists() else (shutil.which("node.exe") or "node.exe")


def abort_turn(t: Turn) -> None:
    """chat.abort on the gateway (what OpenClaw's own /stop does), then end the CLI process
    if it is still there (an embedded fallback run doesn't hear chat.abort)."""
    RUN([node_bin(), "--input-type=module", "-e", ABORT_JS, t.full_key()], 60)
    time.sleep(2)
    key = re.sub(r"[^A-Za-z0-9:._-]", "", t.key)
    if len(key) < 8:
        return
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -in 'node.exe','cmd.exe' "
          f"-and $_.CommandLine -like '*{key}*' }} | ForEach-Object {{ Stop-Process -Id "
          "$_.ProcessId -Force -ErrorAction SilentlyContinue }")
    RUN(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps], 60)


def ago(t0: float) -> str:
    s = int(time.time() - t0)
    return f"{s}s" if s < 90 else f"{s // 60} min"


# --------------------------------------------------------------------------- queue

QUEUE_MODES = ("steer", "followup", "collect", "interrupt")
DROP = ("summarize", "old", "new")


def _duration_ms(v: str) -> int | None:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)?", v.strip().lower())
    if not m:
        return None
    mult = {"ms": 1, "s": 1000, "m": 60000, "h": 3600000, "d": 86400000}[m.group(2) or "ms"]
    return int(float(m.group(1)) * mult)


class Conveyor:
    """One conversational turn at a time; what a message does while one runs follows the
    /queue mode, as OpenClaw defines it (docs/concepts/queue.md). `start(text)` begins a
    turn (the bot's own code); the bot calls `finished()` when that turn's reply is dealt
    with, and `tick()` from its main loop (for the debounce window)."""

    def __init__(self, ctl: BotCtl, start: Callable[[str], None]):
        self.ctl, self.start = ctl, start
        self.lock = threading.RLock()
        self.busy = False
        self.pending: list[tuple[float, str]] = []
        self.summaries: list[str] = []

    def submit(self, text: str) -> str | None:
        """Returns a line to tell Rick (or None)."""
        say = None
        with self.lock:
            if not self.busy:
                self.busy = True
                run = text
            else:
                run = None
                q = self.ctl.queue()
                if q["mode"] == "interrupt":
                    stopped = self.ctl.stop_turns()
                    self.pending = [(time.time(), text)]
                    self.summaries = []
                    say = ("Stopped the answer I was working on; doing this one instead."
                           if stopped else None)
                elif len(self.pending) >= q["cap"]:
                    if q["drop"] == "new":
                        return "I'm still on the last one and the queue is full, so I've dropped that message."
                    old = self.pending.pop(0)[1]
                    if q["drop"] == "summarize":
                        self.summaries.append(old[:120])
                    self.pending.append((time.time(), text))
                else:
                    self.pending.append((time.time(), text))
        if run is not None:
            self.start(run)
        return say

    def finished(self) -> None:
        with self.lock:
            self.busy = False
        self.tick()

    def tick(self) -> None:
        with self.lock:
            if self.busy or not self.pending:
                return
            q = self.ctl.queue()
            if time.time() - self.pending[-1][0] < q["debounce_ms"] / 1000:
                return
            if q["mode"] == "collect":
                items = [t for _, t in self.pending]
                self.pending = []
                text = items[0] if len(items) == 1 else (
                    "Messages that came in while I was busy:\n"
                    + "\n".join(f"{i}. {t}" for i, t in enumerate(items, 1)))
            else:
                text = self.pending.pop(0)[1]
            if self.summaries:
                text = ("(Earlier messages dropped while the queue was full: "
                        + " | ".join(self.summaries) + ")\n" + text)
                self.summaries = []
            self.busy = True
        self.start(text)

    def clear(self) -> int:
        with self.lock:
            n = len(self.pending)
            self.pending, self.summaries = [], []
            return n

    def waiting(self) -> int:
        with self.lock:
            return len(self.pending)


# --------------------------------------------------------------------------- the controller

NOT_HERE = {
    # command: reason it doesn't apply to a bot like this
    "verbose": "it only sends you results, never its tool steps",
    "trace": "there's no plugin trace in a bot chat",
    "fast": "fast mode isn't offered for these models",
    "usage": "there's no per-reply usage footer here",
    "elevated": "host commands and exec settings are shared with Jarvis; ask Claude",
    "exec": "host commands and exec settings are shared with Jarvis; ask Claude",
    "bash": "no shell from a bot chat; ask Claude",
    "config": "those settings are shared with Jarvis; ask Claude",
    "mcp": "those settings are shared with Jarvis; ask Claude",
    "plugins": "plugins are shared with Jarvis; ask Claude",
    "debug": "those settings are shared with Jarvis; ask Claude",
    "restart": "a restart would stop Jarvis and the other bots too; ask Claude",
    "send": "the send policy is shared with Jarvis",
    "allowlist": "allowlists are shared with Jarvis; ask Claude",
    "approve": "there are no approval prompts of that kind here",
    "name": "there's no named session to rename here",
    "session": "a bot has no thread bindings",
    "export-session": "a bot keeps its own chat log instead",
    "export-trajectory": "a bot keeps its own chat log instead",
    "tools": "its tools are fixed by its safety rules, not per chat",
    "context": "a bot builds its own context from recent messages",
    "goal": "there are no goals to manage here",
    "diagnostics": "support reports go through Claude",
    "crestodian": "setup and repair go through Claude",
    "skill": "there are no chat skills here",
    "learn": "it doesn't learn skills from chat",
    "btw": "a bot has no side-question lane; just ask",
    "subagents": "it doesn't start helpers of its own",
    "acp": "a bot has no ACP sessions",
    "focus": "a bot has no thread bindings",
    "unfocus": "a bot has no thread bindings",
    "agents": "there are no thread bindings here (see /model)",
    "tts": "a bot replies in text only",
    "activation": "a bot chat is private; there's no group activation",
    "dreaming": "there's no memory dreaming here",
    "pair": "device pairing is shared with Jarvis",
    "phone": "no phone node from a bot chat",
    "voice": "a bot replies in text only",
    "card": "LINE cards don't apply to Telegram",
    "codex": "a bot doesn't use Codex",
    "login": "Codex/OpenAI login doesn't apply to a bot",
    "dock-telegram": "a bot is already Telegram-only",
    "dock-discord": "a bot is Telegram-only",
    "dock-slack": "a bot is Telegram-only",
    "dock-mattermost": "a bot is Telegram-only",
}
ALIASES = {"thinking": "think", "t": "think", "reason": "reasoning", "v": "verbose",
           "elev": "elevated", "export": "export-session", "trajectory": "export-trajectory",
           "tell": "steer", "id": "whoami", "side": "btw", "plugin": "plugins",
           "dock_telegram": "dock-telegram", "dock_discord": "dock-discord",
           "dock_slack": "dock-slack", "dock_mattermost": "dock-mattermost"}
HANDLED = ("stop", "queue", "model", "models", "think", "reasoning", "status", "new", "reset",
           "compact", "steer", "help", "commands", "whoami", "tasks", "change", "changes",
           "undo")


def parse_command(text: str) -> tuple[str, str] | None:
    """'/Think: high' -> ('think', 'high'); '/model@my_bot x' -> ('model', 'x')."""
    t = (text or "").strip()
    if not t.startswith("/") or len(t) < 2:
        return None
    m = re.match(r"/([A-Za-z0-9_-]+)(?:@[A-Za-z0-9_]+)?\s*:?\s*(.*)$", t, re.S)
    if not m:
        return None
    cmd = m.group(1).lower()
    return ALIASES.get(cmd, cmd), m.group(2).strip()


class BotCtl:
    def __init__(self, ad: Adapter):
        self.ad = ad
        self.conveyor: Conveyor | None = None
        self.state_path = ad.home / "botctl-state.json"
        self.settings_path = ad.home / "agent-settings.json"
        self._state_lock = threading.Lock()

    # ---------------------------------------------------------------- state
    def _state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, st: dict) -> None:
        WRITE(self.state_path, json.dumps(st, indent=1, ensure_ascii=False))

    def queue(self) -> dict:
        q = self._state().get("queue") or {}
        base = self._openclaw_queue()
        return {"mode": q.get("mode") or base["mode"],
                "debounce_ms": int(q.get("debounce_ms", base["debounce_ms"])),
                "cap": int(q.get("cap", base["cap"])),
                "drop": q.get("drop") or base["drop"],
                "override": bool(q)}

    @staticmethod
    def _openclaw_queue() -> dict:
        """The configured default, as OpenClaw resolves it (messages.queue, then built-in)."""
        try:
            mq = (load_openclaw().get("messages") or {}).get("queue") or {}
        except (OSError, ValueError):
            mq = {}
        return {"mode": mq.get("mode") or "steer", "debounce_ms": int(mq.get("debounceMs", 500)),
                "cap": int(mq.get("cap", CAP_DEFAULT)), "drop": mq.get("drop") or "summarize"}

    # ---------------------------------------------------------------- entry points
    def handle(self, text: str, from_id: str | None = None) -> bool:
        """A whole message. True if it was one of the commands handled here."""
        parsed = parse_command(text)
        if not parsed:
            return False
        cmd, arg = parsed
        if cmd in self.ad.clashes:
            return False  # the bot keeps its own meaning (it may call extras below)
        if cmd in HANDLED:
            getattr(self, "cmd_" + cmd.replace("-", "_"))(arg)
            return True
        if cmd in NOT_HERE or cmd.startswith("dock"):
            reason = NOT_HERE.get(cmd, "a bot is Telegram-only")
            self.say(f"/{cmd} is one of Jarvis's commands that doesn't apply to "
                     f"{self.ad.name}: {reason}. /help lists what works here.")
            return True
        return False

    def maybe_change(self, text: str) -> bool:
        """Plain chat that reads like a change request. Call it just before the message
        would go to the AI. True if it was taken (an offer or a question follows)."""
        if self.answer_pending(text):
            return True
        if not looks_like_change(text):
            return False
        self._interpret_then_offer(text, from_chat=True)
        return True

    def answer_pending(self, text: str) -> bool:
        """If the bot asked Rick its one question, this message is the answer."""
        rec = self._open_question()
        if not rec or not text or text.startswith("/"):
            return False
        rec["answer"] = [rec.get("question") or "", text]
        set_status(rec, "proposed", "answered")
        self._interpret_then_offer(rec["words"], answer=(rec["answer"][0], text), rec=rec)
        return True

    def on_button(self, data: str, from_id: str | None = None) -> bool:
        if not data.startswith("chg:"):
            return False
        if from_id is not None and str(from_id) != RICK_ID:
            return True  # only Rick's tap counts
        _, act, rid = (data.split(":", 2) + ["", ""])[:3]
        if act == "x":
            self.say("OK, nothing undone.")
            return True
        rec = load_req(rid)
        if not rec or rec.get("bot") != self.ad.bot:
            self.say("I can't find that request any more.")
            return True
        if act == "c":
            if rec["status"] in ("proposed", "asking", "needs_second_ok"):
                set_status(rec, "cancelled", "Rick tapped Cancel")
                self.say(f"Cancelled {short(rid)}. Nothing will change.")
            elif rec["status"] in ("queued", "waiting"):
                set_status(rec, "cancelled", "Rick tapped Cancel before it started")
                self.say(f"Cancelled {short(rid)} before it started. Nothing will change.")
            else:
                self.say(f"{short(rid)} is already {STATUS_WORDS.get(rec['status'], rec['status'])}.")
            return True
        if act == "b":
            self._build_tapped(rec)
            return True
        if act == "s":
            if rec["status"] != "needs_second_ok":
                self.say(f"{short(rid)} is {STATUS_WORDS.get(rec['status'], rec['status'])}.")
                return True
            rec["second_ok"] = _stamp()
            self._queue(rec)
            return True
        if act == "u":
            self._undo_tapped(rec)
            return True
        return True

    def tick(self) -> None:
        if self.conveyor:
            self.conveyor.tick()

    # ---------------------------------------------------------------- helpers
    def say(self, text: str, buttons: list[tuple[str, str]] | None = None) -> None:
        if buttons:
            self.ad.send(text, buttons)
        else:
            self.ad.send(text)

    def _bg(self, work: Callable[[], object], done: Callable[[object], None]) -> None:
        if self.ad.run_bg:
            self.ad.run_bg(work, done)
        else:
            try:
                res: object = work()
            except Exception as e:  # noqa: BLE001
                res = e
            done(res)

    def _agent(self, word: str) -> Agent | None:
        w = word.lower()
        for a in self.ad.agents:
            if w in (a.target.lower(), a.agent_id.lower(), *[x.lower() for x in a.aliases]):
                return a
        return None

    def stop_turns(self) -> list[Turn]:
        return TURNS.stop_all()

    # ---------------------------------------------------------------- change requests
    def cmd_change(self, arg: str) -> None:
        if not arg:
            self.say("Tell me what to change, in your own words, e.g.\n"
                     "/change stop sending health bets on Sundays")
            return
        self._interpret_then_offer(arg, from_chat=False)

    def _interpret_then_offer(self, words: str, from_chat: bool = False,
                              answer: tuple[str, str] | None = None,
                              rec: dict | None = None) -> None:
        rule = off_limits(self.ad.bot, words)
        if rule:
            self._refuse(words, rule, rec)
            return
        if self.ad.typing:
            with contextlib.suppress(Exception):
                self.ad.typing()

        def work():
            return interpret(self.ad, words, answer)

        def done(res):
            self._offer(words, res, from_chat, answer, rec)

        self._bg(work, done)

    def _refuse(self, words: str, rule: str, rec: dict | None = None) -> None:
        rec = rec or self._new_rec(words)
        rec["refused_rule"] = rule
        set_status(rec, "refused", rule)
        self.say(f"I can't take that one as a change request: {rule}. That's for Claude, "
                 "so ask Claude directly. Nothing has changed.")

    def _new_rec(self, words: str, kind: str = "change") -> dict:
        rid = new_id(self.ad.bot)
        return {"id": rid, "bot": self.ad.bot, "name": self.ad.name, "repo": self.ad.repo,
                "kind": kind, "words": words, "created": _stamp(), "status": "new",
                "history": []}

    def _offer(self, words, res, from_chat, answer, rec) -> None:
        if isinstance(res, BaseException):
            if from_chat:
                self.say("I couldn't work out whether that was a change request (the reader "
                         "didn't answer). If it was, send it again as /change <what to change>.")
            else:
                self.say("I couldn't read that change request just now (the reader didn't "
                         "answer). Please try /change again in a minute. Nothing has changed.")
            if rec:
                set_status(rec, "failed", f"interpreter: {res}")
            return
        if from_chat and not res.get("is_change"):
            # Not a change after all: hand it back to the bot as ordinary chat.
            if rec is None and self._handoff:
                self._handoff(words)
            return
        rule = off_limits(self.ad.bot, words, res.get("summary", "")) or (
            str(res["off_limits"]) if res.get("off_limits") else None)
        rec = rec or self._new_rec(words)
        if rule:
            self._refuse(words, rule, rec)
            return
        rec["summary"] = res.get("summary", "").strip()
        rec["runtime_hint"] = res.get("runtime_hint")
        rec["limits"] = self._limit_rows(words, res)
        if res.get("question") and not answer:
            rec["question"] = res["question"].strip()
            set_status(rec, "asking", rec["question"])
            self.say(f"One question first: {rec['question']}")
            return
        set_status(rec, "proposed", rec["summary"])
        lines = [f"Change for {self.ad.name} ({short(rec['id'])}):", rec["summary"]]
        if rec.get("runtime_hint"):
            lines.append(f"(You can also do this now without a change: {rec['runtime_hint']})")
        if rec["limits"]:
            lines.append("This changes a safety limit, so I'll ask for a second OK: "
                         + "; ".join(f"{x['label']} {x['old']} -> {x['new']}"
                                     for x in rec["limits"]))
        self.say("\n".join(lines), [("Build it", f"chg:b:{rec['id']}"),
                                     ("Cancel", f"chg:c:{rec['id']}")])

    _handoff: Callable[[str], None] | None = None

    def set_handoff(self, fn: Callable[[str], None]) -> None:
        """Where a chat message goes if it turns out not to be a change request."""
        self._handoff = fn

    def _limit_rows(self, words: str, res: dict) -> list[dict]:
        rows: dict[str, dict] = {}
        text = f"{words}\n{res.get('summary', '')}"
        for lim in self.ad.limits:
            hit = next((x for x in res.get("limits") or [] if x.get("key") == lim.key), None)
            if hit or re.search(lim.words, text, re.I):
                new = (hit or {}).get("new_value") or "as you described"
                rows[lim.key] = {"key": lim.key, "label": lim.label,
                                 "old": _safe(lim.current), "new": str(new)}
        return list(rows.values())

    def _open_question(self) -> dict | None:
        cutoff = _stamp(_now() - timedelta(minutes=QUESTION_MINUTES))
        for rec in reversed(list_reqs(self.ad.bot)):
            if rec.get("status") == "asking" and rec["history"][-1]["at"] >= cutoff:
                return rec
            if rec.get("status") == "asking":
                set_status(rec, "expired", "question not answered")
        return None

    def _build_tapped(self, rec: dict) -> None:
        st = rec["status"]
        if st != "proposed":
            self.say(f"{short(rec['id'])} is {STATUS_WORDS.get(st, st)}.")
            return
        if rec["history"] and rec["history"][-1]["at"] < _stamp(
                _now() - timedelta(hours=OFFER_HOURS)):
            set_status(rec, "expired", "offer older than a day")
            self.say("That offer is more than a day old, so it has lapsed. Ask again and "
                     "I'll read it back fresh.")
            return
        if rec.get("limits") and not rec.get("second_ok"):
            set_status(rec, "needs_second_ok", "safety limit")
            rows = "\n".join(f"- {x['label']}: {x['old']} -> {x['new']}" for x in rec["limits"])
            self.say(f"Second OK needed. {short(rec['id'])} changes a safety limit:\n{rows}",
                     [("Yes, change the limit", f"chg:s:{rec['id']}"),
                      ("Cancel", f"chg:c:{rec['id']}")])
            return
        self._queue(rec)

    def _queue(self, rec: dict) -> None:
        rec["tapped"] = _stamp()
        set_status(rec, "queued", "Rick tapped Build it")
        ahead = [r for r in list_reqs() if r.get("repo") == rec["repo"] and r["id"] != rec["id"]
                 and r.get("status") in ("queued", "waiting", "building")]
        extra = (f" There {'is' if len(ahead) == 1 else 'are'} {len(ahead)} ahead of it, "
                 "so it will start after those.") if ahead else ""
        self.say(f"Queued {short(rec['id'])}. I'll build it, test it and put it live, then "
                 f"tell you here what changed (usually 10-30 minutes).{extra}")
        kick_dispatcher()

    def cmd_changes(self, arg: str) -> None:
        recs = [r for r in list_reqs(self.ad.bot) if r.get("status") not in ("new",)]
        if not recs:
            self.say("No change requests yet. Ask with /change <what to change>.")
            return
        lines = [f"{self.ad.name}: recent change requests"]
        for r in recs[-10:][::-1]:
            what = r.get("summary") or r.get("words", "")
            if r.get("kind") == "undo":
                what = f"undo {short(r.get('undo_of', ''))}"
            st = STATUS_WORDS.get(r["status"], r["status"])
            when = r.get("history", [{}])[-1].get("at", "")[5:16].replace("T", " ")
            note = ""
            if r["status"] == "waiting" and r.get("wait_note"):
                note = f" ({r['wait_note']})"
            if r["status"] in ("failed", "refused") and r.get("history"):
                note = f" ({r['history'][-1].get('note', '')[:80]})"
            lines.append(f"{short(r['id'])} {st}{note}, {when}: {what[:110]}")
        self.say("\n".join(lines))

    def cmd_undo(self, arg: str) -> None:
        recs = list_reqs(self.ad.bot)
        busy = [r for r in recs if r.get("status") in ("queued", "waiting", "building")]
        if busy:
            self.say(f"{short(busy[-1]['id'])} is still {STATUS_WORDS[busy[-1]['status']]}. "
                     "Undo works on a finished change, so wait for its report first.")
            return
        done = [r for r in recs if r.get("status") == "done" and r.get("kind") == "change"]
        if not done:
            self.say(f"There's no finished change to undo on {self.ad.name}.")
            return
        last = done[-1]
        self.say(f"Undo {short(last['id'])}: {last.get('summary', last['words'])}\n"
                 "This puts back the version from before it and restarts the same way.",
                 [("Undo it", f"chg:u:{last['id']}"), ("Cancel", "chg:x:none")])

    def _undo_tapped(self, target: dict) -> None:
        if target.get("status") != "done":
            self.say(f"{short(target['id'])} is {STATUS_WORDS.get(target['status'])}; "
                     "there's nothing to undo.")
            return
        rec = self._new_rec(f"/undo {short(target['id'])}", kind="undo")
        rec["undo_of"] = target["id"]
        rec["summary"] = f"Undo {short(target['id'])}: {target.get('summary', '')}"
        rec["limits"] = []
        set_status(rec, "proposed", rec["summary"])
        self._queue(rec)

    # ---------------------------------------------------------------- OpenClaw's commands
    def cmd_stop(self, arg: str = "") -> None:
        self.say(self.stop_text())

    def stop_text(self) -> str:
        turns = self.stop_turns()
        dropped = self.conveyor.clear() if self.conveyor else 0
        parts = [f"{t.what} (running {ago(t.started)})" for t in turns]
        if not parts and not dropped:
            return "Nothing was running, so there was nothing to stop."
        out = "Stopped: " + "; ".join(parts) if parts else "Nothing was running."
        if dropped:
            out += f" Dropped {dropped} message{'s' if dropped != 1 else ''} waiting behind it."
        busy = [r for r in list_reqs(self.ad.bot) if r.get("status") == "building"]
        if busy:
            out += (f"\n{short(busy[-1]['id'])} is being built and carries on (stopping it "
                    "halfway could leave me half-updated); /undo reverses it once it's live.")
        return out

    def cmd_steer(self, arg: str) -> None:
        if not arg:
            self.say("Usage: /steer <message>")
            return
        running = TURNS.running()
        if not running or not self.conveyor:
            if self.conveyor:
                self.conveyor.submit(arg)
            else:
                self.say("Nothing is running to steer; just send it as a message.")
            return
        with self.conveyor.lock:
            self.conveyor.pending.insert(0, (time.time(), arg))
        self.say("An answer can't be changed halfway here, so that goes in first, right "
                 "after the current one (Jarvis does the same when an answer can't be steered).")

    def cmd_queue(self, arg: str) -> None:
        st = self._state()
        a = arg.strip().lower()
        if not a:
            q = self.queue()
            self.say(f"Queue mode: {q['mode']}{' (set here)' if q['override'] else ' (default)'}"
                     f", debounce {q['debounce_ms']}ms, cap {q['cap']}, drop {q['drop']}.\n"
                     "Modes: steer, followup, collect, interrupt. "
                     "Options: debounce:<2s> cap:<n> drop:<summarize|old|new>. "
                     "/queue default clears it.")
            return
        if a in ("default", "reset", "clear", "inherit"):
            st.pop("queue", None)
            self._save_state(st)
            q = self.queue()
            self.say(f"Queue settings cleared; back to the default ({q['mode']}).")
            return
        toks = a.replace("=", ":").split()
        new = dict(st.get("queue") or {})
        for tok in toks:
            if tok in QUEUE_MODES:
                new["mode"] = tok
            elif tok.startswith("debounce:"):
                ms = _duration_ms(tok.split(":", 1)[1])
                if ms is None:
                    self.say(f"Invalid debounce '{tok}'. Use e.g. debounce:2s or debounce:500ms.")
                    return
                new["debounce_ms"] = ms
            elif tok.startswith("cap:"):
                v = tok.split(":", 1)[1]
                if not v.isdigit() or int(v) < 1:
                    self.say("cap must be a whole number of 1 or more.")
                    return
                new["cap"] = int(v)
            elif tok.startswith("drop:"):
                v = tok.split(":", 1)[1]
                if v not in DROP:
                    self.say("drop must be summarize, old or new.")
                    return
                new["drop"] = v
            else:
                self.say(f"Unrecognized queue mode \"{tok}\". Valid modes: steer, followup, "
                         "collect, interrupt.")
                return
        st["queue"] = new
        self._save_state(st)
        q = self.queue()
        line = f"Queue mode set to {q['mode']}."
        if q["mode"] == "steer":
            line += (" (Here an answer can't be changed halfway, so like Jarvis when a run "
                     "can't be steered, a message waits for the current answer to finish.)")
        if len(toks) > 1 or "mode" not in new:
            line += f" Debounce {q['debounce_ms']}ms, cap {q['cap']}, drop {q['drop']}."
        self.say(line)

    def _models_list(self, cfg: dict) -> str:
        return "\n".join(f"{i}. {pretty_model(m)} ({m.split('/')[-1]})"
                         for i, m in enumerate(allowed_models(cfg), 1))

    def cmd_models(self, arg: str) -> None:
        try:
            cfg = load_openclaw()
        except (OSError, ValueError) as e:
            self.say(f"I can't read the model list right now ({e}).")
            return
        self.say("Models allowed here:\n" + self._models_list(cfg)
                 + "\nPick one with /model " + ("<agent> " if len(self.ad.agents) > 1 else "")
                 + "<name or number>.")

    def model_lines(self, cfg: dict | None = None) -> list[str]:
        cfg = cfg if cfg is not None else load_openclaw()
        out = []
        for a in self.ad.agents:
            try:
                m, t = effective(cfg, a.agent_id)
            except KeyError:
                out.append(f"{a.target}: not set up")
                continue
            pinned = "" if (m == a.pinned_model and t == a.pinned_thinking) else (
                f" (normally {pretty_model(a.pinned_model)}, {a.pinned_thinking})")
            label = a.target if len(self.ad.agents) > 1 else self.ad.name
            out.append(f"{label} ({a.role}): {pretty_model(m)}, thinking {t}{pinned}"
                       + (f". {a.note}" if a.note else ""))
        return out

    def cmd_model(self, arg: str) -> None:
        self._agent_setting("model", arg)

    def cmd_think(self, arg: str) -> None:
        self._agent_setting("thinkingDefault", arg)

    def _agent_setting(self, fld: str, arg: str) -> None:
        cmdname = "/model" if fld == "model" else "/think"
        try:
            cfg = load_openclaw()
        except (OSError, ValueError) as e:
            self.say(f"I can't read the model settings right now ({e}).")
            return
        toks = arg.split()
        if not toks or toks[0].lower() in ("status", "list"):
            lines = self.model_lines(cfg)
            if fld == "model":
                lines += ["", "Allowed models:", self._models_list(cfg)]
                ex = (f"/model {self.ad.agents[-1].target} opus-5-5" if len(self.ad.agents) > 1
                      else "/model opus-5-5")
                lines.append(f"Switch with e.g. {ex} (or a number). /model "
                             + ("<agent> " if len(self.ad.agents) > 1 else "")
                             + "default puts it back.")
            else:
                lines += ["", "Levels: " + ", ".join(THINK_LEVELS) + ". /think "
                          + ("<agent> " if len(self.ad.agents) > 1 else "")
                          + "<level>, or default to put it back."]
            self.say("\n".join(lines))
            return
        agent = self._agent(toks[0]) if len(toks) > 1 or len(self.ad.agents) > 1 else None
        if agent is None:
            if len(self.ad.agents) > 1:
                names = ", ".join(a.target for a in self.ad.agents)
                if len(toks) == 1:
                    self.say(f"{self.ad.name} has {len(self.ad.agents)} parts with their own model ({names}). Say "
                             f"which: e.g. {cmdname} {self.ad.agents[-1].target} {toks[0]}")
                else:
                    self.say(f"I don't know '{toks[0]}'. Say one of: {names}.")
                return
            agent = self.ad.agents[0]
            value_word = " ".join(toks)
        else:
            value_word = " ".join(toks[1:])
        if not value_word:
            self.say("\n".join(l for l in self.model_lines(cfg) if l.startswith(agent.target))
                     or "\n".join(self.model_lines(cfg)))
            return
        old_m, old_t = effective(cfg, agent.agent_id)
        old = old_m if fld == "model" else old_t
        vw = value_word.strip().lower()
        if fld == "model":
            if vw in ("default", "reset", "clear", "inherit", "unpin"):
                new, note = agent.pinned_model, "(its normal model)"
            else:
                new, note = resolve_model(value_word, allowed_models(cfg))
                if new is None:
                    self.say(f"'{value_word}' is {note}. Allowed:\n{self._models_list(cfg)}")
                    return
            shown_new, shown_old = pretty_model(new), pretty_model(old)
        else:
            vw = THINK_ALIASES.get(vw, vw)
            if vw in THINK_CLEAR:
                new, note = agent.pinned_thinking, "(its normal level)"
            elif vw == "ultra":
                self.say("ultra is only offered for GPT-5.6 models; these agents run Claude. "
                         "Levels here: " + ", ".join(THINK_LEVELS) + ".")
                return
            elif vw not in THINK_LEVELS:
                self.say(f"Unrecognized thinking level \"{value_word}\". Valid levels: "
                         + ", ".join(THINK_LEVELS) + ".")
                return
            else:
                new, note = vw, ""
            shown_new, shown_old = new, old
        if new == old:
            self.say(f"{self._who(agent)} already uses {shown_new}.")
            return
        if self.ad.before_agent_change:
            veto = self.ad.before_agent_change(agent, fld, old, new)
            if veto:
                self.say(veto)
                return
        try:
            set_agent_field(agent.agent_id, fld, new)
        except (ChangeFailed, KeyError, OSError) as e:
            self.say(f"Not changed: {e}. {self._who(agent)} still uses {shown_old}.")
            return
        extra = None
        if self.ad.after_agent_change:
            try:
                extra = self.ad.after_agent_change(agent, fld, old, new, note)
            except Exception as e:  # noqa: BLE001 - put the setting back, then say why
                try:
                    set_agent_field(agent.agent_id, fld, old)
                    back = f"put back to {shown_old}"
                except (ChangeFailed, KeyError, OSError) as e2:
                    back = f"and putting it back failed too ({e2}); ask Claude"
                self.say(f"Not changed: recording it failed ({e}), so I {back}.")
                return
        self._record_setting(agent, fld, old, new)
        what = "model" if fld == "model" else "thinking level"
        msg = (f"{self._who(agent)} {what} set to {shown_new} {note}".rstrip()
               + f" (was {shown_old}). It applies from its next turn.")
        if extra:
            msg += "\n" + extra
        self.say(msg)

    def _who(self, agent: Agent) -> str:
        return (f"{self.ad.name}'s {agent.target}" if len(self.ad.agents) > 1 else self.ad.name)

    def _record_setting(self, agent: Agent, fld: str, old: str, new: str) -> None:
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        ent = data.setdefault(agent.agent_id, {"history": []})
        ent["model" if fld == "model" else "thinking"] = new
        ent["history"].append({"at": _stamp(), "field": fld, "old": old, "new": new})
        WRITE(self.settings_path, json.dumps(data, indent=1))

    def cmd_reasoning(self, arg: str) -> None:
        self.say(f"{self.ad.name} only ever sends you its final answer, so there's no "
                 "reasoning to show or hide (/reasoning is about showing it). To change how "
                 "hard it thinks, use /think.")

    def cmd_new(self, arg: str) -> None:
        self._fresh("new", arg)

    def cmd_reset(self, arg: str) -> None:
        self._fresh("reset", arg)

    def _fresh(self, reason: str, arg: str) -> None:
        cleared = self.ad.new_session(reason) if self.ad.new_session else ""
        for key in (self.ad.session_keys() if self.ad.session_keys else []):
            agent = key.split(":")[1] if key.startswith("agent:") else ""
            params = {"key": key, "reason": reason}
            if agent:
                params["agentId"] = agent
            RUN([openclaw_bin(), "gateway", "call", "sessions.reset", "--params",
                 json.dumps(params), "--json"], 60)
        msg = "Fresh start. " + (cleared or "Earlier messages won't be used as context.")
        a = arg.strip()
        if reason == "new" and a:
            self.say(msg)
            self.cmd_model(a if len(self.ad.agents) == 1 else f"{self.ad.agents[-1].target} {a}")
            return
        if reason == "reset" and a.lower().startswith("soft"):
            msg += " (soft: the chat log itself is kept; it always is here.)"
        self.say(msg)

    def cmd_compact(self, arg: str) -> None:
        keys = self.ad.session_keys() if self.ad.session_keys else []
        if not keys:
            self.say(f"Nothing to compact: {self.ad.context_note or 'each answer starts fresh.'}"
                     " /new clears the recent messages it uses too.")
            return
        rc, out, err = RUN([openclaw_bin(), "sessions", "compact", keys[0]], 300)
        self.say("Compacted the conversation." if rc == 0 else
                 f"Compacting didn't work: {(err or out).strip()[:200]}")

    def status_lines(self) -> list[str]:
        out = []
        try:
            out += self.model_lines()
        except (OSError, ValueError) as e:
            out.append(f"Model settings unreadable: {e}")
        q = self.queue()
        running = TURNS.running()
        out.append(f"Queue: {q['mode']}"
                   + (f", {self.conveyor.waiting()} waiting" if self.conveyor else ""))
        out += [f"Running: {t.what} ({ago(t.started)})" for t in running] or ["Running: nothing"]
        active = [r for r in list_reqs(self.ad.bot)
                  if r.get("status") in ("queued", "waiting", "building", "needs_second_ok",
                                         "asking")]
        for r in active:
            out.append(f"Change {short(r['id'])}: {STATUS_WORDS.get(r['status'])}"
                       + (f" ({r['wait_note']})" if r.get("wait_note") else ""))
        if self.ad.status_extra:
            try:
                out = list(self.ad.status_extra()) + out
            except Exception as e:  # noqa: BLE001 - status must always answer
                out.append(f"(Part of the status couldn't be read: {e})")
        return out

    def cmd_status(self, arg: str) -> None:
        self.say(f"{self.ad.name}\n" + "\n".join(self.status_lines()))

    def cmd_tasks(self, arg: str) -> None:
        lines = [f"{t.what} ({ago(t.started)})" for t in TURNS.running()]
        lines += [f"Change {short(r['id'])}: {STATUS_WORDS.get(r['status'])}"
                  for r in list_reqs(self.ad.bot)
                  if r.get("status") in ("queued", "waiting", "building")]
        self.say("Running now:\n" + "\n".join(lines) if lines else "Nothing running.")

    def cmd_whoami(self, arg: str) -> None:
        self.say(f"Your Telegram id: {RICK_ID}")

    def help_text(self) -> str:
        multi = len(self.ad.agents) > 1
        tgt = f"{self.ad.agents[-1].target} " if multi else ""
        lines = [
            "Changing me:",
            "/change <what> - ask me to change how I work, in your own words (or just say "
            "\"can you make it...\"). I read it back; nothing happens until you tap Build it.",
            "/changes - recent change requests and where they are",
            "/undo - roll back my last change",
            "",
            "Jarvis's commands (same names as in Jarvis's chat):",
            "/stop - stop the answer I'm working on",
            "/queue [steer|followup|collect|interrupt] - what a message does while I'm busy",
            f"/model [{'agent ' if multi else ''}name|number|default] - show or switch "
            f"model{' (e.g. /model ' + tgt + 'opus-5-5)' if multi else ' (e.g. /model opus-5-5)'}",
            "/models - the models allowed",
            f"/think [{'agent ' if multi else ''}level|default] - how hard it thinks "
            "(also /thinking, /t)",
            "/status - models, queue and anything running",
            "/new, /reset - fresh start (earlier messages no longer used as context)",
            "/compact, /steer <msg>, /tasks, /whoami, /commands",
        ]
        for cmd, meaning in self.ad.clashes.items():
            lines.append(f"/{cmd} here keeps my own meaning: {meaning}")
        lines.append("Not here (they don't fit a bot, or they're shared with Jarvis): "
                     "/reasoning, /verbose, /trace, /fast, /usage, /config, /restart, /bash, "
                     "/exec, /elevated, /plugins, /mcp, /debug, /send, /allowlist, /approve, "
                     "/tts, /voice, /btw, /skill, /acp, /focus, /goal and the rest. "
                     "Each one says why if you send it.")
        return "\n".join(lines)

    def cmd_help(self, arg: str) -> None:
        own = self.ad.own_help() if self.ad.own_help else ""
        self.say((own.rstrip() + "\n\n" if own else "") + self.help_text())

    def cmd_commands(self, arg: str) -> None:
        self.say(self.help_text())
