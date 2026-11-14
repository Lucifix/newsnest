"""The home feed, Discover-style: one ranked stream of unread stories from a rolling window.

rank = final score x freshness x boosts x learned weights
  freshness  floor + (1 - floor) x 0.5^(age / half-life): decays, but a strong story from yesterday
             still beats a weak fresh one
  boosts     top story (importance >= threshold), watch / must-show, not seen yet
  sinking    every time a story was on screen without being opened it sinks; after
             feed.seen_drop_after impressions it drops out (top stories only sink)
  weights    per outlet and per category, set in the card menu ("less from <outlet>")
  drift      what you've read lately, per interest (or category) and outlet (newsnest.drift): up to 40%
             up or down, never lowering top stories or watches; one story per session from a faded
             interest is placed early ("revisit"). While it's active the older learned boosts are ignored
Read stories leave the feed (they are listed under "Read"), unless the story was updated
afterwards: new outlets joined it after it was read (two for a top story, otherwise
three, at least one of them in the last few hours). It is then back as unread, marked Updated.

All of this applies when a ranking is made. The home feed keeps one ranking per feed session
(newsnest.feedsession), so a story read or skipped now leaves or sinks in the next session.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import zip_longest

from newsnest.config import NEWS, WORLD, Config, Source
from newsnest.db import iso, parse_iso, utcnow
from newsnest.digest import Card, Member, make_cards
from newsnest.drift import FADED, LIFTED, Drift, current_drift
from newsnest.levels import load_levels
from newsnest.profile import profile_exists
from newsnest.sections import HEADLINES, block_of, dock_tabs, section_keys, section_labels, scope_blocks, tab_of

WEIGHT_MIN, WEIGHT_MAX = 0.2, 2.0


@dataclass
class FeedItem:
    card: Card
    rank: float
    block: str                # the section key (news is Local); a Local outlet's foreign news is world
    category: str
    top: bool
    updated: list[Member] = field(default_factory=list)   # outlets that joined after the story was read
    big: bool = False
    discovered: bool = False  # from a candidate source still on trial
    first: bool = False       # only the first story's image loads eagerly
    # "new source" | "exploring": shown below the score threshold to widen the feed; "revisit": from an
    # interest you've drifted away from, placed early (_revisit)
    extra: str = ""
    below: bool = False       # under digest.min_final; only shown as an extra or in calibration mode
    # What moved the story up or down the feed, in words, for the card's "why" panel: (+1 | -1, text)
    why: list[tuple[int, str]] = field(default_factory=list)

    @property
    def read_ids(self) -> str:
        return self.card.ids


@dataclass
class Chip:
    key: str      # all | headlines | a section key | cat:<scope>:<name>
    label: str
    count: int
    scope: bool = False   # All / Local / World / a section: picks the part of the feed, not a category
    fold: bool = False    # a long-tail category, folded behind More


@dataclass
class Feed:
    items: list[FeedItem]              # the ranked stream for the active filter
    chips: list[Chip]                  # For you, then the dock's tabs
    subchips: list[Chip]               # under a tab: All + that part's categories
    active: str                        # all | headlines | a section key | cat:<scope>:<name>
    parent: str                        # the part of the feed shown: all, one section, or headlines (Local + World)
    tab: str                           # the dock tab that is lit: all, headlines or a section that is a tab
    profile: bool
    calibration: bool
    unseen: int
    unread: int
    read_count: int
    dropped: int                       # skipped too often, below threshold or over a cap


def load_weights(conn: sqlite3.Connection, skip_old_boosts: bool = False) -> dict[tuple[str, str], float]:
    """Outlet and category weights. skip_old_boosts leaves out the small lifts older versions learned from
    opens and likes (above 1.0, at most IMPLICIT_MAX, with no "more / less" menu choice behind them): drift
    replaces them while it's active, and they come back as they were if it's turned off."""
    if not skip_old_boosts:
        return {(r["kind"], r["key"]): r["weight"] for r in conn.execute("SELECT kind, key, weight FROM weights")}
    rows = conn.execute(
        """SELECT kind, key, weight FROM weights w
           WHERE NOT (w.weight > 1.0 AND w.weight <= ? AND NOT EXISTS (
               SELECT 1 FROM feedback f WHERE f.target = w.key AND f.kind IN ('more_' || w.kind, 'less_' || w.kind)))""",
        (IMPLICIT_MAX,))
    return {(r["kind"], r["key"]): r["weight"] for r in rows}


def adjust_weight(conn: sqlite3.Connection, kind: str, key: str, factor: float) -> float:
    """A card menu choice (factor below 1: less, above: more). It builds on the reader's earlier choices, never
    on an old boost: drift ignores old boosts, so "less" applied to one (1.5 × 0.75 = 1.125) would end up a
    lift. "More" never ends below the old boost, so it still lifts while drift is off."""
    chosen = load_weights(conn, skip_old_boosts=True).get((kind, key), 1.0)
    new = chosen * factor
    if factor > 1:
        new = max(new, load_weights(conn).get((kind, key), 1.0))
    new = round(min(WEIGHT_MAX, max(WEIGHT_MIN, new)), 3)
    conn.execute("INSERT INTO weights (kind, key, weight, updated_at) VALUES (?, ?, ?, ?) "
                 "ON CONFLICT(kind, key) DO UPDATE SET weight = excluded.weight, updated_at = excluded.updated_at",
                 (kind, key, new, iso(utcnow())))
    return new


# Older versions lifted an outlet or category a little on every open and like, this far at most (explicit
# "More about …" in the card menu can go up to WEIGHT_MAX). No longer written; drift took their place
IMPLICIT_MAX = 1.5


def weight_strength(weight: float) -> tuple[str, int]:
    """('more' | 'less', level) for a learned weight, so pages need no maths. Level 0 is a nudge
    within 5% of normal (one open or like); 1 a little, 2 clearly, 3 as far as it goes."""
    if weight >= 1:
        return "more", 0 if weight < 1.05 else 1 if weight < 1.2 else 2 if weight < 1.5 else 3
    return "less", 0 if weight > 0.95 else 1 if weight > 0.8 else 2 if weight > 0.5 else 3


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


def _age(hours: float) -> str:
    return f"{max(1, round(hours * 60))} min" if hours < 1 else f"{round(hours)} h" if hours < 48 else f"{round(hours / 24)} d"


def _taste(weight: float, name: str, asked: bool = False) -> list[tuple[int, str]]:
    """An outlet/category weight, in words; small differences from 1.0 aren't worth a line. asked: only
    the card menu's choices are left (drift is active), so say so."""
    if weight >= 1.1:
        return [(1, f"You asked for more of {name}" if asked else f"You tend to like {name}")]
    if weight <= 0.9:
        return [(-1, f"You asked for less of {name}" if asked else f"You've passed on {name} before")]
    return []


