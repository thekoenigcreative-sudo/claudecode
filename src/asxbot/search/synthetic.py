"""A SYNTHETIC market for plumbing tests: random-walk minute bars in the IBKR history layout,
an index, and random announcements. There is no edge in it by construction (every price is
a driftless random walk, news carries no information), so any strategy run on it must lose
about its costs. A strategy that makes money here is a bug in the engine (look-ahead, a fill
the bars could not give), not a finding. Nothing produced from this is a result.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

SYD = ZoneInfo("Australia/Sydney")


def sessions(first: date, last: date) -> list[date]:
    from asxbot.arena.replay_ibkr import sessions as xs

    return xs(first, last)


def make(root: Path, first: date = date(2026, 3, 26), last: date = date(2026, 9, 25),
         n_codes: int = 40, seed: int = 7) -> tuple[Path, Path]:  # fmt: skip
    """Write history to root/history and announcements/universe to root/data. Returns both."""
    rng = np.random.default_rng(seed)
    hist, data = Path(root) / "history", Path(root) / "data"
    days = sessions(first, last)
    letters = "ABCDEFGHJKLMNPRSTWZ"
    codes = sorted({"".join(rng.choice(list(letters), 3)) for _ in range(n_codes * 2)})[:n_codes]
    price = {c: float(rng.choice([0.4, 1.2, 3.5, 12.0, 45.0])) * float(rng.uniform(0.8, 1.2))
             for c in codes}  # fmt: skip
    base_vol = {c: float(rng.uniform(2e3, 4e5)) / max(price[c], 0.5) * 20 for c in codes}
    idx = 8000.0
    ann = []
    for d in days:
        ix_rows = []
        t0 = datetime.combine(d, time(10, 0), tzinfo=SYD)
        # the index: 10:00 .. 16:10
        for k in range(371):
            o = idx
            idx *= float(np.exp(rng.normal(0, 0.0004)))
            ix_rows.append((t0 + timedelta(minutes=k), o, max(o, idx), min(o, idx), idx, 0.0))
        _write(hist / "^AXJO", d, ix_rows)
        for c in codes:
            group = "ABCDEFGHJKLMNPRSTWZ".index(c[0]) // 4  # staggered open, 0..4
            first_m = [0, 2, 4, 6, 9][min(group, 4)]
            p = price[c] * float(np.exp(rng.normal(0, 0.012)))  # overnight gap
            news = rng.random() < 0.02
            if news:
                ann.append(_ann(c, d, rng))
            day_mult = float(rng.lognormal(0, 0.5)) * (3.0 if news else 1.0)
            sig = 0.0015 if price[c] > 2 else 0.003
            rows = [(t0 - timedelta(minutes=1), p, p, p, p,
                     float(rng.poisson(base_vol[c] * day_mult * 0.3)))]  # the opening auction
            for k in range(first_m, 360):
                o = p
                p = max(0.005, p * float(np.exp(rng.normal(0, sig))))
                hi = max(o, p) * (1 + abs(rng.normal(0, sig / 3)))
                lo = min(o, p) * (1 - abs(rng.normal(0, sig / 3)))
                u = 1.0 + (3.0 if k - first_m < 15 else 0.0) + (2.0 if k > 330 else 0.0)
                lam = base_vol[c] * u * day_mult / 60
                v = 0.0 if rng.random() < 0.15 else float(rng.poisson(lam))
                rows.append((t0 + timedelta(minutes=k), o, hi, lo, p, v))
            auc = float(rng.poisson(base_vol[c] * day_mult * 0.8))
            p_auc = p * float(np.exp(rng.normal(0, sig)))
            rows.append((t0 + timedelta(minutes=370), p_auc, p_auc, p_auc, p_auc, auc))
            price[c] = p_auc
            _write(hist / c, d, rows)
    a = pd.DataFrame(ann)
    (data / "announcements" / "history").mkdir(parents=True, exist_ok=True)
    a.to_parquet(data / "announcements" / "history" / "synthetic.parquet")
    return hist, data


HEADLINES = ["Half Year Results", "Quarterly Activities Report", "Guidance upgrade",
             "Earnings guidance lower than expected", "Contract award", "Drilling results",
             "Takeover offer", "Placement to raise $5m", "Change of director"]  # fmt: skip


def _ann(code: str, d: date, rng) -> dict:
    at = datetime.combine(d, time(8, 30)) + timedelta(minutes=int(rng.integers(0, 420)))
    h = str(rng.choice(HEADLINES))
    return {"code": code, "released_at": pd.Timestamp(at), "release_date": d.isoformat(),
            "headline": h, "type": "", "price_sensitive": bool(rng.random() < 0.8),
            "pre_open": at.hour < 10, "ids_id": f"{code}{d:%m%d}{int(rng.integers(1e6))}",
            "pdf_url": "", "pages": 1, "size": ""}  # fmt: skip


def _write(folder: Path, d: date, rows) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.set_index("ts")
    df.index.name = None
    df.to_parquet(folder / f"{d.isoformat()}.parquet")


__all__ = ["make"]
