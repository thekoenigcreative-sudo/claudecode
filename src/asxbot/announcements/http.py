"""Paced, cached HTTP client for asx.com.au.

Rules (SPEC section 4): descriptive user agent, pause between requests, cache everything,
never re-download what is cached, back off on errors, and STOP on refusal. A refusal
(403, 429, or a bot-challenge page) raises AccessRefused; callers must stop and alert,
never work around it.

Two ways to use it (26 Sep 2026). The collector (history, the stand-alone poller) keeps the
patient settings: a 60 s timeout, 4 retries backing off 10-80 s, 4 attempts per PDF - up to
about half an hour on one PDF in an outage, which is fine for a batch job. The watcher is
single-threaded: while it waits on asx.com.au nothing else happens - no fills, no stops, no
10:30 rule, no 15:50 sweep. It builds its client with `inline()`: 15 s timeout, one retry
after 5 s, one attempt per PDF. A PDF that fails is not lost: the poller retries it on later
cycles (announcements/live.py). Pacing and the %PDF checks are the same in both.
"""

from __future__ import annotations

import gzip
import hashlib
import re
import time
from datetime import datetime
from pathlib import Path

import requests

from asxbot.log import get_logger

log = get_logger("asxbot.announcements.http")

REFUSAL_MARKERS = ("access denied", "captcha", "are you a robot", "request blocked", "cf-chl")
# Where a terms page's pdfURL may point. Anything else is not followed.
PDF_HOSTS = ("announcements.asx.com.au",)
# No announcement PDF is anywhere near this; a reply this large is not one.
MAX_PDF_BYTES = 60 * 1024 * 1024


class AccessRefused(RuntimeError):
    pass


