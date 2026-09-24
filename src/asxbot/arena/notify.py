"""Instant Telegram alerts from the arena, sent as things happen.

Sent the moment it happens, one short message each:
  * a trade DECIDED - by the agent or the yardstick bot - and the order id the broker gave it;
  * a trade the hard limits REFUSED;
  * every FILL, with the fill price and the stop the position now carries;
  * every STOP that fires, every TARGET hit, and every position CLOSED, with the result.

Sent as ONE DIGEST AN HOUR, and only for an hour that had something in it:
  * each announcement the decider looked at and PASSED on, one line and its reason;
  * each order placed that hour (it was also alerted when it happened);
  * each SCREEN-OUT WORTH READING: one decided by today's market data rather than by the
    stock's standing facts - no live quote, no daily history, or no trades by 10:30 on an
    announcement that is not itself a halt notice (tradability.worth_reading). A coarse
    tick, a thin stock or a halt notice is the quiet majority and stays a count;
  * headed by a counts line - seen, screened (split by test), read, passed, traded.

A quiet hour sends nothing. Until 2026-09-24 it sent "nothing passed this hour", so a dead
watcher and a quiet one would not look alike; on 24 Sep that sent eight in a row after the
last thing happened. The watchdog (watchdog.py) and the log_silent self-check now say when
the watcher is down or deaf, which a missing digest never did (LEARNINGS 14).

Sent ONCE A DAY, at 16:10:
  * the end-of-session message: the day's totals, everything that reached the decider,
    and both account balances. Anything still queued for the digest goes just before it.

After the end-of-session message, no digest at all, including when the watcher stops: only
the instant alerts below (orders, fills, stops, refusals) and the self-checks. Until
2026-09-24 the hourly digest carried on after it (16:34, 17:35, 18:35 and 19:25 on 24 Sep).
What is queued after 16:10 waits for the next trading day's first digest.

The digest state - queued passes and screen-outs, when the last digest went out, whether
today's summary has been sent - lives in one small file, so all of it survives the watcher
restarting.

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

from asxbot.arena.tally import counts_for, orders_between
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
        self._queue("passes", ticker, headline, why, now)
        log.info("pass queued for the digest: %s - %s", ticker, one_line(why))

    def screened_out(
        self, ticker: str, headline: str, why: str, now: datetime | None = None
    ) -> None:
        """Queue a screen-out worth reading (the caller decides which). Sent with the digest."""
        self._queue("screened", ticker, headline, why, now)

    def _queue(self, key: str, ticker: str, headline: str, why: str, now) -> None:
        now = (now or datetime.now(SYD)).astimezone(SYD)
        state = self._state()
        state[key].append(
            {
                "at": now.isoformat(timespec="seconds"),
                "ticker": ticker,
                "headline": one_line(headline),
                "why": one_line(why),
            }
        )
        self._write_state(state)

    def flush_passes(self, now: datetime | None = None, force: bool = False) -> bool:
        """Send the digest on the hour, if the hour had anything in it.

        `force` sends the part-hour now rather than waiting for the hour; it still sends
        nothing for a quiet part-hour. After today's end-of-session message nothing is sent
        at all, forced or not, and the queue waits for the next trading day.
        """
        now = (now or datetime.now(SYD)).astimezone(SYD)
        state = self._state()
        if state.get("summary_day") == now.date().isoformat():
            return False  # the session is summarised; only real events from here
        last = state.get("last_digest_at")
        if last is None and not force:  # first cycle: start the clock, say nothing yet
            state["last_digest_at"] = now.isoformat(timespec="seconds")
            self._write_state(state)
            return False
        since = datetime.fromisoformat(last).astimezone(SYD) if last else now
        if not force and now - since < timedelta(minutes=self.digest_minutes):
            return False

        passes, screened = state["passes"], state["screened"]
        orders = orders_between(self.cfg.data_dir, since, now)
        if not (passes or screened or orders):
            # A quiet hour: the clock moves on, nothing is sent.
            state["last_digest_at"] = now.isoformat(timespec="seconds")
            self._write_state(state)
            return False

        day = now.date()
        counts = counts_for(self.cfg.data_dir, day)
        span = f"{since:%H:%M}" if since.date() == day else f"{since:%a %H:%M}"
        parts = [f"🕐 <b>{span}–{now:%H:%M}</b>\n{escape(counts.line())}"]
        if orders:
            body = "\n".join(
                f"• {who(str(o.get('placed_by', '')))} {str(o.get('side', '')).upper()} "
                f"<b>{escape(str(o.get('ticker', '?')))}</b> {int(o.get('qty') or 0):,} "
                f"({o['syd']:%H:%M})"
                for o in orders
            )
            parts.append(f"🟡 <b>orders this hour ({len(orders)})</b>\n{body}")
        if passes:
            parts.append(f"⚪ <b>passed this hour ({len(passes)})</b>\n{_rows(passes, day)}")
        if screened:
            parts.append(
                f"🚧 <b>screened out, worth a look ({len(screened)})</b>\n{_rows(screened, day)}"
            )
        self.send("\n\n".join(parts))
        # Everything else in the state stays. Until 2026-09-24 this wrote a fresh state with
        # only these two keys, dropping `summary_day`, so every hourly digest after 16:10
        # re-armed the end-of-session summary: on 23 Sep it went out at 16:10, 16:56, 17:56
        # and 18:57.
        state.update(passes=[], screened=[], last_digest_at=now.isoformat(timespec="seconds"))
        self._write_state(state)
        return True

    def session_summary(self, text: str, now: datetime | None = None) -> bool:
        """The 16:10 end-of-session message. Sent once a trading day, however many times the
        watcher restarts or the digest runs; the caller builds the text. A send that fails
        (Telegram down) is not counted, so the next cycle tries again.

        Whatever is still queued for the digest goes first, as a part-hour digest: after
        this message no digest is sent today."""
        now = (now or datetime.now(SYD)).astimezone(SYD)
        state = self._state()
        if state.get("summary_day") == now.date().isoformat():
            return False
        self.flush_passes(now, force=True)
        if not self.send(text) and self.enabled:
            return False
        state = self._state()  # re-read: nothing else may be lost by this write
        state["summary_day"] = now.date().isoformat()
        self._write_state(state)
        return True

    def _state(self) -> dict:
        blank: dict = {"passes": [], "screened": [], "last_digest_at": None, "summary_day": ""}
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
        if o.fills and o.fills[0].get("auction"):
            at = "opening auction"  # stamped 09:59 (TRACKER #28)
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


def _rows(rows: list[dict], day) -> str:
    """One line per queued item; one from an earlier day carries its day."""
    out = []
    for r in rows:
        at = datetime.fromisoformat(r["at"]).astimezone(SYD)
        when = f"{at:%H:%M}" if at.date() == day else f"{at:%a %H:%M}"
        out.append(
            f"• <b>{escape(r['ticker'])}</b> {escape(r['headline'])} — "
            f"<i>{escape(r['why'])}</i> ({when})"
        )
    return "\n".join(out)


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
