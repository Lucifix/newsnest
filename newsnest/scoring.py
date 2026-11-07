"""First-pass news scoring with the AI: title + snippet in batches, JSON per item.

Prompt layout, stable parts first so they are cached: instructions + scale + categories, then the
interest profile and seed examples (cache breakpoint), then recent ratings (second breakpoint, changes
only when I rate), then the items.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from newsnest.ai import AIClient, AIError, BudgetExceeded, Refused, Truncated, parse_json
from newsnest.config import Config
from newsnest.events import EVENT_RULES, active_events_text, normalize_label
from newsnest.db import iso, parse_iso, utcnow
from newsnest.interests import bullets, norm
from newsnest.profile import profile_exists, read_profile, seed_examples, strip_comments

log = logging.getLogger(__name__)

# Items still pending after this long are not worth a scoring call any more
MAX_SCORE_AGE = timedelta(days=3)
MAX_ATTEMPTS = 3

INSTRUCTIONS = """\
You rate news items for {reader}. You get the reader's interest \
profile, example headlines they marked as wanted (+) or not wanted (-), their recent ratings, and a \
batch of items with title, source and snippet. Items can be in any language; judge the meaning, not \
the language.

For every item return:
- topic (0-10): how well the subject matches the reader's stated interests.
- importance (0-10): how significant the news is {importance_scope}, \
regardless of the reader's interests. A national emergency, a major policy change, a large disaster \
or a major world event scores high even when its topic is outside the profile.
- personal (0-10): how likely this reader is to actually want to read this specific item, given \
the profile, the examples and the ratings. Clickbait, advertorials, horoscopes, quizzes and \
promotional pages score low.
- reason: one short sentence in English, at most 14 words, telling the reader why the item scored \
as it did: what it is about and how that relates to their interests, e.g. "New parking rules in your \
city; you follow local news." or "Product promo from a fashion shop; not one of your topics."
- category: exactly one of the allowed categories.{place_note}
- interest: the number of the line in want_lines this item matches best, or 0 if none matches well.
{event_rules}

Use this anchored scale for all three scores:
9-10 = the reader would be annoyed to miss it
7-8 = the reader would likely read it
5-6 = the headline is enough
0-4 = skip

Return exactly one entry per input item and copy each item's id unchanged.

