import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from newsnest.config import FeedCfg, SectionCfg, Source
from newsnest.db import connect, iso, migrate
from newsnest.feed import build_feed
from newsnest.models import Item
from newsnest.pipeline import ingest
from newsnest.profile import write_profile
from newsnest.watches import Prefilter

NOW = datetime.now(timezone.utc)
SOURCES = [Source(id=s, type="rss", category="news", name=s.upper(), url=f"https://{s}.si/rss")
           for s in ("a", "b", "c", "d", "e")]


def add(conn, cfg, sid, title, url, when=NOW, **scores):
    src = next(s for s in SOURCES if s.id == sid)
    ingest(conn, src, [Item(sid, "news", title, url, url, when)], Prefilter(cfg), when)
    item_id = conn.execute("SELECT id FROM items WHERE url = ?", (url,)).fetchone()[0]
    if scores:
        conn.execute("UPDATE items SET score_status='scored', topic=?, importance=?, personal=?, reason='r', "
                     "ai_category=? WHERE id=?", (scores.get("topic", 5), scores.get("importance", 5),
                                                  scores.get("personal", 5), scores.get("cat", "Slovenia"), item_id))
    return item_id


@pytest.fixture
def db(env, cfg):
    write_profile("interests.md", "## Want\n- news\n")
    cfg.scoring.calibration_mode = False
    c = connect()
    migrate(c)
    yield c
    c.close()


def ids_in(feed):
    return [it.card.lead.id for it in feed.items]


def test_read_story_leaves_feed_and_comes_back_when_updated(db, cfg):
    first = add(db, cfg, "a", "Papežu Leonu XIV. se rak ni razširil", "https://a.si/1", NOW - timedelta(hours=3))
    db.execute("UPDATE items SET read_at = ?, last_read_at = ? WHERE id = ?",
               (iso(NOW - timedelta(hours=2)), iso(NOW - timedelta(hours=2)), first))
    feed = build_feed(db, cfg, SOURCES)
    assert first not in ids_in(feed) and feed.read_count == 1
    # Two new outlets are the same news reaching more outlets: it stays read
    add(db, cfg, "b", "Papežu se rak ni razširil", "https://b.si/1")
    add(db, cfg, "c", "Papež Leon XIV: rak se ni razširil", "https://c.si/1")
    assert build_feed(db, cfg, SOURCES).items == []
    # A third (fresh) one brings it back, marked Updated and unread again
    add(db, cfg, "d", "Papežu Leonu se rak ni razširil", "https://d.si/1")
    feed = build_feed(db, cfg, SOURCES)
    updated = [it for it in feed.items if it.updated]
    assert len(updated) == 1 and [m.source_name for m in updated[0].updated] == ["B", "C", "D"]
    assert not updated[0].card.read and not updated[0].big


def test_updated_needs_fresh_outlet_unless_top(db, cfg):
    first = add(db, cfg, "a", "Papežu Leonu XIV. se rak ni razširil", "https://a.si/1", NOW - timedelta(hours=20))
    db.execute("UPDATE items SET read_at = ?, last_read_at = ? WHERE id = ?",
               (iso(NOW - timedelta(hours=19)), iso(NOW - timedelta(hours=19)), first))
    for sid in "bcd":  # three new outlets, but all of them hours old
        add(db, cfg, sid, "Papežu se rak ni razširil", f"https://{sid}.si/1", NOW - timedelta(hours=10))
    assert build_feed(db, cfg, SOURCES).items == []
    # A top story comes back on two new outlets, however old
    db.execute("UPDATE items SET score_status='scored', topic=5, importance=9, personal=5, reason='r'")
    feed = build_feed(db, cfg, SOURCES)
    assert len(feed.items) == 1 and feed.items[0].top and feed.items[0].updated


