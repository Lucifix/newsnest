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

# Checked 2026-11-02. No working feed for Heute, oe24 or the Wiener Zeitung; the Salzburger Nachrichten feed is
# mostly event listings.
AUSTRIA = LocalPack("at", "Austria", "Europe/Vienna", ".at", [
    PackFeed("ORF", "https://rss.orf.at/news.xml"),
    PackFeed("Der Standard", "https://www.derstandard.at/rss", 20),
    PackFeed("Die Presse", "https://www.diepresse.com/rss"),
    PackFeed("Kurier", "https://kurier.at/xml/rss"),
    PackFeed("Kleine Zeitung", "https://www.kleinezeitung.at/rss/top"),
    PackFeed("Kronen Zeitung", "https://api.krone.at/v1/rss/rssfeed-google.xml?id=2311992", 20),
    PackFeed("profil", "https://www.profil.at/xml/rss"),
    PackFeed("Falter", "https://www.falter.at/rss"),
    # Austrian tech
    PackFeed("Futurezone", "https://futurezone.at/xml/rss", category="tech"),
], aliases=("österreich", "osterreich"))

# Checked 2026-11-02. HRT has no feed.
CROATIA = LocalPack("hr", "Croatia", "Europe/Zagreb", ".hr", [
    PackFeed("Index.hr", "https://www.index.hr/rss", 20),
    PackFeed("Jutarnji list", "https://www.jutarnji.hr/feed", 20),
    PackFeed("Večernji list", "https://www.vecernji.hr/feeds/latest"),
    PackFeed("24sata", "https://www.24sata.hr/feeds/aktualno.xml"),
    PackFeed("N1 Hrvatska", "https://n1info.hr/feed/", 20),
    PackFeed("Telegram", "https://www.telegram.hr/feed/"),
    PackFeed("Slobodna Dalmacija", "https://slobodnadalmacija.hr/feed", 20),
    PackFeed("Novi list", "https://www.novilist.hr/feed/"),
    PackFeed("Tportal", "https://www.tportal.hr/rss-najnovije.xml"),
    PackFeed("Dnevnik.hr", "https://dnevnik.hr/assets/feed/articles/"),
    PackFeed("Net.hr", "https://net.hr/najnovije/rss.xml", 20),
    # Croatian tech
    PackFeed("Bug", "https://www.bug.hr/rss", 10, category="tech"),
    PackFeed("Netokracija", "https://www.netokracija.com/feed", category="tech"),
], aliases=("hrvatska",))

# Checked 2026-11-02. Corriere della Sera's feeds stopped months ago; Il Giornale has none and Il Messaggero's
# stories have no dates.
ITALY = LocalPack("it", "Italy", "Europe/Rome", ".it", [
    PackFeed("ANSA", "https://www.ansa.it/sito/ansait_rss.xml"),
    PackFeed("la Repubblica", "https://www.repubblica.it/rss/homepage/rss2.0.xml"),
    PackFeed("La Stampa", "https://www.lastampa.it/rss/copertina.xml"),
    PackFeed("Il Sole 24 Ore", "https://www.ilsole24ore.com/rss/italia.xml"),
    PackFeed("Il Fatto Quotidiano", "https://www.ilfattoquotidiano.it/feed/"),
    PackFeed("Il Post", "https://www.ilpost.it/italia/feed/"),
    PackFeed("Rai News", "https://www.rainews.it/rss/tutti", 20),
    PackFeed("TGCom24", "https://www.tgcom24.mediaset.it/rss/homepage.xml"),
    PackFeed("Libero", "https://www.liberoquotidiano.it/rss.xml", 20),
    PackFeed("Domani", "https://www.editorialedomani.it/rss", 20),
    # Italian tech
    PackFeed("HDblog", "https://www.hdblog.it/feed/", 10, category="tech"),
    PackFeed("DDay", "https://www.dday.it/rss", category="tech"),
    PackFeed("Punto Informatico", "https://www.punto-informatico.it/feed/", 10, category="tech"),
], aliases=("italia",))

