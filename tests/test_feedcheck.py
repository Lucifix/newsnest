import httpx
import pytest

from newsnest.fetchers.rss import check_new_feed, clean_feed_url

RSS = '<?xml version="1.0"?><rss version="2.0"><channel><title>City Magazine</title>{items}</channel></rss>'
ITEM = "<item><title>Story</title><link>https://x.example/1</link></item>"


def client_for(status=200, body=RSS.format(items=ITEM), ctype="application/rss+xml", error=None):
    def handler(request: httpx.Request):
        if error:
            raise error
        return httpx.Response(status, text=body, headers={"content-type": ctype})
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


@pytest.mark.parametrize("raw, clean", [
    ("[/](https://citymagazine.si/feed/)", "https://citymagazine.si/feed/"),
    ("  https://a.example/rss \n", "https://a.example/rss"),
    ("<https://a.example/rss>", "https://a.example/rss"),
    ("a.example/rss", "https://a.example/rss"),
    ("http://a.example/rss", "http://a.example/rss"),
    ("", ""),
])
def test_clean_feed_url(raw, clean):
    assert clean_feed_url(raw) == clean


def test_valid_feed_gives_its_title(cfg):
    check = check_new_feed(cfg, "https://a.example/rss", client_for())
    assert check.ok and check.title == "City Magazine"


def test_empty_but_valid_feed_is_allowed(cfg):
    assert check_new_feed(cfg, "https://a.example/rss", client_for(body=RSS.format(items=""))).ok


@pytest.mark.parametrize("url", ["http://a.example/rss", "not a url", "https:///rss"])
def test_non_https_address_is_refused_without_a_request(cfg, url):
    def boom(request):
        raise AssertionError("no request expected")
    check = check_new_feed(cfg, url, httpx.Client(transport=httpx.MockTransport(boom)))
    assert not check.ok and "https://" in check.message


@pytest.mark.parametrize("kwargs, expected", [
    (dict(body="<html><title>My site</title><body>hi</body></html>", ctype="text/html"), "isn't an RSS or Atom feed"),
    (dict(status=404), "HTTP 404. Check the address"),
    (dict(status=500), "HTTP 500"),
    (dict(status=403), "refused the request"),
    (dict(error=httpx.ConnectTimeout("slow")), "Couldn't reach a.example"),
])
def test_failures_say_what_is_wrong(cfg, kwargs, expected):
    check = check_new_feed(cfg, "https://a.example/rss", client_for(**kwargs))
    assert not check.ok and expected in check.message