class PdfFetchFailed(RuntimeError):
    """A PDF could not be fetched. `reason` is one short, fixed phrase (for the self-check)."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason


def _is_pdf(body: bytes) -> bool:
    return body[:5].startswith(b"%PDF")


def head_bytes(path: Path, n: int = 5) -> bytes:
    """The first `n` bytes of a file: enough for the %PDF check, without reading a 20 MB
    document whole to look at five of its bytes."""
    with open(path, "rb") as fh:
        return fh.read(n)


def _pdf_url_from_terms(body: bytes) -> str | None:
    """The real document link out of ASX's terms-of-use interstitial.

    The page is a form with the announcement's true URL in a hidden `pdfURL` field, which
    is what the "Agree and proceed" button submits. Following it does by code what a person
    does by clicking. It is not a bot challenge: a real refusal (401/403/429 or a challenge
    page) still raises AccessRefused and stops the collector.

    What that page asks for is a use condition, not a technical one: announcements are free
    "for investors' private and personal use", and commercial or professional use needs
    ASX's express written authority. Whether this system stays on the private-and-personal
    side is Rick's call, not the code's - see HANDOVER.md, "The terms gate".
    """
    try:
        text = body[:20000].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r'name=["\']pdfURL["\']\s+value=["\']([^"\']+)["\']', text, re.I)
    if not m:
        m = re.search(r'value=["\']([^"\']+\.pdf)["\']\s+name=["\']pdfURL["\']', text, re.I)
    return m.group(1) if m else None


def _refusal_check(body: bytes, url: str) -> None:
    """A bot challenge or refusal page where a PDF should be: stop, as the collector must."""
    low = body[:5000].decode("utf-8", "replace").lower()
    if any(m in low for m in REFUSAL_MARKERS):
        raise AccessRefused(f"bot challenge / refusal page at {url}")


def _allowed_pdf_host(url: str) -> bool:
    from urllib.parse import urlparse

    u = urlparse(url)
    return u.scheme == "https" and (u.hostname or "").lower() in PDF_HOSTS


class PacedClient:
    def __init__(
        self,
        cache_dir: Path,
        user_agent: str,
        pause_s: float = 3.0,
        max_retries: int = 4,
        backoff_base_s: float = 10.0,
        timeout_s: float = 60.0,
        pdf_attempts: int | None = None,
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.pause_s = pause_s
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.timeout_s = timeout_s
        # Whole attempts per PDF (each request inside one also gets max_retries retries).
        self.pdf_attempts = max(1, int(pdf_attempts if pdf_attempts is not None else max_retries))
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Language": "en-AU,en"})
        self._last_request = 0.0
        self.requests_made = 0

    INLINE_TIMEOUT_S = 15.0
    INLINE_RETRIES = 1
    INLINE_BACKOFF_S = 5.0

    @classmethod
    def inline(cls, cache_dir: Path, user_agent: str, pause_s: float = 3.0) -> PacedClient:
        """The watcher's client: bounded waits, so an asx.com.au outage costs a cycle about
        half a minute per URL, not half an hour (module docstring)."""
        return cls(cache_dir, user_agent, pause_s=pause_s, max_retries=cls.INLINE_RETRIES,
                   backoff_base_s=cls.INLINE_BACKOFF_S, timeout_s=cls.INLINE_TIMEOUT_S,
                   pdf_attempts=1)  # fmt: skip

    # -- cache -----------------------------------------------------------
    def _cache_path(self, url: str) -> Path:
        h = hashlib.sha1(url.encode()).hexdigest()
        return self.cache_dir / h[:2] / f"{h}.html.gz"

    def cached(self, url: str) -> str | None:
        p = self._cache_path(url)
        if p.exists():
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                text = fh.read()
            if text.startswith("<!-- asxbot cache"):
                text = text.split("\n", 1)[1] if "\n" in text else ""
            return text
        return None

    def _store(self, url: str, text: str) -> None:
        p = self._cache_path(url)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            fh.write(f"<!-- asxbot cache {url} {datetime.now().isoformat()} -->\n")
            fh.write(text)
        tmp.replace(p)

    # -- fetch -----------------------------------------------------------
    def get(self, url: str, use_cache: bool = True) -> str:
        if use_cache:
            hit = self.cached(url)
            if hit is not None:
                return hit
        text = self._fetch(url)
        if use_cache:
            self._store(url, text)
        return text

    def get_bytes(self, url: str, dest: Path) -> Path:
        """Download a PDF to dest unless a REAL PDF is already there.

        Two traps this walks around, both found on 23 Sep when every live announcement had
        been judged from its headline alone:

        1. asx.com.au does not serve the PDF at the announcement link. It serves a
           terms-of-use page whose hidden `pdfURL` field holds the actual document, on
           announcements.asx.com.au. What landed on disk was that HTML page, named .pdf,
           so pypdf failed with "Stream has ended unexpectedly" and the reader got nothing.
        2. The old code returned early whenever the file existed, so the first bad save
           was permanent: every later poll skipped it. Existing files are now checked for
           the %PDF magic, and anything else is treated as missing and fetched again.
        """
        dest = Path(dest)
        if dest.exists():
            if _is_pdf(head_bytes(dest)):
                return dest
            log.warning("%s is not a PDF (a saved terms page?); fetching it again", dest.name)
        dest.parent.mkdir(parents=True, exist_ok=True)

        reason = detail = ""
        attempts = self.pdf_attempts
        for attempt in range(1, attempts + 1):
            reason = detail = ""  # each attempt says its own reason, never the last one's
            try:
                body = self._download(url)
                if not _is_pdf(body):
                    _refusal_check(body, url)
                    real = _pdf_url_from_terms(body)
                    if not real:
                        reason, detail = "not a PDF, and no pdfURL to follow", url
                    elif not _allowed_pdf_host(real):
                        reason, detail = "pdfURL points off ASX", real
                    else:
                        log.info("%s served the terms page; following its pdfURL", url)
                        body = self._download(real)
                        if not _is_pdf(body):
                            _refusal_check(body, real)
                            reason, detail = "followed link was not a PDF", real
            except AccessRefused:
                raise
            except PdfFetchFailed as e:
                reason, detail = e.reason, str(e)
            except requests.Timeout as e:
                reason, detail = "timeout", str(e)
            except requests.RequestException as e:
                reason, detail = "network error", str(e)
            except RuntimeError as e:  # HTTP 5xx after retries
                reason, detail = "server error", str(e)
            if not reason:
                tmp = dest.with_suffix(dest.suffix + ".tmp")
                tmp.write_bytes(body)
                tmp.replace(dest)
                return dest
            if attempt < attempts:
                wait = self.backoff_base_s * attempt
                log.warning("%s: %s; retry %d in %.0fs", url, reason, attempt, wait)
                time.sleep(wait)
        raise PdfFetchFailed(reason, f"{detail} (after {attempts} attempt(s))")

    def _download(self, url: str) -> bytes:
        """The body, read in chunks and refused past MAX_PDF_BYTES."""
        r = self._request(url, stream=True)
        size = int(r.headers.get("Content-Length") or 0)
        if size > MAX_PDF_BYTES:
            r.close()
            raise PdfFetchFailed("too large", f"{url} is {size:,} bytes")
        chunks, total = [], 0
        for chunk in r.iter_content(64 * 1024):
            total += len(chunk)
            if total > MAX_PDF_BYTES:
                r.close()
                raise PdfFetchFailed("too large", f"{url} passed {MAX_PDF_BYTES:,} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    def _fetch(self, url: str) -> str:
        r = self._request(url)
        text = r.text
        low = text[:5000].lower()
        if any(m in low for m in REFUSAL_MARKERS):
            raise AccessRefused(f"bot challenge / refusal page at {url}")
        return text

    def _request(self, url: str, stream: bool = False) -> requests.Response:
        attempt = 0
        while True:
            self._pace()
            attempt += 1
            self.requests_made += 1
            try:
                extra = {"stream": True} if stream else {}
                r = self.session.get(url, timeout=self.timeout_s, **extra)
            except requests.RequestException as e:
                if attempt > self.max_retries:
                    raise
                wait = self.backoff_base_s * (2 ** (attempt - 1))
                log.warning("network error %s; retry %d in %.0fs", e, attempt, wait)
                time.sleep(wait)
                continue
            if r.status_code in (401, 403, 429):
                raise AccessRefused(f"HTTP {r.status_code} from {url}")
            if r.status_code >= 500:
                if attempt > self.max_retries:
                    raise RuntimeError(f"HTTP {r.status_code} from {url} after retries")
                wait = self.backoff_base_s * (2 ** (attempt - 1))
                log.warning("HTTP %d; retry %d in %.0fs", r.status_code, attempt, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r

    def _pace(self) -> None:
        gap = time.monotonic() - self._last_request
        if gap < self.pause_s:
            time.sleep(self.pause_s - gap)
        self._last_request = time.monotonic()
