"""SQLite storage. One connection per call site (thread-safe with WAL); schema is versioned.

The production database survives redeploys, so every schema change is a new entry in
MIGRATIONS and is applied in order on start.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from newsnest.bootstrap import data_dir

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

    CREATE TABLE stories (
        id INTEGER PRIMARY KEY,
        category TEXT NOT NULL,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        source_count INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE items (
        id INTEGER PRIMARY KEY,
        source_id TEXT NOT NULL,
        category TEXT NOT NULL,
        guid TEXT,
        url TEXT NOT NULL,
        url_norm TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        snippet TEXT,
        publisher TEXT,
        published TEXT,
        fetched_at TEXT NOT NULL,
        story_id INTEGER REFERENCES stories(id),
        tokens TEXT,
        extra TEXT,
        watches TEXT,
        prefilter TEXT,
        score_status TEXT NOT NULL DEFAULT 'pending',
        score_attempts INTEGER NOT NULL DEFAULT 0,
        topic INTEGER, importance INTEGER, personal INTEGER,
        reason TEXT, ai_category TEXT, scored_at TEXT,
        summary TEXT, summary_source TEXT, summary_status TEXT,
        read_at TEXT,
        rating INTEGER, rated_at TEXT
    );
    CREATE INDEX items_fetched ON items(fetched_at);
    CREATE INDEX items_story ON items(story_id);
    CREATE INDEX items_score_status ON items(score_status);
    CREATE INDEX items_rated ON items(rated_at) WHERE rating IS NOT NULL;

    CREATE TABLE source_state (
        source_id TEXT PRIMARY KEY,
        last_run TEXT, last_status TEXT, last_error TEXT, last_http INTEGER,
        last_success TEXT, last_nonempty TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        raw_count INTEGER, new_count INTEGER, warnings TEXT,
        blocked_until TEXT
    );

    CREATE TABLE fetch_runs (
        id INTEGER PRIMARY KEY,
        source_id TEXT NOT NULL, at TEXT NOT NULL, status TEXT NOT NULL,
        http_status INTEGER, raw_count INTEGER, item_count INTEGER, new_count INTEGER,
        duration_ms INTEGER, error TEXT
    );
    CREATE INDEX fetch_runs_at ON fetch_runs(at);

    CREATE TABLE ai_calls (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL, day TEXT NOT NULL, purpose TEXT NOT NULL, model TEXT NOT NULL,
        input_tokens INTEGER, output_tokens INTEGER, cache_write_tokens INTEGER, cache_read_tokens INTEGER,
        cost_usd REAL NOT NULL DEFAULT 0, ok INTEGER NOT NULL, stop_reason TEXT, error TEXT, request_id TEXT
    );
    CREATE INDEX ai_calls_day ON ai_calls(day);

    CREATE TABLE misses (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL, url TEXT NOT NULL, title TEXT, item_id INTEGER,
        found_in_db INTEGER NOT NULL DEFAULT 0, db_status TEXT,
        topic INTEGER, importance INTEGER, personal INTEGER, final REAL, reason TEXT, error TEXT
    );

    CREATE TABLE proposals (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
        current_text TEXT, proposed_text TEXT, rationale TEXT, decided_at TEXT
    );

    CREATE TABLE listings (
        id INTEGER PRIMARY KEY,
        search_id TEXT NOT NULL, site TEXT NOT NULL, listing_key TEXT NOT NULL,
        fingerprint TEXT NOT NULL, url TEXT, title TEXT, price INTEGER, attrs TEXT,
        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
        relisted_from INTEGER REFERENCES listings(id),
        fit_score INTEGER, fit_reason TEXT, fit_status TEXT,
        rating INTEGER, read_at TEXT,
        UNIQUE (site, listing_key)
    );
    CREATE INDEX listings_fp ON listings(site, fingerprint);

    CREATE TABLE listing_events (
        id INTEGER PRIMARY KEY,
        listing_id INTEGER NOT NULL REFERENCES listings(id),
        type TEXT NOT NULL, at TEXT NOT NULL, old_price INTEGER, new_price INTEGER
    );
    CREATE INDEX listing_events_at ON listing_events(at);
    """,
    # 2: per-item "seen" (was on screen), separate from "read" (opened or marked read)
    """
    ALTER TABLE items ADD COLUMN seen_at TEXT
    """,
    # 3: Discover-style feed: impressions, story-level read time, "not interested", learned weights
    """
    ALTER TABLE items ADD COLUMN seen_count INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE items ADD COLUMN last_seen_at TEXT;
    ALTER TABLE items ADD COLUMN last_read_at TEXT;
    ALTER TABLE items ADD COLUMN hidden_at TEXT;
    UPDATE items SET last_read_at = read_at WHERE read_at IS NOT NULL;
    CREATE TABLE feedback (
        id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, target TEXT, item_id INTEGER
    );
    CREATE TABLE weights (
        kind TEXT NOT NULL, key TEXT NOT NULL, weight REAL NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (kind, key)
    )
    """,
    # 4: "opened" (tapped) separate from "read" (out of the feed), so auto-read on scroll is not engagement
    """
    ALTER TABLE items ADD COLUMN opened_at TEXT;
    UPDATE items SET opened_at = last_read_at WHERE last_read_at IS NOT NULL
    """,
    # 5: discovered sources (candidate pool) and conditional GET for every feed
    """
    CREATE TABLE candidates (
        id INTEGER PRIMARY KEY,
        source_id TEXT NOT NULL UNIQUE, url TEXT NOT NULL UNIQUE, title TEXT, category TEXT NOT NULL,
        origin TEXT, status TEXT NOT NULL, added_at TEXT NOT NULL, checked_at TEXT, check_result TEXT, decided_at TEXT
    );
    ALTER TABLE source_state ADD COLUMN etag TEXT;
    ALTER TABLE source_state ADD COLUMN last_modified TEXT
    """,
    # 6: event labels from Claude (the real-world event an article is about)
    """
    ALTER TABLE items ADD COLUMN event TEXT;
    ALTER TABLE items ADD COLUMN event_tries INTEGER NOT NULL DEFAULT 0;
    CREATE INDEX items_event ON items(event) WHERE event IS NOT NULL
    """,
    # 7: the starter pack's international feeds were filed under Local ("news"); they belong to World
    """
    UPDATE items SET category = 'world' WHERE category = 'news' AND source_id IN
        (SELECT source_id FROM candidates WHERE origin = 'starter pack' AND category = 'news');
    UPDATE candidates SET category = 'world' WHERE origin = 'starter pack' AND category = 'news'
    """,
    # 8: key-point summaries. summary holds the one-line lede, summary_points the bullets (JSON list),
    # summary_why the personal angle, summary_depth 'enough' or 'full' with summary_depth_tag (e.g. analysis)
    """
    ALTER TABLE items ADD COLUMN summary_points TEXT;
    ALTER TABLE items ADD COLUMN summary_why TEXT;
    ALTER TABLE items ADD COLUMN summary_depth TEXT;
    ALTER TABLE items ADD COLUMN summary_depth_tag TEXT
    """,
    # 9: "save for later": a story kept until the reader removes it, independent of read state and the feed window
    """
    ALTER TABLE items ADD COLUMN saved_at TEXT;
    CREATE INDEX items_saved ON items(saved_at) WHERE saved_at IS NOT NULL
    """,
    # 10: keyword search. items_fts keeps its own copy of the searchable text (rowid = items.id), kept in step
    # by triggers. Whole words, accents folded (sport = šport); key points only once written (an error
    # leaves its message in summary, which must not match)
    """
    CREATE VIRTUAL TABLE items_fts USING fts5(title, snippet, summary, points, tokenize='unicode61 remove_diacritics 2');
    CREATE TRIGGER items_fts_insert AFTER INSERT ON items BEGIN
        INSERT INTO items_fts (rowid, title, snippet, summary, points) VALUES (new.id, new.title, new.snippet,
            CASE WHEN new.summary_status = 'done' THEN new.summary END,
            CASE WHEN new.summary_status = 'done' THEN new.summary_points END);
    END;
    CREATE TRIGGER items_fts_update AFTER UPDATE OF title, snippet, summary, summary_points, summary_status ON items BEGIN
        DELETE FROM items_fts WHERE rowid = old.id;
        INSERT INTO items_fts (rowid, title, snippet, summary, points) VALUES (new.id, new.title, new.snippet,
            CASE WHEN new.summary_status = 'done' THEN new.summary END,
            CASE WHEN new.summary_status = 'done' THEN new.summary_points END);
    END;
    CREATE TRIGGER items_fts_delete AFTER DELETE ON items BEGIN
        DELETE FROM items_fts WHERE rowid = old.id;
    END;
    INSERT INTO items_fts (rowid, title, snippet, summary, points)
        SELECT id, title, snippet, CASE WHEN summary_status = 'done' THEN summary END,
               CASE WHEN summary_status = 'done' THEN summary_points END FROM items
    """,
    # weekly proposals list their changes one by one (JSON: {"basis": {...}, "items": [...]}); older ones have none
    "ALTER TABLE proposals ADD COLUMN changes TEXT",
    # the listings feature was removed: drop its tables and the bookkeeping of its sources and scheduler
    """
    DROP TABLE IF EXISTS listing_events;
    DROP TABLE IF EXISTS listings;
    DELETE FROM meta WHERE key = 'listing_last_run' OR key LIKE 'listing_plan:%';
    DELETE FROM source_state WHERE source_id LIKE 'avto_net-%' OR source_id LIKE 'nepremicnine_net-%';
    """,
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def parse_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def db_path() -> Path:
    return data_dir() / "newsnest.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    # FastAPI sets a request's connection up and runs its route on different worker threads; the connection is
    # still used by one request at a time, so letting it move between threads is safe
    conn = sqlite3.connect(path or db_path(), timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    current = row["version"] if row else 0
    for version, sql in enumerate(MIGRATIONS[current:], start=current + 1):
        with transaction(conn):
            _run_script(conn, sql)
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    return len(MIGRATIONS)


def _run_script(conn: sqlite3.Connection, sql: str) -> None:
    # executescript() would COMMIT the open transaction; run statements one by one instead.
    # A trigger body has ";" inside BEGIN ... END, so pieces are joined until they form a complete statement.
    pending = ""
    for piece in sql.split(";"):
        pending += piece + ";"
        if sqlite3.complete_statement(pending):
            if pending.strip(" \n;"):
                conn.execute(pending)
            pending = ""
    if pending.strip(" \n;"):
        conn.execute(pending)


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def loads(value: str | None, default: Any = None) -> Any:
    return json.loads(value) if value else default
