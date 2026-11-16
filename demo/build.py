"""Builds the read-only demo: a static copy of the real app's pages, filled with made-up stories.

It starts the app in-process against a throwaway database, requests every page a visitor can reach and saves
each response as HTML under OUT, with links rewritten to the demo's base path. Nothing in newsnest/ changes;
demo.js (loaded before app.js on every page) stops every save.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import shutil
import tempfile
from collections import deque
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit

import yaml
from selectolax.lexbor import LexborHTMLParser

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_BASE = "/newsnest/demo/"
MAX_PAGES = 300
# The website's photos, plus the demo's own (credits in demo/photos/CREDITS.md)
PHOTO_DIRS = (ROOT / "site" / "photos", HERE / "photos")
STRIP = ('<div class="demo-strip" role="note">You\'re trying NewsNest with made-up stories. Changes aren\'t saved. '
         '<a href="https://lucifix.github.io/newsnest/#start">Install it free →</a></div>')
# Shown when a story's headline or "Open article" is tapped: the stories are made up
NO_ARTICLE = "This story is made up, so there's no article to open"

# Routes the build never requests; links to them are switched off ("<label> isn't part of the demo")
OFF_PREFIXES = [
    ("/search", "Search"), ("/login", "Login"), ("/logout", "Logout"),
    ("/settings/backup", "The database backup"), ("/settings/discover/catalog", "The feed catalog"),
    ("/health", "The health check"), ("/sw.js", "Offline mode"), ("/manifest.webmanifest", "Installing"),
    ("/offline", "Offline mode"), ("/discovery/", "Discovery"),
]
ASSET_PREFIXES = ("/static/", "/photos/")
LINK_ATTRS = ("href", "src", "data-fragment", "data-href")
STATES = {"read", "saved", "liked", "disliked"}


class BuildError(Exception):
    pass


# ---------------------------------------------------------------- stories

def load_stories(path: Path, config) -> list[dict]:
    """The stories file, checked. Fails with a message that names the card."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise BuildError(f"{path.name}: expected a list of cards")
    sections = {s.key for s in config.all_sections}
    feeds = {f.id for f in config.feeds}
    categories = set(config.scoring.categories)
    photos = {p.name for folder in PHOTO_DIRS for p in folder.glob("*.webp")}
    seen_events: set[str] = set()
    for n, card in enumerate(raw, 1):
        name = f"card {n} ({card.get('event', '?')})" if isinstance(card, dict) else f"card {n}"

        def bad(msg: str, name=name) -> BuildError:
            return BuildError(f"{path.name}, {name}: {msg}")

        if not isinstance(card, dict):
            raise bad("not a mapping")
        for key in ("section", "hours_ago", "event", "category", "topic", "importance", "personal", "reason",
                    "summary", "articles"):
            if key not in card:
                raise bad(f"missing {key!r}")
        if card["section"] not in sections:
            raise bad(f"unknown section {card['section']!r}")
        if card["category"] not in categories:
            raise bad(f"unknown category {card['category']!r}")
        for key in ("topic", "importance", "personal"):
            if not isinstance(card[key], int) or isinstance(card[key], bool) or not 0 <= card[key] <= 10:
                raise bad(f"{key} must be a whole number from 0 to 10")
        if not isinstance(card["hours_ago"], (int, float)) or not 0.25 <= card["hours_ago"] <= 30:
            raise bad("hours_ago must be between 0.25 and 30")
        if card["event"] in seen_events:
            raise bad("duplicate event")
        seen_events.add(card["event"])
        summary = card["summary"]
        if not isinstance(summary, dict) or not summary.get("lede"):
            raise bad("summary needs a lede")
        if not isinstance(summary.get("points"), list) or not 2 <= len(summary["points"]) <= 4:
            raise bad("summary needs 2 to 4 points")
        if summary.get("depth") not in ("enough", "full"):
            raise bad("summary depth must be 'enough' or 'full'")
        if card.get("image") and card["image"] not in photos:
            raise bad(f"image {card['image']!r} is not in site/photos/ or demo/photos/")
        if not set(card.get("state") or []) <= STATES:
            raise bad(f"state must be a list of {sorted(STATES)}")
        articles = card["articles"]
        if not isinstance(articles, list) or not 1 <= len(articles) <= 4:
            raise bad("1 to 4 articles")
        outlets = [a.get("outlet") for a in articles]
        if len(set(outlets)) != len(outlets):
            raise bad("each article must come from a different outlet")
        sources = {f.id: f for f in config.feeds}
        for a in articles:
            if a.get("outlet") not in feeds:
                raise bad(f"unknown outlet {a.get('outlet')!r}")
            if sources[a["outlet"]].category != card["section"]:
                raise bad(f"outlet {a['outlet']!r} is not in the {card['section']!r} section")
            if not a.get("title") or not a.get("snippet"):
                raise bad("every article needs a title and a snippet")
    return raw


