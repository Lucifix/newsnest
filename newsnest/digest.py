"""Builds a day's digest: story cards grouped as Top, then each section by category."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from newsnest.config import WORLD, Config, Source
from newsnest.db import loads, parse_iso
from newsnest.levels import NO_LEVELS, Levels, load_levels
from newsnest.profile import profile_exists
from newsnest.scoring import final_score
from newsnest.sections import block_of, section_keys


@dataclass
class Member:
    row: sqlite3.Row
    final: float | None
    source_name: str

    @property
    def id(self) -> int:
        return self.row["id"]

    @property
    def paid(self) -> bool:
        """The publisher marks the article as for subscribers (found when its page was loaded for key points)."""
        return (loads(self.row["extra"], {}) or {}).get("paid") is True


@dataclass
class Card:
    """One story: the best item, plus the other outlets that covered it."""
    lead: Member
    others: list[Member]
    unseen: bool
    must_show: bool
    muted: bool
    watches: list[str]
    reopened: bool = False  # read before, back in the feed as Updated: shown and handled as unread
    interest: str | None = None     # the Want line it matches (normalized), when levels were given
    shift: float = 0.0              # what the reader's level for that interest added to every article's score
    interest_label: str | None = None

    @property
    def final(self) -> float | None:
        return self.lead.final

    @property
    def importance(self) -> int:
        return self.lead.row["importance"] or 0

    @property
    def articles(self) -> int:
        return 1 + len(self.others)

    @property
    def outlets(self) -> int:
        return len({m.row["source_id"] for m in [self.lead, *self.others]})

    @property
    def timeline(self) -> list[Member]:
        """The other articles of the story or event, newest first."""
        return sorted(self.others, key=lambda m: m.row["published"] or m.row["fetched_at"], reverse=True)

    @property
    def other_outlets(self) -> list[Member]:
        """The newest article of each other outlet, newest first."""
        seen: dict[str, Member] = {}
        for m in self.timeline:
            if m.source_name != self.lead.source_name and m.source_name not in seen:
                seen[m.source_name] = m
        return list(seen.values())

    @property
    def newest(self) -> datetime | None:
        times = [parse_iso(m.row["published"] or m.row["fetched_at"]) for m in [self.lead, *self.others]]
        return max(times) if times else None

    @property
    def ids(self) -> str:
        """All item ids of the story, so seen/read applies to every outlet's version at once."""
        return ",".join(str(m.id) for m in [self.lead, *self.others])

    @property
    def read(self) -> bool:
        return not self.reopened and any(m.row["read_at"] for m in [self.lead, *self.others])

    @property
    def saved(self) -> bool:
        return any(m.row["saved_at"] for m in [self.lead, *self.others])

    @property
    def summary_member(self) -> Member | None:
        for m in [self.lead, *self.others]:
            if m.row["summary_status"] == "done":
                return m
        return None

    @property
    def can_summarize(self) -> bool:
        """No summary yet and the lead was not already found to have no text: the card offers the button."""
        return self.summary_member is None and self.lead.row["summary_status"] != "no_text"

    @property
    def published(self) -> datetime | None:
        return parse_iso(self.lead.row["published"]) or parse_iso(self.lead.row["fetched_at"])

    @property
    def image(self) -> str | None:
        """The lead's picture, or any other outlet's picture of the same story."""
        for m in [self.lead, *self.others]:
            image = (loads(m.row["extra"], {}) or {}).get("image")
            if image:
                return image
        return None

    @property
    def logo(self) -> str | None:
        return (loads(self.lead.row["extra"], {}) or {}).get("logo")


@dataclass
class Section:
    title: str
    cards: list[Card] = field(default_factory=list)
    hidden_low: int = 0
    hidden_cap: int = 0


@dataclass
class Digest:
    day: date
    profile: bool
    calibration: bool
    top: list[Card]
    blocks: dict[str, list[Section]]  # per section key (news is Local), in the reader's order
    by_source: list[Section]        # used before a profile exists
    muted: list[Card]
    unseen: int
    unread: int
    total: int


def day_bounds(day: date, tz: ZoneInfo) -> tuple[str, str]:
    start = datetime.combine(day, time.min, tz)
    end = start + timedelta(days=1)
    from newsnest.db import iso
    return iso(start), iso(end)


def make_cards(rows: list[sqlite3.Row], cfg: Config, names: dict[str, str], levels: Levels = NO_LEVELS) -> list[Card]:
    """Group item rows into cards. Items belong together when the headline matcher put them in one
    story or Claude gave them the same event label; either link is enough. The best-scoring item
    leads, and the cross-outlet bonus counts the outlets of the whole group. With the reader's levels,
    the card's interest decides one shift for all of its articles (newsnest.levels), so the lead stays."""
    cards: list[Card] = []
    for rows_in in group_rows(rows):
        ranked = rank_members(rows_in, cfg)
        interest, shift = levels.of_card([r for r, _ in ranked])
        members = [Member(r, None if final is None else round(final + shift, 1), names.get(r["source_id"], r["source_id"]))
                   for r, final in ranked]
        lead, others = members[0], members[1:]
        watches = sorted({w for m in members for w in loads(m.row["watches"], [])})
        cards.append(Card(
            lead=lead, others=others,
            # New = no version of the story has been on screen yet
            unseen=not any(m.row["seen_at"] or m.row["read_at"] for m in members),
            must_show=any(m.row["prefilter"] == "must_show" for m in members),
            muted=all(m.row["prefilter"] == "muted" for m in members),
            watches=watches, interest=interest, shift=shift, interest_label=levels.label_of(interest)))
    return cards


