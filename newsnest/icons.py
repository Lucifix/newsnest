"""Source icons: found, downloaded and stored by the server, so a reader's browser only ever talks to NewsNest.

Where an icon comes from, first good one wins:
1. the feed's own image (Atom <icon>, RSS <image>, Atom <logo>): the publisher's logo, even when the feed
   is hosted elsewhere (FeedBurner, hnrss)
2. the site's <link rel="apple-touch-icon">: usually 180 px and opaque, so sharp and fine in dark mode
3. the site's <link rel="icon">, largest declared size first
4. /favicon.ico at the site's root
Nothing usable: the badge keeps showing initials.

"Good" means square and at least GOOD_SIZE px, so it stays sharp on a phone. When nothing is good, the
largest usable (roughly square, at least MIN_SIZE px) candidate is kept instead. Only PNG, JPEG, GIF, WebP
and ICO are kept, recognised from their bytes; SVG is never stored (it can carry scripts).
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import sqlite3
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterator
from urllib.parse import urljoin, urlsplit

import httpx
from selectolax.lexbor import LexborHTMLParser

from newsnest.config import Source
from newsnest.db import dumps, iso, loads, parse_iso, transaction
from newsnest.models import FeedMeta

log = logging.getLogger(__name__)

MAX_ICON_BYTES = 256 * 1024
MAX_PAGE_BYTES = 1024 * 1024
MIN_SIZE = 16        # smaller than this is not worth showing
GOOD_SIZE = 64       # a 22-34 px badge on a 2-3x phone screen
MAX_LINK_ICONS = 6   # <link> candidates tried per site
REFRESH_FOUND = timedelta(days=30)
RETRY_MISSING = timedelta(days=7)
PER_TICK = 4         # sources looked at per tick, so one tick never stalls on slow sites
TICK_BUDGET_S = 30.0


@dataclass
class Icon:
    data: bytes
    mime: str
    width: int
    height: int
    origin: str      # the URL it was downloaded from

    @property
    def usable(self) -> bool:
        short, long = sorted((self.width, self.height))
        return short >= MIN_SIZE and long <= short * 1.5

    @property
    def good(self) -> bool:
        short, long = sorted((self.width, self.height))
        return short >= GOOD_SIZE and long <= short * 1.2


# ---------- recognising images from their bytes ----------

def image_info(data: bytes) -> tuple[str, int, int] | None:
    """(mime type, width, height) for PNG, GIF, JPEG, WebP and ICO; None for anything else (HTML, SVG...)."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            w, h = struct.unpack(">II", data[16:24])
            return "image/png", w, h
        if data[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack("<HH", data[6:10])
            return "image/gif", w, h
        if data[:2] == b"\xff\xd8":
            return _jpeg_info(data)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return _webp_info(data)
        if data[:4] == b"\x00\x00\x01\x00":
            return _ico_info(data)
    except (struct.error, IndexError):
        return None
    return None


def _jpeg_info(data: bytes) -> tuple[str, int, int] | None:
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF or marker == 0x01 or 0xD0 <= marker <= 0xD9:  # fill byte or a marker without a length
            i += 1 if marker == 0xFF else 2
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):  # start of frame: holds the size
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return "image/jpeg", w, h
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return None