Allowed categories: {categories}
"""


def reader_phrase(cfg: Config) -> str:
    """Who the prompts rate for: "one reader who lives in Berlin, Germany", or just "one reader"."""
    place = cfg.region.place
    return f"one reader who lives in {place}" if place else "one reader"


def instructions(cfg: Config) -> str:
    region = cfg.region
    place_note = ""
    if region.place and {"Local", "National"} & set(cfg.scoring.categories):
        area = f"{region.city} and its area" if region.city else "the reader's city or area"
        place_note = f' "Local" means {area}, "National" means {region.country}.'
    return INSTRUCTIONS.format(
        reader=reader_phrase(cfg),
        importance_scope=f"for someone living in {region.country}" if region.country else "in general",
        place_note=place_note, categories=", ".join(cfg.scoring.categories), event_rules=EVENT_RULES)


def want_lines() -> list[str]:
    """The Want bullets of interests.md, each once (compared with interests.norm), in file order. The
    scoring call labels each item with one of them by number, so recent reading can be followed per
    interest (newsnest.drift), finer than the category."""
    seen: set[str] = set()
    out: list[str] = []
    for line in bullets(strip_comments(read_profile("interests.md")))["want"]:
        if norm(line) not in seen:
            seen.add(norm(line))
            out.append(line)
    return out


def carry_interest_labels(conn: sqlite3.Connection, changes: list[dict], text: str) -> int:
    """After an accepted proposal rewords Want lines, move the stories labelled with the old wording to the
    new one, so that interest keeps its history. Only rewords that made it into the saved text count (the
    reader can edit a proposal before accepting it). Returns how many items moved."""
    want = {norm(line) for line in bullets(strip_comments(text))["want"]}
    moved = 0
    for change in changes:
        if change.get("op") != "reword" or change.get("section") != "want":
            continue
        old, new = norm(change.get("old") or ""), norm(change.get("new") or "")
        if old and new and old != new and new in want and old not in want:
            moved += conn.execute("UPDATE items SET interest = ? WHERE interest = ?", (new, old)).rowcount
    return moved


def want_lines_text(lines: list[str]) -> str:
    return "<want_lines>\n" + "\n".join(f"{i}. {line}" for i, line in enumerate(lines, 1)) + "\n</want_lines>"


def score_schema(categories: list[str], n_lines: int = 0) -> dict:
    # interest is listed as required because OpenAI-compatible strict mode rejects a schema with optional
    # properties; it is still not in _REQUIRED, so an answer without it is accepted and never retried
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "topic": {"type": "integer"},
                        "importance": {"type": "integer"},
                        "personal": {"type": "integer"},
                        "reason": {"type": "string"},
                        "category": {"type": "string", "enum": categories},
                        "event": {"type": "string"},
                        "interest": {"type": "integer", "enum": list(range(n_lines + 1))},
                    },
                    "required": ["id", "topic", "importance", "personal", "reason", "category", "event", "interest"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def system_blocks(cfg: Config, lines: list[str] | None = None) -> list[dict]:
    seeds = seed_examples()
    seed_text = "\n".join(f"{'+' if want else '-'} {title}" for want, title in seeds) or "(none yet)"
    lines = want_lines() if lines is None else lines
    profile = (f"<interest_profile>\n{strip_comments(read_profile('interests.md'))}\n</interest_profile>\n\n"
               f"<seed_examples>\n{seed_text}\n</seed_examples>")
    if lines:
        profile += "\n\n" + want_lines_text(lines)
    return [
        {"type": "text", "text": instructions(cfg)},
        {"type": "text", "text": profile, "cache_control": {"type": "ephemeral"}},
    ]


def ratings_text(conn: sqlite3.Connection, limit: int = 20) -> str:
    """Latest ratings, balanced between thumbs up and down, plus pasted misses (strong positives)."""
    half = limit // 2
    up = conn.execute("SELECT title, source_id FROM items WHERE rating = 1 ORDER BY rated_at DESC LIMIT ?",
                      (half,)).fetchall()
    down = conn.execute("SELECT title, source_id FROM items WHERE rating = -1 ORDER BY rated_at DESC LIMIT ?",
                        (half,)).fetchall()
    missed = conn.execute("SELECT title FROM misses WHERE title IS NOT NULL ORDER BY at DESC LIMIT 5").fetchall()
    lines = [f"+ {r['title']}" for r in up] + [f"+ (found elsewhere, was missed) {r['title']}" for r in missed]
    lines += [f"- {r['title']}" for r in down]
    return "\n".join(lines) or "(no ratings yet)"


@dataclass
class ScoreInput:
    id: int
    title: str
    snippet: str
    source: str
    category: str


def _item_lines(items: list[ScoreInput]) -> str:
    out = []
    for it in items:
        snippet = (it.snippet or "")[:300]
        out.append(f"id={it.id} | {it.source} ({it.category}) | {it.title}" + (f"\n  snippet: {snippet}" if snippet else ""))
    return "\n".join(out)


def score_items(ai: AIClient, cfg: Config, conn: sqlite3.Connection, items: list[ScoreInput]) -> dict[int, dict]:
    """Scores a batch; splits it in halves when the model refuses or runs out of tokens.
    Returns id -> scores for the items that got a valid answer."""
    if not items:
        return {}
    messages = [{"role": "user", "content": [
        {"type": "text", "text": f"<recent_ratings>\n{ratings_text(conn)}\n</recent_ratings>",
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": f"<active_events>\n{active_events_text(conn)}\n</active_events>"},
        {"type": "text", "text": f"<items>\n{_item_lines(items)}\n</items>"},
    ]}]
    lines = want_lines()
    try:
        result = ai.call(purpose="score", model=cfg.scoring.model, system=system_blocks(cfg, lines), messages=messages,
                         max_tokens=16000, effort=cfg.scoring.effort,
                         schema=score_schema(cfg.scoring.categories, len(lines)))
    except (Refused, Truncated) as e:
        if len(items) == 1:
            log.warning("item could not be scored", extra={"item": items[0].id, "error": str(e)})
            return {items[0].id: {"error": str(e)}}
        mid = len(items) // 2
        return {**score_items(ai, cfg, conn, items[:mid]), **score_items(ai, cfg, conn, items[mid:])}

    wanted = {it.id for it in items}
    out: dict[int, dict] = {}
    entries = parse_json(result.text).get("items", [])
    for entry in entries if isinstance(entries, list) else []:
        # Models without strict structured output sometimes drop a key: such items count as missing from the
        # answer and are retried
        if not isinstance(entry, dict) or not _REQUIRED.issubset(entry):
            continue
        if entry.get("id") in wanted:
            out[entry["id"]] = {
                "topic": _clamp(entry["topic"]), "importance": _clamp(entry["importance"]),
                "personal": _clamp(entry["personal"]), "reason": str(entry.get("reason") or "")[:300],
                "category": entry["category"] if entry["category"] in cfg.scoring.categories else "Other",
                "event": normalize_label(entry.get("event")),
                "interest": _interest(entry.get("interest"), lines),
            }
    return out


def _interest(value, lines: list[str]) -> str | None:
    """The Want line an answer's number points at, normalized; None for 0, a missing or a wrong value."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= len(lines):
        return None
    return norm(lines[value - 1])


