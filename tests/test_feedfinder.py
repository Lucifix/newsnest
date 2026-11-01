import socket

import httpx
import pytest

from newsnest.feedfinder import MAX_BYTES, FinderError, find_feeds, normalize_address, public_https_only
from tests.test_discovery import rss
from tests.test_web import client  # noqa: F401 - the app with stories in it

PAGE = "text/html; charset=utf-8"
FEED = "application/rss+xml"


def client_for(routes: dict, seen: list | None = None) -> httpx.Client:
    """routes: url -> (body, content type); anything else is a 404. `seen` collects the requested urls."""
    def handler(request: httpx.Request):
        if seen is not None:
            seen.append(str(request.url))
        if str(request.url) not in routes:
            return httpx.Response(404, text="", headers={"content-type": PAGE})
        body, kind = routes[str(request.url)]
        return httpx.Response(200, content=body.encode() if isinstance(body, str) else body, headers={"content-type": kind})
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def page(head: str = "", body: str = "") -> str:
    return f"<!doctype html><html><head><title>Site</title>{head}</head><body>{body}</body></html>"


def test_address_is_normalized():
    assert normalize_address(" delo.si ") == "https://delo.si"
    assert normalize_address("http://delo.si/novice") == "https://delo.si/novice"
    for bad in ("", "javascript:alert(1)", "file:///etc/passwd", "ftp://x.example/feed", "not an address"):
        with pytest.raises(FinderError):
            normalize_address(bad)


def test_a_feed_address_is_the_feed():
    c = client_for({"https://x.example/rss": (rss(5), FEED)})
    [feed] = find_feeds(c, "x.example/rss")
    assert (feed.url, feed.title, feed.entries) == ("https://x.example/rss", "Example", 5)
    assert feed.headlines == ["Story 0", "Story 1", "Story 2"]
    assert feed.newest_days < 1 and feed.per_day > 1


def test_advertised_feeds_come_first_and_comment_feeds_last():
    head = ('<link rel="alternate" type="application/rss+xml" title="Comments" href="/comments/feed/">'
            '<link rel="alternate" type="application/rss+xml" title="News" href="http://x.example/feed/">'
            '<link rel="stylesheet" href="/style.css">')
    c = client_for({"https://x.example": (page(head), PAGE),
                    "https://x.example/feed/": (rss(3, title="News {i}"), FEED),
                    "https://x.example/comments/feed/": (rss(3, title="Comment {i}"), FEED)})
    found = find_feeds(c, "https://x.example")
    # the http link is tried over https; the comment feed is offered, but after the real one
    assert [f.url for f in found] == ["https://x.example/feed/", "https://x.example/comments/feed/"]


def test_links_on_the_page_then_common_paths():
    c = client_for({"https://x.example": (page(body='<a href="https://other.example/rss">x</a><a href="/rss.xml">RSS</a>'), PAGE),
                    "https://x.example/rss.xml": (rss(), FEED)})
    assert [f.url for f in find_feeds(c, "x.example")] == ["https://x.example/rss.xml"]  # other sites' feeds are ignored
    seen = []
    c = client_for({"https://x.example/blog": (page(), PAGE), "https://x.example/rss": (rss(), FEED)}, seen)
    assert [f.url for f in find_feeds(c, "x.example/blog")] == ["https://x.example/rss"]
    assert seen == ["https://x.example/blog", "https://x.example/blog/feed/", "https://x.example/feed/", "https://x.example/rss"]


def test_a_generic_feed_title_gives_way_to_the_site_name():
    for title, site, name in (("RSS StoryEditor", "Dnevnik", "Dnevnik"), ("www.delo.si", None, "x.example"),
                              ("24ur.com RSS", "24ur.com", "24ur.com"), ("MMC RTV - Šport", "RTV SLO", "MMC RTV - Šport")):
        head = '<link rel="alternate" type="application/rss+xml" href="/feed/">'
        if site:
            head += f'<meta property="og:site_name" content="{site}">'
        c = client_for({"https://x.example": (page(head), PAGE),
                        "https://x.example/feed/": (rss().replace("<title>Example</title>", f"<title>{title}</title>"), FEED)})
        assert find_feeds(c, "x.example")[0].name == name


def test_a_failing_link_does_not_skip_the_common_path():
    # /feed redirects somewhere unusable; /feed/ (normalized the same) still has to be tried
    c = client_for({"https://x.example": (page(body='<a href="/feed">RSS</a>'), PAGE), "https://x.example/feed/": (rss(), FEED)})
    assert [f.url for f in find_feeds(c, "x.example")] == ["https://x.example/feed/"]


def test_a_page_listing_the_feeds_offers_each():
    # rtvslo.si: the footer's RSS link opens a page with one feed per section, served from another subdomain
    listing = page(body='<a href="https://img.x.example/feeds/00.xml">All</a><a href="https://img.x.example/feeds/01.xml">Sport</a>'
                        '<a href="https://evil.example/feed/">elsewhere</a>')
    c = client_for({"https://www.x.example": (page(body='<a href="/rss">RSS</a>'), PAGE),
                    "https://www.x.example/rss": (listing, PAGE),
                    "https://img.x.example/feeds/00.xml": (rss(title="All {i}"), FEED),
                    "https://img.x.example/feeds/01.xml": (rss(title="Sport {i}"), FEED),
                    "https://evil.example/feed/": (rss(), FEED)})
    assert [f.headlines[0] for f in find_feeds(c, "www.x.example")] == ["All 0", "Sport 0"]


