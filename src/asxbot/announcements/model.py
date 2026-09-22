"""Announcement record and the MECHANICAL type classifier.

The classifier is keyword rules on the headline only. No AI, no price data, no hindsight.
It exists so reports can break results down by announcement type; it is deliberately crude.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime

import pandas as pd

BASE_URL = "https://www.asx.com.au"

# (type, regex). First match wins. Order matters: specific before generic.
TYPE_RULES: list[tuple[str, str]] = [
    ("trading_halt", r"trading halt|suspension|reinstatement"),
    ("director_interest", r"director.{0,3}s? interest|appendix 3[xyz]"),
    ("substantial_holding", r"substantial holder|substantial holding|form 60[345]"),
    (
        "capital_raising",
        r"placement|capital rais|entitlement offer|rights issue|share purchase plan|spp|"
        r"cleansing|appendix 3b|proposed issue of securities|prospectus",
    ),
    (
        "results",
        r"half.year|full.year|annual report|quarterly|4c\b|appendix 4[cde]|results|"
        r"financial report|interim report|preliminary final",
    ),
    ("guidance", r"guidance|outlook|trading update|earnings update|profit"),
    ("acquisition", r"acqui|merger|takeover|scheme of arrangement|divest|sale of|disposal"),
    ("contract", r"contract|agreement|award|order|partnership|mou|memorandum|licen[cs]e"),
    (
        "exploration",
        r"drill|assay|resource|reserve|intersect|mineralis|discovery|exploration|"
        r"metallurg|jorc|ore",
    ),
    ("clinical", r"clinical|trial|fda|tga|phase [123i]|patient|approval"),
    ("agm", r"agm|general meeting|notice of meeting|chairman.{0,3}s address|presentation"),
    ("dividend", r"dividend|distribution"),
    ("investor_presentation", r"investor|presentation|roadshow|webinar"),
    ("ceased_to_be", r"ceasing to be|cease"),
    ("response_to_asx", r"response to asx|asx query|aware letter|price query"),
]
_COMPILED = [(t, re.compile(rx, re.I)) for t, rx in TYPE_RULES]


def classify_headline(headline: str) -> str:
    h = headline or ""
    for t, rx in _COMPILED:
        if rx.search(h):
            return t
    return "other"


@dataclass
class Announcement:
    code: str
    released_at: datetime  # Sydney local, tz-naive
    headline: str
    price_sensitive: bool
    ids_id: str
    pdf_url: str
    pages: int | None = None
    size: str | None = None

    @property
    def type(self) -> str:
        return classify_headline(self.headline)

    @property
    def pre_open(self) -> bool:
        """Released before the 10:00 open (so a same-day-open entry is conceivable)."""
        return self.released_at.hour < 10

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type
        d["release_date"] = self.released_at.date().isoformat()
        d["pre_open"] = self.pre_open
        return d


def to_frame(items: list[Announcement]) -> pd.DataFrame:
    cols = [
        "code",
        "released_at",
        "release_date",
        "headline",
        "type",
        "price_sensitive",
        "pre_open",
        "ids_id",
        "pdf_url",
        "pages",
        "size",
    ]
    if not items:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame([a.to_dict() for a in items])
    df["released_at"] = pd.to_datetime(df["released_at"])
    return df[cols].sort_values(["released_at", "code"]).reset_index(drop=True)
