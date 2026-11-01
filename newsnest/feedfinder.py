"""Find a site's feed from the address of any of its pages, the way other feed readers do.

In order: the address is a feed itself; the feeds the page advertises in its <head>; links on the page
that look like feeds, or lead to a page listing them (one feed per section); the usual paths (/feed/,
/rss.xml …). Every candidate is downloaded and parsed before it is offered, so the reader sees its
title, how often it posts and its latest headlines.

The address is typed by the reader, but the pages behind it belong to someone else: only https is
followed (redirects included), responses are capped in size, addresses that resolve to this machine
or the home network are refused, and nothing from a page is used except a feed's text.
"""

from __future__ import annotations

import calendar
import io
import ipaddress
import re
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx
from selectolax.lexbor import LexborHTMLParser

from newsnest.config import Config
from newsnest.fetchers.base import html_to_text, is_challenge_page
from newsnest.textmatch import normalize_url

MAX_BYTES = 3_000_000
MAX_CANDIDATES = 8
TIMEOUT = 10.0
FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/rdf+xml", "application/feed+json")
# Tried in this order when the page advertises nothing: WordPress, then the common static-site names
COMMON_PATHS = ("/feed/", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml")
_FEEDISH_LINK = re.compile(r"(?i)(/feeds?/?$|/rss/?$|[./]rss(\.xml)?$|atom\.xml$|/feeds?/[^/]+/?$|[?&]feed=)")
_COMMENTS = re.compile(r"(?i)comment|komentar")
_OTHER_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:(?!\d)")
# Feed titles that name the format or the address, not the site: "RSS StoryEditor", "24ur.com RSS", "www.delo.si"
_GENERIC_TITLE = re.compile(r"(?i)^\W*(rss|feed|atom|www\.)|\b(rss|feed|atom)\W*$")


class FinderError(Exception):
    """A reason, worded for the reader, why the address can't be looked at."""


@dataclass
class FoundFeed:
    url: str
    title: str
    entries: int
    name: str = ""  # suggested feed name: the title, or the site's own name when the title is generic
    per_day: float = 0.0
    newest_days: float | None = None
    headlines: list[str] = field(default_factory=list)
    known: str | None = None  # name of the feed the reader already has at this address


def finder_client(cfg: Config) -> httpx.Client:
    return httpx.Client(
        follow_redirects=True, max_redirects=5, timeout=TIMEOUT,
        headers={"User-Agent": cfg.http.user_agent,
                 "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/html;q=0.8, */*;q=0.5"},
        event_hooks={"request": [public_https_only]},
    )


def public_https_only(request: httpx.Request) -> None:
    """Runs before every request, redirects included: https, and a host on the public internet."""
    if request.url.scheme != "https":
        raise FinderError(f"{request.url.host} sends to a plain http address (https only)")
    host = request.url.host
    try:
        infos = socket.getaddrinfo(host, request.url.port or 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        raise FinderError(f"{host} doesn't exist") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise FinderError(f"{host} is not on the public internet")


def normalize_address(address: str) -> str:
    """'delo.si', 'http://delo.si/novice' and 'https://delo.si' all become https addresses."""
    address = address.strip()
    if not address:
        raise FinderError("Type a website or feed address")
    if "://" not in address:
        if _OTHER_SCHEME.match(address):  # javascript:…, mailto:…, but not delo.si:443
            raise FinderError("That doesn't look like a web address")
        address = "https://" + address.lstrip("/")
    parts = urlsplit(address)
    try:
        parts.port  # noqa: B018 - raises on a port that isn't a number
    except ValueError:
        raise FinderError("That doesn't look like a web address") from None
    if parts.scheme not in ("http", "https") or not parts.hostname or any(c.isspace() for c in address):
        raise FinderError("That doesn't look like a web address")
    return "https" + address[len(parts.scheme):]


def find_feeds(client: httpx.Client, address: str) -> list[FoundFeed]:
    """The feeds behind an address, best first. Empty when the site has none we could read."""
    url, body, content_type = _get(client, normalize_address(address))
    if feed := _as_feed(url, body, content_type):
        return [_named(feed, None)]
    text = body.decode("utf-8", errors="replace")
    if is_challenge_page(text):
        raise FinderError(f"{urlsplit(url).hostname} blocks automated visits, so its feed can't be found from here")
    if "html" not in content_type and not text.lstrip()[:200].lower().startswith(("<!doctype html", "<html")):
        raise FinderError(f"{url} is neither a web page nor a feed")
    site_name = _site_name(text)
    return [_named(f, site_name) for f in _search_page(client, url, text)]


def _search_page(client: httpx.Client, url: str, text: str) -> list[FoundFeed]:
    advertised, linked = _feed_links(url, text)
    if found := _feeds_among(client, advertised):
        return found
    tried, listing_read = {url, *advertised}, False
    for candidate in _unique(linked[:MAX_CANDIDATES] + _common_paths(url)):
        if candidate in tried:
            continue
        tried.add(candidate)
        try:
            final, body, kind = _get(client, candidate)
        except FinderError:
            continue
        if feed := _as_feed(final, body, kind):
            return [feed]
        if "html" in kind and candidate in linked and not listing_read:
            # The site's "RSS" link opens a page listing its feeds (one per section): offer those
            listing_read = True
            listed = [u for u in _unique(sum(_feed_links(final, body.decode("utf-8", errors="replace")), []))
                      if u not in tried]
            if found := _feeds_among(client, listed):
                return found
    return []


def _site_name(html: str) -> str | None:
    """The name a page gives its site (og:site_name, as in link previews), when it gives one."""
    tree = LexborHTMLParser(html)
    for selector in ('meta[property="og:site_name"]', 'meta[name="application-name"]'):
        if (tag := tree.css_first(selector)) is not None and (name := html_to_text(tag.attributes.get("content"), limit=80)):
            return name
    return None


def _named(feed: FoundFeed, site_name: str | None) -> FoundFeed:
    if not _GENERIC_TITLE.search(feed.title):
        feed.name = feed.title
    else:
        feed.name = site_name or _site(feed.url)
    return feed


def _feeds_among(client: httpx.Client, urls: list[str]) -> list[FoundFeed]:
    """The addresses that turn out to be feeds, each feed once, at most MAX_CANDIDATES of them."""
    found: list[FoundFeed] = []
    for url in urls[:MAX_CANDIDATES * 2]:
        if len(found) >= MAX_CANDIDATES:
            break
        try:
            final, body, kind = _get(client, url)
        except FinderError:
            continue
        feed = _as_feed(final, body, kind)
        if feed and normalize_url(feed.url) not in {normalize_url(f.url) for f in found}:
            found.append(feed)
    return found


def _get(client: httpx.Client, url: str) -> tuple[str, bytes, str]:
    """Final address, body (at most MAX_BYTES) and content type. Every failure is a FinderError."""
    host = urlsplit(url).hostname
    try:
        with client.stream("GET", url) as r:
            if r.status_code >= 400:
                raise FinderError(f"{host} answered HTTP {r.status_code}")
            if r.url.scheme != "https":
                raise FinderError(f"{host} sends to a plain http address (https only)")
            if int(r.headers.get("content-length") or 0) > MAX_BYTES:
                raise FinderError(f"{r.url} is too large to be a feed")
            chunks, size = [], 0
            for chunk in r.iter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise FinderError(f"{r.url} is too large to be a feed")
                chunks.append(chunk)
            return str(r.url), b"".join(chunks), r.headers.get("content-type", "").lower()
    except httpx.TooManyRedirects:
        raise FinderError(f"{host} redirects too many times") from None
    except httpx.TimeoutException:
        raise FinderError(f"{host} took too long to answer") from None
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise FinderError(f"{host} couldn't be reached ({type(e).__name__})") from None


def _as_feed(url: str, body: bytes, content_type: str = "") -> FoundFeed | None:
    # A file object, never the bytes themselves: feedparser treats a string it is given as a path or URL to open
    parsed = feedparser.parse(io.BytesIO(body), response_headers={"content-type": content_type})
    if not parsed.version:
        return None
    entries = parsed.entries
    dates = [calendar.timegm(d) for e in entries if (d := e.get("published_parsed") or e.get("updated_parsed"))]
    feed = FoundFeed(url=url, title=html_to_text(parsed.feed.get("title"), limit=80) or urlsplit(url).hostname,
                     entries=len(entries),
                     headlines=[t for e in entries[:3] if (t := html_to_text(e.get("title"), limit=140))])
    if dates:
        feed.newest_days = max(0.0, (time.time() - max(dates)) / 86400)
        feed.per_day = round(len(dates) / max(1.0, (max(dates) - min(dates)) / 86400), 1)
    return feed


def _feed_links(base: str, html: str) -> tuple[list[str], list[str]]:
    """Feeds the page advertises in <link rel="alternate"> (comment feeds last), and same-site <a> links
    that look like feeds. Only http(s) addresses; http is tried as https."""
    tree = LexborHTMLParser(html)
    if (tag := tree.css_first("base[href]")) is not None:
        base = urljoin(base, tag.attributes.get("href") or "")
    advertised = []
    for link in tree.css("link[href]"):
        attrs = link.attributes
        rels = (attrs.get("rel") or "").lower().split()
        kind = (attrs.get("type") or "").lower().split(";")[0].strip()
        if "alternate" in rels and kind in FEED_TYPES and (url := _absolute(base, attrs.get("href"))):
            advertised.append((bool(_COMMENTS.search(f"{attrs.get('title')} {url}")), url))
    site = _site(base)
    linked = [url for a in tree.css("a[href]") if (url := _absolute(base, a.attributes.get("href")))
              and _FEEDISH_LINK.search(url) and _same_site(url, site)]
    return _unique(url for _, url in sorted(advertised, key=lambda c: c[0])), _unique(linked)


def _common_paths(url: str) -> list[str]:
    parts = urlsplit(url)
    root = f"https://{parts.netloc}"
    paths = [root + p for p in COMMON_PATHS]
    if parts.path.strip("/"):  # a blog under a path: https://example.com/blog → /blog/feed/
        paths.insert(0, f"{root}/{parts.path.strip('/')}/feed/")
    return paths


def _absolute(base: str, href: str | None) -> str | None:
    url = urljoin(base, (href or "").strip())
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return "https" + url[len(parts.scheme):]


def _site(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.removeprefix("www.")


def _same_site(url: str, site: str) -> bool:
    """www.rtvslo.si's feeds on img.rtvslo.si count as its own; links to other sites don't."""
    host = urlsplit(url).hostname or ""
    return host == site or host.endswith("." + site)


def _unique(urls) -> list[str]:
    return list(dict.fromkeys(urls))
