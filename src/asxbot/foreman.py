"""The Foreman, from the Trader chat's side: ONE VOICE.

Rick, 25 Sep 2026 23:50: "everytime i ask it something it says it can't do shit". That
night he told the Trader chat "No keep building I have a full reset", "Yes tell me at 99",
"Prioritise the simulator ahead of fetch", and the decider answered "I can't set that up
from here", "I can't pause anything from here" - while the Foreman read the same messages
and acted on them. Two voices, one of them saying no.

The Foreman (G:\\My Drive\\foreman, state in %USERPROFILE%\\.foreman) is Rick's orchestrator:
it runs the builds - the improvement work on all his bots, one at a time, in its queue -
watches Claude usage, limits and resets, keeps his priorities, and answers in this chat
itself as "Foreman: ...". It reads every "from Rick:" line of chat.log.

So the Trader chat:
- recognises a Foreman topic here, in code, before the decider (`topic`): builds and the
  build queue, what's being worked on, Claude usage / limits / resets, priorities of
  improvement work, the Foreman. A short follow-up within ten minutes of one ("yes tell me
  at 99") counts too (`follow_up`). It hands the message over and says nothing: the
  Foreman answers here itself;
- lets the decider hand over anything else it can't do itself: a HANDOVER line in its
  reply (`read_reply`), and Rick gets at most one short line saying the Foreman will
  answer. A reply that says "I can't ... from here" anyway is handed over the same way,
  and the "can't" is taken out.

Handing over = one UTF-8 JSON file in %USERPROFILE%\\.foreman\\inbox (FOREMAN_HOME overrides
the folder; the tests always set it), written as <name>.tmp and renamed to <name>.json so
the Foreman never reads half a file:
    {"bot": "trader", "from": "Rick", "text": <his exact message>,
     "at": <when he sent it, ISO with offset>, "why": "foreman topic" | "can't do: ..."}
The Foreman answers in this chat and acts: a command is applied, anything else becomes a
queued build that makes the system able to do it. It ignores a handover it has already
answered from the chat log, so handing over a Foreman topic is always safe.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot import botctl, plain

SYD = ZoneInfo("Australia/Sydney")
BOT = "trader"
FOLLOW_UP_FOR = timedelta(minutes=10)  # the Foreman's own window for a short follow-up
HEARTBEAT_STALE = timedelta(minutes=5)  # the Foreman writes its heartbeat every 60 s
ON_IT = "The Foreman's on it - it'll answer here."
HANDED_OFF_LIMITS = ("money", "ibkr", "secrets", "jarvis")  # botctl.OFF_LIMITS never handed


def home() -> Path:
    override = os.environ.get("FOREMAN_HOME")
    if override:
        return Path(override)
    return Path(os.environ.get("USERPROFILE") or Path.home()) / ".foreman"


def inbox() -> Path:
    return home() / "inbox"


# --------------------------------------------------------------------------- what is the Foreman's

_WORK = (r"(?:builds?|work|(?<!history )fetch|layman|simulator|practice lab|foreman|guardian|"
         r"etf agent|improvements?|features?|bots?|projects?|jobs?|queue)")  # fmt: skip
_TOPICS: list[tuple[str, re.Pattern[str]]] = [
    ("the Foreman", re.compile(r"\bforem[ae]n\b")),
    ("builds", re.compile(
        r"\bbuilds\b"
        r"|\bbuild (?:queue|order|list|line-?up|status|plan|progress|slots?)\b"
        r"|\b(?:the|this|that|which|what|next|current|last|each|every|any) build\b"
        r"(?! (?:a|an|it|me|up)\b)"
        r"|\b(?:keep|kept|stop|stopped|pause|paused|resume|resumed|start|started|restart|"
        r"continue|carry on|quit|halt|hold off|finish|finished|done|still) (?:on )?building\b"
        r"|\bbuilding (?:everything|it all|things|stuff|away|overnight|tonight)\b"
        r"|\bbeing (?:built|worked on)\b"
        r"|\bwhat(?:'s| is| are| am| will| were)? (?:you |it |they |we |claude |the bots? )?"
        r"(?:working on|building|going to build|build(?:ing)? next)\b"
        r"|\bbacklog\b|\bpractice lab\b|\bimprovement work\b|\bclaude (?:sessions?|jobs?|code)\b"
    )),
    ("usage and limits", re.compile(
        r"(?<!margin )(?<!capital )(?<!cash )\busage\b"
        r"|\b(?:weekly|session|5[- ]?hour|five[- ]hour|plan|usage|rate|claude|opus|token) "
        r"(?:limits?|caps?|resets?|allowance|quota)\b"
        r"|\b(?:limit|full|weekly|usage|session|plan) resets?\b"
        r"|\b(?:the|a|my|that|your|another|next) reset\b"
        r"|\b(?:claude|anthropic)\b[^.?!]{0,40}\b(?:limits?|plan|allowance|quota|credits?|"
        r"tokens|subscription|resets?|running out|run out|ran out)\b"
        r"|\b(?:run|runs|running|ran) out of (?:usage|tokens|credits|claude|allowance|quota)\b"
    )),
    ("keep building", re.compile(
        r"\bkeep (?:the builds?|everything|it all|them all|all of it|all of them|the work) "
        r"(?:going|running|moving)\b"
        r"|\b(?:don't|do not|never) (?:stop|pause|halt) (?:the )?(?:builds?|building|work|"
        r"everything)\b"
    )),
    ("priorities", re.compile(
        r"\b(?:de)?prioriti[sz](?:e|es|ed|ing)\b"
        rf"|\bpriorit(?:y|ies)\b[^.?!]{{0,40}}\b{_WORK}\b"
        rf"|\b{_WORK}\b[^.?!]{{0,40}}\bpriorit(?:y|ies)\b"
    )),
]  # fmt: skip
# The Trader's own change requests ("what changes are being built", "is my change built").
_TRADER_OWN = re.compile(r"\bchange requests?\b|\bchanges\b|\b(?:my|the|that|this|your) change\b")

# After a Foreman topic, a short follow-up that is an instruction for it: an alert at a
# number, a percentage, stop / carry on, how many at a time, its status.
_FOLLOW_UP = re.compile(
    r"\b(?:tell|message|alert|warn|ping|text|let|remind) me\b[^.?!]{0,40}\b(?:at|when|if|once|"
    r"before)\b"
    r"|\b\d{1,3} ?(?:%|percent\b|per ?cent\b)"
    r"|\bat \d{1,3}\b(?! ?(?:am|pm|:|\.\d|o'clock))"
    r"|^(?:(?:yes|yeah|yep|ok|okay|no|nah|and|but|so|then|right)[ ,]+)*(?:stop|pause|halt|"
    r"hold off|hold it|freeze|resume|unpause|restart|start again|go again|carry on|continue|"
    r"keep going|keep at it|keep it going|go ahead|back on)\b"
    r"|\bwon't (?:allow|let) (?:it|them) (?:to )?stop\b"
    r"|\b(?:one|two|three|1|2|3) (?:at a time|at once|at the same time)\b"
    r"|\bstatus\b|\bwhat order\b|\bwhat's next\b|\bwhat(?:'s| is) (?:running|happening)\b"
)  # fmt: skip
# ... unless it is about the Trader's own business.
_TRADERS_OWN = re.compile(
    r"\$|\b(?:order|orders|trade|trades|trading|traded|position|positions|buy|sell|sold|"
    r"bought|short|cover|stock|stocks|shares?|price|stop[- ]loss|watcher|arena|decider|"
    r"reader|announcements?|answer|answers|reply|chat|conversation|report|day ?trader|"
    r"playbook|p ?& ?l|pnl|profit|loss|model|opus|sonnet|think|thinking)\b"
)  # fmt: skip


def topic(text: str) -> str | None:
    """Which of the Foreman's topics a message is about, or None. Plain pattern matching:
    no model reads Rick's words here (tests/test_foreman_handover.py)."""
    t = plain.normalise(text)
    if not t or t.startswith("/") or _TRADER_OWN.search(t):
        return None
    for why, pat in _TOPICS:
        if pat.search(t):
            return why
    return None


