"""The feed sections a source belongs to: Local (stored as "news"), World, Tech and Lifestyle.

Local is Slovenian outlets. A feed filed under Local whose stories are not in Slovenian goes to World
instead (when it is added, checked or promoted), and the settings page flags any that slipped through.
Stories keep the section they were fetched with, so refile_items() moves them when a source changes
section. A Local outlet's foreign news (Claude's "World" category) is shown under World.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import timedelta
from urllib.parse import urlsplit

from newsnest.config import Source
from newsnest.db import iso, utcnow

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

# Short words that are common in Slovenian headlines and rare in English ("so", "da" left out)
_SL_WORDS = {"je", "za", "na", "se", "ki", "pri", "bo", "bodo", "tudi", "ali", "iz", "od", "po", "kot", "ter",
             "pred", "med", "kaj", "kako", "zakaj", "ne", "bi", "še", "več", "lahko", "prvič", "leta"}
_SL_CHARS = re.compile(r"[čšžČŠŽ]")
_WORD = re.compile(r"[a-zčšžćđ]+")


def _text_slovenian(text: str) -> bool:
    if _SL_CHARS.search(text):
        return True
    return len(set(_WORD.findall(text.lower())) & _SL_WORDS) >= 2


def looks_slovenian(texts: list[str], url: str | None = None) -> bool | None:
    """Whether a feed's headlines (and snippets) read as Slovenian; None with too little to go on.
    A .si domain is enough on its own."""
    if url and (urlsplit(url).hostname or "").endswith(".si"):
        return True
    texts = [t for t in texts if t and t.strip()]
    if len(texts) < 3:
        return None
    return sum(_text_slovenian(t) for t in texts) >= 0.4 * len(texts)


def recent_texts(conn: sqlite3.Connection, source_id: str, limit: int = 30) -> list[str]:
    rows = conn.execute("SELECT title, snippet FROM items WHERE source_id = ? ORDER BY id DESC LIMIT ?",
                        (source_id, limit))
    return [f"{r['title']} {r['snippet'] or ''}" for r in rows]


def local_section(category: str, slovenian: bool | None) -> str:
    """Local only for Slovenian feeds: a feed filed under Local that is clearly not goes to World."""
    return "world" if category == "news" and slovenian is False else category


def foreign_local(conn: sqlite3.Connection, sources: list[Source], days: int = 3) -> set[str]:
    """Local sources whose recent stories don't read as Slovenian (flagged on the settings page)."""
    local = {s.id: s.url for s in sources if s.category == "news" and s.type == "rss"}
    if not local:
        return set()
    texts: dict[str, list[str]] = {sid: [] for sid in local}
    rows = conn.execute("SELECT source_id, title, snippet FROM items WHERE fetched_at >= ? AND category = 'news'",
                        (iso(utcnow() - timedelta(days=days)),))
    for r in rows:
        if r["source_id"] in texts and len(texts[r["source_id"]]) < 30:
            texts[r["source_id"]].append(f"{r['title']} {r['snippet'] or ''}")
    return {sid for sid, t in texts.items() if looks_slovenian(t, local[sid]) is False}


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
