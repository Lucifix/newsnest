"""What one feed brings: how much it posts, how much of it is good enough for the feed, what you did with it,
and its latest stories with the reason each one is shown or not. Settings → Feeds → a section → a feed."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta

from newsnest.config import Config
from newsnest.db import iso, parse_iso, utcnow
from newsnest.feed import load_weights, weight_strength
from newsnest.levels import Levels, load_levels
from newsnest.scoring import final_score

DAYS = 30        # the period the numbers cover
LATEST = 10      # stories listed on the page

# Why a story is or isn't in the feed, in the words the page shows
SHOWN, LOW, MUTED, WAITING, UNSCORED = "shown", "low", "muted", "waiting", "unscored"


@dataclass
class Story:
    id: int
    title: str
    fetched_at: str
    final: float | None
    verdict: str          # one of SHOWN, LOW, MUTED, WAITING, UNSCORED
    opened: bool
    rating: int | None


@dataclass
class FeedNumbers:
    days: int                   # days the numbers cover: DAYS, or fewer for a feed that is younger
    stories: int = 0
    shown: int = 0              # good enough for the feed (or always shown by a filter)
    low: int = 0                # scored below your limit
    muted: int = 0              # hidden by a muted word
    waiting: int = 0            # not scored yet
    opened: int = 0
    liked: int = 0
    disliked: int = 0
    first_seen: str | None = None
    ranking: tuple[str, int] = ("more", 0)   # learned from your reading: direction and level 0-3 (newsnest.feed)
    latest: list[Story] = field(default_factory=list)

    @property
    def per_day(self) -> float:
        return self.stories / self.days if self.days else 0.0

    @property
    def shown_share(self) -> int:
        """Percent of the scored stories that were good enough for the feed."""
        scored = self.shown + self.low
        return round(100 * self.shown / scored) if scored else 0


def verdict(row, final: float | None, cfg: Config) -> str:
    """The same test the feed applies (newsnest.digest), without the per-source daily maximum."""
    if row["prefilter"] == "muted":
        return MUTED
    if row["prefilter"] == "must_show":
        return SHOWN
    if row["score_status"] == "pending":
        return WAITING
    if final is None:
        return UNSCORED
    return SHOWN if final >= cfg.digest.min_final else LOW


def feed_numbers(conn: sqlite3.Connection, cfg: Config, source_id: str) -> FeedNumbers:
    now = utcnow()
    since = iso(now - timedelta(days=DAYS))
    first = conn.execute("SELECT MIN(fetched_at) FROM items WHERE source_id = ?", (source_id,)).fetchone()[0]
    first_dt = parse_iso(first)
    days = DAYS if not first_dt or now - first_dt >= timedelta(days=DAYS) else max(1, (now - first_dt).days + 1)
    levels: Levels = load_levels(conn)
    out = FeedNumbers(days=days, first_seen=first)
    out.ranking = weight_strength(load_weights(conn).get(("source", source_id), 1.0))

    def final_of(r) -> float | None:
        return final_score(r, r["source_count"] or 1, cfg, levels.shift_of(r["interest"]))

    rows = conn.execute(
        """SELECT i.*, s.source_count FROM items i LEFT JOIN stories s ON s.id = i.story_id
           WHERE i.source_id = ? AND i.fetched_at >= ?""", (source_id, since)).fetchall()
    for r in rows:
        out.stories += 1
        v = verdict(r, final_of(r), cfg)
        if v == SHOWN:
            out.shown += 1
        elif v == LOW:
            out.low += 1
        elif v == MUTED:
            out.muted += 1
        elif v == WAITING:
            out.waiting += 1
        out.opened += bool(r["opened_at"])
        out.liked += r["rating"] == 1
        out.disliked += r["rating"] == -1

    for r in conn.execute(
            """SELECT i.*, s.source_count FROM items i LEFT JOIN stories s ON s.id = i.story_id
               WHERE i.source_id = ? ORDER BY i.fetched_at DESC, i.id DESC LIMIT ?""", (source_id, LATEST)):
        final = final_of(r)
        out.latest.append(Story(r["id"], r["title"], r["fetched_at"], final, verdict(r, final, cfg),
                                bool(r["opened_at"]), r["rating"]))
    return out
