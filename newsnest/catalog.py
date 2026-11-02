"""The feed catalog: a checked set of feeds in topics, browsed in Settings → Discover and picked one by one.

Picks go on trial like any discovered feed (discovery.add_candidates). A topic is only a way to browse: it
suggests the section its feeds go to, either one the reader has (World, Tech, a "Science" they made) or a
new one they can create with the pick. Each feed's logo is looked up by the server (icons.py) under a stable
id, so the cards show logos while the browser only ever talks to NewsNest.

    python -m newsnest.catalog --check    # run every catalog feed through check_feed (while curating)
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sqlite3
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from newsnest import icons
from newsnest.config import ICONS, KNOWN_SECTIONS, WORLD, Config, SectionCfg
from newsnest.db import parse_iso, session, transaction, utcnow
from newsnest.fetchers.base import make_client
from newsnest.models import FeedMeta
from newsnest.textmatch import normalize_url

log = logging.getLogger(__name__)

CATALOG = Path(__file__).parent / "catalog.opml"
ICON_WORKERS = 4   # sites looked up at once while warming the cards' logos


@dataclass(frozen=True)
class CatalogFeed:
    name: str
    url: str
    description: str
    site: str   # where its logo is looked for: the site= attribute, else the feed's own origin

    @property
    def key(self) -> str:
        return normalize_url(self.url)

    @property
    def icon_id(self) -> str:
        """Its row in source_icons, stable across releases (a renamed feed keeps its logo)."""
        return "cat-" + hashlib.sha1(self.key.encode()).hexdigest()[:10]


@dataclass(frozen=True)
class Topic:
    key: str
    label: str
    section: str          # the section it suggests, by key
    section_label: str    # that section's name when the reader has to create it
    icon: str
    feeds: tuple[CatalogFeed, ...]


@cache
def load_catalog() -> tuple[Topic, ...]:
    """The topics in catalog.opml, checked: https only, every feed once, a known icon for a new section."""
    root = ET.fromstring(CATALOG.read_bytes())
    topics: list[Topic] = []
    seen: set[str] = set()
    for node in root.find("body").findall("outline"):
        section = node.get("section", WORLD)
        known: SectionCfg | None = KNOWN_SECTIONS.get(section)
        label = node.get("section_label") or (known.label if known else "")
        icon = node.get("icon") or (known.icon if known else "star")
        if not label or icon not in ICONS:
            raise ValueError(f"catalog topic {node.get('key')}: section {section!r} needs a section_label and an icon")
        feeds = []
        for f in node.findall("outline"):
            url = f.get("xmlUrl", "")
            if not url.startswith("https://"):
                raise ValueError(f"catalog feed {url!r}: https only")
            parts = urlsplit(url)
            feed = CatalogFeed(f.get("text") or url, url, f.get("description", ""),
                               f.get("site") or f"{parts.scheme}://{parts.netloc}")
            if feed.key in seen:
                raise ValueError(f"catalog feed {url!r} is listed twice")
            seen.add(feed.key)
            feeds.append(feed)
        topics.append(Topic(node.get("key"), node.get("text"), section, label, icon, tuple(feeds)))
    return tuple(topics)


def catalog_size() -> int:
    return sum(len(t.feeds) for t in load_catalog())


def find(urls: list[str]) -> list[tuple[Topic, CatalogFeed]]:
    """The catalog feeds among `urls`, each once, in catalog order. Anything not in the catalog is dropped."""
    wanted = {normalize_url(u) for u in urls if u}
    return [(t, f) for t in load_catalog() for f in t.feeds if f.key in wanted]


def feed_states(conn: sqlite3.Connection, cfg: Config) -> dict[str, str]:
    """Normalised URL -> "yours" (a regular source), "trial" (on trial or being checked) or "tried" (removed,
    retired or failed its check). Feeds the reader never had are left out."""
    states = {normalize_url(r["url"]): {"candidate": "trial", "checking": "trial", "promoted": "yours"}.get(r["status"], "tried")
              for r in conn.execute("SELECT url, status FROM candidates")}
    states.update({normalize_url(f.url): "yours" for f in cfg.feeds})
    return states


def destination(topic: Topic, sections: list[SectionCfg]) -> str | None:
    """The reader's section a topic's feeds go to: the suggested one by key, or one they made with the same
    name. None when they have neither, so the page asks."""
    want = topic.section_label.casefold()
    return next((s.key for s in sections if s.key == topic.section), None) or \
        next((s.key for s in sections if s.label.casefold() == want), None)


# ---------- logos ----------

_warm_lock = threading.Lock()


def warming() -> bool:
    return _warm_lock.locked()


def _due(rows: dict[str, sqlite3.Row], feed: CatalogFeed, now) -> bool:
    row = rows.get(feed.icon_id)
    checked = parse_iso(row["checked_at"]) if row is not None else None
    if checked is None:
        return True
    return now - checked >= (icons.REFRESH_FOUND if row["has"] else icons.RETRY_MISSING)


def icons_due(conn: sqlite3.Connection, now=None) -> dict[str, list[CatalogFeed]]:
    """Catalog feeds whose logo was never looked for (or is due again), by site: feeds of one site share it."""
    now = now or utcnow()
    rows = {r["source_id"]: r for r in conn.execute(
        "SELECT source_id, checked_at, data IS NOT NULL AS has FROM source_icons WHERE source_id LIKE 'cat-%'")}
    due: dict[str, list[CatalogFeed]] = {}
    for t in load_catalog():
        for f in t.feeds:
            if _due(rows, f, now):
                due.setdefault(f.site, []).append(f)
    return due


def warm_icons(cfg: Config, client: httpx.Client | None = None) -> int:
    """Look up the logos the catalog page is missing, a few sites at a time; returns the sites looked at.
    One run at a time: opening the page again while it runs does nothing. Safe to call from a thread."""
    if not _warm_lock.acquire(blocking=False):
        return 0
    own = client is None
    client = client or make_client(cfg)
    try:
        with session() as conn:
            due = icons_due(conn)
            if not due:
                return 0
            with transaction(conn):
                conn.executemany("INSERT OR IGNORE INTO source_icons (source_id, site_url, feed_images) VALUES (?, ?, '[]')",
                                 [(f.icon_id, site) for site, feeds in due.items() for f in feeds])

            def look(site: str, feeds: list[CatalogFeed]) -> icons.Icon | None:
                try:
                    return icons.discover(client, FeedMeta(site, []), feeds[0].url)
                except Exception:  # an odd site must not stop the others
                    log.exception("catalog icon lookup crashed for %s", site)
                    return None

            with ThreadPoolExecutor(ICON_WORKERS, thread_name_prefix="catalog-icon") as pool:
                jobs = {pool.submit(look, site, feeds): feeds for site, feeds in due.items()}
                for job in as_completed(jobs):   # stored as they arrive, so the page can show them
                    now = utcnow()
                    for f in jobs[job]:
                        icons.store(conn, f.icon_id, job.result(), now)
            return len(due)
    finally:
        if own:
            client.close()
        _warm_lock.release()


def start_icon_thread(cfg: Config) -> None:
    if not warming():
        threading.Thread(target=warm_icons, args=(cfg,), daemon=True, name="catalog-icons").start()


def found_icons(conn: sqlite3.Connection) -> dict[str, str]:
    """icon id -> address, for every catalog logo found so far."""
    return {sid: f"/icon/{sid}?v={v}" for sid, v in icons.versions(conn).items() if sid.startswith("cat-")}


def copy_icons(conn: sqlite3.Connection, picks: list[CatalogFeed]) -> None:
    """Give picked feeds' trial sources the logo already found for their card, so it shows straight away.
    The source's own feed may later point somewhere better; icons.note_feed then sends it for a new look."""
    by_url = {p.key: p for p in picks}
    with transaction(conn):
        for r in conn.execute("SELECT source_id, url FROM candidates").fetchall():
            pick = by_url.get(normalize_url(r["url"]))
            if pick is None:
                continue
            conn.execute(
                """INSERT OR IGNORE INTO source_icons (source_id, site_url, feed_images, checked_at, origin, mime,
                       width, height, data, hash)
                   SELECT ?, site_url, feed_images, checked_at, origin, mime, width, height, data, hash
                   FROM source_icons WHERE source_id = ? AND data IS NOT NULL""",
                (r["source_id"], pick.icon_id))


