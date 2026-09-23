"""Short-side strategies S1 and S2 (config.yaml `strategies`, frozen 2026-09-23).

Rules committed before any run (commit 3a936e4). This module is backtest only: nothing in
the arena or the live watcher uses it.

S1  short after an earnings miss: a price-sensitive "results" announcement reacting on
    session t, t's close-to-close return at least 5% below the ASX 200's, on 3x volume.
    Confirmed at t's close, shorted at t+1's open; covered at the open 10 sessions later.
S2  short ahead of placement shares: a price-sensitive placement announcement, shorted at
    the first open after it; covered 5 sessions after the quotation notice (Appendix 2A)
    when one follows within 10 sessions, else 10 sessions after entry.

Both: ASX 200 only (today's list - survivorship), stop 8% above the entry fill tested on
the close and covered at the next open, A's brokerage and slippage adverse both ways, and
a borrow fee accrued every night on the short's value at the close.

The simulator mirrors backtest/engine.py (A's), with the sign turned round: a short sale
is priced below the open by the slippage, a cover above it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from asxbot.backtest.costs import CostModel
from asxbot.backtest.engine import Result, _last_valid
from asxbot.backtest.metrics import cagr, max_drawdown
from asxbot.backtest.signals import Panels, own_announcements, reaction_sessions
from asxbot.log import get_logger

log = get_logger("asxbot.backtest.shorts")


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------
def _reacting(ann: pd.DataFrame, sessions: pd.DatetimeIndex) -> pd.DataFrame:
    """Own announcements with their reaction session, one per company per session."""
    a = own_announcements(ann).copy()
    a["reaction"] = reaction_sessions(a["released_at"], sessions)
    a = a.dropna(subset=["reaction"]).sort_values("released_at")
    return a.drop_duplicates(["code", "reaction"], keep="first")


def s1_events(p: Panels, ann: pd.DataFrame, rule: dict) -> pd.DataFrame:
    """Sessions t on which a results announcement reacted with a fall of at least
    `fall_pct_vs_index` below the index, close to close, on `volume_multiple` volume."""
    idx_ret = (p.index_close / p.index_close.shift(1) - 1).reindex(p.dates)
    rel = (p.close / p.close.shift(1) - 1).sub(idx_ret, axis=0) * 100
    window = int(rule["volume_window_days"])
    avg_vol = p.volume.shift(1).rolling(window, min_periods=window).mean()
    vmult = p.volume / avg_vol.replace(0, np.nan)
    hit = (
        (rel <= -float(rule["fall_pct_vs_index"]))
        & (vmult >= float(rule["volume_multiple"]))
        & p.eligible
        & p.close.notna()
    )
    a = ann
    if rule.get("price_sensitive_only", True):
        a = a[a["price_sensitive"].astype(bool)]
    a = a[a["type"] == rule["announcement_type"]]
    a = _reacting(a, p.dates)
    rows = []
    for r in a.itertuples():
        t, d = r.code, r.reaction
        if t not in hit.columns or d not in hit.index or not bool(hit.at[d, t]):
            continue
        rows.append(
            {"date": d, "ticker": t, "fall_rel": float(rel.at[d, t]),
             "vol_mult": float(vmult.at[d, t]), "headline": r.headline, "ids_id": r.ids_id,
             "priority": float(vmult.at[d, t])}
        )  # fmt: skip
    return _frame(rows)


def s2_events(p: Panels, ann: pd.DataFrame, rule: dict) -> pd.DataFrame:
    """Placement announcements, with the session each is to be covered at."""
    inc = re.compile(rule["headline_pattern"], re.I)
    exc = re.compile(rule["headline_exclude"], re.I)
    quo = re.compile(rule["quotation_pattern"], re.I)
    h = ann["headline"].astype(str)
    a = ann[h.map(lambda s: bool(inc.search(s)) and not exc.search(s))]
    if rule.get("price_sensitive_only", True):
        a = a[a["price_sensitive"].astype(bool)]
    a = _reacting(a, p.dates)

    q = _reacting(ann[h.map(lambda s: bool(quo.search(s)))], p.dates)
    pos_of = {d: i for i, d in enumerate(p.dates)}
    q_by_code: dict[str, list[int]] = {}
    for r in q.itertuples():
        q_by_code.setdefault(r.code, []).append(pos_of[r.reaction])
    for v in q_by_code.values():
        v.sort()

    win = int(rule["quotation_window_sessions"])
    after_q = int(rule["cover_after_quotation_sessions"])
    fallback = int(rule["fallback_hold_days"])
    rows = []
    for r in a.itertuples():
        t, d = r.code, r.reaction
        if t not in p.close.columns:
            continue
        e = pos_of[d]
        qs = [i for i in q_by_code.get(t, []) if e <= i <= e + win]
        if qs:
            cover, basis = qs[0] + after_q, f"quotation {p.dates[qs[0]].date()} + {after_q}"
        else:
            cover, basis = e + fallback, f"no quotation notice; {fallback} sessions"
        adv = p.adv_dollar.at[d, t] if d in p.adv_dollar.index else np.nan
        rows.append(
            {"date": d, "ticker": t, "headline": r.headline, "ids_id": r.ids_id,
             "cover_pos": cover, "cover_basis": basis,
             "priority": float(adv) if np.isfinite(adv) else 0.0}
        )  # fmt: skip
    return _frame(rows)


def _frame(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["date", "ticker", "priority"])
    return (
        pd.DataFrame(rows)
        .sort_values(["date", "priority"], ascending=[True, False])
        .reset_index(drop=True)
    )


# --------------------------------------------------------------------------
# the simulator
# --------------------------------------------------------------------------
@dataclass
class Short:
    ticker: str
    qty: int
    entry_i: int
    entry_px: float  # sale fill, net of slippage
    raw_entry: float
    proceeds: float
    entry_fee: float
    stop_px: float
    exit_due: int
    borrow: float = 0.0
    flagged_stop: bool = False
    meta: dict = field(default_factory=dict)


def simulate_shorts(
    p: Panels,
    events: pd.DataFrame,
    costs: CostModel,
    borrow_pct_annual: float,
    starting_capital: float,
    max_positions: int,
    stop_pct: float,
    entry_lag: int,
    hold_days: int | None = None,
    max_gross_x: float = 1.0,
    min_order_aud: float = 500.0,
    end: pd.Timestamp | None = None,
    name: str = "shorts",
) -> Result:
    """Shorts on daily bars, whole shares. An event's `cover_pos` (a session position), if
    present, is when it is covered at the open; otherwise `hold_days` after entry."""
    dates = p.dates
    mask = np.ones(len(dates), dtype=bool) if end is None else dates <= pd.Timestamp(end)
    pos_of = {d: i for i, d in enumerate(dates)}
    open_np, close_np = p.open.to_numpy(), p.close.to_numpy()
    adv_np, elig_np = p.adv_dollar.to_numpy(), p.eligible.to_numpy()
    idx_close = p.index_close.reindex(dates).to_numpy()
    col = {t: j for j, t in enumerate(p.close.columns)}
    nights = np.diff(dates.values).astype("timedelta64[D]").astype(int)
    nights = np.append(nights, 1)
    rate = borrow_pct_annual / 100.0 / 365.0

    pending: dict[int, list[dict]] = {}
    for ev in events.to_dict("records"):
        d = pd.Timestamp(ev["date"])
        if d in pos_of:
            pending.setdefault(pos_of[d] + entry_lag, []).append(ev)

    cash = float(starting_capital)
    shorts: dict[str, Short] = {}
    trades: list[dict] = []
    equity = np.full(len(dates), np.nan)
    exposure = np.full(len(dates), np.nan)

    def liability(i: int) -> float:
        return sum(s.qty * _last_valid(close_np, i, col[s.ticker]) for s in shorts.values())

    def cover(s: Short, i: int, reason: str, at_close: bool = False) -> None:
        nonlocal cash
        j = col[s.ticker]
        px = close_np[i, j] if at_close else open_np[i, j]
        if not np.isfinite(px):
            px = close_np[i, j] if not at_close else _last_valid(close_np, i, j)
            if not np.isfinite(px):
                return  # halted with no price: carry, and try the next session
        fill = costs.buy_price(px, px * s.qty, adv_np[i, j])
        paid = fill * s.qty
        fee = costs.brokerage(paid)
        cash -= paid + fee
        pnl = s.proceeds - s.entry_fee - paid - fee - s.borrow
        i0, i1 = s.entry_i - 1, i - 1 if not at_close else i
        idx_move = (
            (idx_close[i1] / idx_close[i0] - 1) * 100
            if i0 >= 0 and np.isfinite(idx_close[i0]) and np.isfinite(idx_close[i1])
            else np.nan
        )
        trades.append(
            {
                "ticker": s.ticker, "entry_date": dates[s.entry_i], "exit_date": dates[i],
                "qty": s.qty, "entry_px": s.entry_px, "exit_px": fill,
                "entry_value": s.proceeds, "borrow": s.borrow,
                "costs": s.entry_fee + fee + s.borrow + (s.raw_entry - s.entry_px) * s.qty
                + (fill - px) * s.qty,
                "pnl": pnl, "ret_pct": pnl / s.proceeds * 100,
                "index_move_pct": idx_move,
                "hold_sessions": i - s.entry_i, "reason": reason, **s.meta,
            }
        )  # fmt: skip
        del shorts[s.ticker]

    for i in range(len(dates)):
        if not mask[i]:
            continue
        # 1. covers at the open
        for t in list(shorts):
            s = shorts[t]
            if s.flagged_stop:
                cover(s, i, "stop")
            elif i >= s.exit_due:
                cover(s, i, s.meta.get("cover_reason", "time"))
        # 2. new shorts at the open
        if i in pending and len(shorts) < max_positions:
            owed = liability(i - 1)
            mark = cash - owed
            size = mark / max_positions
            for ev in sorted(pending[i], key=lambda e: -float(e.get("priority", 0.0))):
                if len(shorts) >= max_positions or mark <= 0:
                    break
                t = ev["ticker"]
                if t in shorts or t not in col:
                    continue
                j = col[t]
                px = open_np[i, j]
                if not np.isfinite(px) or px <= 0 or not elig_np[i, j]:
                    continue
                if owed + size > mark * max_gross_x + 1e-6:
                    break  # no leverage: the book is already as short as equity allows
                fill = costs.sell_price(px, size, adv_np[i, j])
                qty = int(math.floor(size / fill))
                if qty <= 0 or fill * qty < min_order_aud:
                    continue
                proceeds = fill * qty
                fee = costs.brokerage(proceeds)
                cash += proceeds - fee
                owed += px * qty
                cover_pos = ev.get("cover_pos")
                due = (
                    int(cover_pos)
                    if cover_pos is not None and cover_pos == cover_pos
                    else (i + int(hold_days or 0))
                )
                shorts[t] = Short(
                    ticker=t, qty=qty, entry_i=i, entry_px=fill, raw_entry=px,
                    proceeds=proceeds, entry_fee=fee, stop_px=fill * (1 + stop_pct / 100),
                    exit_due=max(due, i + 1),
                    meta={
                        "event_date": ev["date"], "headline": ev.get("headline"),
                        "vol_mult": ev.get("vol_mult"), "fall_rel": ev.get("fall_rel"),
                        "cover_basis": ev.get("cover_basis"),
                        "cover_reason": "quotation" if str(ev.get("cover_basis", "")).startswith(
                            "quotation") else "time",
                    },
                )  # fmt: skip
        # 3. the close: borrow for the night, the stop, the mark
        owed = 0.0
        for t, s in shorts.items():
            c = _last_valid(close_np, i, col[t])
            owed += c * s.qty
            fee = c * s.qty * rate * int(nights[i])
            s.borrow += fee
            cash -= fee
            if np.isfinite(close_np[i, col[t]]) and close_np[i, col[t]] > s.stop_px:
                s.flagged_stop = True
        equity[i] = cash - owed
        exposure[i] = owed / equity[i] if equity[i] > 0 else np.nan

    last = int(np.flatnonzero(mask)[-1]) if mask.any() else len(dates) - 1
    for t in list(shorts):
        cover(shorts[t], last, "end", at_close=True)
    eq = pd.Series(equity, index=dates, name="equity").dropna()
    ex = pd.Series(exposure, index=dates, name="exposure").dropna()
    return Result(name=name, trades=pd.DataFrame(trades), equity=eq, exposure=ex)


# --------------------------------------------------------------------------
# the numbers asked for
# --------------------------------------------------------------------------
def short_stats(res: Result, years: float) -> dict:
    t = res.trades
    if t.empty:
        return {"trades": 0}
    worst = t.nsmallest(1, "ret_pct").iloc[0]
    hedged = t["ret_pct"] + t["index_move_pct"]  # the short plus a long in the index
    return {
        "trades": len(t),
        "companies": t["ticker"].nunique(),
        "per_year": len(t) / years if years > 0 else float("nan"),
        "win_pct": (t["pnl"] > 0).mean() * 100,
        "avg_pct": t["ret_pct"].mean(),
        "median_pct": t["ret_pct"].median(),
        "avg_hedged_pct": hedged.mean(),
        "median_hold": t["hold_sessions"].median(),
        "p5_pct": t["ret_pct"].quantile(0.05),
        "worst_pct": float(worst["ret_pct"]),
        "worst_trade": f"{worst['ticker']} {pd.Timestamp(worst['entry_date']).date()}",
        "stops": int((t["reason"] == "stop").sum()),
        "borrow_aud": t["borrow"].sum(),
        "cagr_pct": cagr(res.equity) * 100,
        "max_dd_pct": max_drawdown(res.equity) * 100,
        "final": float(res.equity.iloc[-1]) if len(res.equity) else float("nan"),
    }


# --------------------------------------------------------------------------
# the run: in-sample only, by construction
# --------------------------------------------------------------------------
def run_shorts(cfg, out: Path | None = None) -> Path:
    """Both strategies, 1x and 2x slippage, both borrow assumptions, in-sample only.

    There is deliberately no switch to simulate the holdout here: that is one run, for a
    shortlisted strategy, made on purpose and separately.
    """
    from asxbot.announcements.history import HistoryArchive
    from asxbot.backtest.signals import build_panels
    from asxbot.data.benchmark import build_benchmark
    from asxbot.data.factory import get_store
    from asxbot.io import write_text_atomic

    common = cfg.get("strategies.shorts_common")
    s1, s2 = cfg.get("strategies.S1_earnings_miss"), cfg.get("strategies.S2_placement_supply")
    store = get_store(cfg)
    start = cfg.get("data.price_history_start")
    bm = build_benchmark(
        store, start, cfg.get("backtest.benchmark_ticker"), cfg.get("backtest.index_ticker")
    )
    oos_start = bm.total_return.index.max() - pd.DateOffset(
        years=int(cfg.get("backtest.holdout_years", 3))
    )
    members = sorted(set(pd.read_csv(cfg.data_dir / "universe" / "asx200_members.csv")["code"]))
    arc = HistoryArchive.__new__(HistoryArchive)
    arc.dir = cfg.data_dir / "announcements" / "history"
    # A member with no announcement archive can never trigger either rule; leaving it out
    # also means nothing here reaches for the network.
    codes = [c for c in members if arc.path(c).exists()]
    frames = store.get_many(codes, start, max_age_days=10_000)
    frames = {c: f for c, f in frames.items() if len(f)}
    panels = build_panels(
        frames, bm.index_open, bm.index_close,
        float(common["turnover_floor_aud"]), int(common["turnover_window_days"]),
    )  # fmt: skip
    ann = arc.load_all([c for c in codes if c in panels.close.columns])
    ann["released_at"] = pd.to_datetime(ann["released_at"])

    ev1 = s1_events(panels, ann, s1)
    ev2 = s2_events(panels, ann, s2)
    in_s = panels.dates[panels.dates <= oos_start]
    first = panels.close.loc[in_s].dropna(how="all").index.min()
    years = (oos_start - first).days / 365.25
    runs: list[tuple[str, float, float, Result]] = []
    for mult in common["slippage_multipliers"]:
        costs = CostModel.from_config(cfg, multiplier=float(mult))
        for borrow in common["borrow_pct_annual"]:
            kw = dict(
                costs=costs, borrow_pct_annual=float(borrow),
                starting_capital=float(common["starting_aud"]),
                max_positions=int(common["max_positions"]),
                max_gross_x=float(common["max_gross_short_x_equity"]),
                min_order_aud=float(common["min_order_aud"]), end=oos_start,
            )  # fmt: skip
            r1 = simulate_shorts(
                panels, ev1, stop_pct=float(s1["stop_pct"]), entry_lag=1,
                hold_days=int(s1["hold_days"]), name="S1", **kw,
            )  # fmt: skip
            r2 = simulate_shorts(
                panels, ev2, stop_pct=float(s2["stop_pct"]), entry_lag=0, name="S2", **kw,
            )  # fmt: skip
            runs += [("S1", float(mult), float(borrow), r1), ("S2", float(mult), float(borrow), r2)]
            out_dir = cfg.data_dir / "backtest"
            out_dir.mkdir(parents=True, exist_ok=True)
            for nm, r in (("S1", r1), ("S2", r2)):
                csv = out_dir / f"trades_{nm}_x{mult:g}_borrow{borrow:g}.csv"
                r.trades.to_csv(csv, index=False)

    bench = bm.total_return[(bm.total_return.index >= first) & (bm.total_return.index <= oos_start)]
    text = render_shorts(
        runs,
        ev1,
        ev2,
        bench,
        first,
        oos_start,
        years,
        store.provider.label,
        len(members),
        len(panels.close.columns),
    )
    out = out or (cfg.root / "reports" / "shorts_phase1.md")
    write_text_atomic(text, out)
    log.info("short-side report written: %s", out)
    return out


def _fmt(x, nd=2, suffix=""):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "n/a"
    return f"{x:,.{nd}f}{suffix}"


def render_shorts(runs, ev1, ev2, bench, first, oos_start, years, label, n_codes, n_priced) -> str:
    L = [
        "# Short-side backtest: S1 and S2",
        "",
        f"_Generated {pd.Timestamp.now():%Y-%m-%d %H:%M}. Rules: config.yaml `strategies`, "
        "frozen 2026-09-23 and committed before this run (3a936e4)._",
        "",
        f"## DATA LABEL: **{label.upper()}**",
        "",
        f"- **IN-SAMPLE ONLY**: {first.date()} to {oos_start.date()}. The holdout (the last 3 "
        "years) was not simulated.",
        f"- **Survivorship**: the universe is TODAY's ASX 200 ({n_codes} codes; {n_priced} have "
        "an announcement archive and price history, the rest cannot trigger) applied to every "
        "past year. Stocks that left the index or were delisted are missing: the survivors "
        "are the ones that did well, which works against a short.",
        "- **Borrow fee: both rates are ASSUMPTIONS.** IBKR's rate for ASX 200 shares could "
        "not be sourced; 1% and 5% a year bracket it.",
        "- **S2 is not independent evidence.** Its idea came from slicing this same in-sample "
        "data (A's capital-raising slice), so passing here proves little. Only the holdout can.",
        "- Costs: A's brokerage (0.088%, $6.60 min) and slippage, adverse both ways, at 1x "
        "and 2x; no interest on short proceeds; dividends paid implicitly (adjusted prices).",
        f"- Benchmark (buy and hold, same window): CAGR {_fmt(cagr(bench) * 100, 1, '%')}, "
        f"worst drawdown {_fmt(max_drawdown(bench) * 100, 1, '%')}.",
        "",
    ]
    for nm, ev, title in (
        ("S1", ev1, "S1: short after an earnings miss"),
        ("S2", ev2, "S2: short ahead of placement shares"),
    ):
        in_ev = ev[pd.to_datetime(ev["date"]) <= oos_start] if len(ev) else ev
        L += [f"## {title}", "", f"In-sample events: {len(in_ev)} across "
              f"{in_ev['ticker'].nunique() if len(in_ev) else 0} companies (before slots, "
              "the turnover floor at entry and the minimum order).", ""]  # fmt: skip
        if nm == "S2" and len(in_ev):
            q = in_ev["cover_basis"].astype(str).str.startswith("quotation").sum()
            L += [
                f"Cover basis: {q} events by a quotation notice, {len(in_ev) - q} by the "
                "fixed 10 sessions (quotation notices exist only from Dec 2019).",
                "",
            ]
        L += [
            "| slippage | borrow (assumed) | trades | companies | per year | made money | "
            "avg trade | median trade | avg, index-hedged | median hold | worst trade | "
            "5th pct trade | stops | CAGR | worst drawdown | final $ |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|",
        ]
        for n, mult, borrow, r in runs:
            if n != nm:
                continue
            s = short_stats(r, years)
            if s.get("trades", 0) == 0:
                L.append(f"| {mult:g}x | {borrow:g}% | 0 | | | | | | | | | | | | | |")
                continue
            L.append(
                f"| {mult:g}x | {borrow:g}% | {s['trades']} | {s['companies']} | "
                f"{_fmt(s['per_year'], 1)} | {_fmt(s['win_pct'], 1, '%')} | "
                f"{_fmt(s['avg_pct'], 2, '%')} | {_fmt(s['median_pct'], 2, '%')} | "
                f"{_fmt(s['avg_hedged_pct'], 2, '%')} | {_fmt(s['median_hold'], 0)} | "
                f"{_fmt(s['worst_pct'], 1, '%')} ({s['worst_trade']}) | "
                f"{_fmt(s['p5_pct'], 1, '%')} | {s['stops']} | {_fmt(s['cagr_pct'], 1, '%')} | "
                f"{_fmt(s['max_dd_pct'], 1, '%')} | {_fmt(s['final'], 0)} |"
            )
        base = next(r for n, m, b, r in runs if n == nm and m == 1 and b == 1)
        if not base.trades.empty:
            w = base.trades.nsmallest(5, "ret_pct")
            L += [
                "",
                "The tail - five worst trades at 1x slippage, 1% borrow (a short's loss has no "
                "ceiling):",
                "",
                "| ticker | entry | exit | held | trade | index same window | exit reason | "
                "headline |",
                "|---|---|---|---:|---:|---:|---|---|",
            ]
            for r in w.itertuples():
                L.append(
                    f"| {r.ticker} | {pd.Timestamp(r.entry_date).date()} | "
                    f"{pd.Timestamp(r.exit_date).date()} | {r.hold_sessions} | "
                    f"{r.ret_pct:+.1f}% | {_fmt(r.index_move_pct, 1, '%')} | {r.reason} | "
                    f"{str(r.headline)[:60]} |"
                )
        L.append("")
    L += [
        "## Reading this",
        "",
        "- *made money*: share of trades with a profit after brokerage, slippage and borrow.",
        "- *avg, index-hedged*: each short's return plus the ASX 200's move over the same "
        "window (approximately open to open), i.e. the short with a long index hedge. A short "
        "that loses only because the market rose shows up here as roughly flat.",
        "- *worst trade* and *5th pct*: the loss tail, per trade, after all costs.",
        "- Trades per year use the in-sample span from the first priced session.",
        "",
    ]
    return "\n".join(L)
