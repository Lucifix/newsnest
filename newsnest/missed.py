""""Missed this": a URL I found elsewhere. Logged as a false negative and scored, to show why it was missed."""

from __future__ import annotations

import sqlite3

from newsnest.ai import AIClient, AIError
from newsnest.config import Config
from newsnest.db import iso, utcnow
from newsnest.fetchers.base import FetchContext, FetchFailed, html_to_text, http_get, make_client
from newsnest.levels import NO_LEVELS, Levels, load_levels
from newsnest.scoring import ScoreInput, final_score, score_items
from newsnest.textmatch import normalize_url


def level_note(levels: Levels, interest: str | None) -> str:
    """'−0.5 for your level on Wristwatches' when the reader's level moved the score, else ''."""
    shift = levels.shift_of(interest)
    if not shift:
        return ""
    return f"{'+' if shift > 0 else '−'}{abs(shift):g} for your level on {levels.label_of(interest)}"


def explain_known(row: sqlite3.Row, final: float | None, cfg: Config, levels: Levels = NO_LEVELS) -> str:
    if row["prefilter"] == "muted":
        return "We had it, but a mute keyword dropped it."
    if row["score_status"] == "pending":
        return "We had it, but it was never scored (no profile yet, AI unavailable, or budget reached)."
    if row["score_status"] == "capped":
        return (f"We had it, but its source is on trial and had already used its {cfg.discovery.scored_per_source} "
                "scored stories for the day. Promote the source in Settings to get all of its stories.")
    if row["score_status"] in ("error", "refused", "skipped"):
        return f"We had it, but scoring failed ({row['score_status']})."
    note = f", {n}" if (n := level_note(levels, row["interest"])) else ""
    if final is not None and final < cfg.digest.min_final:
        return f"We had it, scored {final}{note} (below the {cfg.digest.min_final} display threshold): {row['reason']}"
    return f"We had it and it was shown with score {final}{note}: {row['reason']}"


def handle_missed(conn: sqlite3.Connection, cfg: Config, url: str, ai: AIClient | None) -> int:
    url = url.strip()
    known = conn.execute(
        "SELECT i.*, s.source_count FROM items i LEFT JOIN stories s ON s.id = i.story_id WHERE i.url_norm = ?",
        (normalize_url(url),)).fetchone()
    title, snippet, error = None, "", None
    if known:
        title, snippet = known["title"], known["snippet"] or ""
    else:
        with make_client(cfg) as client:
            try:
                r = http_get(FetchContext(cfg, client), url)
                import trafilatura
                meta = trafilatura.extract_metadata(r.text)
                title = (meta.title if meta else None) or url
                snippet = (meta.description if meta and meta.description else "") or html_to_text(
                    trafilatura.extract(r.text) or "", limit=400)
            except FetchFailed as e:
                error = f"could not fetch the page: {e}"
    scores, final = {}, None
    if title and ai is not None:
        try:
            res = score_items(ai, cfg, conn, [ScoreInput(0, title, snippet, "pasted link", "news")])
            scores = res.get(0, {})
            if "error" in scores:
                error, scores = scores["error"], {}
        except AIError as e:
            error = f"scoring failed: {e}"
    levels = load_levels(conn)
    if scores:
        final = final_score(scores, 1, cfg, levels.shift_of(scores.get("interest")))
    elif ai is None and not error:
        error = "AI unavailable, not scored"
    reason = scores.get("reason")
    if reason and (note := level_note(levels, scores.get("interest"))):
        reason = f"{reason} ({note})"
    db_status = None
    if known:
        known_final = final_score(known, known["source_count"] or 1, cfg, levels.shift_of(known["interest"]))
        db_status = explain_known(known, known_final, cfg, levels)
    else:
        db_status = "Not in any configured source: consider adding a feed or a watch."
    return conn.execute(
        """INSERT INTO misses (at, url, title, item_id, found_in_db, db_status, topic, importance, personal,
           final, reason, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (iso(utcnow()), url, title, known["id"] if known else None, int(bool(known)), db_status,
         scores.get("topic"), scores.get("importance"), scores.get("personal"), final, reason, error)).lastrowid
