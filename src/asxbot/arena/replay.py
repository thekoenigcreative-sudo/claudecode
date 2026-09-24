"""The plumbing replay: announcements v2's rule bot and the day trader's rule bot, run over
past days' minute bars through the live code - the same setups, the same order limits, the
same broker - in scratch books, minute by minute on a simulated clock.

PLUMBING, NOT A VERDICT. A few days of Yahoo bars (plumbing test - not a go/no-go), today's
ASX 200 list, and only the rule bots: the agent is never asked about the past (it may know
what happened, and CLAUDE.md forbids AI classification of historical announcements). What it
shows is whether the machinery finds setups, places orders, fills, manages and closes them,
and what the rules would have done - not whether they have an edge.

Delay: a decision at wall time T sees only the bars a feed `delay` minutes behind would have
held as final at T (intraday.visible), and fills at the first bar after T. With the delay at
~20 minutes, that is the arena as it runs on Yahoo; at 0 it is what IBKR live data would give.
"""

from __future__ import annotations

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
from asxbot.arena.correction_arn000003 import CachedBars
from asxbot.arena.intraday import MarketView, ReplayFeed, continuous, prior_sessions
from asxbot.arena.reaction_v2 import load_bot_state
from asxbot.arena.runtime import Arena
from asxbot.backtest.costs import CostModel
from asxbot.config import Config
from asxbot.log import get_logger

log = get_logger("asxbot.arena.replay")
SYD = ZoneInfo("Australia/Sydney")


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@dataclass
class ReplayArena(Arena):
    """An Arena whose books and events live in a scratch folder, whose clock is simulated,
    and whose daily prices come from the real cache without a request."""

    real_data_dir: Path | None = None

    def daily_lookup(self):
        from asxbot.data.factory import get_store

        store = get_store(Config(raw=self._real_raw(), root=self.cfg.root, env=self.cfg.env))

        def daily(ticker: str):
            try:
                return store.load(ticker)
            except Exception:  # noqa: BLE001
                return None

        return daily

    def _real_raw(self) -> dict:
        raw = dict(self.cfg.raw)
        raw["data"] = {**raw.get("data", {}), "dir": str(self.real_data_dir)}
        return raw


def scratch_arena(
    cfg: Config, scratch: Path, clock: Clock, universe: set[str], shorts: set[str]
) -> ReplayArena:
    from asxbot.data.factory import get_store
    from asxbot.live.quotes import median_turnover_20d

    raw = dict(cfg.raw)
    raw["data"] = {**raw.get("data", {}), "dir": str(scratch)}
    scfg = Config(raw=raw, root=cfg.root, env=cfg.env)
    store = get_store(cfg)

    def daily(t):
        try:
            return store.load(t)
        except Exception:  # noqa: BLE001
            return None

    def adv(t):
        d = daily(t)
        return (
            None if d is None or len(d) < 20 else float((d["close"] * d["volume"]).tail(20).mean())
        )

    fill = cfg.get("arena.fill") or {}
    broker = ArenaBroker(
        scratch,
        CostModel.from_config(cfg),
        CachedBars(cfg.data_dir, str(fill.get("minute_price", "close"))),
        adv,
        short_borrow_pct_annual=float(cfg.get("arena.costs.short_borrow_pct_annual", 3.0)),
        resolve_after_minutes=int(fill.get("resolve_after_minutes", 22)),
        clock=clock,
        max_volume_share=float(fill.get("max_volume_share", 0.20)),
    )
    broker.median_turnover = lambda t: median_turnover_20d(daily(t))
    return ReplayArena(scfg, broker, broker.store, universe, shorts, real_data_dir=cfg.data_dir)


def _announcements(cfg: Config, day: date) -> pd.DataFrame:
    frames = []
    for d in [*prior_sessions(day, 1), day]:
        p = cfg.data_dir / "announcements" / "live" / f"{d.isoformat()}.parquet"
        if p.exists():
            frames.append(pd.read_parquet(p))
    if not frames:
        return pd.DataFrame(columns=["code", "released_at", "headline", "price_sensitive"])
    return pd.concat(frames, ignore_index=True).drop_duplicates("ids_id")


