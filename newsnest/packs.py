"""Feed packs: a curated set of regular sources for a section, added from Settings in one tap.

Every feed was checked in October 2026 (https, working, posted in the last two weeks). Busy ones get a
daily cap so a single outlet can't fill the section.
"""

from __future__ import annotations

from dataclasses import dataclass

from newsnest.config import slugify
from newsnest.textmatch import normalize_url


@dataclass(frozen=True)
class PackFeed:
    name: str
    url: str
    daily_cap: int | None = None


LIFESTYLE_PACK: list[PackFeed] = [
    # Gear, gadgets and everyday carry
    PackFeed("Uncrate", "https://feeds.feedburner.com/uncrate"),
    PackFeed("Gear Patrol", "https://www.gearpatrol.com/feed/", 8),
    PackFeed("HiConsumption", "https://hiconsumption.com/feed/", 6),
    PackFeed("Cool Material", "https://coolmaterial.com/feed/"),
    PackFeed("ThisIsWhyImBroke", "https://www.thisiswhyimbroke.com/feed/"),
    PackFeed("The Coolector", "https://www.thecoolector.com/feed/"),
    PackFeed("Man of Many", "https://manofmany.com/feed", 6),
    PackFeed("Carryology", "https://www.carryology.com/feed/"),
    PackFeed("Field Mag", "https://www.fieldmag.com/feed"),
    # Design
    PackFeed("Yanko Design", "https://www.yankodesign.com/feed/", 6),
    PackFeed("Gessato", "https://www.gessato.com/feed/"),
    # Watches
    PackFeed("Hodinkee", "https://www.hodinkee.com/articles/rss.xml"),
    PackFeed("Fratello Watches", "https://www.fratellowatches.com/feed/"),
    # Style and men's magazines
    PackFeed("GQ", "https://www.gq.com/feed/rss", 5),
    PackFeed("Esquire", "https://www.esquire.com/rss/all.xml/", 5),
    PackFeed("Dappered", "https://dappered.com/feed/"),
    PackFeed("Hypebeast", "https://hypebeast.com/feed", 5),
    # Travel
    PackFeed("GoBackpacking", "https://gobackpacking.com/feed/"),
]

# Added to interests.md with the pack, so the stories score as wanted rather than as noise
LIFESTYLE_INTEREST = ("Men's lifestyle and gear: gadgets, everyday carry, watches, cars, style, design, "
                      "food and drink, travel and outdoor gear")


def missing_from(pack: list[PackFeed], feeds: list) -> list[PackFeed]:
    """Pack feeds not yet among the configured feeds (matched by normalised URL)."""
    have = {normalize_url(f["url"] if isinstance(f, dict) else f.url) for f in feeds}
    return [p for p in pack if normalize_url(p.url) not in have]


def add_pack(feeds: list, pack: list[PackFeed], category: str) -> tuple[int, int]:
    """Add the pack's missing feeds to the config's feed list and move the ones already there into the
    pack's section. Returns (added, moved)."""
    urls = {normalize_url(p.url) for p in pack}
    moved = 0
    for f in feeds:
        if normalize_url(f["url"]) in urls and f.get("category") != category:
            f["category"] = category
            moved += 1
    ids = {f.get("id") for f in feeds}
    added = 0
    for p in missing_from(pack, feeds):
        fid = base = slugify(p.name)
        n = 2
        while fid in ids:
            fid, n = f"{base}-{n}", n + 1
        ids.add(fid)
        entry = {"id": fid, "name": p.name, "url": p.url, "category": category}
        if p.daily_cap:
            entry["daily_cap"] = p.daily_cap
        feeds.append(entry)
        added += 1
    return added, moved


def add_interest(profile: str, line: str) -> str:
    """Add a line under "## Want" in interests.md (or start that section); unchanged if it's already there."""
    if line.lower() in profile.lower():
        return profile
    bullet = f"- {line}"
    lines = profile.splitlines()
    for i, text in enumerate(lines):
        if text.strip().lower() == "## want":
            end = i + 1
            while end < len(lines) and lines[end].strip().startswith("-"):
                end += 1
            lines.insert(end, bullet)
            return "\n".join(lines) + "\n"
    return f"## Want\n{bullet}\n\n{profile}".rstrip("\n") + "\n"