_REQUIRED = {"id", "topic", "importance", "personal", "category"}


def _clamp(v) -> int:
    try:
        return max(0, min(10, int(v)))
    except (TypeError, ValueError):
        return 0


def scoring_wait(conn: sqlite3.Connection, cfg: Config) -> str | None:
    """Why scoring should wait for more items, or None to score now. Ticks run every minute and feeds trickle
    in a few items at a time; one call per trickle paid the fixed prompt (instructions, ratings, active
    events) for 3-5 items. Collecting a batch first cuts calls without changing any score."""
    row = conn.execute("SELECT COUNT(*) AS n, MIN(fetched_at) AS oldest FROM items WHERE score_status = 'pending'").fetchone()
    if not row["n"] or row["n"] >= cfg.scoring.min_batch:
        return None
    if utcnow() - parse_iso(row["oldest"]) >= timedelta(minutes=cfg.scoring.max_wait_minutes):
        return None
    return f"{row['n']} pending, waiting for {cfg.scoring.min_batch} or {cfg.scoring.max_wait_minutes} min"


def run_scoring(conn: sqlite3.Connection, cfg: Config, ai: AIClient, max_batches: int = 40,
                force: bool = False) -> dict:
    if not cfg.scoring.enabled:
        return {"skipped": "scoring disabled"}
    if not profile_exists():
        return {"skipped": "no profile yet"}
    cutoff = iso(utcnow() - MAX_SCORE_AGE)
    conn.execute("UPDATE items SET score_status = 'skipped' WHERE score_status = 'pending' AND fetched_at < ?",
                 (cutoff,))
    if not force and (waiting := scoring_wait(conn, cfg)):
        return {"waiting": waiting}
    scored = 0
    newly: list[int] = []
    for _ in range(max_batches):
        rows = conn.execute(
            """SELECT id, title, snippet, publisher, source_id, category FROM items
               WHERE score_status = 'pending' ORDER BY fetched_at DESC, id DESC LIMIT ?""",
            (cfg.scoring.batch_size,)).fetchall()
        if not rows:
            break
        batch = [ScoreInput(r["id"], r["title"], r["snippet"] or "", r["publisher"] or r["source_id"],
                            r["category"]) for r in rows]
        try:
            results = score_items(ai, cfg, conn, batch)
        except BudgetExceeded:
            raise  # not the items' fault: they stay pending and show unscored
        except AIError:
            for r in rows:
                conn.execute("UPDATE items SET score_attempts = score_attempts + 1 WHERE id = ?", (r["id"],))
            conn.execute(f"UPDATE items SET score_status = 'error' WHERE score_status = 'pending' "
                         f"AND score_attempts >= {MAX_ATTEMPTS}")
            raise
        now = iso(utcnow())
        for r in rows:
            res = results.get(r["id"])
            if res and "error" not in res:
                conn.execute(
                    """UPDATE items SET score_status = 'scored', topic = ?, importance = ?, personal = ?,
                       reason = ?, ai_category = ?, scored_at = ?, event = ?, event_tries = event_tries + 1,
                       interest = ?
                       WHERE id = ?""",
                    (res["topic"], res["importance"], res["personal"], res["reason"], res["category"], now,
                     res["event"], res["interest"], r["id"]))
                scored += 1
                newly.append(r["id"])
            elif res:  # refused on its own
                conn.execute("UPDATE items SET score_status = 'refused', reason = ? WHERE id = ?",
                             (res["error"], r["id"]))
            else:  # missing from the answer: retry later, give up after a few attempts
                conn.execute(
                    """UPDATE items SET score_attempts = score_attempts + 1,
                       score_status = CASE WHEN score_attempts + 1 >= ? THEN 'error' ELSE 'pending' END
                       WHERE id = ?""", (MAX_ATTEMPTS, r["id"]))
    return {"scored": scored, "ids": newly}


def final_score(row, story_sources: int, cfg: Config) -> float | None:
    """Weighted sum of the sub-scores, +bonus when 2+ sources cover the story. Computed on read, so
    weight changes in config apply to everything immediately."""
    if row["topic"] is None:
        return None
    w = cfg.scoring.weights
    final = w.topic * row["topic"] + w.importance * row["importance"] + w.personal * row["personal"]
    if story_sources >= 2:
        final += cfg.scoring.cross_source_bonus
    return round(final, 1)
