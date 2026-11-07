"""Following what the reader is into lately (docs/interest-drift-plan.md; terms in GLOSSARY.md).

Most stories are read from the headline and summary and never opened, so a tap is only one sign of interest.
Every card that was shown in the feed gets an engagement value from how it was read:

  👎 / not interested                       -1 (wins over everything else)
  opened, liked, saved                      +1 each
  key points opened, lingered               +0.5 each      (at most 3 in all; scrolled past: 0)

"Lingered" is attention time (newsnest.web /api/attention) in the reader's own top quarter, at least 3 s.
Each card counts for one key: the Want line it matches (items.interest, still in the file), else its category;
and for its lead outlet. Per key, with older cards fading (half-life), engagement is compared with the reader's
usual and smoothed toward it (`prior` cards' worth), then limited:

  weight = clamp(((engaged + prior × base) / (shown + prior)) / base, min, max)

Nothing changes until reading time has been recorded for a while (warm-up), so every install learns from its
own reader first. Worked out from the stored signals every half hour (refresh_drift, by the scheduler) and
kept in meta as a cache only: nothing to reset, and changing a setting or a Want line works it out again.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from newsnest.config import Config
from newsnest.db import get_meta, iso, parse_iso, set_meta, utcnow
from newsnest.digest import Card, group_rows, rank_members
from newsnest.interests import norm

LIFTED, FADED = 1.1, 0.9          # outside this band a weight is worth a "lately" line
LINGER_FLOOR_MS = 3000            # a card under 3 s never counts as lingered
LINGER_MIN_CARDS = 200            # too few timed cards to know what "longer than usual" is
LINGER_QUANTILE = 0.75            # the reader's top quarter
DISLIKE_DAYS = 7                  # a 👎 this recent keeps an interest out of the revisit slot
LABEL_CHARS = 40


@dataclass
class CardSignals:
    """How one shown card was read."""
    key: str                  # "interest:<norm line>" | "category:<name>"
    source: str               # lead source_id
    at: datetime              # latest sign of any kind
    view_ms: int
    tapped: bool = False
    liked: bool = False
    disliked: bool = False
    saved: bool = False
    expanded: bool = False
    rated_at: datetime | None = None


@dataclass
class Drift:
    active: bool = False
    weights: dict[str, float] = field(default_factory=dict)  # "interest:…" / "category:…" / "source:…"
    lines: dict[str, str] = field(default_factory=dict)      # norm(line) -> line, the Want lines now in the file
    recent_dislikes: set[str] = field(default_factory=set)   # keys with a 👎 in the last DISLIKE_DAYS
    warmup_days_left: int | None = None   # while warming up: days to go (0: waiting for more stories)
    min_weight: float = 0.6
    max_weight: float = 1.4

    def key_of(self, card: Card) -> str | None:
        return card_key(card, self.lines)

    def weight(self, key: str | None) -> float:
        return self.weights.get(key, 1.0) if key else 1.0

    def source_weight(self, source_id: str) -> float:
        return self.weights.get(f"source:{source_id}", 1.0)

    def multiplier(self, card: Card) -> float:
        """What the card's rank is multiplied by: its interest (or category) and its outlet, within limits."""
        if not self.active:
            return 1.0
        m = self.weight(self.key_of(card)) * self.source_weight(card.lead.row["source_id"])
        return min(self.max_weight, max(self.min_weight, m))

    def label(self, key: str) -> str:
        kind, _, name = key.partition(":")
        return short_label(self.lines.get(name, name)) if kind == "interest" else name

    def faded(self) -> set[str]:
        """Interest and category keys the reader has been reading clearly less of lately."""
        return {k for k, w in self.weights.items() if not k.startswith("source:") and w <= FADED}


def short_label(line: str) -> str:
    """A Want line as a few words for the why panel: the part before ':' or ' (', at most LABEL_CHARS."""
    text = line.split(":", 1)[0].split(" (", 1)[0].strip() or line.strip()
    if len(text) <= LABEL_CHARS:
        return text
    cut = text[:LABEL_CHARS].rsplit(" ", 1)[0].rstrip(",;—-")
    return cut + "…"