# Checked 2026-11-02. Le Point (404) and Les Echos (503) failed the check; Le Parisien's stories have no dates.
FRANCE = LocalPack("fr", "France", "Europe/Paris", ".fr", [
    PackFeed("Le Monde", "https://www.lemonde.fr/rss/une.xml"),
    PackFeed("Le Figaro", "https://www.lefigaro.fr/rss/figaro_actualites.xml"),
    PackFeed("Libération", "https://www.liberation.fr/arc/outboundfeeds/rss-all/?outputType=xml", 20),
    PackFeed("franceinfo", "https://www.franceinfo.fr/titres.rss"),
    PackFeed("20 Minutes", "https://www.20minutes.fr/feeds/rss-une.xml"),
    PackFeed("Ouest-France", "https://www.ouest-france.fr/rss/une"),
    PackFeed("L'Express", "https://www.lexpress.fr/arc/outboundfeeds/rss/alaune.xml"),
    PackFeed("BFMTV", "https://www.bfmtv.com/rss/news-24-7/"),
    PackFeed("L'Humanité", "https://www.humanite.fr/feed"),
    PackFeed("Mediapart", "https://www.mediapart.fr/articles/feed"),
    # French tech
    PackFeed("Numerama", "https://www.numerama.com/feed/", category="tech"),
    PackFeed("Frandroid", "https://www.frandroid.com/feed", category="tech"),
    PackFeed("Next", "https://next.ink/feed/", category="tech"),
])

# Checked 2026-11-02. The Toronto Star refused the check (429) and the National Post's feeds were empty.
CANADA = LocalPack("ca", "Canada", "America/Toronto", ".ca", [
    PackFeed("CBC News", "https://www.cbc.ca/webfeed/rss/rss-canada"),
    PackFeed("Radio-Canada", "https://ici.radio-canada.ca/info/rss/info/a-la-une"),
    PackFeed("CTV News", "https://www.ctvnews.ca/arc/outboundfeeds/rss/category/canada/"),
    PackFeed("Global News", "https://globalnews.ca/canada/feed/"),
    PackFeed("The Globe and Mail", "https://www.theglobeandmail.com/arc/outboundfeeds/rss/category/canada/"),
    PackFeed("Toronto Sun", "https://torontosun.com/category/news/national/feed"),
    PackFeed("Maclean's", "https://macleans.ca/feed/"),
    PackFeed("iPolitics", "https://www.ipolitics.ca/feed/"),
    PackFeed("The Hub", "https://thehub.ca/feed/"),
    PackFeed("The Tyee", "https://thetyee.ca/rss2.xml"),
    # Canadian tech
    PackFeed("MobileSyrup", "https://mobilesyrup.com/feed/", category="tech"),
    PackFeed("BetaKit", "https://betakit.com/feed/", category="tech"),
])

# Checked 2026-11-02. News Corp (The Australian, news.com.au, Herald Sun, Sky News) served empty feeds or 403.
AUSTRALIA = LocalPack("au", "Australia", "Australia/Sydney", ".au", [
    PackFeed("ABC News", "https://www.abc.net.au/news/feed/2942460/rss.xml"),
    PackFeed("SBS News", "https://www.sbs.com.au/news/topic/australia/feed"),
    PackFeed("Guardian Australia", "https://www.theguardian.com/australia-news/rss"),
    PackFeed("Sydney Morning Herald", "https://www.smh.com.au/rss/national.xml"),
    PackFeed("The Age", "https://www.theage.com.au/rss/national.xml"),
    PackFeed("The West Australian", "https://thewest.com.au/news/rss", 20),
    PackFeed("7NEWS", "https://7news.com.au/news/rss", 20),
    PackFeed("Daily Mail Australia", "https://www.dailymail.com/auhome/index.rss", 20),
    PackFeed("Crikey", "https://www.crikey.com.au/feed/"),
    PackFeed("Quadrant", "https://quadrant.org.au/feed/"),
    # Australian tech
    PackFeed("iTnews", "https://www.itnews.com.au/RSS/rss.ashx", category="tech"),
    PackFeed("Channel News", "https://www.channelnews.com.au/feed/", category="tech"),
])

LOCAL_PACKS: dict[str, LocalPack] = {p.code: p for p in (SLOVENIA, GERMANY, UNITED_STATES, UNITED_KINGDOM, AUSTRIA,
                                                         CROATIA, ITALY, FRANCE, CANADA, AUSTRALIA)}


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