def follow_up(text: str, known_codes: Collection[str] = ()) -> bool:
    """A short instruction that, straight after a Foreman topic, is for the Foreman too
    ("yes tell me at 99", "stop at 80%", "carry on"). The caller checks the time."""
    t = plain.normalise(text)
    if not t or t.startswith("/") or _TRADERS_OWN.search(t):
        return False
    if plain.tickers_in(text, known_codes) or botctl.looks_like_change(text):
        return False
    return bool(_FOLLOW_UP.search(t))


def off_limits(text: str) -> str | None:
    """A hard rule the message is about (real money, IBKR orders, credentials, Jarvis): never
    handed over - nothing will be built for it."""
    for key, pat, rule in botctl.OFF_LIMITS:
        if key in HANDED_OFF_LIMITS and pat.search(text or ""):
            return rule
    return None


# --------------------------------------------------------------------------- handing over


def hand_over(text: str, at: datetime, why: str, *, now: datetime | None = None) -> Path:
    """Write one handover into the Foreman's inbox. Returns the .json path; raises OSError."""
    now = now or datetime.now(SYD)
    at = at if at.tzinfo else at.replace(tzinfo=SYD)
    box = inbox()
    box.mkdir(parents=True, exist_ok=True)
    name = f"{now:%Y%m%d-%H%M%S}-{BOT}-{secrets.token_hex(2)}"
    body = {"bot": BOT, "from": "Rick", "text": text, "at": at.isoformat(timespec="seconds"),
            "why": why}  # fmt: skip
    tmp, final = box / f"{name}.tmp", box / f"{name}.json"
    try:
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, final)
    finally:
        if tmp.exists():
            tmp.unlink()
    return final