# ---------------------------------------------------------------- environment

@contextlib.contextmanager
def throwaway_env(work: Path, password: str):
    config, data = work / "config", work / "data"
    config.mkdir()
    data.mkdir()
    shutil.copy(HERE / "config.yaml", config / "config.yaml")
    shutil.copy(HERE / "interests.md", config / "interests.md")
    changes = {"NEWSNEST_CONFIG_DIR": str(config), "NEWSNEST_DATA_DIR": str(data), "NEWSNEST_SCHEDULER": "off",
               "NEWSNEST_PASSWORD": password, "ANTHROPIC_API_KEY": None, "OPENAI_API_KEY": None}
    before = {k: os.environ.get(k) for k in changes}
    try:
        for key, value in changes.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        # The legacy names win over the new ones in bootstrap.env(): clear them for the build
        for key in ("RADAR_CONFIG_DIR", "RADAR_DATA_DIR", "RADAR_SCHEDULER", "RADAR_PASSWORD"):
            before.setdefault(key, os.environ.get(key))
            os.environ.pop(key, None)
        yield config, data
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ---------------------------------------------------------------- seed

def seed(conn, loaded, stories: list[dict]) -> None:
    from newsnest.db import dumps, iso, set_meta, utcnow
    from newsnest.grouping import touch_story
    from newsnest.models import Item
    from newsnest.pipeline import ingest
    from newsnest.watches import Prefilter

    now = utcnow()
    cfg = loaded.config
    by_id = {s.id: s for s in loaded.sources}
    prefilter = Prefilter(cfg)
    cards: list[tuple[dict, list[int]]] = []
    for card in stories:
        ids = []
        for a in sorted(card["articles"], key=lambda x: -(x.get("minutes_earlier") or 0)):
            source = by_id[a["outlet"]]
            fetched = now - timedelta(hours=card["hours_ago"], minutes=a.get("minutes_earlier") or 0)
            url = f"https://example.com/demo/{card['event']}/{a['outlet']}"
            extra = {"image": f"/photos/{card['image']}"} if card.get("image") else {}
            item = Item(source.id, source.category, a["title"], url, url, fetched, snippet=a["snippet"], extra=extra)
            ids += ingest(conn, source, [item], prefilter, fetched)
        if len(ids) != len(card["articles"]):
            raise BuildError(f"{card['event']}: an article was not stored (duplicate URL?)")
        cards.append((card, ids))

    def stories_of(ids):
        marks = ",".join("?" * len(ids))
        return {r[0] for r in conn.execute(f"SELECT story_id FROM items WHERE id IN ({marks})", ids)}

    # Two different cards must not have been joined by the importer's headline matching
    owner: dict[int, dict] = {}
    for card, ids in cards:
        for sid in stories_of(ids):
            if sid in owner and owner[sid] is not card:
                raise BuildError("these cards were grouped together by headline matching; reword one: "
                                 f"{owner[sid]['articles'][0]['title']!r} / {card['articles'][0]['title']!r}")
            owner[sid] = card

    for card, ids in cards:
        marks = ",".join("?" * len(ids))
        story = min(stories_of(ids))
        conn.execute(f"UPDATE items SET story_id = ? WHERE id IN ({marks})", (story, *ids))
        touch_story(conn, story, now)
        s = card["summary"]
        why = s.get("why") if card["personal"] >= 6 else None
        conn.execute(
            f"""UPDATE items SET score_status = 'scored', topic = ?, importance = ?, personal = ?, reason = ?,
                ai_category = ?, scored_at = fetched_at, event = ?, summary = ?, summary_points = ?,
                summary_why = ?, summary_depth = ?, summary_depth_tag = ?, summary_source = 'article',
                summary_status = 'done' WHERE id IN ({marks})""",
            (card["topic"], card["importance"], card["personal"], card["reason"], card["category"], card["event"],
             s["lede"], dumps(s["points"]), why, s["depth"], s.get("depth_tag") if s["depth"] == "full" else None,
             *ids))
        state = set(card.get("state") or [])
        if "read" in state:
            conn.execute(f"UPDATE items SET read_at = ?, last_read_at = ? WHERE id IN ({marks})",
                         (iso(now - timedelta(hours=1)), iso(now - timedelta(hours=1)), *ids))
        if "saved" in state:
            conn.execute(f"UPDATE items SET saved_at = ? WHERE id IN ({marks})", (iso(now - timedelta(hours=2)), *ids))
        for name, value in (("liked", 1), ("disliked", -1)):
            if name in state:
                conn.execute(f"UPDATE items SET rating = ?, rated_at = ? WHERE id IN ({marks})",
                             (value, iso(now - timedelta(hours=1)), *ids))
    conn.execute("DELETE FROM stories WHERE id NOT IN (SELECT story_id FROM items WHERE story_id IS NOT NULL)")

    # No warning banners: the scheduler "ran" just now, the AI "works", every feed looks healthy
    set_meta(conn, "last_tick", iso(now))
    set_meta(conn, "ai_status", dumps({"ok": True, "at": iso(now)}))
    for source in loaded.sources:
        conn.execute(
            """INSERT INTO source_state (source_id, last_run, last_status, last_http, last_success, last_nonempty,
               consecutive_failures, raw_count, new_count) VALUES (?, ?, 'ok', 200, ?, ?, 0, 10, 0)""",
            (source.id, iso(now), iso(now), iso(now)))


