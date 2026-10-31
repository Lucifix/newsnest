import time
from datetime import datetime, timedelta, timezone
from email.utils import formatdate

import httpx
import pytest
from fastapi.testclient import TestClient

from newsnest.config import Source
from newsnest.db import iso
from newsnest.discovery import (STARTER_PACK, OpmlFeed, add_candidates, cap_trial_scoring, check_feed, discovery_sources, named_sources,
                             parse_opml, promotion_suggestions, retire_candidates)
from newsnest.feed import build_feed
from newsnest.fetchers.base import FetchContext, run_fetch
from newsnest.fetchers.rss import RssFetcher
from newsnest.models import FetchStatus, Item
from newsnest.pipeline import ingest
from newsnest.profile import write_profile
from newsnest.watches import Prefilter

NOW = datetime.now(timezone.utc)


def rss(entries: int = 3, days_old: float = 0, enclosure: str | None = None, title: str = "Story {i}") -> str:
    items = []
    for i in range(entries):
        pub = formatdate(time.time() - days_old * 86400 - i * 3600, usegmt=True)
        enc = f'<enclosure url="https://x.example/{i}.mp3" type="{enclosure}"/>' if enclosure else ""
        items.append(f"<item><title>{title.format(i=i)}</title><link>https://x.example/{i}</link><pubDate>{pub}</pubDate>{enc}</item>")
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>Example</title>{"".join(items)}</channel></rss>'


def client_for(routes: dict) -> httpx.Client:
    def handler(request: httpx.Request):
        body, status = routes.get(str(request.url), ("", 404))
        return httpx.Response(status, text=body, headers={"content-type": "application/rss+xml", "etag": '"v1"'})
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_starter_pack_parses_with_categories():
    feeds = parse_opml(STARTER_PACK.read_bytes())
    assert len(feeds) >= 40
    assert {f.category for f in feeds} == {"world", "tech"}
    assert all(f.url.startswith("https://") for f in feeds)


def test_opml_folder_maps_category():
    opml = """<opml><body><outline text="Cyber security"><outline xmlUrl="https://a.example/feed" text="A"/></outline>
              <outline text="Science"><outline xmlUrl="https://b.example/feed" text="B"/></outline>
              <outline text="Slovenske novice"><outline xmlUrl="https://c.example/feed" text="C"/></outline></body></opml>"""
    # Local only when the folder says so; anything else that isn't tech is World
    assert [(f.title, f.category) for f in parse_opml(opml)] == [("A", "tech"), ("B", "world"), ("C", "news")]


def test_check_feed_rules():
    c = client_for({
        "https://ok.example/feed": (rss(), 200),
        "https://pod.example/feed": (rss(enclosure="audio/mpeg"), 200),
        "https://old.example/feed": (rss(days_old=40), 200),
        "https://sl.example/feed": (rss(title="Vlada je sprejela nov zakon o plačah {i}"), 200),
    })
    assert check_feed(c, "https://ok.example/feed").ok
    assert check_feed(c, "https://ok.example/feed").slovenian is False
    assert check_feed(c, "https://sl.example/feed").slovenian is True
    assert check_feed(c, "https://pod.example/feed").reason == "podcast or video channel"
    assert "no new post" in check_feed(c, "https://old.example/feed").reason
    assert check_feed(c, "http://plain.example/feed").reason.startswith("plain http")
    assert check_feed(c, "https://gone.example/feed").reason == "HTTP 404"


def test_add_candidates_skips_known_feeds(conn, cfg):
    feeds = [OpmlFeed("https://www.24ur.com/rss", "24ur again", "news"),  # already a regular source
             OpmlFeed("https://new.example/feed", "New one", "tech"),
             OpmlFeed("https://new.example/feed/", "New one twice", "tech")]
    assert add_candidates(conn, cfg, feeds, "test") == 1
    assert add_candidates(conn, cfg, feeds, "test") == 0


def test_conditional_get_returns_304_without_items(cfg):
    calls = []

    def handler(request: httpx.Request):
        calls.append(request.headers.get("if-none-match"))
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, text=rss(), headers={"content-type": "application/rss+xml", "etag": '"v1"'})

    ctx = FetchContext(cfg, httpx.Client(transport=httpx.MockTransport(handler)))
    src = Source(id="x", type="rss", category="tech", name="X", url="https://x.example/feed")
    first = run_fetch(RssFetcher(), src, ctx)
    assert first.status == FetchStatus.OK and len(first.items) == 3 and first.etag == '"v1"'
    src.params.update(etag=first.etag, prev_raw=first.raw_count)
    second = run_fetch(RssFetcher(), src, ctx)
    assert second.http_status == 304 and second.items == [] and second.raw_count == 3
    assert calls == [None, '"v1"']


@pytest.fixture
def trial(conn, cfg):
    """A live candidate with two scored stories: one weak, one strong."""
    write_profile("interests.md", "## Want\n- science\n")
    cfg.scoring.calibration_mode = True  # discovered stories must clear the bar even here
    add_candidates(conn, cfg, [OpmlFeed("https://sci.example/feed", "Sci Daily", "news")], "test")
    conn.execute("UPDATE candidates SET status = 'candidate', checked_at = ?", (iso(NOW),))
    src = discovery_sources(conn, cfg)[0]
    ingest(conn, src, [Item(src.id, "news", "Weak story about nothing much", "https://sci.example/1", "1", NOW),
                       Item(src.id, "news", "Strong story about fusion power", "https://sci.example/2", "2", NOW)],
           Prefilter(cfg), NOW)
    conn.execute("UPDATE items SET score_status='scored', topic=4, importance=4, personal=4, ai_category='Science' WHERE url LIKE '%/1'")
    conn.execute("UPDATE items SET score_status='scored', topic=8, importance=6, personal=8, ai_category='Science' WHERE url LIKE '%/2'")
    return src


