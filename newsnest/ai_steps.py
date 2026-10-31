"""AI steps of a tick: scoring, summaries, weekly profile proposal.

Any failure (no key, API error, budget reached) is recorded for the status panel and the banner;
the items involved simply stay unscored and are shown newest first.
"""

from __future__ import annotations

import logging
import sqlite3

from newsnest.ai import AIClient, AIError, AIUnavailable, BudgetExceeded, record_ai_failure
from newsnest.config import Config
from newsnest.db import get_meta, loads
from newsnest.discovery import cap_trial_scoring
from newsnest.events import run_labeling
from newsnest.notify import Event, Notifier
from newsnest.profile import profile_exists
from newsnest.profile_ai import proposal_due, weekly_proposal
from newsnest.scoring import run_scoring
from newsnest.summaries import run_summaries

log = logging.getLogger(__name__)


def run_ai_steps(conn: sqlite3.Connection, cfg: Config, notifier: Notifier, force: bool = False) -> dict:
    """force (manual refresh): score what is pending now instead of waiting for a fuller batch."""
    out: dict = {}
    if capped := cap_trial_scoring(conn, cfg):
        out["capped"] = capped
    pending = conn.execute("SELECT COUNT(*) AS n FROM items WHERE score_status = 'pending'").fetchone()["n"]
    try:
        ai = AIClient.create(cfg, conn)
    except AIUnavailable as e:
        # Only an alarm once there is something to do; before onboarding nothing needs AI yet
        if profile_exists() and pending:
            record_ai_failure(conn, str(e))
        return {"ai": str(e)}

    steps = [
        ("scoring", lambda: run_scoring(conn, cfg, ai, force=force)),
        ("events", lambda: run_labeling(conn, cfg, ai)),
        ("summaries", lambda: run_summaries(conn, cfg, ai)),
        ("proposal", lambda: _maybe_proposal(conn, cfg, ai)),
    ]
    for name, step in steps:
        try:
            out[name] = step()
        except BudgetExceeded as e:
            out[name] = str(e)
            break  # nothing else may spend today
        except AIError as e:
            log.warning("AI step failed", extra={"step": name, "error": str(e)})
            record_ai_failure(conn, f"{name}: {e}")
            out[name] = f"error: {e}"
    _notify_top_stories(conn, cfg, notifier, out.get("scoring"))
    return {k: v for k, v in out.items() if k != "ids"}


def _maybe_proposal(conn: sqlite3.Connection, cfg: Config, ai: AIClient) -> str:
    if not profile_exists():
        return "no profile"
    due, why = proposal_due(conn, cfg)
    if not due:
        return why
    return f"proposal {weekly_proposal(conn, cfg, ai)}"


def _notify_top_stories(conn, cfg, notifier, scoring_result) -> None:
    if not isinstance(scoring_result, dict) or not scoring_result.get("ids"):
        return
    marks = ",".join("?" * len(scoring_result["ids"]))
    for r in conn.execute(f"SELECT title, url FROM items WHERE id IN ({marks}) AND importance >= ?",
                          [*scoring_result["ids"], cfg.scoring.top_story_importance]):
        notifier.notify(Event("top_story", r["title"], r["url"]))
    scoring_result.pop("ids", None)


def ai_health(conn: sqlite3.Connection) -> dict:
    return loads(get_meta(conn, "ai_status"), {}) or {}
