"""Server-rendered web UI (FastAPI + Jinja2). The scheduler runs in the same process."""

from __future__ import annotations

import asyncio
import hmac
from html import escape as html_escape
from urllib.parse import quote, urlencode, urlsplit
import hashlib
import logging
import os
import sqlite3
import tempfile
import threading
import zlib
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from newsnest import REPO_URL, WEBSITE_URL, __version__, catalog, eggs, icons
from newsnest.ai import AIClient, AIError, BudgetExceeded, ai_setup, spent_today
from newsnest.bootstrap import config_dir, env
from newsnest.calibration import calibration_data, subscore_labels
from newsnest.config import ICONS, MAX_TABS, NEWS, WORLD, Config, LoadedConfig, load_config, slugify
from newsnest.config_edit import (ConfigEditError, edit_config, ensure_section, free_section_key, keys_in,
                                  materialize_sections, remove_where, section_entry, section_list, section_map, tab_room)
from newsnest.db import get_meta, iso, loads, migrate, parse_iso, session, set_meta, utcnow
from newsnest.digest import available_days, build_digest
from newsnest.discovery import (OpmlFeed, add_candidates, good_stories, named_sources, parse_opml, promotion_suggestions,
                             region_words, start_check_thread, _signals)
from newsnest.feedfinder import FinderError, capped_get, find_feeds, finder_client
from newsnest import feedsession
from newsnest.drift import FADED, LIFTED, LINGER_MIN_CARDS, Drift, current_drift, current_lines, reading_report
from newsnest.feed import (SEARCH_DAYS, TOP_STORIES, adjust_weight, front_page, load_weights, read_stories,
                        saved_stories, search_stories, story_hidden, swipe_deck, view_feed, weight_strength)
from newsnest.interests import apply_changes, bullet_diff, bullets, norm, word_diff
from newsnest.levels import NAMES as LEVEL_NAMES, forget_removed, level_rows, set_level
from newsnest.missed import handle_missed
from newsnest.profile import PROFILE_FILES, profile_exists, read_profile, strip_comments, write_profile
from newsnest.packs import LIFESTYLE_INTEREST, LIFESTYLE_PACK, LOCAL_PACKS, add_interest, add_pack, missing_from, pack_for
from newsnest.profile_ai import draft_profile
from newsnest.scoring import carry_interest_labels
from newsnest.sections import Tab, dock_tabs, foreign_local, move_section, refile_items, section_keys, section_labels
from newsnest.status import collect_status
from newsnest.summaries import key_points, summarize_on_demand
from newsnest.textmatch import normalize_url

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
# Content fingerprint of the static files: a new deploy changes the URL, so phones never keep an old app.js
ASSET_VERSION = hashlib.sha1(b"".join((HERE / "static" / f).read_bytes() for f in ("app.css", "app.js"))).hexdigest()[:10]


def _start_scheduler():
    if (env("SCHEDULER", "on") or "on").lower() in ("off", "0", "false"):
        log.info("scheduler disabled by NEWSNEST_SCHEDULER")
        return None
    from apscheduler.schedulers.background import BackgroundScheduler
    from newsnest.runner import tick
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(tick, "interval", seconds=60, next_run_time=datetime.now(ZoneInfo("UTC")) + timedelta(seconds=5),
                      max_instances=1, coalesce=True, id="tick")
    scheduler.start()
    return scheduler


def _refile() -> int:
    """Stories follow their source when it moves to another section."""
    loaded = load_config(config_dir())
    if not loaded.config:
        return 0
    with session() as conn:
        moved = refile_items(conn, named_sources(conn, loaded.config, loaded.sources), section_keys(loaded.config))
    if moved:
        log.info("moved %d stories to their source's section", moved)
    return moved


def _move_section(old: str, new: str) -> int:
    """A section was deleted: everything stored under it moves to `new`."""
    with session() as conn:
        return move_section(conn, old, new)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    with session() as conn:
        migrate(conn)
    _refile()
    scheduler = _start_scheduler()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="NewsNest", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


def _manifest() -> dict:
    return {
        "id": "/", "name": "NewsNest: your news digest", "short_name": "NewsNest",
        "description": "Personal news feed", "lang": "en",
        "start_url": "/", "scope": "/", "display": "standalone", "orientation": "portrait",
        "background_color": "#ffffff", "theme_color": "#ffffff", "categories": ["news"],
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
        "shortcuts": [
            {"name": "Archive", "url": "/archive", "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192"}]},
            {"name": "Missed this", "url": "/missed", "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192"}]},
            {"name": "Swipe", "url": "/swipe", "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192"}]},
        ],
    }


