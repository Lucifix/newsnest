"""Fetcher interface, shared HTTP handling and the run wrapper."""

from __future__ import annotations

import calendar
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import httpx
from selectolax.lexbor import LexborHTMLParser

from newsnest.config import Config, Source
from newsnest.models import FetchResult, FetchStatus

log = logging.getLogger(__name__)

# Bot-challenge or captcha pages served with HTTP 200 are recognised by their <title> (normal pages
# often load reCAPTCHA scripts, so the body alone is not evidence) or Cloudflare's challenge marker
_BLOCK_TITLE_MARKERS = ("just a moment", "captcha", "are you a robot", "access denied", "attention required")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


@dataclass
class FetchContext:
    config: Config
    client: httpx.Client
    # Shared state between fetchers within one process (e.g. a resolver cache)
    services: dict[str, Any] = field(default_factory=dict)


class Fetcher(Protocol):
    def fetch(self, source: Source, ctx: FetchContext) -> FetchResult: ...


class FetchFailed(Exception):
    """Raised inside a fetcher to end the fetch with a specific failure status."""

    def __init__(self, status: FetchStatus, message: str, http_status: int | None = None):
        super().__init__(message)
        self.status = status
        self.http_status = http_status


def make_client(cfg: Config) -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=cfg.http.timeout_seconds,
        headers={"User-Agent": cfg.http.user_agent, "Accept-Language": "sl,en;q=0.8"},
    )


def http_get(ctx: FetchContext, url: str, **kwargs) -> httpx.Response:
    """GET that converts every non-success outcome into FetchFailed with the right status."""
    try:
        r = ctx.client.get(url, **kwargs)
    except httpx.HTTPError as e:
        raise FetchFailed(FetchStatus.NETWORK_ERROR, f"{type(e).__name__}: {e}") from e
    if r.status_code in (403, 429):
        raise FetchFailed(FetchStatus.BLOCKED, f"HTTP {r.status_code} from {r.url.host}", r.status_code)
    if r.status_code >= 400:
        raise FetchFailed(FetchStatus.HTTP_ERROR, f"HTTP {r.status_code} from {r.url}", r.status_code)
    if "html" in r.headers.get("content-type", "") and is_challenge_page(r.text):
        raise FetchFailed(FetchStatus.BLOCKED, f"challenge/captcha page from {r.url.host}", r.status_code)
    return r


def is_challenge_page(html: str) -> bool:
    head = html[:20000]
    if "cf-chl-" in head or "challenges.cloudflare.com" in head:
        return True
    m = _TITLE.search(head)
    title = m.group(1).strip().lower() if m else ""
    return any(marker in title for marker in _BLOCK_TITLE_MARKERS)


def run_fetch(fetcher: Fetcher, source: Source, ctx: FetchContext) -> FetchResult:
    """Run one fetcher; never raises, so one broken source cannot take down the run."""
    start = time.monotonic()
    try:
        result = fetcher.fetch(source, ctx)
    except FetchFailed as e:
        result = FetchResult(source.id, e.status, http_status=e.http_status, error=str(e))
    except Exception as e:  # a bug or an unexpected format is still a visible failure
        log.exception("fetcher crashed for %s", source.id)
        result = FetchResult(source.id, FetchStatus.PARSE_ERROR, error=f"{type(e).__name__}: {e}")
    result.duration_ms = int((time.monotonic() - start) * 1000)
    _drop_stale(result, ctx.config.max_item_age_days)
    return result


def _drop_stale(result: FetchResult, max_age_days: float) -> None:
    """Some feeds (e.g. Dnevnik) mix in evergreen pages from years ago. Undated items are kept."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    fresh = [i for i in result.items if i.published is None or i.published >= cutoff]
    if dropped := len(result.items) - len(fresh):
        result.warnings.append(f"dropped {dropped} items older than {max_age_days:g} days")
        result.items = fresh


def html_to_text(html: str | None, limit: int = 600) -> str:
    if not html:
        return ""
    text = LexborHTMLParser(html).text(separator=" ") if "<" in html else html
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def struct_to_dt(st: time.struct_time | None) -> datetime | None:
    """feedparser normalizes dates to UTC struct_time."""
    if not st:
        return None
    return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
