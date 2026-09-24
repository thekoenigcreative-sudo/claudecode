"""Parse asx.com.au announcement pages. Tested against saved pages in tests/fixtures.

Two layouts, both classic v2 pages:
  todayAnns.do            columns: ASX Code | Date | Price sens. | Headline
  announcements.do?by=asxCode&asxCode=XXX&timeframe=Y&year=YYYY
                          columns: Date | Price sens. | Headline
Price-sensitive is an <img class="pricesens"> in the cell; otherwise a non-breaking space.
"""

from __future__ import annotations

import re
from datetime import datetime

from bs4 import BeautifulSoup, Tag

from asxbot.announcements.model import BASE_URL, Announcement

_IDS = re.compile(r"idsId=(\d+)")
# Pre-2003 announcements have no PDF: the link is display=header&documentNumber=NNN
_DOCNUM = re.compile(r"documentNumber=(\d+)")
_ANY_LINK = re.compile(r"idsId=\d+|documentNumber=\d+")
_PAGES = re.compile(r"(\d+)\s*pages?", re.I)


class ParseError(RuntimeError):
    """Layout changed: repair the parser and add the new page as a fixture."""


def _cell_text(td: Tag) -> str:
    return " ".join(td.get_text(" ", strip=True).split())


def _parse_when(td: Tag) -> datetime:
    text = td.get_text(" ", strip=True)
    m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", text)
    t = re.search(r"(\d{1,2}:\d{2})\s*([ap]m)", text, re.I)
    if not m:
        raise ParseError(f"no date in cell: {text!r}")
    date_s = m.group(1)
    time_s = f"{t.group(1)} {t.group(2).lower()}" if t else "12:00 am"
    return datetime.strptime(f"{date_s} {time_s}", "%d/%m/%Y %I:%M %p")


def _parse_headline_cell(td: Tag) -> tuple[str, str, str, int | None, str | None] | None:
    a = td.find("a", href=_ANY_LINK)
    if a is None:
        if _cell_text(td) in ("", "-"):
            return None  # empty placeholder row (seen on AFI 2002); skip it
        raise ParseError("headline cell without an idsId/documentNumber link")
    href = a["href"]
    m = _IDS.search(href)
    # documentNumber ids get a "d" prefix so they never collide with idsId values
    ids_id = m.group(1) if m else "d" + _DOCNUM.search(href).group(1)
    # headline is the text before the <br> inside the link
    parts = [s for s in a.stripped_strings]
    headline = parts[0] if parts else ""
    pages = None
    size = None
    pg = td.find("span", class_="page")
    if pg is not None:
        pm = _PAGES.search(pg.get_text(" ", strip=True))
        pages = int(pm.group(1)) if pm else None
    sz = td.find("span", class_="filesize")
    if sz is not None:
        size = sz.get_text(strip=True) or None
    url = href if href.startswith("http") else BASE_URL + href.replace("&amp;", "&")
    return headline, ids_id, url, pages, size


def _is_sensitive(td: Tag) -> bool:
    return (
        td.find("img", class_="pricesens") is not None
        or "price sensitive" in (td.get("title", "") + td.get_text(" ", strip=True)).lower()
    )


def _find_table(soup: BeautifulSoup) -> Tag:
    for table in soup.find_all("table"):
        ths = [_cell_text(th) for th in table.find_all("th")]
        if any("Headline" in t for t in ths):
            return table
    raise ParseError("no announcements table with a Headline column found")


def _title(soup: BeautifulSoup) -> str:
    return " ".join(soup.title.get_text(" ", strip=True).split()) if soup.title else ""


def is_empty_today_page(soup: BeautifulSoup) -> bool:
    """todayAnns.do before the ASX has posted anything that day.

    At 07:30:05 and 07:31:24 on 24 Sep 2026 the page had no announcements table at all, and
    the poll logged it as an ERROR. It is the ASX's own page (its title) with not one link
    to an announcement. A page with announcement links but no table we can read is a changed
    layout; a page with another title is something else (a block page, an error page). Both
    stay errors. Whether the real empty page has some other table is not known, so a table
    is not held against it: the links are what would be lost.

    No copy of the real empty page was saved on 24 Sep; tests/fixtures/todayAnns_empty.html
    is the populated page with its table cut out. The poller now keeps the first empty page
    of each day (data/announcements/pages/), so the real one can replace it.
    """
    if not re.match(r"today.s announcements\b", _title(soup), re.I):
        return False
    return not any(_ANY_LINK.search(a.get("href", "")) for a in soup.find_all("a"))


def parse_today(html: str) -> list[Announcement]:
    soup = BeautifulSoup(html, "lxml")
    try:
        table = _find_table(soup)
    except ParseError:
        if is_empty_today_page(soup):
            return []
        raise ParseError(
            f"no announcements table with a Headline column found (page title: {_title(soup)!r},"
            f" {len(html):,} chars)"
        ) from None
    out: list[Announcement] = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        code = _cell_text(tds[0]).upper()
        when = _parse_when(tds[1])
        sens = _is_sensitive(tds[2])
        cell = _parse_headline_cell(tds[3])
        if cell is None:
            continue
        headline, ids_id, url, pages, size = cell
        out.append(Announcement(code, when, headline, sens, ids_id, url, pages, size))
    if not out and "No announcements" not in html:
        raise ParseError("today page parsed to zero rows")
    return out


def parse_company(html: str, code: str) -> list[Announcement]:
    soup = BeautifulSoup(html, "lxml")
    try:
        table = _find_table(soup)
    except ParseError:
        if _no_announcements(html):
            return []
        raise
    out: list[Announcement] = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        when = _parse_when(tds[0])
        sens = _is_sensitive(tds[1])
        cell = _parse_headline_cell(tds[2])
        if cell is None:
            continue
        headline, ids_id, url, pages, size = cell
        out.append(Announcement(code.upper(), when, headline, sens, ids_id, url, pages, size))
    return out


def _no_announcements(html: str) -> bool:
    h = html.lower()
    return "no announcements" in h or "not found" in h or "no results" in h
