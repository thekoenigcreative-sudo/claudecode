"""Telegram delivery for the arena's evening report and alerts.

The trader bot is separate from the editorial agent's bot. Its token lives only in .env
(gitignored) and is never logged - only the last four digits, so a wrong token can be
spotted without exposing it.

This is deliberately plain code rather than an agent capability: the report must still
arrive on an evening when a model call fails.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import requests

from asxbot.config import Config
from asxbot.io import write_text_atomic
from asxbot.log import get_logger

log = get_logger("asxbot.telegram")
API = "https://api.telegram.org"
MAX_LEN = 4096  # Telegram's hard limit on one message


class TelegramError(RuntimeError):
    pass


@dataclass
class Bot:
    token: str
    chat_id: str | None = None

    @property
    def masked(self) -> str:
        return f"...{self.token[-4:]}" if len(self.token) > 4 else "(set)"

    def _call(self, method: str, **params):
        r = requests.post(f"{API}/bot{self.token}/{method}", data=params, timeout=30)
        try:
            body = r.json()
        except Exception as e:  # noqa: BLE001
            raise TelegramError(f"{method}: unreadable reply ({r.status_code}): {e}") from e
        if not body.get("ok"):
            raise TelegramError(f"{method} failed: {body.get('description', body)}")
        return body["result"]

    def me(self) -> dict:
        return self._call("getMe")

    def updates(self) -> list[dict]:
        return self._call("getUpdates")

    def send(self, text: str, chat_id: str | None = None) -> list[int]:
        """Send text, split across messages if it exceeds Telegram's limit."""
        target = chat_id or self.chat_id
        if not target:
            raise TelegramError(
                "no Telegram chat id. Send the bot a message, then run: asxbot telegram pair"
            )
        ids = []
        for chunk in _split(text, MAX_LEN):
            res = self._call("sendMessage", chat_id=target, text=chunk, parse_mode="HTML")
            ids.append(int(res["message_id"]))
        log.info("telegram: sent %d message(s) to %s via bot %s", len(ids), target, self.masked)
        return ids


def _split(text: str, limit: int) -> list[str]:
    """Split on line boundaries so a table is never cut mid-row."""
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # a single monstrous line
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit:
            out.append(cur)
            cur = line
        else:
            cur += line
    if cur:
        out.append(cur)
    return out


def _chat_file(cfg: Config) -> Path:
    return cfg.data_dir / "arena" / "telegram_chat.json"


def load_bot(cfg: Config) -> Bot:
    token = (cfg.env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set in .env")
    chat = (cfg.env.get("TELEGRAM_CHAT_ID") or "").strip() or None
    if not chat:
        f = _chat_file(cfg)
        if f.exists():
            chat = str(json.loads(f.read_text(encoding="utf-8")).get("chat_id") or "") or None
    return Bot(token=token, chat_id=chat)


def pair(cfg: Config) -> str:
    """Learn the chat id from whoever has messaged the bot, and remember it."""
    bot = load_bot(cfg)
    ups = bot.updates()
    if not ups:
        raise TelegramError(
            "nobody has messaged the bot yet. Open Telegram, find the trader bot, "
            "send it /start, then run this again."
        )
    chats = {}
    for u in ups:
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            chats[str(chat["id"])] = chat.get("username") or chat.get("first_name") or "?"
    if not chats:
        raise TelegramError("updates held no chat id; send the bot a plain text message")
    chat_id = sorted(chats)[-1]
    write_text_atomic(
        json.dumps({"chat_id": chat_id, "who": chats[chat_id]}, indent=2), _chat_file(cfg)
    )
    log.info("telegram: paired with chat %s (%s)", chat_id, chats[chat_id])
    return chat_id