# ---------- curating ----------

def check_all(cfg: Config) -> list[tuple[CatalogFeed, str]]:
    """Every catalog feed through discovery.check_feed, one request per host at a time. Returns the failures."""
    from newsnest.discovery import HOST_SPACING, check_feed

    by_host: dict[str, list[CatalogFeed]] = {}
    for t in load_catalog():
        for f in t.feeds:
            by_host.setdefault(urlsplit(f.url).hostname or "", []).append(f)

    def run(feeds: list[CatalogFeed]) -> list[tuple[CatalogFeed, str]]:
        failed = []
        with httpx.Client(follow_redirects=True, timeout=cfg.http.timeout_seconds,
                          headers={"User-Agent": cfg.http.user_agent}) as client:
            for i, f in enumerate(feeds):
                if i:
                    time.sleep(HOST_SPACING)
                res = check_feed(client, f.url)
                if not res.ok:
                    failed.append((f, res.reason))
        return failed

    with ThreadPoolExecutor(16) as pool:
        return [x for chunk in pool.map(run, by_host.values()) for x in chunk]


def main() -> None:
    parser = argparse.ArgumentParser(description="The feed catalog")
    parser.add_argument("--check", action="store_true", help="check every catalog feed and list the failures")
    args = parser.parse_args()
    if not args.check:
        for t in load_catalog():
            print(f"{t.label}: {len(t.feeds)} feeds → {t.section_label}")
        print(f"{catalog_size()} feeds")
        return
    failed = check_all(Config())
    for f, reason in failed:
        print(f"FAIL {f.name}: {reason} ({f.url})")
    print(f"{catalog_size() - len(failed)} of {catalog_size()} feeds ok")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
