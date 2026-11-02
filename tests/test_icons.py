import io
import struct
from datetime import datetime, timedelta, timezone

import feedparser
import httpx
import pytest

from newsnest import icons
from newsnest.config import Source
from newsnest.fetchers.base import FetchContext
from newsnest.fetchers.rss import RssFetcher, feed_meta
from newsnest.models import FeedMeta

NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
SOURCE = Source(id="verge", type="rss", category="tech", name="The Verge", url="https://feeds.example/verge.xml")


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Test hosts don't resolve; treat every name as a public one unless a test says otherwise."""
    monkeypatch.setattr(icons, "resolves_public", lambda host, port: True)


def png(w: int, h: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00" + b"\0" * 16


def ico(*sizes: int) -> bytes:
    head = struct.pack("<HHH", 0, 1, len(sizes))
    return head + b"".join(bytes([s % 256, s % 256]) + b"\0" * 14 for s in sizes) + b"\0" * 32


def client_for(routes: dict[str, tuple[bytes | str, str]], log: list[str] | None = None) -> httpx.Client:
    def handler(request: httpx.Request):
        if log is not None:
            log.append(str(request.url))
        if str(request.url) not in routes:
            return httpx.Response(404)
        body, kind = routes[str(request.url)]
        return httpx.Response(200, content=body.encode() if isinstance(body, str) else body,
                              headers={"content-type": kind})
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


# ---------- recognising images ----------

def test_image_types_are_recognised_from_their_bytes():
    assert icons.image_info(png(180, 180)) == ("image/png", 180, 180)
    assert icons.image_info(b"GIF89a" + struct.pack("<HH", 48, 32) + b"\0" * 8) == ("image/gif", 48, 32)
    jpeg = b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0" + b"\0" * 9 + \
        b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 120, 160) + b"\0" * 12
    assert icons.image_info(jpeg) == ("image/jpeg", 160, 120)
    vp8x = b"RIFF\0\0\0\0WEBPVP8X" + b"\0" * 8 + (63).to_bytes(3, "little") + (63).to_bytes(3, "little")
    assert icons.image_info(vp8x) == ("image/webp", 64, 64)
    assert icons.image_info(ico(16, 32, 0)) == ("image/x-icon", 256, 256)   # 0 means 256 in an .ico


@pytest.mark.parametrize("body", [b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
                                  b"<!doctype html><title>Just a moment</title>", b"", b"\x89PNG"])
def test_anything_but_a_raster_image_is_refused(body):
    assert icons.image_info(body) is None


def test_good_and_usable_sizes():
    assert icons.Icon(b"", "image/png", 180, 180, "").good
    assert not icons.Icon(b"", "image/png", 32, 32, "").good and icons.Icon(b"", "image/png", 32, 32, "").usable
    assert not icons.Icon(b"", "image/png", 144, 40, "").usable   # a wide banner would be unreadable as a badge
    assert not icons.Icon(b"", "image/png", 8, 8, "").usable


# ---------- finding candidates ----------

def test_link_icons_prefer_touch_icons_then_the_largest_icon():
    html = """<html><head><base href="https://cdn.example/assets/">
      <link rel="icon" href="fav-16.png" sizes="16x16">
      <link rel="icon" type="image/svg+xml" href="logo.svg">
      <link rel="mask-icon" href="pinned.png">
      <link rel="shortcut icon" href="/fav-96.png" sizes="96x96">
      <link rel="apple-touch-icon" href="touch.png">
      <link rel="apple-touch-icon" sizes="120x120" href="touch-120.png">
      <link rel="stylesheet" href="site.css"></head></html>"""
    assert icons.link_icons(html, "https://site.example/") == [
        "https://cdn.example/assets/touch.png",        # undeclared touch icon: 180 by convention
        "https://cdn.example/assets/touch-120.png",
        "https://cdn.example/fav-96.png",
        "https://cdn.example/assets/fav-16.png",
    ]


def test_feed_meta_reads_the_site_and_logo_images():
    rss = """<?xml version="1.0"?><rss version="2.0"><channel><title>T</title><link>https://www.site.example/</link>
      <image><url>/img/logo.png</url><title>T</title><link>https://www.site.example/</link></image>
      <item><title>a</title><link>https://www.site.example/a</link></item></channel></rss>"""
    meta = feed_meta(feedparser.parse(io.BytesIO(rss.encode())).feed, "https://feeds.feedburner.com/site")
    assert meta.site_url == "https://www.site.example/"            # the publisher, not FeedBurner
    assert meta.images == ["https://feeds.feedburner.com/img/logo.png"]
    atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>T</title>
      <link rel="alternate" href="https://a.example/"/><icon>https://a.example/icon.png</icon>
      <logo>https://a.example/banner.png</logo><entry><title>a</title><link href="https://a.example/1"/></entry></feed>"""
    meta = feed_meta(feedparser.parse(io.BytesIO(atom.encode())).feed, "https://a.example/feed")
    assert meta.site_url == "https://a.example/"
    assert meta.images[0] == "https://a.example/icon.png" and "https://a.example/banner.png" in meta.images


def test_the_fetcher_reports_feed_meta_on_a_full_read_only():
    rss = """<?xml version="1.0"?><rss version="2.0"><channel><title>T</title><link>https://site.example/</link>
      <item><title>a</title><link>https://site.example/a</link></item></channel></rss>"""

    def handler(request):
        if request.headers.get("if-none-match"):
            return httpx.Response(304)
        return httpx.Response(200, text=rss, headers={"content-type": "application/rss+xml", "etag": '"v1"'})
    from newsnest.config import Config
    ctx = FetchContext(Config(), httpx.Client(transport=httpx.MockTransport(handler)))
    full = RssFetcher().fetch(SOURCE, ctx)
    assert full.feed_meta == FeedMeta("https://site.example/", [])
    again = Source(**{**SOURCE.__dict__, "params": {"etag": '"v1"'}})
    assert RssFetcher().fetch(again, ctx).feed_meta is None


@pytest.mark.parametrize("url", ["http://localhost/x.png", "http://192.168.1.10/favicon.ico", "http://10.0.0.1/a",
                                 "http://router.lan/favicon.ico", "file:///etc/passwd", "ftp://site.example/a.png"])
def test_nothing_on_the_home_network_is_fetched(url):
    assert not icons.fetchable(url)


def test_public_addresses_are_fetched():
    assert icons.fetchable("https://www.site.example/favicon.ico") and icons.fetchable("http://8.8.8.8/x.png")


def test_a_name_pointing_at_the_home_network_is_not_fetched(monkeypatch):
    monkeypatch.setattr(icons, "resolves_public", lambda host, port: host != "nas.site.example")
    assert not icons.fetchable("https://nas.site.example/favicon.ico")
    assert icons.fetchable("https://www.site.example/favicon.ico")


def test_every_redirect_hop_is_checked_before_it_is_requested():
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "site.example":
            return httpx.Response(302, headers={"location": "http://192.168.1.1/favicon.ico"})
        return httpx.Response(200, content=png(64, 64), headers={"content-type": "image/png"})
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    assert icons._download(client, "https://site.example/favicon.ico", icons.MAX_ICON_BYTES) is None
    assert seen == ["https://site.example/favicon.ico"]


# ---------- choosing ----------

def test_a_good_feed_image_wins_without_loading_the_site():
    calls: list[str] = []
    client = client_for({"https://site.example/logo.png": (png(144, 144), "image/png")}, calls)
    icon = icons.discover(client, FeedMeta("https://site.example/", ["https://site.example/logo.png"]), SOURCE.url)
    assert icon.origin == "https://site.example/logo.png" and icon.width == 144
    assert calls == ["https://site.example/logo.png"]


def test_a_small_feed_image_gives_way_to_the_touch_icon():
    page = '<link rel="icon" href="/fav.png" sizes="32x32"><link rel="apple-touch-icon" href="/touch.png">'
    client = client_for({
        "https://site.example/cropped-32x32.png": (png(32, 32), "image/png"),   # WordPress feeds carry these
        "https://site.example/": (page, "text/html; charset=utf-8"),
        "https://site.example/touch.png": (png(180, 180), "image/png"),
    })
    icon = icons.discover(client, FeedMeta("https://site.example/", ["https://site.example/cropped-32x32.png"]), SOURCE.url)
    assert icon.origin == "https://site.example/touch.png"


def test_rel_icon_then_favicon_ico_then_the_largest_usable():
    page = '<link rel="icon" href="/fav.png" sizes="16x16"><link rel="icon" href="/logo.svg">'
    client = client_for({
        "https://site.example/news": (page, "text/html"),
        "https://site.example/fav.png": (png(16, 16), "image/png"),
        "https://site.example/favicon.ico": (ico(16, 32, 48), "image/x-icon"),
        "https://site.example/logo.svg": ("<svg/>", "image/svg+xml"),
    })
    icon = icons.discover(client, FeedMeta("https://site.example/news", []), SOURCE.url)
    assert icon.origin == "https://site.example/favicon.ico" and icon.mime == "image/x-icon" and icon.width == 48


def test_a_wide_banner_and_a_page_without_icons_leave_initials():
    client = client_for({
        "https://site.example/banner.png": (png(600, 100), "image/png"),
        "https://site.example/": ("<html><head></head></html>", "text/html"),
    })
    assert icons.discover(client, FeedMeta("https://site.example/", ["https://site.example/banner.png"]), SOURCE.url) is None


def test_an_html_error_page_is_not_an_icon():
    client = client_for({"https://site.example/favicon.ico": ("<html>not found</html>", "text/html")})
    assert icons.discover(client, FeedMeta(None, []), "https://site.example/feed") is None


def test_an_oversized_download_is_dropped():
    client = client_for({"https://site.example/huge.png": (png(512, 512) + b"\0" * icons.MAX_ICON_BYTES, "image/png")})
    assert icons.discover(client, FeedMeta(None, ["https://site.example/huge.png"]), "http://127.0.0.1/feed") is None


# ---------- storage and schedule ----------

def test_refresh_stores_the_icon_and_keeps_it_through_an_outage(conn):
    icons.note_feed(conn, SOURCE, FeedMeta("https://site.example/", ["https://site.example/logo.png"]))
    good = client_for({"https://site.example/logo.png": (png(128, 128), "image/png")})
    assert icons.refresh(conn, [SOURCE], good, NOW) == 1
    first = icons.versions(conn)["verge"]
    assert icons.load(conn, "verge")["mime"] == "image/png"
    assert icons.refresh(conn, [SOURCE], good, NOW + timedelta(days=1)) == 0       # not due again for a month
    later = NOW + icons.REFRESH_FOUND
    assert icons.refresh(conn, [SOURCE], client_for({}), later) == 1               # the site is down
    assert icons.versions(conn)["verge"] == first                                  # the logo stays


def test_a_changed_feed_logo_is_looked_at_again(conn):
    icons.note_feed(conn, SOURCE, FeedMeta("https://site.example/", []))
    icons.refresh(conn, [SOURCE], client_for({}), NOW)
    assert icons.due(conn, [SOURCE], NOW + timedelta(days=1)) == []
    icons.note_feed(conn, SOURCE, FeedMeta("https://site.example/", []))           # unchanged: still not due
    assert icons.due(conn, [SOURCE], NOW + timedelta(days=1)) == []
    icons.note_feed(conn, SOURCE, FeedMeta("https://site.example/", ["https://site.example/new.png"]))
    assert icons.due(conn, [SOURCE], NOW + timedelta(days=1)) == [SOURCE]


def test_release_feeds_paused_feeds_and_opted_out_feeds_are_skipped(conn):
    gh = Source(id="gh-x", type="github_releases", category="tech", name="x releases", url="https://github.com/x.atom")
    off = Source(id="off", type="rss", category="tech", name="Off", url="https://off.example/feed", params={"favicon": False})
    paused = Source(id="paused", type="rss", category="tech", name="P", url="https://p.example/feed", enabled=False)
    for s in (gh, off, paused):
        conn.execute("INSERT INTO source_icons (source_id) VALUES (?)", (s.id,))
    assert icons.due(conn, [gh, off, paused], NOW) == []


def test_a_feed_without_known_metadata_is_read_in_full_once(env, conn, monkeypatch):
    """Installs from before source icons have an etag for every feed; one unconditional read gets the site."""
    from newsnest import runner
    from newsnest.config import load_config
    loaded = load_config(env.config)
    loaded.sources = [Source(id="verge", type="rss", category="tech", name="The Verge", url="https://site.example/rss")]
    conn.execute("INSERT INTO source_state (source_id, last_run, etag) VALUES ('verge', '2020-01-01T00:00:00+00:00', '\"v1\"')")
    rss = """<?xml version="1.0"?><rss version="2.0"><channel><title>T</title><link>https://site.example/</link>
      <item><title>a</title><link>https://site.example/a</link></item></channel></rss>"""
    seen: list[str | None] = []

    def handler(request):
        if request.url.path == "/rss":
            seen.append(request.headers.get("if-none-match"))
            if request.headers.get("if-none-match"):
                return httpx.Response(304)
            return httpx.Response(200, text=rss, headers={"content-type": "application/rss+xml", "etag": '"v1"'})
        if request.url.path == "/favicon.ico":
            return httpx.Response(200, content=ico(32, 64), headers={"content-type": "image/x-icon"})
        return httpx.Response(404)
    monkeypatch.setattr(runner, "make_client", lambda cfg: httpx.Client(transport=httpx.MockTransport(handler)))
    summary = runner.fetch_sources(conn, loaded, force=True)
    assert seen == [None] and summary["icons"] == 1
    assert icons.load(conn, "verge")["mime"] == "image/x-icon"
    runner.fetch_sources(conn, loaded, force=True)
    assert seen == [None, '"v1"']        # from now on, conditional again
