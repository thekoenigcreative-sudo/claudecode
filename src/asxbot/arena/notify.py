"""Instant Telegram alerts from the arena, sent as things happen.

Sent the moment it happens, one short message each:
  * a trade DECIDED - by the agent or the yardstick bot - and the order id the broker gave it;
  * a trade the hard limits REFUSED;
  * every FILL, with the fill price and the stop the position now carries;
  * every STOP that fires, and every position CLOSED, with the result.

Sent as ONE DIGEST PER HOUR, because they are frequent and none of them needs an answer:
  * each announcement the decider looked at and PASSED on, one line and its reason.

Queued passes are held in a small file, so a digest survives the watcher restarting, and
the last part-hour is flushed when the watcher stops for the day.

The evening report is separate and unchanged (report.py).

Plain code, like the report: an alert is a fact the code already holds, never something a
model wrote about what happened. Delivery is best effort - a Telegram failure is logged and
swallowed, because an alert must never stop an order, a fill or a stop being recorded.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot.config import Config
from asxbot.io import write_text_atomic
from asxbot.log import EventLog, get_logger

log = get_logger("asxbot.arena.notify")
SYD = ZoneInfo("Australia/Sydney")
DIGEST_MINUTES = 60


def one_line(text: str) -> str:
    """Text flattened onto one line, never trimmed: reasons and headlines go in full."""
    return " ".join(str(text or "").split())


def who(placed_by: str) -> str:
    return {"agent": "AGENT", "bot": "BOT", "code": "CODE"}.get(placed_by, placed_by.upper())


def account_kind(account: str) -> str:
    return "AGENT" if account.endswith("__agent") else "BOT" if account.endswith("__bot") else ""


class Notifier:
    """Sends arena alerts to the trader bot's Telegram chat. Never raises."""

    def __init__(self, cfg: Config, enabled: bool = True, digest_minutes: int = DIGEST_MINUTES):
        self.cfg = cfg
        self.enabled = bool(enabled)
        self.digest_minutes = int(digest_minutes)
        self.events = EventLog(cfg.data_dir)
        self._bot = None
        self.queue_path = Path(cfg.data_dir) / "arena" / "pass_digest.json"

    def send(self, text: str) -> bool:
        self.events.append("arena_notify", {"text": text, "enabled": self.enabled})
        if not self.enabled:
            return False
        try:
            from asxbot.telegram import load_bot

            if self._bot is None:
                self._bot = load_bot(self.cfg)
            self._bot.send(text)
            return True
        except Exception as e:  # noqa: BLE001 - an alert must never break trading
            log.warning("telegram alert NOT sent (%s): %s", e, text.splitlines()[0][:80])
            return False

    # -- decisions ----------------------------------------------------------
    def decided(self, o, confidence=None) -> None:
        conf = f", confidence {confidence}%" if confidence not in (None, "") else ""
        stop = f"{o.stop:.3f}" if o.stop is not None else "none"
        self.send(
            f"🟡 <b>{who(o.placed_by)} DECIDED</b> {o.side.upper()} {escape(o.ticker)}\n"
            f"{o.qty:,} @ limit {o.limit:.3f} (~${o.qty * o.limit:,.0f}), stop {stop}{conf}\n"
            f"order {o.order_id}, fill pending from the true minute bar\n"
            f"<i>{escape(one_line(o.reason))}</i>"
        )

    def refused(self, placed_by: str, ticker: str, side: str, qty, why: str) -> None:
        self.send(
            f"⛔ <b>{who(placed_by)} REFUSED by the limits</b> {str(side).upper()} "
            f"{escape(ticker)} {qty}\n<i>{escape(one_line(why))}</i>"
        )

    # -- passes: queued, and sent as one digest an hour -----------------------
    def passed(self, ticker: str, headline: str, why: str, now: datetime | None = None) -> None:
        """Queue a pass. Nothing is sent here - flush_passes() sends the hourly digest."""
        now = (now or datetime.now(SYD)).astimezone(SYD)
        q = self._queue()
        q.append(
            {
                "at": now.isoformat(timespec="seconds"),
                "ticker": ticker,
                "headline": one_line(headline),
                "why": one_line(why),
            }
        )
        self._write_queue(q)
        log.info("pass queued for the digest: %s - %s", ticker, one_line(why))

    def flush_passes(self, now: datetime | None = None, force: bool = False) -> bool:
        """Send the digest once the oldest queued pass is an hour old. `force` sends now."""
        now = (now or datetime.now(SYD)).astimezone(SYD)
        q = self._queue()
        if not q:
            return False
        oldest = datetime.fromisoformat(q[0]["at"])
        if not force and now - oldest < timedelta(minutes=self.digest_minutes):
            return False
        lines = [
            f"• <b>{escape(r['ticker'])}</b> {escape(r['headline'])} — "
            f"<i>{escape(r['why'])}</i> ({r['at'][11:16]})"
            for r in q
        ]
        head = (
            f"⚪ <b>PASSED: {len(q)} announcement{'s' if len(q) != 1 else ''}</b> "
            f"({oldest:%H:%M}–{now:%H:%M})"
        )
        self.send(head + "\n" + "\n".join(lines))
        self._write_queue([])
        return True

    def _queue(self) -> list[dict]:
        if not self.queue_path.exists():
            return []
        try:
            return json.loads(self.queue_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:  # noqa: BLE001
            log.warning("the pass digest queue was unreadable (%s); starting a new one", e)
            return []

    def _write_queue(self, rows: list[dict]) -> None:
        write_text_atomic(json.dumps(rows, indent=2), self.queue_path)

    # -- the broker's events ------------------------------------------------
    def filled(self, o, closed: bool, stop_now: float | None) -> None:
        kind = account_kind(o.account) or who(o.placed_by)
        is_stop = o.placed_by == "code" and o.reason.startswith("STOP")
        at = o.fill_minute[11:16] if o.fill_minute else "?"
        if o.side in ("sell", "cover"):
            result = f"result {o.realised:+,.2f} before fees (this leg's fee {o.commission:,.2f})"
            if is_stop:
                head = f"🛑 <b>{kind} STOP FIRED</b> {escape(o.ticker)}"
            else:
                head = f"🔵 <b>{kind} {'CLOSED' if closed else 'REDUCED'}</b> {escape(o.ticker)}"
            self.send(
                f"{head}\n{o.side.upper()} {o.filled_qty:,} @ {o.avg_price:.4f} ({at})\n"
                f"{result}\n<i>{escape(one_line(o.reason))}</i>"
            )
            return
        stop = f"{stop_now:.3f}" if stop_now is not None else "none"
        self.send(
            f"🟢 <b>{kind} FILLED</b> {o.side.upper()} {escape(o.ticker)}\n"
            f"{o.filled_qty:,} @ {o.avg_price:.4f} ({at})"
            f" = ${o.filled_qty * o.avg_price:,.0f}, stop {stop}, fee {o.commission:,.2f}\n"
            f"order {o.order_id}"
        )

    def expired(self, o) -> None:
        kind = account_kind(o.account) or who(o.placed_by)
        self.send(
            f"⌛ {kind} order {o.order_id} {o.side.upper()} {escape(o.ticker)} expired unfilled: "
            f"<i>{escape(one_line(o.message))}</i>"
        )


def build_notifier(cfg: Config) -> Notifier:
    """Alerts are on unless `arena.alerts.telegram: false` in config.yaml."""
    return Notifier(
        cfg,
        enabled=bool(cfg.get("arena.alerts.telegram", True)),
        digest_minutes=int(cfg.get("arena.alerts.pass_digest_minutes", DIGEST_MINUTES)),
    )


def get(arena) -> Notifier | None:
    """The arena's notifier, if it has one. Test doubles simply do not."""
    return getattr(getattr(arena, "broker", None), "notifier", None)
