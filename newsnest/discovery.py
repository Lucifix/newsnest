"""Discovered sources: a pool of candidate feeds that have to earn their place.

Feeds come from an OPML file (a link or an upload) or from the bundled starter pack. Each is checked
once (https, working, real and recent entries, not a podcast or video channel, not a duplicate),
then polled gently. Their stories reach the feed only when they clear the score threshold, tagged
"Discovered". A candidate whose stories I open or 👍 is suggested for promotion to a regular source;
one that never produces anything good, or keeps failing, is retired automatically.
"""

from __future__ import annotations

import calendar
import io
import logging
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import feedparser
import httpx

from newsnest.config import LEGACY_SECTIONS, NEWS, WORLD, Config, Source, slugify
from newsnest.db import dumps, iso, parse_iso, session, utcnow
from newsnest.scoring import final_score
from newsnest.sections import section_keys
from newsnest.textmatch import fold, normalize_url

log = logging.getLogger(__name__)

STARTER_PACK = Path(__file__).parent / "discovery_starter.opml"
STALE_DAYS = 14
HOST_SPACING = 2.0  # seconds between requests to the same host


@dataclass
class OpmlFeed:
    url: str
    title: str
    category: str   # a section key: news (Local), world, tech, lifestyle or one of the reader's own


def parse_opml(data: bytes | str, default_category: str = "tech", local_words: tuple[str, ...] = (),
               keys: tuple[str, ...] = tuple(s.key for s in LEGACY_SECTIONS)) -> list[OpmlFeed]:
    """Every outline with an xmlUrl. Category: the outline's own category attribute (a section key in `keys`),
    else the parent folder's name mapped to a section, else the default. local_words are extra folder
    words that mean Local (from region_words). Tech and Lifestyle folders only map when those sections exist."""
    root = ET.fromstring(data)
    out: list[OpmlFeed] = []

    def walk(node, folder: str | None):
        for child in node.findall("outline"):
            url = child.get("xmlUrl")
            if url:
                cat = (child.get("category") or "").strip().lower()
                if cat not in keys:
                    cat = _map_folder(folder, local_words, keys) or default_category
                out.append(OpmlFeed(url.strip(), (child.get("title") or child.get("text") or url).strip(), cat))
            else:
                walk(child, child.get("title") or child.get("text") or folder)

    body = root.find("body")
    walk(body if body is not None else root, None)
    return out


# Whole words: "men" must not match "Development"
_LIFESTYLE_WORDS = re.compile(r"\b(lifestyle|gear|style|men'?s?|watch(es)?|design|edc|carry|cars?|fashion|grooming)\b")
_TECH_WORDS = ("tech", "security", "apple", "android", "programming", "develop", "diy", "linux", "self", "home", "gadget")


_LOCAL_WORDS = ("local", "lokal", "regional")


def region_words(cfg: Config) -> tuple[str, ...]:
    """Folder words that mean Local for this reader: the start of their country and city ("sloven", "ljubl")."""
    return tuple(fold(w)[:5] for w in (cfg.region.country, cfg.region.city) if len(w.strip()) >= 3)


def _map_folder(folder: str | None, local_words: tuple[str, ...] = (),
                keys: tuple[str, ...] = tuple(s.key for s in LEGACY_SECTIONS)) -> str | None:
    if not folder:
        return None
    f = fold(folder)
    if "lifestyle" in keys and _LIFESTYLE_WORDS.search(f):
        return "lifestyle"
    if "tech" in keys and any(w in f for w in _TECH_WORDS):
        return "tech"
    # Local only when the folder says so: a "News" folder in someone's OPML is BBC and the Guardian
    return NEWS if any(w in f for w in _LOCAL_WORDS + local_words) else WORLD


@dataclass
class CheckResult:
    ok: bool
    reason: str
    title: str | None = None
    final_url: str | None = None
    per_day: float = 0.0