def replay_day(
    cfg: Config,
    day: date,
    delay: int,
    universe: set[str],
    shorts: set[str],
    dt_codes: list[str],
    keep: Path | None = None,
) -> dict:
    """One day, both rule bots. Returns what they did."""
    from asxbot.arena.levels import load_playbook

    v2pb = load_playbook(cfg, "asx_announcements_v2")
    dtpb = load_playbook(cfg, "asx_daytrader")
    scratch = Path(tempfile.mkdtemp(prefix=f"replay_{day:%m%d}_{delay}_"))
    try:
        live = scratch / "announcements" / "live"
        live.mkdir(parents=True)
        for d in [*prior_sessions(day, 1), day]:
            src = cfg.data_dir / "announcements" / "live" / f"{d.isoformat()}.parquet"
            if src.exists():
                shutil.copy(src, live / src.name)
        clock = Clock(datetime.combine(day, time_cls(9, 50), tzinfo=SYD))
        arena = scratch_arena(cfg, scratch, clock, universe, shorts)
        minutes = arena.broker.minutes
        view = MarketView(minutes, day, ReplayFeed(minutes, delay),
                          str(cfg.get("backtest.index_ticker", "^AXJO")), 5, 3)  # fmt: skip
        ann = _announcements(cfg, day)
        st = daytrader.load_state(arena.cfg.data_dir, day)
        st["universe"] = dt_codes
        daytrader.save_state(arena.cfg.data_dir, day, st)
        daytrader._DAY.clear()

        def visible_price(t: str):
            b = continuous(view.bars(t, clock.t))
            return None if not len(b) else float(b["close"].iloc[-1])

        t = datetime.combine(day, time_cls(10, 0), tzinfo=SYD)
        end = datetime.combine(day, time_cls(16, 30), tzinfo=SYD)
        accounts = [(pb, "bot") for pb in (v2pb, dtpb)]
        while t <= end:
            clock.t = t
            v2_flow.v2_bot_cycle(arena, v2pb, view, t, ann=ann)
            daytrader.cycle(arena, dtpb, view, t, use_agent=False, refresh=False)
            for pb, kind in accounts:
                arena.broker.work(arena.account(pb, kind), t - timedelta(minutes=delay))
            for pb in (v2pb, dtpb):
                v2_flow.flatten(arena, pb, t, price_of=visible_price)
            t += timedelta(minutes=1)
        # Let the broker finish the day on every bar, as the evening's resolve does.
        clock.t = datetime.combine(day, time_cls(19, 0), tzinfo=SYD)
        for pb, kind in accounts:
            arena.broker.work(arena.account(pb, kind), clock.t)
        out = {"day": day.isoformat(), "delay_minutes": delay}
        out["v2_bot"] = _book(arena, v2pb, day)
        out["v2_state"] = load_bot_state(arena.cfg.data_dir, day)
        out["daytrader_bot"] = _book(arena, dtpb, day)
        dstate = daytrader.load_state(arena.cfg.data_dir, day)
        sig = dstate.get("signals", [])
        out["daytrader_setups"] = {
            "found": len(sig),
            "by_setup": pd.Series([s["setup"] for s in sig]).value_counts().to_dict()
            if sig
            else {},
            "stale": sum(1 for s in sig if str(s.get("skipped", "")).startswith("stale")),
            "late": sum(1 for s in sig if "last entry" in str(s.get("skipped", ""))),
            "bot_orders": sum(1 for s in sig if (s.get("bot") or {}).get("order_id")),
            "bot_skipped": sum(1 for s in sig if (s.get("bot") or {}).get("skipped")),
            "bot_refused": sum(1 for s in sig if (s.get("bot") or {}).get("refused")),
            "signals": sig,
        }
        return out
    finally:
        if keep is not None:
            shutil.copytree(scratch, keep / scratch.name, dirs_exist_ok=True)
        shutil.rmtree(scratch, ignore_errors=True)


def _book(arena, pb, day: date) -> dict:
    acct = arena.account(pb, "bot")
    trades = []
    for o in sorted(acct.orders.values(), key=lambda o: o.order_id):
        trades.append({
            "order_id": o.order_id, "ticker": o.ticker, "side": o.side, "type": o.order_type,
            "qty": o.qty, "filled": o.filled_qty, "limit": o.limit, "avg": o.avg_price,
            "status": o.status, "decided": o.decided_at[11:19],
            "first_fill": (o.fill_minute or "")[11:16], "fee": o.commission,
            "realised": o.realised, "reason": o.reason[:140],
        })  # fmt: skip
    return {
        "start": acct.starting_cash,
        "cash": round(acct.cash, 2),
        "equity": round(acct.equity(arena.broker.prices(acct, day)), 2),
        "pnl": round(acct.equity(arena.broker.prices(acct, day)) - acct.starting_cash, 2),
        "fees": round(acct.fees_paid, 2),
        "open_positions": {t: p.qty for t, p in acct.positions.items()},
        "orders": trades,
    }


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------
LABEL = (
    "PLUMBING REPLAY - plumbing test, not a go/no-go. Yahoo 1-minute bars, a few days, "
    "today's ASX 200 list, rule bots only (the agent is never asked about the past)."
)