def _lately(weight: float, name: str, outlet: bool = False) -> list[tuple[int, str]]:
    """A drift weight, in words (newsnest.drift)."""
    if weight >= LIFTED:
        return [(1, f"You've been reading {name} lately" if outlet else f"You've been reading about {name} lately")]
    if weight <= FADED:
        return [(-1, f"You've read less from {name} lately" if outlet else f"You've read less about {name} lately")]
    return []


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
    # Only the last `days` days are read (items_fetched; "+" keeps SQLite from walking all of
    # items_source_fetched for the GROUP BY); a source with anything older is not new (items_source_fetched)
    rows = conn.execute("SELECT source_id FROM items i WHERE fetched_at >= :since GROUP BY +source_id "
                        "HAVING SUM(seen_count > 0) < :n AND NOT EXISTS (SELECT 1 FROM items o "
                        "WHERE o.source_id = i.source_id AND o.fetched_at < :since)",
                        {"since": since, "n": TRIAL_IMPRESSIONS})
    return {r["source_id"] for r in rows} - skip


def _turned_down(conn: sqlite3.Connection) -> set[str]:
    """Categories with more 👎 than 👍 in the last 30 days: exploring does not push them."""
    since = iso(utcnow() - timedelta(days=30))
    # rating IS NOT NULL changes nothing (an unrated row counts for neither side) but lets items_rated be used
    rows = conn.execute("SELECT ai_category FROM items WHERE ai_category IS NOT NULL AND rating IS NOT NULL "
                        "AND rated_at >= ? "
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


def _revisit(capped: list[FeedItem], drift: Drift, cfg: Config) -> list[FeedItem]:
    """Mark up to drift.revisit_slots stories from interests (or categories) the reader has drifted away
    from, so view_feed places them early: without this a faded interest sits low in the feed and a return
    to it would go unnoticed. Only stories already in the feed (above the threshold), past the first
    explore_every × 2, not top stories, one per interest, and none for an interest 👎'd in the last week or
    set to Less or Much less (newsnest.levels: the reader turned it down themself).
    They stay in `capped`, so chip counts and category views don't change."""
    slots = cfg.drift.revisit_slots
    if not drift.active or slots <= 0:
        return []
    eligible = drift.faded() - drift.recent_dislikes
    if not eligible:
        return []
    picked: list[FeedItem] = []
    keys: set[str] = set()
    for it in sorted(capped[cfg.feed.explore_every * 2:], key=lambda it: it.rank, reverse=True):
        if len(picked) >= slots:
            break
        if it.top or it.extra or it.card.shift < 0:
            continue
        key = drift.key_of(it.card)
        if key in eligible and key not in keys:
            it.extra = "revisit"
            keys.add(key)
            picked.append(it)
    return picked


def _weave(stream: list[FeedItem], extras: list[FeedItem], every: int) -> list[FeedItem]:
    """Spread the extra stories through the stream: the first after a few cards, then one every `every`."""
    out = list(stream)
    pos = 3
    for it in extras:
        out.insert(min(pos, len(out)), it)
        pos += max(every, 4) + 1
    return out


@dataclass
class Ranking:
    """The ranked feed for every tab at once, before a tab is picked: what a feed session stores."""
    items: list[FeedItem]     # above the threshold, ranked, capped per source
    extras: list[FeedItem]    # below it but given a slot: new sources and exploring
    affinity: Counter         # category engagement, for the order of the category chips
    profile: bool
    calibration: bool
    read_count: int
    dropped: int
    cutoff: str               # start of the feed window, so the rows can be loaded again


def window_rows(conn: sqlite3.Connection, cutoff: str) -> list[sqlite3.Row]:
    """Every member of every story that has an item inside the window (fetched at or after cutoff)."""
    return conn.execute(
        """SELECT i.*, s.source_count FROM items i JOIN stories s ON s.id = i.story_id
           WHERE i.story_id IN (SELECT story_id FROM items WHERE fetched_at >= ?)
              OR i.event IN (SELECT event FROM items WHERE fetched_at >= ? AND event IS NOT NULL)
           ORDER BY i.id""", (cutoff, cutoff)).fetchall()


def build_feed(conn: sqlite3.Connection, cfg: Config, sources: list[Source], active: str = "all",
               now: datetime | None = None) -> Feed:
    """Rank and pick a tab in one go: a fresh ranking, as of now."""
    return view_feed(rank_feed(conn, cfg, sources, now), cfg, active)


def rank_feed(conn: sqlite3.Connection, cfg: Config, sources: list[Source], now: datetime | None = None) -> Ranking:
    now = now or utcnow()
    f = cfg.feed
    names = {s.id: s.name for s in sources}
    caps = {s.id: s.daily_cap for s in sources}
    discovered_ids = {s.id for s in sources if s.params.get("discovered")}
    cutoff = iso(now - timedelta(hours=f.window_hours))
    cards = make_cards(window_rows(conn, cutoff), cfg, names, load_levels(conn))
    drift = current_drift(conn, cfg, now)
    weights = load_weights(conn, skip_old_boosts=drift.active)
    profile = profile_exists()
    calibration = cfg.scoring.calibration_mode
    top_at = cfg.scoring.top_story_importance
    new_ids = _new_sources(conn, f.new_source_days, discovered_ids, now)
    sections = section_keys(cfg)

    candidates: list[FeedItem] = []
    pool: list[FeedItem] = []   # below digest.min_final
    read_count = dropped = 0
    for card in cards:
        members = [card.lead, *card.others]
        if card.muted or any(m.row["hidden_at"] for m in members):
            continue
        if card.lead.row["category"] not in sections:
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
        why: list[tuple[int, str]] = []
        if top:
            rank *= 1.3
            why.append((1, "Top story: big news"))
        if card.must_show:
            rank *= 1.15
            why.append((1, "Matches one of your watches"))
        if updated:
            rank *= 1.2
            why.append((1, "New coverage since you read it"))
        if card.unseen:
            rank *= 1.1
            why.append((1, "You haven't seen it yet"))
        if age_h < f.half_life_hours / 2:
            why.append((1, f"Fresh: {_age(age_h)} old"))
        elif age_h >= f.half_life_hours:
            why.append((-1, f"Older: {_age(age_h)} old"))
        rank *= 0.85 ** impressions
        if impressions:
            why.append((-1, f"Already shown to you {impressions}×"))
        source_w = weights.get(("source", card.lead.row["source_id"]), 1.0)
        category_w = weights.get(("category", _category(card)), 1.0)
        rank *= source_w * category_w
        why += _taste(source_w, card.lead.source_name, asked=drift.active)
        why += _taste(category_w, _category(card), asked=drift.active)
        if drift.active:
            # What you've read lately; big news and watches can be lifted, never lowered
            m = drift.multiplier(card)
            protected = top or card.must_show
            rank *= max(m, 1.0) if protected else m
            key = drift.key_of(card)
            lately = (_lately(drift.weight(key), drift.label(key)) if key else []) + \
                _lately(drift.source_weight(card.lead.row["source_id"]), card.lead.source_name, outlet=True)
            why += [w for w in lately if w[0] > 0] if protected else lately
        (pool if below else candidates).append(
            FeedItem(card, round(rank, 3), block_of(card.lead.row["category"], card.lead.row["ai_category"]),
                     _category(card), top, updated,
                     discovered=discovered, below=below, why=why))

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
    _revisit(capped, drift, cfg)

    return Ranking(items=capped, extras=extras, affinity=_affinity(conn), profile=profile, calibration=calibration,
                   read_count=read_count, dropped=dropped, cutoff=cutoff)


# Scores within this distance of digest.min_final are close calls: the stories the swipe deck asks about
SWIPE_BAND = 1.5


def swipe_deck(conn: sqlite3.Connection, cfg: Config, sources: list[Source], skip: set[str] = frozenset(),
               now: datetime | None = None) -> list[FeedItem]:
    """The stories NewsNest is least sure about, for the swipe deck: a rating there teaches it the most.
    In order: scores close to the cutoff (closest first, on either side, so stories the feed hides
    are asked about too), then stories from new sources, then unscored ones; newest first within each.
    Left out: read, rated, hidden or muted stories and those in `skip` (swiped up for later)."""
    now = now or utcnow()
    names = {s.id: s.name for s in sources}
    discovered_ids = {s.id for s in sources if s.params.get("discovered")}
    new_ids = _new_sources(conn, cfg.feed.new_source_days, discovered_ids, now)
    sections = section_keys(cfg)
    cutoff = cfg.digest.min_final
    cards = make_cards(window_rows(conn, iso(now - timedelta(hours=cfg.feed.window_hours))), cfg, names,
                       load_levels(conn))
    picked: list[tuple[tuple, FeedItem]] = []
    for card in cards:
        members = [card.lead, *card.others]
        if card.muted or card.lead.row["category"] not in sections or str(card.lead.id) in skip:
            continue
        if any(m.row["hidden_at"] or m.row["rating"] is not None for m in members) or story_read_at(card):
            continue
        final = card.final
        if final is not None and abs(final - cutoff) <= SWIPE_BAND:
            group, distance = 0, abs(final - cutoff)
        elif card.lead.row["source_id"] in new_ids:
            group, distance = 1, 0.0
        elif final is None:
            group, distance = 2, 0.0
        else:
            continue  # NewsNest is sure about this one either way
        published = card.newest or card.published or now
        item = FeedItem(card, 0.0, block_of(card.lead.row["category"], card.lead.row["ai_category"]),
                        _category(card), card.lead.row["importance"] is not None
                        and card.importance >= cfg.scoring.top_story_importance,
                        discovered=card.lead.row["source_id"] in discovered_ids,
                        below=final is not None and final < cutoff)
        picked.append(((group, round(distance, 1), -published.timestamp()), item))
    picked.sort(key=lambda p: p[0])
    return [it for _, it in picked]


def view_feed(ranking: Ranking, cfg: Config, active: str = "all") -> Feed:
    """One tab of a ranking: its stream, the dock's tabs and the chips under it. No database access."""
    capped, extras, affinity = ranking.items, ranking.extras, ranking.affinity
    sections = section_keys(cfg)
    f = cfg.feed
    for it in capped + extras:  # set again below for this tab's stream
        it.first = it.big = False
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
        if (scope in sections or scope == HEADLINES) and name:
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
    elif active == "all" or active in sections or active == HEADLINES:
        parent = active
    else:
        active = parent = "all"

    # The dock: For you, then the sections that are tabs (News holds Local and World). Inside a tab, one row
    # of chips: the parts of it (All, Local, World on News) and then its categories. A section that is not
    # a tab is a chip under For you; opening it lights For you.
    labels = section_labels(cfg)
    tabs = dock_tabs(cfg, tab_of(parent))
    tab_keys = {t.key for t in tabs}
    tab = tab_of(parent) if tab_of(parent) in tab_keys else "all"
    chips = [Chip("all", "For you", len(capped))] + [Chip(t.key, t.label, len(in_scope(t.key))) for t in tabs]
    loose = [Chip(k, labels[k], len(in_scope(k)), scope=True) for k in sections
             if k not in tab_keys and k not in (NEWS, WORLD) and (in_scope(k) or k == parent)]
    subchips: list[Chip] = []
    if parent == "all":
        if loose:
            subchips = [Chip("all", "All", len(capped), scope=True), *loose]
    else:
        shown_here = in_scope(parent)
        if tab == HEADLINES:
            subchips = [Chip(HEADLINES, "All", len(in_scope(HEADLINES)), scope=True),
                        Chip(NEWS, labels[NEWS], len(in_scope(NEWS)), scope=True),
                        Chip(WORLD, labels[WORLD], len(in_scope(WORLD)), scope=True)]
        elif tab == "all":
            subchips = [Chip("all", "All", len(capped), scope=True), *loose]
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
    # A revisit story (newsnest.feed._revisit) moves up front in For you and section streams; a single
    # category's stream keeps it in its place
    revisit = [] if category else [it for it in selected if it.extra == "revisit"]
    if revisit:
        selected = [it for it in selected if it.extra != "revisit"]
    stream = _diversify(selected) if not category else selected
    # The extra stories join the For you and section streams, not a single category's
    if not category:
        stream = _weave(stream, revisit + [it for it in extras if parent == "all" or it.block in scope_blocks(parent)],
                        f.explore_every)
    if stream:
        stream[0].first = True
    # Every story with a photo gets the large photo card
    for it in stream:
        if it.card.image and not it.updated:
            it.big = True

    return Feed(items=stream, chips=chips, subchips=subchips, active=active, parent=parent, tab=tab,
                profile=ranking.profile, calibration=ranking.calibration,
                unseen=sum(1 for it in capped if it.card.unseen), unread=sum(1 for it in capped if not it.card.read),
                read_count=ranking.read_count + sum(1 for it in capped if it.card.read), dropped=ranking.dropped)


TOP_STORIES = "Top stories"


@dataclass
class Package:
    """A block of the desktop front page: one story large, the rest as headlines beside it."""
    title: str               # TOP_STORIES or a category
    items: list[FeedItem]    # the large one first


@dataclass
class FrontPage:
    lead: FeedItem | None
    packages: list[Package]

    @property
    def lead_ids(self) -> set[int]:
        """The lead item id of every story on the front page; the feed lane beside it leaves these out."""
        stories = ([self.lead] if self.lead else []) + [it for p in self.packages for it in p.items]
        return {it.card.lead.id for it in stories}


def front_page(items: list[FeedItem], per_package: int = 4, max_packages: int = 4, min_package: int = 2) -> FrontPage:
    """The desktop front page beside the feed, picked from the feed's own ranked stream: the best story as
    the lead, then Top stories (the next ones by rank), then a package for each category in the order of its
    best story. Unread stories come first, so the page shows what is still new; read ones only fill up.
    Every story appears at most once on it, and the feed lane beside it leaves them out (lead_ids).
    In a package the first story with a photo is the large one. No database access."""
    pool = [it for it in items if not it.card.read] + [it for it in items if it.card.read]
    if not pool:
        return FrontPage(lead=None, packages=[])
    lead, rest = pool[0], pool[1:]

    def package(title: str, chosen: list[FeedItem]) -> Package:
        with_photo = next((it for it in chosen if it.card.image), chosen[0])
        return Package(title, [with_photo] + [it for it in chosen if it is not with_photo])

    packages: list[Package] = []
    top = rest[:per_package]
    if len(top) >= min_package:
        packages.append(package(TOP_STORIES, top))
    used = {id(it) for it in [lead, *top]}
    by_category: dict[str, list[FeedItem]] = {}
    for it in rest:  # by rank, so the categories come in the order of their best remaining story
        if id(it) not in used and it.category and it.category != "Unscored":
            by_category.setdefault(it.category, []).append(it)
    # Inside one category every story shares it: a package of that category would only repeat the feed
    if len(by_category) > 1 or (by_category and lead.category not in by_category):
        for title, members in by_category.items():
            if len(packages) >= max_packages:
                break
            if len(members) >= min_package:
                packages.append(package(title, members[:per_package]))
    return FrontPage(lead=lead, packages=packages)


def read_stories(conn: sqlite3.Connection, cfg: Config, sources: list[Source], now: datetime | None = None) -> list[tuple[Card, datetime]]:
    """Stories read inside the feed window, most recently read first."""
    now = now or utcnow()
    cutoff = iso(now - timedelta(hours=cfg.feed.window_hours))
    rows = conn.execute(
        """SELECT i.*, s.source_count FROM items i JOIN stories s ON s.id = i.story_id
           WHERE i.story_id IN (SELECT story_id FROM items WHERE last_read_at >= ?)
              OR i.event IN (SELECT event FROM items WHERE last_read_at >= ? AND event IS NOT NULL)
           ORDER BY i.id""", (cutoff, cutoff)).fetchall()
    out = [(c, story_read_at(c)) for c in make_cards(rows, cfg, {s.id: s.name for s in sources}, load_levels(conn))]
    return sorted([(c, t) for c, t in out if t], key=lambda ct: ct[1], reverse=True)


def saved_stories(conn: sqlite3.Connection, cfg: Config, sources: list[Source]) -> list[tuple[Card, datetime]]:
    """Every saved story, most recently saved first. No time window: saved stories stay until removed."""
    rows = conn.execute(
        """SELECT i.*, s.source_count FROM items i JOIN stories s ON s.id = i.story_id
           WHERE i.story_id IN (SELECT story_id FROM items WHERE saved_at IS NOT NULL)
              OR i.event IN (SELECT event FROM items WHERE saved_at IS NOT NULL AND event IS NOT NULL)
           ORDER BY i.id""").fetchall()
    out = [(c, story_saved_at(c)) for c in make_cards(rows, cfg, {s.id: s.name for s in sources}, load_levels(conn))]
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
    cards = make_cards(rows, cfg, {s.id: s.name for s in sources}, load_levels(conn))
    cards.sort(key=lambda c: c.newest or c.published, reverse=True)
    return cards[:SEARCH_LIMIT], older


def story_hidden(card: Card) -> bool:
    return card.muted or any(m.row["hidden_at"] for m in [card.lead, *card.others])
