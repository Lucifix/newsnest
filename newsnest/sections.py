"""The feed sections a source belongs to: Local (stored as "news"), World, Tech and Lifestyle.

Local is whatever the reader files there: news from where they live. A Local feed whose stories don't
read like the other Local feeds (most likely another language) is flagged on the settings page, never
moved. The check knows no language: it learns the common words of the other Local feeds. Stories keep the section they were fetched with, so refile_items() moves them when a source changes
section. A Local outlet's foreign news (Claude's "World" category) is shown under World.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from datetime import timedelta
from urllib.parse import urlsplit

from newsnest.config import Source
from newsnest.db import iso, utcnow
from newsnest.textmatch import fold

SECTIONS = ("news", "world", "tech", "lifestyle")
LABELS = {"news": "Local", "world": "World", "tech": "Tech", "lifestyle": "Lifestyle"}

# The dock has four tabs plus search, like Apple News. "headlines" is the News tab: Local and World
# together, with Local | World as chips inside it.
HEADLINES = "headlines"
TABS = {"all": "For you", "tech": "Tech", HEADLINES: "News", "lifestyle": "Lifestyle"}
_TAB_BLOCKS = {HEADLINES: ("news", "world")}


def scope_blocks(scope: str) -> tuple[str, ...]:
    """The sections a filter covers: one, or Local and World for the News tab."""
    return _TAB_BLOCKS.get(scope, (scope,))


def tab_of(scope: str) -> str:
    """The dock tab a filter lights up: Local and World both belong to News."""
    return HEADLINES if scope in ("news", "world", HEADLINES) else scope

# The local vocabulary: words in at least this share of the other Local feeds' stories, most common first.
# In any language with spaces between words these are its function words ("je", "und", "the"); in one
# without (Japanese, Chinese) too few words qualify and the check stays silent.
VOCAB_SHARE = 0.03
VOCAB_SIZE = 60
VOCAB_MIN = 15
_WORD = re.compile(r"[^\W\d_]{2,}")


def text_words(text: str) -> set[str]:
    return set(_WORD.findall(fold(text)))


def vocabulary(texts: list[str]) -> set[str]:
    """The words that make texts read like this set of texts (see VOCAB_SHARE)."""
    df: Counter = Counter()
    for t in texts:
        df.update(text_words(t))
    floor = max(3, VOCAB_SHARE * len(texts))
    return {w for w, n in df.most_common(VOCAB_SIZE) if n >= floor}


def looks_local(texts: list[str], url: str | None, vocab: set[str], tld: str = "") -> bool | None:
    """Whether a feed's stories (headline + snippet) read like the reader's Local feeds; None with too
    little to go on. A url on the region's domain (tld, e.g. ".si") is enough on its own."""
    if tld and url and (urlsplit(url).hostname or "").endswith(tld):
        return True
    texts = [t for t in texts if t and t.strip()]
    if len(texts) < 3 or len(vocab) < VOCAB_MIN:
        return None
    return sum(len(text_words(t) & vocab) >= 2 for t in texts) >= 0.4 * len(texts)


def foreign_local(conn: sqlite3.Connection, sources: list[Source], tld: str = "", days: int = 3) -> set[str]:
    """Local sources whose recent stories don't read like the other Local feeds (flagged on the settings
    page). Each feed is measured against the others, so a wrong one can't vouch for itself."""
    local = {s.id: s.url for s in sources if s.category == "news" and s.type == "rss"}
    if len(local) < 2:
        return set()
    texts: dict[str, list[str]] = {sid: [] for sid in local}
    rows = conn.execute("SELECT source_id, title, snippet FROM items WHERE fetched_at >= ? AND category = 'news' "
                        "ORDER BY id DESC", (iso(utcnow() - timedelta(days=days)),))
    for r in rows:
        if r["source_id"] in texts and len(texts[r["source_id"]]) < 30:
            texts[r["source_id"]].append(f"{r['title']} {r['snippet'] or ''}")
    flagged = set()
    for sid, own in texts.items():
        vocab = vocabulary([t for other, ts in texts.items() if other != sid for t in ts])
        if looks_local(own, local[sid], vocab, tld) is False:
            flagged.add(sid)
    return flagged


def refile_items(conn: sqlite3.Connection, sources: list[Source]) -> int:
    """Move stored stories to their source's current section. Returns how many moved."""
    want = {s.id: s.category for s in sources if s.category in SECTIONS}
    moved = 0
    rows = conn.execute(f"SELECT DISTINCT source_id, category FROM items WHERE category IN ({', '.join('?' * len(SECTIONS))})",
                        SECTIONS).fetchall()
    for r in rows:
        target = want.get(r["source_id"])
        if target and target != r["category"]:
            moved += conn.execute("UPDATE items SET category = ? WHERE source_id = ? AND category = ?",
                                  (target, r["source_id"], r["category"])).rowcount
    return moved


def block_of(category: str, ai_category: str | None) -> str:
    """The feed section a story is shown in: its source's, except a Local outlet's foreign news is World."""
    return "world" if category == "news" and ai_category == "World" else category