def card_interest(rows: list, lines: dict[str, str]) -> str | None:
    """The Want line (normalized) a card is about, from its item rows lead first: the lead's label, else the
    most common label among its items (the higher-ranked one on a tie). A label for a line that is no longer
    in the file doesn't count."""
    labels = [r["interest"] for r in rows if r["interest"] and r["interest"] in lines]
    if not labels:
        return None
    if rows[0]["interest"] in lines:
        return rows[0]["interest"]
    counts = Counter(labels)
    best = max(counts.values())
    return next(label for label in labels if counts[label] == best)


def rows_key(rows: list, lines: dict[str, str]) -> str | None:
    """The key a card counts for, from its item rows lead first: its interest, else its lead's category."""
    interest = card_interest(rows, lines)
    if interest:
        return f"interest:{interest}"
    category = rows[0]["ai_category"]
    return f"category:{category}" if category else None


def card_key(card: Card, lines: dict[str, str]) -> str | None:
    return rows_key([card.lead.row, *(m.row for m in card.others)], lines)


def load_signals(conn: sqlite3.Connection, cfg: Config, since: datetime, lines: dict[str, str]) -> list[CardSignals]:
    """Every card shown in the feed (or opened) whose latest sign is at or after `since`, as CardSignals.
    Cards are grouped and led exactly as the feed's (digest.group_rows, rank_members). Ratings on cards never
    shown in the feed (the swipe deck) don't count: the deck asks about borderline stories on purpose."""
    start = iso(since)
    # Recent activity is found through its indexes (items_last_seen, items_opened, items_rated), then every
    # other copy of those stories and events joins so cards are whole. UNION ALL: a plain UNION lets SQLite
    # merge the three in id order by walking the whole table. Only the columns used here are read.
    rows = conn.execute(
        """WITH recent AS (
               SELECT id FROM items WHERE last_seen_at >= :s
               UNION ALL SELECT id FROM items WHERE opened_at >= :s
               UNION ALL SELECT id FROM items WHERE rated_at >= :s AND rating IS NOT NULL),
           linked AS (SELECT story_id, event FROM items WHERE id IN (SELECT id FROM recent))
           SELECT id, story_id, event, source_id, published, topic, importance, personal, ai_category, interest,
                  seen_count, view_ms, last_seen_at, opened_at, rating, rated_at, saved_at, expanded_at
           FROM items WHERE id IN (SELECT id FROM recent)
              OR story_id IN (SELECT story_id FROM linked)
              OR event IN (SELECT event FROM linked WHERE event IS NOT NULL)
           ORDER BY id""", {"s": start}).fetchall()
    out: list[CardSignals] = []
    for group in group_rows(rows):
        if not any(r["seen_count"] > 0 or r["opened_at"] for r in group):
            continue
        latest = max((t for r in group for t in (r["last_seen_at"], r["opened_at"], r["rated_at"], r["saved_at"],
                                                  r["expanded_at"]) if t), default=None)
        if latest is None or latest < start:  # ISO strings in UTC compare like the times they hold
            continue
        ranked = [r for r, _ in rank_members(group, cfg)] if len(group) > 1 else group
        key = rows_key(ranked, lines)
        if key is None:
            continue
        ratings = [r["rating"] for r in group if r["rating"] is not None]
        rated = max((r["rated_at"] for r in group if r["rating"] is not None and r["rated_at"]), default=None)
        out.append(CardSignals(
            key=key, source=ranked[0]["source_id"], at=parse_iso(latest),
            view_ms=max(r["view_ms"] or 0 for r in group),
            tapped=any(r["opened_at"] for r in group), liked=1 in ratings, disliked=-1 in ratings,
            saved=any(r["saved_at"] for r in group), expanded=any(r["expanded_at"] for r in group),
            rated_at=parse_iso(rated) if rated else None))
    return out


