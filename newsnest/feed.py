"""The home feed, Discover-style: one ranked stream of unread stories from a rolling window.

rank = final score x freshness x boosts x learned weights
  freshness  floor + (1 - floor) x 0.5^(age / half-life): decays, but a strong story from yesterday
             still beats a weak fresh one
  boosts     top story (importance >= threshold), watch / must-show, not seen yet
  sinking    every time a story was on screen without being opened it sinks; after
             feed.seen_drop_after impressions it drops out (top stories only sink)
  weights    per outlet and per category, learned from the card menu ("less from <outlet>")
Read stories leave the feed (they are listed under "Read"), unless the story was updated
afterwards: new outlets joined it after it was read (two for a top story, otherwise
three, at least one of them in the last few hours). It is then back as unread, marked Updated.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import zip_longest

from newsnest.config import Config, Source
from newsnest.db import iso, parse_iso, utcnow
from newsnest.digest import Card, Member, make_cards
from newsnest.profile import profile_exists
from newsnest.sections import HEADLINES, LABELS, SECTIONS, TABS, block_of, scope_blocks, tab_of

WEIGHT_MIN, WEIGHT_MAX = 0.2, 2.0


@dataclass
class FeedItem:
    card: Card
    rank: float
    block: str                # news (Local) | tech | world; a Local outlet's foreign news is world
    category: str
    top: bool
    updated: list[Member] = field(default_factory=list)   # outlets that joined after the story was read
    big: bool = False
    discovered: bool = False  # from a candidate source still on trial
    first: bool = False       # only the first story's image loads eagerly
    extra: str = ""           # "new source" | "exploring": shown below the score threshold to widen the feed

    @property
    def read_ids(self) -> str:
        return self.card.ids


@dataclass
class Chip:
    key: str      # all | headlines | news | tech | world | lifestyle | cat:<scope>:<name>
    label: str
    count: int
    scope: bool = False   # All / Local / World: picks the part of the feed, not a category
    fold: bool = False    # a long-tail category, folded behind More


@dataclass
class Feed:
    items: list[FeedItem]              # the ranked stream for the active filter
    chips: list[Chip]                  # For you / Tech / News / Lifestyle
    subchips: list[Chip]               # under News or Tech: All + that block's categories
    active: str                        # all | headlines | news | tech | world | lifestyle | cat:<scope>:<name>
    parent: str                        # the part of the feed shown: all, one section, or headlines (Local + World)
    tab: str                           # the dock tab that is lit: all | tech | headlines | lifestyle
    profile: bool
    calibration: bool
    unseen: int
    unread: int
    read_count: int
    dropped: int                       # skipped too often, below threshold or over a cap


def load_weights(conn: sqlite3.Connection) -> dict[tuple[str, str], float]:
    return {(r["kind"], r["key"]): r["weight"] for r in conn.execute("SELECT kind, key, weight FROM weights")}


def adjust_weight(conn: sqlite3.Connection, kind: str, key: str, factor: float) -> float:
    current = load_weights(conn).get((kind, key), 1.0)
    new = round(min(WEIGHT_MAX, max(WEIGHT_MIN, current * factor)), 3)
    conn.execute("INSERT INTO weights (kind, key, weight, updated_at) VALUES (?, ?, ?, ?) "
                 "ON CONFLICT(kind, key) DO UPDATE SET weight = excluded.weight, updated_at = excluded.updated_at",
                 (kind, key, new, iso(utcnow())))
    return new


# Implicit signals (opening, liking) can lift an outlet or category this far at most; explicit
# "More about …" in the card menu can go up to WEIGHT_MAX
IMPLICIT_MAX = 1.5


def weight_strength(weight: float) -> tuple[str, int]:
    """('more' | 'less', level) for a learned weight, so pages need no maths. Level 0 is a nudge
    within 5% of normal (one open or like); 1 a little, 2 clearly, 3 as far as it goes."""
    if weight >= 1:
        return "more", 0 if weight < 1.05 else 1 if weight < 1.2 else 2 if weight < 1.5 else 3
    return "less", 0 if weight > 0.95 else 1 if weight > 0.8 else 2 if weight > 0.5 else 3
OPEN_NUDGE = {"source": 1.03, "category": 1.02}
LIKE_NUDGE = {"source": 1.06, "category": 1.04}


def nudge(conn: sqlite3.Connection, source_id: str, category: str | None, factors: dict[str, float],
          undo: bool = False) -> None:
    """Small automatic weight change from opening or liking a story. Never pushes above IMPLICIT_MAX
    and never touches a weight that is already above it (set explicitly)."""
    weights = load_weights(conn)
    for kind, key in (("source", source_id), ("category", category)):
        if not key:
            continue
        current = weights.get((kind, key), 1.0)
        factor = factors[kind]
        if undo:
            new = max(1.0, current / factor) if current > 1.0 else current
        elif current >= IMPLICIT_MAX:
            continue
        else:
            new = min(IMPLICIT_MAX, current * factor)
        if new != current:
            conn.execute("INSERT INTO weights (kind, key, weight, updated_at) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(kind, key) DO UPDATE SET weight = excluded.weight, updated_at = excluded.updated_at",
                         (kind, key, round(new, 4), iso(utcnow())))


def story_read_at(card: Card) -> datetime | None:
    times = [parse_iso(m.row["last_read_at"]) for m in [card.lead, *card.others] if m.row["last_read_at"]]
    return max(times) if times else None


def story_saved_at(card: Card) -> datetime | None:
    times = [parse_iso(m.row["saved_at"]) for m in [card.lead, *card.others] if m.row["saved_at"]]
    return max(times) if times else None


def _joined_since(card: Card, read_at: datetime) -> list[Member]:
    """The first article of each outlet that joined the story after it was read."""
    members = [card.lead, *card.others]
    before = {m.row["source_id"] for m in members if parse_iso(m.row["fetched_at"]) <= read_at}
    joined: dict = {}
    for m in sorted(members, key=lambda m: m.row["fetched_at"]):
        sid = m.row["source_id"]
        if sid not in before and sid not in joined and parse_iso(m.row["fetched_at"]) > read_at:
            joined[sid] = m
    return list(joined.values())


# A read story comes back as Updated when this many new outlets joined it after it was read: fewer
# for top stories, where follow-ups matter. Otherwise it is mostly the same news reaching more
# outlets, so it also needs one of them in the last UPDATE_FRESH_HOURS. Watch hits get no lower bar:
# a broad watch (your city) matches much of the local news.
UPDATE_OUTLETS_KEY, UPDATE_OUTLETS = 2, 3
UPDATE_FRESH_HOURS = 6


def resurfaced(card: Card, read_at: datetime, key: bool, now: datetime) -> list[Member]:
    """First article of each outlet that brings a read story back (oldest first), or [] if it stays read."""
    joined = _joined_since(card, read_at)
    if key:
        back = len(joined) >= UPDATE_OUTLETS_KEY
    else:
        fresh = now - timedelta(hours=UPDATE_FRESH_HOURS)
        back = len(joined) >= UPDATE_OUTLETS and any(parse_iso(m.row["fetched_at"]) >= fresh for m in joined)
    return joined if back else []


def _category(card: Card) -> str:
    return card.lead.row["ai_category"] or "Unscored"


def _diversify(items: list[FeedItem], lookahead: int = 6) -> list[FeedItem]:
    """No three in a row from the same category or the same outlet, when an alternative is close by."""
    pending = list(items)
    out: list[FeedItem] = []
    while pending:
        pick = 0
        if len(out) >= 2:
            last_cats = {it.category for it in out[-2:]}
            last_srcs = {it.card.lead.row["source_id"] for it in out[-2:]}
            same_cat = len(last_cats) == 1
            same_src = len(last_srcs) == 1
            for i, cand in enumerate(pending[:lookahead]):
                clash = (same_cat and cand.category in last_cats) or (same_src and cand.card.lead.row["source_id"] in last_srcs)
                if not clash:
                    pick = i
                    break
        out.append(pending.pop(pick))
    return out


def _affinity(conn: sqlite3.Connection) -> Counter:
    """How much I engage with each category: opened (tapped, not auto-read) or 👍 in the last 30 days."""
    since = iso(utcnow() - timedelta(days=30))
    rows = conn.execute("SELECT ai_category, COUNT(*) AS n FROM items WHERE ai_category IS NOT NULL AND "
                        "(opened_at >= ? OR (rating = 1 AND rated_at >= ?)) GROUP BY ai_category", (since, since))
    return Counter({r["ai_category"]: r["n"] for r in rows})


# A new source is on trial until this many of its stories have been on screen
TRIAL_IMPRESSIONS = 10


def _new_sources(conn: sqlite3.Connection, days: int, skip: set[str], now: datetime) -> set[str]:
    """Sources still on trial: first story in the last `days` days and hardly any of them seen yet.
    Not discovered ones (they have their own trial)."""
    since = iso(now - timedelta(days=days))
    rows = conn.execute("SELECT source_id FROM items GROUP BY source_id "
                        "HAVING MIN(fetched_at) >= ? AND SUM(seen_count > 0) < ?", (since, TRIAL_IMPRESSIONS))
    return {r["source_id"] for r in rows} - skip


def _turned_down(conn: sqlite3.Connection) -> set[str]:
    """Categories with more 👎 than 👍 in the last 30 days: exploring does not push them."""
    since = iso(utcnow() - timedelta(days=30))
    rows = conn.execute("SELECT ai_category FROM items WHERE ai_category IS NOT NULL AND rated_at >= ? "
                        "GROUP BY ai_category HAVING SUM(rating < 0) > SUM(rating > 0)", (since,))
    return {r["ai_category"] for r in rows}


def _widen(pool: list[FeedItem], capped: list[FeedItem], new_ids: set[str], turned_down: set[str],
           weights: dict, f) -> list[FeedItem]:
    """Stories below the threshold that get a slot anyway, alternating: the best few from each new
    source (whatever they score), and one per category the feed rarely shows (fewest stories in it
    first, at least explore_min_final, not a category I keep turning down). Outlets and categories
    turned down in the card menu ("less from …") are left out of both."""
    def wanted(it: FeedItem) -> bool:
        return (weights.get(("source", it.card.lead.row["source_id"]), 1.0) >= 1.0
                and weights.get(("category", it.category), 1.0) >= 1.0)

    trial: list[FeedItem] = []
    per_source: Counter = Counter()
    for it in pool:
        sid = it.card.lead.row["source_id"]
        if sid in new_ids and per_source[sid] < f.new_source_stories and wanted(it):
            per_source[sid] += 1
            it.extra = "new source"
            trial.append(it)
    explore: list[FeedItem] = []
    if f.explore_every > 0:
        shown = Counter(it.category for it in capped)
        slots = max(1, len(capped) // f.explore_every)
        # Rarely shown: under half of an even share of the feed
        rare = len(capped) / (2 * max(1, len({it.category for it in capped + pool})))
        best: dict[str, FeedItem] = {}
        for it in pool:   # by rank, so the first of each category is its best
            if (not it.extra and it.category not in best and it.category not in turned_down and shown[it.category] < rare
                    and it.category != "Unscored" and it.card.final >= f.explore_min_final and wanted(it)):
                best[it.category] = it
        explore = sorted(best.values(), key=lambda it: (shown[it.category], -it.rank))[:slots]
        for it in explore:
            it.extra = "exploring"
    return [it for pair in zip_longest(explore, trial) for it in pair if it]


def _weave(stream: list[FeedItem], extras: list[FeedItem], every: int) -> list[FeedItem]:
    """Spread the extra stories through the stream: the first after a few cards, then one every `every`."""
    out = list(stream)
    pos = 3
    for it in extras:
        out.insert(min(pos, len(out)), it)
        pos += max(every, 4) + 1
    return out


def build_feed(conn: sqlite3.Connection, cfg: Config, sources: list[Source], active: str = "all",
               now: datetime | None = None) -> Feed:
    now = now or utcnow()
    f = cfg.feed
    names = {s.id: s.name for s in sources}
    caps = {s.id: s.daily_cap for s in sources}
    discovered_ids = {s.id for s in sources if s.params.get("discovered")}
    cutoff = iso(now - timedelta(hours=f.window_hours))
    # Every member of every story that has an item inside the window
    rows = conn.execute(
        """SELECT i.*, s.source_count FROM items i JOIN stories s ON s.id = i.story_id
           WHERE i.story_id IN (SELECT story_id FROM items WHERE fetched_at >= ?)
              OR i.event IN (SELECT event FROM items WHERE fetched_at >= ? AND event IS NOT NULL)
           ORDER BY i.id""", (cutoff, cutoff)).fetchall()
    cards = make_cards(rows, cfg, names)
    weights = load_weights(conn)
    profile = profile_exists()
    calibration = cfg.scoring.calibration_mode
    top_at = cfg.scoring.top_story_importance
    new_ids = _new_sources(conn, f.new_source_days, discovered_ids, now)

    candidates: list[FeedItem] = []
    pool: list[FeedItem] = []   # below digest.min_final
    read_count = dropped = 0
    for card in cards:
        members = [card.lead, *card.others]
        if card.muted or any(m.row["hidden_at"] for m in members):
            continue
        if card.lead.row["category"] not in SECTIONS:
            continue
        top = card.lead.row["importance"] is not None and card.importance >= top_at
        updated: list[Member] = []
        read_at = story_read_at(card)
        if read_at:
            updated = resurfaced(card, read_at, top, now)
            if not updated:
                read_count += 1
                continue
            card.reopened = True
        final = card.final
        discovered = card.lead.row["source_id"] in discovered_ids
        # Discovered stories must earn their place, even in calibration mode; top stories always pass
        if discovered and not top and (final is None or final < cfg.discovery.min_final):
            dropped += 1
            continue
        # Below the threshold: kept apart for the new-source and exploration slots. Calibration mode
        # still shows the rest of them, at the bottom of the stream.
        below = (profile and not top and not card.must_show and final is not None
                 and final < cfg.digest.min_final)
        if below and not calibration and final < f.explore_min_final and card.lead.row["source_id"] not in new_ids:
            dropped += 1
            continue
        impressions = max(m.row["seen_count"] or 0 for m in members)
        if impressions >= f.seen_drop_after and not top and not card.must_show and not updated and not calibration:
            dropped += 1
            continue

        published = card.newest or card.published or now  # a developing story ranks by its latest article
        age_h = max(0.0, (now - published).total_seconds() / 3600)
        freshness = f.freshness_floor + (1 - f.freshness_floor) * 0.5 ** (age_h / f.half_life_hours)
        base = final if final is not None else 5.0
        rank = base * freshness
        if top:
            rank *= 1.3
        if card.must_show:
            rank *= 1.15
        if card.unseen:
            rank *= 1.1
        if updated:
            rank *= 1.2
        rank *= 0.85 ** impressions
        rank *= weights.get(("source", card.lead.row["source_id"]), 1.0)
        rank *= weights.get(("category", _category(card)), 1.0)
        (pool if below else candidates).append(
            FeedItem(card, round(rank, 3), block_of(card.lead.row["category"], card.lead.row["ai_category"]),
                     _category(card), top, updated,
                     discovered=discovered))

    candidates.sort(key=lambda it: it.rank, reverse=True)
    pool.sort(key=lambda it: it.rank, reverse=True)
    # Per-source daily caps (tech is high-volume); top and watched stories are exempt
    shown: Counter = Counter()
    capped: list[FeedItem] = []

    def cap_into(items: list[FeedItem]) -> int:
        n = 0
        for it in items:
            sid = it.card.lead.row["source_id"]
            cap = caps.get(sid)
            if cap is not None and not it.top and not it.card.must_show and shown[sid] >= cap:
                n += 1
                continue
            shown[sid] += 1
            capped.append(it)
        return n

    dropped += cap_into(candidates)
    extras = _widen(pool, capped, new_ids, _turned_down(conn), weights, f)
    rest = [it for it in pool if not it.extra]
    dropped += cap_into(rest) if calibration else len(rest)

    # The dock: For you / Tech / News (Local + World) / Lifestyle. Inside a tab, one row of chips:
    # the parts of it (All, Local, World on News) and then its categories.
    chips = [Chip("all", TABS["all"], len(capped))]
    chips += [Chip(key, TABS[key], sum(1 for it in capped if it.block in scope_blocks(key)))
              for key in ("tech", HEADLINES, "lifestyle")]
    affinity = _affinity(conn)
    blocks_of: dict[str, Counter] = defaultdict(Counter)
    for it in capped:
        blocks_of[it.category][it.block] += 1

    def in_scope(scope: str) -> list[FeedItem]:
        return [it for it in capped if it.block in scope_blocks(scope)]

    # A category chip is scoped to where it was tapped (cat:news:AI, cat:headlines:AI): the same category
    # can show up under Local and Tech, and tapping it under Local must not jump to the Tech stories.
    # A bare cat:<name> (an older filter cookie) goes to the section where the category is most common.
    category = None
    if active.startswith("cat:"):
        scope, _, name = active[4:].partition(":")
        if (scope in SECTIONS or scope == HEADLINES) and name:
            parent = scope
            if sum(blocks_of[name][b] for b in scope_blocks(scope)):
                category = name
            else:
                active = scope   # nothing left in that category: the part's All
        elif active[4:] in blocks_of:
            category = active[4:]
            parent = blocks_of[category].most_common(1)[0][0]
            active = f"cat:{parent}:{category}"
        else:
            active = parent = "all"
    elif active == "all" or active in SECTIONS or active == HEADLINES:
        parent = active
    else:
        active = parent = "all"
    tab = tab_of(parent)
    subchips: list[Chip] = []
    if parent != "all":
        shown_here = in_scope(parent)
        if tab == HEADLINES:
            subchips = [Chip(HEADLINES, "All", len(in_scope(HEADLINES)), scope=True),
                        Chip("news", LABELS["news"], len(in_scope("news")), scope=True),
                        Chip("world", LABELS["world"], len(in_scope("world")), scope=True)]
        else:
            subchips = [Chip(parent, "All", len(shown_here), scope=True)]
        counts = Counter(it.category for it in shown_here if it.category != "Unscored")
        ranked = sorted(counts.items(), key=lambda kv: (-affinity[kv[0]], -kv[1], kv[0]))
        # All + the four categories you read most are shown; the rest, except the selected one, fold behind More
        subchips += [Chip(f"cat:{parent}:{c}", c, n, fold=i >= 4 and f"cat:{parent}:{c}" != active)
                     for i, (c, n) in enumerate(ranked)]

    if category:
        selected = [it for it in in_scope(parent) if it.category == category]
    elif parent != "all":
        selected = in_scope(parent)
    else:
        selected = capped
    stream = _diversify(selected) if not category else selected
    # The extra stories join the For you and section streams, not a single category's
    if not category:
        stream = _weave(stream, [it for it in extras if parent == "all" or it.block in scope_blocks(parent)],
                        f.explore_every)
    if stream:
        stream[0].first = True
    # Every story with a photo gets the large photo card
    for it in stream:
        if it.card.image and not it.updated:
            it.big = True

    return Feed(items=stream, chips=chips, subchips=subchips, active=active, parent=parent, tab=tab, profile=profile,
                calibration=calibration, unseen=sum(1 for it in capped if it.card.unseen),
                unread=len(capped), read_count=read_count, dropped=dropped)


def read_stories(conn: sqlite3.Connection, cfg: Config, sources: list[Source], now: datetime | None = None) -> list[tuple[Card, datetime]]:
    """Stories read inside the feed window, most recently read first."""
    now = now or utcnow()
    cutoff = iso(now - timedelta(hours=cfg.feed.window_hours))
    rows = conn.execute(
        """SELECT i.*, s.source_count FROM items i JOIN stories s ON s.id = i.story_id
           WHERE i.story_id IN (SELECT story_id FROM items WHERE last_read_at >= ?)
              OR i.event IN (SELECT event FROM items WHERE last_read_at >= ? AND event IS NOT NULL)
           ORDER BY i.id""", (cutoff, cutoff)).fetchall()
    out = [(c, story_read_at(c)) for c in make_cards(rows, cfg, {s.id: s.name for s in sources})]
    return sorted([(c, t) for c, t in out if t], key=lambda ct: ct[1], reverse=True)


def saved_stories(conn: sqlite3.Connection, cfg: Config, sources: list[Source]) -> list[tuple[Card, datetime]]:
    """Every saved story, most recently saved first. No time window: saved stories stay until removed."""
    rows = conn.execute(
        """SELECT i.*, s.source_count FROM items i JOIN stories s ON s.id = i.story_id
           WHERE i.story_id IN (SELECT story_id FROM items WHERE saved_at IS NOT NULL)
              OR i.event IN (SELECT event FROM items WHERE saved_at IS NOT NULL AND event IS NOT NULL)
           ORDER BY i.id""").fetchall()
    out = [(c, story_saved_at(c)) for c in make_cards(rows, cfg, {s.id: s.name for s in sources})]
    return sorted([(c, t) for c, t in out if t], key=lambda ct: ct[1], reverse=True)


SEARCH_DAYS = 14
SEARCH_LIMIT = 50


def search_query(q: str) -> str | None:
    """The reader's words as an FTS5 query: every word must match, whole words only, except the last
    one, which is a prefix so results show up while typing. Each word is quoted, so FTS syntax
    (quotes, *, AND, NEAR, columns) typed by the reader is just text."""
    words = [w.replace('"', "") for w in q.split()]
    words = [w for w in words if w.strip("*")]
    if not words:
        return None
    terms = [f'"{w.strip("*")}"' for w in words]
    terms[-1] += "*"
    return " ".join(terms)


def search_stories(conn: sqlite3.Connection, cfg: Config, sources: list[Source], q: str,
                   days: int | None = SEARCH_DAYS, now: datetime | None = None) -> tuple[list[Card], bool]:
    """Stories with an article matching every word of `q` (title, snippet, key points), newest first.
    `days` limits the search to recently fetched articles (None: everything). Hidden stories and muted
    sources are included; the page marks them. Returns (cards, whether older articles also match)."""
    match = search_query(q)
    if match is None:
        return [], False
    cutoff = iso((now or utcnow()) - timedelta(days=days)) if days else None
    hits = conn.execute(
        """SELECT i.id, i.story_id, i.event FROM items_fts f JOIN items i ON i.id = f.rowid
           WHERE items_fts MATCH ? AND (? IS NULL OR i.fetched_at >= ?)
           ORDER BY i.fetched_at DESC LIMIT ?""", (match, cutoff, cutoff, SEARCH_LIMIT * 6)).fetchall()
    older = cutoff is not None and conn.execute(
        """SELECT EXISTS (SELECT 1 FROM items_fts f JOIN items i ON i.id = f.rowid
                          WHERE items_fts MATCH ? AND i.fetched_at < ?)""", (match, cutoff)).fetchone()[0] == 1
    if not hits:
        return [], older
    story_ids = sorted({h["story_id"] for h in hits if h["story_id"] is not None})
    events = sorted({h["event"] for h in hits if h["event"] is not None})
    item_ids = [h["id"] for h in hits]
    rows = conn.execute(
        f"""SELECT i.*, s.source_count FROM items i JOIN stories s ON s.id = i.story_id
            WHERE i.id IN ({','.join('?' * len(item_ids))})
               OR i.story_id IN ({','.join('?' * len(story_ids)) or 'NULL'})
               OR i.event IN ({','.join('?' * len(events)) or 'NULL'})
            ORDER BY i.id""", (*item_ids, *story_ids, *events)).fetchall()
    cards = make_cards(rows, cfg, {s.id: s.name for s in sources})
    cards.sort(key=lambda c: c.newest or c.published, reverse=True)
    return cards[:SEARCH_LIMIT], older


def story_hidden(card: Card) -> bool:
    return card.muted or any(m.row["hidden_at"] for m in [card.lead, *card.others])


# --- snapshots: the ranked list is frozen per page load, so "load more" never repeats or skips stories ---
_SNAPSHOT_TTL = 1800
_SNAPSHOT_MAX = 16
_snapshots: "OrderedDict[str, tuple[float, Feed]]" = OrderedDict()
_snapshot_lock = threading.Lock()


def remember(feed: Feed) -> str:
    token = secrets.token_urlsafe(9)
    now = time.monotonic()
    with _snapshot_lock:
        _snapshots[token] = (now, feed)
        while len(_snapshots) > _SNAPSHOT_MAX:
            _snapshots.popitem(last=False)
    return token


def recall(token: str) -> Feed | None:
    with _snapshot_lock:
        entry = _snapshots.get(token)
        if not entry or time.monotonic() - entry[0] > _SNAPSHOT_TTL:
            _snapshots.pop(token, None)
            return None
        return entry[1]
