"""Server-rendered web UI (FastAPI + Jinja2). The scheduler runs in the same process."""

from __future__ import annotations

import asyncio
import hmac
from html import escape as html_escape
from urllib.parse import quote
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

from newsnest import __version__
from newsnest.ai import AIClient, AIError, BudgetExceeded, spent_today
from newsnest.bootstrap import config_dir, env
from newsnest.calibration import calibration_data, subscore_labels
from newsnest.config import Config, LoadedConfig, load_config, slugify
from newsnest.config_edit import ConfigEditError, edit_config, remove_where, section_list, section_map
from newsnest.db import iso, loads, migrate, parse_iso, session, utcnow
from newsnest.digest import available_days, build_digest
from newsnest.discovery import (STARTER_PACK, add_candidates, named_sources, parse_opml, promotion_suggestions,
                             region_words, start_check_thread, starter_missing, _signals)
from newsnest.feedfinder import FinderError, find_feeds, finder_client
from newsnest.feed import (LIKE_NUDGE, OPEN_NUDGE, SEARCH_DAYS, adjust_weight, build_feed, load_weights, nudge,
                        read_stories, recall, remember, saved_stories, search_stories, story_hidden, weight_strength)
from newsnest.interests import apply_changes, bullet_diff, word_diff
from newsnest.missed import handle_missed
from newsnest.profile import PROFILE_FILES, profile_exists, read_profile, write_profile
from newsnest.packs import LIFESTYLE_INTEREST, LIFESTYLE_PACK, LOCAL_PACKS, add_interest, add_pack, missing_from, pack_for
from newsnest.profile_ai import draft_profile
from newsnest.sections import LABELS, SECTIONS, foreign_local, refile_items
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
    """Stories follow their source when it moves to another section (Local / World / Tech)."""
    loaded = load_config(config_dir())
    if not loaded.config:
        return 0
    with session() as conn:
        moved = refile_items(conn, named_sources(conn, loaded.config, loaded.sources))
    if moved:
        log.info("moved %d stories to their source's section", moved)
    return moved


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
    return target if target.startswith("/") and not target.startswith("//") else "/"


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
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(SESSION_COOKIE, _session_token(password), max_age=365 * 86400, httponly=True,
                        samesite="lax", secure=secure)
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


def tz_of(loaded: LoadedConfig) -> ZoneInfo:
    return ZoneInfo(loaded.config.timezone if loaded.config else "UTC")


def feed_host(url: str) -> str:
    """The site a feed comes from, short enough for one line on a phone (img.rtvslo.si → rtvslo.si)."""
    from urllib.parse import urlsplit
    parts = (urlsplit(url).hostname or url).split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 and parts[-2] not in ("co", "com") else ".".join(parts)