# ---------------------------------------------------------------- urls

def url_attrs(tree):
    """Every (node, attribute) that holds a URL the browser GETs, each exactly once. One selector per attribute:
    a combined selector returns a node once per matching attribute, and it would then be rewritten twice."""
    for attr in LINK_ATTRS:
        for node in tree.css(f"[{attr}]"):
            if node.attributes.get(attr) is None:
                continue
            if node.tag == "link" and "manifest" in (node.attributes.get("rel") or ""):
                continue
            yield node, attr
    for node in tree.css("form[action]"):
        if (node.attributes.get("method") or "get").lower() == "get" and node.attributes.get("action"):
            yield node, "action"


def is_fragment(path: str) -> bool:
    """/feed/more answers with a piece of HTML, not a whole page."""
    return path.startswith("/feed/")


def canon_query(query: str) -> str:
    return urlencode(sorted(parse_qsl(query, keep_blank_values=True)))


def page_dir(path: str, query: str) -> str:
    """Folder of the page file for an internal URL, relative to the output ('' for the home page)."""
    if not re.fullmatch(r"[A-Za-z0-9/_.-]*", path):
        raise BuildError(f"unexpected characters in URL path {path!r}")
    parts = [p for p in path.split("/") if p]
    if query:
        parts.append("q-" + hashlib.sha1(query.encode()).hexdigest()[:10])
    return "/".join(parts)