def last_seen(now: datetime | None = None) -> tuple[bool, datetime | None]:
    """(is the Foreman running, when it last wrote its heartbeat)."""
    now = now or datetime.now(SYD)
    try:
        hb = json.loads((home() / "heartbeat.json").read_text(encoding="utf-8"))
        at = datetime.fromisoformat(str(hb["at"]))
    except (OSError, ValueError, KeyError, TypeError):
        return False, None
    if at.tzinfo is None:
        at = at.replace(tzinfo=SYD)
    down = str(hb.get("state") or "") in ("stopped", "stopping", "exited")
    return (not down and now - at <= HEARTBEAT_STALE), at


def _ampm(t: datetime) -> str:
    t = t.astimezone(SYD)
    return f"{t.hour % 12 or 12}:{t.minute:02d}{'am' if t.hour < 12 else 'pm'}"


def late_line(last: datetime | None, now: datetime | None = None) -> str:
    """What Rick is told when the Foreman is down: honest, and still not "I can't"."""
    now = now or datetime.now(SYD)
    if last is None:
        when = "and it isn't running on this PC right now"
    elif last.astimezone(SYD).date() == now.astimezone(SYD).date():
        when = f"and it hasn't checked in since {_ampm(last)}"
    else:
        when = f"and it hasn't checked in since {last.astimezone(SYD):%a} {_ampm(last)}"
    return (f"That one's for the Foreman (it runs the builds and watches usage and "
            f"priorities), {when}, so its answer may be late. Your message is waiting in its "
            "inbox.")  # fmt: skip


# --------------------------------------------------------------------------- the decider's reply

