"""How much of each interest: a level per Want line (terms in GLOSSARY.md).

A level shifts the score of every card matched to that line, before the cut-off decides what gets in:

  Much less -1, Less -0.5, Normal 0, More +0.5, Much more +1

The shift is per card: its interest (newsnest.interests.card_interest, the rule drift uses) decides it,
and every article of the card gets it, so an article that matched nothing can't lead the card past it.
Single articles (key points, discovery, calibration, the weekly suggestion, Missed this) use their own
interest. Levels are kept in the database, not in interests.md, because the AI reads that file on every
call and the weekly suggestion rewrites it. A level only counts while its line is in the file.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from newsnest.config import Config
from newsnest.db import iso, utcnow
from newsnest.interests import bullets, card_interest, norm, short_label
from newsnest.profile import strip_comments
from newsnest.scoring import want_lines

STEP = 0.5
NAMES = {-2: "Much less", -1: "Less", 0: "Normal", 1: "More", 2: "Much more"}
COUNT_DAYS = 7


@dataclass(frozen=True)
class Levels:
    steps: dict[str, int] = field(default_factory=dict)   # norm(line) -> step, only lines in the file, never 0
    lines: dict[str, str] = field(default_factory=dict)   # norm(line) -> line: every Want line now in the file

    def shift_of(self, interest: str | None) -> float:
        return self.steps.get(interest, 0) * STEP if interest else 0.0

    def label_of(self, interest: str | None) -> str | None:
        return short_label(self.lines[interest]) if interest in self.lines else None

    def of_card(self, rows: list) -> tuple[str | None, float]:
        """A card's interest and shift, from its item rows lead first."""
        interest = card_interest(rows, self.lines) if self.lines else None
        return interest, self.shift_of(interest)


NO_LEVELS = Levels()


def load_levels(conn: sqlite3.Connection) -> Levels:
    lines = {norm(line): line for line in want_lines()}
    steps = {r["interest"]: r["step"] for r in conn.execute("SELECT interest, step FROM interest_levels")
             if r["interest"] in lines and r["step"]}
    return Levels(steps=steps, lines=lines)


def _changed(conn: sqlite3.Connection) -> None:
    """What gets in has changed: every device's feed session is ranked again on its next load."""
    conn.execute("DELETE FROM feed_sessions")


def set_level(conn: sqlite3.Connection, line: str, step: int) -> None:
    """Save one line's level; Normal (0) removes the row."""
    if step not in NAMES:
        raise ValueError(f"no level {step}")
    if step == 0:
        conn.execute("DELETE FROM interest_levels WHERE interest = ?", (norm(line),))
    else:
        conn.execute("INSERT INTO interest_levels (interest, line, step, updated_at) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(interest) DO UPDATE SET line = excluded.line, step = excluded.step, "
                     "updated_at = excluded.updated_at", (norm(line), line, step, iso(utcnow())))
    _changed(conn)


def forget_removed(conn: sqlite3.Connection, text: str) -> list[tuple[str, str]]:
    """After interests.md was saved as `text`: drop the levels of lines no longer in it. Returns
    [(short label, level name)] of what was dropped, for a note on the page."""
    want = {norm(line) for line in bullets(strip_comments(text))["want"]}
    gone = [r for r in conn.execute("SELECT interest, line, step FROM interest_levels ORDER BY line")
            if r["interest"] not in want]
    for r in gone:
        conn.execute("DELETE FROM interest_levels WHERE interest = ?", (r["interest"],))
    if gone:
        _changed(conn)
    return [(short_label(r["line"]), NAMES.get(r["step"], "")) for r in gone]


def daily_counts(conn: sqlite3.Connection, cfg: Config, levels: Levels, now: datetime | None = None) -> dict[str, float]:
    """Per interest: how many of its stories a day got in over the last COUNT_DAYS days, at the levels as they
    are now. Counts cards at or above the cut-off plus top stories and watch matches; caps, exploring and
    stories that sank out after being shown are left out, so it's an estimate."""
    from newsnest.digest import make_cards  # digest imports this module for Levels
    since = iso((now or utcnow()) - timedelta(days=COUNT_DAYS))
    rows = conn.execute(
        """SELECT i.*, s.source_count FROM items i JOIN stories s ON s.id = i.story_id
           WHERE i.fetched_at >= ? ORDER BY i.id""", (since,)).fetchall()
    counts: dict[str, int] = {}
    for card in make_cards(rows, cfg, {}, levels):
        if card.interest is None or card.muted or card.final is None:
            continue
        top = card.lead.row["importance"] is not None and card.importance >= cfg.scoring.top_story_importance
        if top or card.must_show or card.final >= cfg.digest.min_final:
            counts[card.interest] = counts.get(card.interest, 0) + 1
    return {key: n / COUNT_DAYS for key, n in counts.items()}


def per_day_text(per_day: float) -> str:
    if per_day <= 0:
        return f"None in the last {COUNT_DAYS} days"
    if per_day < 1:
        return "Less than 1 a day"
    return f"About {round(per_day)} a day"


def level_rows(conn: sqlite3.Connection, cfg: Config | None) -> list[dict]:
    """The Profile page's list: one row per Want line, in file order. Without a config there are no counts."""
    levels = load_levels(conn)
    counts = daily_counts(conn, cfg, levels) if cfg else None
    return [{"line": line, "label": short_label(line), "step": levels.steps.get(key, 0),
             "count": per_day_text(counts.get(key, 0.0)) if counts is not None else None}
            for key, line in levels.lines.items()]
