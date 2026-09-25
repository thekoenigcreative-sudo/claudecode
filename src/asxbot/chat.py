"""The Trader's own Telegram chat: Rick talks to the decider about the arena.

`asxbot chat` long-polls @rick_asx_trader_bot (TELEGRAM_BOT_TOKEN in .env) and answers only
Rick (the paired chat id, data/arena/telegram_chat.json). Until 24 Sep 2026 OpenClaw's
gateway polled this bot as a channel; it moved here so commands are handled in code, like
Rick's other bots. Never add it back to OpenClaw as a channel: two programs calling
getUpdates on one bot take each other's messages (Telegram answers 409 Conflict), so this
refuses to start while ~/.openclaw/openclaw.json still has channels.telegram.accounts.trader.

A message goes, in order, to:
  1. botctl (the shared module, src/asxbot/botctl.py, a verbatim copy of the master in
     C:\\Users\\Richa\\.cc-jobs\\changes): OpenClaw's slash commands (/stop /queue /model
     /think /status /new /help ...) and change requests (/change /changes /undo);
  2. the Trader's own commands: /start (= /help) and /positions;
  3. any other /command: "I don't know that one";
  3b. the Foreman's topics (asxbot.foreman, since 26 Sep: Rick, "everytime i ask it
     something it says it can't do shit"): builds and the build queue, what's being worked
     on, Claude usage / limits / resets, priorities, the Foreman - and a short follow-up
     within ten minutes of one. Handed to the Foreman's inbox in code; the Trader says
     nothing, because the Foreman answers them in this chat itself ("Foreman: ...");
  4. plain words (asxbot.plain, since 25 Sep: Rick, "i need to be able to just tell it
     things without commands"): every command above from an ordinary sentence - "how's it
     going today", "what did it trade", "stop it for today", "use opus for the decider",
     "why did it pass on NWL", "show me the positions" - worked out in code, no model. A
     question about the day's trading is answered from the arena's records
     (asxbot.arena.today), never guessed. Replies never tell Rick to type a command;
     the commands stay as shortcuts;
  5. a change request if it reads like one ("can you make it..."), otherwise a
     conversation turn with trader-decider in its own session, one at a time (/queue). A
     question about the day's trading carries the day's records with it, so the decider
     answers from them.

The decider never says "I can't" for something the system could do: it hands the message
over (a HANDOVER line in its reply) and Rick gets at most one short line saying the Foreman
will answer; a reply that says "I can't ... from here" anyway is handed over the same way.

Nothing here can place, change or approve an order: the decider is told so, and this code
has no order path at all. /model and /think are strategy changes (arena/settings_history).

26 Sep 2026, after a review of the chat (tests/test_chat_review.py):
- A model or thinking change in plain words is READ BACK with buttons, never applied at
  once; while a playbook is in its test the models are frozen, and even /model only goes
  ahead after Rick taps "Change it anyway" on a read-back that says it breaks the test.
- "No new entries today" (arena/pause.py) is a real switch, set and lifted after a read-back.
- IB Gateway and the feed, the evening report, a watcher restart and the week are answered
  from the records; a reply to a Foreman message goes to the Foreman with that message.
- A clear request (stop, status, today, P&L, feed, pause) is never swallowed as the answer
  to a change reader's question; the chat's arena never fetches prices (disk only).

Every process it starts goes through asxbot.proc (botctl's RUN hook is pointed there).
Logs go to stdout only; scripts/chat.pyw writes them to chat.log in the local logs folder.

Exit codes: 0 stopped; 2 config error; 3 Telegram token or chat id problem;
10 OpenClaw still polls this bot; 11 another Trader chat holds the lock;
12 Telegram 409 Conflict (another program is polling this bot).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

from asxbot import botctl, plain, proc
from asxbot import foreman as F
from asxbot.arena import today as T
from asxbot.arena.agents import (
    DECIDER,
    FALLBACK_EFFORT,
    FALLBACK_MODELS,
    READER,
    AgentCallFailed,
    call_agent,
    expected_model,
)
from asxbot.io import write_text_atomic
from asxbot.log import get_logger
from asxbot.telegram import MAX_LEN, _split

log = get_logger("asxbot.chat")
SYD = ZoneInfo("Australia/Sydney")

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_TELEGRAM = 3
EXIT_OPENCLAW_POLLS = 10
EXIT_ALREADY_RUNNING = 11
EXIT_CONFLICT = 12

API = "https://api.telegram.org"
POLL_TIMEOUT_S = 20
TYPING_EVERY_S = 4
STALE_AFTER = timedelta(minutes=30)  # a message older than this at start-up is not answered
LOCK_WAIT_S = 30  # a launcher restart can overlap the old instance's last seconds
BACKOFF_START_S = 5.0  # Telegram unreachable: wait this, doubling to 5 minutes
PENDING_FOR = timedelta(minutes=10)  # "For the reader or the decider?" waits this long
CONFIRM_FOR = timedelta(minutes=30)  # a read-back's buttons lapse after this
TYPED_CONFIRM_FOR = timedelta(minutes=10)  # a typed "yes" answers a read-back this long

PREFACE = """\
This is Rick, writing to you directly in the Trader's own Telegram chat. It is a
conversation, not a watcher cycle: there is no announcement and no decision packet.
- Answer him plainly, in plain text for Telegram (no markdown tables or headings), and
  briefly unless he asks for detail.
- This chat cannot place, change, close or approve any order, and nothing said here is an
  order: that is a hard rule, not a gap. If he asks for a trade, say plainly that orders
  aren't placed from this chat and say what you would look at instead. Do not end with a
  DECISION block.
- The arena is fake money. Never say an order was placed, approved or filled unless an
  arena record you have read shows the broker's order id.
- If he wants to change how the Trader works, ask him to say what he wants changed in his
  own words here (for example "can you make it ..."): it is read back to him with a Build
  it button, and nothing changes until he taps it.
- Never tell him to type a command: everything here works from plain words.

Who does what. You answer about the arena: its trades, playbooks, results and the day's
records. The Foreman is Rick's orchestrator: it runs the builds (the improvement work on
all his bots, one at a time, in its queue), watches Claude usage, limits and resets, keeps
his priorities, and answers in this chat itself as "Foreman: ...". His messages about those
reach it by code, and you don't see them.

Never tell Rick "I can't", "I cannot", "I'm unable", "I have no way to" or "not from here"
about something the system could do. If he asks for something you and this chat don't do
- a reminder or an alert, something scheduled or watched, anything about builds, usage,
priorities or another of his bots, anything the system would first need to be built to do
- hand it over: make the first line of your reply
HANDOVER: <a few words on what he wants done>
and add at most one short line for him, such as "The Foreman's on it - it'll answer here."
Code passes his message to the Foreman, which answers here and acts, or queues the build
that makes it possible. Never hand over a trade or an order, anything about real money or
the live broker, or passwords: those are hard rules, so say so plainly. Nor a change to the
playbooks, their stops, targets, limits or sizes, or the agents' models while a playbook is
in its test: they are frozen for the test, which is a rule too - say so plainly.
If his message only answers or thanks the Foreman and needs nothing from you, reply with
exactly: SILENT
"""
FOREMAN_NOTE = """\
Since your last answer Rick said these to the Foreman; code passed them on and the Foreman
answers them in this chat itself (you did not see them):
"""

FACTS_RULE = """\
The block below is what the arena's own records say about the day - accounts, orders,
fills, decisions - gathered by code just now. It is the truth about what happened as of
{at} Sydney time. Answer anything about the day's trading from it; where it is silent,
say "not on record" rather than recall or guess, and never restate a number it does not
contain. Numbers from earlier turns of this conversation are older than this block and may
be stale: where they differ, this block is right.

FACTS ON RECORD:
"""

OWN_HELP = """\
The Trader chat: talk to the decider, the agent that makes the arena's trades, in your own \
words. It never places real trades, and it can't place, change or approve any order from \
this chat. The arena is fake money. The watcher runs by itself from 7:30am into the \
evening on trading days.

Things you can just say:
- "how's it going today", "what did it trade", "how much are we up", "show me the positions"
- "why did it pass on NWL", "what happened with HLS" - answered from the day's records
- "is it running", "when does it start", "what's running", "did the evening report go out"
- "is the gateway up", "are we on live prices" - IBKR and the data feed, from the records
- "no new entries today" (or "stop it for today") pauses new entries for the rest of the \
day, both playbooks and both books, after you confirm; exits keep working. "resume trading" \
lifts it
- "use opus for the decider", "make the reader think harder", "what model is it on" - a \
model or thinking change is a strategy change: I read it back first, and while a playbook \
is in its test the models are frozen
- "stop" drops the answer I'm working on; "start over" begins a fresh conversation
- "can you make it ..." asks for a change to how I work: I read it back, and nothing is \
built until you tap Build it. "what changes have I asked for", "undo the last change"
- builds and their order, Claude usage and limits, priorities: the Foreman answers those \
here itself ("Foreman: ..."), and anything else I don't do goes to it too
Anything else is a conversation with the decider.

The same things as shortcuts, if you prefer them: /positions, /status, /stop, /model, \
/think, /new, /change, /changes, /undo, /help"""

NO_ORDERS = (
    "Nothing in this chat can place, change or close an order, and nothing I say here is "
    "one. The arena's positions are opened by the agent and the rule bots during the day "
    "and closed by their stops, targets and the pre-close sweep, all in code. If you want it "
    "to trade differently, tell me what to change in your own words and I'll read it back "
    "before anything is built."
)
# Rick's "no new entries today" (arena/pause.py, 26 Sep 2026). It replaced a note that said
# trading couldn't be stopped from here and offered a change request instead.
PAUSE_ASK = (
    "Stop NEW entries for the rest of today, for both playbooks and both books? Exits, "
    "stops and the {sweep} close keep working."
)
PAUSE_WATCHER = (
    " The watcher itself keeps running - it is never stopped in market hours or with a "
    "position open, and the exits need it."
)
WATCHER_LOCK = (
    "The watcher isn't restarted from this chat. It is locked from 7:25am to 7:25pm on a "
    "trading day, and whenever an arena account holds a position or has an order working: on "
    "25 Sep a restart at 10:52 re-ran a rule late. It starts itself at 7:30am on trading days "
    "through its scheduled task, and the watchdog tells you here if it isn't running when it "
    "should be. To stop new trades for the day, say \"no new entries today\" - exits keep "
    "working."
)
FROZEN_VETO = (
    "Not changed: {test} freezes the agents' models and thinking levels. Tell me the change "
    "in your own words and I'll read it back, with the choice to break the test anyway."
)
LAPSED = "That read-back has lapsed, so nothing was changed. Ask again and I'll read it back."
# botctl's replies that name a command, in Rick's words here (botctl.py is a verbatim copy
# of a shared master, so they are reworded on the way out; the master should change too).
PLAIN_REWRITES = [
    ("; /undo reverses it once it's live.",
     "; saying \"undo the last change\" reverses it once it's live."),
    ("If it was, send it again as /change <what to change>.",
     "If it was, say it again starting \"can you make it ...\"."),
    ("Ask with /change <what to change>.", "Ask in your own words - \"can you make it ...\"."),
    ("Please try /change again in a minute.", "Please ask again in a minute."),
]  # fmt: skip
# A message from the Foreman, or a change job's report ("Change #0926-1015 is live: ...").
FOREMAN_MSG = re.compile(r"^\s*(?:foreman\b|change #?\d{4}-\d{4}\b)", re.I)
UNKNOWN_COMMAND = (
    "I don't know that one. Just say what you want in plain words - \"how's it going "
    "today\", \"show me the positions\", \"use opus for the decider\" - or \"help\" for the "
    "list."
)
WHICH_AGENT = (
    "For the reader or the decider? (The reader reads each announcement; the decider makes "
    "the trades and answers you here. Say \"both\" for both.)"
)
QUEUE_WORDS = {
    "steer": "waits for the current answer to finish and is answered next (an answer can't "
             "be changed halfway here)",
    "followup": "waits its turn and is answered next",
    "collect": "is bundled with any others that arrive and answered as one",
    "interrupt": "stops the current answer and is answered instead",
}  # fmt: skip
AGENT_IDS = {"reader": READER, "decider": DECIDER}
# Intents that win even when the sentence also reads like a change request ("can you make
# the decider think harder" is a setting, read back on the spot, not a build; "can you make
# it stop trading today" is the pause).
PLAIN_FIRST = {"model_set", "think_set", "stop", "order_request", "pause", "resume",
               "watcher_restart"}  # fmt: skip
# Clear requests that are never taken as the answer to a change reader's open question
# (26 Sep 2026: for 20 minutes after one, "stop" and "how's it going" were swallowed).
CLEAR_INTENTS = {"stop", "status", "today", "pnl", "feed", "pause", "resume", "order_request"}
# A reply to a Foreman message with one of these is the Foreman's ("stop", "status").
FOREMAN_REPLY_INTENTS = {"stop", "status", "tasks", "resume"}


# --------------------------------------------------------------------------- hooks


def run_hidden(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    """botctl's process runner, through the windowless helper (tests/test_proc.py)."""
    try:
        p = proc.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, stdin=proc.DEVNULL,
        )  # fmt: skip
    except proc.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as e:
        return 127, "", str(e)
    return p.returncode, p.stdout or "", p.stderr or ""