def check_feed(client: httpx.Client, url: str) -> CheckResult:
    """One request: https, HTTP 200, a parsable feed with entries, something posted in the last
    two weeks, and not a podcast or video channel."""
    if not url.startswith("https://"):
        return CheckResult(False, "plain http (https only)")
    try:
        r = client.get(url)
    except httpx.HTTPError as e:
        return CheckResult(False, f"{type(e).__name__}")
    if r.status_code != 200:
        return CheckResult(False, f"HTTP {r.status_code}")
    if r.url.scheme != "https":
        return CheckResult(False, "redirects to plain http")
    parsed = feedparser.parse(io.BytesIO(r.content))  # never the bytes: feedparser opens a body that is a local path
    entries = parsed.entries
    if not entries:
        return CheckResult(False, "no entries")
    media = sum(1 for e in entries if e.get("itunes_duration") or any(
        enc.get("type", "").startswith(("audio", "video")) for enc in e.get("enclosures") or []))
    if media >= len(entries) / 2 or "youtube.com" in str(r.url):
        return CheckResult(False, "podcast or video channel")
    dates = [calendar.timegm(e.get("published_parsed") or e.get("updated_parsed"))
             for e in entries if e.get("published_parsed") or e.get("updated_parsed")]
    if not dates:
        return CheckResult(False, "entries have no dates")
    newest_days = (time.time() - max(dates)) / 86400
    if newest_days > STALE_DAYS:
        return CheckResult(False, f"no new post in {int(newest_days)} days")
    span = max(1.0, (max(dates) - min(dates)) / 86400)
    return CheckResult(True, "ok", (parsed.feed.get("title") or "").strip() or None, str(r.url), round(len(dates) / span, 1))


def _key(url: str) -> str:
    return normalize_url(url)


def known_feed_keys(conn: sqlite3.Connection, cfg: Config) -> set[str]:
    keys = {_key(f.url) for f in cfg.feeds}
    keys |= {_key(r["url"]) for r in conn.execute("SELECT url FROM candidates")}
    return keys


def starter_missing(conn: sqlite3.Connection, cfg: Config) -> list[OpmlFeed]:
    """Starter-pack feeds that are neither regular sources nor candidates yet (empty once the pack was added)."""
    known = known_feed_keys(conn, cfg)
    return [f for f in parse_opml(STARTER_PACK.read_bytes(), keys=section_keys(cfg)) if _key(f.url) not in known]


def add_candidates(conn: sqlite3.Connection, cfg: Config, feeds: list[OpmlFeed], origin: str) -> int:
    """Queue feeds for checking; duplicates of regular sources or earlier candidates are skipped."""
    known = known_feed_keys(conn, cfg)
    taken = {r["source_id"] for r in conn.execute("SELECT source_id FROM candidates")} | {f.id for f in cfg.feeds}
    keys = section_keys(cfg)
    added = 0
    for f in feeds:
        if _key(f.url) in known:
            continue
        if f.category not in keys:   # a section the reader doesn't have (the starter pack's tech feeds, say)
            f = OpmlFeed(f.url, f.title, WORLD)
        known.add(_key(f.url))
        sid = base = f"disc-{slugify(f.title, 30)}"
        n = 2
        while sid in taken:
            sid, n = f"{base}-{n}", n + 1
        taken.add(sid)
        conn.execute("INSERT INTO candidates (source_id, url, title, category, origin, status, added_at) "
                     "VALUES (?, ?, ?, ?, ?, 'checking', ?)", (sid, f.url, f.title, f.category, origin, iso(utcnow())))
        added += 1
    return added


_check_lock = threading.Lock()


def check_pending(cfg: Config, client: httpx.Client | None = None) -> dict:
    """Check every queued candidate, spacing requests to the same host. Safe to call from a thread."""
    if not _check_lock.acquire(blocking=False):
        return {"skipped": "a check is already running"}
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=cfg.http.timeout_seconds,
                                    headers={"User-Agent": cfg.http.user_agent})
    done = {"candidate": 0, "rejected": 0}
    try:
        with session() as conn:
            rows = conn.execute("SELECT * FROM candidates WHERE status = 'checking' ORDER BY id").fetchall()
            last_hit: dict[str, float] = {}
            for r in rows:
                host = urlsplit(r["url"]).hostname or ""
                wait = last_hit.get(host, 0) + HOST_SPACING - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                res = check_feed(client, r["url"])
                last_hit[host] = time.monotonic()
                status = "candidate" if res.ok else "rejected"
                done[status] += 1
                conn.execute("UPDATE candidates SET status = ?, checked_at = ?, check_result = ?, title = COALESCE(?, title) "
                             "WHERE id = ?",
                             (status, iso(utcnow()), dumps({"reason": res.reason, "per_day": res.per_day}),
                              r["title"] if r["title"] and not r["title"].startswith("http") else res.title, r["id"]))
    finally:
        if own:
            client.close()
        _check_lock.release()
    return done


def start_check_thread(cfg: Config) -> None:
    threading.Thread(target=check_pending, args=(cfg,), daemon=True, name="feed-check").start()


def discovery_sources(conn: sqlite3.Connection, cfg: Config) -> list[Source]:
    """Candidates as pollable sources: slower interval, their own daily cap, marked discovered."""
    if not cfg.discovery.enabled:
        return []
    return [Source(id=r["source_id"], type="rss", category=r["category"], name=r["title"] or r["source_id"],
                   url=r["url"], daily_cap=cfg.discovery.daily_cap_per_source,
                   params={"discovered": True, "interval": cfg.discovery.poll_minutes})
            for r in conn.execute("SELECT * FROM candidates WHERE status = 'candidate' ORDER BY id")]


