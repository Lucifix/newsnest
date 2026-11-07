"""Feed sessions: the home feed's order holds still while you read, and moves on in the next session."""

import random
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from newsnest import feedsession
from newsnest.db import MIGRATIONS, connect, iso, migrate, utcnow
from newsnest.feed import build_feed, view_feed
from tests.test_feed import NOW, SOURCES, add, db  # noqa: F401  (db is a fixture)

rng = random.Random(7)


def headline() -> str:
    # random words, so no two headlines share enough to be grouped into one story
    return " ".join("".join(rng.choice("abcdefghijklmnoprstuvz") for _ in range(8)) for _ in range(4))


def stories(db, cfg, n=8):
    """n separate stories with falling scores, so they rank in the order they were added."""
    return [add(db, cfg, "abcde"[i % 5], headline(), f"https://x.si/{i}", NOW - timedelta(minutes=10 + i),
                topic=9 - i % 5, importance=5, personal=9 - i // 2) for i in range(n)]


def order(page: str) -> list[int]:
    return [int(i) for i in re.findall(r'id="item-(\d+)"', page)]


def read_flags(page: str) -> dict[int, str]:
    return {int(i): r for i, r in re.findall(r'id="item-(\d+)" data-ids="[^"]*" data-read="(\d)"', page)}


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    from newsnest.web import app
    with TestClient(app) as c:
        yield c


def test_opening_a_story_and_coming_back_keeps_the_order(db, cfg, client):
    stories(db, cfg)
    first = order(client.get("/").text)
    assert len(first) == 8
    opened = first[1]
    assert client.get(f"/go/{opened}", follow_redirects=False).status_code == 302
    client.post("/api/seen", data={"ids": ",".join(map(str, first))})
    again = client.get("/").text
    # Same stories, same order; the opened one stays in place, dimmed as read
    assert order(again) == first
    assert read_flags(again)[opened] == "1" and read_flags(again)[first[0]] == "0"
    # The signals were recorded all the same: a fresh ranking leaves the read story out
    assert opened not in [it.card.lead.id for it in build_feed(db, cfg, SOURCES).items]


def test_switching_tabs_and_back_is_identical(db, cfg, client):
    stories(db, cfg)
    first = client.get("/?f=all").text
    client.post("/api/seen", data={"ids": ",".join(map(str, order(first)))})
    client.get(f"/go/{order(first)[0]}", follow_redirects=False)
    other = client.get("/?f=headlines").text
    assert set(order(other)) <= set(order(first))
    assert order(client.get("/?f=all").text) == order(first)


def test_a_new_session_after_the_idle_time_applies_the_signals(db, cfg, client):
    stories(db, cfg)
    first = order(client.get("/").text)
    client.get(f"/go/{first[0]}", follow_redirects=False)
    db.execute("UPDATE feed_sessions SET last_used_at = ?",
               (iso(utcnow() - timedelta(minutes=cfg.feed.session_idle_minutes + 1)),))
    # A fresh ranking: the opened story is gone (its open also nudged its outlet, so the rest may move)
    assert sorted(order(client.get("/").text)) == sorted(first[1:])


def test_refresh_starts_a_new_session(db, cfg, client):
    stories(db, cfg)
    first = order(client.get("/?f=all").text)
    client.get(f"/go/{first[0]}", follow_redirects=False)
    r = client.get("/?refresh=1&f=all", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?f=all"
    assert sorted(order(client.get(r.headers["location"]).text)) == sorted(first[1:])
    assert client.get("/?refresh=1", follow_redirects=False).headers["location"] == "/"


def test_not_interested_leaves_at_once_and_the_rest_holds_still(db, cfg, client):
    stories(db, cfg)
    first = order(client.get("/").text)
    client.post("/api/feedback", data={"action": "not_interested", "ids": str(first[2])})
    assert order(client.get("/").text) == first[:2] + first[3:]


def test_stories_folded_into_one_event_keep_the_first_place(db, cfg, client):
    stories(db, cfg)
    first = order(client.get("/").text)
    db.execute("UPDATE items SET event = 'ev' WHERE id IN (?, ?)", (first[1], first[4]))
    again = order(client.get("/").text)
    assert again == first[:4] + first[5:]


def test_a_better_outlet_joining_does_not_change_the_headline(db, cfg, client):
    stories(db, cfg)
    first = order(client.get("/").text)
    late = add(db, cfg, "e", headline(), "https://x.si/late", topic=10, importance=9, personal=10)
    lead_story = db.execute("SELECT story_id FROM items WHERE id = ?", (first[3],)).fetchone()[0]
    db.execute("UPDATE items SET story_id = ? WHERE id = ?", (lead_story, late))
    page = client.get("/").text
    assert order(page)[:8] == first  # the late outlet is a member of story 3, not a card of its own
    assert re.search(rf'id="item-{first[3]}" data-ids="[^"]*{late}', page)


def test_new_stories_wait_for_the_next_session(db, cfg, client):
    stories(db, cfg)
    first = order(client.get("/").text)
    fresh = add(db, cfg, "a", headline(), "https://x.si/new", topic=10, importance=6, personal=10)
    assert order(client.get("/").text) == first
    assert order(client.get("/?refresh=1").text)[0] == fresh


def test_load_more_pages_come_from_the_session(db, cfg, client):
    stories(db, cfg, 30)
    page = client.get("/?f=all").text
    shown = order(page)
    url = page.split('data-fragment="')[1].split('"')[0].replace("&amp;", "&")
    client.get(f"/go/{shown[0]}", follow_redirects=False)
    more = order(client.get(url).text)
    assert len(shown) == 20 and len(more) == 10 and not set(more) & set(shown)
    # An unknown token (e.g. from before the deploy) starts a session instead of failing
    r = client.get("/feed/more?token=gone&offset=20&f=all")
    assert r.status_code == 200 and len(order(r.text)) == 9  # a fresh ranking, without the opened story


def test_mark_all_read_ends_the_session(db, cfg, client):
    stories(db, cfg)
    page = client.get("/").text
    token = page.split('name="token" value="')[1].split('"')[0]
    assert 'name="f" value="all"' in page
    client.post("/api/read", data={"token": token, "f": "all", "value": "1"})
    assert db.execute("SELECT COUNT(*) FROM feed_sessions WHERE id = ?", (token,)).fetchone()[0] == 0
    assert "You're all caught up" in client.get("/").text


def test_session_matches_a_fresh_ranking_when_it_starts(db, cfg):
    stories(db, cfg, 12)
    now = utcnow()
    sid, ranking = feedsession.start(db, cfg, SOURCES, now)
    resumed = feedsession.resume(db, cfg, SOURCES, sid, now)
    fresh = build_feed(db, cfg, SOURCES, "all", now)
    for r in (ranking, resumed):
        feed = view_feed(r, cfg, "all")
        assert [it.card.lead.id for it in feed.items] == [it.card.lead.id for it in fresh.items]
        assert [it.why for it in feed.items] == [it.why for it in fresh.items]
        assert [c.key for c in feed.subchips] == [c.key for c in fresh.subchips]


def test_a_changed_config_damaged_or_old_format_session_is_ranked_again(db, cfg):
    stories(db, cfg)
    sid, _ = feedsession.start(db, cfg, SOURCES)
    assert feedsession.resume(db, cfg, SOURCES, sid) is not None
    assert feedsession.resume(db, cfg, SOURCES[:4], sid) is None  # a source removed
    cfg.feed.explore_every = 5
    assert feedsession.resume(db, cfg, SOURCES, sid) is None      # config edited
    sid, _ = feedsession.start(db, cfg, SOURCES)
    db.execute("UPDATE feed_sessions SET format = 0 WHERE id = ?", (sid,))
    assert feedsession.resume(db, cfg, SOURCES, sid) is None
    sid, _ = feedsession.start(db, cfg, SOURCES)
    db.execute("UPDATE feed_sessions SET payload = '{\"items\": 3}' WHERE id = ?", (sid,))
    assert feedsession.resume(db, cfg, SOURCES, sid) is None
    assert feedsession.resume(db, cfg, SOURCES, None) is None
    assert feedsession.resume(db, cfg, SOURCES, "unknown") is None
    assert feedsession.resume(db, cfg, SOURCES, "x" * 16 + "\r\nSet-Cookie: a=b") is None


def test_the_cookie_only_ever_carries_a_stored_session_id(db, cfg, client):
    stories(db, cfg)
    client.cookies.set(feedsession.COOKIE, "forged;path=/")
    r = client.get("/")
    sid = r.cookies.get(feedsession.COOKIE)
    assert sid != "forged;path=/" and db.execute("SELECT 1 FROM feed_sessions WHERE id = ?", (sid,)).fetchone()
    def flags(response):
        header = next(h for h in response.headers.get_list("set-cookie") if h.startswith(feedsession.COOKIE + "="))
        return {part.strip().split("=")[0].lower() for part in header.split(";")[1:]}
    assert "httponly" in flags(r) and "secure" not in flags(r)
    r = client.get("/", headers={"x-forwarded-proto": "https"})
    assert r.cookies.get(feedsession.COOKIE) == sid  # the same session, now marked secure
    assert "secure" in flags(r)


def test_touch_keeps_a_session_alive_but_never_revives_one(db, cfg):
    sid, _ = feedsession.start(db, cfg, SOURCES)
    idle = timedelta(minutes=cfg.feed.session_idle_minutes)
    later = utcnow() + idle - timedelta(minutes=1)
    feedsession.touch(db, cfg, sid, later)
    assert feedsession.resume(db, cfg, SOURCES, sid, later + idle - timedelta(minutes=1)) is not None
    gone = later + idle * 3
    feedsession.touch(db, cfg, sid, gone)
    assert feedsession.resume(db, cfg, SOURCES, sid, gone) is None


def test_old_sessions_are_cleaned_up(db, cfg):
    old = utcnow() - timedelta(days=2)
    feedsession.start(db, cfg, SOURCES, old)
    for _ in range(feedsession.KEEP_MAX + 5):
        feedsession.start(db, cfg, SOURCES)
    assert db.execute("SELECT COUNT(*) FROM feed_sessions").fetchone()[0] == feedsession.KEEP_MAX
    assert db.execute("SELECT COUNT(*) FROM feed_sessions WHERE created_at < ?", (iso(old + timedelta(hours=1)),)).fetchone()[0] == 0


def test_stable_sessions_off_ranks_on_every_load(db, cfg, client, env):
    path = env.config / "config.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("\nfeed:\n", "\nfeed:\n  stable_sessions: false\n", 1),
                    encoding="utf-8")
    stories(db, cfg)
    first = order(client.get("/").text)
    client.get(f"/go/{first[0]}", follow_redirects=False)
    assert sorted(order(client.get("/").text)) == sorted(first[1:])


def test_the_migration_adds_the_table_to_an_existing_database(env):
    conn = connect()
    migrate(conn)
    conn.execute("DROP TABLE feed_sessions")
    conn.execute("ALTER TABLE ai_calls DROP COLUMN provider")  # added by later migrations
    for column in ("interest", "view_ms", "expanded_at"):
        conn.execute(f"ALTER TABLE items DROP COLUMN {column}")
    before = next(i for i, sql in enumerate(MIGRATIONS) if "CREATE TABLE feed_sessions" in sql)
    conn.execute("UPDATE schema_version SET version = ?", (before,))
    assert migrate(conn) == len(MIGRATIONS)
    assert conn.execute("SELECT COUNT(*) FROM feed_sessions").fetchone()[0] == 0
    conn.close()


def test_tapping_the_tab_you_are_on_refreshes(db, cfg, client):
    stories(db, cfg)
    page = client.get("/?f=all").text
    assert page.count('href="/?f=all&amp;refresh=1" data-tab="all"') == 2  # top bar and dock
    assert 'href="/?f=headlines" data-tab="headlines"' in page
    # Inside a part or chip of a tab the first tap goes back to its All, without a refresh
    assert 'href="/?f=headlines&amp;refresh=1" data-tab="headlines"' in client.get("/?f=headlines").text
    assert 'href="/?f=headlines" data-tab="headlines"' in client.get("/?f=news").text
    first = order(client.get("/?f=all").text)
    client.get(f"/go/{first[0]}", follow_redirects=False)
    assert first[0] not in order(client.get("/?f=all&refresh=1").text)
    # Other pages have no refresh link
    assert "refresh=1" not in client.get("/saved").text
