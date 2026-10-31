"""Group articles about the same story, by headline word overlap weighted by rarity.

A shared rare word ("php2sh", "cambri") says far more than a shared common one ("govern"), so
overlap is measured in IDF weight, not in word count. Words in more than COMMON_SHARE of the open
headlines are filler and ignored, so any language works without word lists. The thresholds below
were tuned on Slovenian headlines. Items from News and Tech can join the same
story. The same outlet can join a story it is already in when the new article is a follow-up
(hours later) or practically the same headline again.

Claude's event labels (newsnest.scoring) are a second, language-independent signal; cards merge both
(newsnest.digest.make_cards).
"""

from __future__ import annotations

import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from newsnest.db import iso, loads, parse_iso
from newsnest.textmatch import SEED_STOPWORDS

# Bump when the matcher changes: the runner regroups the last 72 h once after the deploy
GROUPING_VERSION = "3"

# Stories stay open for new members this long after their latest item
STORY_WINDOW = timedelta(hours=36)

# Tuned on 2,100 real headlines: wrong merges all shared two ordinary words (weight <= 12, e.g. "ustavno
# sodišče", "umetne inteligence"); right ones shared 3+ words (weight >= 15) or two very rare ones.
MIN_SHARED = 2          # at least two shared content words
MIN_OVERLAP = 0.4       # shared weight / weight of the shorter headline
MIN_SHARED_WEIGHT = 14.0  # about three ordinary words, or two names / rare terms
COMMON_SHARE = 0.03     # a stem in this share of the open headlines is filler, not signal
CORPUS_FLOOR = 1500     # weights assume at least this many headlines, so tiny databases behave like real ones
SAME_OUTLET_GAP_H = 2.0  # a follow-up from the same outlet joins when this many hours later...
SAME_OUTLET_OVERLAP = 0.6  # ...and clearly about the same thing
DUPLICATE_OVERLAP = 0.9    # near-identical headline: the same article published again


@dataclass
class Entry:
    story_id: int
    source_id: str
    ts: float
    tokens: frozenset


class StoryIndex:
    """Inverted index over the open stories' headlines, so a new article is compared only with
    articles it shares a word with."""

    def __init__(self) -> None:
        self.entries: list[Entry] = []
        self.postings: dict[str, list[int]] = defaultdict(list)
        self.df: Counter = Counter()

    @classmethod
    def load(cls, conn: sqlite3.Connection, now: datetime) -> "StoryIndex":
        index = cls()
        rows = conn.execute(
            """SELECT i.story_id, i.source_id, i.tokens, COALESCE(i.published, i.fetched_at) AS ts
               FROM items i JOIN stories s ON s.id = i.story_id WHERE s.last_seen >= ?""",
            (iso(now - STORY_WINDOW),))
        for r in rows:
            index.add(r["story_id"], r["source_id"], parse_iso(r["ts"]).timestamp(), frozenset(loads(r["tokens"], [])))
        return index

    def add(self, story_id: int, source_id: str, ts: float, tokens: frozenset) -> None:
        idx = len(self.entries)
        self.entries.append(Entry(story_id, source_id, ts, tokens))
        for t in tokens:
            self.postings[t].append(idx)
            self.df[t] += 1

    def weight(self, token: str) -> float:
        # Rarer = heavier; unseen tokens get the maximum
        return math.log((max(len(self.entries), CORPUS_FLOOR) + 2) / (self.df.get(token, 0) + 1))

    def is_common(self, token: str) -> bool:
        """Filler: an English seed word, or a stem in at least COMMON_SHARE of the open headlines."""
        return token in SEED_STOPWORDS or self.df[token] >= COMMON_SHARE * max(len(self.entries), CORPUS_FLOOR)

    def match(self, tokens: frozenset, source_id: str, ts: float) -> int | None:
        """The story this headline belongs to, or None."""
        tokens = frozenset(t for t in tokens if not self.is_common(t))
        if len(tokens) < MIN_SHARED:
            return None
        counts: Counter = Counter()
        for t in tokens:
            posting = self.postings.get(t)
            if posting:
                counts.update(posting)
        weights = {t: self.weight(t) for t in tokens}
        total = sum(weights.values())
        best: tuple[float, int] | None = None
        for idx, shared_n in counts.items():
            if shared_n < MIN_SHARED:
                continue
            e = self.entries[idx]
            shared = tokens & e.tokens
            w_shared = sum(weights[t] for t in shared)
            w_other = sum(self.weight(t) for t in e.tokens if not self.is_common(t))
            overlap = w_shared / max(0.001, min(total, w_other))
            if overlap < MIN_OVERLAP or w_shared < MIN_SHARED_WEIGHT:
                continue
            if e.source_id == source_id:
                gap_h = abs(ts - e.ts) / 3600
                if not (overlap >= DUPLICATE_OVERLAP or (gap_h >= SAME_OUTLET_GAP_H and overlap >= SAME_OUTLET_OVERLAP)):
                    continue  # two different angles from one outlet within hours stay separate
            if best is None or w_shared > best[0]:
                best = (w_shared, e.story_id)
        return best[1] if best else None


def new_story(conn: sqlite3.Connection, category: str, now: datetime) -> int:
    return conn.execute("INSERT INTO stories (category, first_seen, last_seen, source_count) VALUES (?, ?, ?, 1)",
                        (category, iso(now), iso(now))).lastrowid


def touch_story(conn: sqlite3.Connection, story_id: int, now: datetime) -> None:
    conn.execute(
        """UPDATE stories SET last_seen = ?,
           source_count = (SELECT COUNT(DISTINCT source_id) FROM items WHERE story_id = ?) WHERE id = ?""",
        (iso(now), story_id, story_id))


def regroup(conn: sqlite3.Connection, hours: float, now: datetime) -> dict:
    """Re-assign recent items to stories with the current matcher (items keep their read/rating state).
    Run once after the matcher changes."""
    since = iso(now - timedelta(hours=hours))
    rows = conn.execute(
        """SELECT id, source_id, category, tokens, COALESCE(published, fetched_at) AS ts, fetched_at FROM items
           WHERE fetched_at >= ? ORDER BY COALESCE(published, fetched_at), id""", (since,)).fetchall()
    before = len({r[0] for r in conn.execute("SELECT story_id FROM items WHERE fetched_at >= ?", (since,))})
    index = StoryIndex()
    for r in rows:
        tokens = frozenset(loads(r["tokens"], []))
        ts = parse_iso(r["ts"]).timestamp()
        story = index.match(tokens, r["source_id"], ts)
        if story is None:
            story = new_story(conn, r["category"], parse_iso(r["fetched_at"]))
        conn.execute("UPDATE items SET story_id = ? WHERE id = ?", (story, r["id"]))
        index.add(story, r["source_id"], ts, tokens)
    conn.execute("DELETE FROM stories WHERE id NOT IN (SELECT DISTINCT story_id FROM items WHERE story_id IS NOT NULL)")
    conn.execute(
        """UPDATE stories SET source_count = (SELECT COUNT(DISTINCT source_id) FROM items WHERE story_id = stories.id),
           first_seen = (SELECT MIN(fetched_at) FROM items WHERE story_id = stories.id),
           last_seen = (SELECT MAX(fetched_at) FROM items WHERE story_id = stories.id)
           WHERE id IN (SELECT DISTINCT story_id FROM items WHERE fetched_at >= ?)""", (since,))
    after = len({r[0] for r in conn.execute("SELECT story_id FROM items WHERE fetched_at >= ?", (since,))})
    return {"items": len(rows), "stories_before": before, "stories_after": after}
