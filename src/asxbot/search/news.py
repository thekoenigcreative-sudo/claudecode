"""Announcement types for the search, from HEADLINE RULES ONLY (no model; CLAUDE.md: no AI
classification of historical announcements).

The base type is the repo's own classifier (announcements/model.classify_headline). On top of
it, a few finer buckets the brief asks for, also keyword rules, fixed 26 Sep 2026 before any
strategy ran:
  results            the repo's "results"
  guidance_up/down   the repo's "guidance" with an up or down word in the headline
                     (neither: guidance_other)
  contract           the repo's "contract"
  drilling           the repo's "exploration" (drilling, assays, resources)
  takeover           takeover / scheme / bid / offer-for words, whatever the base type
  capital_raising    the repo's "capital_raising"
  other              everything else
"""

from __future__ import annotations

import re

import pandas as pd

from asxbot.announcements.model import classify_headline

UP = re.compile(
    r"upgrade|increase|raise[sd]?\b|record|above|ahead of|strong|improv|exceed|beat|higher|"
    r"reaffirm|upper end",
    re.I,
)
DOWN = re.compile(
    r"downgrade|lower|below|weak|declin|impair|write.?down|writedown|miss|revis|reduc|soft|"
    r"challeng|delay",
    re.I,
)
TAKEOVER = re.compile(
    r"takeover|scheme (of arrangement|implementation|booklet)|bidder|target.s statement|"
    r"non.binding (indicative )?(offer|proposal)|indicative proposal|off.market offer|"
    r"unsolicited (offer|proposal)",
    re.I,
)
BUCKETS = ("results", "guidance_up", "guidance_down", "guidance_other", "contract", "drilling",
           "takeover", "capital_raising", "other")  # fmt: skip


def bucket(headline: str) -> str:
    h = headline or ""
    if TAKEOVER.search(h):
        return "takeover"
    base = classify_headline(h)
    if base == "results":
        return "results"
    if base == "guidance":
        up, down = bool(UP.search(h)), bool(DOWN.search(h))
        if up and not down:
            return "guidance_up"
        if down and not up:
            return "guidance_down"
        return "guidance_other"
    if base == "contract":
        return "contract"
    if base == "exploration":
        return "drilling"
    if base == "capital_raising":
        return "capital_raising"
    return "other"


def enrich(ann: pd.DataFrame) -> pd.DataFrame:
    a = ann.copy()
    if not len(a):
        a["bucket"] = []
        return a
    a["released_at"] = pd.to_datetime(a["released_at"])
    if a["released_at"].dt.tz is not None:
        a["released_at"] = a["released_at"].dt.tz_convert("Australia/Sydney").dt.tz_localize(None)
    a["code"] = a["code"].astype(str).str.upper()
    a["price_sensitive"] = a["price_sensitive"].fillna(False).astype(bool)
    a["bucket"] = a["headline"].fillna("").map(bucket)
    return a.sort_values("released_at").reset_index(drop=True)


__all__ = ["BUCKETS", "bucket", "enrich"]