def rank_members(rows: list[sqlite3.Row], cfg: Config) -> list[tuple[sqlite3.Row, float | None]]:
    """A card's items with their final score, best first (newest on a tie): the first one leads. The
    cross-outlet bonus counts the outlets of the whole card. Rows need id, source_id, published and the
    sub-scores (scoring.final_score)."""
    outlets = len({r["source_id"] for r in rows})
    ranked = [(r, final_score(r, outlets, cfg)) for r in rows]
    ranked.sort(key=lambda m: (m[1] if m[1] is not None else -1, m[0]["published"] or ""), reverse=True)
    return ranked


def group_rows(rows: list[sqlite3.Row]) -> list[list[sqlite3.Row]]:
    """Item rows grouped as cards: a shared story_id or a shared event label joins them, and the links chain.
    Rows need id, story_id and event."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    first_of_story: dict[int, int] = {}
    first_of_event: dict[str, int] = {}
    for r in rows:
        parent[r["id"]] = r["id"]
    for r in rows:
        for table, key in ((first_of_story, r["story_id"]), (first_of_event, r["event"])):
            if key is None:
                continue
            if key in table:
                parent[find(r["id"])] = find(table[key])
            else:
                table[key] = r["id"]

    groups: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        groups[find(r["id"])].append(r)
    return list(groups.values())


def _sort_key(card: Card):
    return (card.final if card.final is not None else -1, card.published or datetime.min.replace(tzinfo=ZoneInfo("UTC")))


def build_digest(conn: sqlite3.Connection, cfg: Config, sources: list[Source], day: date) -> Digest:
    tz = ZoneInfo(cfg.timezone)
    start, end = day_bounds(day, tz)
    names = {s.id: s.name for s in sources}
    caps = {s.id: s.daily_cap for s in sources}
    rows = conn.execute(
        """SELECT i.*, s.source_count FROM items i JOIN stories s ON s.id = i.story_id
           WHERE i.fetched_at >= ? AND i.fetched_at < ? ORDER BY i.id""", (start, end)).fetchall()

    cards = make_cards(rows, cfg, names, load_levels(conn))
    profile = profile_exists()
    calibration = cfg.scoring.calibration_mode
    muted = [c for c in cards if c.muted]
    live = [c for c in cards if not c.muted]

    top: list[Card] = []
    groups: dict[str, dict[str, Section]] = {key: {} for key in section_keys(cfg)}
    by_source: dict[str, Section] = {}
    if not profile:
        for c in sorted(live, key=lambda c: c.published or datetime.min.replace(tzinfo=tz), reverse=True):
            by_source.setdefault(c.lead.source_name, Section(c.lead.source_name)).cards.append(c)
    else:
        per_source_shown: dict[str, int] = defaultdict(int)
        for c in sorted(live, key=_sort_key, reverse=True):
            if c.lead.row["importance"] is not None and c.importance >= cfg.scoring.top_story_importance:
                top.append(c)
                continue
            block = block_of(c.lead.row["category"], c.lead.row["ai_category"])
            group = groups[block if block in groups else WORLD]   # a section removed by hand: not Local's
            label = c.lead.row["ai_category"] or ("Unscored" if c.final is None else "Other")
            section = group.setdefault(label, Section(label))
            if not calibration and not c.must_show and c.final is not None and c.final < cfg.digest.min_final:
                section.hidden_low += 1
                continue
            cap = caps.get(c.lead.row["source_id"])
            if cap is not None and not c.must_show:
                if per_source_shown[c.lead.row["source_id"]] >= cap:
                    section.hidden_cap += 1
                    continue
                per_source_shown[c.lead.row["source_id"]] += 1
            section.cards.append(c)

    def ordered(sections: dict[str, Section]) -> list[Section]:
        order = {name: i for i, name in enumerate(cfg.scoring.categories)}
        return sorted(sections.values(), key=lambda s: (s.title == "Unscored", order.get(s.title, 999)))

    return Digest(day=day, profile=profile, calibration=calibration, top=top,
                  blocks={key: ordered(sections) for key, sections in groups.items()},
                  by_source=list(by_source.values()), muted=muted,
                  unseen=sum(1 for c in live if c.unseen), unread=sum(1 for c in live if not c.read), total=len(live))


def available_days(conn: sqlite3.Connection, cfg: Config, limit: int = 60) -> list[tuple[date, int]]:
    tz = ZoneInfo(cfg.timezone)
    counts: dict[date, int] = defaultdict(int)
    for r in conn.execute("SELECT fetched_at FROM items ORDER BY fetched_at DESC LIMIT 20000"):
        counts[parse_iso(r["fetched_at"]).astimezone(tz).date()] += 1
    return sorted(counts.items(), reverse=True)[:limit]