class Crawler:
    def __init__(self, client, headers: dict, conn, base: str):
        self.client, self.headers, self.conn, self.base = client, headers, conn, base
        self.pages: dict[tuple[str, str], str] = {}        # final key -> html
        self.alias: dict[tuple[str, str], tuple[str, str]] = {}  # requested key -> final key
        self.parents: dict[tuple[str, str], str] = {}
        self.days: set[str] | None = None  # the days that have stories (from the archive page)

    # classify one attribute value of a page at page_url
    def classify(self, value: str, page_url: str):
        value = value.strip()
        if not value or value.startswith(("#", "mailto:", "javascript:", "data:", "tel:")):
            return ("keep",)
        full = urljoin(page_url, value)
        parts = urlsplit(full)
        if parts.scheme or parts.netloc:
            return ("keep",)
        path = parts.path or "/"
        if path.startswith(ASSET_PREFIXES):
            return ("asset", path, parts.query, parts.fragment)
        if path.startswith("/go/"):
            return ("go", path[4:])
        if path.startswith(("/api/", "/icon/")):
            raise BuildError(f"{page_url} links to {value!r}, which the demo cannot serve")
        day = re.fullmatch(r"/day/(\d{4}-\d{2}-\d{2})", path)
        if day and self.days is not None and day.group(1) not in self.days:
            return ("off", "That day")  # the day pages link on to earlier days for ever
        for prefix, label in OFF_PREFIXES:
            if path == prefix.rstrip("/") or path.startswith(prefix):
                return ("off", label)
        return ("page", path, canon_query(parts.query), parts.fragment)

    def links(self, html: str, page_url: str):
        for node, attr in url_attrs(LexborHTMLParser(html)):
            yield node, attr, self.classify(node.attributes[attr], page_url)

    def fetch(self, path: str, query: str) -> tuple[tuple[str, str], str]:
        key = (path, query)
        for _ in range(4):
            self.client.cookies.clear()
            url = path + (f"?{query}" if query else "")
            response = self.client.get(url, headers=self.headers, follow_redirects=False)
            if response.status_code in (301, 302, 303, 307, 308):
                target = urlsplit(response.headers["location"])
                if target.netloc and target.netloc != "testserver":
                    raise BuildError(f"{url} redirects away to {response.headers['location']}")
                path, query = target.path or "/", canon_query(target.query)
                continue
            if response.status_code != 200:
                raise BuildError(f"{url} answered {response.status_code}, linked from {self.parents.get(key, '?')}")
            if "text/html" not in response.headers.get("content-type", ""):
                raise BuildError(f"{url} is not a page ({response.headers.get('content-type')})")
            return (path, query), response.text
        raise BuildError(f"too many redirects from {path}")

    def crawl(self, starts: list[str]) -> None:
        _final, archive = self.fetch("/archive", "")
        self.days = set(re.findall(r'href="/day/(\d{4}-\d{2}-\d{2})"', archive))
        queue = deque((s, "") for s in starts)
        while queue:
            key = queue.popleft()
            if key in self.alias:
                continue
            final, html = self.fetch(*key)
            self.alias[key] = final
            if final in self.pages:
                continue
            if len(self.pages) >= MAX_PAGES:
                raise BuildError(f"more than {MAX_PAGES} pages; something is linking without end")
            self.pages[final] = html
            page_url = final[0] + (f"?{final[1]}" if final[1] else "")
            for _node, _attr, found in self.links(html, page_url):
                if found[0] == "page":
                    nxt = (found[1], found[2])
                    if nxt not in self.alias:
                        self.parents.setdefault(nxt, page_url)
                        queue.append(nxt)

    # ---- output
    def page_href(self, key: tuple[str, str], fragment: str = "") -> str:
        final = self.alias.get(key, key)
        folder = page_dir(*final)
        href = self.base + (folder + "/" if folder else "")
        return href + (f"#{fragment}" if fragment else "")

    def check_story(self, item_id: str) -> None:
        row = self.conn.execute("SELECT 1 FROM items WHERE id = ?", (int(item_id),)).fetchone() \
            if item_id.isdigit() else None
        if not row:
            raise BuildError(f"link to unknown story /go/{item_id}")

    def render(self, final: tuple[str, str]) -> str:
        page_url = final[0] + (f"?{final[1]}" if final[1] else "")
        html = self.pages[final]
        tree = LexborHTMLParser(html)
        for node in tree.css('link[rel="manifest"]'):
            node.decompose()
        for node, attr in list(url_attrs(tree)):
            found = self.classify(node.attributes[attr], page_url)
            kind = found[0]
            if kind == "keep":
                continue
            if kind == "asset":
                _k, path, query, fragment = found
                node.attrs[attr] = self.base + path.lstrip("/") + (f"?{query}" if query else "") \
                    + (f"#{fragment}" if fragment else "")
            elif kind == "page":
                node.attrs[attr] = self.page_href((found[1], found[2]), found[3])
            else:  # "go" (a story's article) or "off" (a route the demo leaves out): a note instead
                if node.tag not in ("a", "form"):
                    raise BuildError(f"{page_url}: <{node.tag} {attr}> points at a link the demo switches off")
                if kind == "go":
                    self.check_story(found[1])
                node.attrs[attr] = "#"
                node.attrs["data-demo-off"] = NO_ARTICLE if kind == "go" else f"{found[1]} isn't part of the demo"
                for extra in ("download", "target"):
                    if extra in node.attributes:
                        del node.attrs[extra]
        if is_fragment(final[0]):  # the next page of the feed, fetched and inserted by app.js: no page around it
            return tree.body.inner_html if tree.body else ""
        out = tree.html
        if not out.lstrip().lower().startswith("<!doctype"):
            out = "<!doctype html>" + out
        out, n = re.subn(r"(<script\b[^>]*static/app\.js[^>]*></script>)",
                         lambda m: f'<link rel="stylesheet" href="{self.base}demo.css">'
                                   f'<script src="{self.base}demo.js"></script>' + m.group(1), out, count=1)
        if n != 1:
            raise BuildError(f"{page_url}: no app.js script tag to put demo.js before")
        out, n = re.subn(r"(<body\b[^>]*>)", lambda m: m.group(1) + STRIP, out, count=1)
        if n != 1:
            raise BuildError(f"{page_url}: no <body> tag")
        return out


