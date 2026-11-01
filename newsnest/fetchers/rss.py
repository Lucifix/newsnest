"""Generic RSS/Atom fetcher (news sites, tech sites, hnrss, GitHub releases)."""

from __future__ import annotations

import io
import re

import feedparser

from newsnest.config import Source
from newsnest.fetchers.base import FetchContext, FetchFailed, html_to_text, http_get, struct_to_dt
from newsnest.models import FetchResult, FetchStatus, Item

_HN_POINTS = re.compile(r"Points:\s*(\d+)")
_HN_COMMENTS = re.compile(r"# Comments:\s*(\d+)")
_PRERELEASE = re.compile(r"(?i)(?:\d|[.-])(?:a|alpha|b|beta|rc|pre|dev)\.?\d*$")


def parse_feed(source: Source, ctx: FetchContext) -> tuple[feedparser.FeedParserDict | None, FetchResult]:
    """Download and parse a feed, classifying empty vs broken. Returns the parsed feed and a result shell.
    Uses a conditional request when validators are known; an unchanged feed (304) returns parsed=None."""
    headers = {}
    if source.params.get("etag"):
        headers["If-None-Match"] = source.params["etag"]
    if source.params.get("last_modified"):
        headers["If-Modified-Since"] = source.params["last_modified"]
    r = http_get(ctx, source.url, headers=headers)
    if r.status_code == 304:
        # Unchanged since the last fetch: same entries as before, nothing new to parse
        return None, FetchResult(source.id, FetchStatus.OK, http_status=304,
                                 raw_count=source.params.get("prev_raw") or 0)
    # A file object, never the bytes: feedparser opens a body like "/dev/zero" as a local path
    parsed = feedparser.parse(io.BytesIO(r.content), response_headers={"content-type": r.headers.get("content-type", "")})
    result = FetchResult(source.id, FetchStatus.OK, http_status=r.status_code, raw_count=len(parsed.entries),
                         etag=r.headers.get("etag"), last_modified=r.headers.get("last-modified"))
    if str(r.url) != source.url:
        result.warnings.append(f"redirected to {r.url}")
    if not parsed.entries:
        if parsed.bozo or not parsed.version:
            # HTTP 200 but not a feed we can read: the format probably changed
            raise FetchFailed(FetchStatus.PARSE_ERROR,
                              f"not a valid feed ({type(parsed.get('bozo_exception')).__name__})", r.status_code)
        result.status = FetchStatus.EMPTY
    elif parsed.bozo and not isinstance(parsed.get("bozo_exception"), feedparser.CharacterEncodingOverride):
        # An encoding override (declared us-ascii, actually utf-8) is harmless: text decodes fine
        result.warnings.append(f"feed is malformed but readable: {parsed.get('bozo_exception')}")
    return parsed, result


def entry_to_item(entry, source: Source) -> Item | None:
    title = html_to_text(entry.get("title"), limit=300)
    link = entry.get("link")
    if not title or not link:
        return None
    published = struct_to_dt(entry.get("published_parsed") or entry.get("updated_parsed"))
    summary = entry.get("summary") or (entry.get("content") or [{}])[0].get("value")
    image = entry_image(entry)
    return Item(
        source_id=source.id,
        category=source.category,
        title=title,
        url=link,
        guid=entry.get("id") or link,
        published=published,
        snippet=html_to_text(summary),
        publisher=source.name,
        extra={"image": image} if image else {},
    )


_IMG_SRC = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)


def entry_image(entry) -> str | None:
    """Best article image a feed entry offers: media:content, media:thumbnail, an image enclosure,
    or the first <img> in the content. https only (no mixed content, no plain http)."""
    candidates: list[str] = []
    media = sorted(entry.get("media_content") or [], key=lambda m: int(m.get("width") or 0), reverse=True)
    candidates += [m.get("url") for m in media if m.get("medium", "image") == "image" or "image" in m.get("type", "")]
    candidates += [m.get("url") for m in entry.get("media_thumbnail") or []]
    candidates += [e.get("href") for e in entry.get("enclosures") or [] if e.get("type", "").startswith("image")]
    html = (entry.get("summary") or "") + "".join(c.get("value", "") for c in entry.get("content") or [])
    candidates += _IMG_SRC.findall(html)
    for url in candidates:
        if not url:
            continue
        url = url.replace("&amp;", "&").strip()
        if url.startswith("//"):
            url = "https:" + url
        # Skip tracking pixels and plain-http images
        if url.startswith("https://") and not re.search(r"(feedburner|pixel|tracking|1x1|spacer)", url, re.I):
            return url
    return None


def finalize(result: FetchResult, raw_count: int) -> FetchResult:
    """Entries present but nothing usable extracted = parser broke on a changed format."""
    if raw_count and not result.items:
        result.status = FetchStatus.PARSE_ERROR
        result.error = f"{raw_count} entries but no title/link extracted"
    elif raw_count > len(result.items):
        result.warnings.append(f"skipped {raw_count - len(result.items)} entries without title/link")
    return result


class RssFetcher:
    def fetch(self, source: Source, ctx: FetchContext) -> FetchResult:
        parsed, result = parse_feed(source, ctx)
        if parsed is None:
            return result
        is_hn = "hnrss.org" in source.url
        plain_http = 0
        for entry in parsed.entries:
            item = entry_to_item(entry, source)
            if not item:
                continue
            if item.url.startswith("http://"):
                if source.params.get("https_links"):
                    item.url = "https://" + item.url[len("http://"):]
                else:
                    plain_http += 1
            if is_hn:
                # hnrss puts the article in <link> and stats in the description
                desc = entry.get("summary", "")
                if m := _HN_POINTS.search(desc):
                    item.extra["points"] = int(m.group(1))
                if m := _HN_COMMENTS.search(desc):
                    item.extra["comments"] = int(m.group(1))
                item.extra["discussion_url"] = entry.get("comments")
                item.snippet = ""
            result.items.append(item)
        if plain_http:
            result.warnings.append(f"{plain_http} item links use http:// (set https_links: true if the site supports https)")
        return finalize(result, len(parsed.entries))


class GithubReleasesFetcher:
    def fetch(self, source: Source, ctx: FetchContext) -> FetchResult:
        parsed, result = parse_feed(source, ctx)
        if parsed is None:
            return result
        repo = source.params["repo"]
        skipped_pre = 0
        for entry in parsed.entries:
            item = entry_to_item(entry, source)
            if not item:
                continue
            if not source.params.get("include_prereleases") and _PRERELEASE.search(item.title):
                skipped_pre += 1
                continue
            item.extra["version"] = item.title
            # The feed's thumbnail is the project avatar: a logo, not an article photo
            if "image" in item.extra:
                item.extra["logo"] = item.extra.pop("image")
            item.title = f"{repo} {item.title}"
            item.publisher = "GitHub"
            result.items.append(item)
        if skipped_pre:
            result.warnings.append(f"skipped {skipped_pre} pre-releases")
        # Pre-releases are deliberately skipped, not lost to a parser problem
        return finalize(result, len(parsed.entries) - skipped_pre)
