"""THE REPLAY (25 Sep 2026): the frozen announcements-v2 and day-trader RULE BOTS run over
months of IBKR 1-minute history through the live code - the same scanner, the same order
limits, the same broker (costs, slippage, volume-capped fills, stops, trailing, the flat
sweep) - one scratch book per day on a simulated clock, and a scorecard per playbook.

REPLAY, NOT LIVE, NOT A VERDICT ON THE AGENT. What is different from the arena:
  * the agent is never asked (a model may know what happened, and CLAUDE.md forbids AI
    classification of past announcements): rule bots only;
  * every decision sees the bars a live IBKR feed would have held at that minute: the
    clock steps to 20 s past each minute, when the live feed has closed the minute before
    (26 Sep 2026; it was 2 minutes behind), with every stock watched every minute - the live
    rotation streams ~50 and polls the rest every few minutes, so live catches at most what
    this catches;
  * each day starts a fresh $20,000 book, so a day's sizing never depends on the day before;
    the report chains the daily results into one curve;
  * the ASX 300 list and the ASX 200 short list are today's, applied to every past day
    (survivorship: the stocks that went on to be big), and the daily prices behind the
    turnover screens are cut off at the day being replayed. v2 screens news in the arena's
    whole universe (the ASX 300 and the small-cap list), as live; but the announcement
    archive holds the ASX 300's only, so small caps' news is in the replay only for the
    live collector's days (from 22 Sep 2026) - v2 is understated before then;
  * announcements come from the ASX archive (data/announcements/history), with the live
    collector's files for the days it ran; the opening auction is not modelled (no daily
    open is fetched for past days), so nothing fills in the auction - neither playbook
    places an order before the open anyway.
Data gaps (days the index or many stocks have no bars) are named in the report.

    asxbot arena replay-ibkr --from 2026-03-26 --to 2026-09-25 [--workers 8]
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as time_cls
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from asxbot.arena import daytrader, v2_flow
from asxbot.arena.broker import ArenaBroker
from asxbot.arena.intraday import MarketView, ReplayFeed, continuous, prior_sessions
from asxbot.arena.liquid import SizeRule, liquid_universe, size_rule
from asxbot.arena.minutes import MinuteBars
from asxbot.arena.reaction_v2 import load_bot_state
from asxbot.arena.runtime import Arena
from asxbot.backtest.costs import CostModel
from asxbot.config import Config
from asxbot.io import safe_stem, write_parquet_atomic, write_text_atomic
from asxbot.log import get_logger

log = get_logger("asxbot.arena.replay_ibkr")
SYD = ZoneInfo("Australia/Sydney")
LABEL = ("REPLAY - not live, not a verdict on the agent. IBKR 1-minute bars, rule bots only, "
         "today's index lists applied to every day.")  # fmt: skip
START_CASH = 20_000.0
MIN_STOCKS_WITH_BARS = 100  # fewer on a day and the day is a data gap, not a quiet day


def history_root() -> Path:
    """%LOCALAPPDATA%\\asx-bot\\ibkr\\history; ASXBOT_IBKR_HISTORY points elsewhere (a cloud
    session's unzipped copy on Linux)."""
    import os

    if os.environ.get("ASXBOT_IBKR_HISTORY"):
        return Path(os.environ["ASXBOT_IBKR_HISTORY"])
    from asxbot.localdir import asx_local

    return asx_local() / "ibkr" / "history"


class HistoryBars(MinuteBars):
    """The IBKR history cache, read-only: <root>/<code>/<day>.parquet (the caret kept for the
    index). No request, no write, no opening auction."""

    source_label = "IBKR history minute"  # what the broker records as each fill's feed

    def __init__(self, root: Path, price_field: str = "close"):
        self.root = Path(root)
        self.dir = self.root
        if price_field not in ("close", "open", "typical"):
            raise ValueError(f"minute_price must be close|open|typical, got {price_field!r}")
        self.price_field = price_field
        self._warned = set()
        self.live = None
        self._live_days = set()
        self._memo: dict = {}

    def _path(self, code: str, day: date) -> Path:
        # PRN (a Windows device name) is cached as PRN_, as every cache here names it.
        return self.root / safe_stem(code.upper()) / f"{day.isoformat()}.parquet"

    def cached(self, code: str, day: date) -> pd.DataFrame | None:
        key = (code.upper(), day)
        if key in self._memo:
            return self._memo[key]
        p = self._path(code, day)
        df = None
        if p.exists():
            df = pd.read_parquet(p)
            if df.index.tz is None:
                df.index = df.index.tz_localize(SYD)
        self._memo[key] = df
        return df

    def fetch(self, code: str, day: date, force: bool = False) -> pd.DataFrame | None:
        return self.cached(code, day)

    def fetch_daily_row(self, code: str, day: date) -> dict | None:
        return None


def sessions(first: date, last: date) -> list[date]:
    import exchange_calendars as xc

    cal = xc.get_calendar("XASX")
    return [t.date() for t in cal.sessions_in_range(pd.Timestamp(first), pd.Timestamp(last))]


# --------------------------------------------------------------------------
# announcements: the archive, plus the live collector's files
# --------------------------------------------------------------------------
COLS = ["code", "released_at", "release_date", "headline", "type", "price_sensitive",
        "pre_open", "ids_id", "pdf_url", "pages", "size"]  # fmt: skip


def announcements(cfg: Config, first: date, last: date) -> pd.DataFrame:
    """Every announcement released between `first` and `last`, from the archive and the
    live files, one row per id."""
    frames = []
    hist = cfg.data_dir / "announcements" / "history"
    lo, hi = pd.Timestamp(first), pd.Timestamp(last) + pd.Timedelta(days=1)
    for p in sorted(hist.glob("*.parquet")) if hist.exists() else []:
        if p.name.startswith("_"):
            continue
        try:
            df = pd.read_parquet(p)
        except Exception:  # noqa: BLE001
            continue
        df = df[(df["released_at"] >= lo) & (df["released_at"] < hi)]
        if len(df):
            frames.append(df)
    live = cfg.data_dir / "announcements" / "live"
    for p in sorted(live.glob("*.parquet")) if live.exists() else []:
        try:
            d = date.fromisoformat(p.stem)
        except ValueError:
            continue
        if first <= d <= last:
            frames.append(pd.read_parquet(p))
    if not frames:
        return pd.DataFrame(columns=COLS)
    df = pd.concat(frames, ignore_index=True)
    df["ids_id"] = df["ids_id"].astype(str)
    df = df.drop_duplicates("ids_id").sort_values("released_at").reset_index(drop=True)
    for c in COLS:
        if c not in df.columns:
            df[c] = None
    return df[COLS]


# --------------------------------------------------------------------------
# the scratch arena for one day
# --------------------------------------------------------------------------
class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@dataclass
class DayArena(Arena):
    """Books and events in a scratch folder, a simulated clock, and daily prices cut off at
    the replayed day so no screen sees the future."""

    real_cfg: Config | None = None
    day: date | None = None

    def daily_lookup(self):
        return self._daily

    def _daily(self, ticker: str):
        return _daily_until(self.real_cfg, ticker, self.day)


_STORE: dict = {}


def _daily_until(cfg: Config, ticker: str, day: date):
    from asxbot.data.factory import get_store

    key = str(cfg.data_dir)
    store = _STORE.get(key)
    if store is None:
        store = _STORE[key] = get_store(cfg)
    try:
        d = store.load(ticker)
    except Exception:  # noqa: BLE001
        return None
    if d is None or not len(d):
        return None
    return d[d.index < pd.Timestamp(day)]


def day_universe(cfg: Config, pb, codes: list[str], day: date, hist: HistoryBars) -> list[str]:
    """The day trader's liquid universe as of `day`: the size-aware rule and the tick limit
    on daily prices up to the day before, and only stocks with bars that day."""
    conf = pb.raw.get("universe") or {}
    rule = size_rule(pb) or SizeRule(0.05, 20)
    order = pb.level.max_position_aud or 5000.0
    with_bars = [c for c in codes if hist._path(c, day).exists()]
    ok, _why = liquid_universe(with_bars, lambda c: _daily_until(cfg, c, day), rule, order,
                               float(conf.get("max_tick_pct", 1.0)))  # fmt: skip
    return ok


def scratch_arena(cfg: Config, scratch: Path, clock: Clock, day: date, universe: set[str],
                  shorts: set[str], hist: HistoryBars) -> DayArena:  # fmt: skip
    from asxbot.live.quotes import median_turnover_20d

    raw = dict(cfg.raw)
    raw["data"] = {**raw.get("data", {}), "dir": str(scratch), "live_provider": "yfinance"}
    scfg = Config(raw=raw, root=cfg.root, env=cfg.env)

    def adv(t):
        d = _daily_until(cfg, t, day)
        return (None if d is None or len(d) < 20
                else float((d["close"] * d["volume"]).tail(20).mean()))  # fmt: skip

    fill = cfg.get("arena.fill") or {}
    broker = ArenaBroker(
        scratch, CostModel.from_config(cfg), hist, adv,
        short_borrow_pct_annual=float(cfg.get("arena.costs.short_borrow_pct_annual", 3.0)),
        resolve_after_minutes=0, clock=clock,
        max_volume_share=float(fill.get("max_volume_share", 0.20)),
        settle_minutes=0, opening_auction="first_minute",
    )  # fmt: skip
    broker.median_turnover = lambda t: median_turnover_20d(_daily_until(cfg, t, day))
    return DayArena(scfg, broker, broker.store, universe, shorts, real_cfg=cfg, day=day)


# --------------------------------------------------------------------------
# one day
# --------------------------------------------------------------------------
def replay_day(cfg: Config, day: date, codes: list[str], shorts: set[str], ann: pd.DataFrame,
               hist: HistoryBars | None = None, keep: Path | None = None,
               universe: set[str] | None = None, *, v2pb=None, dtpb=None,
               agent=None) -> dict:  # fmt: skip
    """Both rule bots through one session. Returns the day's record (books, trades, gaps).

    The Practice Lab (26 Sep 2026, PRACTICE_LAB.md) passes a VARIANT's playbooks (`v2pb`,
    `dtpb`: the frozen blocks with its changes) and, for the day trader's agent book, `agent`:
    a stand-in for `daytrader.ask_agent` (the lab's own cached model call, never OpenClaw).
    Without them this is the frozen rule-bot replay, unchanged."""
    from asxbot.arena.levels import load_playbook

    hist = hist or HistoryBars(history_root(), str((cfg.get("arena.fill") or {}).get(
        "minute_price", "close")))  # fmt: skip
    index = str(cfg.get("backtest.index_ticker", "^AXJO"))
    v2pb = v2pb or load_playbook(cfg, "asx_announcements_v2")
    dtpb = dtpb or load_playbook(cfg, "asx_daytrader")
    prev = prior_sessions(day, 1)
    prev_day = prev[0] if prev else day - timedelta(days=1)
    out: dict = {"day": day.isoformat(), "gaps": []}
    idx = hist.cached(index, day)
    if idx is None or len(idx) < 300:
        out["gaps"].append(f"index bars missing or short ({0 if idx is None else len(idx)})")
    dt_codes = day_universe(cfg, dtpb, codes, day, hist)
    with_bars = sum(1 for c in codes if hist._path(c, day).exists())
    if with_bars < min(MIN_STOCKS_WITH_BARS, max(1, len(codes) // 2)):
        out["gaps"].append(f"only {with_bars} of {len(codes)} stocks have bars")
    out["stocks_with_bars"] = with_bars
    out["dt_universe"] = len(dt_codes)
    out["market_move_pct"] = _index_move(hist, index, day, prev_day)
    if out["gaps"]:
        return out

    scratch = Path(tempfile.mkdtemp(prefix=f"replay_{day:%m%d}_"))
    real_ask = daytrader.ask_agent
    if agent is not None:
        daytrader.ask_agent = agent
    try:
        live = scratch / "announcements" / "live"
        live.mkdir(parents=True)
        for d in (prev_day, day):
            part = ann[ann["release_date"] == d.isoformat()] if len(ann) else ann
            if len(part):
                write_parquet_atomic(part.reset_index(drop=True), live / f"{d.isoformat()}.parquet")
        clock = Clock(datetime.combine(day, time_cls(9, 50), tzinfo=SYD))
        arena = scratch_arena(cfg, scratch, clock, day, set(universe or codes), shorts, hist)
        view = MarketView(hist, day, ReplayFeed(hist, None), index, 5, 3)
        st = daytrader.load_state(arena.cfg.data_dir, day)
        st["universe"] = dt_codes
        daytrader.save_state(arena.cfg.data_dir, day, st)
        daytrader._DAY.clear()
        daytrader._PRE.clear()
        since = pd.Timestamp(datetime.combine(prev_day, time_cls(16, 10)))
        until = pd.Timestamp(datetime.combine(day, time_cls(16, 10)))
        day_ann = ann[(ann["released_at"] >= since) & (ann["released_at"] < until)]

        def visible_price(t: str):
            b = continuous(view.bars(t, clock.t))
            return None if not len(b) else float(b["close"].iloc[-1])

        t = datetime.combine(day, time_cls(10, 0, 20), tzinfo=SYD)
        end = datetime.combine(day, time_cls(16, 30), tzinfo=SYD)
        books = [(v2pb, "bot"), (dtpb, "bot")] + ([(dtpb, "agent")] if agent is not None else [])
        while t <= end:
            clock.t = t
            v2_flow.v2_bot_cycle(arena, v2pb, view, t, ann=day_ann)
            daytrader.cycle(arena, dtpb, view, t, use_agent=agent is not None, refresh=False)
            for pb, kind in books:
                arena.broker.work(arena.account(pb, kind), t)
            for pb in (v2pb, dtpb):
                v2_flow.flatten(arena, pb, t, price_of=visible_price)
            t += timedelta(minutes=1)
        clock.t = datetime.combine(day, time_cls(19, 0), tzinfo=SYD)
        for pb, kind in books:
            arena.broker.work(arena.account(pb, kind), clock.t)
        out["v2"] = _book(arena, v2pb, day, hist)
        sens = day_ann["price_sensitive"].fillna(False).astype(bool) if len(day_ann) else []
        out["v2"]["news_candidates"] = int(sum(sens)) if len(day_ann) else 0
        out["v2"]["state"] = {k: v for k, v in load_bot_state(arena.cfg.data_dir, day).items()
                              if k in ("status", "why", "decided_at")}  # fmt: skip
        out["v2"]["candidates"] = len(load_bot_state(arena.cfg.data_dir, day).get("candidates", []))
        out["daytrader"] = _book(arena, dtpb, day, hist)
        if agent is not None:
            out["daytrader"]["agent_book"] = _book(arena, dtpb, day, hist, kind="agent")
        sig = daytrader.load_state(arena.cfg.data_dir, day).get("signals", [])
        by_setup = pd.Series([s["setup"] for s in sig]).value_counts().to_dict() if sig else {}
        out["daytrader"]["setups"] = {
            "found": len(sig),
            "by_setup": by_setup,
            "stale": sum(1 for s in sig if str(s.get("skipped", "")).startswith("stale")),
            "uneconomic": sum(1 for s in sig if _uneconomic(s)),
            "bot_orders": sum(1 for s in sig if (s.get("bot") or {}).get("order_id")),
        }  # fmt: skip
        return out
    finally:
        daytrader.ask_agent = real_ask
        if keep is not None:
            shutil.copytree(scratch, keep / scratch.name, dirs_exist_ok=True)
        shutil.rmtree(scratch, ignore_errors=True)


def _uneconomic(sig: dict) -> bool:
    return str((sig.get("bot") or {}).get("skipped", "")).startswith("uneconomic")


def _index_move(hist: HistoryBars, index: str, day: date, prev_day: date) -> float | None:
    a, b = hist.cached(index, prev_day), hist.cached(index, day)
    if a is None or b is None or not len(a) or not len(b):
        return None
    a, b = a[a["close"] > 0], b[b["close"] > 0]
    if not len(a) or not len(b):
        return None
    return round((float(b["close"].iloc[-1]) / float(a["close"].iloc[-1]) - 1) * 100, 3)


def _book(arena, pb, day: date, hist: HistoryBars, kind: str = "bot") -> dict:
    """The bot's (or, in the Practice Lab, the agent's) book after the day: its equity and
    every trade (an opening fill and the exits that closed it), with fees, the initial risk
    and the R multiple."""
    acct = arena.account(pb, kind)
    prices = arena.broker.prices(acct, day)
    orders = sorted(acct.orders.values(), key=lambda o: o.order_id)
    trades = []
    for o in orders:
        if o.side not in ("buy", "short") or not o.filled_qty:
            continue
        entry = float(o.avg_price or o.limit)
        stop = float(o.stop) if o.stop is not None else None
        risk = abs(entry - stop) * o.filled_qty if stop is not None else None
        exits = [x for x in orders if x.ticker == o.ticker and x.side in ("sell", "cover")
                 and x.filled_qty and x.decided_at > o.decided_at]  # fmt: skip
        later_entries = [x for x in orders if x.ticker == o.ticker and x.side in ("buy", "short")
                         and x.decided_at > o.decided_at and x.filled_qty]  # fmt: skip
        if later_entries:
            cut = later_entries[0].decided_at
            exits = [x for x in exits if x.decided_at <= cut]
        gross = sum(float(x.realised) for x in exits)
        fees = float(o.commission) + sum(float(x.commission) for x in exits)
        net = gross - fees
        trades.append({
            "ticker": o.ticker, "side": o.side, "qty": o.filled_qty, "entry": round(entry, 4),
            "stop": stop, "risk": None if risk is None else round(risk, 2),
            "decided": o.decided_at[11:19], "first_fill": (o.fill_minute or "")[11:16],
            "gross": round(gross, 2), "fees": round(fees, 2), "net": round(net, 2),
            "r": None if not risk else round(net / risk, 3),
            "exits": [f"{x.order_type}:{x.reason[:24]}" for x in exits],
            "reason": o.reason[:120],
        })  # fmt: skip
    equity = acct.equity(prices)
    open_left = {t: p.qty for t, p in acct.positions.items() if p.qty}
    return {
        "start": acct.starting_cash, "equity": round(equity, 2),
        "pnl": round(equity - acct.starting_cash, 2), "fees": round(acct.fees_paid, 2),
        "realised": round(acct.realised_pnl, 2),
        # Positions still open when the day's work is done (19:00): the flat-by-close rule
        # could not be met - an exit the bars' volume could not absorb (the 22 Sep plumbing
        # replay left AVM and AON mostly unsold). An engine issue: counted and flagged.
        "open_positions": open_left,
        "stuck_at_close": [{"ticker": tk, "qty": q, "value": round(abs(q) * float(
            prices.get(tk) or 0.0), 2)} for tk, q in open_left.items()],  # fmt: skip
        "orders": len(acct.orders), "trades": trades,
    }  # fmt: skip


# --------------------------------------------------------------------------
# the scorecard
# --------------------------------------------------------------------------
def scorecard(days: list[dict], key: str) -> dict:
    """Per playbook, over the replayed days: trades, win rate, average R, P&L after fees,
    green/red days, worst drawdown on the chained curve, the top-3 trades' share, and up
    against down market days."""
    played = [d for d in days if key in d]
    trades = [t for d in played for t in d[key]["trades"]]
    wins = [t for t in trades if t["net"] > 0]
    rs = [t["r"] for t in trades if t.get("r") is not None]
    daily = [(d["day"], float(d[key]["pnl"]), d.get("market_move_pct")) for d in played]
    green = sum(1 for _, p, _ in daily if p > 0.005)
    red = sum(1 for _, p, _ in daily if p < -0.005)
    flat = len(daily) - green - red
    curve, eq, peak, dd = [], START_CASH, START_CASH, 0.0
    for _, p, _ in daily:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100 if peak else 0.0)
        curve.append(round(eq, 2))
    total = sum(t["net"] for t in trades)
    top3 = sorted((t["net"] for t in trades), reverse=True)[:3]
    top3_share = None if total <= 0 else round(sum(x for x in top3 if x > 0) / total * 100, 1)
    up = [p for _, p, m in daily if m is not None and m > 0]
    down = [p for _, p, m in daily if m is not None and m <= 0]
    worst_day = min((p for _, p, _ in daily), default=0.0)
    stuck = [(d["day"], s) for d in played for s in (d[key].get("stuck_at_close") or [])]
    return {
        "days": len(daily), "trades": len(trades), "wins": len(wins),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        "pnl_after_fees": round(sum(p for _, p, _ in daily), 2),
        "fees": round(sum(float(d[key]["fees"]) for d in played), 2),
        "green_days": green, "red_days": red, "flat_days": flat,
        "worst_day": round(worst_day, 2),
        "max_drawdown_pct": round(dd, 2), "end_equity": curve[-1] if curve else START_CASH,
        "top3_share_pct": top3_share,
        "top3": [round(x, 2) for x in top3],
        "up_days": {"n": len(up), "pnl": round(sum(up), 2)},
        "down_days": {"n": len(down), "pnl": round(sum(down), 2)},
        "avg_trade": round(total / len(trades), 2) if trades else None,
        "stuck_at_close": len(stuck),
        "stuck_examples": [f"{d} {s['ticker']} {s['qty']} (${s['value']:,.0f})"
                           for d, s in stuck[:10]],  # fmt: skip
        "curve": curve,
    }


def verdict(s: dict) -> tuple[str, list[str]]:
    """A plain verdict for one playbook's scorecard: promising / unclear / not working.
    Fixed before any result was read (26 Sep 2026): at least 30 trades to say anything;
    losing after costs is "not working"; winning needs more green days than red, a top-3
    share under 60% and a worst drawdown under 25% (the ladder's own bar, ARENA.md) to be
    "promising"; anything else is "unclear". A replay earns a hypothesis, never a promotion."""
    why = []
    if s["trades"] < 30:
        why.append(f"only {s['trades']} trades (fewer than 30)")
        if s["trades"] and s["pnl_after_fees"] <= 0:
            why.append("and those lost after costs")
        return "unclear", why
    if s["pnl_after_fees"] <= 0:
        why.append(f"loses after costs ({s['pnl_after_fees']:+,.0f} over {s['days']} days)")
        return "not working", why
    good = True
    if s["green_days"] <= s["red_days"]:
        good = False
        why.append(f"{s['green_days']} green days against {s['red_days']} red")
    if s["top3_share_pct"] is not None and s["top3_share_pct"] > 60:
        good = False
        why.append(f"{s['top3_share_pct']:.0f}% of the profit is three trades")
    if s["max_drawdown_pct"] >= 25:
        good = False
        why.append(f"worst drawdown {s['max_drawdown_pct']:.1f}%")
    if good:
        why.append(f"makes {s['pnl_after_fees']:+,.0f} after costs on {s['trades']} trades, "
                   f"{s['green_days']} green / {s['red_days']} red days")  # fmt: skip
        return "promising", why
    return "unclear", why


def render(results: list[dict], first: date, last: date, n_codes: int, meta: dict) -> str:
    played = [r for r in results if not r.get("gaps")]
    gaps = [r for r in results if r.get("gaps")]
    v2, dt = scorecard(played, "v2"), scorecard(played, "daytrader")
    lines = [
        f"# Replay: announcements v2 and day-trader rule bots, {first} to {last}",
        "",
        f"*{LABEL}*",
        "",
        f"Generated {datetime.now(SYD):%Y-%m-%d %H:%M} Sydney. {len(results)} sessions, "
        f"{len(played)} replayed, {len(gaps)} with data gaps. Universe: today's ASX 300 list "
        f"({n_codes} codes) with bars that day; the day trader's liquid subset averaged "
        f"{meta.get('avg_dt_universe', 0):.0f} stocks. Rules: config.yaml `asx_announcements_v2` "
        "and `asx_daytrader` as frozen 2026-09-24, with the 25 Sep corrections (the first "
        "close beyond the range is the breakout; uneconomic setups filtered). Costs: IBKR "
        "brokerage both ways ($6.60 minimum), the arena's slippage model, fills capped at 20% "
        "of a bar's volume. Each day starts a fresh $20,000 book; the curve chains the days.",
        "",
        "| Playbook (rule bot) | Trades | Win rate | Avg R | P&L after fees | Fees | "
        "Green/red/flat days | Worst day | Worst drawdown | Top-3 trades' share | "
        "Up-market days | Down-market days |",
        "|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---|---|",
    ]
    for name, s in (("announcements v2 (10:30 rule)", v2), ("day trader v1", dt)):
        wr = "-" if s["win_rate_pct"] is None else f"{s['win_rate_pct']:.0f}%"
        ar = "-" if s["avg_r"] is None else f"{s['avg_r']:+.2f}"
        t3 = "-" if s["top3_share_pct"] is None else f"{s['top3_share_pct']:.0f}%"
        lines.append(
            f"| {name} | {s['trades']} | {wr} | {ar} | {s['pnl_after_fees']:+,.2f} | "
            f"{s['fees']:,.2f} | {s['green_days']}/{s['red_days']}/{s['flat_days']} | "
            f"{s['worst_day']:+,.2f} | {s['max_drawdown_pct']:.2f}% | {t3} | "
            f"{s['up_days']['n']} days {s['up_days']['pnl']:+,.2f} | "
            f"{s['down_days']['n']} days {s['down_days']['pnl']:+,.2f} |"
        )
    lines += ["", "## Verdict per playbook (rules fixed before the run: `verdict`)", ""]
    for name, s in (("announcements v2 (10:30 rule bot)", v2), ("day trader v1 (rule bot)", dt)):
        word, why = verdict(s)
        lines.append(f"- **{name}: {word}** - {'; '.join(why)}.")
    stuck_lines = []
    for name, s in (("v2", v2), ("day trader", dt)):
        if s["stuck_at_close"]:
            stuck_lines.append(
                f"- {name}: {s['stuck_at_close']} position(s) still open after the close - the "
                f"bars' volume could not absorb the flat-by-close exit (20% of each bar): "
                f"{', '.join(s['stuck_examples'])}. An ENGINE issue: the entry screen admits "
                "a size the exit liquidity cannot take by 15:50; a rule question for Rick "
                "(recommend, not changed)."
            )  # fmt: skip
    if stuck_lines:
        lines += ["", "## Stuck at the close", "", *stuck_lines]
    lines += ["", "## What it means, and what it does not", ""]
    for name, s in (("v2", v2), ("day trader", dt)):
        if s["trades"] and s["top3_share_pct"] is not None and s["top3_share_pct"] > 60:
            lines.append(f"- {name}: more than half the profit is three trades "
                         f"({s['top3']}); not a result to lean on.")  # fmt: skip
        if s["trades"] < 30:
            lines.append(f"- {name}: {s['trades']} trades is a small sample (config "
                         "backtest.small_sample_trades is 30).")  # fmt: skip
        if s["trades"] and s["pnl_after_fees"] <= 0:
            lines.append(f"- {name}: loses after costs over the window.")
    lines += [
        "- Rule bots only: the agent's judgment is not in this. Live, the agent confirms or "
        "rejects each day-trader setup and decides each v2 reaction look.",
        "- Every stock is watched every minute here; live, the rotation streams the moving "
        "stocks and polls the quiet ones every few minutes, so live catches at most this.",
        "- Survivorship: today's ASX 300 and ASX 200 lists on every past day.",
        "- v2's news: the announcement archive holds the ASX 300 only; small caps' news is in "
        "the replay only from 22 Sep (the live collector), so v2 is understated before that.",
        "- The opening auction is not modelled for past days (no order is placed before the "
        "open by either playbook).",
    ]
    if gaps:
        lines += ["", "## Data gaps (days not replayed)", ""]
        for r in gaps:
            lines.append(f"- {r['day']}: {'; '.join(r['gaps'])}")
    lines += ["", "## Day by day", "",
              "| Day | Market | v2 P&L | v2 trades | DT P&L | DT trades | "
              "DT setups (stale, uneconomic) |",
              "|---|---:|---:|---:|---:|---:|---|"]  # fmt: skip
    for r in played:
        s = r["daytrader"]["setups"]
        mm = r.get("market_move_pct")
        lines.append(
            f"| {r['day']} | {'' if mm is None else f'{mm:+.2f}%'} | {r['v2']['pnl']:+,.2f} | "
            f"{len(r['v2']['trades'])} | {r['daytrader']['pnl']:+,.2f} | "
            f"{len(r['daytrader']['trades'])} | {s['found']} ({s['stale']}, {s['uneconomic']}) |"
        )
    lines += ["", "## Every trade", ""]
    for r in played:
        for key, name in (("v2", "v2"), ("daytrader", "DT")):
            for t in r[key]["trades"]:
                lines.append(
                    f"- {r['day']} {name} {t['side']} {t['ticker']} {t['qty']} @ {t['entry']} "
                    f"stop {t['stop']} ({t['first_fill']}): net {t['net']:+,.2f}"
                    + (f", {t['r']:+.2f}R" if t.get("r") is not None else "")
                    + f" - {t['reason'][:80]}"
                )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# running it
# --------------------------------------------------------------------------
def run_days(cfg: Config, days: list[date], codes: list[str], shorts: set[str],
             ann: pd.DataFrame, progress=None,
             universe: set[str] | None = None) -> list[dict]:  # fmt: skip
    hist = HistoryBars(history_root(), str((cfg.get("arena.fill") or {}).get("minute_price",
                                                                            "close")))  # fmt: skip
    out = []
    for i, day in enumerate(days):
        try:
            r = replay_day(cfg, day, codes, shorts, ann, hist, universe=universe)
        except Exception as e:  # noqa: BLE001
            log.exception("replay of %s failed: %s", day, e)
            r = {"day": day.isoformat(), "gaps": [f"replay failed: {type(e).__name__}: {e}"]}
        out.append(r)
        if progress:
            progress(i + 1, len(days), r)
    return out


def run(cfg: Config, first: date, last: date, workers: int = 1, out_dir: Path | None = None,
        codes: list[str] | None = None) -> Path:  # fmt: skip
    """Replay every session in [first, last]; write reports/replay_ibkr_<stamp>.md and .json.
    With workers > 1 the days are split across child processes (`asxbot arena replay-ibkr
    --worker`), each writing its slice to a JSON file the parent gathers."""
    from asxbot.data.universe import asx200_codes

    a_codes, universe = arena_universe(cfg)
    codes = [c.upper() for c in (codes or a_codes)]
    shorts = asx200_codes(cfg.data_dir, cfg.get("collector.user_agent"))
    days = sessions(first, last)
    ann = announcements(cfg, first - timedelta(days=5), last)
    out_dir = out_dir or (cfg.root / "reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(SYD).strftime("%Y%m%d_%H%M")
    if workers > 1 and len(days) > 1:
        results = _run_parallel(cfg, days, workers, out_dir / f"replay_ibkr_{stamp}_parts", codes)
    else:
        def progress(i, n, r):
            log.info("replayed %s (%d/%d): v2 %s dt %s%s", r["day"], i, n,
                     (r.get("v2") or {}).get("pnl"), (r.get("daytrader") or {}).get("pnl"),
                     f" GAP {r['gaps']}" if r.get("gaps") else "")  # fmt: skip

        results = run_days(cfg, days, codes, shorts, ann, progress, universe=universe)
    played = [r for r in results if not r.get("gaps")]
    meta = {"avg_dt_universe": (sum(r.get("dt_universe", 0) for r in played) / len(played))
            if played else 0}  # fmt: skip
    js = out_dir / f"replay_ibkr_{stamp}.json"
    write_text_atomic(json.dumps({"label": LABEL, "first": first.isoformat(),
                                  "last": last.isoformat(), "codes": len(codes),
                                  "v2": scorecard(played, "v2"),
                                  "daytrader": scorecard(played, "daytrader"),
                                  "days": results}, indent=1, default=str), js)  # fmt: skip
    md = out_dir / f"replay_ibkr_{stamp}.md"
    write_text_atomic(render(results, first, last, len(codes), meta), md)
    return md


def _run_parallel(cfg: Config, days: list[date], workers: int, parts: Path,
                  codes: list[str]) -> list[dict]:  # fmt: skip
    import sys

    from asxbot import proc

    parts.mkdir(parents=True, exist_ok=True)
    n = max(1, min(int(workers), len(days)))
    slices = [days[i::n] for i in range(n)]
    procs = []
    for i, sl in enumerate(slices):
        if not sl:
            continue
        out = parts / f"part_{i}.json"
        cmd = [sys.executable, "-m", "asxbot.cli", "arena", "replay-ibkr", "--worker",
               "--days", ",".join(d.isoformat() for d in sl), "--json", str(out),
               "--codes", ",".join(codes)]  # fmt: skip
        log_path = parts / f"part_{i}.log"
        fh = open(log_path, "w", encoding="utf-8")  # noqa: SIM115
        procs.append((proc.popen(cmd, cwd=str(cfg.root), stdout=fh, stderr=proc.STDOUT), out, fh))
        log.info("replay worker %d: %d days", i, len(sl))
    results = []
    for p, out, fh in procs:
        p.wait()
        fh.close()
        if out.exists():
            results += json.loads(out.read_text(encoding="utf-8"))
        else:
            log.error("replay worker produced nothing: %s", out)
    results.sort(key=lambda r: r["day"])
    return results


def arena_universe(cfg: Config) -> tuple[list[str], set[str]]:
    """(the ASX 300 codes, the arena's whole universe): the live arena watches news in the
    ASX 300 and the small-cap list (runtime.build_arena), so the replay's v2 does too."""
    from asxbot.data.universe import build_universes

    a, b = build_universes(cfg.data_dir, cfg.get("collector.user_agent"))
    return [c.upper() for c in a.codes], {c.upper() for c in (*a.codes, *b.codes)}


def worker_main(cfg: Config, days: list[date], out: Path, codes: list[str]) -> int:
    from asxbot.data.universe import asx200_codes

    shorts = asx200_codes(cfg.data_dir, cfg.get("collector.user_agent"))
    _, universe = arena_universe(cfg)
    ann = announcements(cfg, min(days) - timedelta(days=5), max(days))
    results = run_days(cfg, days, codes, shorts, ann,
                       lambda i, n, r: print(f"{r['day']} {i}/{n}", flush=True),
                       universe=universe)  # fmt: skip
    write_text_atomic(json.dumps(results, default=str), out)
    return 0


__all__ = ["HistoryBars", "LABEL", "announcements", "render", "replay_day", "run", "scorecard",
           "sessions", "worker_main"]  # fmt: skip
