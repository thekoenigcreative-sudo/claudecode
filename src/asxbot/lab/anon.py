"""Anonymised agent packets for days before the models' knowledge cutoff (PRACTICE_LAB.md 2).

The model may remember what a named ASX stock did on a named day. So on TUNE days the agent
sees: an alias for the stock (stable within the day), every price multiplied by a hidden
per-stock-per-day factor between 0.5 and 2 (percentages, R, volumes and dollar values are
unchanged, so the decision's economics are the same), "Day N" for the date, and announcement
headlines reduced to their generic words. The simulation itself runs on the real prices; only
what the agent reads is disguised, and its tighter stop is scaled back before use.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import re

# Words an ASX headline can keep: generic announcement vocabulary, never a name.
VOCAB = set(
    """
quarterly activities activity report reports cashflow cash flow half year yearly annual full
results result financial statements accounts preliminary final interim dividend distribution
distributions dividends trading update updates guidance outlook market investor presentation
briefing webinar conference call acquisition acquires acquire acquired merger scheme
implementation agreement deed binding non-binding offer takeover bid target statement bidders
capital raising placement entitlement share purchase plan spp issue issued shares securities
options notes convertible bond debt facility refinancing loan funding completion complete
completed commencement commences commence first production drilling results assay assays
exploration resource reserve reserves estimate mineral ore project projects study feasibility
scoping pre-feasibility definitive contract contracts award awarded awards order orders
customer customers partnership collaboration joint venture jv sale divestment disposal asset
assets sells sold buy-back buyback on-market off-market director directors ceo cfo chair
chairman appointment appointments resignation retirement change changes substantial holder
holding holdings becoming ceasing initial notice interest interests appendix 4c 4d 4e 5b 3b 2a
3y 3x 3z quotation application cleansing prospectus supplementary reinstatement reinstated
suspension suspended voluntary halt trading halt pause response query aware letter price
sensitive agm egm meeting general notice results of proxy form constitution record date ex date
payment dfn drp reinvestment plan tax impairment writedown write-down restatement revenue sales
earnings profit loss ebitda npat underlying statutory growth strong record upgrade downgrade
approval approved regulatory fda tga trial trials phase patent licence license regulator court
litigation settlement claim dispute ruling decision confirmation confirms confirmed
clarification clarifies strategic review update on the of and for to in a an with by at from
new further additional second third
""".split()
)

DATE_ISO = re.compile(r"\b20\d\d-\d\d-\d\d\b")
DATE_WORDY = re.compile(
    r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{1,2} (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b"
)
PRICE_IN_TEXT = re.compile(r"(?<![\w.%])(\d+\.\d{2,4})(?![\d%x])")


class Anon:
    def __init__(self, day, salt: str, day_number: int):
        self.day = day
        self.salt = salt.encode()
        self.n = day_number

    def _h(self, ticker: str) -> str:
        return hmac.new(
            self.salt, f"{self.day}|{ticker.upper()}".encode(), hashlib.sha256
        ).hexdigest()

    def alias(self, ticker: str) -> str:
        return "S" + self._h(ticker)[:4].upper()

    def factor(self, ticker: str) -> float:
        return 0.5 + int(self._h(ticker)[4:8], 16) / 0xFFFF * 1.5

    def price(self, ticker: str, x):
        return None if x is None else round(float(x) * self.factor(ticker), 4)

    def unprice(self, ticker: str, x):
        return None if x is None else float(x) / self.factor(ticker)

    def text(self, ticker: str, s: str) -> str:
        """Prices inside a sentence scaled; the stock's code replaced; dates removed."""
        f = self.factor(ticker)
        s = PRICE_IN_TEXT.sub(
            lambda m: (
                f"{float(m.group(1)) * f:.4f}".rstrip("0").rstrip(".")
                if "." in m.group(1)
                else m.group(1)
            ),
            s,
        )
        s = re.sub(rf"\b{re.escape(ticker.upper())}\b", self.alias(ticker), s)
        return self.dates(s)

    def dates(self, s: str) -> str:
        s = DATE_ISO.sub(f"Day {self.n}", s)
        return DATE_WORDY.sub(lambda m: m.group(1), s)

    def headline(self, h: str) -> str:
        words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]*|[^\sA-Za-z0-9]", h or "")
        out = []
        for w in words:
            low = w.lower()
            if low in VOCAB or re.fullmatch(r"\d+(\.\d+)?%?|[&:()/,.\-]", w):
                out.append(w)
            elif not out or out[-1] != "[name]":
                out.append("[name]")
        return " ".join(out)

    def news_row(self, row: str) -> str:
        m = re.match(
            r"^((?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{1,2} \w{3} )?\d{2}:\d{2}) (\[price "
            r"sensitive\] )?(.*)$",
            row,
        )
        if not m:
            return self.headline(row)
        when = m.group(1)
        if len(when) > 5:
            when = "the previous day " + when[-5:]
        return f"{when} {m.group(2) or ''}{self.headline(m.group(3))}"

    def setup(self, s):
        c = dict(s.context or {})
        for k in ("range_low", "range_high"):
            if c.get(k) is not None:
                c[k] = self.price(s.ticker, c[k])
        return dataclasses.replace(
            s,
            ticker=self.alias(s.ticker),
            last=self.price(s.ticker, s.last),
            stop=self.price(s.ticker, s.stop),
            why=self.text(s.ticker, s.why),
            trigger_bar=self.dates(s.trigger_bar),
            context=c,
        )

    def terms(self, ticker: str, t: dict) -> dict:
        f = self.factor(ticker)
        out = dict(t)
        for k in ("limit", "stop", "last"):
            if out.get(k) is not None:
                out[k] = self.price(ticker, out[k])
        out["qty"] = max(1, int(round(int(t["qty"]) / f)))  # same dollar value and risk
        return out

    def context(self, ticker: str, ctx: dict) -> dict:
        c = dict(ctx)
        for k in ("last", "day_high", "day_low"):
            if c.get(k) is not None:
                c[k] = self.price(ticker, c[k])
        bars = []
        for b in c.get("last_15_bars") or []:
            bars.append(
                re.sub(
                    r"([ohlc])(\d+\.\d+)",
                    lambda m: (
                        m.group(1)
                        + f"{self.price(ticker, float(m.group(2))):.4f}".rstrip("0").rstrip(".")
                    ),
                    b,
                )
            )
        c["last_15_bars"] = bars
        c["news_today"] = [self.news_row(r) for r in c.get("news_today") or []]
        return c