def test_updated_card_can_be_marked_read(db, cfg, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    first = add(db, cfg, "a", "Papežu Leonu XIV. se rak ni razširil", "https://a.si/1", NOW - timedelta(hours=3))
    db.execute("UPDATE items SET read_at = ?, last_read_at = ? WHERE id = ?",
               (iso(NOW - timedelta(hours=2)), iso(NOW - timedelta(hours=2)), first))
    for sid in "bcd":
        add(db, cfg, sid, "Papežu se rak ni razširil", f"https://{sid}.si/1")
    from newsnest.web import app
    with TestClient(app) as client:
        page = client.get("/").text
        assert 'data-read="0"' in page
        assert 'aria-label="Mark as read"' in page
        assert ">Updated<" in page
        assert re.search(r'class="whatsnew[^"]*"><span class="src">d</span>', page, re.I)
        ids = build_feed(db, cfg, SOURCES).items[0].card.ids
        client.post("/api/read", data={"ids": ids, "value": "1"})
    assert build_feed(db, cfg, SOURCES).items == []


def test_go_reads_whole_story_and_clears_updated(db, cfg, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    a = add(db, cfg, "a", "Papežu Leonu XIV. se rak ni razširil", "https://a.si/1")
    add(db, cfg, "b", "Papežu se rak ni razširil", "https://b.si/1")
    from newsnest.web import app
    with TestClient(app) as client:
        client.get(f"/go/{a}", follow_redirects=False)
    assert db.execute("SELECT COUNT(*) FROM items WHERE last_read_at IS NULL").fetchone()[0] == 0
    assert build_feed(db, cfg, SOURCES).items == []


def test_freshness_floor_keeps_strong_older_story_above_weak_fresh(db, cfg):
    strong = add(db, cfg, "a", "Vlada sprejela proračun za prihodnje leto", "https://a.si/s", NOW - timedelta(hours=30),
                 topic=9, importance=7, personal=9)
    weak = add(db, cfg, "b", "Dnevni horoskop za četrtek", "https://b.si/w", NOW, topic=5, importance=5, personal=5)
    order = ids_in(build_feed(db, cfg, SOURCES))
    assert order.index(strong) < order.index(weak)


def test_skipped_stories_sink_then_drop_but_top_stories_stay(db, cfg):
    normal = add(db, cfg, "a", "Občina uredila novo kolesarsko stezo", "https://a.si/n", topic=7, importance=5, personal=7)
    top = add(db, cfg, "b", "Potres v Ljubljani, škode ni", "https://b.si/t", topic=7, importance=9, personal=7)
    db.execute("UPDATE items SET seen_count = 3")
    order = ids_in(build_feed(db, cfg, SOURCES))
    assert normal not in order and top in order


def test_impressions_are_throttled(db, cfg, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    i = add(db, cfg, "a", "Občina uredila novo kolesarsko stezo", "https://a.si/n")
    from newsnest.web import app
    with TestClient(app) as client:
        for _ in range(3):
            client.post("/api/seen", data={"ids": str(i)})
    assert db.execute("SELECT seen_count FROM items WHERE id = ?", (i,)).fetchone()[0] == 1


def test_menu_feedback_hides_and_adjusts_weights_without_touching_config(db, cfg, env, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    i = add(db, cfg, "a", "Občina uredila novo kolesarsko stezo", "https://a.si/n", topic=7, importance=5, personal=7)
    j = add(db, cfg, "b", "Nova cena elektrike od januarja", "https://b.si/e", topic=7, importance=5, personal=7)
    config_before = (env.config / "config.yaml").read_text(encoding="utf-8")
    from newsnest.web import app
    with TestClient(app) as client:
        client.post("/api/feedback", data={"action": "not_interested", "ids": str(i)})
        assert i not in ids_in(build_feed(db, cfg, SOURCES))
        assert db.execute("SELECT rating FROM items WHERE id = ?", (i,)).fetchone()[0] == -1
        client.post("/api/feedback", data={"action": "undo_not_interested", "ids": str(i)})
        assert i in ids_in(build_feed(db, cfg, SOURCES))
        r = client.post("/api/feedback", data={"action": "less_source", "target": "b"}, headers={"Accept": "application/json"})
        assert r.json()["weight"] == 0.75
        for _ in range(10):
            client.post("/api/feedback", data={"action": "less_source", "target": "b"})
        assert "How your feed is tuned" in client.get("/profile").text
    assert db.execute("SELECT weight FROM weights WHERE key = 'b'").fetchone()[0] == 0.2  # clamped
    assert (env.config / "config.yaml").read_text(encoding="utf-8") == config_before
    assert db.execute("SELECT COUNT(*) FROM feedback WHERE kind = 'less_source'").fetchone()[0] == 11
    order = ids_in(build_feed(db, cfg, SOURCES))
    assert order.index(i) < order.index(j)


def test_category_chips_and_filter(db, cfg, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    add(db, cfg, "a", "Nova bolnišnica v Ljubljani", "https://a.si/h", cat="Health", topic=7, importance=5, personal=7)
    add(db, cfg, "b", "Inflacija oktobra nižja", "https://b.si/e", cat="Economy", topic=7, importance=5, personal=7)
    feed = build_feed(db, cfg, SOURCES, active="cat:Health")
    assert [it.category for it in feed.items] == ["Health"]
    assert feed.parent == "news" and feed.tab == "headlines"  # the News tab stays lit under a Local category
    assert [c.key for c in feed.chips] == ["all", "headlines", "tech"]   # the example config: Local, World, Tech
    assert feed.active == "cat:news:Health"  # an older bare cat:<name> is scoped to its block
    # one row on News: All / Local / World, then the categories of the part shown
    assert [(c.key, c.label) for c in feed.subchips if c.scope] == [("headlines", "All"), ("news", "Local"), ("world", "World")]
    assert {"news", "cat:news:Health", "cat:news:Economy"} <= {c.key for c in feed.subchips}
    # a category with no stories in the chosen block falls back to that block's All
    tech = build_feed(db, cfg, SOURCES, active="cat:tech:Health")
    assert (tech.parent, tech.active, tech.items) == ("tech", "tech", [])
    assert build_feed(db, cfg, SOURCES, active="all").subchips == []
    # a Local outlet's foreign news is shown under World
    add(db, cfg, "a", "Volitve v Nemčiji", "https://a.si/w", cat="World", topic=7, importance=5, personal=7)
    assert [it.category for it in build_feed(db, cfg, SOURCES, active="world").items] == ["World"]
    assert "World" not in [it.category for it in build_feed(db, cfg, SOURCES, active="news").items]
    from newsnest.web import app
    with TestClient(app) as client:
        page = client.get("/?f=cat:Economy").text
        assert "Inflacija" in page and "bolnišnica" not in page and 'aria-label="Categories"' in page
        # the choice is remembered
        assert "Inflacija" in client.get("/").text and "bolnišnica" not in client.get("/").text


def test_auto_read_setting_and_opened_vs_read(db, cfg, env, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    a = add(db, cfg, "a", "Nova bolnišnica v Ljubljani", "https://a.si/h", cat="Health", topic=7, importance=5, personal=7)
    b = add(db, cfg, "b", "Inflacija oktobra nižja", "https://b.si/e", cat="Economy", topic=7, importance=5, personal=7)
    from newsnest.web import app
    with TestClient(app) as client:
        assert 'data-auto-read="0"' in client.get("/").text
        client.post("/settings/general", data={"auto_read_on_scroll": "on", "min_final": "5", "daily_usd": "0.5",
                                               "summary_language": "English"})
        assert 'data-auto-read="1"' in client.get("/").text
        assert "auto_read_on_scroll: true" in (env.config / "config.yaml").read_text(encoding="utf-8")
        # scrolled past = read but not opened; tapped = read and opened
        client.post("/api/read", data={"ids": str(a), "value": "1"})
        client.get(f"/go/{b}", follow_redirects=False)
    rows = dict(db.execute("SELECT id, opened_at IS NOT NULL FROM items").fetchall())
    assert rows[a] == 0 and rows[b] == 1
    assert build_feed(db, cfg, SOURCES).items == []


def test_feed_pages_load_incrementally_without_repeats(db, cfg, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    import random
    rng = random.Random(4)
    for i in range(45):
        # random words, so no two headlines share enough to be grouped into one story
        title = " ".join("".join(rng.choice("abcdefghijklmnoprstuvz") for _ in range(8)) for _ in range(4))
        add(db, cfg, "abcde"[i % 5], title, f"https://x.si/{i}", NOW - timedelta(minutes=i),
            topic=6, importance=5, personal=6)
    assert db.execute("SELECT COUNT(DISTINCT story_id) FROM items").fetchone()[0] == 45
    from newsnest.web import app
    with TestClient(app) as client:
        page = client.get("/?f=all").text
        assert page.count('<article class="story') == 20
        assert page.count('id="cardmenu"') == 1 and page.count("<form") < 30  # one shared menu, not 5 forms per card
        seen = set(__import__("re").findall(r'id="item-(\d+)"', page))
        url = page.split('data-fragment="')[1].split('"')[0].replace("&amp;", "&")
        pages = 1
        while url:
            frag = client.get(url).text
            ids = set(__import__("re").findall(r'id="item-(\d+)"', frag))
            assert not ids & seen
            seen |= ids
            pages += 1
            url = frag.split('data-fragment="')[1].split('"')[0].replace("&amp;", "&") if "data-fragment" in frag else None
        assert len(seen) == 45 and pages == 3 and "That's everything" in frag


def test_opens_and_likes_nudge_weights(db, cfg, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    i = add(db, cfg, "a", "Nova bolnišnica v Ljubljani", "https://a.si/h", cat="Health", topic=7, importance=5, personal=7)
    from newsnest.feed import load_weights
    from newsnest.web import app
    with TestClient(app) as client:
        client.get(f"/go/{i}", follow_redirects=False)
        client.get(f"/go/{i}", follow_redirects=False)  # reopening doesn't count twice
        w = load_weights(db)
        assert w[("source", "a")] == pytest.approx(1.03) and w[("category", "Health")] == pytest.approx(1.02)
        client.post(f"/api/items/{i}/rate", data={"value": "1"})
        assert load_weights(db)[("source", "a")] == pytest.approx(1.03 * 1.06, abs=1e-3)
        client.post(f"/api/items/{i}/rate", data={"value": "0"})  # undo the like
        assert load_weights(db)[("source", "a")] == pytest.approx(1.03, abs=1e-3)
        client.post("/api/read", data={"ids": str(i), "value": "1"})  # auto-read style: no change
        assert load_weights(db)[("source", "a")] == pytest.approx(1.03, abs=1e-3)
    from newsnest.feed import IMPLICIT_MAX, LIKE_NUDGE, nudge
    for _ in range(50):
        nudge(db, "a", "Health", LIKE_NUDGE)
    assert load_weights(db)[("source", "a")] == IMPLICIT_MAX


def test_world_block_gets_its_own_chip(db, cfg):
    world = Source(id="w", type="rss", category="world", name="W", url="https://w.example/rss")
    ingest(db, world, [Item("w", "world", "Summit ends in Geneva", "https://w.example/1", "https://w.example/1", NOW)],
           Prefilter(cfg), NOW)
    add(db, cfg, "a", "Nova bolnišnica v Ljubljani", "https://a.si/h", cat="Health", topic=7, importance=5, personal=7)
    sources = [*SOURCES, world]
    feed = build_feed(db, cfg, sources, active="world")
    assert [it.card.lead.row["source_id"] for it in feed.items] == ["w"]
    assert feed.parent == "world" and feed.tab == "headlines" and feed.subchips[0].label == "All"
    assert [it.card.lead.row["source_id"] for it in build_feed(db, cfg, sources, active="news").items] == ["a"]
    # the News tab itself is Local and World together, with its own counts per chip
    news = build_feed(db, cfg, sources, active="headlines")
    assert {it.card.lead.row["source_id"] for it in news.items} == {"a", "w"}
    assert [(c.key, c.count) for c in news.subchips if c.scope] == [("headlines", 2), ("news", 1), ("world", 1)]
    assert "cat:headlines:Health" in {c.key for c in news.subchips}
    assert [it.category for it in build_feed(db, cfg, sources, active="cat:headlines:Health").items] == ["Health"]
    assert len(build_feed(db, cfg, sources, active="all").items) == 2
    assert build_feed(db, cfg, sources, active="no-such-tab").active == "all"


def sports_source(db, cfg, tab=True, title="Final tonight", url="https://s.example/1"):
    """Give the config a Sports section with one feed and one scored story in it."""
    cfg.sections = [SectionCfg(key="news", label="Local"), SectionCfg(key="world", label="World"),
                    SectionCfg(key="tech", label="Tech", icon="chip"), SectionCfg(key="sports", label="Sports", icon="ball", tab=tab)]
    cfg.feeds.append(FeedCfg(id="s", url="https://s.example/rss", category="sports"))
    src = Source(id="s", type="rss", category="sports", name="S", url="https://s.example/rss")
    ingest(db, src, [Item("s", "sports", title, url, url, NOW)], Prefilter(cfg), NOW)
    db.execute("UPDATE items SET score_status='scored', topic=7, importance=5, personal=7, reason='r', ai_category='Sports' "
               "WHERE url = ?", (url,))
    return [*SOURCES, src]


def test_a_custom_section_is_a_tab_with_its_own_stories(db, cfg):
    sources = sports_source(db, cfg)
    feed = build_feed(db, cfg, sources, active="sports")
    assert [c.key for c in feed.chips] == ["all", "headlines", "tech", "sports"]
    assert [c.label for c in feed.chips][-1] == "Sports"
    assert (feed.parent, feed.tab) == ("sports", "sports")
    assert [it.card.lead.row["title"] for it in feed.items] == ["Final tonight"]
    assert [c.key for c in feed.subchips] == ["sports", "cat:sports:Sports"]
    assert "Final tonight" in [it.card.lead.row["title"] for it in build_feed(db, cfg, sources, active="all").items]
    assert build_feed(db, cfg, sources, active="headlines").items == []


def test_a_section_that_is_not_a_tab_is_a_chip_under_for_you(db, cfg):
    sources = sports_source(db, cfg, tab=False)
    feed = build_feed(db, cfg, sources, active="all")
    assert [c.key for c in feed.chips] == ["all", "headlines", "tech"]
    assert [(c.key, c.label) for c in feed.subchips] == [("all", "All"), ("sports", "Sports")]
    opened = build_feed(db, cfg, sources, active="sports")
    assert (opened.parent, opened.tab) == ("sports", "all")   # For you stays lit
    assert [it.card.lead.row["title"] for it in opened.items] == ["Final tonight"]
    assert [c.key for c in opened.subchips if c.scope] == ["all", "sports"]


def test_a_deleted_section_falls_back_to_for_you(db, cfg):
    sources = sports_source(db, cfg)
    cfg.sections = [s for s in cfg.sections if s.key != "sports"]
    cfg.feeds = [f for f in cfg.feeds if f.category != "sports"]
    for active in ("sports", "cat:sports:Sports"):
        feed = build_feed(db, cfg, sources, active=active)
        assert (feed.active, feed.parent, feed.tab) == ("all", "all", "all"), active
        assert "Final tonight" not in [it.card.lead.row["title"] for it in feed.items]   # no section, no place to show it


def test_a_config_without_a_sections_list_keeps_the_four_tabs(db, cfg):
    cfg.sections = None
    cfg.feeds.append(FeedCfg(id="gear", url="https://gear.example/rss", category="lifestyle"))
    feed = build_feed(db, cfg, SOURCES, active="all")
    assert [c.key for c in feed.chips] == ["all", "tech", "headlines", "lifestyle"]
    assert [c.label for c in feed.chips] == ["For you", "Tech", "News", "Lifestyle"]


def test_weight_strength_buckets():
    from newsnest.feed import WEIGHT_MAX, WEIGHT_MIN, weight_strength
    assert weight_strength(1.03) == ("more", 0)  # one open: a nudge, folded away on the profile page
    assert weight_strength(0.97) == ("less", 0)
    assert weight_strength(1.1) == ("more", 1) and weight_strength(1.25) == ("more", 2)  # one "More about"
    assert weight_strength(0.75) == ("less", 2)  # one "Less from"
    assert weight_strength(WEIGHT_MAX) == ("more", 3) and weight_strength(WEIGHT_MIN) == ("less", 3)