def test_discovered_stories_need_the_threshold_and_are_tagged(conn, cfg, trial):
    feed = build_feed(conn, cfg, named_sources(conn, cfg, []))
    assert [it.card.lead.row["url"] for it in feed.items] == ["https://sci.example/2"]
    assert feed.items[0].discovered and feed.items[0].card.lead.source_name == "Sci Daily"


def test_promotion_suggestion_and_promote(conn, cfg, env, trial, monkeypatch):
    conn.execute("UPDATE items SET opened_at = ?", (iso(NOW),))
    conn.execute("UPDATE items SET rating = 1, rated_at = ? WHERE url LIKE '%/1'", (iso(NOW),))
    assert promotion_suggestions(conn, cfg) == []  # 2 signals, needs 3
    ingest(conn, trial, [Item(trial.id, "news", "Third story on quantum sensors", "https://sci.example/3", "3", NOW)],
           Prefilter(cfg), NOW)
    conn.execute("UPDATE items SET opened_at = ? WHERE url LIKE '%/3'", (iso(NOW),))
    assert [s["title"] for s in promotion_suggestions(conn, cfg)] == ["Sci Daily"]
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    from newsnest.web import app
    with TestClient(app) as client:
        assert "earned a place" in client.get("/?f=all").text
        client.post(f"/discovery/{trial.id}/promote")
    assert "url: https://sci.example/feed" in (env.config / "config.yaml").read_text(encoding="utf-8")
    assert conn.execute("SELECT status FROM candidates").fetchone()[0] == "promoted"
    assert discovery_sources(conn, cfg) == []


def test_trial_sources_score_only_their_quota_newest_first(conn, cfg, trial):
    cfg.discovery.scored_per_source = 3
    conn.execute("UPDATE items SET scored_at = ?", (iso(NOW),))  # the fixture's 2 stories used 2 of 3
    ingest(conn, trial, [Item(trial.id, "news", f"Story number {i} on new materials", f"https://sci.example/n{i}", f"n{i}",
                              NOW - timedelta(hours=5 - i)) for i in range(3)]
           + [Item(trial.id, "news", "Home Assistant adds a fusion dashboard", "https://sci.example/ha", "ha",
                   NOW - timedelta(hours=9))], Prefilter(cfg), NOW)
    assert cap_trial_scoring(conn, cfg) == 2
    status = dict(conn.execute("SELECT url, score_status FROM items WHERE score_status != 'scored'").fetchall())
    assert status == {"https://sci.example/n2": "pending",  # the newest takes the last slot
                      "https://sci.example/n1": "capped", "https://sci.example/n0": "capped",
                      "https://sci.example/ha": "pending"}  # watched: always scored
    assert cap_trial_scoring(conn, cfg) == 0


def test_retirement(conn, cfg, trial):
    assert retire_candidates(conn, cfg) == []  # still in its trial period
    conn.execute("UPDATE candidates SET checked_at = ?", (iso(NOW - timedelta(days=30)),))
    assert retire_candidates(conn, cfg) == []  # it did produce a good story
    conn.execute("UPDATE items SET topic = 3, importance = 3, personal = 3")
    assert retire_candidates(conn, cfg) == [trial.id]


def test_settings_panel_and_starter_import(conn, cfg, env, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    import newsnest.web as web
    monkeypatch.setattr(web, "start_check_thread", lambda cfg: None)  # no network in tests
    with TestClient(web.app) as client:
        page = client.get("/settings/discover").text
        assert "Feeds on trial" in page and 'action="/discovery/starter"' in page
        r = client.post("/discovery/starter", follow_redirects=False)
        assert r.headers["location"].startswith("/settings/discover?") and "added=" in r.headers["location"]
        # once every starter feed is known the button goes away; a feed missing again brings back "Add 1 new"
        page = client.get("/settings/discover").text
        assert 'action="/discovery/starter"' not in page and "Starter pack added" in page
        conn.execute("DELETE FROM candidates WHERE id = (SELECT MIN(id) FROM candidates)")
        assert "Add 1 new starter feed</span>" in client.get("/settings/discover").text
    assert conn.execute("SELECT COUNT(*) FROM candidates WHERE status = 'checking'").fetchone()[0] >= 40


def test_trial_rows_show_progress_toward_promotion(conn, cfg, env, trial, monkeypatch):
    """"N of 3" is the weekly count promotion uses, and each row's ⋯ menu carries Keep and Remove."""
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    import newsnest.web as web
    with TestClient(web.app) as client:
        need = cfg.discovery.promote_after
        page = client.get("/settings/discover").text
        assert f"0 of {need}" in page and f"/discovery/{trial.id}/promote" in page and f"/discovery/{trial.id}/remove" in page
        conn.execute("UPDATE items SET opened_at = ?", (iso(NOW),))  # two stories opened this week
        assert f"2 of {need}" in client.get("/settings/discover").text
        conn.execute("UPDATE items SET opened_at = ?", (iso(NOW - timedelta(days=9)),))  # too long ago to count
        assert f"0 of {need}" in client.get("/settings/discover").text
