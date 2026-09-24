from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from asxbot.alerts import ACCESS_REFUSED, Alerts
from asxbot.announcements.http import AccessRefused, PacedClient
from asxbot.announcements.model import classify_headline, to_frame
from asxbot.announcements.parser import ParseError, parse_company, parse_today

FIX = Path(__file__).parent / "fixtures"
SYD = ZoneInfo("Australia/Sydney")


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8", errors="replace")


def test_parse_today_fixture():
    items = parse_today(_read("todayAnns.html"))
    assert len(items) == 40
    first = items[0]
    assert first.code == "HLS"
    assert first.released_at == datetime(2026, 9, 22, 14, 33)
    assert first.headline == "Change in substantial holding"
    assert first.price_sensitive is False
    assert first.ids_id == "03142216"
    assert first.pdf_url.startswith("https://www.asx.com.au/asx/v2/statistics/displayAnnouncement")
    assert first.pages == 5 and first.size == "1.3MB"
    assert any(a.price_sensitive for a in items), "fixture should contain a sensitive item"
    assert all(len(a.code) in (3, 4) for a in items)


def test_parse_company_fixture():
    items = parse_company(_read("announcements_BHP_2010.html"), "bhp")
    assert len(items) == 40
    assert all(a.code == "BHP" for a in items)
    assert items[0].released_at == datetime(2010, 12, 31, 9, 30)
    assert items[0].headline == "BHP Billiton - Trading Policy"
    assert items[0].pages == 12 and items[0].size == "281.4KB"
    assert not any(a.price_sensitive for a in items)  # first 40 rows of 2010 are all routine


def test_parse_company_recent_fixture():
    items = parse_company(_read("announcements_BHP_M6.html"), "BHP")
    assert len(items) > 10
    assert all(a.released_at.year >= 2026 for a in items)
    assert sum(a.price_sensitive for a in items) == 5


def test_parse_layout_change_raises():
    with pytest.raises(ParseError):
        parse_today("<html><body><table><tr><th>Nope</th></tr></table></body></html>")


def test_company_no_announcements_returns_empty():
    assert parse_company("<html><body>No announcements found</body></html>", "XYZ") == []


def test_to_frame_columns_and_flags():
    items = parse_today(_read("todayAnns.html"))
    df = to_frame(items)
    assert {"code", "released_at", "type", "price_sensitive", "pre_open", "ids_id"} <= set(df)
    assert df["released_at"].is_monotonic_increasing
    assert df["pre_open"].dtype == bool


@pytest.mark.parametrize(
    "headline,expected",
    [
        ("Change in substantial holding", "substantial_holding"),
        ("Trading Halt", "trading_halt"),
        ("Quarterly Activities Report", "results"),
        ("Placement to raise $5m", "capital_raising"),
        ("High grade gold intersected at Bluebird", "exploration"),
        ("FY26 Guidance upgrade", "guidance"),
        ("Something unusual", "other"),
    ],
)
def test_classify(headline, expected):
    assert classify_headline(headline) == expected


class _Resp:
    def __init__(self, status, text="", content=b""):
        self.status_code = status
        self.text = text
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_client_caches_and_paces(tmp_path, monkeypatch):
    calls = []
    c = PacedClient(tmp_path, "ua", pause_s=0.0)
    monkeypatch.setattr(
        c.session, "get", lambda url, timeout: calls.append(url) or _Resp(200, "ok")
    )
    assert c.get("http://x/a") == "ok"
    assert c.get("http://x/a") == "ok"
    assert calls == ["http://x/a"]  # second call served from cache
    assert c.get("http://x/a", use_cache=False) == "ok"
    assert len(calls) == 2


def test_client_refusal_raises(tmp_path, monkeypatch):
    c = PacedClient(tmp_path, "ua", pause_s=0.0)
    monkeypatch.setattr(c.session, "get", lambda url, timeout: _Resp(403, "nope"))
    with pytest.raises(AccessRefused):
        c.get("http://x/b")
    monkeypatch.setattr(c.session, "get", lambda url, timeout: _Resp(200, "<html>Access Denied"))
    with pytest.raises(AccessRefused):
        c.get("http://x/c")


def test_alert_flag_lifecycle(tmp_path):
    al = Alerts(tmp_path)
    assert not al.is_active(ACCESS_REFUSED)
    al.raise_alert(ACCESS_REFUSED, "403")
    assert al.is_active(ACCESS_REFUSED)
    assert al.active()[0][0] == ACCESS_REFUSED
    al.clear(ACCESS_REFUSED)
    assert not al.is_active(ACCESS_REFUSED)