def write_file(path: Path, text: str) -> None:
    write_text_atomic(text, Path(path))


# Every botctl process and file write in this package goes through the repo's own helpers.
botctl.RUN = run_hidden
botctl.WRITE = write_file


def chat_home() -> Path:
    """The chat's state folder: local disk, outside Google Drive."""
    override = os.environ.get("ASXBOT_CHAT_HOME")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "asx-bot" / "chat"


def git_commit(repo: Path) -> str:
    """'754c226 (master)', read from .git without starting git."""
    g = Path(repo) / ".git"
    try:
        head = (g / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return f"{head[:7]} (detached)"
        ref = head[4:].strip()
        branch = ref.rsplit("/", 1)[-1]
        f = g / ref
        if f.exists():
            return f"{f.read_text(encoding='utf-8').strip()[:7]} ({branch})"
        for line in (g / "packed-refs").read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return f"{line[:7]} ({branch})"
    except OSError:
        pass
    return "unknown"


# --------------------------------------------------------------------------- Telegram


class TgError(RuntimeError):
    pass


class TgConflict(TgError):
    """409: another program is calling getUpdates on this bot (or a webhook is set)."""


class TgUnauthorized(TgError):
    """401/404: the token is wrong or revoked."""


class TgRetry(TgError):
    def __init__(self, text: str, after: float):
        super().__init__(text)
        self.after = after


def keyboard(buttons: list[tuple[str, str]]) -> dict:
    """[(label, data), ...] -> an inline keyboard, two buttons a row."""
    rows = [
        [{"text": label, "callback_data": data} for label, data in buttons[i : i + 2]]
        for i in range(0, len(buttons), 2)
    ]
    return {"inline_keyboard": rows}


class Telegram:
    """The Bot API calls the chat needs. Plain text only (no parse_mode), so nothing Rick
    or the decider writes can break a message. The token never reaches a log line."""

    def __init__(self, token: str, post=None):
        self.token = token
        self.masked = f"...{token[-4:]}" if len(token) > 4 else "(set)"
        self._post = post or requests.post
        self._send_lock = threading.Lock()

    def _scrub(self, text) -> str:
        return str(text).replace(self.token, f"<token {self.masked}>")

    def call(self, method: str, payload: dict | None = None, timeout: float = 30):
        try:
            r = self._post(f"{API}/bot{self.token}/{method}", json=payload or {}, timeout=timeout)
        except requests.RequestException as e:
            raise TgError(f"{method}: network error: {self._scrub(e)}") from None
        try:
            body = r.json()
        except ValueError:
            raise TgError(f"{method}: unreadable reply (HTTP {r.status_code})") from None
        if body.get("ok"):
            return body.get("result")
        code = int(body.get("error_code") or r.status_code or 0)
        desc = self._scrub(body.get("description") or "")
        if code == 409:
            raise TgConflict(desc)
        if code in (401, 404):
            raise TgUnauthorized(f"{method} refused ({code}): {desc}")
        if code == 429:
            after = float((body.get("parameters") or {}).get("retry_after") or 5)
            raise TgRetry(f"{method}: rate limited: {desc}", after)
        raise TgError(f"{method} failed ({code}): {desc}")

    def get_updates(self, offset: int | None, timeout: int = POLL_TIMEOUT_S) -> list[dict]:
        payload = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset:
            payload["offset"] = offset
        return self.call("getUpdates", payload, timeout=timeout + 15) or []

    def send(self, chat_id: str, text: str, buttons: list[tuple[str, str]] | None = None):
        chunks = _split(text or "(nothing to say)", MAX_LEN)
        with self._send_lock:  # one message at a time, so a split reply stays in order
            for i, chunk in enumerate(chunks):
                payload = {"chat_id": chat_id, "text": chunk,
                           "link_preview_options": {"is_disabled": True}}  # fmt: skip
                if buttons and i == len(chunks) - 1:
                    payload["reply_markup"] = keyboard(buttons)
                self._retry("sendMessage", payload)

    def _retry(self, method: str, payload: dict, tries: int = 3):
        for n in range(tries):
            try:
                return self.call(method, payload)
            except (TgConflict, TgUnauthorized):
                raise
            except TgRetry as e:
                if n == tries - 1:
                    raise
                time.sleep(min(e.after, 30))
            except TgError:
                if n == tries - 1:
                    raise
                time.sleep(2 + 3 * n)
        return None

    def typing(self, chat_id: str) -> None:
        self.call("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=10)

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        self.call("answerCallbackQuery", payload, timeout=10)


class PrintTelegram:
    """`asxbot chat --probe`: prints what would be sent. Never polls, never sends."""

    masked = "(probe)"

    def __init__(self, out=None):
        self.out = out or sys.stdout
        self.sent: list[tuple[str, list | None]] = []
        self._lock = threading.Lock()

    def send(self, chat_id, text, buttons=None):
        with self._lock:
            self.sent.append((text, buttons))
            print(f"\n--- to Rick (chat {chat_id}) ---\n{text}", file=self.out)
            if buttons:
                print("buttons: " + "  ".join(f"[{lab}] -> {d}" for lab, d in buttons),
                      file=self.out)  # fmt: skip
            self.out.flush()

    def typing(self, chat_id):
        pass

    def answer_callback(self, callback_id, text=None):
        pass

    def get_updates(self, offset, timeout=POLL_TIMEOUT_S):
        raise RuntimeError("the probe never polls Telegram")


# --------------------------------------------------------------------------- guards


def openclaw_still_polls(token: str, path: Path | None = None) -> str | None:
    """Why this bot must not start polling (OpenClaw still polls it), or None."""
    path = Path(path or botctl.OPENCLAW_JSON)
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        return f"cannot read {path} to check that OpenClaw no longer polls this bot ({e})"
    tg = ((cfg.get("channels") or {}).get("telegram")) or {}
    accounts = tg.get("accounts") or {}
    how = ("so OpenClaw's gateway polls this bot too and the two would take each other's "
           "messages. Remove that account and its binding (the go-live step) first.")  # fmt: skip
    if "trader" in accounts:
        return f"{path} still has channels.telegram.accounts.trader, {how}"
    for name, acct in accounts.items():
        if isinstance(acct, dict) and acct.get("botToken") == token:
            return f"{path} account channels.telegram.accounts.{name} uses this bot's token, {how}"
    if tg.get("botToken") == token:
        return f"{path} channels.telegram uses this bot's token, {how}"
    return None


class SingleInstance:
    """One Trader chat per PC: an OS lock on a file, released when the process ends."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fh = None

    def acquire(self, wait_s: float = 0.0) -> bool:
        deadline = time.time() + wait_s
        while True:
            if self._try():
                return True
            if time.time() >= deadline:
                return False
            time.sleep(1)

    def _try(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")  # noqa: SIM115 - held open for the life of the lock
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.truncate(0)
        fh.write(str(os.getpid()).encode())
        fh.flush()
        self.fh = fh
        return True

    def release(self) -> None:
        if self.fh is None:
            return
        try:
            self.fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self.fh.close()
        self.fh = None


# --------------------------------------------------------------------------- the chat


def _money(v) -> str:
    return f"${float(v):,.0f}"


def _pct(v) -> str:
    return f"{float(v):g}%"


def _times(v) -> str:
    return f"{float(v):g}x"


def _count(v) -> str:
    return str(int(v))


# (key, label, how Rick would say it, path in config.yaml, format)
LIMITS: list[tuple[str, str, str, tuple, object]] = [
    ("risk_per_trade_pct", "risk per trade (Level 1)",
     r"risk (per|a|on each|for each) trade|risk budget|risk per position",
     ("arena", "levels", 1, "risk_per_trade_pct"), _pct),
    ("max_open_positions", "most open positions per account (Level 1)",
     r"(open|max(imum)?|more|fewer|how many) positions|positions (open )?at once",
     ("arena", "levels", 1, "max_open_positions"), _count),
    ("max_position_aud", "largest position (Level 1)",
     r"position size|size of (a|each) position|max(imum)? position|largest position|"
     r"position cap|(bigger|smaller|larger) positions",
     ("arena", "levels", 1, "max_position_aud"), _money),
    ("max_new_positions_per_day", "new positions a day per account (Level 1)",
     r"new positions|positions (a|per|each) day|(trades|entries) (a|per|each) day",
     ("arena", "levels", 1, "max_new_positions_per_day"), _count),
    ("leverage_asx", "ASX leverage (Level 1)", r"leverage|margin|gearing",
     ("arena", "levels", 1, "leverage_asx"), _times),
    ("leverage_crypto", "crypto leverage (Level 1)",
     r"crypto[^.]{0,30}leverage|leverage[^.]{0,30}crypto",
     ("arena", "levels", 1, "leverage_crypto"), _times),
    ("daily_loss_limit_pct", "daily loss limit (Level 1)",
     r"daily loss|loss limit|lose in a day|(stop|halt) trading for the day",
     ("arena", "levels", 1, "daily_loss_limit_pct"), _pct),
    ("guard_max_orders_per_day", "most orders a day per account (guard)",
     r"orders (a|per|each) day|order count|number of orders",
     ("arena", "guards", "max_orders_per_day"), _count),
    ("guard_max_order_value_aud", "largest single order (guard)",
     r"order (value|size)|largest order|max(imum)? order|single order|biggest order",
     ("arena", "guards", "max_order_value_aud"), _money),
    ("guard_min_order_aud", "smallest order (guard)",
     r"min(imum)? order|smallest order|tiny orders|small orders",
     ("arena", "guards", "min_order_aud"), _money),
    ("daytrader_risk_per_trade_pct", "the day trader's risk per trade",
     r"day ?trader[^.]{0,60}risk|risk[^.]{0,60}day ?trader",
     ("arena", "playbooks", "asx_daytrader", "risk_per_trade_pct"), _pct),
    ("real_max_position_aud", "real-money largest position", r"real[- ]money[^.]{0,40}position",
     ("limits", "max_position_aud"), _money),
    ("real_min_order_aud", "real-money smallest order", r"real[- ]money[^.]{0,40}order",
     ("limits", "min_order_aud"), _money),
    ("real_max_open_positions", "real-money most open positions",
     r"real[- ]money[^.]{0,40}open positions", ("limits", "max_open_positions"), _count),
    ("real_max_new_positions_per_day", "real-money new positions a day",
     r"real[- ]money[^.]{0,40}(new positions|per day|a day)",
     ("limits", "max_new_positions_per_day"), _count),
]  # fmt: skip


def dig(raw: dict, path: tuple):
    node = raw
    for part in path:
        if not isinstance(node, dict) or part not in node:
            raise KeyError(".".join(str(p) for p in path))
        node = node[part]
    return node


def chat_message(text: str, facts: str = "", foreman: str = "",
                 at: datetime | None = None) -> str:  # fmt: skip
    body = PREFACE
    if foreman:
        body += "\n" + FOREMAN_NOTE + foreman + "\n"
    if facts:
        stamp = f"{(at or datetime.now(SYD)).astimezone(SYD):%H:%M on %a %d %b}"
        body += "\n" + FACTS_RULE.format(at=stamp) + facts + "\n"
    return body + "\nRick's message:\n" + text


def _ampm(t) -> str:
    return f"{t.hour % 12 or 12}:{t.minute:02d}{'am' if t.hour < 12 else 'pm'}"


def _when(at: datetime | None, now: datetime) -> str:
    """'19:22 Fri, 7 h ago' - when a record was written, and how old it is."""
    if at is None:
        return "time not recorded"
    at = at.astimezone(SYD)
    s = max(0.0, (now - at).total_seconds())
    ago = ("just now" if s < 90 else f"{s / 60:.0f} min ago" if s < 5400
           else f"{s / 3600:.0f} h ago" if s < 172800 else f"{s / 86400:.0f} days ago")  # fmt: skip
    when = f"{at:%H:%M}" if at.date() == now.astimezone(SYD).date() else f"{at:%H:%M %a}"
    return f"{when}, {ago}"


def _stamp(value) -> datetime | None:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=SYD)


def _read_json(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def frozen_words(fz: dict) -> str:
    """'the 10-day test of ASX announcements v2 and ASX day trader (Fri 25 Sep to Thu 08
    Oct)'."""
    span = ""
    if fz.get("start") and fz.get("end"):
        span = f" ({fz['start']:%a %d %b} to {fz['end']:%a %d %b})"
    elif fz.get("end"):
        span = f" (to {fz['end']:%a %d %b})"
    return f"the {fz.get('days') or 10}-day test of {' and '.join(fz['titles'])}{span}"


def data_line(data_dir: Path, now: datetime) -> str:
    """'Data: live data (IBKR) (as of 11:02, just now)' - the watcher's feed status."""
    from asxbot.ibkr.feed import read_status

    st = read_status(data_dir)
    if not st:
        return "Data: no feed status on record yet (the watcher writes it while it runs)"
    line = (f"Data: {st.get('label') or st.get('provider_in_use') or '?'} (as of "
            f"{_when(_stamp(st.get('at')), now)})")  # fmt: skip
    if st.get("entries") == "paused":
        line += f"; new entries paused by the feed - {st.get('paused_why') or 'reason not given'}"
    return line


def feed_lines(data_dir: Path, now: datetime, doctor_file: Path | None,
               supervisor_file: Path | None) -> list[str]:  # fmt: skip
    """IB Gateway and the prices in plain words, from the records only: the watcher's feed
    status (live_data.json), the connection doctor (doctor.json) and the Gateway supervisor
    (supervisor.json), each with its age. Nothing is probed from here."""
    from asxbot.arena.today import pause_line
    from asxbot.ibkr.doctor import WORDS
    from asxbot.ibkr.feed import read_status

    out = ["IBKR and the prices, from the records:"]
    st = read_status(data_dir)
    if not st:
        out.append("- The watcher's feed: nothing on record yet (it writes its status every "
                   "cycle while it runs).")  # fmt: skip
    else:
        at = _stamp(st.get("at"))
        old = at is None or now - at > timedelta(minutes=10)
        src = "IBKR" if st.get("provider_in_use") == "ibkr" else "Yahoo (delayed)"
        out.append(f"- The watcher's feed, as of {_when(at, now)}: prices from {src} - "
                   f"{st.get('label') or '?'}."
                   + (" The watcher isn't writing it now, so that is its last word."
                      if old else ""))  # fmt: skip
        gw = st.get("gateway") or {}
        if gw:
            bits = ["connected to IB Gateway" if gw.get("connected")
                    else "NOT connected to IB Gateway"]  # fmt: skip
            if gw.get("connected"):
                bits.append("Gateway's link to IBKR is up" if gw.get("server_ok")
                            else "but Gateway is cut off from IBKR's servers")  # fmt: skip
            if gw.get("market_data"):
                bits.append(f"{gw['market_data']} prices")
            if gw.get("last_ok_at"):
                bits.append(f"last good answer {_when(_stamp(gw['last_ok_at']), now)}")
            if gw.get("heartbeat_age_s") is not None:
                bits.append(f"heartbeat {float(gw['heartbeat_age_s']):.0f}s old")
            out.append("- Connection: " + ", ".join(bits) + ".")
        if st.get("entries") == "paused":
            since = _stamp(st.get("paused_since"))
            out.append("- New entries: PAUSED by the feed"
                       + (f" since {since:%H:%M}" if since else "")
                       + f" - {st.get('paused_why') or 'reason not given'}. Stops and exits "
                       "keep working.")  # fmt: skip
        elif st.get("entries") == "allowed":
            out.append("- New entries: allowed by the feed.")
    doc = _read_json(doctor_file)
    last = doc.get("last") or {}
    if last:
        ev = "; ".join(str(x) for x in (last.get("evidence") or [])[:2])
        out.append(f"- Connection doctor, last check {_when(_stamp(last.get('at')), now)}: "
                   f"{WORDS.get(last.get('cause'), last.get('cause'))}"
                   + (f" ({ev})" if ev and last.get("cause") != "healthy" else "") + ".")
    ep = doc.get("episode")
    if ep:
        tried = [a.get("action") for a in ep.get("actions") or [] if a.get("action") != "wait"]
        out.append(f"- A problem is open since {_when(_stamp(ep.get('since')), now)}: "
                   + ", ".join(WORDS.get(c, c) for c in ep.get("causes") or [ep.get("cause")])
                   + (f"; tried so far: {', '.join(dict.fromkeys(tried))}" if tried else "")
                   + ".")  # fmt: skip
    hist = doc.get("history") or []
    if hist:
        h = hist[-1]
        since, until = _stamp(h.get("since")), _stamp(h.get("until"))
        if since and until:
            on = "" if since.date() == now.date() else f" on {since:%a %d %b}"
            span = f"{since:%H:%M} to {until:%H:%M}{on}"
        else:
            span = "time not recorded"
        out.append(f"- The last problem it closed: {span}, "
                   + ", ".join(WORDS.get(c, c) for c in h.get("causes") or [])
                   + f" - {h.get('fixed_by') or 'fixed'}.")  # fmt: skip
    if not doc:
        out.append("- Connection doctor: nothing on record (it runs inside the watcher).")
    sup = _read_json(supervisor_file)
    if sup:
        result = str(sup.get("last_result") or "")
        head, _, detail = result.partition("|")
        out.append(f"- IB Gateway, the supervisor's check at "
                   f"{_when(_stamp(sup.get('last_check')), now)}: "
                   + (detail.strip() or head.strip() or "no result recorded") + ".")
        obs = sup.get("observation") or {}
        low = result.lower()
        hand = obs.get("foreign_gateway") if obs else ("hand-started" in low or None)
        launcher = obs.get("launcher_alive") if obs else ("launcher alive" in low)
        stored = obs.get("login_stored") if obs else (
            False if "no login stored" in low else True if "login stored" in low else None)
        if launcher:
            out.append("- Gateway was started by its launcher task (the supervisor looks "
                       "after it).")  # fmt: skip
        elif hand:
            out.append("- Gateway was started by hand, not by its launcher task.")
        if stored is True:
            out.append("- Your IBKR login is stored, so a restart logs in by itself (it may "
                       "still need the phone approval).")  # fmt: skip
        elif stored is False:
            out.append("- Your IBKR login is NOT stored: a restart would wait at the login "
                       "for you.")  # fmt: skip
        outage = sup.get("outage")
        if isinstance(outage, dict) and outage:
            out.append(f"- The supervisor has an outage open since "
                       f"{_when(_stamp(outage.get('since')), now)}.")  # fmt: skip
    else:
        out.append("- IB Gateway supervisor: nothing on record.")
    mine = pause_line(data_dir, now.date())
    if mine:
        out.append("- " + mine)
    return out


class TraderChat:
    """One Telegram chat with Rick. `tg` sends (Telegram or PrintTelegram); `agent_runner
    (message, session_key) -> text` is the decider turn (call_agent, unless a test or
    --no-agent replaces it)."""

    def __init__(self, tg, chat_id: str, repo: Path, home: Path, *, cfg_loader=None,
                 agent_runner=None, handover=None):  # fmt: skip
        self.tg = tg
        # (text, at, why) -> the file written: the Foreman's inbox, unless a probe prints it.
        self.handover = handover or F.hand_over
        self.chat_id = str(chat_id)
        self.repo = Path(repo)
        self.config_path = self.repo / "config.yaml"
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.state_path = self.home / "chat-state.json"
        self._state_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._threads_lock = threading.Lock()
        self.cfg_loader = cfg_loader or self._load_cfg
        self.agent_runner = agent_runner or self._call_decider
        self.clock = lambda: datetime.now(SYD)  # a test sets the day
        self._pending: dict | None = None  # "For the reader or the decider?" awaiting Rick
        self._foreman_at: datetime | None = None  # the last Foreman topic Rick raised here
        self._handed: list[tuple[datetime, str]] = []  # Foreman topics the decider hasn't seen
        self._said_at: dict[str, datetime] = {}  # when each message for the decider was sent
        # Read-backs waiting for Rick's tap (a pause, a model change): id -> what it does.
        self._confirms: dict[str, dict] = {}
        self._confirm_last: str | None = None  # the newest, for a typed "yes" / "no"
        self._allowed: tuple | None = None  # the one setting change Rick just confirmed
        self._how: dict | None = None  # how he asked for it, for config.yaml's history
        self._rec_codes: tuple | None = None  # (day, when read, the day's record codes)
        self.doctor_file: Path | None = None  # tests point these at their own files
        self.supervisor_file: Path | None = None
        self.adapter = self._adapter()
        self.ctl = botctl.BotCtl(self.adapter)
        self.ctl.conveyor = botctl.Conveyor(self.ctl, self._start_turn)
        self.ctl.set_handoff(self.to_conveyor)

    # ---------------------------------------------------------------- config and state
    def _load_cfg(self):
        from asxbot.config import load_config

        return load_config(self.config_path)

    def raw(self) -> dict:
        """config.yaml as it is now (re-read every time: a strategy change may just have
        been committed)."""
        return yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}

    def state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_state(self, **changes) -> dict:
        with self._state_lock:
            st = {**{"gen": 1}, **self.state(), **changes}
            write_text_atomic(json.dumps(st, indent=1), self.state_path)
            return st

    def session_key(self) -> str:
        return f"agent:{DECIDER}:rick-chat-{int(self.state().get('gen') or 1)}"

    # ---------------------------------------------------------------- the adapter
    def _agents(self) -> list[botctl.Agent]:
        ag = ((self.raw().get("arena") or {}).get("agents")) or {}
        models, effort = ag.get("models") or {}, ag.get("effort") or {}

        def pin(role: str) -> tuple[str, str]:
            return (str(models.get(role) or FALLBACK_MODELS[role]),
                    str(effort.get(role) or FALLBACK_EFFORT[role]))  # fmt: skip

        return [
            botctl.Agent("reader", READER, "reads each announcement", *pin("reader"),
                         aliases=("read",)),
            botctl.Agent("decider", DECIDER, "decides the trades, and answers you here",
                         *pin("decider"), aliases=("decide",)),
        ]  # fmt: skip

    def refresh_pins(self) -> None:
        """What 'normal' means for /model and /status is what config.yaml expects now."""
        try:
            self.adapter.agents[:] = self._agents()
        except (OSError, yaml.YAMLError) as e:
            log.warning("could not re-read the expected models from config.yaml: %s", e)

    def _limits(self) -> list[botctl.Limit]:
        def reader(path, fmt):
            return lambda: fmt(dig(self.raw(), path))

        return [botctl.Limit(k, label, words, reader(path, fmt))
                for k, label, words, path, fmt in LIMITS]  # fmt: skip

    def _adapter(self) -> botctl.Adapter:
        return botctl.Adapter(
            bot="trader",
            name="the Trader",
            repo=str(self.repo),
            about=(
                "Rick's fake-money ASX trading arena. Two OpenClaw agents trade simulated "
                "$20,000 accounts: trader-reader reads each company announcement and "
                "trader-decider decides whether and how to trade it, within hard limits "
                "enforced in plain code, each against a rule-based yardstick bot, one "
                "playbook (tactic) at a time in PLAN.md's order. A watcher runs itself "
                "7:30am into the evening on ASX trading days; Rick gets instant alerts, an hourly "
                "digest and an evening report on Telegram. It never places real-money orders."
            ),
            abilities=(
                "In this chat: /positions shows the open fake-money positions per arena "
                "account; /status says whether the watcher is running, the open positions, "
                "today's orders and the agents' models; a plain question goes to the "
                "decider, who can explain its trades, the playbooks and the day's results. "
                "/model and /think change the reader's or decider's model or effort, "
                "recorded as a dated strategy change in config.yaml. The evening report "
                "arrives by itself after the close."
            ),
            agents=self._agents(),
            send=self.send,
            home=self.home,
            limits=self._limits(),
            run_bg=self.run_bg,
            typing=self._typing_once,
            new_session=self.new_session,
            session_keys=lambda: [self.session_key()],
            before_agent_change=self.before_agent_change,
            after_agent_change=self.after_agent_change,
            context_note=(
                "the decider keeps this chat as one conversation on the gateway, "
                "until /new starts a fresh one"
            ),
            own_help=lambda: OWN_HELP,
            status_extra=self.status_lines,
        )

    # ---------------------------------------------------------------- sending
    def send(self, text: str, buttons: list[tuple[str, str]] | None = None) -> None:
        for old, new in PLAIN_REWRITES:  # replies never tell Rick to type a command
            text = text.replace(old, new) if text else text
        first = (text or "").splitlines()[0][:100] if text else ""
        log.info("to Rick: %s%s", first, f" [+{len(buttons)} buttons]" if buttons else "")
        try:
            self.tg.send(self.chat_id, text, buttons)
        except TgError as e:
            log.error("could not send to Rick: %s", e)

    def _typing_once(self) -> None:
        try:
            self.tg.typing(self.chat_id)
        except TgError:
            pass

    def _keep_typing(self, done: threading.Event) -> None:
        while not done.is_set():
            self._typing_once()
            done.wait(TYPING_EVERY_S)

    def spawn(self, target, *args) -> threading.Thread:
        t = threading.Thread(target=target, args=args, daemon=True)
        with self._threads_lock:
            self._threads = [x for x in self._threads if x.is_alive()] + [t]
        t.start()
        return t

    def run_bg(self, work, done) -> None:
        def runner():
            try:
                res = work()
            except Exception as e:  # noqa: BLE001 - botctl reports an exception result
                res = e
            try:
                done(res)
            except Exception:  # noqa: BLE001
                log.exception("a background step failed")

        self.spawn(runner)

    def busy(self) -> bool:
        with self._threads_lock:
            alive = any(t.is_alive() for t in self._threads)
        conv = self.ctl.conveyor
        return alive or bool(conv and (conv.busy or conv.waiting()))

    # ---------------------------------------------------------------- incoming
    def on_update(self, u: dict, now: datetime | None = None) -> str:
        """Handle one Telegram update. Returns what happened (for the log and tests)."""
        try:
            if "callback_query" in u:
                return self.on_callback(u["callback_query"])
            msg = u.get("message")
            if not msg:
                return "ignored: not a message"
            chat = str((msg.get("chat") or {}).get("id"))
            who = msg.get("from") or {}
            if chat != self.chat_id or str(who.get("id")) != self.chat_id:
                log.warning("ignored a message from chat %s (user %s @%s): only Rick is "
                            "answered", chat, who.get("id"), who.get("username"))  # fmt: skip
                return "ignored: not Rick"
            sent = datetime.fromtimestamp(int(msg.get("date") or 0), SYD)
            if (now or datetime.now(SYD)) - sent > STALE_AFTER:
                log.info("skipped a message from %s (sent while the chat was not running)",
                         f"{sent:%d %b %H:%M}")  # fmt: skip
                return f"stale: {sent:%H:%M}"
            text = msg.get("text")
            if text is None:
                self.send("I can only read text messages here.")
                return "not text"
            reply = msg.get("reply_to_message") or {}
            replied = None
            if reply and (reply.get("from") or {}).get("is_bot"):
                replied = str(reply.get("text") or reply.get("caption") or "") or None
            self.on_text(text.strip(), sent, reply_to=replied)
            return "handled"
        except Exception as e:  # noqa: BLE001 - one bad message must never stop the chat
            log.exception("handling an update failed")
            self.send(f"Something went wrong handling that ({type(e).__name__}: {e}). "
                      "It's in the chat log.")  # fmt: skip
            return "error"

    def on_callback(self, cq: dict) -> str:
        who = str((cq.get("from") or {}).get("id"))
        chat = str((((cq.get("message") or {}).get("chat")) or {}).get("id", who))
        if who != self.chat_id or chat != self.chat_id:
            log.warning("ignored a button tap from user %s in chat %s: only Rick", who, chat)
            return "ignored: not Rick"
        try:
            self.tg.answer_callback(cq.get("id", ""))
        except TgError as e:
            log.warning("answerCallbackQuery failed: %s", e)
        data = str(cq.get("data") or "")
        log.info("Rick tapped %s", data)
        if data.startswith("trd:"):  # the chat's own read-backs (a pause, a model change)
            self.on_own_button(data)
            return "button"
        if not self.ctl.on_button(data, who):
            log.info("a button this chat doesn't know: %s", data)
            return "unknown button"
        return "button"

    def on_text(self, text: str, at: datetime | None = None,
                reply_to: str | None = None) -> None:  # fmt: skip
        # The whole message on one line: the Foreman reads these lines, and matches a handover
        # to the line it has already answered by the text.
        log.info("from Rick: %s", " ".join(text.split()))
        if reply_to:
            log.info("Rick's message replies to: %s", " ".join(reply_to.split())[:300])
        at = at or self.clock()
        self.refresh_pins()
        parsed = botctl.parse_command(text)
        if parsed and parsed[0] in ("model", "think") and self.slash_setting(*parsed):
            return  # a test freezes the models: read back first
        if self.ctl.handle(text, self.chat_id):
            return
        if parsed or text.startswith("/"):
            cmd = parsed[0] if parsed else ""
            if cmd == "start":
                self.ctl.cmd_help("")
            elif cmd == "positions":
                self.cmd_positions()
            else:
                self.send(UNKNOWN_COMMAND)
            return
        if reply_to and self.reply_to_foreman(text, at, reply_to):
            return
        if self.answer_confirm(text):
            return  # "yes" / "no" to a read-back just sent
        intent = self.understand(text)
        clear = intent is not None and intent.name in CLEAR_INTENTS
        if not clear and self.ctl.answer_pending(text):
            return  # the answer to the change reader's one question
        if self.for_foreman(text, at):
            return
        if self.plain_intent(text, intent):
            return
        if self.ctl.maybe_change(text):
            return
        self._said_at = {**dict(list(self._said_at.items())[-20:]), text: at}
        self.to_conveyor(text)

    def to_conveyor(self, text: str) -> None:
        line = self.ctl.conveyor.submit(text)
        if line:
            self.send(line)

    # ---------------------------------------------------------------- the Foreman (one voice)
    def for_foreman(self, text: str, at: datetime) -> bool:
        """A Foreman topic - builds, usage and limits, priorities, the Foreman - or a short
        follow-up to one within ten minutes: handed over, and the Trader says nothing. The
        Foreman answers it in this chat itself. True if it was taken."""
        why = F.topic(text)
        if why is None and self._foreman_at is not None \
                and timedelta(0) <= at - self._foreman_at <= F.FOLLOW_UP_FOR:  # fmt: skip
            codes = self.known_codes()
            intent = plain.understand(text, codes)
            stop_answer = intent is not None and intent.name == "stop" and self.busy()
            if not stop_answer and F.follow_up(text, codes):
                why = "a follow-up"
        if why is None:
            return False
        self._foreman_at = at
        self._handed = [*self._handed[-4:], (at, text)]
        log.info("for the Foreman (%s): handing it over, the Trader stays silent", why)
        self.hand_over(text, at, "foreman topic", say=None)
        return True

    def reply_to_foreman(self, text: str, at: datetime, replied: str) -> bool:
        """Rick's reply to a Foreman message (or a change job's report): "yes", "ok do it",
        "go ahead" mean nothing to the decider, so they go to the Foreman with the message
        they answer (26 Sep 2026). A reply about trading is still the Trader's. True if
        it was handed over."""
        if not FOREMAN_MSG.match(replied or "") or F.off_limits(text):
            return False
        codes = self.known_codes()
        intent = plain.understand(text, codes, self.record_codes())
        if intent is not None and intent.name not in FOREMAN_REPLY_INTENTS:
            return False
        if F.traders_own(text) or plain.tickers_in(text, codes, self.record_codes()):
            return False
        self._foreman_at = at
        self._handed = [*self._handed[-4:], (at, text)]
        log.info("a reply to a Foreman message: handing it over with that message")
        self.hand_over(text, at, "reply to a Foreman message", say=None, reply_to=replied)
        return True

    def hand_over(self, text: str, at: datetime, why: str, say: str | None,
                  reply_to: str | None = None) -> bool:  # fmt: skip
        """Put Rick's message in the Foreman's inbox, then send `say` (None: nothing). If
        the Foreman isn't running, Rick is told plainly instead. True if it was written."""
        try:
            path = Path(self.handover(text, at, why, reply_to=reply_to) if reply_to
                        else self.handover(text, at, why))  # fmt: skip
        except OSError as e:
            log.error("could not write the handover to the Foreman's inbox %s: %s", F.inbox(), e)
            self.send("That one's for the Foreman, but passing it on failed just now "
                      f"({type(e).__name__}); it also reads this chat, so it may still pick "
                      "it up.")  # fmt: skip
            return False
        log.info("handed to the Foreman as %s (%s)", path.name, why)
        alive, last = F.last_seen()
        if not alive:
            log.warning("the Foreman's heartbeat is stale (last %s)", last)
            late = F.late_line(last)
            say = say.replace(F.ON_IT, late) if say and F.ON_IT in say else late
        if say:
            self.send(say)
        return True

    def foreman_note(self) -> str:
        """Rick's Foreman topics since the decider's last turn, so its conversation still
        makes sense (it never saw them)."""
        handed, self._handed = self._handed, []
        return "".join(f"- {_ampm(t.astimezone(SYD))} \"{s}\"\n" for t, s in handed)

    # ---------------------------------------------------------------- plain language
    def known_codes(self) -> set[str]:
        try:
            return T.known_codes(self.cfg_loader().data_dir)
        except Exception as e:  # noqa: BLE001 - a missing list only costs ticker recognition
            log.warning("could not read the ASX code lists: %s", e)
            return set()

    def record_codes(self) -> set[str]:
        """The codes the day's records name (or the last session's, on a day with none), so
        a lower-case "nwl" counts as a stock when the day is about it. Re-read each minute."""
        day = self.clock().date()
        hit = self._rec_codes
        if hit and hit[0] == day and time.time() - hit[1] < 60:
            return hit[2]
        try:
            data_dir = self.cfg_loader().data_dir
            codes = T.record_codes(data_dir, day) or T.record_codes(
                data_dir, T.pick_day(data_dir, day)[0])  # fmt: skip
        except Exception as e:  # noqa: BLE001 - only costs lower-case code recognition
            log.warning("could not read the day's record codes: %s", e)
            codes = set()
        self._rec_codes = (day, time.time(), codes)
        return codes

    def understand(self, text: str) -> plain.Intent | None:
        """Rick's message as an intent - or, if the chat has just asked "for the reader or
        the decider?", the answer to that."""
        pending, self._pending = self._pending, None
        if pending and self.clock() - pending["asked"] <= PENDING_FOR:
            agent = plain.answer_agent(text)
            if agent:
                return plain.Intent(pending["name"], {**pending["args"], "agent": agent})
        return plain.understand(text, self.known_codes(), self.record_codes())

    def plain_intent(self, text: str, intent: plain.Intent | None) -> bool:
        """A plain sentence that means one of the chat's own commands, done in code. True
        if it was taken."""
        if intent is None:
            return False
        if intent.name not in PLAIN_FIRST and botctl.looks_like_change(text):
            return False  # "can you make it show positions first" is a change request
        log.info("plain: %s %s", intent.name, intent.args or "")
        try:
            getattr(self, "do_" + intent.name)(intent, text)
        except Exception as e:  # noqa: BLE001 - one bad answer must never stop the chat
            log.exception("plain-language %s failed", intent.name)
            self.send(f"I couldn't do that ({type(e).__name__}: {e}). Nothing was changed.")
        return True

    def _day(self, text: str) -> tuple[date, str]:
        """The day a question is about: one Rick named, else today, else the last session
        on record (a Saturday's 'how did it go' is Friday's answer, and says so)."""
        now = self.clock()
        named = T.named_day(text, now.date())
        if named:
            return named, ""
        return T.pick_day(self.cfg_loader().data_dir, now.date())

    @staticmethod
    def _trading_day(d: date) -> bool:
        from asxbot.announcements.live import is_trading_day

        try:
            return d.weekday() < 5 and bool(is_trading_day(d))
        except Exception:  # noqa: BLE001 - no calendar: a weekday is taken as a trading day
            return d.weekday() < 5

    def _lead(self, text: str, label: str) -> list[str]:
        """On a trading day with nothing recorded yet, today's watcher comes first - before
        any earlier session (26 Sep 2026: a day with the watcher down was reported as the
        previous session, with no word about the watcher)."""
        now = self.clock()
        if label == "today" or T.named_day(text, now.date()) or not self._trading_day(now.date()):
            return []
        return [f"Nothing recorded today ({now:%a %d %b}) - {self._watcher()}", ""]

    def _watcher(self) -> str:
        try:
            return watcher_line(self.cfg_loader(), self.clock())
        except Exception as e:  # noqa: BLE001
            return f"Watcher: couldn't tell ({type(e).__name__}: {e})"

    def _ask_agent(self, name: str, args: dict) -> None:
        self._pending = {"name": name, "args": dict(args), "asked": self.clock()}
        self.send(WHICH_AGENT)

    def _running(self) -> bool:
        conv = self.ctl.conveyor
        return bool(botctl.TURNS.running() or (conv and conv.waiting()))

    def do_order_request(self, intent, text: str) -> None:
        self.send(NO_ORDERS)

    def do_stop(self, intent, text: str) -> None:
        self.send(self.ctl.stop_text())

    # -- Rick's "no new entries today" (arena/pause.py) --------------------------------
    def _sweep(self) -> str:
        try:
            return str(self.cfg_loader().get("arena.preclose.sweep_time") or "15:50")
        except Exception:  # noqa: BLE001
            return "15:50"

    def do_pause(self, intent, text: str) -> None:
        """A read-back with buttons; the switch is set only on Rick's tap (or "yes")."""
        from asxbot.arena import pause as P

        cfg = self.cfg_loader()
        now = self.clock()
        day = now.date()
        lead = ""
        if intent.args.get("stop_answer") and self._running():
            lead = self.ctl.stop_text() + "\n"  # "stop everything" stops the answer too
        if P.read_pause(cfg.data_dir, day):
            self.send(lead + T.pause_line(cfg.data_dir, day) + " It clears itself tomorrow.")
            return
        if not self._trading_day(day):
            self.send(lead + f"{day:%a %d %b} isn't an ASX trading day, so nothing enters today "
                      "anyway. A pause only ever covers the day it is set on.")  # fmt: skip
            return
        close = dtime.fromisoformat(str(cfg.get("arena.preclose.close_deadline") or "16:10"))
        if now.time() >= close:
            self.send(lead + f"The market has closed for today ({_ampm(close)}), so nothing more "
                      "enters today anyway. A pause only covers the day it is set on, and "
                      "tomorrow starts clear.")  # fmt: skip
            return
        ask = PAUSE_ASK.format(sweep=self._sweep())
        if intent.args.get("watcher"):
            ask += PAUSE_WATCHER
        cid = self._new_confirm({"kind": "pause", "words": text})
        self.send(lead + ask, [("Pause new entries", f"trd:ok:{cid}"),
                               ("Cancel", f"trd:no:{cid}")])  # fmt: skip

    def do_resume(self, intent, text: str) -> None:
        from asxbot.arena import pause as P

        cfg = self.cfg_loader()
        body = P.read_pause(cfg.data_dir, self.clock().date())
        if not body:
            self.send("New entries aren't paused today, so there's nothing to lift.")
            return
        cid = self._new_confirm({"kind": "resume"})
        at = str(body.get("at") or "")[11:16] or "?"
        self.send(f"Allow new entries again for the rest of today? They have been paused since "
                  f"{at} ({body.get('by') or 'Rick'}).",
                  [("Allow entries again", f"trd:ok:{cid}"), ("Cancel", f"trd:no:{cid}")])

    def _pause_now(self, c: dict) -> None:
        from asxbot.arena import pause as P
        from asxbot.log import EventLog

        cfg = self.cfg_loader()
        now = self.clock()
        P.set_pause(cfg.data_dir, now, "Rick", c.get("words") or "", EventLog(cfg.data_dir))
        ok, _ = P.paused(cfg.data_dir, now)  # the file itself, not our account of it
        if not ok:
            self.send("The pause didn't take - its file isn't there - so new entries are NOT "
                      "paused. It's in the chat log.")  # fmt: skip
            log.error("the pause file for %s was not found after writing it", now.date())
            return
        log.info("new entries paused for %s by Rick (%s)", now.date(), c.get("words"))
        self.send(f"Done: new entries are paused for the rest of today, from {now:%H:%M}, for "
                  "both playbooks and both books. The watcher reads this before every new "
                  f"entry. Exits, stops and the {self._sweep()} close keep working. It clears "
                  "itself tomorrow; say \"resume trading\" to lift it sooner.")  # fmt: skip

    def _resume_now(self, c: dict) -> None:
        from asxbot.arena import pause as P
        from asxbot.log import EventLog

        cfg = self.cfg_loader()
        now = self.clock()
        body = P.read_pause(cfg.data_dir, now.date())
        P.clear_pause(cfg.data_dir, now, "Rick", EventLog(cfg.data_dir))
        if P.paused(cfg.data_dir, now)[0]:
            self.send("The pause is still there - lifting it didn't take - so new entries "
                      "are still paused. It's in the chat log.")  # fmt: skip
            log.error("the pause file for %s is still there after clearing it", now.date())
            return
        at = str(body.get("at") or "")[11:16]
        self.send("Done: new entries are allowed again for the rest of today"
                  + (f" (the pause from {at} is lifted)." if at else "."))

    # -- read-backs: a tap or a typed yes / no -------------------------------------------
    def _new_confirm(self, c: dict) -> str:
        cid = secrets.token_hex(4)
        self._confirms = {k: v for k, v in self._confirms.items()
                          if self.clock() - v["asked"] <= CONFIRM_FOR}  # fmt: skip
        self._confirms[cid] = {**c, "asked": self.clock()}
        self._confirm_last = cid
        return cid

    def on_own_button(self, data: str) -> None:
        _, act, cid = (data.split(":", 2) + ["", ""])[:3]
        c = self._confirms.pop(cid, None)
        if self._confirm_last == cid:
            self._confirm_last = None
        if c is None or self.clock() - c["asked"] > CONFIRM_FOR:
            self.send(LAPSED)
            return
        if act != "ok":
            self.send({"pause": "OK - nothing was paused; entries carry on as normal.",
                       "resume": "OK - new entries stay paused for today.",
                       }.get(c["kind"], "Cancelled. Nothing was changed."))  # fmt: skip
            return
        try:
            {"pause": self._pause_now, "resume": self._resume_now,
             "setting": self._apply_setting}[c["kind"]](c)  # fmt: skip
        except Exception as e:  # noqa: BLE001 - one bad tap must never stop the chat
            log.exception("a confirmed %s failed", c["kind"])
            self.send(f"That didn't go through ({type(e).__name__}: {e}). It's in the chat log.")

    def answer_confirm(self, text: str) -> bool:
        """A typed "yes" or "no" straight after a read-back answers it. Breaking a frozen
        test needs the button, or the words "change it anyway". True if it was taken."""
        cid = self._confirm_last
        c = self._confirms.get(cid) if cid else None
        if c is None or self.clock() - c["asked"] > TYPED_CONFIRM_FOR:
            return False
        yn = plain.yes_no(text)
        if yn is None:
            return False
        if yn == "yes" and c.get("frozen") and plain.normalise(text) != "change it anyway":
            self.send("That breaks the frozen test, so it needs the Change it anyway button "
                      "(or the words \"change it anyway\"). Nothing has changed yet.")  # fmt: skip
            return True
        self.on_own_button(f"trd:{'ok' if yn == 'yes' else 'no'}:{cid}")
        return True

    # -- the other plain intents ----------------------------------------------------------
    def do_help(self, intent, text: str) -> None:
        self.ctl.cmd_help("")

    def do_new(self, intent, text: str) -> None:
        if intent.reset:
            self.ctl.cmd_reset("")
        else:
            self.ctl.cmd_new("")

    def do_undo(self, intent, text: str) -> None:
        self.ctl.cmd_undo("")

    def do_changes(self, intent, text: str) -> None:
        if not [r for r in botctl.list_reqs("trader") if r.get("status") != "new"]:
            self.send("No change requests yet. Ask in your own words - \"can you make it "
                      "...\" - and I'll read it back before anything is built.")  # fmt: skip
            return
        self.ctl.cmd_changes("")

    def do_queue_show(self, intent, text: str) -> None:
        q = self.ctl.queue()
        what = QUEUE_WORDS.get(q["mode"], q["mode"])
        self.send(f"While I'm busy with an answer, a new message {what}. That's queue mode "
                  f"{q['mode']}{' (set here)' if q['override'] else ' (the default)'}, debounce "
                  f"{q['debounce_ms']}ms, cap {q['cap']}, drop {q['drop']}. To change it, say "
                  "\"answer them one at a time\", \"bundle my messages\", \"interrupt mode\" "
                  "or \"queue back to normal\".")  # fmt: skip

    def do_queue_set(self, intent, text: str) -> None:
        self.ctl.cmd_queue(intent.mode)

    def _openclaw(self) -> dict | None:
        try:
            return botctl.load_openclaw()
        except (OSError, ValueError) as e:
            log.warning("openclaw.json unreadable: %s", e)
            self.send("I can't read the model settings right now; try again in a minute.")
            return None

    def do_model_show(self, intent, text: str) -> None:
        cfg = self._openclaw()
        if cfg is None:
            return
        lines = self.ctl.model_lines(cfg)
        allowed = ", ".join(botctl.pretty_model(m) for m in botctl.allowed_models(cfg))
        lines.append(f"Models allowed here: {allowed}.")
        lines.append("To switch one, say for example \"use Sonnet 5 for the reader\" or \"put "
                     "the decider back to normal\". A switch is a strategy change: I read it "
                     "back first, and it is recorded in config.yaml and listed in the evening "
                     "report.")  # fmt: skip
        fz = self.freeze()
        if fz:
            lines.append(f"Right now {frozen_words(fz)} freezes them.")
        self.send("\n".join(lines))

    def do_think_show(self, intent, text: str) -> None:
        cfg = self._openclaw()
        if cfg is None:
            return
        lines = self.ctl.model_lines(cfg)
        lines.append("Thinking levels, lowest to highest: " + ", ".join(plain.THINK_LADDER)
                     + ". Say for example \"make the decider think harder\" or \"reader "
                     "effort low\"; that is a strategy change, read back first, recorded and "
                     "reported.")  # fmt: skip
        fz = self.freeze()
        if fz:
            lines.append(f"Right now {frozen_words(fz)} freezes them.")
        self.send("\n".join(lines))

    def do_model_set(self, intent, text: str) -> None:
        if not intent.agent:
            self._ask_agent("model_set", intent.args)
            return
        agents = ["reader", "decider"] if intent.agent == "both" else [intent.agent]
        self.offer_setting(agents, "model", intent.model, how="plain words")

    def do_think_set(self, intent, text: str) -> None:
        if not intent.agent:
            self._ask_agent("think_set", intent.args)
            return
        agents = ["reader", "decider"] if intent.agent == "both" else [intent.agent]
        self.offer_setting(agents, "effort", intent.level, how="plain words")

    def do_ticker(self, intent, text: str) -> None:
        arena = self.light_arena()
        day, label = self._day(text)
        lead = self._lead(text, label)
        for i, code in enumerate(intent.tickers[:3]):
            body = T.ticker_text(arena, day, code, label)
            self.send("\n".join(lead + [body]) if i == 0 and lead else body)

    def do_today(self, intent, text: str) -> None:
        arena = self.light_arena()
        day, label = self._day(text)
        facts = T.gather(arena, day, label)
        watcher = self._watcher() if label == "today" else None
        lines = self._lead(text, label) + T.summary_lines(facts, arena, watcher)
        self.send("\n".join(lines))

    def do_trades(self, intent, text: str) -> None:
        arena = self.light_arena()
        day, label = self._day(text)
        lines = self._lead(text, label) + T.trades_lines(T.gather(arena, day, label), arena)
        self.send("\n".join(lines))

    def do_pnl(self, intent, text: str) -> None:
        arena = self.light_arena()
        day, label = self._day(text)
        lines = self._lead(text, label) + T.money_lines(T.gather(arena, day, label))
        self.send("\n".join(lines))

    def do_week(self, intent, text: str) -> None:
        arena = self.light_arena()
        self.send("\n".join(T.week_lines(arena, self.clock().date(), intent.which)))

    def do_positions(self, intent, text: str) -> None:
        self.cmd_positions()

    def do_tasks(self, intent, text: str) -> None:
        self.ctl.cmd_tasks("")

    def do_status(self, intent, text: str) -> None:
        self.ctl.cmd_status("")

    def do_whoami(self, intent, text: str) -> None:
        self.ctl.cmd_whoami("")

    def do_feed(self, intent, text: str) -> None:
        from asxbot.ibkr.doctor import state_path
        from asxbot.ibkr.supervisor import SUPERVISOR_STATE

        cfg = self.cfg_loader()
        self.send("\n".join(feed_lines(cfg.data_dir, self.clock(),
                                       self.doctor_file or state_path(),
                                       self.supervisor_file or SUPERVISOR_STATE)))  # fmt: skip

    def do_watcher_restart(self, intent, text: str) -> None:
        self.send(f"{WATCHER_LOCK}\nRight now: {self._watcher()}")

    def do_report_sent(self, intent, text: str) -> None:
        """'Did the evening report go out?' - from data/arena/report_sent.json."""
        from asxbot.arena import hours as H
        from asxbot.arena.report import last_report_sent

        cfg = self.cfg_loader()
        now = self.clock()
        last = last_report_sent(cfg)
        slot = H.evening_slot(cfg, now.date())
        lines = [f"The last evening report went out at {_ampm(last.astimezone(SYD))} on "
                 f"{last.astimezone(SYD):%a %d %b} (its record of being sent)." if last
                 else "No evening report is on record as sent."]  # fmt: skip
        due = datetime.combine(now.date(), slot, tzinfo=SYD)
        if not self._trading_day(now.date()):
            lines.append(f"{now:%a %d %b} isn't a trading day, so there's no report tonight.")
        elif last and last.astimezone(SYD).date() == now.date():
            lines.append("That's tonight's.")
        elif now < due:
            lines.append(f"Tonight's is due at {_ampm(slot)}.")
        elif now < due + timedelta(minutes=30):
            lines.append(f"Tonight's was due at {_ampm(slot)} and isn't on record as sent yet; "
                         "it can take a few minutes.")  # fmt: skip
        else:
            lines.append(f"Tonight's was due at {_ampm(slot)} and is NOT on record as sent - "
                         "the evening run may have failed, and that's worth a look.")  # fmt: skip
        self.send("\n".join(lines))

    def do_hours(self, intent, text: str) -> None:
        from asxbot.arena import hours as H

        cfg = self.cfg_loader()
        now = self.clock()
        day = now.date()
        trading = self._trading_day(day)
        a0, a1 = H.announcement_window(cfg, day)
        o0, o1 = H.order_window(cfg, day)
        lines = [
            f"{day:%a %d %b}: " + ("an ASX trading day." if trading
                                   else "not an ASX trading day, so the watcher does not run."),
            f"On a trading day the watcher starts itself at 7:30am and stops at "
            f"{_ampm(H.watcher_stop_time(cfg, day))}; it reads announcements {_ampm(a0)} to "
            f"{_ampm(a1)} and the arena accepts orders {_ampm(o0)} to {_ampm(o1)}.",
            f"The evening report goes out at {_ampm(H.evening_slot(cfg, day))}. Daylight "
            f"saving is {'on' if H.is_dst(day) else 'off'}; the times move with it by "
            "themselves.",
        ]  # fmt: skip
        if trading:
            open_ = dtime(10, 0) <= now.time() < dtime(16, 10)
            lines.append(f"Right now ({now:%H:%M}) the market is "
                         f"{'open' if open_ else 'closed'} (10:00am to 4:00pm, then the closing "
                         "auction to about 4:10pm).")  # fmt: skip
        self.send("\n".join(lines))

    def facts_for(self, text: str) -> str:
        """The day's records, for a conversation turn that asks about the day's trading
        (or names a stock), so the decider answers from them and not from memory."""
        try:
            tickers = plain.tickers_in(text, self.known_codes(), self.record_codes())
            if not tickers and not plain.about_trading(text):
                return ""
            arena = self.light_arena()
            day, label = self._day(text)
            watcher = self._watcher() if label == "today" else None
            lead = self._lead(text, label)
            return "\n".join(lead + [T.facts_for_agent(arena, day, label, tickers, watcher)])
        except Exception as e:  # noqa: BLE001 - the turn still goes, without the block
            log.warning("could not gather the day's records for the decider: %s", e)
            return ""

    # ---------------------------------------------------------------- a conversation turn
    def _start_turn(self, text: str) -> None:
        self.spawn(self._turn, text)

    def _turn(self, text: str) -> None:
        key = self.session_key()
        typing_done = threading.Event()
        threading.Thread(target=self._keep_typing, args=(typing_done,), daemon=True).start()
        try:
            with botctl.TURNS.track(key, DECIDER, "the decider's answer to you") as turn:
                try:
                    message = chat_message(text, self.facts_for(text), self.foreman_note(),
                                           at=self.clock())
                    reply = self.agent_runner(message, key)
                except AgentCallFailed as e:
                    reply = (f"The decider didn't answer ({e}). Nothing was placed or "
                             "changed; try again in a minute.")  # fmt: skip
            if turn.stopped:
                log.info("the decider's answer was stopped by /stop; dropped")
                return
            self.deliver(text, reply)
        except Exception as e:  # noqa: BLE001
            log.exception("a conversation turn failed")
            self.send(f"Something went wrong getting the decider's answer "
                      f"({type(e).__name__}: {e}).")  # fmt: skip
        finally:
            typing_done.set()
            self.ctl.conveyor.finished()

    def deliver(self, text: str, reply: str) -> None:
        """The decider's answer to Rick - or, when it hands the message over (or says it
        can't do something anyway), the handover and at most one short line."""
        out = F.read_reply(reply)
        at = self._said_at.pop(text, None) or self.clock()
        if out.note:
            log.info("%s", out.note)
        if out.handover is None:
            if out.text is not None:
                self.send(out.text.strip() or "The decider sent back an empty answer.")
            return
        rule = F.off_limits(text)
        intent = plain.understand(text, self.known_codes())
        if rule or (intent is not None and intent.name == "order_request"):
            log.info("not handed over: %s", rule or "an order request")
            self.send(NO_ORDERS if not rule else
                      f"That's one of your hard rules ({rule}), so nothing will be built for "
                      "it.")  # fmt: skip
            return
        self.hand_over(text, at, out.handover, say=out.text)

    def _call_decider(self, message: str, session_key: str) -> str:
        cfg = self.cfg_loader()
        reply = call_agent(
            DECIDER, message, expect_model=expected_model(cfg, "decider"), timeout_s=600,
            data_dir=cfg.data_dir, purpose="chat with Rick", session_key=session_key,
        )  # fmt: skip
        return reply.text

    def new_session(self, reason: str) -> str:
        with self._state_lock:
            st = {"gen": 1, **self.state()}
            st["gen"] = int(st.get("gen") or 1) + 1
            write_text_atomic(json.dumps(st, indent=1), self.state_path)
        log.info("new conversation #%d (%s)", st["gen"], reason)
        return (f"The decider starts a new conversation with you (#{st['gen']}); what you "
                "said before won't be used.")  # fmt: skip

    # ---------------------------------------------------------------- strategy changes
    @staticmethod
    def _setting(fld: str) -> str:
        return "model" if fld == "model" else "effort"

    def freeze(self) -> dict | None:
        """The playbook test that freezes the models now, or None. If config.yaml can't be
        read, the models are treated as frozen: a change then needs Rick's tap."""
        from asxbot.arena import settings_history as H

        try:
            return H.frozen_test(self.raw())
        except Exception as e:  # noqa: BLE001
            log.warning("could not tell whether a playbook test is running: %s", e)
            return {"titles": ["the playbooks"], "start": None, "end": None, "days": 10}

    def _agent_named(self, word: str) -> botctl.Agent | None:
        w = str(word or "").lower()
        for a in self.adapter.agents:
            if w in (a.target.lower(), a.agent_id.lower(), *[x.lower() for x in a.aliases]):
                return a
        return None

    def slash_setting(self, cmd: str, arg: str) -> bool:
        """/model decider sonnet-5 or /think reader high while a test freezes the models:
        read back with the choice to break the test, instead of changing it at once. True if
        it was taken (outside a test, botctl changes it as it always has)."""
        toks = arg.split()
        if len(toks) < 2 or self._agent_named(toks[0]) is None or self.freeze() is None:
            return False
        agent = self._agent_named(toks[0])
        self.offer_setting([agent.target], "model" if cmd == "model" else "effort",
                           " ".join(toks[1:]), how=f"/{cmd}")  # fmt: skip
        return True

    def offer_setting(self, agents: list[str], setting: str, word: str, how: str) -> None:
        """Work out the change - the reader's model from Sonnet 5 to Opus 5.5, the decider's
        thinking from high to xhigh - and read it back with buttons. Nothing changes until
        Rick taps; while a test freezes the models the read-back says it breaks the test.
        A value botctl would refuse is passed to it to explain (nothing changes)."""
        cfg = self._openclaw()
        if cfg is None:
            return
        changes, already = [], []
        for name in agents:
            a = self._agent_named(name)
            try:
                old_m, old_t = botctl.effective(cfg, a.agent_id)
            except (KeyError, AttributeError):
                self.send(f"The {name} isn't set up on this PC, so there's nothing to change.")
                return
            w = str(word or "").strip().lower()
            if setting == "model":
                old, show = old_m, botctl.pretty_model
                if w in ("default", "reset", "clear", "inherit", "unpin"):
                    new = a.pinned_model
                else:
                    new, _ = botctl.resolve_model(word, botctl.allowed_models(cfg))
                    if new is None:
                        self.ctl.cmd_model(f"{name} {word}")  # says what is allowed
                        return
            else:
                old, show = old_t, str
                w = botctl.THINK_ALIASES.get(w, w)
                if w in ("up", "down"):
                    new = plain.step_level(old_t, w)
                    if new is None:
                        self.send(f"The {name} is on \"{old_t}\" thinking, which isn't a step on "
                                  "the ladder. Say a level: " + ", ".join(plain.THINK_LADDER)
                                  + ".")  # fmt: skip
                        return
                    if new == old_t:
                        end = "top" if w == "up" else "bottom"
                        already.append(f"The {name} is already at {old_t}, the {end} of the "
                                       "ladder.")  # fmt: skip
                        continue
                elif w in botctl.THINK_CLEAR:
                    new = a.pinned_thinking
                elif w not in botctl.THINK_LEVELS:
                    self.ctl.cmd_think(f"{name} {word}")  # says which levels there are
                    return
                else:
                    new = w
            if new == old:
                already.append(f"The Trader's {name} already uses {show(new)}.")
                continue
            changes.append({"agent": name, "old": old, "new": new,
                            "what": f"the {name}'s {'model' if setting == 'model' else 'thinking'}"
                                    f" from {show(old)} to {show(new)}"})  # fmt: skip
        if not changes:
            self.send("\n".join(already))
            return
        fz = self.freeze()
        what = " and ".join(c["what"] for c in changes)
        cid = self._new_confirm({"kind": "setting", "setting": setting, "changes": changes,
                                 "how": how, "frozen": fz})  # fmt: skip
        extra = ("\n" + "\n".join(already)) if already else ""
        if fz:
            self.send(f"Switching {what} now would break {frozen_words(fz)}: the agents' models "
                      "and thinking levels are frozen for it, so the days after the change "
                      "couldn't be compared with the days before. Change it anyway? It would be "
                      "recorded in config.yaml as a deliberate change that breaks the test, and "
                      "the evening report would say so." + extra,
                      [("Change it anyway", f"trd:ok:{cid}"), ("Cancel", f"trd:no:{cid}")])
            return
        self.send(f"Switch {what}? It's a strategy change: it applies from the agent's next "
                  "turn, is recorded in config.yaml with the date, and the evening report lists "
                  "it." + extra, [("Change it", f"trd:ok:{cid}"), ("Cancel", f"trd:no:{cid}")])

    def _apply_setting(self, c: dict) -> None:
        """Rick tapped the read-back: botctl makes the change (OpenClaw, then config.yaml),
        let through before_agent_change for exactly this value."""
        for ch in c["changes"]:
            self._allowed = (ch["agent"], c["setting"], ch["new"])
            self._how = {"how": c["how"], "frozen": c.get("frozen")}
            try:
                if c["setting"] == "model":
                    self.ctl.cmd_model(f"{ch['agent']} {ch['new']}")
                else:
                    self.ctl.cmd_think(f"{ch['agent']} {ch['new']}")
            finally:
                self._allowed = self._how = None

    def before_agent_change(self, agent: botctl.Agent, fld: str, old: str, new: str):
        from asxbot.arena import settings_history as H

        setting = self._setting(fld)
        fz = self.freeze()
        if fz is not None and self._allowed != (agent.target, setting, new):
            return FROZEN_VETO.format(test=frozen_words(fz))  # e.g. "/new opus" mid-test
        return H.refusal(self.repo, botctl.list_reqs(), agent.target, setting)

    def after_agent_change(self, agent, fld: str, old: str, new: str, note: str) -> str:
        """Record the change in config.yaml and commit it. Raising puts the OpenClaw setting
        back (botctl). The entry says how Rick asked, and that he confirmed on a read-back."""
        from asxbot.arena import settings_history as H

        cmd = "/model" if fld == "model" else "/think"
        how = self._how
        by = f"Rick, {cmd} in the Trader chat"
        entry_note = ""
        if how:
            by = f"Rick, {how['how']} in the Trader chat, confirmed on a read-back"
            fz = how.get("frozen")
            if fz:
                by += f"; breaks the frozen {fz['days']}-day test"
                end = f" (to {fz['end']:%d %b})" if fz.get("end") else ""
                entry_note = ("deliberate change by Rick, not a finding; it breaks the "
                              f"frozen playbook test{end}")  # fmt: skip
        entry = H.record(self.repo, agent.target, self._setting(fld), old, new, by=by,
                         note=entry_note)  # fmt: skip
        self.refresh_pins()
        log.info("strategy change recorded: %s %s %s -> %s (commit %s; %s)", agent.target,
                 self._setting(fld), old, new, entry.get("commit"), by)  # fmt: skip
        return (f"Recorded as a dated strategy change in config.yaml (commit "
                f"{entry.get('commit')}); {self._next_report()} will mention it.")  # fmt: skip

    def _next_report(self) -> str:
        """Which report will list a change: tonight's (with its time) before tonight's slot
        on a trading day, else the next one (26 Sep 2026: this said 19:00, and the slot
        moves with daylight saving)."""
        from asxbot.arena import hours as H

        now = self.clock()
        try:
            slot = H.evening_slot(self.cfg_loader(), now.date())
        except Exception:  # noqa: BLE001
            return "the next evening report"
        if self._trading_day(now.date()) and now.time() < slot:
            return f"tonight's evening report ({_ampm(slot)})"
        return "the next evening report"

    # ---------------------------------------------------------------- the Trader's own
    def light_arena(self):
        """Accounts and the arena broker only: no universes, no network. Its minute bars
        are the ones already on disk - the chat never fetches a price (26 Sep 2026: marking a
        position here called Yahoo with no timeout, on the poll thread, and wrote the cache
        from it)."""
        from asxbot.arena.runtime import Arena, arena_broker

        cfg = self.cfg_loader()
        broker = arena_broker(cfg)
        minutes = getattr(broker, "minutes", None)
        if minutes is not None:
            minutes.fetch = lambda code, day, force=False, _m=minutes: _m.cached(code, day)
        return Arena(cfg, broker, broker.store, set(), set())

    def cmd_positions(self) -> None:
        from asxbot.arena.cli import positions_lines

        try:
            arena = self.light_arena()
            lines = positions_lines(arena)
            priced = self._price_notes(arena)
        except Exception as e:  # noqa: BLE001
            log.exception("/positions failed")
            self.send(f"I couldn't read the arena accounts just now ({type(e).__name__}: {e}).")
            return
        self.send("Arena positions (FAKE money):\n" + ("\n".join(lines) or "no accounts")
                  + (f"\n{priced}" if priced else ""))  # fmt: skip

    def _price_notes(self, arena) -> str:
        """Where each open position's 'now' comes from: the last minute bar on disk and its
        time, or its cost. None of it is a live quote."""
        day = self.clock().date()
        notes = []
        for pb in arena.playbooks():
            for kind in ("agent", "bot"):
                for t in arena.account(pb, kind).positions:
                    px, at = T.disk_price(arena, t, day)
                    if px is None:
                        notes.append(f"{t} at cost (no price on disk today)")
                    else:
                        notes.append(f"{t} at its last minute bar on disk, {at:%H:%M}")
        if not notes:
            return ""
        return "Prices: " + "; ".join(dict.fromkeys(notes)) + ". None of these is a live quote."

    def status_lines(self) -> list[str]:
        out = []
        cfg = None
        try:
            cfg = self.cfg_loader()
            out.append(watcher_line(cfg, self.clock()))
        except Exception as e:  # noqa: BLE001
            out.append(f"Watcher: couldn't tell ({type(e).__name__}: {e})")
        if cfg is not None:
            try:
                out.append(data_line(cfg.data_dir, self.clock()))
            except Exception as e:  # noqa: BLE001
                out.append(f"Data: couldn't read the feed status ({type(e).__name__}: {e})")
            try:
                paused = T.pause_line(cfg.data_dir, self.clock().date())
                if paused:
                    out.append(paused)
            except Exception as e:  # noqa: BLE001
                out.append(f"New entries: couldn't read the pause switch ({e})")
        try:
            arena = self.light_arena()
            n = sum(len(arena.account(pb, k).positions)
                    for pb in arena.playbooks() for k in ("agent", "bot"))  # fmt: skip
            out.append(f"Open fake-money positions: {n}"
                       + (" (say \"positions\" to see them)" if n else ""))  # fmt: skip
        except Exception as e:  # noqa: BLE001
            out.append(f"Positions: couldn't read ({type(e).__name__}: {e})")
        if cfg is not None:
            try:
                out.append(orders_today_line(cfg, self.clock().date()))
            except Exception as e:  # noqa: BLE001
                out.append(f"Orders today: couldn't read ({type(e).__name__}: {e})")
        out.append(f"Conversation: #{int(self.state().get('gen') or 1)} (say \"start over\" "
                   "for a fresh one)")  # fmt: skip
        return out


