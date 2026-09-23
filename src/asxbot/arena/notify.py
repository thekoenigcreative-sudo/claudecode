"""Instant Telegram alerts from the arena, sent as things happen.

Sent the moment it happens, one short message each:
  * a trade DECIDED - by the agent or the yardstick bot - and the order id the broker gave it;
  * a trade the hard limits REFUSED;
  * every FILL, with the fill price and the stop the position now carries;
  * every STOP that fires, every TARGET hit, and every position CLOSED, with the result.

Sent as ONE DIGEST PER HOUR, every hour the watcher is up, whether or not anything passed:
  * a counts line - seen, screened (split by test), read, passed, traded;
  * each announcement the decider looked at and PASSED on, one line and its reason.

A quiet hour still sends, because "nothing passed this hour" and "the watcher died at
11:04" must never look the same on a phone.

Sent ONCE A DAY, at 16:10:
  * the end-of-session message: the day's totals, everything that reached the decider,
    and both account balances.

The digest state - queued passes, when the last digest went out, whether today's summary
has been sent - lives in one small file, so all of it survives the watcher restarting.

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

from asxbot.arena.tally import counts_for
from asxbot.config import Config
from asxbot.io import write_text_atomic
from asxbot.log import EventLog, get_logger

log = get_logger("asxbot.arena.notify")
SYD = ZoneInfo("Australia/Sydney")
DIGEST_MINUTES = 60


def one_line(text: str) -> str:
    """Text flattened onto one line, never trimmed: reasons and headlines go in full."""
    return " ".join(str(text or "").split())


def escape_text(text: str) -> str:
    """Escape for Telegram's HTML parse mode. Detail lines carry file names and errors."""
    return escape(one_line(text))


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

    # -- the hourly digest ----------------------------------------------------
    def passed(self, ticker: str, headline: str, why: str, now: datetime | None = None) -> None:
        """Queue a pass. Nothing is sent here - the digest goes out on the hour."""
        now = (now or datetime.now(SYD)).astimezone(SYD)
        state = self._state()
        state["passes"].append(
            {
                "at": now.isoformat(timespec="seconds"),
                "ticker": ticker,
                "headline": one_line(headline),
                "why": one_line(why),
            }
        )
        self._write_state(state)
        log.info("pass queued for the digest: %s - %s", ticker, one_line(why))

    def flush_passes(self, now: datetime | None = None, force: bool = False) -> bool:
        """Send the digest every hour the watcher is up, whether or not anything passed.

        A quiet hour is worth saying out loud: it is the difference between "nothing
        happened" and "the watcher died at 11:04 and nobody noticed".
        """
        now = (now or datetime.now(SYD)).astimezone(SYD)
        state = self._state()
        last = state.get("last_digest_at")
        if last is None and not force:  # first cycle: start the clock, say nothing yet
            state["last_digest_at"] = now.isoformat(timespec="seconds")
            self._write_state(state)
            return False
        since = datetime.fromisoformat(last) if last else now
        if not force and now - since < timedelta(minutes=self.digest_minutes):
            return False

        rows = state["passes"]
        counts = counts_for(self.cfg.data_dir, now.date())
        head = f"🕐 <b>{since:%H:%M}–{now:%H:%M}</b>\n{escape(counts.line())}"
        if rows:
            body = "\n".join(
                f"• <b>{escape(r['ticker'])}</b> {escape(r['headline'])} — "
                f"<i>{escape(r['why'])}</i> ({r['at'][11:16]})"
                for r in rows
            )
            body = f"\n\n⚪ <b>passed this hour ({len(rows)})</b>\n{body}"
        else:
            body = "\n\n<i>nothing passed this hour</i>"
        self.send(head + body)
        self._write_state({"passes": [], "last_digest_at": now.isoformat(timespec="seconds")})
        return True

    def session_summary(self, text: str, now: datetime | None = None) -> bool:
        """The 16:10 end-of-session message. Sent once a day; the caller builds the text."""
        now = (now or datetime.now(SYD)).astimezone(SYD)
        state = self._state()
        if state.get("summary_day") == now.date().isoformat():
            return False
        self.send(text)
        state["summary_day"] = now.date().isoformat()
        self._write_state(state)
        return True

    def _state(self) -> dict:
        blank: dict = {"passes": [], "last_digest_at": None, "summary_day": ""}
        if not self.queue_path.exists():
            return blank
        try:
            raw = json.loads(self.queue_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:  # noqa: BLE001
            log.warning("the digest state was unreadable (%s); starting a new one", e)
            return blank
        if isinstance(raw, list):  # the pre-23-Sep shape: a bare list of passes
            return {**blank, "passes": raw}
        return {**blank, **raw}

    def _write_state(self, state: dict) -> None:
        write_text_atomic(json.dumps(state, indent=2), self.queue_path)

    # -- the broker's events ------------------------------------------------
    def filled(self, o, closed: bool, stop_now: float | None) -> None:
        kind = account_kind(o.account) or who(o.placed_by)
        is_stop = o.placed_by == "code" and o.reason.startswith("STOP")
        is_target = o.placed_by == "code" and o.reason.startswith("TARGET")
        at = o.fill_minute[11:16] if o.fill_minute else "?"
        if o.side in ("sell", "cover"):
            result = f"result {o.realised:+,.2f} before fees (this leg's fee {o.commission:,.2f})"
            if is_stop:
                head = f"🛑 <b>{kind} STOP FIRED</b> {escape(o.ticker)}"
            elif is_target:
                head = f"🎯 <b>{kind} TARGET HIT</b> {escape(o.ticker)}"
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
