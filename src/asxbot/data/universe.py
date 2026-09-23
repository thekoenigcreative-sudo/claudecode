"""Universes.

(a) asx300: the S&P/ASX 300. Point-in-time membership only once Norgate is in. Until then,
    a PROXY: the 300 largest companies by market cap in the ASX's own listed-companies
    directory, today. Survivorship-biased and current-only; labelled as such.
    Override with data/universe/asx300_manual.csv (one code per line, header "code").
(b) small: every other company in the directory.

Both are filtered by a point-in-time turnover floor: on day t a stock is eligible only if its
median close*volume over the previous `window` sessions (t-window .. t-1) is >= the floor.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from asxbot.io import write_csv_atomic
from asxbot.log import get_logger

log = get_logger("asxbot.data.universe")

DIRECTORY_URLS = [
    # (url, kind). The markit file carries market cap; the classic CSV is the fallback.
    (
        "https://asx.api.markitdigital.com/asx-research/1.0/companies/directory/file"
        "?access_token=83ff96335c2d45a094df02a206a39ff4",
        "markit",
    ),
    ("https://www.asx.com.au/asx/research/ASXListedCompanies.csv", "classic"),
]

ASX300_SIZE = 300
ASX200_SIZE = 200

# The ASX 200 short universe (23 Sep). It used to be "the 200 largest by market cap in
# today's directory", which is NOT the index: on 23 September that proxy held only 175 of
# the 200 genuine constituents, excluded members like TUA and ORA, and admitted 25
# non-members - foreign listings, LICs and recent risers. The arena refuses shorts outside
# this set, so every wrong entry refuses a trade without ever erroring.
ASX200_MEMBERS = "asx200_members.csv"  # the constituent list, refreshed and dated
ASX200_MANUAL = "asx200_manual.csv"  # a human override, never overwritten by code
ASX200_MIN = 190  # fewer than this and the list is not the index; say so, loudly
ASX200_MAX_AGE_DAYS = 8  # one rebalance cycle is a quarter; a week keeps drift small
ASX200_WIKI = "https://en.wikipedia.org/wiki/S%26P/ASX_200"


@dataclass
class Universe:
    name: str
    codes: list[str]
    source: str  # human-readable provenance, printed in reports
    as_of: date


def fetch_directory(data_dir: Path, user_agent: str, max_age_days: int = 7) -> pd.DataFrame:
    """ASX listed companies: code, name, industry, market_cap (NaN if unavailable). Cached."""
    udir = Path(data_dir) / "universe"
    udir.mkdir(parents=True, exist_ok=True)
    cache = udir / "asx_directory.csv"
    if cache.exists():
        age = (date.today() - date.fromtimestamp(cache.stat().st_mtime)).days
        if age <= max_age_days:
            return pd.read_csv(cache)
    last_err: Exception | None = None
    for url, kind in DIRECTORY_URLS:
        try:
            r = requests.get(url, headers={"User-Agent": user_agent}, timeout=60)
            r.raise_for_status()
            df = parse_directory(r.text, kind)
            df["source"] = kind
            write_csv_atomic(df, cache, index=False)
            log.info("directory: %d companies from %s", len(df), kind)
            return df
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("directory source %s failed: %s", kind, e)
    if cache.exists():
        log.warning("using stale directory cache")
        return pd.read_csv(cache)
    raise RuntimeError(f"could not fetch ASX directory: {last_err}")


def parse_directory(text: str, kind: str) -> pd.DataFrame:
    if kind == "classic":
        # first line is a title, then the header
        lines = text.splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.startswith("Company name"))
        df = pd.read_csv(io.StringIO("\n".join(lines[start:])))
        df = df.rename(
            columns={"Company name": "name", "ASX code": "code", "GICS industry group": "industry"}
        )
        df["market_cap"] = float("nan")
        df["listing_date"] = pd.NaT
    else:
        df = pd.read_csv(io.StringIO(text))
        df = df.rename(
            columns={
                "ASX code": "code",
                "Company name": "name",
                "GICs industry group": "industry",
                "Listing date": "listing_date",
                "Market Cap": "market_cap",
            }
        )
        df["listing_date"] = pd.to_datetime(df["listing_date"], dayfirst=True, errors="coerce")
        df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
    df["code"] = df["code"].astype(str).str.strip().str.upper()
    df = df[df["code"].str.fullmatch(r"[A-Z0-9]{3}")]  # ordinary shares only
    return df[["code", "name", "industry", "listing_date", "market_cap"]].reset_index(drop=True)


def build_universes(data_dir: Path, user_agent: str) -> tuple[Universe, Universe]:
    directory = fetch_directory(data_dir, user_agent)
    today = date.today()
    manual = Path(data_dir) / "universe" / "asx300_manual.csv"
    if manual.exists():
        codes = pd.read_csv(manual)["code"].astype(str).str.upper().tolist()
        src = f"manual list {manual.name} ({len(codes)} codes)"
    elif directory["market_cap"].notna().any():
        top = directory.dropna(subset=["market_cap"]).nlargest(ASX300_SIZE, "market_cap")
        codes = top["code"].tolist()
        src = "PROXY: top 300 by market cap in ASX directory today (survivorship-biased)"
    else:
        raise RuntimeError(
            "no ASX 300 source: directory has no market caps and no asx300_manual.csv"
        )
    a = Universe("asx300", sorted(codes), src, today)
    rest = sorted(set(directory["code"]) - set(codes))
    b = Universe("small", rest, "ASX directory minus asx300 (current listings only)", today)
    log.info("universes: asx300=%d small=%d", len(a.codes), len(b.codes))
    return a, b


@dataclass
class ShortUniverse:
    """The set the arena will allow a short in, and exactly where it came from."""

    codes: set[str]
    source: str
    as_of: date | None
    is_index_list: bool  # True only for a real constituent list, never for the proxy

    @property
    def age_days(self) -> int | None:
        return None if self.as_of is None else (date.today() - self.as_of).days

    @property
    def stale(self) -> bool:
        age = self.age_days
        return age is None or age > ASX200_MAX_AGE_DAYS

    @property
    def too_small(self) -> bool:
        return len(self.codes) < ASX200_MIN


def asx200_status(data_dir: Path, user_agent: str) -> ShortUniverse:
    """Where the short universe comes from, with its provenance attached.

    Order: a human override, then the dated constituent list, then - only if neither
    exists - the old market-cap proxy, which is reported as what it is rather than passed
    off as the index. Nothing here decides silently: `is_index_list`, `stale` and
    `too_small` are what the self-check reads.
    """
    udir = Path(data_dir) / "universe"
    manual = udir / ASX200_MANUAL
    if manual.exists():
        codes = set(pd.read_csv(manual)["code"].astype(str).str.strip().str.upper())
        as_of = date.fromtimestamp(manual.stat().st_mtime)
        return ShortUniverse(codes, f"manual override {ASX200_MANUAL}", as_of, True)

    members = udir / ASX200_MEMBERS
    if members.exists():
        df = pd.read_csv(members)
        codes = set(df["code"].astype(str).str.strip().str.upper())
        as_of = None
        if "as_of" in df.columns and len(df):
            try:
                as_of = date.fromisoformat(str(df["as_of"].iloc[0])[:10])
            except ValueError:
                as_of = None
        src = str(df["source"].iloc[0]) if "source" in df.columns and len(df) else ASX200_MEMBERS
        return ShortUniverse(codes, f"{src} ({ASX200_MEMBERS})", as_of, True)

    directory = fetch_directory(data_dir, user_agent)
    if not directory["market_cap"].notna().any():
        log.error(
            "no ASX 200 constituent list and no market caps: the short universe is EMPTY, "
            "so every short will be refused. Run: asxbot universe asx200 --refresh"
        )
        return ShortUniverse(set(), "nothing (no list, no market caps)", None, False)
    top = directory.dropna(subset=["market_cap"]).nlargest(ASX200_SIZE, "market_cap")
    log.error(
        "no ASX 200 constituent list: falling back to the top %d by market cap, which is "
        "NOT the index and will refuse shorts in real members. "
        "Run: asxbot universe asx200 --refresh",
        ASX200_SIZE,
    )
    return ShortUniverse(
        set(top["code"].astype(str).str.upper()),
        "PROXY: top 200 by market cap (NOT the index)",
        date.today(),
        False,
    )


def asx200_codes(data_dir: Path, user_agent: str) -> set[str]:
    """The set the arena allows shorts in (ARENA.md: shorts only in the ASX 200)."""
    return asx200_status(data_dir, user_agent).codes


def refresh_asx200(data_dir: Path, user_agent: str, url: str = ASX200_WIKI) -> ShortUniverse:
    """Fetch the current constituent list and write it, dated, after validating it.

    A stopgap until Norgate provides point-in-time membership: this is today's list applied
    to today only, which is all the arena needs to decide whether a short is allowed. It is
    validated before it is written - a short, malformed or unrecognisable list is refused
    rather than saved, because a bad list here refuses trades without erroring.
    """
    r = requests.get(url, headers={"User-Agent": user_agent}, timeout=60)
    r.raise_for_status()
    tables = pd.read_html(io.StringIO(r.text))
    wanted = [t for t in tables if list(t.columns)[:2] == ["Code", "Company"]]
    if not wanted:
        raise RuntimeError(f"no constituent table at {url}")
    df = wanted[0]
    df["code"] = df["Code"].astype(str).str.strip().str.upper()
    df = df[df["code"].str.fullmatch(r"[A-Z0-9]{3}")]
    codes = sorted(set(df["code"]))
    if len(codes) < ASX200_MIN:
        raise RuntimeError(f"{url} gave only {len(codes)} codes; refusing to save it")

    # Cross-check against the ASX's own directory: a list that barely overlaps it is not
    # an ASX index, whatever the page said.
    directory = fetch_directory(data_dir, user_agent)
    known = set(directory["code"].astype(str).str.upper())
    overlap = len(set(codes) & known) / len(codes)
    if overlap < 0.8:
        raise RuntimeError(
            f"only {overlap:.0%} of those codes are in the ASX directory; refusing to save"
        )

    out = pd.DataFrame(
        {
            "code": codes,
            "name": [
                str(df[df["code"] == c]["Company"].iloc[0])[:80] for c in codes
            ],
            "as_of": date.today().isoformat(),
            "source": url,
        }
    )
    udir = Path(data_dir) / "universe"
    udir.mkdir(parents=True, exist_ok=True)
    write_csv_atomic(out, udir / ASX200_MEMBERS, index=False)
    log.info("ASX 200 constituent list refreshed: %d codes from %s", len(codes), url)
    return asx200_status(data_dir, user_agent)


def turnover_eligibility(
    close: pd.DataFrame, volume: pd.DataFrame, floor_aud: float, window: int
) -> pd.DataFrame:
    """Boolean dates x tickers. True on day t if median(close*volume) over the *previous*
    `window` sessions is >= floor. Uses only information available before t's open."""
    dollar = (close * volume).astype("float64")
    med = dollar.rolling(window, min_periods=window).median().shift(1)
    return med >= floor_aud


def dollar_turnover_avg(close: pd.DataFrame, volume: pd.DataFrame, window: int) -> pd.DataFrame:
    """Mean dollar turnover over the previous `window` sessions (for slippage scaling)."""
    return (close * volume).rolling(window, min_periods=max(5, window // 2)).mean().shift(1)