@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse(_manifest(), media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
def service_worker():
    """Served from the root so it can control every page; the version in it changes with each deploy."""
    body = (HERE / "pwa" / "sw.js").read_text(encoding="utf-8").replace("__VERSION__", ASSET_VERSION)
    return Response(body, media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/offline", response_class=HTMLResponse)
def offline():
    return HTMLResponse((HERE / "pwa" / "offline.html").read_text(encoding="utf-8"))


# --- password protection: one shared password from NEWSNEST_PASSWORD; no password = no access ---

SESSION_COOKIE = "newsnest_session"
PUBLIC_PATHS = {"/login", "/logout", "/manifest.webmanifest", "/sw.js", "/offline", "/health"}


def _password() -> str:
    return env("PASSWORD", "") or ""


def _session_token(password: str) -> str:
    # Derived from the password: changing NEWSNEST_PASSWORD logs every device out
    return hmac.new(password.encode(), b"newsnest-session-v1", hashlib.sha256).hexdigest()


def request_authorized(request: Request) -> bool:
    password = _password()
    cookie = request.cookies.get(SESSION_COOKIE, "")
    return bool(password) and hmac.compare_digest(cookie, _session_token(password))


@app.middleware("http")
async def require_login(request: Request, call_next):
    # The path the router dispatches on. Not request.url.path: that is rebuilt from the Host header,
    # so "Host: x/login?" would make every URL look like /login and skip the password.
    path = request.scope["path"]
    if path in PUBLIC_PATHS or path.startswith("/static/") or request_authorized(request):
        return await call_next(request)
    if not _password():
        return HTMLResponse("<h1>NewsNest is locked</h1><p>Set the NEWSNEST_PASSWORD environment variable "
                            "(Portainer stack → Environment) and redeploy.</p>", status_code=503)
    if request.method != "GET" or "application/json" in request.headers.get("accept", "") \
            or path.startswith(("/api/", "/feed/")):
        return JSONResponse({"error": "login required"}, status_code=401)
    return RedirectResponse(f"/login?next={quote(path)}", status_code=303)


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light dark">
<title>Log in · NewsNest</title><link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials">
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;font:16px/1.5 system-ui,sans-serif;
background:#fff;color:#0a0a0c}}@media(prefers-color-scheme:dark){{body{{background:#0a0a0c;color:#f4f4f6}}
input{{background:#141418;color:#f4f4f6;border-color:#26262d}}}}form{{width:100%;max-width:320px;display:grid;gap:12px}}
h1{{display:flex;align-items:center;gap:12px;font-size:1.8rem;letter-spacing:-.02em;margin:0}}input{{font:inherit;padding:12px;border:1px solid #e3e3df;border-radius:8px}}
button{{font:inherit;font-weight:700;padding:12px;border:0;border-radius:6px;background:#ff3da6;color:#0a0a0c}}
.err{{color:#b3261e;margin:0}}</style></head><body>
<form method="post" action="/login"><h1><svg viewBox="0 0 32 32" width="40" height="40" aria-hidden="true"><rect width="32" height="32" rx="7" fill="#0a0a0c"/><g fill="none" stroke="#f4f4f6" stroke-width="3.6" stroke-linecap="round" transform="translate(16 16) scale(.8) translate(-16 -16)"><path d="M8.8 13.6A9.6 9.6 0 0 1 18.4 23.2"/><path d="M8.8 6.4A16.8 16.8 0 0 1 25.6 23.2"/></g><circle cx="10.24" cy="21.76" r="3.4" fill="#c6ff3d"/></svg>NewsNest</h1>{error}
<input type="hidden" name="next" value="{next}">
<input type="password" name="password" placeholder="Password" autocomplete="current-password" autofocus required>
<button type="submit">Log in</button></form></body></html>"""


def _safe_next(target: str) -> str:
    """A path on this site, or /. Browsers read "/\\evil.com" and "/<tab>/evil.com" as //evil.com, another site."""
    parts = urlsplit(target)
    if (not target.startswith("/") or target.startswith("//") or parts.scheme or parts.netloc
            or any(c == "\\" or ord(c) < 0x20 or ord(c) == 0x7f for c in target)):
        return "/"
    # The same path, built on our own "/": a fixed prefix is what CodeQL recognises as a local redirect
    return "/" + target[1:]


@app.get("/login", response_class=HTMLResponse)
def login_page(next: str = "/"):
    if not _password():
        return HTMLResponse("<h1>NewsNest is locked</h1><p>Set NEWSNEST_PASSWORD and redeploy.</p>", status_code=503)
    return HTMLResponse(LOGIN_PAGE.format(error="", next=html_escape(_safe_next(next), quote=True)))


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    password, target = _password(), _safe_next(str(form.get("next") or "/"))
    # Compared as bytes: compare_digest raises on non-ASCII str, so a password like "šifra" could never log in
    if not password or not hmac.compare_digest(str(form.get("password") or "").encode(), password.encode()):
        await asyncio.sleep(1.5)  # slows down guessing
        return HTMLResponse(LOGIN_PAGE.format(error='<p class="err">Wrong password.</p>',
                                              next=html_escape(target, quote=True)), status_code=401)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(SESSION_COOKIE, _session_token(password), max_age=365 * 86400, httponly=True,
                        samesite="lax", secure=_https(request))
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.middleware("http")
async def no_stale_pages(request: Request, call_next):
    """Pages are always revalidated, so the browser picks up the fingerprinted asset URLs of a new deploy."""
    response = await call_next(request)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def get_conn():
    with session() as conn:
        yield conn


def get_loaded() -> LoadedConfig:
    return load_config(config_dir())


@app.get("/icon/{source_id}")
def source_icon(source_id: str, conn=Depends(get_conn)):
    """A source's stored icon. The address carries the icon's hash (?v=), so it can be cached for good."""
    row = icons.load(conn, source_id)
    if row is None:
        raise HTTPException(404)
    return Response(row["data"], media_type=row["mime"], headers={
        "Cache-Control": "private, max-age=31536000, immutable", "ETag": f'"{row["hash"]}"',
        "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; sandbox"})


def tz_of(loaded: LoadedConfig) -> ZoneInfo:
    return ZoneInfo(loaded.config.timezone if loaded.config else "UTC")


def feed_host(url: str) -> str:
    """The site a feed comes from, short enough for one line on a phone (img.rtvslo.si → rtvslo.si)."""
    parts = (urlsplit(url).hostname or url).split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 and parts[-2] not in ("co", "com") else ".".join(parts)


def icon_lookup(loaded: LoadedConfig):
    """icon(source_id) -> the address of the source's stored icon, or None (the badge shows initials).
    The table is read once per page, and only if the page shows a badge."""
    off = {f.id for f in loaded.config.feeds if not f.favicon} if loaded.config else set()
    found: dict[str, str] | None = None

    def icon(source_id: str | None) -> str | None:
        nonlocal found
        if not source_id or source_id in off:
            return None
        if found is None:
            with session() as conn:
                found = icons.versions(conn)
        version = found.get(source_id)
        return f"/icon/{quote(source_id, safe='')}?v={version}" if version else None
    return icon


def template_helpers(loaded: LoadedConfig) -> dict:
    tz = tz_of(loaded)

    def localtime(value, fmt="%H:%M"):
        dt = parse_iso(value) if isinstance(value, str) else value
        return dt.astimezone(tz).strftime(fmt) if dt else ""

    categories = loaded.config.scoring.categories if loaded.config else []
    return {"cfg": loaded.config, "version": __version__, "repo_url": REPO_URL, "website_url": WEBSITE_URL,
            "bug_url": f"{REPO_URL}/issues/new?template=bug_report.yml&version={__version__}",
            "asset_version": ASSET_VERSION, "localtime": localtime, "ago": ago, "initials": initials,
            "icon": icon_lookup(loaded),
            "key_points": key_points, "section_labels": section_labels(loaded.config), "host": feed_host,
            "dock": lambda current=None: [Tab("all", "For you", "star"), *dock_tabs(loaded.config, current)],
            "hue": hue, "cat_hue": lambda c: (categories.index(c) % 8) + 1 if c in categories else 0,
            "now_local": datetime.now(tz), "today": datetime.now(tz).date(), "timedelta": timedelta,
            # the hour only counts once a time zone was chosen: the default UTC is nobody's wall clock
            "caughtup_aside": lambda seed, empty: eggs.caughtup_aside(
                seed, empty, datetime.now(tz) if tz.key != "UTC" else None),
            "subscores": subscore_labels(loaded.config) if loaded.config else ()}


def render(request: Request, name: str, conn, loaded: LoadedConfig, **context) -> HTMLResponse:
    status = collect_status(conn, loaded)
    return templates.TemplateResponse(request, name, {
        **template_helpers(loaded), "banner": status.banner, "status": status, **context})


def ago(value) -> str:
    """'just now', '25 min', '3 h', 'yesterday', '4 d'."""
    dt = parse_iso(value) if isinstance(value, str) else value
    if not dt:
        return ""
    seconds = (utcnow() - dt).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    if seconds < 172800:
        return "yesterday"
    return f"{int(seconds // 86400)} d ago"


def initials(name: str) -> str:
    """Badge text for a source: '24ur' -> '24', 'N1 Slovenija' -> 'N1', 'The Verge' -> 'TV'."""
    words = [w for w in (name or "?").replace("-", " ").split() if w[0].isalnum()]
    if len(words) >= 2 and not any(c.isdigit() for c in words[0]):
        return (words[0][0] + words[1][0]).upper()
    return words[0][:2].upper() if words else "?"


def hue(name: str) -> int:
    """Stable color slot 1-8 per source name."""
    return (zlib.crc32((name or "").encode()) % 8) + 1


def _require_cfg(loaded: LoadedConfig) -> Config:
    if not loaded.config:
        raise HTTPException(503, "config.yaml is invalid: " + "; ".join(loaded.errors))
    return loaded.config


# --- digest ---

FILTER_COOKIE = "newsnest_filter"


PAGE_SIZE = 20


def _https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def _set_feed_cookie(request: Request, response, sid: str) -> None:
    """sid is always a stored session's id from the database (newsnest.feedsession), never the cookie as sent."""
    response.set_cookie(feedsession.COOKIE, sid, max_age=7 * 86400, samesite="lax", httponly=True,
                        secure=_https(request))


@app.get("/", response_class=HTMLResponse)
def home(request: Request, f: str | None = None, n: int = PAGE_SIZE, refresh: int = 0, view: str = "",
         at: int | None = None, conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    """The Discover-style feed. Only the first page is rendered; the rest loads while scrolling.
    The chosen filter chip is remembered in a cookie. The order comes from the device's feed session
    (newsnest.feedsession), so it holds still across reloads, tab switches and Back; refresh=1 starts
    a new one and comes back here without it.
    On wide screens the page is a front page beside the first page of the feed, which ends in More stories;
    view=list is that button's page: the whole feed alone, as one column (phones look the same either way).
    at=<story id> starts the list at that story (the first one the lane left out), wherever it ranks now;
    a story no longer in the feed starts it from the top."""
    cfg = _require_cfg(loaded)
    sources = named_sources(conn, cfg, loaded.sources)
    list_view = view == "list"
    if refresh:
        sid, _ = feedsession.start(conn, cfg, sources)
        query = {k: v for k, v in (("f", f), ("view", "list" if list_view else None)) if v}
        response = RedirectResponse(("/?" + urlencode(query)) if query else "/", status_code=303)
        _set_feed_cookie(request, response, sid)
        return response
    active = f or request.cookies.get(FILTER_COOKIE) or "all"
    # stable_sessions off: every page load ranks again (the session then only serves its own "load more")
    sid, ranking = feedsession.open_session(conn, cfg, sources, request.cookies.get(feedsession.COOKIE),
                                            fresh=not cfg.feed.stable_sessions)
    feed = view_feed(ranking, cfg, active)
    token = sid
    shown = max(PAGE_SIZE, min(n, 300))
    start = next((i for i, it in enumerate(feed.items) if it.card.lead.id == at), 0) if list_view and at else 0
    end = start + shown
    suggestion = (promotion_suggestions(conn, cfg) or [None])[0] if feed.active == "all" else None
    # front: the desktop front page beside the feed (wide screens only; phones never show it)
    response = render(request, "home.html", conn, loaded, feed=feed, token=token, suggestion=suggestion,
                      page=feed.items[start:end], next_offset=end if end < len(feed.items) else None,
                      front=None if list_view else front_page(feed.items), top_stories=TOP_STORIES,
                      list_view=list_view, start=start)
    response.set_cookie(FILTER_COOKIE, feed.active, max_age=365 * 86400, samesite="lax")
    _set_feed_cookie(request, response, sid)
    return response


@app.get("/feed/more", response_class=HTMLResponse)
def feed_more(request: Request, token: str, offset: int, f: str | None = None, conn=Depends(get_conn),
              loaded: LoadedConfig = Depends(get_loaded)):
    """The next page of the session's ranking, as an HTML fragment for the infinite scroll. The token is
    the session the page was rendered from."""
    cfg = _require_cfg(loaded)
    sources = named_sources(conn, cfg, loaded.sources)
    # A session that ended meanwhile (away for long, Refresh elsewhere) starts again; the page skips duplicates
    token, ranking = feedsession.open_session(conn, cfg, sources, token)
    feed = view_feed(ranking, cfg, f or request.cookies.get(FILTER_COOKIE) or "all")
    offset = max(0, offset)
    end = offset + PAGE_SIZE
    return templates.TemplateResponse(request, "_feed_page.html", {
        **template_helpers(loaded), "feed": feed, "page": feed.items[offset:end], "token": token,
        "next_offset": end if end < len(feed.items) else None})


SWIPE_DECK = 30
SWIPE_LATER_COOKIE = "newsnest_swipe_later"  # lead ids swiped up ("later"), written by app.js


@app.get("/swipe", response_class=HTMLResponse)
def swipe(request: Request, conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    """Tinder-style deck of the stories NewsNest is least sure about (feed.swipe_deck): right = 👍,
    left = not interested, up = later, tap opens. Impressions and auto-read are not tracked here."""
    cfg = _require_cfg(loaded)
    later = set(request.cookies.get(SWIPE_LATER_COOKIE, "").split("."))
    deck = swipe_deck(conn, cfg, named_sources(conn, cfg, loaded.sources), later)
    return render(request, "swipe.html", conn, loaded, deck=deck[:SWIPE_DECK], left=len(deck))


@app.get("/read", response_class=HTMLResponse)
def read_page(request: Request, conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    cfg = _require_cfg(loaded)
    return render(request, "read.html", conn, loaded, stories=read_stories(conn, cfg, named_sources(conn, cfg, loaded.sources)))


@app.get("/saved", response_class=HTMLResponse)
def saved_page(request: Request, conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    cfg = _require_cfg(loaded)
    return render(request, "saved.html", conn, loaded, stories=saved_stories(conn, cfg, named_sources(conn, cfg, loaded.sources)))


@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = "", all: int = 0, conn=Depends(get_conn),
                loaded: LoadedConfig = Depends(get_loaded)):
    """Keyword search over the last 14 days (all=1: everything). The search layer on every page asks
    with X-Fragment: 1 and gets just the results; without it this is a normal page."""
    cfg = _require_cfg(loaded)
    q = q.strip()[:200]
    cards, older = search_stories(conn, cfg, named_sources(conn, cfg, loaded.sources), q, None if all else SEARCH_DAYS)
    context = {"q": q, "cards": cards, "older": older, "everything": bool(all), "days": SEARCH_DAYS,
               "hidden": story_hidden}
    if request.headers.get("x-fragment") == "1":
        return templates.TemplateResponse(request, "_search_results.html", {**template_helpers(loaded), **context})
    return render(request, "search.html", conn, loaded, **context)


@app.get("/day/{day}", response_class=HTMLResponse)
def digest_day(day: date, request: Request, conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    cfg = _require_cfg(loaded)
    d = build_digest(conn, cfg, named_sources(conn, cfg, loaded.sources), day)
    return render(request, "digest.html", conn, loaded, digest=d, archive=True)


@app.get("/more")
def more():
    """The old hub; Settings holds all of it now. Kept so an installed home-screen link still works."""
    return RedirectResponse("/settings", status_code=303)


@app.get("/archive", response_class=HTMLResponse)
def archive(request: Request, conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    cfg = _require_cfg(loaded)
    return render(request, "archive.html", conn, loaded, days=available_days(conn, cfg))


def _mark_story_read(conn, item_ids: list[int]) -> None:
    """Read applies to the whole story; last_read_at moves on every open, so 'Updated' resets."""
    if not item_ids:
        return
    now = iso(utcnow())
    marks = ",".join("?" * len(item_ids))
    conn.execute(
        f"""UPDATE items SET read_at = COALESCE(read_at, ?), last_read_at = ?, seen_at = COALESCE(seen_at, ?)
            WHERE story_id IN (SELECT story_id FROM items WHERE id IN ({marks}))
               OR event IN (SELECT event FROM items WHERE id IN ({marks}) AND event IS NOT NULL)""",
        (now, now, now, *item_ids, *item_ids))


CARD_HOPS = 4


def _card_items(conn, item_id: int) -> list[int]:
    """Every item on the same card as this one. A card joins items that share a story or an event
    (digest.make_cards), and those links can chain (story A ↔ event ↔ story B), so follow them a few hops."""
    ids = {item_id}
    for _ in range(CARD_HOPS):
        m = ",".join("?" * len(ids))
        found = {r[0] for r in conn.execute(
            f"""SELECT id FROM items
                WHERE story_id IN (SELECT story_id FROM items WHERE id IN ({m}))
                   OR event IN (SELECT event FROM items WHERE id IN ({m}) AND event IS NOT NULL)""",
            (*ids, *ids))}
        if found <= ids:
            break
        ids |= found
    return sorted(ids)


def _mark_expanded(conn, item_id: int) -> None:
    """The reader opened the card's key points: a sign of interest that needs no tap (newsnest.drift)."""
    ids = _card_items(conn, item_id)
    conn.execute(f"UPDATE items SET expanded_at = COALESCE(expanded_at, ?) WHERE id IN ({','.join('?' * len(ids))})",
                 (iso(utcnow()), *ids))


@app.get("/go/{item_id}")
def go(request: Request, item_id: int, conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    row = conn.execute("SELECT url, opened_at FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    _mark_story_read(conn, [item_id])
    if loaded.config:  # opening a story is reading: the feed session stays
        feedsession.touch(conn, loaded.config, request.cookies.get(feedsession.COOKIE))
    if row["opened_at"] is None:  # first open only; auto-read on scroll never comes through here
        conn.execute("UPDATE items SET opened_at = ? WHERE id = ?", (iso(utcnow()), item_id))
    return RedirectResponse(row["url"], status_code=302)


@app.post("/api/items/{item_id}/rate")
def rate(item_id: int, request: Request, value: int = Form(...), conn=Depends(get_conn)):
    if value not in (-1, 0, 1):
        raise HTTPException(400, "value must be -1, 0 or 1")
    conn.execute("UPDATE items SET rating = ?, rated_at = ? WHERE id = ?",
                 (value or None, iso(utcnow()) if value else None, item_id))
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"id": item_id, "rating": value})
    return RedirectResponse(request.headers.get("referer", "/"), status_code=303)


SUMMARIZE_MESSAGES = {
    "no_text": "This article has no readable text, so there are no key points.",
    "error": "Could not write key points this time. Try again in a bit.",
    "budget": "Today's AI budget is used up. Raise it in Settings or try tomorrow.",
    "unavailable": "The AI is not set up (no API key), so key points are off.",
}


@app.post("/api/items/{item_id}/summarize")
def summarize(item_id: int, request: Request, lines: int = Form(3), conn=Depends(get_conn),
              loaded: LoadedConfig = Depends(get_loaded)):
    """The card's "Get key points" button: fetches the article and writes key points on demand.
    Returns the card's new summary block as HTML, rendered by the same macro as the page."""
    cfg = _require_cfg(loaded)
    try:
        status = summarize_on_demand(conn, cfg, AIClient.create(cfg, conn), item_id)
    except BudgetExceeded:
        status = "budget"
    except AIError:
        status = "unavailable"
    if status == "missing":
        raise HTTPException(404)
    if status == "done":
        _mark_expanded(conn, item_id)
    html = None
    if status == "done":
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        html = templates.get_template("_summary.html").render(
            **template_helpers(loaded), row=row, lines=min(max(lines, 2), 4))
    return _back(request, {"status": status, "html": html, "message": SUMMARIZE_MESSAGES.get(status)})


def _ids(raw: str) -> list[int]:
    try:
        return [int(x) for x in raw.split(",") if x.strip()][:2000]
    except ValueError:
        raise HTTPException(400, "ids must be comma-separated integers")


def _back(request: Request, payload: dict):
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(payload)
    return RedirectResponse(request.headers.get("referer", "/"), status_code=303)


@app.post("/api/read")
def set_read(request: Request, ids: str = Form(""), value: int = Form(1), token: str = Form(""), f: str = Form("all"),
             conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    """Mark stories read (value=1) or unread (value=0); ids are item ids of the stories.
    With a feed token instead of ids: every story of that feed session's tab f (Mark all read). The session
    then ends, so the next page load ranks again and the feed is empty."""
    if token:
        cfg = _require_cfg(loaded)
        ranking = feedsession.resume(conn, cfg, named_sources(conn, cfg, loaded.sources), token)
        if ranking is None:
            raise HTTPException(409, "The page is out of date; reload and try again")
        ids = ",".join(it.card.ids for it in view_feed(ranking, cfg, f).items)
        feedsession.end(conn, token)
    id_list = _ids(ids)
    if id_list and value:
        _mark_story_read(conn, id_list)
    elif id_list:
        m = ",".join("?" * len(id_list))
        conn.execute(f"""UPDATE items SET read_at = NULL, last_read_at = NULL
                         WHERE story_id IN (SELECT story_id FROM items WHERE id IN ({m}))
                            OR event IN (SELECT event FROM items WHERE id IN ({m}) AND event IS NOT NULL)""",
                     (*id_list, *id_list))
    return _back(request, {"ids": id_list, "read": bool(value)})


@app.post("/api/saved")
def set_saved(request: Request, ids: str = Form(...), value: int = Form(1), conn=Depends(get_conn)):
    """Save a story for later (value=1) or take it off the Saved list (value=0). Applies to the whole story.
    Saving is a bookmark only: it does not touch read state, rating or ranking."""
    id_list = _ids(ids)
    if id_list:
        m = ",".join("?" * len(id_list))
        conn.execute(f"""UPDATE items SET saved_at = ?
                         WHERE id IN ({m})
                            OR story_id IN (SELECT story_id FROM items WHERE id IN ({m}))
                            OR event IN (SELECT event FROM items WHERE id IN ({m}) AND event IS NOT NULL)""",
                     (iso(utcnow()) if value else None, *id_list, *id_list, *id_list))
    return _back(request, {"ids": id_list, "saved": bool(value)})


SEEN_THROTTLE = timedelta(minutes=45)


@app.post("/api/seen")
def set_seen(request: Request, ids: str = Form(...), conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    """Impressions from the home feed. Counted at most once per story per 45 min; a story skipped often
    enough sinks and then drops out, from the next feed session on (the current one holds still)."""
    if loaded.config:  # cards on screen: the reader is still at it
        feedsession.touch(conn, loaded.config, request.cookies.get(feedsession.COOKIE))
    id_list = _ids(ids)
    if id_list:
        now = utcnow()
        conn.execute(
            f"""UPDATE items SET seen_at = COALESCE(seen_at, ?),
                    seen_count = seen_count + CASE WHEN last_seen_at IS NULL OR last_seen_at < ? THEN 1 ELSE 0 END,
                    last_seen_at = CASE WHEN last_seen_at IS NULL OR last_seen_at < ? THEN ? ELSE last_seen_at END
                WHERE id IN ({','.join('?' * len(id_list))})""",
            (iso(now), iso(now - SEEN_THROTTLE), iso(now - SEEN_THROTTLE), iso(now), *id_list))
    return JSONResponse({"seen": len(id_list)})


# Attention time (newsnest.drift): one report adds at most ATTENTION_REPORT_MAX ms to a card, and a card
# collects at most ATTENTION_CARD_MAX ms in total, so a phone left open on one story can't dominate
ATTENTION_REPORT_MAX = 60_000
ATTENTION_CARD_MAX = 300_000
ATTENTION_PAIRS_MAX = 200


def _attention_pairs(raw: str) -> list[tuple[int, int]]:
    """'<lead id>:<ms>,…' from the feed page; malformed pairs are skipped, ms clamped."""
    out = []
    for part in raw.split(",")[:ATTENTION_PAIRS_MAX]:
        item_id, _, ms = part.strip().partition(":")
        try:
            out.append((int(item_id), max(0, min(ATTENTION_REPORT_MAX, int(float(ms))))))
        except ValueError:
            continue
    return [(i, ms) for i, ms in out if ms > 0]


@app.post("/api/attention")
def set_attention(request: Request, ms: str = Form(""), conn=Depends(get_conn),
                  loaded: LoadedConfig = Depends(get_loaded)):
    """How long each card on the home feed was in view while the reader was active, split between the cards
    on screen at once (app.js). Counts for every copy of the story; no throttle, the caps are the guard."""
    if loaded.config:
        feedsession.touch(conn, loaded.config, request.cookies.get(feedsession.COOKIE))
    pairs = _attention_pairs(ms)
    for item_id, add in pairs:
        ids = _card_items(conn, item_id)
        conn.execute(f"UPDATE items SET view_ms = MIN(view_ms + ?, ?) WHERE id IN ({','.join('?' * len(ids))})",
                     (add, ATTENTION_CARD_MAX, *ids))
    if pairs and get_meta(conn, "attention_since") is None:
        set_meta(conn, "attention_since", iso(utcnow()))
    return JSONResponse({"ok": len(pairs)})


@app.post("/api/items/{item_id}/expanded")
def set_expanded(item_id: int, conn=Depends(get_conn)):
    """The reader opened a card's key points (a click, not a page opening them by itself)."""
    _mark_expanded(conn, item_id)
    return JSONResponse({"ok": True})


FEEDBACK_ACTIONS = {"not_interested", "undo_not_interested", "less_source", "more_source", "less_category", "more_category"}


@app.post("/api/feedback")
def feedback(request: Request, action: str = Form(...), ids: str = Form(""), target: str = Form(""),
             conn=Depends(get_conn)):
    """The card's menu. 'Not interested' hides the story and counts as 👎; the other actions adjust
    learned outlet/category weights (ranking only) and are logged for the weekly profile proposal."""
    if action not in FEEDBACK_ACTIONS:
        raise HTTPException(400, f"unknown action {action}")
    id_list = _ids(ids)
    now = iso(utcnow())
    weight = None
    if action in ("not_interested", "undo_not_interested") and id_list:
        marks = ",".join("?" * len(id_list))
        hide = action == "not_interested"
        conn.execute(f"UPDATE items SET hidden_at = ? WHERE id IN ({marks})", (now if hide else None, *id_list))
        conn.execute("UPDATE items SET rating = ?, rated_at = ? WHERE id = ?",
                     (-1 if hide else None, now if hide else None, id_list[0]))
    elif action.endswith("_source") and target:
        weight = adjust_weight(conn, "source", target, 0.75 if action.startswith("less") else 1.25)
    elif action.endswith("_category") and target:
        weight = adjust_weight(conn, "category", target, 0.75 if action.startswith("less") else 1.25)
    if action != "undo_not_interested":
        conn.execute("INSERT INTO feedback (at, kind, target, item_id) VALUES (?, ?, ?, ?)",
                     (now, action, target or None, id_list[0] if id_list else None))
    else:
        conn.execute("DELETE FROM feedback WHERE id = (SELECT MAX(id) FROM feedback WHERE kind = 'not_interested' "
                     "AND item_id = ?)", (id_list[0] if id_list else None,))
    return _back(request, {"action": action, "weight": weight})


@app.post("/api/weights/reset")
def weights_reset(kind: str = Form(""), key: str = Form(""), conn=Depends(get_conn)):
    if kind and key:
        conn.execute("DELETE FROM weights WHERE kind = ? AND key = ?", (kind, key))
    else:
        conn.execute("DELETE FROM weights")
    return RedirectResponse("/profile#tuning", status_code=303)


# --- status, health, manual run ---

@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request, conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    recent = conn.execute("SELECT * FROM fetch_runs WHERE status NOT IN ('ok', 'empty') ORDER BY id DESC LIMIT 20").fetchall()
    calls = conn.execute("SELECT * FROM ai_calls ORDER BY id DESC LIMIT 15").fetchall()
    return render(request, "status.html", conn, loaded, page="status", require_cfg=False, recent_errors=recent, ai_calls=calls,
                  running=_manual_run.is_alive() if _manual_run else False, **settings_context("status", conn, loaded))


_manual_run: threading.Thread | None = None


@app.post("/api/run")
def run_now():
    """Fetch everything now (and score), in the background."""
    global _manual_run
    from newsnest.runner import tick
    if not (_manual_run and _manual_run.is_alive()):
        _manual_run = threading.Thread(target=tick, kwargs={"force": True}, daemon=True)
        _manual_run.start()
    return RedirectResponse("/status", status_code=303)


@app.get("/health")
def health(request: Request, conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    status = collect_status(conn, loaded)
    if not request_authorized(request):  # public for the Docker healthcheck: no details
        return {"status": "degraded" if status.banner else "ok"}
    failing = [s.id for s in status.sources if s.health.level == "failing"]
    return {"status": "degraded" if status.banner else "ok", "version": __version__,
            "sources": len(loaded.sources), "failing_sources": failing, "problems": status.banner,
            "last_tick": iso(status.last_tick), "config_errors": loaded.errors}


# --- calibration ---

@app.get("/calibration", response_class=HTMLResponse)
def calibration(request: Request, days: int = 30, conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    cfg = _require_cfg(loaded)
    days = days if days in (7, 30, 90) else 30
    return render(request, "calibration.html", conn, loaded, cal=calibration_data(conn, cfg, days), days=days,
                  page="calibration", saved=request.query_params.get("saved"), error=request.query_params.get("error"),
                  **settings_context("calibration", conn, loaded))


# --- missed this ---

@app.get("/missed", response_class=HTMLResponse)
def missed_page(request: Request, conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    misses = conn.execute("SELECT * FROM misses ORDER BY id DESC LIMIT 50").fetchall()
    return render(request, "missed.html", conn, loaded, misses=misses)


@app.post("/missed")
def missed_submit(url: str = Form(...), conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    cfg = _require_cfg(loaded)
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Paste a full link starting with https://")
    try:
        ai = AIClient.create(cfg, conn)
    except AIError:
        ai = None
    handle_missed(conn, cfg, url, ai)
    return RedirectResponse("/missed", status_code=303)


# --- profile and onboarding ---

def onboarding_items(conn, limit: int = 30):
    """A spread of recent headlines: round-robin across sources so one outlet doesn't dominate."""
    rows = conn.execute(
        """SELECT * FROM items WHERE prefilter IS NOT 'muted' AND fetched_at >= ?
           ORDER BY published DESC""", (iso(utcnow() - timedelta(days=3)),)).fetchall()
    by_source: dict[str, list] = {}
    for r in rows:
        by_source.setdefault(r["source_id"], []).append(r)
    picked, i = [], 0
    while len(picked) < limit and any(i < len(v) for v in by_source.values()):
        for items in by_source.values():
            if i < len(items) and len(picked) < limit:
                picked.append(items[i])
        i += 1
    return picked


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    files = {name: read_profile(name) for name in PROFILE_FILES}
    proposals = conn.execute("SELECT * FROM proposals WHERE status = 'pending' ORDER BY id DESC").fetchall()
    rated = conn.execute("SELECT COUNT(*) FROM items WHERE rating IS NOT NULL").fetchone()[0]
    onboarding = not profile_exists()
    proposals = [{**dict(r), "count": len((loads(r["changes"], None) or {}).get("items") or [])} for r in proposals]
    levels = level_rows(conn, loaded.config)
    skip = bullets(strip_comments(files["interests.md"]))["skip"]
    edit = request.query_params.get("edit", "")
    return render(request, "profile.html", conn, loaded, page="profile", files=files, proposals=proposals, onboarding=onboarding,
                  items=onboarding_items(conn) if onboarding else [], rated=rated, error=request.query_params.get("error"),
                  saved=request.query_params.get("saved"), require_cfg=False, error_prefix="", tuning=tuning_rows(conn, loaded),
                  levels=levels, skip=skip, edit=edit, level_names=LEVEL_NAMES,
                  dropped=request.query_params.getlist("dropped"),
                  **settings_context("profile", conn, loaded))


@app.post("/profile/level")
def profile_level(line: str = Form(...), step: int = Form(...), conn=Depends(get_conn)):
    """How much of one interest (newsnest.levels). Only a line that is in the interests file now."""
    if step not in LEVEL_NAMES:
        raise HTTPException(400, "unknown level")
    from newsnest.scoring import want_lines
    current = {norm(w): w for w in want_lines()}
    if norm(line) not in current:
        raise HTTPException(404, "that interest is no longer in your list")
    set_level(conn, current[norm(line)], step)
    return RedirectResponse("/profile?saved=level#interests", status_code=303)


def _change_interests(conn, change: dict, saved: str) -> RedirectResponse:
    """One line of interests.md added, reworded or removed from the Profile page's list. Only that line
    changes; comments and everything else in the file stay as they are. A Want line reworded here keeps its
    stories' history and its level, since the old and new wording are both known."""
    text, applied, skipped = apply_changes(read_profile("interests.md"), [change])
    if not applied:
        reason = skipped[0][1] if skipped else "nothing changed"
        return RedirectResponse(f"/profile?error={quote(reason[:1].upper() + reason[1:])}#interests", status_code=303)
    write_profile("interests.md", text)
    carry_interest_labels(conn, applied, text)
    forget_removed(conn, text)
    return RedirectResponse(f"/profile?saved={saved}#interests", status_code=303)


@app.post("/profile/interest/add")
def interest_add(section: str = Form(...), text: str = Form(""), conn=Depends(get_conn)):
    return _change_interests(conn, {"op": "add", "section": section, "new": " ".join(text.split())}, "interest_add")


@app.post("/profile/interest/edit")
def interest_edit(section: str = Form(...), old: str = Form(...), new: str = Form(""), conn=Depends(get_conn)):
    return _change_interests(conn, {"op": "reword", "section": section, "old": old, "new": " ".join(new.split())},
                             "interest_edit")


@app.post("/profile/interest/remove")
def interest_remove(section: str = Form(...), line: str = Form(...), conn=Depends(get_conn)):
    return _change_interests(conn, {"op": "remove", "section": section, "old": line}, "interest_remove")


def _dropped_query(dropped: list[tuple[str, str]]) -> str:
    """The levels a save dropped, for the note on the Profile page: '&dropped=Wristwatches%3ALess…'."""
    return "".join("&" + urlencode({"dropped": f"{label}:{level}"}) for label, level in dropped)


LATELY_SHOWN = 5


def tuning_rows(conn, loaded: LoadedConfig) -> dict:
    """How the feed is tuned, in the groups the profile page shows: what you've read lately (drift, read-only),
    then the outlet/category weights: more, less, and tiny ones. While drift is active the older learned
    boosts are left out, as the feed leaves them out."""
    names = {s.id: s.name for s in named_sources(conn, loaded.config, loaded.sources)} if loaded.config else {}
    drift = current_drift(conn, loaded.config) if loaded.config else None
    more, less, small = [], [], []
    for (kind, key), w in load_weights(conn, skip_old_boosts=bool(drift and drift.active)).items():
        direction, level = weight_strength(w)
        row = {"kind": kind, "key": key, "name": names.get(key, key) if kind == "source" else key, "weight": w,
               "level": level, "outlet": kind == "source"}
        (small if level == 0 else more if direction == "more" else less).append(row)
    lately_more: list[str] = []
    lately_less: list[str] = []
    if drift and drift.active:
        def name(key: str) -> str:
            return names.get(key[7:], key[7:]) if key.startswith("source:") else drift.label(key)
        ranked = sorted(drift.weights.items(), key=lambda kv: -abs(kv[1] - 1))
        lately_more = [name(k) for k, w in ranked if w >= LIFTED][:LATELY_SHOWN]
        lately_less = [name(k) for k, w in ranked if w <= FADED][:LATELY_SHOWN]
    return {"more": sorted(more, key=lambda r: -r["weight"]), "less": sorted(less, key=lambda r: r["weight"]),
            "small": sorted(small, key=lambda r: r["name"].lower()),
            "lately_more": lately_more, "lately_less": lately_less,
            "warmup_days_left": drift.warmup_days_left if drift else None}


@app.get("/profile/reading", response_class=HTMLResponse)
def reading_page(request: Request, conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    """Everything following the reader's reading is based on, and what it changes: read-only, with the card
    menu's own more/less choices as the only actions."""
    return render(request, "profile_reading.html", conn, loaded, page="profile",
                  reading=reading_context(conn, loaded) if loaded.config else None,
                  **settings_context("profile", conn, loaded))


def effect_text(weight: float | None) -> str | None:
    """A drift weight as words: '25% more', '18% less', 'As usual'; None while it isn't in use."""
    if weight is None:
        return None
    pct = round((weight - 1) * 100)
    return "As usual" if pct == 0 else f"{pct}% more" if pct > 0 else f"{-pct}% less"


def reading_context(conn, loaded: LoadedConfig) -> dict:
    cfg = loaded.config
    names = {s.id: s.name for s in named_sources(conn, cfg, loaded.sources)}
    lines = current_lines()
    report = reading_report(conn, cfg, lines=lines)
    drift = current_drift(conn, cfg)
    label = Drift(lines=lines).label

    def row(key: str, count) -> dict:
        kind, _, name = key.partition(":")
        weight = drift.weights.get(key, 1.0) if drift.active else None
        return {"kind": kind, "target": name, "name": names.get(name, name) if kind == "source" else label(key),
                "line": lines.get(name) if kind == "interest" else None, "shown": count.shown, "read": count.read,
                "effect": effect_text(weight)}

    rows = sorted((row(k, c) for k, c in report.keys.items()), key=lambda r: (-r["shown"], r["name"].lower()))
    set_aside = []
    if drift.active:
        kept = load_weights(conn, skip_old_boosts=True)
        set_aside = sorted(({"key": key, "name": names.get(key, key) if kind == "source" else key, "weight": w,
                             "outlet": kind == "source", "effect": effect_text(w)} for (kind, key), w in load_weights(conn).items()
                            if (kind, key) not in kept), key=lambda r: (-r["weight"], r["name"].lower()))
    stored = loads(get_meta(conn, "drift"), None) if cfg.drift.enabled else None
    if not cfg.drift.enabled:
        status = "Off"
    elif drift.active:
        status = "On"
    elif drift.warmup_days_left:
        status = f"Learning · about {drift.warmup_days_left} day{'s' if drift.warmup_days_left != 1 else ''} left"
    else:
        status = "Learning · needs a bit more reading"
    return {"report": report, "status": status, "active": drift.active, "worked_out": stored["at"] if stored else None,
            "topics": [r for r in rows if r["kind"] != "source"], "outlets": [r for r in rows if r["kind"] == "source"],
            "set_aside": set_aside, "drift": cfg.drift, "linger_min": LINGER_MIN_CARDS}


@app.post("/profile/save/{name}")
def profile_save(name: str, text: str = Form(""), proposal_id: int | None = Form(None), conn=Depends(get_conn)):
    if name not in PROFILE_FILES:
        raise HTTPException(404)
    write_profile(name, text)
    if proposal_id:
        conn.execute("UPDATE proposals SET status = 'accepted', decided_at = ? WHERE id = ?", (iso(utcnow()), proposal_id))
        p = conn.execute("SELECT changes FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        if name == "interests.md" and p:
            carry_interest_labels(conn, (loads(p["changes"], None) or {}).get("items") or [], text)
    dropped = forget_removed(conn, text) if name == "interests.md" else []
    return RedirectResponse("/profile?saved=" + name + _dropped_query(dropped), status_code=303)


@app.post("/profile/draft")
def profile_draft(conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    cfg = _require_cfg(loaded)
    try:
        pid = draft_profile(conn, cfg, AIClient.create(cfg, conn))
    except AIError as e:
        return RedirectResponse(f"/profile?error={str(e)[:200]}", status_code=303)
    return RedirectResponse(f"/profile/proposals/{pid}", status_code=303)


def basis_text(basis: dict | None) -> str:
    """'Based on 46 ratings, 3 missed stories and 12 more/less choices since 22.10.'"""
    if not basis:
        return ""
    parts = [f"{n} {word}" for n, word in ((basis.get("ratings"), "ratings"), (basis.get("missed"), "missed stories"),
                                          (basis.get("choices"), "more/less choices")) if n]
    since = parse_iso(basis["since"]) if basis.get("since", "")[:4] > "1970" else None
    if not parts:
        return ""
    text = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"Based on {text}" + (f" since {since.strftime('%d.%m.')}" if since else "")


@app.get("/profile/proposals/{pid}", response_class=HTMLResponse)
def proposal_page(pid: int, request: Request, conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    p = conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
    if not p:
        raise HTTPException(404)
    changes = loads(p["changes"], None)  # None for proposals made before changes were listed one by one
    items = changes["items"] if changes else None
    # an older whole-file proposal: show only the bullets that differ, not a line diff
    gone = bullet_diff(p["current_text"] or "", p["proposed_text"] or "") if items is None and p["kind"] == "weekly" else None
    return render(request, "proposal.html", conn, loaded, page="profile", require_cfg=False, error_prefix="",
                  p=p, items=items, gone=gone, basis=basis_text(changes["basis"]) if changes else "", word_diff=word_diff,
                  error=request.query_params.get("error"), **settings_context("profile", conn, loaded))


@app.post("/profile/proposals/{pid}/apply")
def proposal_apply(pid: int, accept: list[str] = Form([]), conn=Depends(get_conn)):
    """Apply the ticked changes to interests.md as it is now; whatever else is in the file stays as it was."""
    p = conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
    if not p or p["status"] != "pending":
        raise HTTPException(404)
    items = (loads(p["changes"], None) or {}).get("items") or []
    picked = {int(x) for x in accept if x.isdigit() and int(x) < len(items)}
    back = "/profile/proposals/" + str(pid)  # concatenated, not formatted: CodeQL sees the fixed prefix
    if not picked:
        return RedirectResponse(f"{back}?error={quote('Tick at least one change, or dismiss the suggestion')}", status_code=303)
    text, applied, skipped = apply_changes(read_profile("interests.md"), [items[i] for i in sorted(picked)])
    if not applied:
        return RedirectResponse(f"{back}?error={quote('None of these fit your interests as they are now')}", status_code=303)
    write_profile("interests.md", text)
    carry_interest_labels(conn, applied, text)
    dropped = forget_removed(conn, text)
    conn.execute("UPDATE proposals SET status = 'accepted', decided_at = ? WHERE id = ?", (iso(utcnow()), pid))
    return RedirectResponse(f"/profile?saved=applied&skipped={len(skipped)}" + _dropped_query(dropped), status_code=303)


@app.post("/profile/proposals/{pid}/reject")
def proposal_reject(pid: int, conn=Depends(get_conn)):
    conn.execute("UPDATE proposals SET status = 'rejected', decided_at = ? WHERE id = ?", (iso(utcnow()), pid))
    return RedirectResponse("/profile", status_code=303)


# --- settings ---

# Settings is a few short pages, iOS Settings style; every change lands back on the page it was made on
SETTINGS_PAGES = ("general", "feeds", "discover", "topics", "more", "app", "about")
ACTION_PAGE = {"general": "general", "region": "general",
               "feed_add": "feeds", "feed_section": "feeds", "feed_remove": "feeds", "feed_toggle": "feeds",
               "feed_favicon": "feeds",
               "section_add": "feeds", "section_edit": "feeds", "section_move": "feeds", "section_remove": "feeds",
               "watch_add": "topics", "watch_remove": "topics", "keywords": "topics",
               "repo_add": "more", "repo_remove": "more"}
# These are made on the Calibration page and go back there
CALIBRATION_ACTIONS = ("threshold", "calibration_toggle")
TIME_ZONES = sorted(available_timezones())


def local_pack_context(cfg: Config) -> dict:
    """The Local news pack for the reader's country (and which of its feeds they don't have yet), plus all packs."""
    pack = pack_for(cfg.region.country)
    return {"local_packs": list(LOCAL_PACKS.values()), "local_pack": pack,
            "local_missing": missing_from(pack.feeds, cfg.feeds) if pack else []}


def settings_context(page: str, conn, loaded: LoadedConfig) -> dict:
    """What one settings page needs, plus the counts shown in the settings menu. Each page runs only its own queries."""
    cfg = loaded.config
    if not cfg:
        return {}
    suggestions = promotion_suggestions(conn, cfg)
    ctx = {"nav": {"feeds": len(cfg.feeds), "watches": len(cfg.watches), "earned": len(suggestions),
                   "trial": conn.execute("SELECT COUNT(*) FROM candidates WHERE status IN ('candidate', 'checking')").fetchone()[0]}}
    if page == "general":
        ctx.update(spent=spent_today(conn, cfg), ai_setup=ai_setup(cfg), time_zones=TIME_ZONES, **local_pack_context(cfg))
    elif page == "profile":
        ctx.update(time_zones=TIME_ZONES, **local_pack_context(cfg))
    elif page == "feeds":
        ctx.update(foreign=foreign_local(conn, loaded.sources, cfg.region.tld), lifestyle_missing=missing_from(LIFESTYLE_PACK, cfg.feeds),
                   lifestyle_interest=LIFESTYLE_INTEREST.lower() in read_profile("interests.md").lower(),
                   section_icons=ICONS, max_tabs=MAX_TABS, catalog_size=catalog.catalog_size(), **local_pack_context(cfg))
    elif page == "discover":
        ctx.update(candidates=candidate_rows(conn, cfg), suggestions=suggestions, catalog_size=catalog.catalog_size(),
                   catalog_topics=len(catalog.load_catalog()))
    elif page == "catalog":
        sections = cfg.all_sections
        ctx.update(topics=catalog.load_catalog(), states=catalog.feed_states(conn, cfg), sections=sections,
                   dest={t.key: catalog.destination(t, sections) for t in catalog.load_catalog()})
    return ctx


def _settings(request: Request, page: str, conn, loaded: LoadedConfig, root: bool = False, **extra) -> HTMLResponse:
    return render(request, f"settings_{page}.html", conn, loaded, page=page, root=root,
                  error=request.query_params.get("error"), saved=request.query_params.get("saved"),
                  **settings_context(page, conn, loaded), **extra)


@app.get("/settings", response_class=HTMLResponse)
def settings_root(request: Request, conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    """Phones show the settings menu; wider screens show the menu beside General."""
    return _settings(request, "general", conn, loaded, root=True)


@app.get("/settings/backup/database")
def database_backup(conn=Depends(get_conn)):
    """Download a consistent copy of the whole database. Uses SQLite's online backup, so it is safe while the
    app is running (copying newsnest.db alone would miss what is still in the WAL file)."""
    fd, tmp = tempfile.mkstemp(prefix="newsnest-backup-", suffix=".db")
    os.close(fd)
    dest = sqlite3.connect(tmp)
    try:
        conn.backup(dest)
        dest.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal/-shm beside it
    except Exception:
        dest.close()
        os.unlink(tmp)
        raise
    dest.close()
    name = f"newsnest-{datetime.now().strftime('%Y-%m-%d-%H%M')}.db"
    return FileResponse(tmp, media_type="application/vnd.sqlite3", filename=name,
                        headers={"Cache-Control": "no-store"}, background=BackgroundTask(os.unlink, tmp))


@app.get("/settings/discover/catalog", response_class=HTMLResponse)
def settings_catalog(request: Request, conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    """The feed catalog, under Discover. Opening it starts the lookup of the logos it doesn't have yet."""
    if loaded.config:
        catalog.start_icon_thread(loaded.config)
    return render(request, "settings_catalog.html", conn, loaded, page="discover", sub="catalog",
                  error=request.query_params.get("error"), **settings_context("catalog", conn, loaded))


@app.get("/settings/discover/catalog/icons")
def catalog_icons(conn=Depends(get_conn)):
    """The catalog logos found so far, while the page waits for the rest."""
    return {"running": catalog.warming(), "icons": catalog.found_icons(conn)}


@app.get("/settings/{page}", response_class=HTMLResponse)
def settings_page(page: str, request: Request, conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    if page not in SETTINGS_PAGES:
        raise HTTPException(404)
    return _settings(request, page, conn, loaded, require_cfg=page != "about")  # a broken config is when people need the bug link


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace(",", "\n").splitlines() if line.strip()]


def _refuse_duplicate(feeds: list, url: str) -> None:
    """The same feed twice means twice the requests: sites answer that with 429 and the red banner."""
    key = normalize_url(url.strip())
    for f in feeds:
        if normalize_url(str(f.get("url", ""))) == key:
            raise ConfigEditError(f"{f.get('name') or f.get('id')} is already a feed (same address)")


def _free_id(feeds: list, name: str) -> str:
    """A slug of the name, numbered when another feed already has it (two papers both called "Novice")."""
    base = fid = slugify(name)
    taken = {f.get("id") for f in feeds}
    n = 2
    while fid in taken:
        fid, n = f"{base}-{n}", n + 1
    return fid


@app.post("/settings/feed_find", response_class=HTMLResponse)
def feed_find(request: Request, address: str = Form(""), conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    """Look up the feeds behind a website address and show them with a preview, each with its own Add."""
    cfg = _require_cfg(loaded)
    error = None
    try:
        with finder_client(cfg) as client:
            found = find_feeds(client, address[:2000])
        if not found:
            error = "No feed found there. Try the address of the site's news page, or ask the site whether it has RSS."
    except FinderError as e:
        found, error = [], str(e)
    known = {normalize_url(f.url): f.name or f.id for f in cfg.feeds}
    for f in found:
        f.known = known.get(normalize_url(f.url))
    return _settings(request, "feeds", conn, loaded, finder={"address": address, "found": found, "error": error})


@app.post("/settings/pack/lifestyle")
async def lifestyle_pack(request: Request):
    """One tap: the curated lifestyle feeds as regular sources, the matching interest in the profile,
    and stories of feeds that were already there moved to the Lifestyle section."""
    form = await request.form()
    counts = {}

    def change(data):
        ensure_section(data, "lifestyle")   # the section comes back if it was deleted
        counts["added"], counts["moved"] = add_pack(section_list(data, "feeds"), LIFESTYLE_PACK, "lifestyle")

    try:
        edit_config(change)
    except (ConfigEditError, KeyError, ValueError) as e:
        return RedirectResponse(f"/settings/feeds?error={quote(str(e)[:500])}", status_code=303)
    if form.get("interest"):
        write_profile("interests.md", add_interest(read_profile("interests.md"), LIFESTYLE_INTEREST))
    await asyncio.to_thread(_refile)
    return RedirectResponse(f"/settings/feeds?pack={counts['added']}&packmoved={counts['moved']}"
                            f"&interest={1 if form.get('interest') else 0}#section-lifestyle", status_code=303)


def _onboarding_next(form) -> bool:
    """Region and pack forms on the onboarding page go back there; nothing else is followed."""
    return form.get("next") == "/profile"


def _write_drift_enabled(data, on: bool) -> None:
    """"Follow what I've been reading lately". On is the default, so turning it on removes the setting
    rather than writing it: configs of people who never touch it stay as they were."""
    if not on:
        section_map(data, "drift")["enabled"] = False
        return
    drift = data.get("drift")
    if drift is not None:
        drift.pop("enabled", None)
        if not drift:
            del data["drift"]


def _write_region(data, form) -> None:
    """Country and city from a form, plus the web domain and time zone when it has them. A country with a
    Local pack fills in the pack's domain and zone when the form leaves them out (the onboarding form does)."""
    region = section_map(data, "region")
    region["country"], region["city"] = form.get("country", "").strip(), form.get("city", "").strip()
    pack = pack_for(region["country"])
    if "tld" in form:
        region["tld"] = form["tld"].strip()
    elif pack and not region.get("tld"):
        region["tld"] = pack.tld
    zone = form.get("timezone", "").strip()
    if zone:
        data["timezone"] = zone
    elif pack:
        data["timezone"] = pack.timezone


@app.post("/settings/pack/local/{code}")
async def local_pack(code: str, request: Request):
    """One tap: a country's Local news feeds as regular sources (its tech sites go to Tech), and the
    country's web domain as the region's when none is set."""
    pack = LOCAL_PACKS.get(code)
    if not pack:
        raise HTTPException(404)
    form = await request.form()
    back = "/profile" if _onboarding_next(form) else "/settings/feeds"
    counts = {}

    def change(data):
        for key in {p.category for p in pack.feeds if p.category}:   # the pack's tech sites, say
            ensure_section(data, key)
        counts["added"], counts["moved"] = add_pack(section_list(data, "feeds"), pack.feeds, "news")
        region = section_map(data, "region")
        if not region.get("tld") and pack_for(region.get("country") or "") is pack:
            region["tld"] = pack.tld

    try:
        edit_config(change)
    except (ConfigEditError, KeyError, ValueError) as e:
        return RedirectResponse(f"{back}?error={quote(str(e)[:500])}", status_code=303)
    await asyncio.to_thread(_refile)
    if back == "/profile":
        return RedirectResponse(f"/profile?localpack={counts['added']}", status_code=303)
    return RedirectResponse(f"/settings/feeds?localpack={counts['added']}&packmoved={counts['moved']}&country={quote(pack.country)}"
                            "#section-news", status_code=303)


@app.post("/settings/{action}")
async def settings_action(action: str, request: Request):
    form = dict(await request.form())
    back = "/calibration" if action in CALIBRATION_ACTIONS else "/settings/" + ACTION_PAGE.get(action, "general")
    if action == "region" and _onboarding_next(form):
        back = "/profile"
    if "category" in form and form["category"] not in section_keys(load_config(config_dir()).config):
        return RedirectResponse(f"{back}?error={quote('unknown section ' + form['category'])}", status_code=303)

    added = {}
    removed = {"feeds": 0}

    def change(data):
        if action == "section_add":
            seq = materialize_sections(data)
            label = " ".join(form["label"].split())
            icon = form.get("icon", "star")
            if icon not in ICONS:
                raise ConfigEditError(f"unknown icon {icon}")
            added["key"] = free_section_key(seq, label)
            seq.append(section_entry(added["key"], label, icon, tab=tab_room(seq)))
        elif action == "section_edit":
            seq = materialize_sections(data)
            entry = next((s for s in seq if s.get("key") == form["key"]), None)
            if entry is None:
                raise ConfigEditError(f"unknown section {form['key']}")
            entry["label"] = " ".join(form["label"].split())
            if entry["key"] not in (NEWS, WORLD):   # Local and World share the News tab: no icon, no switch
                entry["icon"] = form.get("icon", "star")
                if form.get("tab") == "on":
                    entry.pop("tab", None)   # on is the default
                else:
                    entry["tab"] = False
        elif action == "section_move":
            seq = materialize_sections(data)
            i = next((i for i, s in enumerate(seq) if s.get("key") == form["key"]), None)
            j = None if i is None else i + (-1 if form["dir"] == "up" else 1)
            if j is not None and 0 <= j < len(seq):
                seq[i], seq[j] = seq[j], seq[i]
        elif action == "section_remove":
            key, dest = form["key"], form["move_to"]
            if key in (NEWS, WORLD):
                raise ConfigEditError("Local and World are built in. You can rename them, not remove them")
            seq = materialize_sections(data)
            if dest == key or not any(s.get("key") == dest for s in seq):
                raise ConfigEditError("choose a section for its feeds to move to")
            for f in section_list(data, "feeds"):
                if f.get("category") == key:
                    f["category"] = dest
                    removed["feeds"] += 1
            # With or without repos: a repo added later would land in the section that is gone
            gh = data.get("github_releases")
            if gh is not None and gh.get("category", "tech") == key:
                gh["category"] = dest
            if (caps := data.get("categories")) is not None and key in caps:
                del caps[key]
            remove_where(seq, lambda s: s.get("key") == key)
        elif action == "feed_add":
            feeds = section_list(data, "feeds")
            url = form["url"].strip()
            _refuse_duplicate(feeds, url)
            name = " ".join(form["name"].split())[:80] or feed_host(url)
            added["id"] = _free_id(feeds, form.get("id") or name)
            feeds.append({"id": added["id"], "name": name, "url": url, "category": form["category"]})
        elif action == "feed_section":
            for f in section_list(data, "feeds"):
                if f.get("id") == form["id"]:
                    f["category"] = form["category"]
        elif action == "feed_remove":
            remove_where(section_list(data, "feeds"), lambda f: f.get("id") == form["id"])
        elif action == "feed_toggle":
            for f in section_list(data, "feeds"):
                if f.get("id") == form["id"]:
                    f["enabled"] = not f.get("enabled", True)
        elif action == "feed_favicon":
            for f in section_list(data, "feeds"):
                if f.get("id") == form["id"]:
                    if f.get("favicon", True):
                        f["favicon"] = False
                    else:
                        f.pop("favicon", None)   # the default: no need to spell it out
        elif action == "watch_add":
            section_list(data, "watches").append({"name": form["name"].strip(), "keywords": _lines(form["keywords"])})
        elif action == "watch_remove":
            remove_where(section_list(data, "watches"), lambda w: w.get("name") == form["name"])
        elif action == "keywords":
            data.setdefault("prefilter", {})
            data["prefilter"]["mute_keywords"] = _lines(form.get("mute", ""))
            data["prefilter"]["must_show_keywords"] = _lines(form.get("must_show", ""))
        elif action == "repo_add":
            section_list(data, "github_releases", "repos").append(form["repo"].strip())
            gh = data["github_releases"]
            if gh.get("category", "tech") not in keys_in(data):   # its section was removed (by hand, say)
                gh["category"] = WORLD
        elif action == "repo_remove":
            repos = section_list(data, "github_releases", "repos")
            remove_where(repos, lambda r: r == form["repo"])
        elif action == "threshold":
            data.setdefault("digest", {})["min_final"] = float(form["min_final"])
        elif action == "calibration_toggle":
            data["scoring"]["calibration_mode"] = not data["scoring"].get("calibration_mode", False)
        elif action == "general":
            data["scoring"]["calibration_mode"] = form.get("calibration_mode") == "on"
            data.setdefault("digest", {})["min_final"] = float(form["min_final"])
            data["budget"]["daily_usd"] = float(form["daily_usd"])
            data["scoring"]["summary_language"] = form["summary_language"].strip() or "English"
            section_map(data, "feed")["auto_read_on_scroll"] = form.get("auto_read_on_scroll") == "on"
            _write_drift_enabled(data, form.get("follow_lately") == "on")
            if "country" in form:
                _write_region(data, form)
        elif action == "region":
            _write_region(data, form)
        else:
            raise ConfigEditError(f"unknown action {action}")

    try:
        edit_config(change)
    except (ConfigEditError, KeyError, ValueError) as e:
        return RedirectResponse(f"{back}?error={quote(str(e)[:500])}", status_code=303)
    extra = anchor = ""
    # Feed changes come back to the feed's own row, so the page doesn't jump to the top
    if action == "feed_section":
        n = await asyncio.to_thread(_refile)
        extra += f"&changed={quote(form['id'])}&n={n}"
    if action == "section_remove":
        # Stories already stored under the section follow its feeds; this also catches those of feeds that are gone
        n = await asyncio.to_thread(_move_section, form["key"], form["move_to"])
        extra += f"&feeds={removed['feeds']}&n={n}"
    if action in ("feed_section", "feed_toggle"):
        anchor = f"#feed-{form['id']}"
    elif action == "feed_add":
        anchor = f"#feed-{added['id']}"
    elif action.startswith("section_"):
        anchor = "#sections"
    # Concatenated, not formatted: CodeQL sees that what the form sent only follows our own path
    return RedirectResponse(back + "?saved=" + action + extra + anchor, status_code=303)


# --- discovered sources ---

def candidate_rows(conn, cfg: Config) -> list[dict]:
    """Candidates with `signals` (opens and likes this week, the number promotion counts), `good` (stories this
    week good enough to reach the feed) and, for one with nothing good yet, `days_left` before it retires."""
    now = utcnow()
    since = iso(now - timedelta(days=7))
    rows = conn.execute(
        "SELECT * FROM candidates ORDER BY CASE status WHEN 'candidate' THEN 0 WHEN 'checking' THEN 1 ELSE 2 END, title").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["check"] = loads(r["check_result"], {}) or {}
        d["signals"] = d["good"] = 0
        d["days_left"] = None
        if r["status"] == "candidate":
            d["signals"] = _signals(conn, r["source_id"], since)
            d["good"] = good_stories(conn, cfg, r["source_id"], since)
            # Retirement looks at the whole trial: only one that never had a good story or a read is on the clock
            trial = iso(now - timedelta(days=cfg.discovery.retire_after_days))
            if not d["good"] and not _signals(conn, r["source_id"], trial) and not good_stories(conn, cfg, r["source_id"]):
                age = now - parse_iso(r["checked_at"] or r["added_at"])
                d["days_left"] = max(0, cfg.discovery.retire_after_days - age.days)
        out.append(d)
    return out


@app.post("/discovery/catalog")
async def discovery_catalog(request: Request, loaded: LoadedConfig = Depends(get_loaded)):
    """Picked catalog feeds go on trial. Each topic's feeds go to the section its `section.<topic>` field names:
    one the reader has, or "new" for the topic's suggested section, created here (once, if topics share it)."""
    cfg = _require_cfg(loaded)
    form = await request.form()
    picks = catalog.find(form.getlist("url"))
    if not picks:
        return RedirectResponse("/settings/discover/catalog", status_code=303)
    keys = set(section_keys(cfg))
    wanted = {}
    for t, _ in picks:
        home = catalog.destination(t, cfg.all_sections)
        asked = form.get(f"section.{t.key}")
        wanted[t.key] = home if home and asked in (None, "new") else asked or WORLD   # never a second Science
    created: dict[str, str] = {}   # suggested key -> the key it got

    def change(data):
        seq = materialize_sections(data)
        for t, _ in picks:
            if wanted[t.key] == "new" and t.section not in created:
                created[t.section] = free_section_key(seq, t.section_label)
                seq.append(section_entry(created[t.section], t.section_label, t.icon, tab=tab_room(seq)))

    if any(v == "new" for v in wanted.values()):
        try:
            edit_config(change)
        except (ConfigEditError, KeyError, ValueError) as e:
            return RedirectResponse(f"/settings/discover/catalog?error={quote(str(e)[:500])}", status_code=303)
        cfg = _require_cfg(load_config(config_dir()))
        keys = set(section_keys(cfg))

    def section_of(t: catalog.Topic) -> str:
        want = created.get(t.section) if wanted[t.key] == "new" else wanted[t.key]
        return want if want in keys else WORLD

    feeds = [OpmlFeed(f.url, f.name, section_of(t)) for t, f in picks]
    with session() as conn:
        added = add_candidates(conn, cfg, feeds, "catalog")
        catalog.copy_icons(conn, [f for _, f in picks])
    start_check_thread(cfg)
    return RedirectResponse(f"/settings/discover?saved=discovery&added={added}", status_code=303)


@app.post("/discovery/import")
async def discovery_import(request: Request, loaded: LoadedConfig = Depends(get_loaded)):
    """OPML from an uploaded file or a link; feeds go to the candidate pool and are checked in the background."""
    cfg = _require_cfg(loaded)
    form = await request.form()
    upload, url = form.get("file"), (form.get("url") or "").strip()
    category = form.get("category") or "world"
    try:
        if upload is not None and getattr(upload, "filename", ""):
            data = await upload.read()
            origin = upload.filename
        elif url.startswith("https://"):
            # The link is someone else's: https and public hosts only (redirects included), size capped
            def download() -> bytes:
                with finder_client(cfg) as client:
                    return capped_get(client, url)[1]
            data, origin = await asyncio.to_thread(download), url
        else:
            raise ValueError("choose an OPML file or paste an https link to one")
        feeds = parse_opml(data, category, region_words(cfg), section_keys(cfg))
    except Exception as e:  # bad file, bad link, not OPML
        from urllib.parse import quote
        return RedirectResponse(f"/settings/discover?error={quote(f'OPML import failed: {e}'[:300])}", status_code=303)
    with session() as conn:
        added = add_candidates(conn, cfg, feeds, origin[:120])
    start_check_thread(cfg)
    return RedirectResponse(f"/settings/discover?saved=discovery&added={added}", status_code=303)


@app.post("/discovery/{source_id}/section")
async def discovery_section(source_id: str, category: str = Form(...), conn=Depends(get_conn),
                            loaded: LoadedConfig = Depends(get_loaded)):
    """A source on trial moves to another section, and the stories it already brought go with it."""
    cfg = _require_cfg(loaded)
    if category not in section_keys(cfg):
        raise HTTPException(400, "unknown section")
    row = conn.execute("SELECT id, source_id FROM candidates WHERE source_id = ?", (source_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    conn.execute("UPDATE candidates SET category = ? WHERE id = ?", (category, row["id"]))
    n = await asyncio.to_thread(_refile)
    # The id in the address is the stored one, never what the request sent
    sid = quote(row["source_id"])
    return RedirectResponse("/settings/discover?saved=discovery&changed=" + sid + "&n=" + str(n) + "#trial-" + sid,
                            status_code=303)


@app.post("/discovery/{source_id}/{action}")
def discovery_action(source_id: str, action: str, conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    _require_cfg(loaded)
    row = conn.execute("SELECT * FROM candidates WHERE source_id = ?", (source_id,)).fetchone()
    if not row or action not in ("promote", "remove", "restore"):
        raise HTTPException(404)
    if action == "promote":
        def change(data):
            # On trial since before its section was removed by hand: it becomes a World feed
            category = row["category"] if row["category"] in keys_in(data) else WORLD
            _refuse_duplicate(section_list(data, "feeds"), row["url"])
            ids = {f.get("id") for f in section_list(data, "feeds")}
            fid = base = slugify(row["title"] or source_id)
            n = 2
            while fid in ids:
                fid, n = f"{base}-{n}", n + 1
            section_list(data, "feeds").append({"id": fid, "name": row["title"], "url": row["url"], "category": category})
        try:
            edit_config(change)
        except ConfigEditError as e:
            from urllib.parse import quote
            return RedirectResponse(f"/settings/discover?error={quote(str(e)[:300])}", status_code=303)
        status = "promoted"
    else:
        status = "dismissed" if action == "remove" else "candidate"
    conn.execute("UPDATE candidates SET status = ?, decided_at = ? WHERE id = ?", (status, iso(utcnow()), row["id"]))
    return RedirectResponse("/settings/discover?saved=discovery", status_code=303)