def linger_threshold(signals: list[CardSignals]) -> int | None:
    """Attention time from which a card counts as lingered: the reader's 75th percentile (nearest rank),
    at least LINGER_FLOOR_MS. None while fewer than LINGER_MIN_CARDS cards have a time."""
    timed = sorted(s.view_ms for s in signals if s.view_ms > 0)
    if len(timed) < LINGER_MIN_CARDS:
        return None
    return max(LINGER_FLOOR_MS, timed[math.ceil(LINGER_QUANTILE * len(timed)) - 1])


def engagement(s: CardSignals, threshold: int | None) -> float:
    if s.disliked:
        return -1.0
    lingered = threshold is not None and s.view_ms >= threshold
    return min(3.0, s.tapped + s.liked + s.saved + 0.5 * s.expanded + 0.5 * lingered)


def compute_weights(signals: list[CardSignals], cfg: Config, now: datetime) -> dict[str, float]:
    """Weight per key and per outlet; {} when there is nothing to compare with (no engagement at all)."""
    d = cfg.drift
    threshold = linger_threshold(signals)
    shown: dict[str, float] = defaultdict(float)
    engaged: dict[str, float] = defaultdict(float)
    for s in signals:
        age = max(0.0, (now - s.at).total_seconds() / 86400)
        a = 0.5 ** (age / d.half_life_days)
        v = engagement(s, threshold)
        for key in (s.key, f"source:{s.source}"):
            shown[key] += a
            engaged[key] += a * v
    total_shown = sum(w for k, w in shown.items() if not k.startswith("source:"))
    total_engaged = sum(w for k, w in engaged.items() if not k.startswith("source:"))
    if total_shown <= 0 or total_engaged <= 0:
        return {}
    base = total_engaged / total_shown
    out = {}
    for key, s in shown.items():
        lo, hi = (d.source_min_weight, d.source_max_weight) if key.startswith("source:") else (d.min_weight, d.max_weight)
        rate = (engaged[key] + d.prior * base) / (s + d.prior)
        out[key] = round(min(hi, max(lo, rate / base)), 3)
    return out


# Reading the signals takes a while on a long history (it grows with every story shown), so the scheduler
# works the weights out every DRIFT_REFRESH and stores them; a ranking uses the stored ones. If they're
# older than DRIFT_STALE (the scheduler is off) or were made for other settings or Want lines, the
# ranking works them out itself. Half-lives are measured in days: minutes of lag change nothing.
DRIFT_REFRESH = timedelta(minutes=30)
DRIFT_STALE = timedelta(hours=3)


def _fingerprint(cfg: Config, lines: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps([cfg.drift.model_dump(), sorted(lines)], sort_keys=True).encode()).hexdigest()[:16]


def _store(conn: sqlite3.Connection, drift: Drift, fp: str, now: datetime) -> None:
    set_meta(conn, "drift", json.dumps({"at": iso(now), "fp": fp, "active": drift.active, "weights": drift.weights,
                                        "recent_dislikes": sorted(drift.recent_dislikes),
                                        "warmup_days_left": drift.warmup_days_left}))


def refresh_drift(conn: sqlite3.Connection, cfg: Config, now: datetime | None = None, force: bool = False) -> Drift | None:
    """Work the weights out and store them, when the stored ones are older than DRIFT_REFRESH (the scheduler
    calls this every tick). Returns the new Drift, or None when the stored one is still fresh."""
    now = now or utcnow()
    lines = current_lines()
    fp = _fingerprint(cfg, lines)
    stored = json.loads(get_meta(conn, "drift") or "null")
    if not force and stored and stored["fp"] == fp and now - parse_iso(stored["at"]) < DRIFT_REFRESH:
        return None
    drift = compute_drift(conn, cfg, now, lines)
    _store(conn, drift, fp, now)
    return drift


