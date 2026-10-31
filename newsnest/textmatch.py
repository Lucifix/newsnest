"""Text helpers shared by keyword matching, URL dedupe and story grouping."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|ref_src$|oc$|cmpid$|ncid$)", re.I)


def normalize_url(url: str) -> str:
    """Canonical form for dedupe: https, lowercase host, no www, no tracking params, no fragment."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                             if not _TRACKING_PARAMS.match(k)))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, query, ""))


def fold(text: str) -> str:
    """Casefold and strip diacritics: Žurnal -> zurnal."""
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in text if not unicodedata.combining(c))


def keyword_pattern(keywords: list[str]) -> re.Pattern | None:
    """Case-insensitive match at a word start, so Slovenian stems catch every inflection."""
    words = [fold(k) for k in keywords if k.strip()]
    if not words:
        return None
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + ")")


def matches(pattern: re.Pattern | None, *texts: str | None) -> bool:
    return bool(pattern) and any(pattern.search(fold(t)) for t in texts if t)


# Function words that say nothing about which story a headline is about (Slovenian + English)
_STOPWORDS = set("""
in je se na za da so ki ne pa bo bi z s v k o od do po pri iz ali tudi kot ter ze še se ga jo ju jih
smo ste sem si bil bila bilo bili bodo lahko kaj kako kdo kje zakaj tega ta to te ti tem teh tej
vse vsi več vec nov nova novo nove novi med pred nad pod brez proti zaradi glede dan leta let
huda hudo hude hudi spet danes foto video pozor letni letnik moski moški zenska ženska
the a an of to and or in on at for with by from as is are was were be been it its this that these
those has have had will would can could not no new how why what who when after over into about
says said than more most just now out up off your you our their his her they we he she
""".split())


def title_tokens(title: str) -> set[str]:
    """Content-word stems of a headline. Prefix stemming (6 chars) folds Slovenian inflections:
    Ljubljana/Ljubljani -> ljublj, Dončić/Dončića -> doncic."""
    words = re.findall(r"[a-z0-9]+", fold(title))
    out = set()
    for w in words:
        if w in _STOPWORDS:
            continue
        if w.isdigit():
            if len(w) >= 2:
                out.add(w)
        elif len(w) >= 3:
            out.add(w[:6])
    return out