def cap_trial_scoring(conn: sqlite3.Connection, cfg: Config) -> int:
    """Mark pending items of discovered sources beyond discovery.scored_per_source (per 24 h) as 'capped', so
    they are never scored. A trial source can show only daily_cap_per_source stories, yet busy ones (Phys.org,
    Android Authority) publish 100+ items a day, and scoring all of them was ~40% of the AI bill.
    Returns how many items were capped."""
    quota = cfg.discovery.scored_per_source
    if not cfg.discovery.enabled or not quota:
        return 0
    since = iso(utcnow() - timedelta(days=1))
    capped = 0
    for r in conn.execute("SELECT source_id FROM candidates WHERE status = 'candidate'").fetchall():
        sid = r["source_id"]
        used = conn.execute("SELECT COUNT(*) FROM items WHERE source_id = ? AND scored_at >= ?", (sid, since)).fetchone()[0]
        pending = conn.execute(
            """SELECT id FROM items WHERE source_id = ? AND score_status = 'pending'
               AND COALESCE(prefilter, '') != 'must_show' ORDER BY COALESCE(published, fetched_at) DESC, id DESC""",
            (sid,)).fetchall()
        over = [(p["id"],) for p in pending[max(0, quota - used):]]
        conn.executemany("UPDATE items SET score_status = 'capped' WHERE id = ?", over)
        capped += len(over)
    return capped


def _signals(conn: sqlite3.Connection, source_id: str, since: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM items WHERE source_id = ? AND (opened_at >= ? OR (rating = 1 AND rated_at >= ?))",
        (source_id, since, since)).fetchone()[0]


def promotion_suggestions(conn: sqlite3.Connection, cfg: Config) -> list[dict]:
    """Candidates whose stories I opened or liked often enough this week."""
    since = iso(utcnow() - timedelta(days=7))
    out = []
    for r in conn.execute("SELECT * FROM candidates WHERE status = 'candidate'"):
        n = _signals(conn, r["source_id"], since)
        if n >= cfg.discovery.promote_after:
            out.append({"source_id": r["source_id"], "title": r["title"], "url": r["url"], "category": r["category"], "signals": n})
    return sorted(out, key=lambda d: -d["signals"])


def retire_candidates(conn: sqlite3.Connection, cfg: Config) -> list[str]:
    """Retire candidates that never produced a story above the threshold in their trial period, or
    that keep failing. Runs at most once a day."""
    now = utcnow()
    retired = []
    trial = timedelta(days=cfg.discovery.retire_after_days)
    for r in conn.execute("SELECT c.*, s.consecutive_failures FROM candidates c "
                          "LEFT JOIN source_state s ON s.source_id = c.source_id WHERE c.status = 'candidate'").fetchall():
        reason = None
        if (r["consecutive_failures"] or 0) >= 5:
            reason = "kept failing"
        elif now - parse_iso(r["checked_at"] or r["added_at"]) > trial:
            rows = conn.execute("SELECT i.*, st.source_count FROM items i JOIN stories st ON st.id = i.story_id "
                                "WHERE i.source_id = ? AND i.score_status = 'scored'", (r["source_id"],)).fetchall()
            good = [x for x in rows if (final_score(x, x["source_count"], cfg) or 0) >= cfg.discovery.min_final]
            if not good and not _signals(conn, r["source_id"], iso(now - trial)):
                reason = f"no story above {cfg.discovery.min_final} in {cfg.discovery.retire_after_days} days"
        if reason:
            conn.execute("UPDATE candidates SET status = 'retired', decided_at = ?, check_result = ? WHERE id = ?",
                         (iso(now), dumps({"reason": reason}), r["id"]))
            retired.append(r["source_id"])
    return retired


def named_sources(conn: sqlite3.Connection, cfg: Config, config_sources: list[Source]) -> list[Source]:
    """Regular sources plus every candidate (any status), so discovered stories keep their outlet name.
    Only live candidates are marked discovered and capped."""
    out = list(config_sources)
    live = {s.id for s in discovery_sources(conn, cfg)}
    for r in conn.execute("SELECT * FROM candidates"):
        out.append(Source(id=r["source_id"], type="rss", category=r["category"], name=r["title"] or r["source_id"],
                          url=r["url"], daily_cap=cfg.discovery.daily_cap_per_source if r["source_id"] in live else None,
                          params={"discovered": r["source_id"] in live}))
    return out