def test_history_archive_stops_on_refusal(tmp_path, monkeypatch):
    from asxbot.announcements.history import HistoryArchive

    al = Alerts(tmp_path)
    c = PacedClient(tmp_path / "cache", "ua", pause_s=0.0)
    monkeypatch.setattr(c.session, "get", lambda url, timeout: _Resp(429, ""))
    arc = HistoryArchive(tmp_path, c, al)
    with pytest.raises(AccessRefused):
        arc.run(["BHP"])
    assert al.is_active(ACCESS_REFUSED)
    with pytest.raises(AccessRefused, match="alert is active"):
        arc.run(["BHP"])


def test_history_archive_parses_and_resumes(tmp_path, monkeypatch):
    from asxbot.announcements.history import HistoryArchive

    page = _read("announcements_BHP_2010.html")
    al = Alerts(tmp_path)
    c = PacedClient(tmp_path / "cache", "ua", pause_s=0.0)
    calls = []
    monkeypatch.setattr(
        c.session, "get", lambda url, timeout: calls.append(url) or _Resp(200, page)
    )
    arc = HistoryArchive(tmp_path, c, al)
    arc.run(["BHP"], listing_dates={"BHP": datetime(2024, 1, 1).date()})
    years_hit = len(calls)
    assert years_hit >= 2  # 2024 .. current year
    df = arc.load("BHP")
    assert len(df) == 40  # same page every year -> deduplicated on ids_id
    # resume: past years done, only a stale current year is refetched
    arc2 = HistoryArchive(tmp_path, c, al)
    this_year = datetime.now().year
    assert arc2.progress[f"BHP:{this_year}"]
    arc2.progress[f"BHP:{this_year}"] = "2000-01-01"  # pretend it is stale
    arc2.run(["BHP"], listing_dates={"BHP": datetime(2024, 1, 1).date()})
    assert len(calls) == years_hit + 1
    # and a fresh current year is not refetched at all
    arc3 = HistoryArchive(tmp_path, c, al)
    stats = arc3.run(["BHP"], listing_dates={"BHP": datetime(2024, 1, 1).date()})
    assert len(calls) == years_hit + 1 and stats["skipped"] == 1


def test_parse_company_2002_text_era_links():
    """Pre-PDF rows link by documentNumber, not idsId. They must parse, with a distinct id."""
    items = parse_company(_read("announcements_BHP_2002.html"), "BHP")
    assert len(items) == 40
    doc_ids = [a for a in items if a.ids_id.startswith("d")]
    assert doc_ids, "2002 fixture should contain documentNumber rows"
    assert all(a.pdf_url.startswith("https://www.asx.com.au/") for a in items)
    assert doc_ids[0].pages is None


def test_company_page_skips_empty_placeholder_row():
    html = (
        "<html><body><table><thead><tr><th>Date</th><th>Price sens.</th><th>Headline</th></tr>"
        "</thead><tbody>"
        "<tr><td>17/06/2002<br><span class='dates-time'>12:28 pm</span></td><td>&nbsp;</td>"
        "<td> - </td></tr>"
        "<tr><td>18/06/2002<br><span class='dates-time'>9:00 am</span></td><td>&nbsp;</td>"
        "<td><a href='/asx/v2/statistics/displayAnnouncement.do?display=pdf&amp;idsId=123'>"
        "Real one<br></a></td></tr>"
        "</tbody></table></body></html>"
    )
    items = parse_company(html, "AFI")
    assert [a.headline for a in items] == ["Real one"]
    with pytest.raises(ParseError):
        parse_company(html.replace(" - ", "something without a link"), "AFI")


# -- the PDF that was never a PDF (23 Sep) ---------------------------------
TERMS_PAGE = b"""<html><body><h2>Access to this site</h2>
<form name="showAnnouncementPDFForm" method="post" action="/asx/v2/statistics/announcementTerms.do">
<input value="Decline" type="submit"><input value="Agree and proceed" type="submit">
<input name="pdfURL"
 value="https://announcements.asx.com.au/asxpdf/20260923/pdf/abc123.pdf"
 type="hidden">
</form></body></html>"""
REAL_PDF = b"%PDF-1.6\n%real document bytes\n"


