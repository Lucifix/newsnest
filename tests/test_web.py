from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from newsnest.config import Source
from newsnest.db import connect, iso, migrate
from newsnest.models import Item
from newsnest.pipeline import ingest
from newsnest.watches import Prefilter

NOW = datetime.now(timezone.utc)


@pytest.fixture
def client(env, cfg, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    conn = connect()
    migrate(conn)
    pf = Prefilter(cfg)
    s1 = Source(id="24ur", type="rss", category="news", name="24ur", url="https://24ur.com/rss")
    s2 = Source(id="n1", type="rss", category="news", name="N1 Slovenija", url="https://n1info.si/feed/")
    s3 = Source(id="theverge", type="rss", category="tech", name="The Verge", url="https://theverge.com/rss")
    ingest(conn, s1, [Item("24ur", "news", "Papežu Leonu XIV. se rak ni razširil", "https://24ur.com/a", "a", NOW,
                           snippet="Vatikan je sporočil ...")], pf, NOW)
    ingest(conn, s2, [Item("n1", "news", "Papežu se rak ni razširil", "https://n1info.si/a", "b", NOW)], pf, NOW)
    ingest(conn, s1, [Item("24ur", "news", "Ljubljana zapira Slovensko cesto", "https://24ur.com/b", "c", NOW)], pf, NOW)
    ingest(conn, s3, [Item("theverge", "tech", "Airbnb bans making guests do dishes", "https://theverge.com/x", "d", NOW)], pf, NOW)
    conn.execute("UPDATE items SET score_status='scored', topic=8, importance=9, personal=7, reason='big news', "
                 "ai_category='World', scored_at=? WHERE url='https://24ur.com/a'", (iso(NOW),))
    conn.close()
    from newsnest.web import app
    with TestClient(app) as c:
        yield c


def test_digest_without_profile_groups_by_source(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Teach NewsNest what you care about" in r.text
    assert "Papežu Leonu XIV. se rak ni razširil" in r.text and "The Verge" in r.text


def test_digest_with_profile_shows_top_story_and_cross_source(client, env):
    (env.config / "interests.md").write_text("## Want\n- world news\n", encoding="utf-8")
    r = client.get("/")
    assert r.status_code == 200
    assert "Top story" in r.text and "big news" in r.text
    assert "Full coverage · 2 articles from 2 outlets" in r.text  # N1 grouped into the 24ur story
    assert "Ljubljana" in r.text  # watch tag


def test_every_page_renders(client):
    for path in ["/archive", "/status", "/calibration", "/missed", "/profile", "/settings", f"/day/{NOW.date()}",
                 "/settings/general", "/settings/feeds", "/settings/discover", "/settings/topics", "/settings/more", "/settings/app"]:
        r = client.get(path)
        assert r.status_code == 200, path
    assert client.get("/settings/nope").status_code == 404


def test_settings_menu_and_general_autosave(client, env):
    root = client.get("/settings").text
    assert 'class="settings is-root"' in root and 'href="/settings/feeds"' in root and "Profile &amp; tuning" in root
    page = client.get("/settings/topics").text
    assert 'class="settings"' in page and 'href="/settings/topics" aria-current="page"' in page
    r = client.post("/settings/general", data={"min_final": "4.5", "daily_usd": "0.75", "summary_language": "Slovenian",
                                                "auto_read_on_scroll": "on"}, follow_redirects=False)
    assert r.headers["location"] == "/settings/general?saved=general"
    page = client.get(r.headers["location"]).text
    assert 'data-toast="Saved"' in page and 'role="switch" name="auto_read_on_scroll" checked' in page
    text = (env.config / "config.yaml").read_text(encoding="utf-8")
    assert "min_final: 4.5" in text and "daily_usd: 0.75" in text
    # a bad value comes back as an error on the same page
    r = client.post("/settings/general", data={"min_final": "x", "daily_usd": "1", "summary_language": ""}, follow_redirects=False)
    assert r.headers["location"].startswith("/settings/general?error=")


def test_database_backup_download(client, tmp_path):
    import sqlite3
    r = client.get("/settings/backup/database")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"] and "newsnest-" in r.headers["content-disposition"]
    path = tmp_path / "restored.db"
    path.write_bytes(r.content)
    copy = sqlite3.connect(path)
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    live = connect()
    assert copy.execute("SELECT COUNT(*) FROM items").fetchone()[0] == live.execute("SELECT COUNT(*) FROM items").fetchone()[0] > 0
    live.close()
    copy.close()
    assert not list(tmp_path.glob("*.db-wal"))


def test_health_reports_problems(client):
    body = client.get("/health").json()
    assert body["sources"] == 26
    assert body["status"] == "degraded"  # scheduler has not run in tests
    assert any("scheduler" in p for p in body["problems"])


def test_rating_round_trip(client):
    r = client.post("/api/items/1/rate", data={"value": "1"}, headers={"Accept": "application/json"})
    assert r.json() == {"id": 1, "rating": 1}
    conn = connect()
    assert conn.execute("SELECT rating FROM items WHERE id = 1").fetchone()[0] == 1
    client.post("/api/items/1/rate", data={"value": "0"}, headers={"Accept": "application/json"})
    assert conn.execute("SELECT rating FROM items WHERE id = 1").fetchone()[0] is None


def test_go_marks_read_and_redirects(client):
    r = client.get("/go/1", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://24ur.com/a"
    assert connect().execute("SELECT read_at FROM items WHERE id = 1").fetchone()[0]


def test_settings_edit_keeps_comments_and_validates(client, env):
    path = env.config / "config.yaml"
    before = path.read_text(encoding="utf-8")
    r = client.post("/settings/feed_add", data={"name": "Primorske novice", "url": "https://www.primorske.si/rss",
                                                 "category": "news"}, follow_redirects=False)
    assert r.headers["location"] == "/settings/feeds?saved=feed_add#feed-primorske-novice"
    after = path.read_text(encoding="utf-8")
    assert "id: primorske-novice" in after
    assert "# Plain RSS / Atom feeds" in after  # comments survive
    client.post("/settings/feed_remove", data={"id": "primorske-novice"})
    assert path.read_text(encoding="utf-8") == before  # round trip is lossless
    # the same address twice is refused (twice the requests ends in HTTP 429 and the red banner)
    r = client.post("/settings/feed_add", data={"name": "24ur again", "url": "https://www.24ur.com/rss/",
                                                 "category": "news"}, follow_redirects=False)
    assert "already+a+feed" in r.headers["location"] or "already%20a%20feed" in r.headers["location"]
    assert path.read_text(encoding="utf-8") == before
    # http feed is rejected by validation and nothing is written
    r = client.post("/settings/feed_add", data={"name": "Bad", "url": "http://bad.example/rss", "category": "news"},
                    follow_redirects=False)
    assert "error=" in r.headers["location"]
    assert path.read_text(encoding="utf-8") == before
    # moving a feed to another section takes its stored stories along
    client.post("/settings/feed_section", data={"id": "24ur", "category": "world"})
    assert {r[0] for r in connect().execute("SELECT category FROM items WHERE source_id = '24ur'")} == {"world"}
    client.post("/settings/feed_section", data={"id": "24ur", "category": "news"})
    assert path.read_text(encoding="utf-8") == before
    assert "error=" in client.post("/settings/feed_section", data={"id": "24ur", "category": "local"},
                                   follow_redirects=False).headers["location"]


def test_lifestyle_pack(client, env):
    from newsnest.packs import LIFESTYLE_PACK
    from newsnest.profile import read_profile
    assert "Lifestyle &amp; gear pack" in client.get("/settings/feeds").text
    assert 'href="/?f=lifestyle"' not in client.get("/settings/feeds").text  # no tab until there are feeds
    r = client.post("/settings/pack/lifestyle", data={"interest": "1"}, follow_redirects=False)
    assert r.headers["location"].startswith(f"/settings/feeds?pack={len(LIFESTYLE_PACK)}&")
    text = (env.config / "config.yaml").read_text(encoding="utf-8")
    assert "id: gear-patrol" in text and "daily_cap: 8" in text
    assert "Men's lifestyle and gear" in read_profile("interests.md")
    page = client.get("/settings/feeds").text
    assert "Lifestyle &amp; gear pack" not in page and 'href="/?f=lifestyle"' in page
    # a second tap adds nothing and the interest line isn't repeated
    client.post("/settings/pack/lifestyle", data={"interest": "1"})
    assert read_profile("interests.md").count("Men's lifestyle and gear") == 1
    assert (env.config / "config.yaml").read_text(encoding="utf-8") == text


def test_settings_watch_and_keywords(client, env):
    client.post("/settings/watch_add", data={"name": "Energy", "keywords": "elektrik, cene energ"})
    client.post("/settings/keywords", data={"mute": "horoskop\nnagradna igra", "must_show": ""})
    from newsnest.config import load_config
    cfg = load_config(env.config).config
    assert any(w.name == "Energy" and w.keywords == ["elektrik", "cene energ"] for w in cfg.watches)
    assert cfg.prefilter.mute_keywords == ["horoskop", "nagradna igra"]


def test_profile_save_and_proposal_approve(client, env):
    client.post("/profile/save/seed_examples.md", data={"text": "+ Ljubljana uvaja nove parkirne cone\n"})
    assert "parkirne" in (env.config / "seed_examples.md").read_text(encoding="utf-8")
    conn = connect()
    pid = conn.execute("INSERT INTO proposals (created_at, kind, status, current_text, proposed_text, rationale) "
                       "VALUES (?, 'weekly', 'pending', 'old', '## Want\n- new\n', 'because')", (iso(NOW),)).lastrowid
    page = client.get(f"/profile/proposals/{pid}")
    assert page.status_code == 200 and "because" in page.text
    client.post("/profile/save/interests.md", data={"text": "## Want\n- new\n", "proposal_id": str(pid)})
    assert conn.execute("SELECT status FROM proposals WHERE id = ?", (pid,)).fetchone()[0] == "accepted"
    assert (env.config / "interests.md").read_text(encoding="utf-8") == "## Want\n- new\n"


def test_missed_known_url_without_ai(client):
    client.post("/missed", data={"url": "https://24ur.com/b"})
    page = client.get("/missed")
    assert "We had it" in page.text and "AI unavailable" in page.text


def test_seen_and_read_state(client):
    page = client.get("/").text
    assert page.count('class="pill new"') >= 3  # nothing seen yet
    client.post("/api/seen", data={"ids": "1,2"})
    conn = connect()
    assert conn.execute("SELECT COUNT(*) FROM items WHERE seen_at IS NOT NULL").fetchone()[0] == 2
    # read applies to every outlet's version of the story
    r = client.post("/api/read", data={"ids": "1,2", "value": "1"}, headers={"Accept": "application/json"})
    assert r.json() == {"ids": [1, 2], "read": True}
    assert conn.execute("SELECT COUNT(*) FROM items WHERE read_at IS NOT NULL").fetchone()[0] == 2
    client.post("/api/read", data={"ids": "1,2", "value": "0"}, headers={"Accept": "application/json"})
    assert conn.execute("SELECT COUNT(*) FROM items WHERE read_at IS NOT NULL").fetchone()[0] == 0
    assert client.post("/api/read", data={"ids": "1,x"}).status_code == 400


def test_mark_all_read_empties_the_feed(client):
    page = client.get("/").text
    assert 'class="readall"' in page
    token = page.split('class="readall"')[1].split('name="token" value="')[1].split('"')[0]
    client.post("/api/read", data={"token": token, "value": "1"})
    assert client.post("/api/read", data={"token": "expired", "value": "1"}).status_code == 409
    page = client.get("/").text
    assert "You're all caught up" in page
    assert "Papežu Leonu XIV" in client.get("/read").text
    r = client.get("/more", follow_redirects=False)  # the old hub: Settings holds all of it now
    assert r.status_code == 303 and r.headers["location"] == "/settings"


def test_save_for_later(client):
    assert "Nothing saved yet" in client.get("/saved").text
    assert 'class="act savetoggle"' in client.get("/").text and "Saved</a>" in client.get("/").text
    r = client.post("/api/saved", data={"ids": "1", "value": "1"}, headers={"Accept": "application/json"})
    assert r.json() == {"ids": [1], "saved": True}
    conn = connect()
    # saving applies to every outlet's version of the story, and to nothing else
    assert [row[0] for row in conn.execute("SELECT id FROM items WHERE saved_at IS NOT NULL ORDER BY id")] == [1, 2]
    # a bookmark only: no read state, rating or ranking change
    assert conn.execute("SELECT COUNT(*) FROM items WHERE read_at IS NOT NULL OR rating IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM weights").fetchone()[0] == 0
    assert 'aria-label="Remove from saved"' in client.get("/").text
    # saved stories outlive the feed window
    conn.execute("UPDATE items SET fetched_at = '2020-01-01T00:00:00+00:00', published = '2020-01-01T00:00:00+00:00'")
    page = client.get("/saved").text
    assert "Papežu Leonu XIV" in page and "Slovensko cesto" not in page and "saved " in page
    client.post("/api/saved", data={"ids": "2", "value": "0"})
    assert conn.execute("SELECT COUNT(*) FROM items WHERE saved_at IS NOT NULL").fetchone()[0] == 0
    assert "Nothing saved yet" in client.get("/saved").text
    assert client.post("/api/saved", data={"ids": "1,x"}).status_code == 400


def test_cards_have_one_tap_feedback_and_fresh_assets(client, env):
    (env.config / "interests.md").write_text("## Want\n- world news\n", encoding="utf-8")
    r = client.get("/")
    from newsnest.web import ASSET_VERSION
    assert f"/static/app.js?v={ASSET_VERSION}" in r.text
    assert r.headers["cache-control"] == "no-cache"
    cards = r.text.count('<article class="story')
    assert cards and r.text.count('class="act like"') == cards and r.text.count('class="act dislike"') == cards
    assert 'data-action="like"' not in r.text  # like/dislike live on the card, not in the menu


def test_pwa_manifest_service_worker_and_icons(client):
    import json
    m = client.get("/manifest.webmanifest")
    assert m.headers["content-type"].startswith("application/manifest+json")
    data = json.loads(m.text)
    assert data["display"] == "standalone" and data["start_url"] == "/" and data["scope"] == "/"
    sizes = {(i["sizes"], i["purpose"]) for i in data["icons"]}
    assert ("192x192", "any") in sizes and ("512x512", "any") in sizes and ("512x512", "maskable") in sizes
    for icon in data["icons"]:  # every icon in the manifest actually exists
        assert client.get(icon["src"]).status_code == 200
    from newsnest.web import ASSET_VERSION
    sw = client.get("/sw.js")
    assert sw.headers["content-type"].startswith("text/javascript") and sw.headers["cache-control"] == "no-cache"
    assert "newsnest-shell-" in sw.text and ASSET_VERSION in sw.text and "__VERSION__" not in sw.text
    assert "/offline" in sw.text and client.get("/offline").status_code == 200
    page = client.get("/").text
    assert 'rel="manifest"' in page and 'rel="apple-touch-icon"' in page
    assert 'id="install"' in client.get("/settings/app").text


def test_for_you_tab_leaves_a_remembered_section(client):
    client.get("/?f=tech")  # remembered in the filter cookie, so a bare / would reopen Tech
    page = client.get("/?f=tech").text
    assert 'href="/?f=all"' in page
    assert '<a href="/?f=tech" data-tab="tech" aria-current="page">' in page
    page = client.get("/?f=all").text
    assert '<a href="/?f=all" data-tab="all" aria-current="page">' in page
    # News is one tab for Local and World: a Local or World filter lights it, and an old link still works
    for f in ("headlines", "news", "world", "cat:news:Slovenia"):
        page = client.get(f"/?f={f}").text
        assert '<a href="/?f=headlines" data-tab="headlines" aria-current="page">' in page, f
    assert 'href="/?f=world"' not in client.get("/?f=all").text  # World is a chip inside News, not a tab


def test_swipe_deck(client):
    assert 'href="/swipe"' in client.get("/").text  # Swipe is in the ⋯ menu while it is being tried out
    page = client.get("/swipe")
    assert page.status_code == 200
    assert page.text.count('<article class="swipecard') == 3 and 'data-left>3<' in page.text
    assert 'href="/go/1"' in page.text  # a tap opens the story through /go like the feed does
    assert "data-track-seen" not in page.text  # the deck counts no impressions
    # right (👍), left (not interested) and up (later, a cookie) all leave the deck
    client.post("/api/items/1/rate", data={"value": "1"})
    client.post("/api/feedback", data={"action": "not_interested", "ids": "3"})
    page = client.get("/swipe").text
    assert page.count('<article class="swipecard') == 1 and "Airbnb" in page
    client.cookies.set("newsnest_swipe_later", "4")
    page = client.get("/swipe").text
    assert '<article class="swipecard' not in page and "All sorted" in page
    client.cookies.clear()
    assert "Papežu Leonu XIV" in client.get("/").text  # a liked story stays in the feed


def test_settings_pages_use_rows_menus_and_chips(client, env):
    feeds = client.get("/settings/feeds").text
    # a feed is one row: its section in a select, Pause and Remove in the ⋯ menu
    assert 'id="feed-24ur"' in feeds and 'action="/settings/feed_section"' in feeds and "rowmenu-btn" in feeds
    assert 'action="/settings/feed_toggle"' in feeds and 'data-confirm="Remove 24ur?"' in feeds
    assert 'class="seg' not in feeds  # the four-button switch is gone
    topics = client.get("/settings/topics").text
    assert 'data-chipbox' in topics and 'name="mute"' in topics and 'name="must_show"' in topics
    client.post("/settings/keywords", data={"mute": "horoskop\nnagradna igra", "must_show": "potres"})
    topics = client.get("/settings/topics").text
    assert "horoskop" in topics and "potres" in topics
    more = client.get("/settings/more").text
    assert "Add a repository" in more and "gnews" not in more
    r = client.post("/settings/repo_add", data={"repo": "immich-app/immich"}, follow_redirects=False)
    assert r.headers["location"] == "/settings/more?saved=repo_add"
    assert "immich-app/immich" in client.get("/settings/more").text


def test_profile_tuning_groups_and_reset(client):
    conn = connect()
    page = client.get("/profile").text
    assert "How your feed is tuned" in page and "Nothing yet" in page and "Forget all tuning" not in page
    for kind, key, w in [("source", "24ur", 1.6), ("category", "Sport", 0.75), ("category", "Kultura", 1.03)]:
        conn.execute("INSERT INTO weights (kind, key, weight, updated_at) VALUES (?, ?, ?, ?)", (kind, key, w, iso(NOW)))
    page = client.get("/profile").text
    more, less, small = page.index("Showing more"), page.index("Showing less"), page.index("Small adjustments")
    title = lambda name: page.index(f'class="srow-title">{name}<')
    assert more < title("24ur") < less < title("Sport") < small < title("Kultura")
    assert 'aria-label="Back to normal: Sport"' in page and "Forget all tuning" in page
    r = client.post("/api/weights/reset", data={"kind": "category", "key": "Sport"}, follow_redirects=False)
    assert r.headers["location"] == "/profile#tuning"
    assert conn.execute("SELECT COUNT(*) FROM weights").fetchone()[0] == 2
    client.post("/api/weights/reset")
    assert conn.execute("SELECT COUNT(*) FROM weights").fetchone()[0] == 0


def test_proposal_page_lists_changes_and_applies_the_ticked_ones(client, env):
    import json
    conn = connect()
    (env.config / "interests.md").write_text("<!-- keep me -->\n## Want\n- Linux\n- AI coding\n\n## Skip\n- Sports\n", encoding="utf-8")
    items = [{"op": "add", "section": "want", "old": "", "new": "Store recalls", "reason": "rated up 3 recall stories"},
             {"op": "reword", "section": "want", "old": "Linux", "new": "Linux and homelab", "reason": "opened homelab stories"},
             {"op": "remove", "section": "skip", "old": "Sports", "new": "", "reason": "you opened two sports stories"}]
    basis = {"ratings": 46, "missed": 3, "choices": 12, "since": "2026-10-22T08:00:00+00:00"}
    pid = conn.execute("INSERT INTO proposals (created_at, kind, status, current_text, proposed_text, rationale, changes) "
                       "VALUES (?, 'weekly', 'pending', 'old', 'proposed', 'Three small changes.', ?)",
                       (iso(NOW), json.dumps({"basis": basis, "items": items}))).lastrowid
    assert "Suggested changes · 3" in client.get("/profile").text
    page = client.get(f"/profile/proposals/{pid}").text
    assert "Based on 46 ratings, 3 missed stories and 12 more/less choices since 22.10." in page
    assert "<ins>and homelab</ins>" in page and "rated up 3 recall stories" in page and page.count('name="accept"') == 3
    # tick only the first two; the Skip bullet stays
    r = client.post(f"/profile/proposals/{pid}/apply", data={"accept": ["0", "1"]}, follow_redirects=False)
    assert r.headers["location"] == "/profile?saved=applied&skipped=0"
    assert (env.config / "interests.md").read_text(encoding="utf-8") == (
        "<!-- keep me -->\n## Want\n- Linux and homelab\n- AI coding\n- Store recalls\n\n## Skip\n- Sports\n")
    assert conn.execute("SELECT status FROM proposals WHERE id = ?", (pid,)).fetchone()[0] == "accepted"
    assert client.post(f"/profile/proposals/{pid}/apply", data={"accept": ["2"]}).status_code == 404  # already decided


def test_proposal_apply_needs_a_choice_and_old_proposals_still_render(client, env):
    import json
    conn = connect()
    pid = conn.execute("INSERT INTO proposals (created_at, kind, status, changes, current_text, proposed_text, rationale) "
                       "VALUES (?, 'weekly', 'pending', ?, '', '', 'x')", (iso(NOW), json.dumps({"basis": {}, "items": [
                           {"op": "add", "section": "want", "old": "", "new": "Recalls", "reason": "r"}]}))).lastrowid
    r = client.post(f"/profile/proposals/{pid}/apply", data={}, follow_redirects=False)
    assert "error=Tick" in r.headers["location"] and conn.execute("SELECT status FROM proposals WHERE id = ?", (pid,)).fetchone()[0] == "pending"
    # made before changes were listed: only the bullets that differ, no line diff
    old = conn.execute("INSERT INTO proposals (created_at, kind, status, current_text, proposed_text, rationale) VALUES "
                       "(?, 'weekly', 'pending', '## Want\n- A\n- B\n', '## Want\n- A\n- C\n', 'because')", (iso(NOW),)).lastrowid
    page = client.get(f"/profile/proposals/{old}").text
    assert "What differs" in page and 'optag remove">Remove' in page and ">B<" in page and ">C<" in page and "@@" not in page


def test_app_menu_is_short_and_settings_holds_the_rest(client):
    menu = client.get("/").text.split('id="appmenu"')[1].split("</nav>")[0]
    for gone in ("/profile", "/status", "/calibration", "/more"):
        assert f'href="{gone}"' not in menu
    for kept in ("/saved", "/swipe", "/archive", "/missed", "/settings"):
        assert f'href="{kept}"' in menu
    hub = client.get("/settings").text
    assert 'href="/calibration"' in hub and 'href="/status"' in hub and 'href="/settings/app"' in hub
    app = client.get("/settings/app").text
    assert 'id="install"' in app and 'href="/logout"' in app


def test_a_connection_can_move_between_threads(conn):
    """A request's connection is opened in one worker thread and used in another."""
    import threading
    out = []
    t = threading.Thread(target=lambda: out.append(conn.execute("SELECT 1").fetchone()[0]))
    t.start(); t.join()
    assert out == [1]


def test_calibration_page_picks_a_limit_in_one_tap(client, env):
    page = client.get("/calibration").text
    assert "Where to cut off" in page and 'action="/settings/threshold"' in page and "Calibration mode" in page
    assert 'href="/calibration" aria-current="page"' in page  # lit in the settings menu
    r = client.post("/settings/threshold", data={"min_final": "5.0"}, follow_redirects=False)
    assert r.headers["location"] == "/calibration?saved=threshold"
    assert "min_final: 5.0" in (env.config / "config.yaml").read_text(encoding="utf-8")
    before = "calibration_mode: true" in (env.config / "config.yaml").read_text(encoding="utf-8")
    client.post("/settings/calibration_toggle")
    assert ("calibration_mode: true" in (env.config / "config.yaml").read_text(encoding="utf-8")) != before
