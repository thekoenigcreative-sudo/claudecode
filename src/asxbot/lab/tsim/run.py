"""Running the simulator: days in date order with ONE account (continuity: cash, positions,
GTC orders, alerts and the journal carry from each simulated day to the next, like a real
account), and independent runs (different strategies, different samples) in parallel.

A run lives in <lab_local>/tsim/runs/<run_id>/: spec.json (what ran, on what days, disguised or
not), state.json (the account and alerts after the last finished day - a run resumes from it),
days/<day>.json (each day's result: P&L, fills, wakes, tokens, think time), the AI's call log
(calls/<day>.jsonl) and its journal (journal/).
"""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from asxbot.lab import store
from asxbot.lab.tsim.alerts import AlertBook
from asxbot.lab.tsim.anon import NoAnon, RunAnon
from asxbot.lab.tsim.broker import Account, SimBroker
from asxbot.lab.tsim.costs import CostModel
from asxbot.lab.tsim.engine import run_day
from asxbot.lab.tsim.journal import Journal
from asxbot.lab.tsim.market import INDEX, History, Market
from asxbot.lab.tsim.summaries import Summaries
from asxbot.lab.tsim.tools import TraderView

CUTOFF = date(2026, 7, 1)  # the models' knowledge cutoff (lab/splits.CUTOFF)
START_CASH = 20_000.0


def tsim_local() -> Path:
    return store.lab_local() / "tsim"


def history_root() -> Path:
    from asxbot.arena.replay_ibkr import history_root as hr

    return hr()


@dataclass
class Inputs:
    """Everything a day needs that does not change with the strategy."""

    history_root: str
    summaries_dir: str
    codes: list = field(default_factory=list)
    shortable: list = field(default_factory=list)
    ann_path: str = ""  # a parquet of announcements covering the run (plus a few days before)
    news_coverage: dict = field(default_factory=dict)  # day -> "full" | "partial" | "none"

    def history(self) -> History:
        return History(Path(self.history_root))

    def summaries(self) -> Summaries:
        return Summaries(Path(self.summaries_dir))

    def announcements(self) -> pd.DataFrame:
        if self.ann_path and Path(self.ann_path).exists():
            return pd.read_parquet(self.ann_path)
        return pd.DataFrame(columns=["code", "released_at", "release_date", "headline", "type",
                                     "price_sensitive", "ids_id"])  # fmt: skip


def prepare_inputs(cfg, days: list[date], history: Path | None = None,
                   workers: int = 4, ann: pd.DataFrame | None = None) -> Inputs:  # fmt: skip
    """Build (or update) the daily summaries and write the run's announcements once."""
    from datetime import timedelta

    hroot = Path(history or history_root())
    hist = History(hroot)
    codes = [c for c in hist.codes() if c != INDEX]
    summ = Summaries(tsim_local() / "summary")
    summ.build(hist, [*codes, INDEX], workers=workers)
    shortable: list[str] = []
    if cfg is not None:
        try:
            from asxbot.data.universe import asx200_codes

            shortable = sorted(asx200_codes(cfg.data_dir, cfg.get("collector.user_agent")))
        except Exception:  # noqa: BLE001 - no list: nothing is shortable
            shortable = []
    if ann is None and cfg is not None:
        from asxbot.arena.replay_ibkr import announcements

        ann = announcements(cfg, min(days) - timedelta(days=7), max(days))
    ann = ann if ann is not None else pd.DataFrame()
    key = hashlib.sha1(f"{min(days)}{max(days)}{len(ann)}".encode()).hexdigest()[:10]
    ap = tsim_local() / "inputs" / f"ann_{key}.parquet"
    if len(ann):
        ap.parent.mkdir(parents=True, exist_ok=True)
        a = ann.copy()
        a["ids_id"] = a["ids_id"].astype(str)
        a.to_parquet(ap)
    from asxbot.lab.tsim.coverage import news_coverage

    cov = news_coverage(cfg, days) if cfg is not None else {}
    return Inputs(str(hroot), str(summ.dir), codes, shortable, str(ap) if len(ann) else "",
                  {d.isoformat(): v for d, v in cov.items()})  # fmt: skip


