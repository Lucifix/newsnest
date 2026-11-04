"""Feed sessions: the home feed's ranking is computed once and reused, so it holds still while you read.

Without this every page load, tab switch or Back ranked the feed again, and much of what the ranking
reads changes while you scan: an opened story is read (and left the feed), a card on screen for a second
sinks, an open nudges its outlet's weight, new articles arrive. Opening a story and coming back then
showed a reshuffled page.

A session stores the ranking for every tab (newsnest.feed.Ranking) as story positions. Each view loads
the stories' current rows again, so read, saved and rating show as they are now; hidden and "not
interested" stories drop out, read ones stay in place, dimmed. A new session (fresh ranking, with all
the signals of the last one) starts when the device has none, after feed.session_idle_minutes away,
on Refresh or Mark all read, when the config or the source list changes, or when a release changed
the stored format. One session per device: the id lives in a cookie.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from collections import Counter
from datetime import datetime, timedelta

from newsnest.config import Config, Source
from newsnest.db import iso, parse_iso, utcnow
from newsnest.digest import make_cards
from newsnest.feed import FeedItem, Ranking, rank_feed, story_hidden, story_read_at, window_rows

COOKIE = "newsnest_feed"
# Bumped whenever the stored payload changes shape: a session of another format is ranked again
FORMAT = 1
# Sessions nobody used for this long are deleted, and only the newest few are kept
KEEP = timedelta(hours=24)
KEEP_MAX = 20
# What start() hands out (secrets.token_urlsafe(12)); anything else is not looked up
_SID = re.compile(r"[A-Za-z0-9_-]{16}")


def config_fingerprint(cfg: Config, sources: list[Source]) -> str:
    """Changes when the config or the source list does, so a session never outlives an edit to either."""
    blob = json.dumps([cfg.model_dump(mode="json"),
                       [(s.id, s.category, s.name, s.daily_cap, bool(s.params.get("discovered"))) for s in sources]],
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


def _entry(it: FeedItem) -> dict:
    return {"lead": it.card.lead.id, "rank": it.rank, "block": it.block, "category": it.category, "top": it.top,
            "updated": [m.id for m in it.updated], "discovered": it.discovered, "extra": it.extra,
            "below": it.below, "why": it.why, "reopened": it.card.reopened}


def _dump(ranking: Ranking) -> str:
    return json.dumps({"items": [_entry(it) for it in ranking.items], "extras": [_entry(it) for it in ranking.extras],
                       "affinity": dict(ranking.affinity), "profile": ranking.profile,
                       "calibration": ranking.calibration, "read_count": ranking.read_count,
                       "dropped": ranking.dropped, "cutoff": ranking.cutoff}, ensure_ascii=False)


def _restore(conn: sqlite3.Connection, cfg: Config, sources: list[Source], payload: str,
             created: datetime) -> Ranking:
    """The stored ranking over the stories' current rows."""
    data = json.loads(payload)
    cards = make_cards(window_rows(conn, data["cutoff"]), cfg, {s.id: s.name for s in sources})
    card_of = {m.id: card for card in cards for m in [card.lead, *card.others]}
    placed: set[int] = set()

    def restore(entries: list[dict]) -> list[FeedItem]:
        out = []
        for e in entries:
            card = card_of.get(e["lead"])
            # An event label can fold two stories into one card after ranking: it keeps the first place
            if card is None or id(card) in placed or story_hidden(card):
                continue
            placed.add(id(card))
            members = [card.lead, *card.others]
            # The headline stays the one that was ranked, even if a better-scoring outlet joined since
            lead = next(m for m in members if m.id == e["lead"])
            if lead is not card.lead:
                card.lead, card.others = lead, [m for m in members if m is not lead]
            if e["reopened"]:  # Updated, unless it was read again during this session
                read_at = story_read_at(card)
                card.reopened = not (read_at and read_at > created)
            updated = set(e["updated"])
            out.append(FeedItem(card, e["rank"], e["block"], e["category"], e["top"],
                                [m for m in members if m.id in updated], discovered=e["discovered"],
                                extra=e["extra"], below=e["below"], why=[(sign, text) for sign, text in e["why"]]))
        return out

    items = restore(data["items"])
    extras = restore(data["extras"])
    return Ranking(items=items, extras=extras, affinity=Counter(data["affinity"]), profile=data["profile"],
                   calibration=data["calibration"], read_count=data["read_count"], dropped=data["dropped"],
                   cutoff=data["cutoff"])


