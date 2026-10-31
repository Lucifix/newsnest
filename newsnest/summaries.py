"""Key-point summaries: a one-line lede, 2-4 bullets, the personal angle and whether the full article
is worth reading. Written automatically for stories whose best item scores >= summary_threshold, and
on demand from the card's "Key points" button for any other story.

Article text comes from trafilatura. If the fetch or extraction fails, or the page is paywalled
(too little text), the summary is written from the RSS snippet and marked "from snippet only".
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import timedelta

from newsnest.ai import AIClient, AIError, BudgetExceeded, parse_json
from newsnest.config import Config
from newsnest.db import dumps, iso, loads, utcnow
from newsnest.fetchers.base import FetchContext, FetchFailed, http_get, make_client
from newsnest.profile import read_profile, strip_comments
from newsnest.scoring import final_score

log = logging.getLogger(__name__)

MIN_ARTICLE_CHARS = 500   # less than this after extraction = paywall or teaser page
MAX_ARTICLE_CHARS = 15000
SUMMARY_WINDOW = timedelta(days=2)
MAX_POINTS = 4
WHY_MIN_PERSONAL = 6      # the personal angle shows only on stories that scored as personally relevant

SYSTEM = """\
You turn a news article into key points for a busy reader who would rather not read the whole thing. \
Write in {language}. Use only facts that are in the text.

- lede: one sentence of at most 20 words saying what happened. Lead with the news, not the background, \
and do not repeat the headline word for word.
- points: 2-4 bullets of at most 15 words each with what the lede leaves out: the key number, name, \
date, price, deadline, or what happens next. Never repeat the lede. Use fewer bullets for a simple story. \
No full stop at the end of a bullet.
- why: one short sentence (at most 15 words) on how this concretely affects the reader, based on their \
interest profile. Leave it empty unless the link is direct and specific. Never write generic lines \
such as "this may interest you".
- depth: "full" only when the article offers real value the points cannot carry: a step-by-step guide, \
in-depth analysis, an interview, original data or a review. Otherwise "enough". Most news is "enough".
- depth_tag: when depth is "full", one or two words naming what the article offers (guide, analysis, \
interview, data, review). Otherwise empty."""

SCHEMA = {
    "type": "object",
    "properties": {
        "lede": {"type": "string"},
        "points": {"type": "array", "items": {"type": "string"}},
        "why": {"type": "string"},
        "depth": {"type": "string", "enum": ["enough", "full"]},
        "depth_tag": {"type": "string"},
    },
    "required": ["lede", "points", "why", "depth", "depth_tag"],
    "additionalProperties": False,
}


def candidates(conn: sqlite3.Connection, cfg: Config, limit: int) -> list[sqlite3.Row]:
    rows = conn.execute(
        """SELECT i.*, s.source_count FROM items i JOIN stories s ON s.id = i.story_id
           WHERE i.score_status = 'scored' AND i.fetched_at >= ?""",
        (iso(utcnow() - SUMMARY_WINDOW),)).fetchall()
    done_stories = {r["story_id"] for r in rows if r["summary_status"]}
    best: dict[int, tuple[float, sqlite3.Row]] = {}
    for r in rows:
        if r["story_id"] in done_stories:
            continue
        f = final_score(r, r["source_count"], cfg)
        if f is not None and f >= cfg.scoring.summary_threshold and (r["story_id"] not in best or f > best[r["story_id"]][0]):
            best[r["story_id"]] = (f, r)
    ranked = sorted(best.values(), key=lambda t: t[0], reverse=True)
    return [r for _, r in ranked[:limit]]


def extract_article(ctx: FetchContext, url: str) -> tuple[str | None, str | None, str | None]:
    """Returns (text, error, preview image url)."""
    import trafilatura  # heavy import, only needed here

    try:
        r = http_get(ctx, url)
    except FetchFailed as e:
        return None, str(e), None
    meta = trafilatura.extract_metadata(r.text)
    image = meta.image if meta and meta.image and meta.image.startswith("https://") else None
    text = trafilatura.extract(r.text, url=str(r.url), include_comments=False, include_tables=False,
                               favor_precision=True)
    if not text or len(text) < MIN_ARTICLE_CHARS:
        return None, "too little text (paywall or teaser)", image
    return text[:MAX_ARTICLE_CHARS], None, image


def system_blocks(cfg: Config) -> list[dict]:
    blocks = [{"type": "text", "text": SYSTEM.format(language=cfg.scoring.summary_language)}]
    profile = strip_comments(read_profile("interests.md"))
    if profile:
        blocks.append({"type": "text", "text": f"<interest_profile>\n{profile}\n</interest_profile>"})
    return blocks


def key_points(row) -> list[str] | None:
    """The bullets of a key-point summary; None for an older prose summary."""
    return loads(row["summary_points"]) if row["summary_points"] is not None else None


def clean_points(points: list) -> list[str]:
    out = []
    for p in points:
        p = str(p).strip().lstrip("-•*· ").strip()
        if p:
            out.append(p)
    return out[:MAX_POINTS]


def summarize_one(conn: sqlite3.Connection, cfg: Config, ai: AIClient, ctx: FetchContext, r: sqlite3.Row,
                  purpose: str = "summary") -> str:
    """Fetches the article and writes the key points. Returns the new summary_status:
    'done', 'no_text' (nothing to summarize) or 'error'. BudgetExceeded propagates."""
    text, _err, image = extract_article(ctx, r["url"])
    extra = loads(r["extra"], {}) or {}
    if image and not extra.get("image"):  # feeds like TechCrunch and HN carry no image
        conn.execute("UPDATE items SET extra = ? WHERE id = ?", (dumps({**extra, "image": image}), r["id"]))
    source = "article"
    if text is None:
        if not r["snippet"]:
            conn.execute("UPDATE items SET summary_status = 'no_text', summary = NULL WHERE id = ?", (r["id"],))
            return "no_text"
        text, source = r["snippet"], "snippet"
    picked = f"Why it was picked for this reader: {r['reason']}\n" if r["reason"] else ""
    body = ("Article text" if source == "article" else
            "Only the feed snippet is available (give at most 2 points and nothing beyond it)")
    try:
        result = ai.call(
            purpose=purpose, model=cfg.scoring.model, system=system_blocks(cfg),
            messages=[{"role": "user", "content":
                       f"Title: {r['title']}\nSource: {r['publisher'] or r['source_id']}\n{picked}\n{body}:\n{text}"}],
            max_tokens=4000, effort=cfg.scoring.effort, schema=SCHEMA)
        data = parse_json(result.text)
    except BudgetExceeded:
        raise
    except AIError as e:
        conn.execute("UPDATE items SET summary_status = 'error', summary = ? WHERE id = ?", (str(e), r["id"]))
        return "error"
    lede = str(data.get("lede") or "").strip()
    if not lede:
        conn.execute("UPDATE items SET summary_status = 'error', summary = 'empty summary' WHERE id = ?", (r["id"],))
        return "error"
    why = str(data.get("why") or "").strip()
    if (r["personal"] or 0) < WHY_MIN_PERSONAL:
        why = ""
    # The model never saw the article when it only had the snippet, so it cannot judge depth
    depth = data.get("depth") if source == "article" and data.get("depth") in ("enough", "full") else None
    tag = str(data.get("depth_tag") or "").strip()[:24] if depth == "full" else ""
    conn.execute(
        """UPDATE items SET summary = ?, summary_points = ?, summary_why = ?, summary_depth = ?,
               summary_depth_tag = ?, summary_source = ?, summary_status = 'done' WHERE id = ?""",
        (lede, dumps(clean_points(data.get("points") or [])), why or None, depth, tag or None, source, r["id"]))
    return "done"


def run_summaries(conn: sqlite3.Connection, cfg: Config, ai: AIClient, limit: int = 15) -> dict:
    rows = candidates(conn, cfg, limit)
    if not rows:
        return {"summarized": 0}
    done = 0
    with make_client(cfg) as client:
        ctx = FetchContext(cfg, client)
        for i, r in enumerate(rows):
            if i:
                time.sleep(1)  # polite to publishers
            if summarize_one(conn, cfg, ai, ctx, r) == "done":
                done += 1
    return {"summarized": done}


def summarize_on_demand(conn: sqlite3.Connection, cfg: Config, ai: AIClient, item_id: int) -> str:
    """The card's button. Returns the item's summary_status after the attempt."""
    r = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if r is None:
        return "missing"
    if r["summary_status"] == "done":
        return "done"
    with make_client(cfg) as client:
        return summarize_one(conn, cfg, ai, FetchContext(cfg, client), r, purpose="summary_tap")