def watcher_line(cfg, now: datetime | None = None) -> str:
    """Is the watcher running? The watchdog's own verdict, from the heartbeat file."""
    from asxbot.arena.heartbeat import read
    from asxbot.arena.watchdog import assess, expected_window, pid_alive

    now = (now or datetime.now(SYD)).astimezone(SYD)
    hb = read(cfg.data_dir)
    v = assess(hb, now, expected_window(cfg, now), pid_alive)
    started = (hb or {}).get("started") or ""
    if v.expected and v.ok:
        return f"Watcher: running (pid {v.pid}, started {started[11:16]})"
    if v.expected:
        return f"Watcher: {v.problem.upper()} - {v.detail}"
    if hb and hb.get("state") == "running" and hb.get("pid") and pid_alive(int(hb["pid"])):
        return f"Watcher: running outside its usual hours (pid {hb['pid']})"
    last = f" Last run started {started[:16].replace('T', ' ')}, ended {hb.get('state')}." \
        if hb and started else ""  # fmt: skip
    try:
        from asxbot.arena.hours import watcher_stop_time

        stop = _ampm(watcher_stop_time(cfg, now.date()))
    except Exception:  # noqa: BLE001 - the words still stand without the time
        stop = "the evening"
    return ("Watcher: not running now; it starts itself at 7:30am on trading days and stops "
            f"at {stop}." + last)  # fmt: skip