def current_drift(conn: sqlite3.Connection, cfg: Config, now: datetime | None = None) -> Drift:
    """The weights a ranking uses: the stored ones when fresh enough and made for these settings and lines,
    otherwise worked out (and stored) now."""
    if not cfg.drift.enabled:
        return Drift()
    now = now or utcnow()
    lines = current_lines()
    stored = json.loads(get_meta(conn, "drift") or "null")
    if stored and stored["fp"] == _fingerprint(cfg, lines) and now - parse_iso(stored["at"]) < DRIFT_STALE:
        return Drift(active=stored["active"], weights=stored["weights"], lines=lines,
                     recent_dislikes=set(stored["recent_dislikes"]), warmup_days_left=stored["warmup_days_left"],
                     min_weight=cfg.drift.min_weight, max_weight=cfg.drift.max_weight)
    return refresh_drift(conn, cfg, now, force=True)


@dataclass
class KeyCount:
    shown: int = 0
    read: int = 0             # engagement above 0: opened, liked, saved, key points or lingered


@dataclass
class ReadingReport:
    """What the weights are worked out from, as plain counts of cards (no fading), for the reader to check.
    Same cards and window as compute_drift."""
    since: datetime | None = None         # attention start; None: nothing recorded yet
    shown: int = 0
    opened: int = 0
    expanded: int = 0
    lingered: int = 0
    saved: int = 0
    liked: int = 0
    disliked: int = 0
    timed: int = 0                        # shown cards with any attention time
    threshold: int | None = None          # ms from which a card counts as lingered
    keys: dict[str, KeyCount] = field(default_factory=dict)   # interest/category keys and "source:" keys


def reading_report(conn: sqlite3.Connection, cfg: Config, now: datetime | None = None,
                   lines: dict[str, str] | None = None) -> ReadingReport:
    started = get_meta(conn, "attention_since")
    if not started:
        return ReadingReport()
    now = now or utcnow()
    since = parse_iso(started)
    lines = current_lines() if lines is None else lines
    signals = load_signals(conn, cfg, max(now - timedelta(days=cfg.drift.window_days), since), lines)
    threshold = linger_threshold(signals)
    report = ReadingReport(since=since, shown=len(signals), threshold=threshold,
                           timed=sum(s.view_ms > 0 for s in signals))
    for s in signals:
        report.opened += s.tapped
        report.expanded += s.expanded
        report.lingered += threshold is not None and s.view_ms >= threshold
        report.saved += s.saved
        report.liked += s.liked
        report.disliked += s.disliked
        read = engagement(s, threshold) > 0
        for key in (s.key, f"source:{s.source}"):
            count = report.keys.setdefault(key, KeyCount())
            count.shown += 1
            count.read += read
    return report


def current_lines() -> dict[str, str]:
    from newsnest.scoring import want_lines  # scoring pulls in the AI client; only needed here
    return {norm(line): line for line in want_lines()}


def compute_drift(conn: sqlite3.Connection, cfg: Config, now: datetime | None = None,
                  lines: dict[str, str] | None = None) -> Drift:
    """The weights for one ranking, or an inactive Drift (every weight 1.0) when it's turned off or still
    warming up."""
    d = cfg.drift
    now = now or utcnow()
    if not d.enabled:
        return Drift()
    lines = current_lines() if lines is None else lines
    base = Drift(lines=lines, min_weight=d.min_weight, max_weight=d.max_weight)
    started = get_meta(conn, "attention_since")
    if not started:
        base.warmup_days_left = math.ceil(d.warmup_days)
        return base
    started_at = parse_iso(started)
    days_left = max(0, math.ceil(d.warmup_days - (now - started_at).total_seconds() / 86400))
    signals = load_signals(conn, cfg, max(now - timedelta(days=d.window_days), started_at), lines)
    if days_left > 0 or len(signals) < d.warmup_stories:
        base.warmup_days_left = days_left
        return base
    weights = compute_weights(signals, cfg, now)
    if not weights:
        base.warmup_days_left = 0
        return base
    recent = now - timedelta(days=DISLIKE_DAYS)
    base.active = True
    base.weights = weights
    base.recent_dislikes = {s.key for s in signals if s.disliked and s.rated_at and s.rated_at >= recent}
    return base
