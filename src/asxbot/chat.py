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

Nothing here can place, change or approve an order: the decider is told so, and this code
has no order path at all. /model and /think are strategy changes (arena/settings_history).

Every process it starts goes through asxbot.proc (botctl's RUN hook is pointed there).
Logs go to stdout only; scripts/chat.pyw writes them to chat.log in the local logs folder.

Exit codes: 0 stopped; 2 config error; 3 Telegram token or chat id problem;
10 OpenClaw still polls this bot; 11 another Trader chat holds the lock;
12 Telegram 409 Conflict (another program is polling this bot).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

from asxbot import botctl, plain, proc
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

PREFACE = """\
This is Rick, writing to you directly in the Trader's own Telegram chat. It is a
conversation, not a watcher cycle: there is no announcement and no decision packet.
- Answer him plainly, in plain text for Telegram (no markdown tables or headings), and
  briefly unless he asks for detail.
- You cannot place, change, close or approve any order from this chat, and nothing said
  here is an order. If he asks for a trade, say so plainly and say what you would look at
  instead. Do not end with a DECISION block.
- The arena is fake money. Never say an order was placed, approved or filled unless an
  arena record you have read shows the broker's order id.
- If he asks you to change how the Trader works, tell him to say what he wants changed, in
  his own words, in this chat (for example "can you make it ..."): it is read back to him
  with a Build it button, and nothing changes until he taps it. You cannot change it.
- Never tell him to type a command: everything here works from plain words.
"""

FACTS_RULE = """\
The block below is what the arena's own records say about the day - accounts, orders,
fills, decisions - gathered by code just now. It is the truth about what happened. Answer
anything about the day's trading from it; where it is silent, say "not on record" rather
than recall or guess, and never restate a number it does not contain.

FACTS ON RECORD:
"""

OWN_HELP = """\
The Trader chat: talk to the decider, the agent that makes the arena's trades, in your own \
words. It never places real trades, and it can't place, change or approve any order from \
this chat. The arena is fake money. The watcher runs by itself, 7:30am to 7:25pm on \
trading days.

Things you can just say:
- "how's it going today", "what did it trade", "how much are we up", "show me the positions"
- "why did it pass on NWL", "what happened with HLS" - answered from the day's records
- "is it running", "when does it start", "what's running"
- "use opus for the decider", "make the reader think harder", "what model is it on" - a \
model or thinking change is a strategy change, recorded in config.yaml and in the evening \
report
- "stop" drops the answer I'm working on; "start over" begins a fresh conversation
- "can you make it ..." asks for a change to how I work: I read it back, and nothing is \
built until you tap Build it. "what changes have I asked for", "undo the last change"
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
TRADING_NOTE = (
    "Trading itself isn't stopped from this chat: the watcher runs by itself until 7:25pm on "
    "a trading day and is never stopped in market hours or with a position open. If you want "
    "it to trade differently, tell me what to change in your own words and I'll read it back "
    "before anything is built."
)
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
# the decider think harder" is a setting, done on the spot, not a build).
PLAIN_FIRST = {"model_set", "think_set", "stop", "order_request"}


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


def chat_message(text: str, facts: str = "") -> str:
    body = PREFACE
    if facts:
        body += "\n" + FACTS_RULE + facts + "\n"
    return body + "\nRick's message:\n" + text


def _ampm(t) -> str:
    return f"{t.hour % 12 or 12}:{t.minute:02d}{'am' if t.hour < 12 else 'pm'}"


class TraderChat:
    """One Telegram chat with Rick. `tg` sends (Telegram or PrintTelegram); `agent_runner
    (message, session_key) -> text` is the decider turn (call_agent, unless a test or
    --no-agent replaces it)."""

    def __init__(self, tg, chat_id: str, repo: Path, home: Path, *, cfg_loader=None,
                 agent_runner=None):  # fmt: skip
        self.tg = tg
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
                "7:30am-7:25pm on ASX trading days; Rick gets instant alerts, an hourly "
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
            self.on_text(text.strip())
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
        if not self.ctl.on_button(data, who):
            log.info("a button this chat doesn't know: %s", data)
            return "unknown button"
        return "button"

    def on_text(self, text: str) -> None:
        log.info("from Rick: %s", text[:200].replace("\n", " "))
        self.refresh_pins()
        if self.ctl.handle(text, self.chat_id):
            return
        parsed = botctl.parse_command(text)
        if parsed or text.startswith("/"):
            cmd = parsed[0] if parsed else ""
            if cmd == "start":
                self.ctl.cmd_help("")
            elif cmd == "positions":
                self.cmd_positions()
            else:
                self.send(UNKNOWN_COMMAND)
            return
        if self.ctl.answer_pending(text):
            return  # the answer to the change reader's one question
        if self.plain_intent(text):
            return
        if self.ctl.maybe_change(text):
            return
        self.to_conveyor(text)

    def to_conveyor(self, text: str) -> None:
        line = self.ctl.conveyor.submit(text)
        if line:
            self.send(line)

    # ---------------------------------------------------------------- plain language
    def known_codes(self) -> set[str]:
        try:
            return T.known_codes(self.cfg_loader().data_dir)
        except Exception as e:  # noqa: BLE001 - a missing list only costs ticker recognition
            log.warning("could not read the ASX code lists: %s", e)
            return set()

    def understand(self, text: str) -> plain.Intent | None:
        """Rick's message as an intent - or, if the chat has just asked "for the reader or
        the decider?", the answer to that."""
        pending, self._pending = self._pending, None
        if pending and self.clock() - pending["asked"] <= PENDING_FOR:
            agent = plain.answer_agent(text)
            if agent:
                return plain.Intent(pending["name"], {**pending["args"], "agent": agent})
        return plain.understand(text, self.known_codes())

    def plain_intent(self, text: str) -> bool:
        """A plain sentence that means one of the chat's own commands, done in code. True
        if it was taken."""
        intent = self.understand(text)
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

    def _watcher(self) -> str:
        try:
            return watcher_line(self.cfg_loader(), self.clock())
        except Exception as e:  # noqa: BLE001
            return f"Watcher: couldn't tell ({type(e).__name__}: {e})"

    def _ask_agent(self, name: str, args: dict) -> None:
        self._pending = {"name": name, "args": dict(args), "asked": self.clock()}
        self.send(WHICH_AGENT)

    def do_order_request(self, intent, text: str) -> None:
        self.send(NO_ORDERS)

    def do_stop(self, intent, text: str) -> None:
        out = self.ctl.stop_text()
        if intent.trading:
            out += "\n" + TRADING_NOTE
        self.send(out)

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
                  "\"answer them one at a time\", \"bundle my messages\", \"interrupt\" or "
                  "\"queue back to normal\".")  # fmt: skip

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
                     "the decider back to normal\". A switch is a strategy change: it is "
                     "recorded in config.yaml and listed in the evening report.")  # fmt: skip
        self.send("\n".join(lines))

    def do_think_show(self, intent, text: str) -> None:
        cfg = self._openclaw()
        if cfg is None:
            return
        lines = self.ctl.model_lines(cfg)
        lines.append("Thinking levels, lowest to highest: " + ", ".join(plain.THINK_LADDER)
                     + ". Say for example \"make the decider think harder\" or \"reader "
                     "effort low\"; that is a strategy change, recorded and reported.")  # fmt: skip
        self.send("\n".join(lines))

    def do_model_set(self, intent, text: str) -> None:
        if not intent.agent:
            self._ask_agent("model_set", intent.args)
            return
        agents = ("reader", "decider") if intent.agent == "both" else (intent.agent,)
        for agent in agents:
            self.ctl.cmd_model(f"{agent} {intent.model}")

    def do_think_set(self, intent, text: str) -> None:
        if not intent.agent:
            self._ask_agent("think_set", intent.args)
            return
        agents = ("reader", "decider") if intent.agent == "both" else (intent.agent,)
        for agent in agents:
            level = intent.level
            if level in ("up", "down"):
                cfg = self._openclaw()
                if cfg is None:
                    return
                try:
                    _, current = botctl.effective(cfg, AGENT_IDS[agent])
                except KeyError:
                    self.send(f"The {agent} isn't set up on this PC, so there's nothing to "
                              "change.")  # fmt: skip
                    return
                new = plain.step_level(current, level)
                if new is None:
                    self.send(f"The {agent} is on \"{current}\" thinking, which isn't a step "
                              "on the ladder. Say a level: " + ", ".join(plain.THINK_LADDER)
                              + ".")  # fmt: skip
                    return
                if new == current:
                    end = "top" if level == "up" else "bottom"
                    self.send(f"The {agent} is already at {current}, the {end} of the ladder.")
                    return
                level = new
            self.ctl.cmd_think(f"{agent} {level}")

    def do_ticker(self, intent, text: str) -> None:
        arena = self.light_arena()
        day, label = self._day(text)
        for code in intent.tickers[:3]:
            self.send(T.ticker_text(arena, day, code, label))

    def do_today(self, intent, text: str) -> None:
        arena = self.light_arena()
        day, label = self._day(text)
        facts = T.gather(arena, day, label)
        watcher = self._watcher() if label == "today" else None
        self.send("\n".join(T.summary_lines(facts, arena, watcher)))

    def do_trades(self, intent, text: str) -> None:
        arena = self.light_arena()
        day, label = self._day(text)
        self.send("\n".join(T.trades_lines(T.gather(arena, day, label), arena)))

    def do_pnl(self, intent, text: str) -> None:
        arena = self.light_arena()
        day, label = self._day(text)
        self.send("\n".join(T.money_lines(T.gather(arena, day, label))))

    def do_positions(self, intent, text: str) -> None:
        self.cmd_positions()

    def do_tasks(self, intent, text: str) -> None:
        self.ctl.cmd_tasks("")

    def do_status(self, intent, text: str) -> None:
        self.ctl.cmd_status("")

    def do_whoami(self, intent, text: str) -> None:
        self.ctl.cmd_whoami("")

    def do_hours(self, intent, text: str) -> None:
        from asxbot.announcements.live import is_trading_day
        from asxbot.arena import hours as H

        cfg = self.cfg_loader()
        day = self.clock().date()
        trading = day.weekday() < 5 and is_trading_day(day)
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
        self.send("\n".join(lines))

    def facts_for(self, text: str) -> str:
        """The day's records, for a conversation turn that asks about the day's trading
        (or names a stock), so the decider answers from them and not from memory."""
        try:
            tickers = plain.tickers_in(text, self.known_codes())
            if not tickers and not plain.about_trading(text):
                return ""
            arena = self.light_arena()
            day, label = self._day(text)
            watcher = self._watcher() if label == "today" else None
            return T.facts_for_agent(arena, day, label, tickers, watcher)
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
                    reply = self.agent_runner(chat_message(text, self.facts_for(text)), key)
                except AgentCallFailed as e:
                    reply = (f"The decider didn't answer ({e}). Nothing was placed or "
                             "changed; try again in a minute.")  # fmt: skip
            if turn.stopped:
                log.info("the decider's answer was stopped by /stop; dropped")
                return
            self.send((reply or "").strip() or "The decider sent back an empty answer.")
        except Exception as e:  # noqa: BLE001
            log.exception("a conversation turn failed")
            self.send(f"Something went wrong getting the decider's answer "
                      f"({type(e).__name__}: {e}).")  # fmt: skip
        finally:
            typing_done.set()
            self.ctl.conveyor.finished()

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

    def before_agent_change(self, agent: botctl.Agent, fld: str, old: str, new: str):
        from asxbot.arena import settings_history as H

        return H.refusal(self.repo, botctl.list_reqs(), agent.target, self._setting(fld))

    def after_agent_change(self, agent, fld: str, old: str, new: str, note: str) -> str:
        """Record the change in config.yaml and commit it. Raising puts the OpenClaw setting
        back (botctl)."""
        from asxbot.arena import settings_history as H

        cmd = "/model" if fld == "model" else "/think"
        entry = H.record(self.repo, agent.target, self._setting(fld), old, new,
                         by=f"Rick, {cmd} in the Trader chat")  # fmt: skip
        self.refresh_pins()
        log.info("strategy change recorded: %s %s %s -> %s (commit %s)", agent.target,
                 self._setting(fld), old, new, entry.get("commit"))  # fmt: skip
        now = datetime.now(SYD)
        report = ("tonight's evening report" if now.weekday() < 5 and now.hour < 19
                  else "the next evening report")  # fmt: skip
        return (f"Recorded as a dated strategy change in config.yaml (commit "
                f"{entry.get('commit')}); {report} will mention it.")  # fmt: skip

    # ---------------------------------------------------------------- the Trader's own
    def light_arena(self):
        """Accounts and the arena broker only: no universes, no network."""
        from asxbot.arena.runtime import Arena, arena_broker

        cfg = self.cfg_loader()
        broker = arena_broker(cfg)
        return Arena(cfg, broker, broker.store, set(), set())

    def cmd_positions(self) -> None:
        from asxbot.arena.cli import positions_lines

        try:
            lines = positions_lines(self.light_arena())
        except Exception as e:  # noqa: BLE001
            log.exception("/positions failed")
            self.send(f"I couldn't read the arena accounts just now ({type(e).__name__}: {e}).")
            return
        self.send("Arena positions (FAKE money):\n" + ("\n".join(lines) or "no accounts"))

    def status_lines(self) -> list[str]:
        out = []
        cfg = None
        try:
            cfg = self.cfg_loader()
            out.append(watcher_line(cfg))
        except Exception as e:  # noqa: BLE001
            out.append(f"Watcher: couldn't tell ({type(e).__name__}: {e})")
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
                out.append(orders_today_line(cfg))
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
    return ("Watcher: not running now; it starts itself at 7:30am on trading days and stops "
            "at 7:25pm." + last)  # fmt: skip


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
        app = TraderChat(PrintTelegram(), bot.chat_id, repo, home, agent_runner=runner)
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
