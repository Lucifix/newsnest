"""The read-only demo (demo/): builds offline, every saved link leads to a saved file, and nothing can be sent."""

import re
import socket
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from selectolax.lexbor import LexborHTMLParser

from demo import build as demo_build
from newsnest.config import load_config

BASE = "/newsnest/demo/"
DEMO_DIR = Path(demo_build.HERE)


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    target = tmp_path_factory.mktemp("demo") / "site"
    with pytest.MonkeyPatch.context() as mp:
        def no_network(*args, **kwargs):
            raise AssertionError("the demo build must not use the network")
        mp.setattr(socket.socket, "connect", no_network)
        demo_build.build(target, BASE)
    return target


def pages(out):
    return sorted(out.rglob("index.html"))


def internal_target(out, value):
    """The file an internal link leads to, or None for links the browser leaves alone."""
    if not value or value.startswith(("#", "mailto:")):
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return None
    assert parts.path.startswith(BASE), f"link outside the demo: {value}"
    path = out / parts.path[len(BASE):]
    return path / "index.html" if parts.path.endswith("/") else path


def test_pages_exist(out):
    for folder in ("", "swipe", "settings", "profile"):
        assert (out / folder / "index.html").is_file(), folder
    assert (out / "demo.js").is_file() and (out / "demo.css").is_file()


def test_every_link_leads_to_a_file(out):
    for page in pages(out):
        tree = LexborHTMLParser(page.read_text(encoding="utf-8"))
        for node in tree.css("[href],[src],[data-fragment],[data-href],form"):
            if node.tag == "form":
                if (node.attributes.get("method") or "get").lower() != "get":
                    continue
                values = [node.attributes.get("action")]
            else:
                values = [node.attributes.get(a) for a in ("href", "src", "data-fragment", "data-href")]
            for value in values:
                target = internal_target(out, value)
                if target is not None:
                    assert target.is_file(), f"{page.relative_to(out)}: {value} leads nowhere"


def test_pages_are_marked_and_wired(out):
    for page in pages(out):
        html = page.read_text(encoding="utf-8")
        rel = page.relative_to(out)
        if rel.parts[0] == "feed":  # the feed's "load more" pieces are inserted into a page that has all this
            assert "<html" not in html and 'href="/go/' not in html, rel
            continue
        assert 'class="demo-strip"' in html, rel
        assert html.index(f"{BASE}demo.js") < html.index("static/app.js"), rel
        assert 'rel="manifest"' not in html, rel
        assert 'href="/go/' not in html and "sumbtn" not in html, rel


def test_assets(out):
    css = (out / "static" / "app.css").read_text(encoding="utf-8")
    assert "url(/static/" not in css and f"url({BASE}static/" in css


def test_home_page_shows_the_stories(out):
    home = (out / "index.html").read_text(encoding="utf-8")
    assert "Coastal cities agree on a shared flood-defence fund" in home
    assert 'class="points"' in home
    assert "Full coverage ·" in home
    assert "Teach NewsNest what you care about" not in home  # the profile is not empty


def test_story_links_show_a_note_instead_of_leaving(out):
    home = (out / "index.html").read_text(encoding="utf-8")
    assert re.search(r'data-demo-off="This story is made up', home)
    assert 'href="https://example.com' not in home


@pytest.fixture
def loaded():
    result = load_config(DEMO_DIR)
    assert not result.errors, result.errors
    return result.config


def with_change(tmp_path, change):
    cards = yaml.safe_load((DEMO_DIR / "stories.yaml").read_text(encoding="utf-8"))
    change(cards)
    path = tmp_path / "stories.yaml"
    path.write_text(yaml.safe_dump(cards), encoding="utf-8")
    return path


def test_stories_file_is_valid(loaded):
    cards = demo_build.load_stories(DEMO_DIR / "stories.yaml", loaded)
    assert 24 <= len(cards) <= 40


def test_bad_score_is_refused(tmp_path, loaded):
    path = with_change(tmp_path, lambda cards: cards[0].update(topic=11))
    with pytest.raises(demo_build.BuildError, match="topic must be a whole number"):
        demo_build.load_stories(path, loaded)


def test_unknown_outlet_is_refused(tmp_path, loaded):
    path = with_change(tmp_path, lambda cards: cards[0]["articles"][0].update(outlet="nowhere-news"))
    with pytest.raises(demo_build.BuildError, match="unknown outlet"):
        demo_build.load_stories(path, loaded)