def resume(conn: sqlite3.Connection, cfg: Config, sources: list[Source], sid: str | None,
           now: datetime | None = None) -> Ranking | None:
    """The session's ranking, or None when there is no usable session (unknown, idle too long, another
    config or format, damaged). Using it keeps it alive."""
    found = _resume(conn, cfg, sources, sid, now)
    return found[1] if found else None


def _resume(conn: sqlite3.Connection, cfg: Config, sources: list[Source], sid: str | None,
            now: datetime | None = None) -> tuple[str, Ranking] | None:
    """The stored session id and its ranking. The id comes from the database, not from what the browser
    sent, so it is safe to put in a cookie or a page."""
    if not sid or not _SID.fullmatch(sid):
        return None
    now = now or utcnow()
    row = conn.execute("SELECT * FROM feed_sessions WHERE id = ?", (sid,)).fetchone()
    if (row is None or row["format"] != FORMAT or row["config_fp"] != config_fingerprint(cfg, sources)
            or parse_iso(row["last_used_at"]) < now - timedelta(minutes=cfg.feed.session_idle_minutes)):
        return None
    try:
        ranking = _restore(conn, cfg, sources, row["payload"], parse_iso(row["created_at"]))
    except (KeyError, TypeError, ValueError, StopIteration):
        return None  # a damaged payload: rank again rather than fail the page
    conn.execute("UPDATE feed_sessions SET last_used_at = ? WHERE id = ?", (iso(now), row["id"]))
    return row["id"], ranking


def start(conn: sqlite3.Connection, cfg: Config, sources: list[Source], now: datetime | None = None) -> tuple[str, Ranking]:
    """A new session with a fresh ranking."""
    now = now or utcnow()
    ranking = rank_feed(conn, cfg, sources, now)
    sid = secrets.token_urlsafe(12)
    conn.execute("DELETE FROM feed_sessions WHERE last_used_at < ?", (iso(now - KEEP),))
    conn.execute("INSERT INTO feed_sessions (id, created_at, last_used_at, config_fp, format, payload) "
                 "VALUES (?, ?, ?, ?, ?, ?)",
                 (sid, iso(now), iso(now), config_fingerprint(cfg, sources), FORMAT, _dump(ranking)))
    conn.execute("DELETE FROM feed_sessions WHERE id NOT IN "
                 "(SELECT id FROM feed_sessions ORDER BY last_used_at DESC LIMIT ?)", (KEEP_MAX,))
    return sid, ranking


def open_session(conn: sqlite3.Connection, cfg: Config, sources: list[Source], sid: str | None,
                 fresh: bool = False, now: datetime | None = None) -> tuple[str, Ranking]:
    """The device's session, or a new one when it has none usable or fresh is asked for."""
    found = None if fresh else _resume(conn, cfg, sources, sid, now)
    return found or start(conn, cfg, sources, now)


def touch(conn: sqlite3.Connection, cfg: Config, sid: str | None, now: datetime | None = None) -> None:
    """The reader is still at it (opened a story, cards on screen): keep the session alive. An expired one
    stays expired, so a page left open overnight does not bring yesterday's ranking back."""
    if not sid:
        return
    now = now or utcnow()
    conn.execute("UPDATE feed_sessions SET last_used_at = ? WHERE id = ? AND last_used_at >= ?",
                 (iso(now), sid, iso(now - timedelta(minutes=cfg.feed.session_idle_minutes))))


def end(conn: sqlite3.Connection, sid: str | None) -> None:
    if sid:
        conn.execute("DELETE FROM feed_sessions WHERE id = ?", (sid,))
