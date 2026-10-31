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
    category: str | None = None  # overrides the pack's section, e.g. a country's tech sites
    https_links: bool = False    # the feed publishes http:// article links that work over https


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


@dataclass(frozen=True)
class LocalPack:
    """Local news for one country: added to the Local section, with the region it suggests."""
    code: str       # "si"
    country: str    # "Slovenia", matched case-insensitively against region.country
    timezone: str   # suggested time zone
    tld: str        # feeds on this domain count as Local; "" when the country has no common one
    feeds: list[PackFeed]
    aliases: tuple[str, ...] = ()  # other names people type for the country, lowercase


SLOVENIA = LocalPack("si", "Slovenia", "Europe/Ljubljana", ".si", [
    # Many outlets covering the same story is what makes a story "big" (cross-source boost).
    # No RSS found for Finance, Primorske novice, STA, Svet24.
    PackFeed("RTV SLO", "https://img.rtvslo.si/feeds/00.xml"),
    PackFeed("24ur", "https://www.24ur.com/rss"),
    PackFeed("Delo", "https://www.delo.si/rss"),
    PackFeed("Dnevnik", "https://www.dnevnik.si/rss.xml"),
    PackFeed("Večer", "https://vecer.com/feed/allarticles"),
    PackFeed("N1 Slovenija", "https://n1info.si/feed/"),
    PackFeed("Siol", "https://siol.net/feeds/latest"),
    PackFeed("Žurnal24", "https://www.zurnal24.si/feeds/latest"),
    PackFeed("Nova24TV", "https://nova24tv.si/feed/"),
    PackFeed("Mladina", "https://www.mladina.si/media/rss/rss-fb-mladina.xml"),
    # Slovenian tech
    PackFeed("Slo-Tech", "https://static.slo-tech.com/slotech.xml", category="tech", https_links=True),
    PackFeed("Računalniške novice", "https://racunalniske-novice.com/feed/", category="tech"),
    PackFeed("Monitor", "https://www.monitor.si/rss.php", category="tech"),
], aliases=("slovenija",))

# Checked 2026-10-31. National outlets across the spectrum; feeds posting 40+ stories a day are capped.
# DW's German feed was nearly empty.
GERMANY = LocalPack("de", "Germany", "Europe/Berlin", ".de", [
    PackFeed("Tagesschau", "https://www.tagesschau.de/index~rss2.xml"),
    PackFeed("ZDFheute", "https://www.zdf.de/rss/zdf/nachrichten"),
    PackFeed("Der Spiegel", "https://www.spiegel.de/schlagzeilen/index.rss"),
    PackFeed("Die Zeit", "https://newsfeed.zeit.de/index"),
    PackFeed("FAZ", "https://www.faz.net/rss/aktuell/"),
    PackFeed("Süddeutsche Zeitung", "https://rss.sueddeutsche.de/rss/Topthemen"),
    PackFeed("Die Welt", "https://www.welt.de/feeds/latest.rss"),
    PackFeed("n-tv", "https://www.n-tv.de/rss", 20),
    PackFeed("taz", "https://taz.de/!p4608;rss/"),
    PackFeed("Stern", "https://www.stern.de/feed/standard/alle-nachrichten/"),
    # German tech
    PackFeed("heise online", "https://www.heise.de/rss/heise-atom.xml", 10, category="tech"),
    PackFeed("Golem", "https://rss.golem.de/rss.php?feed=RSS2.0", category="tech"),
    PackFeed("t3n", "https://t3n.de/rss.xml", category="tech"),
], aliases=("deutschland",))