def needs_disguise(days: list[date]) -> bool:
    return any(d < CUTOFF for d in days)


def nights_until_next(day: date, days: list[date]) -> int:
    later = [d for d in days if d > day]
    if later:
        return (later[0] - day).days
    return 3 if day.weekday() == 4 else 1


def run(
    run_id: str,
    trader_spec: dict,
    days: list[date],
    inputs: Inputs,
    *,
    cfg=None,
    start_cash: float = START_CASH,
    leverage: float = 1.0,
    salt: str = "tsim",
    disguise: bool | None = None,
    broker: str | None = None,
    resume: bool = True,
    ask=None,
    progress=None,
) -> dict:
    """Run one trader over `days` in date order with one account. `trader_spec`:
    {"kind": "rules", "family": ..., "params": {...}} or {"kind": "ai", "addendum": "...",
    "max_looks": 3, "max_calls_per_day": 60, "model": ..., "effort": ...}."""
    days = sorted(days)
    conf = (cfg.get("tradesim") or {}) if cfg is not None else {}
    if start_cash == START_CASH and conf.get("start_cash_aud"):
        start_cash = float(conf["start_cash_aud"])
    if leverage == 1.0 and conf.get("leverage"):
        leverage = float(conf["leverage"])
    rdir = tsim_local() / "runs" / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    disguise = needs_disguise(days) if disguise is None else disguise
    spec = {"run_id": run_id, "trader": trader_spec, "days": [d.isoformat() for d in days],
            "disguised": disguise, "start_cash": start_cash, "leverage": leverage,
            "news_coverage": {d.isoformat(): inputs.news_coverage.get(d.isoformat(), "unknown")
                              for d in days}}  # fmt: skip
    store.write_json(rdir / "spec.json", spec)
    costs = CostModel.from_config(cfg, broker)
    state = store.read_json(rdir / "state.json") if resume else None
    acct = Account.from_dict(state["account"]) if state else Account(
        "sim", start_cash, start_cash, leverage)  # fmt: skip
    book = AlertBook()
    if state and state.get("alerts"):
        book.alerts = state["alerts"]["alerts"]
        book.next_id = state["alerts"]["next_id"]
        book.news_seen = set(state["alerts"].get("news_seen") or [])
    done = set(state["done"]) if state else set()
    anon = RunAnon(f"{salt}:{run_id}", days) if disguise else NoAnon()
    journal = Journal(rdir)
    trader = make_trader(trader_spec, journal=journal, cfg=cfg, ask=ask, log_dir=rdir / "calls")
    if state and state.get("trader_state") is not None and hasattr(trader, "state"):
        trader.state = _restore(state["trader_state"])
    hist, summ = inputs.history(), inputs.summaries()
    ann = inputs.announcements()
    shortable = set(inputs.shortable)
    sb = SimBroker(acct, costs, float(conf.get("max_volume_share", 0.20)), shortable)
    results = []
    for i, d in enumerate(days):
        if d.isoformat() in done:
            results.append(store.read_json(rdir / "days" / f"{d.isoformat()}.json"))
            continue
        m = Market(d, hist, inputs.codes, _ann_window(ann, d), summ, shortable)
        view = TraderView(m, sb, anon, journal, reader=_reader(cfg, d, disguise))
        if hasattr(trader, "bind_alerts"):
            trader.bind_alerts(book, anon)
        r = run_day(m, sb, trader, book, view, nights=nights_until_next(d, days))
        rec = asdict(r)
        rec["news_coverage"] = inputs.news_coverage.get(d.isoformat(), "unknown")
        rec["market_move_pct"] = _index_close_move(m)
        rec["stocks"] = len(m.bars)
        store.write_json(rdir / "days" / f"{d.isoformat()}.json", rec)
        done.add(d.isoformat())
        store.write_json(rdir / "state.json", {
            "account": acct.to_dict(), "done": sorted(done),
            "alerts": {"alerts": book.alerts, "next_id": book.next_id,
                       "news_seen": sorted(book.news_seen)[-5000:]},
            "trader_state": _dump(getattr(trader, "state", None)),
        })  # fmt: skip
        results.append(rec)
        if progress:
            progress(i + 1, len(days), rec)
    store.write_json(rdir / "account.json", acct.to_dict())
    return {"run_id": run_id, "spec": spec, "days": results, "account": acct.to_dict()}