def run(cfg: Config, days: list[date], delays: list[int], out_dir: Path) -> Path:
    """Replay each day at each delay; write reports/<name>.md and .json. Returns the .md."""
    import json

    from asxbot.arena.levels import load_playbook
    from asxbot.data.universe import asx200_codes, build_universes

    a, b = build_universes(cfg.data_dir, cfg.get("collector.user_agent"))
    universe = set(a.codes) | set(b.codes)
    shorts = asx200_codes(cfg.data_dir, cfg.get("collector.user_agent"))

    class _Cfg:
        pass

    holder = _Cfg()
    holder.cfg = cfg
    codes, _ = daytrader.build_universe(holder, load_playbook(cfg, "asx_daytrader"))
    results = []
    for delay in delays:
        for day in days:
            log.info("replaying %s at a %d-minute delay", day, delay)
            results.append(replay_day(cfg, day, delay, universe, shorts, codes))
    stamp = datetime.now(SYD).strftime("%Y%m%d_%H%M")
    out_dir.mkdir(parents=True, exist_ok=True)
    js = out_dir / f"replay_v2_daytrader_{stamp}.json"
    js.write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    md = out_dir / f"replay_v2_daytrader_{stamp}.md"
    md.write_text(render(results, len(codes), days), encoding="utf-8")
    return md


def render(results: list[dict], n_codes: int, days: list[date]) -> str:
    lines = [
        "# Plumbing replay: announcements v2 rule bot and the day-trader rule bot",
        "",
        f"*{LABEL}*",
        "",
        f"Generated {datetime.now(SYD):%Y-%m-%d %H:%M} Sydney. Days: "
        + ", ".join(d.isoformat() for d in days)
        + f". Day-trader universe: {n_codes} liquid ASX 300 stocks. Rules: config.yaml "
        "`asx_announcements_v2` and `asx_daytrader` (frozen 2026-09-24, before this ran). "
        "Each decision sees only the bars a feed that many minutes behind held as final, and "
        "fills at the first bar after it. 20 minutes is the arena on Yahoo; 0 is what IBKR "
        "live data would give. Costs: IBKR brokerage both ways ($6.60 minimum) and the "
        "arena's slippage model; fills capped at 20% of a bar's volume.",
        "",
    ]
    for r in results:
        d, delay = r["day"], r["delay_minutes"]
        v2, dt, ds = r["v2_bot"], r["daytrader_bot"], r["daytrader_setups"]
        st = r.get("v2_state") or {}
        sigs = [c for c in st.get("candidates", []) if c.get("signal")]
        lines += [
            f"## {d}, feed {delay} minutes behind",
            "",
            f"**v2 rule bot:** {st.get('status', '?')}"
            + (
                f", decided {str(st.get('decided_at', ''))[11:16]} on bars to "
                f"{str(st.get('feed_at', ''))[11:16]}"
                if st.get("decided_at")
                else ""
            )
            + f"; {len(st.get('candidates', []))} stocks with news, {len(sigs)} signals; "
            f"P&L after costs {v2['pnl']:+,.2f} (fees {v2['fees']:,.2f}).",
        ]
        for c in sigs:
            lines.append(f"- signal {c['ticker']}: {c['why']}")
        for o in v2["orders"]:
            lines.append(_order_line(o))
        lines += [
            "",
            f"**Day trader:** {ds['found']} setups found "
            + "("
            + ", ".join(f"{k} {v}" for k, v in ds["by_setup"].items())
            + ")"
            + f"; the rule bot placed {ds['bot_orders']}, skipped {ds['bot_skipped']} (account "
            f"full, already in the stock, or unsizeable), {ds['bot_refused']} refused by the "
            f"limits, {ds['stale']} stale; P&L after costs {dt['pnl']:+,.2f} "
            f"(fees {dt['fees']:,.2f}).",
        ]
        for o in dt["orders"]:
            lines.append(_order_line(o))
        lines.append("")
    lines += [
        "## What this is not",
        "",
        "- Not a result. A handful of days, one market regime, survivorship-biased lists, and "
        "Yahoo's minute bars (the opening auction is missing or folded into the 10:00 bar).",
        "- The agent is not in it: only the rule bots.",
        "- Announcement coverage: the live collector started on 22 Sep and its 22 Sep file ends "
        "at 14:37, so news released after the 21 and 22 Sep closes is missing from the "
        "22 and 23 Sep v2 candidates. 24 Sep's candidates are complete.",
        "",
    ]
    return "\n".join(lines)


def _order_line(o: dict) -> str:
    px = "" if o.get("avg") is None else f" avg {o['avg']}"
    when = f" first fill {o['first_fill']}" if o.get("first_fill") else ""
    pnl = f" realised {o['realised']:+.2f}" if o.get("realised") else ""
    return (
        f"- {o['order_id']} {o['side']} {o['ticker']} {o['filled']}/{o['qty']} "
        f"({o['type']}, {o['status']}) decided {o['decided'][:5]}{when}{px}{pnl}"
        f" - {o['reason'][:90]}"
    )
