"""The feed sections a source belongs to. The reader's list lives in config.yaml (`sections:`, see config.py);
Local (stored as "news") and World are built in, the rest are theirs: Tech and Lifestyle are the usual two.

Local is whatever the reader files there: news from where they live. A Local feed whose stories don't
read like the other Local feeds (most likely another language) is flagged on the settings page, never
moved. The check knows no language: it learns the common words of the other Local feeds. Stories keep the section they were fetched with, so refile_items() moves them when a source changes
section. A Local outlet's foreign news (Claude's "World" category) is shown under World.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from newsnest.config import LEGACY_SECTIONS, NEWS, WORLD, Config, SectionCfg, Source
from newsnest.db import iso, transaction, utcnow
from newsnest.textmatch import fold

# The dock is For you, the sections that are tabs, and search, like Apple News. "headlines" is the News tab:
# Local and World together, with Local | World as chips inside it.
HEADLINES = "headlines"
_TAB_BLOCKS = {HEADLINES: (NEWS, WORLD)}


def section_list(cfg: Config | None) -> list[SectionCfg]:
    """The sections in the reader's order (the legacy four while there is no config)."""
    return cfg.all_sections if cfg else list(LEGACY_SECTIONS)


def section_keys(cfg: Config | None) -> tuple[str, ...]:
    return tuple(s.key for s in section_list(cfg))


def section_labels(cfg: Config | None) -> dict[str, str]:
    return {s.key: s.label for s in section_list(cfg)}


@dataclass(frozen=True)
class Tab:
    key: str     # the feed filter it opens: a section key, or HEADLINES for News
    label: str
    icon: str


def dock_tabs(cfg: Config | None, current: str | None = None) -> list[Tab]:
    """The dock's tabs after For you, in the section order. News stands in for Local and World at the place
    of the first of them. A section is a tab when it has `tab: true` and a feed to read (or is the one open),
    so nobody sees an empty tab. Without a valid config every tab is offered."""
    used = None if cfg is None else {f.category for f in cfg.feeds if f.enabled}
    if cfg is not None and cfg.github_releases.repos:
        used.add(cfg.github_releases.category)
    tabs: list[Tab] = []
    for s in section_list(cfg):
        if s.key in (NEWS, WORLD):
            if not any(t.key == HEADLINES for t in tabs):
                tabs.append(Tab(HEADLINES, "News", "newspaper"))
        elif s.tab and (used is None or s.key in used or s.key == current):
            tabs.append(Tab(s.key, s.label, s.icon))
    return tabs


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


def refile_items(conn: sqlite3.Connection, sources: list[Source], keys: tuple[str, ...] | None = None) -> int:
    """Move stored stories to their source's current section (one of `keys`, when given). Returns how many moved."""
    want = {s.id: s.category for s in sources if keys is None or s.category in keys}
    moved = 0
    rows = conn.execute("SELECT DISTINCT source_id, category FROM items").fetchall()
    for r in rows:
        target = want.get(r["source_id"])
        if target and target != r["category"]:
            moved += conn.execute("UPDATE items SET category = ? WHERE source_id = ? AND category = ?",
                                  (target, r["source_id"], r["category"])).rowcount
    return moved


def move_section(conn: sqlite3.Connection, old: str, new: str) -> int:
    """Move everything stored under section `old` to `new`: stories, their groups and sources on trial.
    Used when a section is deleted; also catches the stories of feeds that are gone. Returns the stories moved."""
    with transaction(conn):
        moved = conn.execute("UPDATE items SET category = ? WHERE category = ?", (new, old)).rowcount
        conn.execute("UPDATE stories SET category = ? WHERE category = ?", (new, old))
        conn.execute("UPDATE candidates SET category = ? WHERE category = ?", (new, old))
    return moved


def block_of(category: str, ai_category: str | None) -> str:
    """The feed section a story is shown in: its source's, except a Local outlet's foreign news is World."""
    return "world" if category == "news" and ai_category == "World" else category