def template_helpers(loaded: LoadedConfig) -> dict:
    tz = tz_of(loaded)

    def localtime(value, fmt="%H:%M"):
        dt = parse_iso(value) if isinstance(value, str) else value
        return dt.astimezone(tz).strftime(fmt) if dt else ""

    categories = loaded.config.scoring.categories if loaded.config else []
    return {"cfg": loaded.config, "version": __version__, "asset_version": ASSET_VERSION, "localtime": localtime, "ago": ago, "initials": initials,
            "key_points": key_points, "section_labels": LABELS, "host": feed_host,
            "hue": hue, "cat_hue": lambda c: (categories.index(c) % 8) + 1 if c in categories else 0,
            "now_local": datetime.now(tz), "today": datetime.now(tz).date(), "timedelta": timedelta,
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


@app.get("/", response_class=HTMLResponse)
def home(request: Request, f: str | None = None, n: int = PAGE_SIZE, conn=Depends(get_conn),
         loaded: LoadedConfig = Depends(get_loaded)):
    """The Discover-style feed. Only the first page is rendered; the rest loads while scrolling.
    The chosen filter chip is remembered in a cookie."""
    cfg = _require_cfg(loaded)
    active = f or request.cookies.get(FILTER_COOKIE) or "all"
    feed = build_feed(conn, cfg, named_sources(conn, cfg, loaded.sources), active)
    token = remember(feed)
    shown = max(PAGE_SIZE, min(n, 300))
    suggestion = (promotion_suggestions(conn, cfg) or [None])[0] if feed.active == "all" else None
    response = render(request, "home.html", conn, loaded, feed=feed, token=token, suggestion=suggestion,
                      page=feed.items[:shown], next_offset=shown if shown < len(feed.items) else None)
    response.set_cookie(FILTER_COOKIE, feed.active, max_age=365 * 86400, samesite="lax")
    return response


@app.get("/feed/more", response_class=HTMLResponse)
def feed_more(request: Request, token: str, offset: int, f: str | None = None, conn=Depends(get_conn),
              loaded: LoadedConfig = Depends(get_loaded)):
    """The next page of the frozen ranking, as an HTML fragment for the infinite scroll."""
    cfg = _require_cfg(loaded)
    feed = recall(token)
    if feed is None:  # snapshot expired (tab left open for long): rebuild; the page skips duplicates
        feed = build_feed(conn, cfg, named_sources(conn, cfg, loaded.sources), f or request.cookies.get(FILTER_COOKIE) or "all")
        token = remember(feed)
    offset = max(0, offset)
    end = offset + PAGE_SIZE
    return templates.TemplateResponse(request, "_feed_page.html", {
        **template_helpers(loaded), "feed": feed, "page": feed.items[offset:end], "token": token,
        "next_offset": end if end < len(feed.items) else None})


SWIPE_DECK = 30
SWIPE_LATER_COOKIE = "newsnest_swipe_later"  # lead ids swiped up ("later"), written by app.js


@app.get("/swipe", response_class=HTMLResponse)
def swipe(request: Request, conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    """Tinder-style deck over the For you ranking: right = 👍, left = not interested, up = later, tap opens.
    Liked and "later" stories stay in the feed, so the deck leaves them out; impressions and auto-read
    are not tracked here."""
    cfg = _require_cfg(loaded)
    feed = build_feed(conn, cfg, named_sources(conn, cfg, loaded.sources), "all")
    later = set(request.cookies.get(SWIPE_LATER_COOKIE, "").split("."))
    deck = [it for it in feed.items if it.card.lead.row["rating"] != 1 and str(it.card.lead.id) not in later]
    return render(request, "swipe.html", conn, loaded, feed=feed, deck=deck[:SWIPE_DECK], left=len(deck))


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


@app.get("/go/{item_id}")
def go(item_id: int, conn=Depends(get_conn)):
    row = conn.execute("SELECT url, source_id, ai_category, opened_at FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    _mark_story_read(conn, [item_id])
    if row["opened_at"] is None:  # first open only; auto-read on scroll never comes through here
        conn.execute("UPDATE items SET opened_at = ? WHERE id = ?", (iso(utcnow()), item_id))
        nudge(conn, row["source_id"], row["ai_category"], OPEN_NUDGE)
    return RedirectResponse(row["url"], status_code=302)


@app.post("/api/items/{item_id}/rate")
def rate(item_id: int, request: Request, value: int = Form(...), conn=Depends(get_conn)):
    if value not in (-1, 0, 1):
        raise HTTPException(400, "value must be -1, 0 or 1")
    before = conn.execute("SELECT rating, source_id, ai_category FROM items WHERE id = ?", (item_id,)).fetchone()
    conn.execute("UPDATE items SET rating = ?, rated_at = ? WHERE id = ?",
                 (value or None, iso(utcnow()) if value else None, item_id))
    if before is not None and (before["rating"] == 1) != (value == 1):  # 👍 added or taken back
        nudge(conn, before["source_id"], before["ai_category"], LIKE_NUDGE, undo=value != 1)
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
def set_read(request: Request, ids: str = Form(""), value: int = Form(1), token: str = Form(""),
             conn=Depends(get_conn)):
    """Mark stories read (value=1) or unread (value=0); ids are item ids of the stories.
    With a feed token instead of ids: every story of that feed snapshot (Mark all read)."""
    if token:
        feed = recall(token)
        if feed is None:
            raise HTTPException(409, "The page is out of date; reload and try again")
        ids = ",".join(it.card.ids for it in feed.items)
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
def set_seen(ids: str = Form(...), conn=Depends(get_conn)):
    """Impressions from the home feed. Counted at most once per story per 45 min, so reloading the
    page never pushes stories out; a story skipped often enough sinks and then drops out."""
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
        lead = conn.execute("SELECT rating, source_id, ai_category FROM items WHERE id = ?", (id_list[0],)).fetchone()
        if hide and lead is not None and lead["rating"] == 1:  # a liked story disliked: take the like boost back
            nudge(conn, lead["source_id"], lead["ai_category"], LIKE_NUDGE, undo=True)
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
    return render(request, "profile.html", conn, loaded, page="profile", files=files, proposals=proposals, onboarding=onboarding,
                  items=onboarding_items(conn) if onboarding else [], rated=rated, error=request.query_params.get("error"),
                  saved=request.query_params.get("saved"), require_cfg=False, error_prefix="", tuning=tuning_rows(conn, loaded),
                  **settings_context("profile", conn, loaded))


def tuning_rows(conn, loaded: LoadedConfig) -> dict[str, list[dict]]:
    """What the feed learned, in the groups the profile page shows: more, less, and tiny nudges."""
    names = {s.id: s.name for s in named_sources(conn, loaded.config, loaded.sources)} if loaded.config else {}
    more, less, small = [], [], []
    for (kind, key), w in load_weights(conn).items():
        direction, level = weight_strength(w)
        row = {"kind": kind, "key": key, "name": names.get(key, key) if kind == "source" else key, "weight": w,
               "level": level, "outlet": kind == "source"}
        (small if level == 0 else more if direction == "more" else less).append(row)
    return {"more": sorted(more, key=lambda r: -r["weight"]), "less": sorted(less, key=lambda r: r["weight"]),
            "small": sorted(small, key=lambda r: r["name"].lower())}


@app.post("/profile/save/{name}")
def profile_save(name: str, text: str = Form(""), proposal_id: int | None = Form(None), conn=Depends(get_conn)):
    if name not in PROFILE_FILES:
        raise HTTPException(404)
    write_profile(name, text)
    if proposal_id:
        conn.execute("UPDATE proposals SET status = 'accepted', decided_at = ? WHERE id = ?", (iso(utcnow()), proposal_id))
    return RedirectResponse("/profile?saved=" + name, status_code=303)


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
    back = f"/profile/proposals/{pid}"
    if not picked:
        return RedirectResponse(f"{back}?error={quote('Tick at least one change, or dismiss the suggestion')}", status_code=303)
    text, applied, skipped = apply_changes(read_profile("interests.md"), [items[i] for i in sorted(picked)])
    if not applied:
        return RedirectResponse(f"{back}?error={quote('None of these fit your interests as they are now')}", status_code=303)
    write_profile("interests.md", text)
    conn.execute("UPDATE proposals SET status = 'accepted', decided_at = ? WHERE id = ?", (iso(utcnow()), pid))
    return RedirectResponse(f"/profile?saved=applied&skipped={len(skipped)}", status_code=303)


@app.post("/profile/proposals/{pid}/reject")
def proposal_reject(pid: int, conn=Depends(get_conn)):
    conn.execute("UPDATE proposals SET status = 'rejected', decided_at = ? WHERE id = ?", (iso(utcnow()), pid))
    return RedirectResponse("/profile", status_code=303)


# --- settings ---

# Settings is a few short pages, iOS Settings style; every change lands back on the page it was made on
SETTINGS_PAGES = ("general", "feeds", "discover", "topics", "more", "app")
ACTION_PAGE = {"general": "general", "region": "general",
               "feed_add": "feeds", "feed_section": "feeds", "feed_remove": "feeds", "feed_toggle": "feeds",
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
                   "trial": conn.execute("SELECT COUNT(*) FROM candidates WHERE status = 'candidate'").fetchone()[0]}}
    if page == "general":
        ctx.update(spent=spent_today(conn, cfg), time_zones=TIME_ZONES, **local_pack_context(cfg))
    elif page == "profile":
        ctx.update(time_zones=TIME_ZONES, **local_pack_context(cfg))
    elif page == "feeds":
        ctx.update(foreign=foreign_local(conn, loaded.sources, cfg.region.tld), lifestyle_missing=missing_from(LIFESTYLE_PACK, cfg.feeds),
                   lifestyle_interest=LIFESTYLE_INTEREST.lower() in read_profile("interests.md").lower(),
                   **local_pack_context(cfg))
    elif page == "discover":
        ctx.update(candidates=candidate_rows(conn), suggestions=suggestions, starter_missing=starter_missing(conn, cfg),
                   starter_total=len(parse_opml(STARTER_PACK.read_bytes())))
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


@app.get("/settings/{page}", response_class=HTMLResponse)
def settings_page(page: str, request: Request, conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    if page not in SETTINGS_PAGES:
        raise HTTPException(404)
    return _settings(request, page, conn, loaded)


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
    back = "/calibration" if action in CALIBRATION_ACTIONS else f"/settings/{ACTION_PAGE.get(action, 'general')}"
    if action == "region" and _onboarding_next(form):
        back = "/profile"
    if "category" in form and form["category"] not in SECTIONS:
        return RedirectResponse(f"{back}?error={quote('unknown section ' + form['category'])}", status_code=303)

    added = {}

    def change(data):
        if action == "feed_add":
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
    if action in ("feed_section", "feed_toggle"):
        anchor = f"#feed-{form['id']}"
    elif action == "feed_add":
        anchor = f"#feed-{added['id']}"
    return RedirectResponse(f"{back}?saved={action}{extra}{anchor}", status_code=303)


# --- discovered sources ---

def candidate_rows(conn) -> list[dict]:
    """Candidates with story counts and `signals`: opens and likes this week, the number promotion counts."""
    since = iso(utcnow() - timedelta(days=7))
    rows = conn.execute(
        """SELECT c.*, (SELECT COUNT(*) FROM items i WHERE i.source_id = c.source_id) AS items,
                  (SELECT COUNT(*) FROM items i WHERE i.source_id = c.source_id AND (i.opened_at IS NOT NULL OR i.rating = 1)) AS liked
           FROM candidates c ORDER BY CASE c.status WHEN 'candidate' THEN 0 WHEN 'checking' THEN 1 ELSE 2 END, c.title""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["check"] = loads(r["check_result"], {}) or {}
        d["signals"] = _signals(conn, r["source_id"], since) if r["status"] == "candidate" else 0
        out.append(d)
    return out


@app.post("/discovery/starter")
def discovery_starter(conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    cfg = _require_cfg(loaded)
    added = add_candidates(conn, cfg, parse_opml(STARTER_PACK.read_bytes()), "starter pack")
    start_check_thread(cfg)
    return RedirectResponse(f"/settings/discover?saved=discovery&added={added}", status_code=303)


@app.post("/discovery/import")
async def discovery_import(request: Request, loaded: LoadedConfig = Depends(get_loaded)):
    """OPML from an uploaded file or a link; feeds go to the candidate pool and are checked in the background."""
    cfg = _require_cfg(loaded)
    form = await request.form()
    upload, url = form.get("file"), (form.get("url") or "").strip()
    category = form.get("category") or "tech"
    try:
        if upload is not None and getattr(upload, "filename", ""):
            data = await upload.read()
            origin = upload.filename
        elif url.startswith("https://"):
            import httpx
            r = httpx.get(url, follow_redirects=True, timeout=20, headers={"User-Agent": cfg.http.user_agent})
            r.raise_for_status()
            data, origin = r.content, url
        else:
            raise ValueError("choose an OPML file or paste an https link to one")
        feeds = parse_opml(data, category, region_words(cfg))
    except Exception as e:  # bad file, bad link, not OPML
        from urllib.parse import quote
        return RedirectResponse(f"/settings/discover?error={quote(f'OPML import failed: {e}'[:300])}", status_code=303)
    with session() as conn:
        added = add_candidates(conn, cfg, feeds, origin[:120])
    start_check_thread(cfg)
    return RedirectResponse(f"/settings/discover?saved=discovery&added={added}", status_code=303)


@app.post("/discovery/{source_id}/{action}")
def discovery_action(source_id: str, action: str, conn=Depends(get_conn), loaded: LoadedConfig = Depends(get_loaded)):
    _require_cfg(loaded)
    row = conn.execute("SELECT * FROM candidates WHERE source_id = ?", (source_id,)).fetchone()
    if not row or action not in ("promote", "remove", "restore"):
        raise HTTPException(404)
    if action == "promote":
        category = row["category"]

        def change(data):
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
