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


def parse_today(html: str) -> list[Announcement]:
    soup = BeautifulSoup(html, "lxml")
    table = _find_table(soup)
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