# ---------------------------------------------------------------- build

def build(out: Path, base: str = DEFAULT_BASE) -> int:
    """Writes the demo into out (emptied first). Returns the number of pages."""
    if not (base.startswith("/") and base.endswith("/")):
        raise BuildError("base must start and end with '/'")
    out = Path(out)
    password = secrets.token_urlsafe(24)
    with tempfile.TemporaryDirectory(prefix="newsnest-demo-") as work, throwaway_env(Path(work), password):
        from fastapi.testclient import TestClient

        from newsnest.config import load_config
        from newsnest.db import connect, migrate

        loaded = load_config(Path(os.environ["NEWSNEST_CONFIG_DIR"]))
        if loaded.errors:
            raise BuildError("demo/config.yaml: " + "; ".join(loaded.errors))
        stories = load_stories(HERE / "stories.yaml", loaded.config)
        conn = connect()
        migrate(conn)
        seed(conn, loaded, stories)

        import newsnest.web as web
        headers = {"cookie": f"{web.SESSION_COOKIE}={web._session_token(password)}"}
        with TestClient(web.app) as client:
            crawler = Crawler(client, headers, conn, base)
            crawler.crawl(["/", "/swipe", "/profile", "/settings"])
            if out.exists():
                shutil.rmtree(out)
            out.mkdir(parents=True)
            for final in crawler.pages:
                folder = page_dir(*final)
                target = out / folder / "index.html"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(crawler.render(final), encoding="utf-8")
        conn.close()
        count = len(crawler.pages)

    # assets
    shutil.copytree(ROOT / "newsnest" / "static", out / "static")
    css = out / "static" / "app.css"
    css.write_text(css.read_text(encoding="utf-8").replace("url(/static/", f"url({base}static/"), encoding="utf-8")
    (out / "photos").mkdir()
    for folder in PHOTO_DIRS:
        for photo in folder.glob("*.webp"):
            shutil.copy(photo, out / "photos" / photo.name)
    shutil.copy(HERE / "demo.js", out / "demo.js")
    shutil.copy(HERE / "demo.css", out / "demo.css")
    (out / ".nojekyll").touch()
    return count


def serve(base: str = DEFAULT_BASE, port: int = 8000) -> None:
    import functools
    import http.server

    with tempfile.TemporaryDirectory(prefix="newsnest-demo-serve-") as tmp:
        root = Path(tmp)
        count = build(root / base.strip("/"), base)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
        with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
            print(f"Built {count} pages. Demo at http://localhost:{port}{quote(base)}  (Ctrl-C to stop)")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass
