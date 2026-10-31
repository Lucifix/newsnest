"""Claude jobs that write the profile: the onboarding draft and the weekly interests.md proposal.

Both are stored in `proposals` and only reach interests.md when I approve (or edit and save) them.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

from newsnest.ai import AIClient, parse_json
from newsnest.config import Config
from newsnest.db import iso, parse_iso, utcnow
from newsnest.interests import OPS, SECTIONS, apply_changes
from newsnest.profile import read_profile, seed_examples, strip_comments
from newsnest.scoring import final_score

BULLET_RULES = """\
Bullets are short topics, places or kinds of stories, under about 12 words each. No individual \
headlines, product names or lists of examples in parentheses. Keep to about 20 bullets under Want and \
12 under Skip, so merge or sharpen instead of only adding."""

PROFILE_FORMAT = f"""\
Write it as Markdown with two sections, "## Want" and "## Skip", each a list of concrete bullets \
(e.g. "Ljubljana traffic, parking and construction", "celebrity gossip"). {BULLET_RULES} English."""

DRAFT_SYSTEM = f"""\
You draft a news interest profile for one reader in Ljubljana, Slovenia, from headlines they rated \
(+ = want, - = don't want). Most headlines are Slovenian. Infer the underlying interests, \
generalise from the examples, and do not invent interests the ratings don't support. {PROFILE_FORMAT}"""

PROPOSAL_SYSTEM = f"""\
You maintain a reader's news interest profile, a Markdown file with "## Want" and "## Skip" bullets. \
You get the current profile and what happened since it was last updated: items the reader rated (with \
the score the system gave them), articles the reader found elsewhere because the system missed them, \
and "less/more of this" choices the reader made on the feed. Propose the smallest set of changes that \
would have ranked these better: add what the reader clearly wants, add skips for what they reject, \
sharpen vague bullets, and leave everything that still holds alone. Each change is one bullet: \
"add" (leave old empty), "remove" or "reword" (copy the existing bullet exactly into old, without the \
leading dash). The reason is one short sentence with the evidence, e.g. "rated up 4 recall stories, \
two of them scored below the limit". If the profile already explains the ratings, return no changes. \
{BULLET_RULES}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "interests_md": {"type": "string"},
        "rationale": {"type": "string"},
        "changed": {"type": "boolean"},
    },
    "required": ["interests_md", "rationale", "changed"],
    "additionalProperties": False,
}

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": list(OPS)},
                    "section": {"type": "string", "enum": list(SECTIONS)},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["op", "section", "old", "new", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "changes"],
    "additionalProperties": False,
}


def rated_for_onboarding(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT title, rating FROM items WHERE rating IS NOT NULL ORDER BY rated_at DESC LIMIT 80").fetchall()


def draft_profile(conn: sqlite3.Connection, cfg: Config, ai: AIClient) -> int:
    rated = rated_for_onboarding(conn)
    lines = [f"{'+' if r['rating'] > 0 else '-'} {r['title']}" for r in rated]
    lines += [f"{'+' if want else '-'} {title}" for want, title in seed_examples()]
    result = ai.call(purpose="profile_draft", model=cfg.scoring.profile_model,
                     system=[{"type": "text", "text": DRAFT_SYSTEM}],
                     messages=[{"role": "user", "content": "<ratings>\n" + "\n".join(lines) + "\n</ratings>"}],
                     max_tokens=16000, effort="medium", schema=SCHEMA)
    data = parse_json(result.text)
    return conn.execute(
        "INSERT INTO proposals (created_at, kind, status, current_text, proposed_text, rationale) VALUES (?, 'draft', 'pending', ?, ?, ?)",
        (iso(utcnow()), read_profile("interests.md"), data["interests_md"].strip() + "\n", data["rationale"])).lastrowid


def proposal_due(conn: sqlite3.Connection, cfg: Config) -> tuple[bool, str]:
    if cfg.scoring.proposal_every_days <= 0:
        return False, "disabled"
    last = conn.execute("SELECT MAX(created_at) AS at FROM proposals WHERE kind = 'weekly'").fetchone()["at"]
    since = parse_iso(last) if last else None
    if since and utcnow() - since < timedelta(days=cfg.scoring.proposal_every_days):
        return False, "not due"
    since_iso = iso(since) if since else "1970-01-01"
    signals = conn.execute("SELECT COUNT(*) AS n FROM items WHERE rated_at > ?", (since_iso,)).fetchone()["n"]
    signals += conn.execute("SELECT COUNT(*) AS n FROM misses WHERE at > ?", (since_iso,)).fetchone()["n"]
    signals += conn.execute("SELECT COUNT(*) AS n FROM feedback WHERE at > ? AND kind != 'not_interested'",
                            (since_iso,)).fetchone()["n"]
    if signals < 10:
        return False, f"only {signals} ratings/misses since the last proposal"
    return True, ""


def weekly_proposal(conn: sqlite3.Connection, cfg: Config, ai: AIClient) -> int | None:
    last = conn.execute("SELECT MAX(created_at) AS at FROM proposals WHERE kind = 'weekly'").fetchone()["at"] or "1970-01-01"
    rated = conn.execute(
        """SELECT i.*, s.source_count FROM items i JOIN stories s ON s.id = i.story_id
           WHERE i.rated_at > ? ORDER BY i.rated_at DESC LIMIT 150""", (last,)).fetchall()
    lines = []
    for r in rated:
        f = final_score(r, r["source_count"], cfg)
        verdict = "+" if r["rating"] > 0 else "-"
        flag = ""
        if f is not None and r["rating"] > 0 and f < cfg.digest.min_final:
            flag = " [UNDERRATED: wanted but scored low]"
        elif f is not None and r["rating"] < 0 and f >= cfg.scoring.summary_threshold:
            flag = " [OVERRATED: rejected but scored high]"
        lines.append(f"{verdict} {r['title']} (score {f if f is not None else 'none'}; {r['reason'] or ''}){flag}")
    misses = conn.execute("SELECT * FROM misses WHERE at > ? ORDER BY at DESC LIMIT 30", (last,)).fetchall()
    miss_lines = [f"MISSED: {m['title'] or m['url']} (score {m['final']}; {m['reason'] or m['error'] or ''})" for m in misses]
    # Card-menu feedback ("less from 24ur", "more about Health"); seen-but-skipped is never sent here
    fb = conn.execute("SELECT kind, target, COUNT(*) AS n FROM feedback WHERE at > ? AND target IS NOT NULL "
                      "GROUP BY kind, target ORDER BY n DESC LIMIT 30", (last,)).fetchall()
    labels = {"less_source": "less from outlet", "more_source": "more from outlet",
              "less_category": "less about", "more_category": "more about"}
    fb_lines = [f"{labels.get(r['kind'], r['kind'])}: {r['target']} (x{r['n']})" for r in fb]
    file_text = read_profile("interests.md")
    current = strip_comments(file_text)
    result = ai.call(purpose="profile_proposal", model=cfg.scoring.profile_model,
                     system=[{"type": "text", "text": PROPOSAL_SYSTEM}],
                     messages=[{"role": "user", "content":
                                f"<current_profile>\n{current}\n</current_profile>\n\n<ratings>\n" + "\n".join(lines)
                                + "\n</ratings>\n\n<missed>\n" + ("\n".join(miss_lines) or "(none)") + "\n</missed>"
                                + "\n\n<feed_feedback>\n" + ("\n".join(fb_lines) or "(none)") + "\n</feed_feedback>"}],
                     max_tokens=16000, effort="medium", schema=PROPOSAL_SCHEMA)
    data = parse_json(result.text)
    # Only changes that fit the file as it is now; the rest (a bullet the model misquoted) can't be applied anyway
    proposed, items, _ = apply_changes(file_text, data["changes"])
    basis = {"ratings": len(rated), "missed": len(misses), "choices": sum(r["n"] for r in fb), "since": last}
    return conn.execute(
        "INSERT INTO proposals (created_at, kind, status, current_text, proposed_text, rationale, changes) "
        "VALUES (?, 'weekly', ?, ?, ?, ?, ?)",
        (iso(utcnow()), "pending" if items else "no_change", file_text, proposed, data["summary"],
         json.dumps({"basis": basis, "items": items}))).lastrowid
