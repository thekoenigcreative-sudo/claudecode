"""Scanner: a new price-sensitive announcement plus its live price/volume reaction becomes a
proposal. Proposals are records for the human; nothing here can place an order.

Live reaction rule (fixed 2026-09-22, mirrors the backtest with intraday data):
    move_rel   = (last / prev_close - 1) - (index_last / index_prev_close - 1)  >= X%
    vol_ok     = volume_today >= Y * avg_volume_20d * max(0.25, elapsed_session_fraction)
    eligible   = median dollar turnover over the last 20 sessions >= floor
Entry limit = last price + one slippage allowance, rounded to the ASX tick.
Stop        = limit * (1 - stop_loss_pct).  Size = equity / max_positions, whole shares.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from asxbot.announcements.model import Announcement
from asxbot.backtest.costs import CostModel
from asxbot.io import write_text_atomic
from asxbot.live.quotes import Quote, QuoteProvider
from asxbot.live.reaction import measure
from asxbot.log import EventLog, get_logger

log = get_logger("asxbot.live.scanner")
SYD = ZoneInfo("Australia/Sydney")
SESSION_OPEN = time(10, 0)
SESSION_CLOSE = time(16, 0)


def asx_tick(price: float) -> float:
    if price < 0.10:
        return 0.001
    if price < 2.00:
        return 0.005
    return 0.01


def round_to_tick(price: float, up: bool) -> float:
    tick = asx_tick(price)
    n = price / tick
    n = math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)
    return round(n * tick, 3)


def session_fraction(now: datetime) -> float:
    t = now.astimezone(SYD).time()
    if t <= SESSION_OPEN:
        return 0.0
    if t >= SESSION_CLOSE:
        return 1.0
    total = (SESSION_CLOSE.hour * 60 + SESSION_CLOSE.minute) - (SESSION_OPEN.hour * 60)
    done = (t.hour * 60 + t.minute) - (SESSION_OPEN.hour * 60)
    return done / total


@dataclass
class Proposal:
    id: str
    created_at: str
    ticker: str
    side: str
    entry_limit: float
    stop: float
    qty: int
    value_aud: float
    dollar_risk: float
    move_rel_pct: float
    vol_mult: float
    last: float
    prev_close: float
    announcement_headline: str
    announcement_type: str
    announcement_time: str
    announcement_link: str
    reasoning: str
    quote_source: str
    status: str = "pending"  # pending | approved | placed | rejected | expired
    order_id: str | None = None
    broker_status: str | None = None  # copied verbatim from the broker's OrderResult

    def to_dict(self) -> dict:
        return asdict(self)


class Scanner:
    def __init__(
        self,
        data_dir: Path,
        quotes: QuoteProvider,
        daily_lookup,  # callable(ticker) -> daily DataFrame or None
        gap_pct: float,
        vol_mult: float,
        turnover_floor: float,
        stop_pct: float,
        position_size_aud: float,
        costs: CostModel,
        universe: set[str],
    ):
        self.data_dir = Path(data_dir)
        self.dir = self.data_dir / "proposals"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.quotes = quotes
        self.daily_lookup = daily_lookup
        self.gap_pct = gap_pct
        self.vol_mult = vol_mult
        self.turnover_floor = turnover_floor
        self.stop_pct = stop_pct
        self.size = position_size_aud
        self.costs = costs
        self.universe = universe
        self.events = EventLog(data_dir)

    # -- proposals on disk -------------------------------------------------
    def _next_id(self, now: datetime) -> str:
        stamp = now.strftime("%Y%m%d")
        n = len(list(self.dir.glob(f"P-{stamp}-*.json"))) + 1
        return f"P-{stamp}-{n:03d}"

    def save(self, p: Proposal) -> Path:
        path = self.dir / f"{p.id}.json"
        write_text_atomic(json.dumps(p.to_dict(), indent=2), path)
        return path

    def load(self, pid: str) -> Proposal | None:
        path = self.dir / f"{pid}.json"
        if not path.exists():
            return None
        return Proposal(**json.loads(path.read_text(encoding="utf-8")))

    def list(self, status: str | None = None) -> list[Proposal]:
        out = []
        for f in sorted(self.dir.glob("P-*.json")):
            p = Proposal(**json.loads(f.read_text(encoding="utf-8")))
            if status is None or p.status == status:
                out.append(p)
        return out

    def already_proposed(self, ticker: str, ids_id: str) -> bool:
        return any(p.ticker == ticker and p.announcement_link.endswith(ids_id) for p in self.list())

    # -- the check ----------------------------------------------------------
    def evaluate(self, a: Announcement, now: datetime | None = None) -> tuple[Proposal | None, str]:
        """Returns (proposal or None, reason)."""
        now = now or datetime.now(SYD)
        if a.code not in self.universe:
            return None, "not in universe"
        if not a.price_sensitive:
            return None, "not price sensitive"
        daily = self.daily_lookup(a.code)
        q = self.quotes.quote(a.code)
        iq = self.quotes.index_quote()
        if q is None or iq is None:
            return None, "no quote"
        r = measure(q, iq, daily, now, session_fraction)
        if r is None:
            return None, "insufficient daily history"
        if r.median_turnover < self.turnover_floor:
            return None, (
                f"turnover {r.median_turnover:,.0f} below floor {self.turnover_floor:,.0f}"
            )
        ok, why = r.passes(self.gap_pct, self.vol_mult)
        if not ok:
            return None, why
        return self._build(a, q, r.move_rel_pct, r.vol_mult, r.median_turnover, now), "ok"

    def _build(
        self, a: Announcement, q: Quote, move_rel: float, vmult: float, adv: float, now: datetime
    ) -> Proposal:
        slip = self.costs.slippage_pct(self.size, adv)
        limit = round_to_tick(q.last * (1 + slip), up=True)
        stop = round_to_tick(limit * (1 - self.stop_pct / 100), up=False)
        qty = int(math.floor(self.size / limit))
        value = qty * limit
        risk = qty * (limit - stop)
        reasoning = (
            f"{a.code} released a price-sensitive announcement at {a.released_at:%H:%M} "
            f"('{a.headline}', type {a.type}). Since the previous close it is {move_rel:+.1f}% "
            f"versus the ASX 200, on {vmult:.1f}x its session-adjusted 20-day average volume. "
            f"Rule: gap >= {self.gap_pct}% and volume >= {self.vol_mult}x. Buy {qty} at limit "
            f"{limit:.3f} (last {q.last:.3f} + slippage allowance), stop {stop:.3f} "
            f"({self.stop_pct}% below), time exit after 10 sessions. Quote: {q.source}."
        )
        p = Proposal(
            id=self._next_id(now),
            created_at=now.isoformat(timespec="seconds"),
            ticker=a.code,
            side="buy",
            entry_limit=limit,
            stop=stop,
            qty=qty,
            value_aud=round(value, 2),
            dollar_risk=round(risk, 2),
            move_rel_pct=round(move_rel, 2),
            vol_mult=round(vmult, 2),
            last=q.last,
            prev_close=q.prev_close,
            announcement_headline=a.headline,
            announcement_type=a.type,
            announcement_time=a.released_at.isoformat(timespec="minutes"),
            announcement_link=a.pdf_url,
            reasoning=reasoning,
            quote_source=q.source,
        )
        self.save(p)
        self.events.append("proposals", p.to_dict())
        log.info("proposal %s: %s", p.id, reasoning)
        return p

    def scan(
        self, announcements: list[Announcement], now: datetime | None = None
    ) -> list[Proposal]:
        out = []
        for a in announcements:
            if not a.price_sensitive or a.code not in self.universe:
                continue
            if self.already_proposed(a.code, a.ids_id):
                continue
            p, why = self.evaluate(a, now)
            self.events.append(
                "signals",
                {"ticker": a.code, "ids_id": a.ids_id, "headline": a.headline, "result": why},
            )
            if p:
                out.append(p)
        return out
