"""Paced, cached HTTP client for asx.com.au.

Rules (SPEC section 4): descriptive user agent, pause between requests, cache everything,
never re-download what is cached, back off on errors, and STOP on refusal. A refusal
(403, 429, or a bot-challenge page) raises AccessRefused; callers must stop and alert,
never work around it.
"""

from __future__ import annotations

import gzip
import hashlib
import time
from datetime import datetime
from pathlib import Path

import requests

from asxbot.log import get_logger

log = get_logger("asxbot.announcements.http")

REFUSAL_MARKERS = ("access denied", "captcha", "are you a robot", "request blocked", "cf-chl")


class AccessRefused(RuntimeError):
    pass


class PacedClient:
    def __init__(
        self,
        cache_dir: Path,
        user_agent: str,
        pause_s: float = 3.0,
        max_retries: int = 4,
        backoff_base_s: float = 10.0,
        timeout_s: float = 60.0,
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.pause_s = pause_s
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Language": "en-AU,en"})
        self._last_request = 0.0
        self.requests_made = 0

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
        """Download a binary (PDF) to dest unless it exists."""
        dest = Path(dest)
        if dest.exists():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = self._request(url)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(r.content)
        tmp.replace(dest)
        return dest

    def _fetch(self, url: str) -> str:
        r = self._request(url)
        text = r.text
        low = text[:5000].lower()
        if any(m in low for m in REFUSAL_MARKERS):
            raise AccessRefused(f"bot challenge / refusal page at {url}")
        return text

    def _request(self, url: str) -> requests.Response:
        attempt = 0
        while True:
            self._pace()
            attempt += 1
            self.requests_made += 1
            try:
                r = self.session.get(url, timeout=self.timeout_s)
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
