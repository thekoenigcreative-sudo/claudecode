"""Event detection. Parameters frozen 2026-09-22; see config.yaml `strategy`.

Event on session t for ticker s (strategy B, "drift after a volume-confirmed surprise"):
    gap_rel(t)  = (open_t / close_{t-1} - 1) - (idx_open_t / idx_close_{t-1} - 1)  >= X%
    vol_mult(t) = volume_t / mean(volume_{t-20..t-1})                               >= Y
    eligible(t) = median dollar turnover over t-20..t-1 >= floor
The event is CONFIRMED at the close of t (volume is not known before then).
Main entry: open of t+1. Upper bound (labelled, look-ahead on volume): open of t.

Strategy A adds: a price-sensitive announcement by s whose reaction session is t, where the
reaction session is the release day if released before 10:00, else the next session.
Rows on a company's page that are another company's announcement (headline prefixed with
that other code) are dropped mechanically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
import pandas as pd

_FOREIGN = re.compile(r"^\s*([A-Z0-9]{3,4})(?:'s ann)?\s*:\s")


@dataclass
class Panels:
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    index_open: pd.Series
    index_close: pd.Series
    eligible: pd.DataFrame  # bool, point-in-time
    adv_dollar: pd.DataFrame  # prior-window mean dollar turnover

    @property
    def dates(self) -> pd.DatetimeIndex:
        return self.close.index


def build_panels(
    frames: dict[str, pd.DataFrame],
    index_open: pd.Series,
    index_close: pd.Series,
    turnover_floor: float,
    turnover_window: int,
) -> Panels:
    from asxbot.data.store import PriceStore
    from asxbot.data.universe import dollar_turnover_avg, turnover_eligibility

    frames = {t: f for t, f in frames.items() if len(f) > turnover_window + 5}
    o = PriceStore.panel(frames, "open")
    h = PriceStore.panel(frames, "high")
    lo = PriceStore.panel(frames, "low")
    c = PriceStore.panel(frames, "close")
    v = PriceStore.panel(frames, "volume")
    dates = c.index.union(index_close.index)
    o, h, lo, c, v = (x.reindex(dates) for x in (o, h, lo, c, v))
    idx_c = index_close.reindex(dates)
    idx_o = index_open.reindex(dates)
    return Panels(
        open=o,
        high=h,
        low=lo,
        close=c,
        volume=v,
        index_open=idx_o,
        index_close=idx_c,
        eligible=turnover_eligibility(c, v, turnover_floor, turnover_window),
        adv_dollar=dollar_turnover_avg(c, v, turnover_window),
    )


def detect_events(p: Panels, gap_pct: float, vol_mult: float, vol_window: int) -> pd.DataFrame:
    """Strategy-B events. Columns: date, ticker, gap_rel, vol_mult. Sorted by date."""
    prev_close = p.close.shift(1)
    gap = p.open / prev_close - 1
    idx_gap = (p.index_open / p.index_close.shift(1) - 1).reindex(p.dates)
    gap_rel = gap.sub(idx_gap, axis=0) * 100
    avg_vol = p.volume.shift(1).rolling(vol_window, min_periods=vol_window).mean()
    vmult = p.volume / avg_vol.replace(0, np.nan)
    hit = (gap_rel >= gap_pct) & (vmult >= vol_mult) & p.eligible & p.open.notna()
    rows = np.argwhere(hit.to_numpy())
    if len(rows) == 0:
        return pd.DataFrame(columns=["date", "ticker", "gap_rel", "vol_mult"])
    dates = p.dates[rows[:, 0]]
    tickers = p.close.columns[rows[:, 1]]
    out = pd.DataFrame(
        {
            "date": dates,
            "ticker": tickers,
            "gap_rel": gap_rel.to_numpy()[rows[:, 0], rows[:, 1]],
            "vol_mult": vmult.to_numpy()[rows[:, 0], rows[:, 1]],
        }
    )
    return out.sort_values(["date", "vol_mult"], ascending=[True, False]).reset_index(drop=True)


def own_announcements(ann: pd.DataFrame) -> pd.DataFrame:
    """Drop rows that are another company's announcement listed on this company's page."""
    if ann.empty:
        return ann
    prefix = ann["headline"].astype(str).str.extract(_FOREIGN, expand=False)
    foreign = prefix.notna() & (prefix.str.upper() != ann["code"].str.upper())
    return ann[~foreign].copy()


def reaction_sessions(released_at: pd.Series, sessions: pd.DatetimeIndex) -> pd.Series:
    """Map each release timestamp to the session whose open first reflects it."""
    rel = pd.to_datetime(released_at)
    day = rel.dt.normalize()
    after_open = rel.dt.hour >= 10  # released at/after 10:00 -> next session
    # position of first session >= day (or > day when after the open)
    pos = sessions.searchsorted(day.to_numpy(), side="left")
    pos_after = sessions.searchsorted(day.to_numpy(), side="right")
    idx = np.where(after_open.to_numpy(), pos_after, pos)
    idx = np.clip(idx, 0, len(sessions) - 1)
    out = pd.Series(sessions[idx], index=released_at.index)
    # if the release day is beyond the last session, mark NaT
    out[idx >= len(sessions)] = pd.NaT
    return out


def announcement_events(
    events_b: pd.DataFrame, ann: pd.DataFrame, sessions: pd.DatetimeIndex
) -> pd.DataFrame:
    """Strategy-A events: B events whose ticker has a price-sensitive announcement with
    reaction session == event date. Adds headline/type/ids_id/pre_open of that announcement
    (the earliest one on that session if several)."""
    if events_b.empty or ann.empty:
        return events_b.iloc[0:0].assign(headline=None, ann_type=None, ids_id=None, pre_open=None)
    a = own_announcements(ann)
    a = a[a["price_sensitive"].astype(bool)].copy()
    a["reaction"] = reaction_sessions(a["released_at"], sessions)
    a = a.dropna(subset=["reaction"]).sort_values("released_at")
    a = a.drop_duplicates(["code", "reaction"], keep="first")
    a = a.rename(columns={"code": "ticker", "reaction": "date", "type": "ann_type"})
    merged = events_b.merge(
        a[["ticker", "date", "headline", "ann_type", "ids_id", "pre_open"]],
        on=["ticker", "date"],
        how="inner",
    )
    return merged.sort_values(["date", "vol_mult"], ascending=[True, False]).reset_index(drop=True)