_HANDOVER = re.compile(r"^[ \t*_`>]*HAND ?OVER[ \t*_`]*(?::[ \t*_`]*(.*))?$", re.I | re.M)
_SILENT = {"SILENT", "NO_REPLY", "NO REPLY"}
# A capability refusal: "I can't set that up from here", "I have no way to schedule",
# "I don't control what runs" - the phrases of 25 Sep and their kin. A quoted one ('never
# say "I can't"') is a mention, not a refusal.
_CANT = re.compile(
    r"(?<![\"'“])\b(?:i|we)(?: really| just| simply| also)? (?:can't|cannot|can not|have no "
    r"way (?:to|of)|don't have (?:a|any) way (?:to|of)|don't control|do not control|have no "
    r"control over|don't have access to|have no access to)\b"
    r"|(?<![\"'“])\b(?:i'm|i am|we're|we are)(?: really| just| simply)? (?:unable to|not able "
    r"to)\b"
    r"|\bnot (?:something|a thing) i can\b|\b(?:isn't|is not|not) possible (?:from|in) "
    r"(?:here|this chat)\b|\bno way for me to\b|\bout of my hands\b"
)  # fmt: skip
# ... but "I can't tell from the records" is about knowing, not doing.
_KNOWING = re.compile(
    r"\bcan(?:'t|not| not) (?:tell|say|be sure|know|see|recall|remember|find|work out|"
    r"confirm|verify|guarantee|promise|predict|rule out|explain)\b"
)  # fmt: skip
# ... and "orders aren't placed from this chat" is a hard rule, not a gap.
_RULE = re.compile(
    r"\b(?:orders?|trades?|trading|buy|sell|short|close|cover|positions?|real[- ]money|live "
    r"(?:account|broker|trading)|broker|ibkr|passwords?|credentials?|jarvis)\b"
)  # fmt: skip


@dataclass
class Reply:
    text: str | None  # what Rick is sent; None: nothing
    handover: str | None = None  # the "why" for the Foreman, when the message goes to it
    note: str = ""  # for the chat log


def _sentences(line: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", line) if s.strip()]


def _low(s: str) -> str:
    return (s or "").lower().replace("’", "'").replace("‘", "'")


def refusals(reply: str) -> list[str]:
    """The sentences of a reply that refuse something the system could be built to do."""
    out = []
    for line in (reply or "").splitlines():
        for s in _sentences(line):
            low = _low(s)
            if _CANT.search(low) and not _KNOWING.search(low) and not _RULE.search(low):
                out.append(s.strip())
    return out


def read_reply(reply: str) -> Reply:
    """The decider's answer, read for a handover. HANDOVER: <why> on a line of its own hands
    the message over (Rick gets at most one short line); SILENT sends nothing; a reply that
    says it can't do something anyway is handed over too, with those sentences taken out."""
    r = (reply or "").strip()
    if r.strip(" .").upper() in _SILENT:
        return Reply(None, note="the decider stayed silent (a message for the Foreman)")
    lines = [ln.strip() for ln in r.splitlines() if ln.strip()]
    # Only its first or last line: a HANDOVER quoted in the middle of an answer isn't one.
    m = next((m for ln in lines[:1] + lines[-1:] if (m := _HANDOVER.fullmatch(ln))), None)
    if m:
        why = " ".join((m.group(1) or "").strip(" *_`").split())[:120]
        rest = [ln for ln in lines if ln != m.string]
        line = rest[0] if rest else ""
        if not line or len(line) > 200 or _CANT.search(_low(line)):
            line = ON_IT
        return Reply(line, "can't do: " + (why or "the decider doesn't do this itself"),
                     note="the decider handed it over")  # fmt: skip
    cant = refusals(r)
    if not cant:
        return Reply(r)
    kept = []
    for line in r.splitlines():
        s = " ".join(x for x in _sentences(line) if x.strip() not in cant)
        if s.strip() or not line.strip():
            kept.append(s)
    left = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    text = f"{left}\n\n{ON_IT}" if len(left) >= 40 else ON_IT
    why = " ".join(cant[0].split())[:100]
    return Reply(text, f"can't do: the decider said \"{why}\"",
                 note=f"the decider said it couldn't ({why!r}); handed over instead")  # fmt: skip