def orders_today_line(cfg, day: date | None = None) -> str:
    from asxbot.log import EventLog

    day = day or datetime.now(SYD).date()
    placed = refused = 0
    for r in EventLog(cfg.data_dir).read("arena_orders"):
        try:
            t = datetime.fromisoformat(str(r.get("ts"))).astimezone(SYD).date()
        except ValueError:
            continue
        if t == day:
            placed += r.get("outcome") == "accepted"
            refused += r.get("outcome") == "refused"
    return f"Orders today: {placed} placed, {refused} refused by the limits"


# --------------------------------------------------------------------------- running it


def poll(app: TraderChat, stop: threading.Event) -> int:
    """Long-poll Telegram until `stop` is set. Returns an exit code."""
    offset = int(app.state().get("offset") or 0) or None
    backoff = BACKOFF_START_S
    done = threading.Event()

    def ticker():  # the /queue debounce window needs a clock between messages
        while not done.wait(1) and not stop.is_set():
            try:
                app.ctl.tick()
            except Exception:  # noqa: BLE001
                log.exception("queue tick failed")

    threading.Thread(target=ticker, daemon=True).start()
    try:
        return _poll(app, stop, offset, backoff)
    finally:
        done.set()


def _poll(app: TraderChat, stop: threading.Event, offset: int | None, backoff: float) -> int:
    while not stop.is_set():
        try:
            updates = app.tg.get_updates(offset, POLL_TIMEOUT_S)
        except TgConflict as e:
            log.error("STOPPING: Telegram 409 Conflict - another program is polling this bot "
                      "(%s). Only one may: check OpenClaw's gateway and any other `asxbot "
                      "chat`.", e)  # fmt: skip
            return EXIT_CONFLICT
        except TgUnauthorized as e:
            log.error("STOPPING: Telegram refused the bot token: %s", e)
            return EXIT_TELEGRAM
        except TgRetry as e:
            log.warning("%s; waiting %.0fs", e, e.after)
            stop.wait(min(e.after, 300))
            continue
        except TgError as e:
            log.warning("Telegram unreachable (%s); trying again in %.0fs", e, backoff)
            stop.wait(backoff)
            backoff = min(backoff * 2, 300.0)
            continue
        backoff = BACKOFF_START_S
        stale = []
        for u in updates:
            offset = int(u["update_id"]) + 1
            app.save_state(offset=offset)  # before handling: a crash never answers twice
            what = app.on_update(u)
            if what.startswith("stale"):
                stale.append(what.split(": ", 1)[1])
        if stale:
            app.send(f"I wasn't running when you sent {len(stale)} message(s) (the last at "
                     f"{stale[-1]}), so I haven't answered. Send it again if you still want "
                     "an answer.")  # fmt: skip
    return EXIT_OK