def test_nothing_found_or_unreadable():
    assert find_feeds(client_for({"https://x.example": (page(), PAGE)}), "x.example") == []
    with pytest.raises(FinderError, match="HTTP 404"):
        find_feeds(client_for({}), "x.example")
    with pytest.raises(FinderError, match="blocks automated"):
        find_feeds(client_for({"https://x.example": ("<html><title>Just a moment...</title></html>", PAGE)}), "x.example")
    with pytest.raises(FinderError, match="neither"):
        find_feeds(client_for({"https://x.example": ("%PDF-1.4", "application/pdf")}), "x.example")


def test_hostile_pages_are_contained(tmp_path):
    # too large: refused while reading, also when the size isn't announced
    with pytest.raises(FinderError, match="too large"):
        find_feeds(client_for({"https://x.example": (b"<html>" + b"a" * (MAX_BYTES + 1), PAGE)}), "x.example")
    # a body that is the path of a feed on this machine is parsed as text, never opened
    # (feedparser opens a path or URL it is given as a string)
    local = tmp_path / "local.xml"
    local.write_text(rss(), encoding="utf-8")
    with pytest.raises(FinderError, match="neither"):
        find_feeds(client_for({"https://x.example": (str(local), "application/xml")}), "x.example")
    # feed links to other schemes are ignored; markup in a feed's title or headlines arrives as plain text
    hostile = rss(1, title="<script>alert(1)</script>Real <b>story</b>").replace(
        "<title>Example</title>", "<title>&lt;img src=x onerror=alert(1)&gt;Bad</title>")
    head = ('<link rel="alternate" type="application/rss+xml" href="javascript:alert(1)">'
            '<link rel="alternate" type="application/rss+xml" href="file:///etc/passwd">'
            '<link rel="alternate" type="application/rss+xml" href="/feed/">')
    c = client_for({"https://x.example": (page(head), PAGE), "https://x.example/feed/": (hostile, FEED)})
    [feed] = find_feeds(c, "x.example")
    assert "<" not in feed.title and all("<" not in h for h in feed.headlines)


def resolve_to(monkeypatch, ip: str) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))])


def test_only_public_https_hosts(monkeypatch):
    resolve_to(monkeypatch, "93.184.215.14")
    public_https_only(httpx.Request("GET", "https://x.example/feed"))
    for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "fd00::1"):
        resolve_to(monkeypatch, ip)
        with pytest.raises(FinderError, match="public internet"):
            public_https_only(httpx.Request("GET", "https://x.example/feed"))
    with pytest.raises(FinderError, match="https only"):
        public_https_only(httpx.Request("GET", "http://x.example/feed"))


def test_redirect_to_http_is_refused(monkeypatch):
    resolve_to(monkeypatch, "93.184.215.14")

    def handler(request):
        return httpx.Response(301, headers={"location": "http://x.example/feed"})
    c = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True, event_hooks={"request": [public_https_only]})
    with pytest.raises(FinderError, match="https only"):
        find_feeds(c, "x.example")


def test_find_then_add(client, env, monkeypatch):
    from newsnest import web
    hostile = rss(2, title="<script>alert(1)</script>Story {i}")
    routes = {"https://x.example": (page('<link rel="alternate" type="application/rss+xml" href="/feed/">'), PAGE),
              "https://x.example/feed/": (hostile, FEED)}
    monkeypatch.setattr(web, "finder_client", lambda cfg: client_for(routes))
    text = client.post("/settings/feed_find", data={"address": "x.example"}).text
    assert 'value="https://x.example/feed/"' in text and 'value="Example"' in text and "Story 0" in text
    assert "<script>alert" not in text
    # the reader picks the section; the name is the feed's title, numbered when another feed has it
    r = client.post("/settings/feed_add", data={"name": "  Example\n", "url": "https://x.example/feed/", "category": "news"},
                    follow_redirects=False)
    assert r.headers["location"] == "/settings/feeds?saved=feed_add#feed-example"
    r = client.post("/settings/feed_add", data={"name": "Example", "url": "https://y.example/feed", "category": "world"},
                    follow_redirects=False)
    assert r.headers["location"] == "/settings/feeds?saved=feed_add#feed-example-2"
    config = (env.config / "config.yaml").read_text(encoding="utf-8")
    assert "name: Example\n" in config and "id: example-2" in config
    # once added, finding it again says so instead of offering it twice
    assert "Already added as Example" in client.post("/settings/feed_find", data={"address": "x.example"}).text
    # nothing found and errors are shown in place
    routes.clear()
    assert "answered HTTP 404" in client.post("/settings/feed_find", data={"address": "x.example"}).text
    # addresses that aren't web addresses never reach config.yaml
    for url in ("javascript:alert(1)", "file:///etc/passwd", "https://"):
        r = client.post("/settings/feed_add", data={"name": "Bad", "url": url, "category": "news"}, follow_redirects=False)
        assert "error=" in r.headers["location"]
    assert (env.config / "config.yaml").read_text(encoding="utf-8") == config