# Checked 2026-10-31. AP (403) and the Washington Post (502) refused the check; CBS and CNET feeds were nearly empty.
UNITED_STATES = LocalPack("us", "United States", "America/New_York", "", [
    PackFeed("NPR News", "https://feeds.npr.org/1001/rss.xml"),
    PackFeed("New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/US.xml"),
    PackFeed("Fox News", "https://moxie.foxnews.com/google-publisher/us.xml"),
    PackFeed("ABC News", "https://abcnews.go.com/abcnews/usheadlines"),
    PackFeed("NBC News", "https://feeds.nbcnews.com/nbcnews/public/news"),
    PackFeed("PBS NewsHour", "https://www.pbs.org/newshour/feeds/rss/headlines"),
    PackFeed("The Hill", "https://thehill.com/news/feed/"),
    PackFeed("Politico", "https://rss.politico.com/politics-news.xml"),
    # US tech (The Verge, Ars Technica and TechCrunch are in the starter config already)
    PackFeed("Wired", "https://www.wired.com/feed/rss", category="tech"),
], aliases=("usa", "united states of america", "america"))

# Checked 2026-10-31. The Telegraph's feed is paywalled (HTTP 402).
UNITED_KINGDOM = LocalPack("uk", "United Kingdom", "Europe/London", ".uk", [
    PackFeed("BBC News UK", "https://feeds.bbci.co.uk/news/uk/rss.xml"),
    PackFeed("BBC Politics", "https://feeds.bbci.co.uk/news/politics/rss.xml"),
    PackFeed("Guardian UK", "https://www.theguardian.com/uk-news/rss"),
    PackFeed("The Independent", "https://www.independent.co.uk/news/uk/rss", 20),
    PackFeed("Sky News", "https://feeds.skynews.com/feeds/rss/uk.xml"),
    PackFeed("Daily Mail", "https://www.dailymail.co.uk/news/index.rss"),
    PackFeed("Evening Standard", "https://www.standard.co.uk/news/uk/rss"),
    PackFeed("The i Paper", "https://inews.co.uk/news/feed"),
    PackFeed("Daily Mirror", "https://www.mirror.co.uk/news/uk-news/?service=rss"),
    PackFeed("GB News", "https://www.gbnews.com/feeds/news.rss"),
    # UK tech
    PackFeed("The Register", "https://www.theregister.com/headlines.atom", category="tech"),
    PackFeed("TechRadar", "https://www.techradar.com/rss", 10, category="tech"),
], aliases=("gb", "great britain", "britain", "england", "scotland", "wales", "northern ireland"))

LOCAL_PACKS: dict[str, LocalPack] = {p.code: p for p in (SLOVENIA, GERMANY, UNITED_STATES, UNITED_KINGDOM)}


def pack_for(country: str) -> LocalPack | None:
    """The local pack for a country, by name ("Slovenia"), code ("si") or another name ("Slovenija"), ignoring case."""
    key = country.strip().casefold()
    if not key:
        return None
    return next((p for p in LOCAL_PACKS.values() if key in (p.code, p.country.casefold(), *p.aliases)), None)


def missing_from(pack: list[PackFeed], feeds: list) -> list[PackFeed]:
    """Pack feeds not yet among the configured feeds (matched by normalised URL)."""
    have = {normalize_url(f["url"] if isinstance(f, dict) else f.url) for f in feeds}
    return [p for p in pack if normalize_url(p.url) not in have]


def add_pack(feeds: list, pack: list[PackFeed], category: str) -> tuple[int, int]:
    """Add the pack's missing feeds to the config's feed list and move the ones already there into the
    pack's section (a feed's own category wins over `category`). Returns (added, moved)."""
    section = {normalize_url(p.url): p.category or category for p in pack}
    moved = 0
    for f in feeds:
        want = section.get(normalize_url(f["url"]))
        if want and f.get("category") != want:
            f["category"] = want
            moved += 1
    ids = {f.get("id") for f in feeds}
    added = 0
    for p in missing_from(pack, feeds):
        fid = base = slugify(p.name)
        n = 2
        while fid in ids:
            fid, n = f"{base}-{n}", n + 1
        ids.add(fid)
        entry = {"id": fid, "name": p.name, "url": p.url, "category": p.category or category}
        if p.daily_cap:
            entry["daily_cap"] = p.daily_cap
        if p.https_links:
            entry["https_links"] = True
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