def watch_parent(pid: int, stop: threading.Event) -> None:
    """Exit when the launcher that started this is gone (a task ended from Task Scheduler
    kills the launcher only), so a restarted task never finds an orphan still polling."""
    from asxbot.arena.watchdog import pid_alive

    def loop():
        while not stop.wait(5):
            if not pid_alive(pid):
                log.warning("the launcher (pid %d) has gone; stopping", pid)
                for h in log.parent.handlers if log.parent else []:
                    h.flush()
                os._exit(EXIT_OK)

    threading.Thread(target=loop, daemon=True).start()


def wait_idle(app: TraderChat, limit_s: float = 900) -> None:
    deadline = time.time() + limit_s
    while time.time() < deadline:
        app.ctl.tick()
        if not app.busy():
            time.sleep(0.3)
            if not app.busy():
                return
        time.sleep(0.2)
    log.warning("the probe stopped waiting after %.0fs", limit_s)


def main(args) -> int:
    from asxbot.config import load_config, repo_root
    from asxbot.log import setup_logging
    from asxbot.telegram import TelegramError, load_bot

    setup_logging(file=False, stream=sys.stdout)
    repo = repo_root()
    from asxbot import release

    log.info("Trader chat starting on commit %s (botctl %s, pid %d), code from %s",
             git_commit(repo), botctl.BOTCTL_VERSION, os.getpid(), release.describe())  # fmt: skip
    cfg = load_config()
    try:
        bot = load_bot(cfg)
    except TelegramError as e:
        log.error("STOPPING: %s", e)
        return EXIT_TELEGRAM
    if not bot.chat_id:
        log.error("STOPPING: no paired chat id (asxbot telegram pair --chat-id <id>)")
        return EXIT_TELEGRAM
    if str(bot.chat_id) != botctl.RICK_ID:
        log.warning("the paired chat %s is not Rick's id %s: botctl ignores taps from anyone "
                    "else", bot.chat_id, botctl.RICK_ID)  # fmt: skip
    home = chat_home()
    probe = args.probe is not None or args.button is not None
    if probe:
        runner = None
        if args.no_agent:
            def runner(message: str, key: str) -> str:
                return (f"(--no-agent: the decider was not called. On session {key} it "
                        f"would have been sent:)\n\n{message}")  # fmt: skip
        def printed(text: str, at: datetime, why: str, reply_to: str | None = None) -> str:
            """A probe never writes to the Foreman's inbox (the Foreman would answer Rick)."""
            body = {"bot": F.BOT, "from": "Rick", "text": text,
                    "at": at.isoformat(timespec="seconds"), "why": why}  # fmt: skip
            if reply_to:
                body["reply_to"] = reply_to
            print(f"\n--- to the Foreman's inbox (probe: not written) ---\n"
                  f"{json.dumps(body, ensure_ascii=False)}", flush=True)  # fmt: skip
            return "probe-not-written.json"

        app = TraderChat(PrintTelegram(), bot.chat_id, repo, home, agent_runner=runner,
                         handover=printed)  # fmt: skip
        if args.button is not None:
            app.on_callback({"id": "probe", "from": {"id": bot.chat_id}, "data": args.button,
                             "message": {"chat": {"id": bot.chat_id}}})  # fmt: skip
        else:
            app.on_text(args.probe.strip())
        wait_idle(app)
        return EXIT_OK

    problem = openclaw_still_polls(bot.token)
    if problem:
        log.error("REFUSING TO START: %s", problem)
        return EXIT_OPENCLAW_POLLS
    lock = SingleInstance(home / "chat.lock")
    if not lock.acquire(wait_s=LOCK_WAIT_S):
        log.error("REFUSING TO START: another Trader chat is already running (lock %s)",
                  home / "chat.lock")  # fmt: skip
        return EXIT_ALREADY_RUNNING
    try:
        tg = Telegram(bot.token)
        try:
            me = tg.call("getMe")
        except TgUnauthorized as e:
            log.error("STOPPING: Telegram refused the bot token: %s", e)
            return EXIT_TELEGRAM
        except TgError as e:
            me = {}
            log.warning("getMe failed (%s); polling anyway", e)
        app = TraderChat(tg, bot.chat_id, repo, home)
        stop = threading.Event()
        parent = os.environ.get("ASXBOT_CHAT_PARENT")
        if parent and parent.isdigit():
            watch_parent(int(parent), stop)
        log.info("Running. Polling @%s (token %s) for Rick's chat %s; state in %s",
                 me.get("username", "?"), tg.masked, bot.chat_id, home)  # fmt: skip
        try:
            return poll(app, stop)
        except KeyboardInterrupt:
            log.info("stopped (Ctrl-C)")
            return EXIT_OK
    finally:
        lock.release()
