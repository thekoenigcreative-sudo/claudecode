"""Disguise for a whole simulated RUN inside the decision model's knowledge range (before
2026-07-01): the model may remember what a named ASX stock did on a named day.

Unlike lab/anon.py (one packet, one day), a run carries positions and a journal from day to
day, so the disguise is stable for the run: each code has one alias and one hidden price factor
(0.5-2) for every day of the run; volumes are divided by the same factor so share counts x
prices still give the real dollar values (costs, turnover and position sizes keep their real
economics); dates become "Day N" (the weekday stays: day-of-week effects are not a secret);
headlines keep only generic announcement words (lab/anon.VOCAB), so company, project and
product names are gone. The index is "the index", its level scaled by its own factor.

What the trader sends back (codes, prices, share counts) is translated to the real ones before
the broker sees it. Whether the model can still tell what it is looking at is MEASURED
(contamination.py), not assumed.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import date

from asxbot.lab.anon import Anon as _DayAnon

INDEX_ALIAS = "INDEX"


class RunAnon:
    def __init__(self, salt: str, days: list[date]):
        self.salt = salt.encode()
        self.day_no = {d: i + 1 for i, d in enumerate(sorted(days))}
        self._alias: dict[str, str] = {}
        self._real: dict[str, str] = {}
        self._words = _DayAnon(date(2000, 1, 1), "x", 0)

    def _h(self, code: str) -> str:
        return hmac.new(self.salt, code.upper().encode(), hashlib.sha256).hexdigest()

    def alias(self, code: str) -> str:
        code = code.upper()
        if code == "^AXJO":
            return INDEX_ALIAS
        if code not in self._alias:
            h = self._h(code)
            a = "S" + h[:4].upper()
            i = 4
            while a in self._real and self._real[a] != code:
                i += 1
                a = "S" + h[i - 3 : i + 1].upper()
            self._alias[code] = a
            self._real[a] = code
        return self._alias[code]

    def real(self, alias: str) -> str | None:
        alias = (alias or "").upper()
        if alias == INDEX_ALIAS:
            return "^AXJO"
        return self._real.get(alias)

    def factor(self, code: str) -> float:
        """0.25x to 4x, log-uniform (26 Sep: at 0.5-2x the model still named stocks from their
        price level)."""
        return 4.0 ** (2 * int(self._h(code)[8:12], 16) / 0xFFFF - 1)

    def price(self, code: str, x):
        return None if x is None else round(float(x) * self.factor(code), 4)

    def unprice(self, code: str, x):
        return None if x is None else float(x) / self.factor(code)

    def volume(self, code: str, v):
        return None if v is None else round(float(v) / self.factor(code))

    def shares_to_real(self, code: str, shown: int) -> int:
        """The trader's share count (at disguised prices) as real shares of the same value."""
        return int(round(int(shown) * self.factor(code)))

    def shares_to_shown(self, code: str, real: int) -> int:
        return int(round(int(real) / self.factor(code)))

    def date(self, d: date) -> str:
        # no weekday: with the reporting season it let the model name the date (26 Sep probe)
        return f"Day {self.day_no.get(d, '?')}"

    def headline(self, h: str) -> str:
        """Generic announcement words only (lab/anon.VOCAB), percentages and small numbers
        kept; no names and no calendar: years, day numbers, months and reporting periods
        ("31 March 2026", "FY26", "1H26", "Q3") are removed."""
        from asxbot.lab.anon import VOCAB

        h = re.sub(r"\b(19|20)\d\d\b", " ", h or "")
        h = re.sub(r"\b(FY|CY|[12]H|H[12]|Q[1-4])\s?'?\d{0,4}\b", " ", h, flags=re.I)
        h = re.sub(r"\b\d{1,2}(st|nd|rd|th)?\s+(?=(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|"
                   r"Dec))", " ", h, flags=re.I)  # fmt: skip
        out: list[str] = []
        for w in re.findall(r"\d+(?:\.\d+)?%|[A-Za-z0-9][A-Za-z0-9'\-]*|[&:()/,.\-]", h):
            if w.lower() in VOCAB or re.fullmatch(r"\d+(\.\d+)?%|[&:()/,.\-]", w):
                out.append(w)
            elif re.fullmatch(r"\d{1,3}(\.\d+)?", w) and not re.match(
                    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", w):  # fmt: skip
                out.append(w)
            elif not out or out[-1] != "[name]":
                out.append("[name]")
        return " ".join(out)

    def text(self, s: str) -> str:
        """Codes in free text replaced by their aliases; ISO dates removed."""
        for code, a in self._alias.items():
            s = re.sub(rf"\b{re.escape(code)}\b", a, s)
        return re.sub(r"\b20\d\d-\d\d-\d\d\b", "Day ?", s)


class NoAnon:
    """Post-cutoff runs: everything as it was."""

    def alias(self, code):
        return code.upper()

    def real(self, alias):
        return (alias or "").upper()

    def factor(self, code):
        return 1.0

    def price(self, code, x):
        return None if x is None else round(float(x), 4)

    def unprice(self, code, x):
        return None if x is None else float(x)

    def volume(self, code, v):
        return None if v is None else round(float(v))

    def shares_to_real(self, code, shown):
        return int(shown)

    def shares_to_shown(self, code, real):
        return int(real)

    def date(self, d):
        return f"{d:%a %d %b %Y}"

    def headline(self, h):
        return h or ""

    def text(self, s):
        return s
