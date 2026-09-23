"""Orchestrate the Phase 1 backtest: both strategies, baseline, benchmark, both universes,
1x and 2x slippage. Writes reports/phase1.md and trade lists under data/backtest/.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from asxbot.announcements.history import HistoryArchive
from asxbot.backtest.baseline import run_momentum
from asxbot.backtest.costs import CostModel
from asxbot.backtest.engine import Result, simulate
from asxbot.backtest.report import Block, ReportInput, render
from asxbot.backtest.signals import announcement_events, build_panels, detect_events
from asxbot.config import Config
from asxbot.data.benchmark import build_benchmark
from asxbot.data.factory import get_store
from asxbot.data.universe import build_universes
from asxbot.io import write_csv_atomic, write_text_atomic
from asxbot.log import get_logger

log = get_logger("asxbot.backtest")


def _coverage(arc_dir: Path, codes: list[str]) -> tuple[str, float]:
    have = {p.stem.rstrip("_") for p in arc_dir.glob("*.parquet")} if arc_dir.exists() else set()
    n = len(set(codes) & have)
    frac = n / len(codes) if codes else 0.0
    return f"{n}/{len(codes)} codes have an archive file ({frac:.0%})", frac


def run_phase1(
    cfg: Config,
    universes: list[str] | None = None,
    out: Path | None = None,
    in_sample_only: bool = False,
) -> Path:
    """`in_sample_only` stops every simulation at the start of the holdout.

    The holdout is not a parameter and is not being changed: it stays the last
    `backtest.holdout_years` years. This only decides whether those years are simulated at
    all, so a run can be read without the out-of-sample period having been looked at.
    """
    store = get_store(cfg)
    start = cfg.get("data.price_history_start")
    bm = build_benchmark(
        store, start, cfg.get("backtest.benchmark_ticker"), cfg.get("backtest.index_ticker")
    )
    a, b = build_universes(cfg.data_dir, cfg.get("collector.user_agent"))
    unis = {"asx300": a, "small": b}
    if universes:
        unis = {k: v for k, v in unis.items() if k in universes}
    cap = float(cfg.get("capital.starting_aud"))
    maxpos = int(cfg.get("capital.max_positions"))
    st = cfg.get("strategy")
    bl = cfg.get("baseline")
    uni_cfg = cfg.get("universe")
    small = int(cfg.get("backtest.small_sample_trades", 30))
    last = bm.total_return.index.max()
    oos_start = last - pd.DateOffset(years=int(cfg.get("backtest.holdout_years", 3)))
    arc_dir = cfg.data_dir / "announcements" / "history"
    archive = HistoryArchive.__new__(HistoryArchive)
    archive.dir = arc_dir

    rin = ReportInput(
        data_label=store.provider.label,
        provider_name=store.provider.name,
        survivorship_safe=store.provider.survivorship_safe,
        benchmark_note=bm.note,
        benchmark_equity=cap * bm.total_return / bm.total_return.iloc[0],
        universes={k: v.source for k, v in unis.items()},
        universe_sizes={k: len(v.codes) for k, v in unis.items()},
        announcement_coverage="",
        params={
            "gap_pct_vs_index (X)": st["gap_pct_vs_index"],
            "volume_multiple (Y)": st["volume_multiple"],
            "entry": f"{st['entry']} (main); {st['entry_upper_bound']} (upper bound only)",
            "hold_days": st["hold_days"],
            "stop_loss_pct": st["stop_loss_pct"],
            "turnover_floor_aud": uni_cfg["turnover_floor_aud"],
            "baseline": f"{bl['lookback_months']}-{bl['skip_months']} momentum, top {bl['top_n']}, "
            f"{bl['regime_sma_days']}d regime",
            "costs": f"{cfg.get('costs.brokerage_pct')}% min "
            f"${cfg.get('costs.brokerage_min_aud')}; slippage {cfg.get('costs.slippage')}",
            "priority when slots are full": "highest volume multiple first",
            "minimum order": f"${cfg.get('limits.min_order_aud', 500)} (ours, not an ASX rule)",
        },
        oos_start=oos_start,
        in_sample_only=in_sample_only,
    )
    cov_notes = []
    out_dir = cfg.data_dir / "backtest"
    out_dir.mkdir(parents=True, exist_ok=True)

    for uname, u in unis.items():
        log.info("universe %s: loading %d tickers from cache", uname, len(u.codes))
        frames = store.get_many(u.codes, start, max_age_days=10_000)
        panels = build_panels(
            frames, bm.index_open, bm.index_close,
            float(uni_cfg["turnover_floor_aud"]), int(uni_cfg["turnover_window_days"]),
        )  # fmt: skip
        ev_b = detect_events(
            panels, float(st["gap_pct_vs_index"]), float(st["volume_multiple"]),
            int(st["volume_window_days"]),
        )  # fmt: skip
        ann = archive.load_all(u.codes)
        cov, frac = _coverage(arc_dir, u.codes)
        span = ""
        if len(ann):
            span = (
                f", {len(ann):,} rows {str(ann['released_at'].min())[:10]}"
                f" to {str(ann['released_at'].max())[:10]}"
            )
        cov_notes.append(f"{uname}: {cov}{span}")
        ev_a = announcement_events(ev_b, ann, panels.dates)
        log.info(
            "%s: %d B events, %d A events, %d announcements", uname, len(ev_b), len(ev_a), len(ann)
        )
        if frac < 0.95:
            rin.warnings.append(
                f"Universe {uname}: announcement archive is incomplete ({cov}). Strategy A "
                "results cover only archived codes; rerun when the archive finishes."
            )
        ev_b.to_csv(out_dir / f"events_B_{uname}.csv", index=False)
        ev_a.to_csv(out_dir / f"events_A_{uname}.csv", index=False)

        for mult in cfg.get("costs.slippage_multipliers", [1, 2]):
            costs = CostModel.from_config(cfg, multiplier=float(mult))
            common = dict(
                costs=costs, starting_capital=cap, max_positions=maxpos,
                hold_days=int(st["hold_days"]), stop_pct=float(st["stop_loss_pct"]),
                min_order_aud=float(cfg.get("limits.min_order_aud", 500)),
                end=oos_start if in_sample_only else None,
            )  # fmt: skip
            runs: list[tuple[str, str, Result]] = []
            r = simulate(panels, ev_a, entry_lag=1, name="A_drift_next_open", **common)
            runs.append(("A post-announcement drift", "(entry next open, MAIN)", r))
            r = simulate(
                panels, ev_a[ev_a["pre_open"].astype(bool)] if len(ev_a) else ev_a,
                entry_lag=0, name="A_drift_same_day_UPPER_BOUND", **common,
            )  # fmt: skip
            runs.append(
                (
                    "A post-announcement drift",
                    "(same-day open, UPPER BOUND: look-ahead on volume)",
                    r,
                )
            )
            r = simulate(panels, ev_b, entry_lag=1, name="B_volume_surprise_next_open", **common)
            runs.append(("B volume-confirmed surprise", "(entry next open, MAIN)", r))
            for strat, note, res in runs:
                rin.blocks.append(Block(uname, strat, float(mult), res, note))
                if len(res.trades):
                    write_csv_atomic(
                        res.trades, out_dir / f"trades_{res.name}_{uname}_x{mult}.csv", index=False
                    )
            base = run_momentum(
                panels, costs, cap, int(bl["top_n"]), int(bl["lookback_months"]),
                int(bl["skip_months"]), int(bl["regime_sma_days"]),
                min_order_aud=float(cfg.get("limits.min_order_aud", 500)),
                end=oos_start if in_sample_only else None,
            )  # fmt: skip
            rin.baselines[(uname, float(mult))] = base
            if len(base.trades):
                write_csv_atomic(
                    base.trades, out_dir / f"trades_baseline_{uname}_x{mult}.csv", index=False
                )
            log.info(
                "%s x%s: A=%d B=%d baseline=%d trades",
                uname,
                mult,
                len(runs[0][2].trades),
                len(runs[2][2].trades),
                len(base.trades),
            )

    rin.announcement_coverage = "; ".join(cov_notes)
    text = render(rin, small)
    out = out or (cfg.root / "reports" / "phase1.md")
    write_text_atomic(text, out)
    log.info("report written: %s  [%s]", out, store.provider.label)
    return out
