"""Event labels: Claude names the real-world event an article is about.

Headline matching (newsnest.grouping) cannot connect "The internet is having a very bad cybersecurity
day" with "Ranljivost v programskem jeziku PHP prizadela 70 odstotkov interneta". A short label such
as `php2shell-vulnerability` can, across languages, wordings and angles. Claude gets the labels that
are currently active, so it reuses one instead of inventing a variant. Cards merge items that share
a label or a headline-matched story (newsnest.digest.make_cards).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta

from newsnest.ai import AIClient, AIError, BudgetExceeded, Refused, Truncated, parse_json
from newsnest.config import Config, slugify
from newsnest.db import iso, utcnow

log = logging.getLogger(__name__)

ACTIVE_WINDOW = timedelta(hours=48)
ACTIVE_LIMIT = 250        # labels shown to Claude (about 10 tokens each; fewer labels means missed merges)
LABEL_MAX = 48
BACKLOG_BATCH = 30
MAX_TRIES = 3

# Part of the (cached) scoring instructions; also used by the backlog labeller
EVENT_RULES = """\
- event: a short label for the real-world event or story the item is about, lowercase ASCII words \
joined by hyphens, at most 5 words, e.g. "php2shell-vulnerability", "tesla-q3-earnings", \
"moharic-mandat-resnica". It must identify THAT event, not a topic: "php-flaw-patches" and \
"slovenia-raises-cyber-threat-level" are the same event if the threat level was raised because of the \
PHP flaw, but "cybersecurity" or "politics" are topics and wrong. If the item is about an event \
listed in <active_events>, or about the same event as another item of this batch, use that exact \
label, whatever language or angle the article has. Only invent a new label when the item is about \
something new. Items with nothing in common with any other article still get their own label."""

LABEL_ONLY_SYSTEM = """\
You label news items with the real-world event they are about, so that articles from different \
outlets, in different languages and from different angles about the same event get the same label.

For every item return its id and an event label: lowercase ASCII words joined by hyphens, at most \
5 words, e.g. "php2shell-vulnerability", "tesla-q3-earnings". The label identifies THAT event, \
not a topic ("cybersecurity" is wrong). If the item is about an event listed in <active_events>, or \
about the same event as another item of this batch, use that exact label, whatever the language or \
angle. Only invent a new label when the item is about something new. Return one entry per item."""

LABEL_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "event": {"type": "string"}},
        "required": ["id", "event"], "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}


def normalize_label(label: str | None) -> str | None:
    slug = slugify(label or "", LABEL_MAX)
    return None if slug in ("", "x", "none", "null", "n-a") else slug


def active_events(conn: sqlite3.Connection, limit: int = ACTIVE_LIMIT) -> list[sqlite3.Row]:
    since = iso(utcnow() - ACTIVE_WINDOW)
    return conn.execute(
        """SELECT event, COUNT(*) AS n, MAX(COALESCE(published, fetched_at)) AS last
           FROM items WHERE event IS NOT NULL AND fetched_at >= ?
           GROUP BY event ORDER BY n DESC, last DESC LIMIT ?""", (since, limit)).fetchall()


def active_events_text(conn: sqlite3.Connection) -> str:
    """One label per line, biggest events first. Labels alone: they already name the event, and a sample
    headline per label made this block ~80% of every scoring prompt."""
    rows = active_events(conn)
    if not rows:
        return "(none yet)"
    return "\n".join(r["event"] for r in rows)


def run_labeling(conn: sqlite3.Connection, cfg: Config, ai: AIClient, max_batches: int = 5) -> dict:
    """Label scored items that have no event yet (articles scored before events existed, or a label
    Claude left empty). Oldest first, so earlier articles define the labels later ones reuse."""
    since = iso(utcnow() - ACTIVE_WINDOW)
    labelled = 0
    for _ in range(max_batches):
        rows = conn.execute(
            """SELECT id, title, snippet, publisher, source_id FROM items
               WHERE score_status = 'scored' AND event IS NULL AND event_tries < ? AND fetched_at >= ?
               ORDER BY COALESCE(published, fetched_at), id LIMIT ?""", (MAX_TRIES, since, BACKLOG_BATCH)).fetchall()
        if not rows:
            break
        lines = "\n".join(f"id={r['id']} | {r['publisher'] or r['source_id']} | {r['title']}"
                          + (f"\n  snippet: {(r['snippet'] or '')[:200]}" if r["snippet"] else "") for r in rows)
        try:
            result = ai.call(
                purpose="event_label", model=cfg.scoring.model,
                system=[{"type": "text", "text": LABEL_ONLY_SYSTEM}],
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": f"<active_events>\n{active_events_text(conn)}\n</active_events>"},
                    {"type": "text", "text": f"<items>\n{lines}\n</items>"}]}],
                max_tokens=8000, effort=cfg.scoring.effort, schema=LABEL_SCHEMA)
            answers = {e.get("id"): normalize_label(e.get("event")) for e in parse_json(result.text).get("items", [])}
        except (Refused, Truncated):
            answers = {}
        except BudgetExceeded:
            raise
        except AIError:
            for r in rows:
                conn.execute("UPDATE items SET event_tries = event_tries + 1 WHERE id = ?", (r["id"],))
            raise
        for r in rows:
            label = answers.get(r["id"])
            conn.execute("UPDATE items SET event = ?, event_tries = event_tries + 1 WHERE id = ?", (label, r["id"]))
            labelled += 1 if label else 0
    return {"labelled": labelled}