def _dump(x):
    if x is None:
        return None
    return json.loads(json.dumps(x, default=lambda o: sorted(o) if isinstance(o, set) else str(o)))


def _restore(x):
    return x


def _ann_window(ann: pd.DataFrame, d: date) -> pd.DataFrame:
    if not len(ann):
        return ann
    rel = pd.to_datetime(ann["released_at"])
    if rel.dt.tz is not None:
        rel = rel.dt.tz_convert("Australia/Sydney").dt.tz_localize(None)
    lo = pd.Timestamp(d) - pd.Timedelta(days=6)
    hi = pd.Timestamp(d) + pd.Timedelta(days=1)
    return ann[(rel >= lo) & (rel < hi)]


def _index_close_move(m: Market) -> float | None:
    if m.index is None:
        return None
    from asxbot.lab.tsim.market import SLOTS

    last = m.index.last_close(SLOTS)
    prev = m.prev_close(INDEX)
    return None if last is None or not prev else round((last / prev - 1) * 100, 3)


def _reader(cfg, d: date, disguised: bool):
    """The announcement reader (Sonnet), only for days after the cutoff and only when the run
    is not disguised: CLAUDE.md forbids AI classification of historical announcements."""
    if disguised or d < CUTOFF or cfg is None:
        return None
    from asxbot.lab.tsim.reader import cached_reader

    return cached_reader(cfg)


def make_trader(spec: dict, *, journal: Journal, cfg=None, ask=None, log_dir=None):
    kind = spec.get("kind", "rules")
    if kind == "rules":
        from asxbot.lab.tsim.rules import make

        return make(spec)
    if kind == "ai":
        from asxbot.lab.tsim.ai import AITrader

        conf = ((cfg.get("tradesim") or {}).get("ai") or {}) if cfg is not None else {}
        return AITrader(
            journal=journal,
            model=str(spec.get("model") or conf.get("model") or "claude-opus-5-5"),
            effort=str(spec.get("effort") or conf.get("effort") or "high"),
            max_looks=int(spec.get("max_looks") or conf.get("max_looks") or 3),
            max_calls_per_day=int(spec.get("max_calls_per_day") or conf.get("max_calls_per_day")
                                  or 60),
            addendum=str(spec.get("addendum") or ""), cfg=cfg, ask=ask, log_dir=log_dir,
        )  # fmt: skip
    raise ValueError(f"unknown trader kind {kind!r}")


# --------------------------------------------------------------------- in parallel
def _job(args) -> dict:
    run_id, spec, days, inputs, kw = args
    from asxbot.config import load_config

    cfg = None
    if kw.pop("with_cfg", False):
        try:
            cfg = load_config()
        except Exception:  # noqa: BLE001
            cfg = None
    out = run(run_id, spec, [date.fromisoformat(d) for d in days], Inputs(**inputs), cfg=cfg,
              **kw)  # fmt: skip
    from asxbot.lab.tsim.report import score

    return {"run_id": run_id, "score": score(out, cfg)}


def run_many(jobs: list[tuple[str, dict, list[date]]], inputs: Inputs, workers: int | None = None,
             with_cfg: bool = True, **kw) -> list[dict]:  # fmt: skip
    """Independent runs in parallel worker processes (rules traders; each run's days stay in
    order inside its worker)."""
    workers = workers or max(1, min(len(jobs), (os.cpu_count() or 2) - 1))
    args = [(rid, spec, [d.isoformat() for d in ds], asdict(inputs), {**kw, "with_cfg": with_cfg})
            for rid, spec, ds in jobs]  # fmt: skip
    if workers <= 1:
        return [_job(a) for a in args]
    out = []
    with ProcessPoolExecutor(workers) as ex:
        futs = [ex.submit(_job, a) for a in args]
        for f in as_completed(futs):
            out.append(f.result())
    return out
