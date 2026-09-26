"""A made-up market in the history cache's layout, for tests and for proving the plumbing where
the real history is not available (the cloud session, 26 Sep: no data could be reached).
Nothing produced from it is a result: every report built on it says SYNTHETIC."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from asxbot.lab.tsim.market import SYD

LABEL = "SYNTHETIC DATA - plumbing test only, not a result"


def _day_frame(rng, day: date, code: str, prev_close: float, drift: float, vol_pd: float,
               daily_volume: float, gap: float = 0.0) -> pd.DataFrame:  # fmt: skip
    minutes = [-1, *range(0, 360), 370]  # 09:59 is the opening auction (as IBKR stamps it)
    n = len(minutes)
    sigma = vol_pd / np.sqrt(n)
    rets = rng.normal(drift / n, sigma, n)
    open_px = prev_close * (1 + gap)
    path = open_px * np.exp(np.cumsum(rets))
    u = np.linspace(-1, 1, n)
    shape = 0.4 + 1.6 * u**2
    shape[0] *= 6  # the opening auction's bar
    shape[-1] *= 12  # the closing auction
    vols = rng.poisson(np.maximum(1, daily_volume * shape / shape.sum()))
    rows = []
    prev = open_px
    for i, (mi, px) in enumerate(zip(minutes, path, strict=True)):
        o = prev if i else open_px
        c = px if mi != 370 else prev
        noise = abs(rng.normal(0, sigma * px)) if mi != 370 else 0
        h, lo = max(o, c) + noise, min(o, c) - noise
        if mi == 370:
            o = h = lo = c
        ts = datetime.combine(day, time(10, 0), tzinfo=SYD) + timedelta(minutes=mi)
        rows.append((ts, round(o, 3), round(h, 3), round(max(lo, 0.001), 3), round(c, 3),
                     float(vols[i])))  # fmt: skip
        prev = c
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    return df.set_index("ts")


def make_history(root, days: list[date], codes: list[str], seed: int = 7,
                 news_every: int = 6) -> pd.DataFrame:  # fmt: skip
    """Writes <root>/<code>/<day>.parquet for every code and day (plus ^AXJO) and returns a
    matching announcements frame (a few price-sensitive items with real-looking timestamps).
    Some stocks get news-driven gaps so scanners have something to find."""
    from pathlib import Path

    rng = np.random.default_rng(seed)
    root = Path(root)
    ann = []
    prices = {c: float(rng.uniform(0.2, 40.0)) for c in codes}
    prices["^AXJO"] = 8800.0
    volumes = {c: float(rng.uniform(2e5, 8e6)) for c in codes}
    volumes["^AXJO"] = 5e8
    for di, d in enumerate(days):
        mkt = rng.normal(0, 0.008)
        for ci, c in enumerate([*codes, "^AXJO"]):
            gap, drift, volmult = 0.0, mkt, 1.0
            if c != "^AXJO" and (ci + di) % news_every == 0:
                gap = float(rng.choice([-1, 1]) * rng.uniform(0.02, 0.10))
                drift += gap * 0.5
                volmult = 4.0
                released = datetime.combine(d, time(8, 30), tzinfo=SYD) + timedelta(
                    minutes=int(rng.integers(0, 60)))  # fmt: skip
                ann.append({
                    "code": c, "released_at": released.replace(tzinfo=None),
                    "release_date": d.isoformat(),
                    "headline": "Quarterly Activities Report" if gap > 0 else "Trading Update",
                    "type": "periodic" if gap > 0 else "guidance", "price_sensitive": True,
                    "pre_open": True, "ids_id": f"{c}{d:%m%d}", "pdf_url": "", "pages": 2,
                    "size": "",
                })  # fmt: skip
            df = _day_frame(rng, d, c, prices[c], drift, 0.02 if c != "^AXJO" else 0.008,
                            volumes[c] * volmult, gap)  # fmt: skip
            out = root / c / f"{d.isoformat()}.parquet"
            out.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(out)
            prices[c] = float(df["close"].iloc[-1])
    return pd.DataFrame(ann)