class _Reply:
    def __init__(self, content):
        self.content = content
        self.headers = {"Content-Length": str(len(content))}

    def iter_content(self, n):
        for i in range(0, len(self.content), n):
            yield self.content[i : i + n]

    def close(self):
        pass


def _client(tmp_path, monkeypatch, replies):
    """A PacedClient whose requests come from a scripted list, newest first."""
    from asxbot.announcements.http import PacedClient

    c = PacedClient(tmp_path / "cache", "test-agent", pause_s=0, backoff_base_s=0)
    seen = []

    def fake(url, stream=False):
        seen.append(url)
        return _Reply(replies.pop(0))

    monkeypatch.setattr(c, "_request", fake)
    return c, seen


def test_the_terms_page_is_followed_to_the_real_pdf(tmp_path, monkeypatch):
    c, seen = _client(tmp_path, monkeypatch, [TERMS_PAGE, REAL_PDF])
    dest = tmp_path / "AAA_1.pdf"
    c.get_bytes("https://www.asx.com.au/announcement", dest)
    assert dest.read_bytes() == REAL_PDF
    assert seen[1] == "https://announcements.asx.com.au/asxpdf/20260923/pdf/abc123.pdf"


def test_a_saved_terms_page_is_not_mistaken_for_a_cached_pdf(tmp_path, monkeypatch):
    """The bug that made it permanent: the file existed, so it was never fetched again."""
    dest = tmp_path / "AAA_1.pdf"
    dest.write_bytes(TERMS_PAGE)
    c, _ = _client(tmp_path, monkeypatch, [TERMS_PAGE, REAL_PDF])
    c.get_bytes("https://www.asx.com.au/announcement", dest)
    assert dest.read_bytes() == REAL_PDF


def test_a_real_pdf_on_disk_is_not_downloaded_again(tmp_path, monkeypatch):
    dest = tmp_path / "AAA_1.pdf"
    dest.write_bytes(REAL_PDF)
    c, seen = _client(tmp_path, monkeypatch, [])
    c.get_bytes("https://www.asx.com.au/announcement", dest)
    assert seen == []  # nothing was requested


def test_it_retries_then_gives_up_rather_than_saving_rubbish(tmp_path, monkeypatch):
    import pytest as _pytest

    junk = b"<html>something else entirely</html>"
    c, seen = _client(tmp_path, monkeypatch, [junk] * 4)
    dest = tmp_path / "AAA_1.pdf"
    with _pytest.raises(RuntimeError, match="not a PDF"):
        c.get_bytes("https://www.asx.com.au/announcement", dest)
    assert len(seen) == 4 and not dest.exists()


def test_each_failure_carries_a_short_reason(tmp_path, monkeypatch):
    """For the pdf_fetch_failed self-check: why, not only that (2026-09-24)."""
    from asxbot.announcements.http import PdfFetchFailed

    c, _ = _client(tmp_path, monkeypatch, [b"<html>nothing here</html>"] * 4)
    with pytest.raises(PdfFetchFailed) as e:
        c.get_bytes("https://www.asx.com.au/announcement", tmp_path / "AAA_1.pdf")
    assert e.value.reason == "not a PDF, and no pdfURL to follow"


def test_a_pdfurl_off_asx_is_not_followed(tmp_path, monkeypatch):
    from asxbot.announcements.http import PdfFetchFailed

    evil = TERMS_PAGE.replace(b"https://announcements.asx.com.au", b"https://evil.example")
    c, seen = _client(tmp_path, monkeypatch, [evil] * 4)
    with pytest.raises(PdfFetchFailed, match="pdfURL points off ASX"):
        c.get_bytes("https://www.asx.com.au/announcement", tmp_path / "AAA_1.pdf")
    assert all("evil" not in u for u in seen)


def test_a_challenge_page_instead_of_a_pdf_stops_the_collector(tmp_path, monkeypatch):
    from asxbot.announcements.http import AccessRefused

    c, _ = _client(tmp_path, monkeypatch, [b"<html>Are you a robot? captcha</html>"])
    with pytest.raises(AccessRefused):
        c.get_bytes("https://www.asx.com.au/announcement", tmp_path / "AAA_1.pdf")


