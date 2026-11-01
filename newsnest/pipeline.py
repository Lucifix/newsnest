"""Store fetched items: dedupe by URL, group the same story across sources, record source health."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from newsnest.config import Config, Source
from newsnest.db import dumps, iso, loads, parse_iso, transaction
from newsnest.models import FetchResult, FetchStatus, Item
from newsnest.grouping import StoryIndex, new_story, touch_story
from newsnest.textmatch import normalize_url, title_tokens
from newsnest.watches import Prefilter

def ingest(conn: sqlite3.Connection, source: Source, items: list[Item], prefilter: Prefilter,
           now: datetime) -> list[int]:
    """Insert new items; returns their ids. Items whose normalized URL is known are skipped."""
    new_ids = []
    with transaction(conn):
        index = StoryIndex.load(conn, now)
        for item in items:
            url_norm = normalize_url(item.url)
            known = conn.execute("SELECT id, extra FROM items WHERE url_norm = ?", (url_norm,)).fetchone()
            if known:
                # Items stored before image capture existed get their image on the next poll
                extra = loads(known["extra"], {}) or {}
                new_media = {k: item.extra[k] for k in ("image", "logo") if item.extra.get(k) and k not in extra}
                if new_media:
                    conn.execute("UPDATE items SET extra = ? WHERE id = ?", (dumps({**extra, **new_media}), known["id"]))
                continue
            pf, watched = prefilter.classify(item)
            tokens = title_tokens(item.title)
            item_id = conn.execute(
                """INSERT INTO items (source_id, category, guid, url, url_norm, title, snippet, publisher,
                   published, fetched_at, tokens, extra, watches, prefilter, score_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (source.id, source.category, item.guid, item.url, url_norm, item.title, item.snippet,
                 item.publisher, iso(item.published), iso(now), dumps(sorted(tokens)),
                 dumps(item.extra) if item.extra else None, dumps(watched) if watched else None, pf,
                 "muted" if pf == "muted" else "pending")).lastrowid
            ts = (item.published or now).timestamp()
            story = index.match(frozenset(tokens), source.id, ts)
            if story is None:
                story = new_story(conn, source.category, now)
            conn.execute("UPDATE items SET story_id = ? WHERE id = ?", (story, item_id))
            touch_story(conn, story, now)  # after the item joined, so the outlet count includes it
            index.add(story, source.id, ts, frozenset(tokens))
            new_ids.append(item_id)
    return new_ids


def record_run(conn: sqlite3.Connection, source: Source, result: FetchResult, new_count: int,
               cfg: Config, now: datetime) -> None:
    failed = result.status.is_failure and result.status != FetchStatus.NOT_IMPLEMENTED
    blocked_until = None
    if result.status == FetchStatus.BLOCKED:
        blocked_until = iso(now + timedelta(hours=1))
    with transaction(conn):
        conn.execute(
            """INSERT INTO fetch_runs (source_id, at, status, http_status, raw_count, item_count, new_count,
               duration_ms, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (source.id, iso(now), result.status.value, result.http_status, result.raw_count,
             len(result.items), new_count, result.duration_ms, result.error))
        conn.execute(
            """INSERT INTO source_state (source_id, last_run, last_status, last_error, last_http, last_success,
                   last_nonempty, consecutive_failures, raw_count, new_count, warnings, blocked_until, etag, last_modified)
               VALUES (:id, :now, :status, :error, :http, :success, :nonempty, :fails, :raw, :new, :warn, :blocked,
                       :etag, :lastmod)
               ON CONFLICT(source_id) DO UPDATE SET
                   last_run = :now, last_status = :status, last_error = :error, last_http = :http,
                   last_success = COALESCE(:success, last_success),
                   last_nonempty = COALESCE(:nonempty, last_nonempty),
                   consecutive_failures = CASE WHEN :failed THEN consecutive_failures + 1 ELSE 0 END,
                   raw_count = :raw, new_count = :new, warnings = :warn,
                   blocked_until = :blocked,
                   etag = CASE WHEN :http = 304 THEN etag ELSE :etag END,
                   last_modified = CASE WHEN :http = 304 THEN last_modified ELSE :lastmod END""",
            {"id": source.id, "now": iso(now), "status": result.status.value, "error": result.error,
             "http": result.http_status, "success": None if failed else iso(now),
             "nonempty": iso(now) if result.raw_count > 0 else None, "fails": 1 if failed else 0,
             "failed": failed, "raw": result.raw_count, "new": new_count,
             "warn": dumps(result.warnings) if result.warnings else None, "blocked": blocked_until,
             "etag": result.etag, "lastmod": result.last_modified})


@dataclass
class Health:
    level: str      # ok | warning | failing | pending | disabled
    message: str


def source_health(source: Source, state: sqlite3.Row | None, cfg: Config, now: datetime) -> Health:
    """Heartbeat rules. "Nothing new" must mean nothing new, so silence is a failure too."""
    if not source.enabled:
        return Health("disabled", "disabled in config")
    if state is None:
        return Health("pending", "not fetched yet")
    blocked = parse_iso(state["blocked_until"])
    if blocked and blocked > now:
        return Health("failing", f"blocked ({state['last_error']}); paused until {blocked:%H:%M} UTC")
    fails = state["consecutive_failures"]
    if fails >= 2:
        return Health("failing", f"{fails} failed runs in a row: {state['last_error']}")
    last_run = parse_iso(state["last_run"])
    interval = cfg.poll_minutes(source.category)
    if now - last_run > timedelta(minutes=interval * 3):
        return Health("failing", f"not fetched since {last_run:%Y-%m-%d %H:%M} UTC (scheduler stalled?)")
    nonempty = parse_iso(state["last_nonempty"])
    first_run = last_run
    empty_limit = timedelta(hours=cfg.heartbeat.news_empty_hours)
    if (nonempty or first_run) and now - (nonempty or first_run) > empty_limit:
        since = f"since {nonempty:%Y-%m-%d %H:%M} UTC" if nonempty else "ever"
        return Health("failing", f"feed returned 0 entries {since}")
    if fails:
        return Health("warning", f"last run failed: {state['last_error']}")
    if state["warnings"]:
        return Health("ok", "; ".join(loads(state["warnings"], [])))
    return Health("ok", "")