def _webp_info(data: bytes) -> tuple[str, int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8 ":
        w, h = struct.unpack("<HH", data[26:30])
        return "image/webp", w & 0x3FFF, h & 0x3FFF
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        return "image/webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":
        return "image/webp", int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    return None


def _ico_info(data: bytes) -> tuple[str, int, int] | None:
    count = struct.unpack("<H", data[4:6])[0]
    if not count or len(data) < 6 + 16 * count:
        return None
    sizes = [(data[6 + 16 * n] or 256, data[7 + 16 * n] or 256) for n in range(count)]  # 0 means 256
    w, h = max(sizes, key=lambda s: s[0] * s[1])
    return "image/x-icon", w, h


# ---------- finding candidates ----------

def link_icons(html: str, page_url: str) -> list[str]:
    """Icon URLs a page declares: apple-touch-icon first, then rel=icon; largest declared size first.
    SVG is skipped, relative addresses are resolved against <base href> or the page's own URL."""
    tree = LexborHTMLParser(html)
    base = tree.css_first("base[href]")
    base_url = urljoin(page_url, base.attributes.get("href") or "") if base else page_url
    touch: list[tuple[int, int, str]] = []
    plain: list[tuple[int, int, str]] = []
    for n, link in enumerate(tree.css("link[rel][href]")):
        rel = (link.attributes.get("rel") or "").lower().split()
        href = (link.attributes.get("href") or "").strip()
        kind = (link.attributes.get("type") or "").lower()
        if not href or href.startswith("data:") or "svg" in kind or urlsplit(href).path.lower().endswith(".svg"):
            continue
        size = _declared_size(link.attributes.get("sizes") or "")
        url = urljoin(base_url, href)
        if "apple-touch-icon" in rel or "apple-touch-icon-precomposed" in rel:
            touch.append((-(size or 180), n, url))   # undeclared touch icons are 180 px by convention
        elif "icon" in rel:
            plain.append((-size, n, url))
    seen: list[str] = []
    for _, _, url in sorted(touch) + sorted(plain):
        if url not in seen:
            seen.append(url)
    return seen[:MAX_LINK_ICONS]


def _declared_size(sizes: str) -> int:
    best = 0
    for token in sizes.lower().split():
        w, _, h = token.partition("x")
        if w.isdigit() and h.isdigit():
            best = max(best, min(int(w), int(h)))
    return best


def fetchable(url: str) -> bool:
    """Plain web addresses only: no other schemes, and nothing on this machine or the home network."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        return False
    if host == "localhost" or host.endswith((".localhost", ".local", ".lan", ".internal", ".home.arpa")):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return ip.is_global


def _download(client: httpx.Client, url: str, limit: int) -> httpx.Response | None:
    """GET with a size cap; None on any failure, an error status, a redirect off the web or a body too large."""
    if not fetchable(url):
        return None
    try:
        with client.stream("GET", url) as r:
            if r.status_code != 200 or not fetchable(str(r.url)):
                return None
            body = bytearray()
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > limit:
                    return None
            r._content = bytes(body)  # lets .content / .text work after the stream closed
            return r
    except httpx.HTTPError as e:
        log.debug("icon download failed: %s: %s", url, e)
        return None


def _fetch_icon(client: httpx.Client, url: str) -> Icon | None:
    r = _download(client, url, MAX_ICON_BYTES)
    info = image_info(r.content) if r is not None else None
    if not info:
        return None
    mime, w, h = info
    return Icon(r.content, mime, w, h, url)


def candidates(client: httpx.Client, meta: FeedMeta, feed_url: str) -> Iterator[str]:
    """Candidate icon URLs in order. The site's page is only downloaded once the feed's images ran out."""
    yield from meta.images
    site = meta.site_url or feed_url
    page = _download(client, site, MAX_PAGE_BYTES)
    if page is not None and "html" in page.headers.get("content-type", ""):
        yield from link_icons(page.text, str(page.url))
    root = page.url if page is not None else httpx.URL(site)
    yield f"{root.scheme}://{root.netloc.decode('ascii')}/favicon.ico"


def discover(client: httpx.Client, meta: FeedMeta, feed_url: str) -> Icon | None:
    """The first good icon, or else the largest usable one; None when there is nothing worth showing."""
    best: Icon | None = None
    tried: set[str] = set()
    for url in candidates(client, meta, feed_url):
        if url in tried:
            continue
        tried.add(url)
        icon = _fetch_icon(client, url)
        if icon is None or not icon.usable:
            continue
        if icon.good:
            return icon
        if best is None or min(icon.width, icon.height) > min(best.width, best.height):
            best = icon
    return best


# ---------- storage ----------

def note_feed(conn: sqlite3.Connection, source: Source, meta: FeedMeta) -> None:
    """Remember where a feed says its site and logo are. A change sends the source back for a new look."""
    row = conn.execute("SELECT site_url, feed_images FROM source_icons WHERE source_id = ?", (source.id,)).fetchone()
    images = dumps(meta.images)
    if row is not None and row["site_url"] == meta.site_url and row["feed_images"] == images:
        return
    with transaction(conn):
        conn.execute(
            """INSERT INTO source_icons (source_id, site_url, feed_images) VALUES (?, ?, ?)
               ON CONFLICT(source_id) DO UPDATE SET site_url = excluded.site_url,
                   feed_images = excluded.feed_images, checked_at = NULL""",
            (source.id, meta.site_url, images))


def known_sources(conn: sqlite3.Connection) -> set[str]:
    """Sources whose feed metadata is stored (their feed has been read in full at least once)."""
    return {r[0] for r in conn.execute("SELECT source_id FROM source_icons")}


def wants_icon(source: Source) -> bool:
    # Release feeds carry the project's avatar on every item; the site would only give GitHub's logo
    return source.enabled and source.type == "rss" and source.params.get("favicon", True)


def due(conn: sqlite3.Connection, sources: list[Source], now: datetime) -> list[Source]:
    """Sources to look at now: never checked first, then the longest ago."""
    rows = {r["source_id"]: r for r in conn.execute("SELECT source_id, checked_at, data IS NOT NULL AS has FROM source_icons")}
    picked: list[tuple[str, Source]] = []
    for s in sources:
        row = rows.get(s.id)
        if row is None or not wants_icon(s):   # no row: the feed hasn't told us its site yet
            continue
        checked = parse_iso(row["checked_at"])
        if checked is None or now - checked >= (REFRESH_FOUND if row["has"] else RETRY_MISSING):
            picked.append((row["checked_at"] or "", s))
    picked.sort(key=lambda p: p[0])
    return [s for _, s in picked]


def refresh(conn: sqlite3.Connection, sources: list[Source], client: httpx.Client, now: datetime,
            limit: int = PER_TICK, budget_s: float = TICK_BUDGET_S) -> int:
    """Look for icons of a few due sources; returns how many were looked at. Never raises."""
    start = time.monotonic()
    done = 0
    for source in due(conn, sources, now)[:limit]:
        if time.monotonic() - start > budget_s:
            break
        try:
            row = conn.execute("SELECT site_url, feed_images FROM source_icons WHERE source_id = ?",
                               (source.id,)).fetchone()
            icon = discover(client, FeedMeta(row["site_url"], loads(row["feed_images"], [])), source.url)
            store(conn, source.id, icon, now)
        except Exception:  # an odd site must not stop the tick
            log.exception("icon lookup crashed for %s", source.id)
        done += 1
    return done


def store(conn: sqlite3.Connection, source_id: str, icon: Icon | None, now: datetime) -> None:
    """Save what was found. Finding nothing keeps an icon found earlier: a site that is down for a day
    should not turn its logo back into initials."""
    with transaction(conn):
        if icon is None:
            conn.execute("UPDATE source_icons SET checked_at = ? WHERE source_id = ?", (iso(now), source_id))
            return
        conn.execute(
            """UPDATE source_icons SET checked_at = ?, origin = ?, mime = ?, width = ?, height = ?, data = ?,
               hash = ? WHERE source_id = ?""",
            (iso(now), icon.origin, icon.mime, icon.width, icon.height, icon.data,
             hashlib.sha1(icon.data).hexdigest()[:12], source_id))


def versions(conn: sqlite3.Connection) -> dict[str, str]:
    """source id -> content hash, for every source with an icon (the hash goes into the icon's address)."""
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT source_id, hash FROM source_icons WHERE data IS NOT NULL")}
    except sqlite3.OperationalError:  # not migrated yet
        return {}


def load(conn: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT mime, data, hash FROM source_icons WHERE source_id = ? AND data IS NOT NULL",
                        (source_id,)).fetchone()