def test_an_oversized_reply_is_refused(tmp_path, monkeypatch):
    from asxbot.announcements import http as H

    monkeypatch.setattr(H, "MAX_PDF_BYTES", 10)
    c, _ = _client(tmp_path, monkeypatch, [REAL_PDF] * 4)
    with pytest.raises(H.PdfFetchFailed, match="too large"):
        c.get_bytes("https://www.asx.com.au/announcement", tmp_path / "AAA_1.pdf")


# -- the today page's three shapes (24 Sep 2026) ----------------------------
# At 07:30:05 and 07:31:24 the poll logged ERROR "no announcements table with a Headline
# column found": before the ASX posts anything the page has no table. That is zero, not a
# fault. A page that is neither that nor the populated page must still fail loudly.
def test_the_populated_page_of_24_sep_parses():
    items = parse_today(_read("todayAnns_2026-09-24.html"))
    assert len(items) == 40  # the fixture keeps the first 40 of the 751 rows fetched
    first = items[0]
    assert (first.code, first.ids_id, first.headline) == (
        "RND", "03143646", "Annual Report to shareholders"
    )  # fmt: skip
    assert first.released_at == datetime(2026, 9, 24, 19, 24)
    assert first.pages == 61 and first.size == "2.7MB"
    assert len({a.ids_id for a in items}) == 40


def test_the_empty_page_before_anything_is_posted_is_zero_not_an_error():
    # Built by hand from the populated page (no real empty page was kept); see its header.
    assert parse_today(_read("todayAnns_empty.html")) == []


def test_a_changed_layout_with_announcement_links_but_no_table_is_still_an_error():
    with pytest.raises(ParseError, match="Headline column"):
        parse_today(_read("todayAnns_layout_changed.html"))


def test_a_page_that_is_not_the_asx_today_page_is_still_an_error():
    block = (
        "<html><head><title>Request unsuccessful. Incapsula incident ID: 0-1234</title></head>"
        "<body>Request unsuccessful.</body></html>"
    )
    with pytest.raises(ParseError, match="Incapsula"):
        parse_today(block)
    with pytest.raises(ParseError):
        parse_today("<html><body>nothing here</body></html>")


class _PageClient:
    def __init__(self, html):
        self.html = html

    def get(self, url, use_cache=True):
        return self.html


def _poller(tmp_path, html):
    from asxbot.announcements.live import LivePoller

    al = Alerts(tmp_path)
    return LivePoller(tmp_path, _PageClient(html), al, {"RND"}, fetch_pdfs=False), al


def test_the_poller_counts_an_empty_page_as_zero_at_info_and_keeps_one_copy(tmp_path):
    import logging

    records = []
    catch = logging.Handler()
    catch.emit = records.append
    root = logging.getLogger("asxbot")  # does not propagate once logging is set up
    root.addHandler(catch)
    level = root.level
    root.setLevel(logging.INFO)
    try:
        poller, al = _poller(tmp_path, _read("todayAnns_empty.html"))
        now = datetime(2026, 9, 25, 7, 30, 5, tzinfo=SYD)
        assert poller.poll_once(now) == []
        assert poller.poll_once(now.replace(minute=31)) == []
    finally:
        root.removeHandler(catch)
        root.setLevel(level)
    assert not [r for r in records if r.levelno >= logging.WARNING]
    assert any("posted no announcements yet" in r.getMessage() for r in records)
    assert al.active() == []
    kept = list((tmp_path / "announcements" / "pages").iterdir())
    assert [p.name for p in kept] == ["todayAnns_empty_2026-09-25.html"]  # once a day


def test_the_poller_still_fails_loudly_on_a_page_it_cannot_read(tmp_path):
    poller, al = _poller(tmp_path, _read("todayAnns_layout_changed.html"))
    now = datetime(2026, 9, 25, 9, 0, tzinfo=SYD)
    for minute in range(7):
        with pytest.raises(ParseError):
            poller.poll_once(now.replace(minute=minute))
    assert [k for k, _ in al.active()] == ["collector_parse_error"]
    kept = sorted(p.name for p in (tmp_path / "announcements" / "pages").iterdir())
    assert len(kept) == 5 and kept[0] == "todayAnns_unparsed_2026-09-25_090000.html"


def test_the_poller_reads_the_populated_page(tmp_path):
    poller, al = _poller(tmp_path, _read("todayAnns_2026-09-24.html"))
    new = poller.poll_once(datetime(2026, 9, 24, 19, 35, tzinfo=SYD))
    assert len(new) == 40 and al.active() == []
    assert not (tmp_path / "announcements" / "pages").exists()
