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
        # the feed's own cards (the wide-screen front page before it repeats some of them)
        assert page.split('id="stream"', 1)[1].count('<article class="story') == 20
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


def test_opens_and_likes_leave_the_weights_alone(db, cfg, monkeypatch):
    """Opens and likes used to lift the outlet and category for good; drift (newsnest.drift) follows them now,
    and the card menu's choices stay exactly as the reader set them."""
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    i = add(db, cfg, "a", "Nova bolnišnica v Ljubljani", "https://a.si/h", cat="Health", topic=7, importance=5, personal=7)
    from newsnest.feed import load_weights
    from newsnest.web import app
    with TestClient(app) as client:
        client.post("/api/feedback", data={"action": "less_category", "target": "Health"})
        before = load_weights(db)
        assert before == {("category", "Health"): 0.75}
        client.get(f"/go/{i}", follow_redirects=False)
        client.post(f"/api/items/{i}/rate", data={"value": "1"})
        client.post(f"/api/items/{i}/rate", data={"value": "0"})
        client.post("/api/feedback", data={"action": "not_interested", "ids": str(i)})
        assert load_weights(db) == before
    assert db.execute("SELECT opened_at IS NOT NULL FROM items WHERE id = ?", (i,)).fetchone()[0]  # still drift's input


def test_old_boosts_are_skipped_only_when_asked(db):
    from newsnest.feed import load_weights
    rows = [("source", "a", 1.12), ("category", "a", 1.3),     # boosts from opens; a source named like a category
            ("source", "b", 1.25), ("category", "AI", 0.75),   # chosen in the menu
            ("source", "c", 1.8)]                              # explicit, beyond what opens could do
    for kind, key, w in rows:
        db.execute("INSERT INTO weights (kind, key, weight, updated_at) VALUES (?, ?, ?, ?)", (kind, key, w, iso(NOW)))
    db.execute("INSERT INTO feedback (at, kind, target) VALUES (?, 'more_source', 'b')", (iso(NOW),))
    db.execute("INSERT INTO feedback (at, kind, target) VALUES (?, 'less_category', 'AI')", (iso(NOW),))
    db.execute("INSERT INTO feedback (at, kind, target) VALUES (?, 'more_category', 'b')", (iso(NOW),))  # not source b's
    assert len(load_weights(db)) == 5
    assert load_weights(db, skip_old_boosts=True) == {("source", "b"): 1.25, ("category", "AI"): 0.75, ("source", "c"): 1.8}


def test_a_menu_choice_never_builds_on_an_old_boost(db, cfg, monkeypatch):
    """Drift ignores old boosts, so "less" on a boosted category must still mean less, on or off."""
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    from newsnest.feed import adjust_weight, load_weights
    from newsnest.web import app
    for kind, key in (("category", "Watches"), ("category", "Gear"), ("source", "a")):
        db.execute("INSERT INTO weights (kind, key, weight, updated_at) VALUES (?, ?, 1.5, ?)", (kind, key, iso(NOW)))
    with TestClient(app) as client:
        client.post("/api/feedback", data={"action": "less_category", "target": "Watches"})
        client.post("/api/feedback", data={"action": "more_category", "target": "Gear"})
        client.post("/api/feedback", data={"action": "less_source", "target": "a"})
    weights = load_weights(db)
    assert weights[("category", "Watches")] == 0.75                 # from neutral, not 1.5 × 0.75 = 1.125
    assert weights[("source", "a")] == 0.75
    assert weights[("category", "Gear")] == 1.5                     # "more" never below the old boost
    assert load_weights(db, skip_old_boosts=True) == weights        # all three are the reader's choices now
    # later choices build on the earlier ones, as before
    assert adjust_weight(db, "category", "Watches", 0.75) == 0.562
    assert adjust_weight(db, "category", "Gear", 1.25) == 1.875


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
    assert weight_strength(1.03) == ("more", 0)  # a tiny old boost, folded away on the profile page
    assert weight_strength(0.97) == ("less", 0)
    assert weight_strength(1.1) == ("more", 1) and weight_strength(1.25) == ("more", 2)  # one "More about"
    assert weight_strength(0.75) == ("less", 2)  # one "Less from"
    assert weight_strength(WEIGHT_MAX) == ("more", 3) and weight_strength(WEIGHT_MIN) == ("less", 3)


def test_ranking_lookups_do_not_read_every_stored_story(db, cfg, monkeypatch):
    """A new feed session is ranked on tab switches too: its lookups must use indexes, not scan all of history."""
    import newsnest.feed as feed_mod
    plans = []
    real = db.execute

    class Spy:
        def execute(self, sql, params=()):
            if "GROUP BY" in sql:
                plans.append([r[3] for r in real("EXPLAIN QUERY PLAN " + sql, params)])
            return real(sql, params)

    spy = Spy()
    feed_mod._affinity(spy)
    feed_mod._turned_down(spy)
    feed_mod._new_sources(spy, 14, set(), NOW)
    assert len(plans) == 3
    for plan in plans:
        assert not any(step.startswith("SCAN") for step in plan), plan
    # Same answers as before: a source with only recent stories is new, one with an old story is not
    add(db, cfg, "a", "Old story", "https://a.si/old", NOW - timedelta(days=30))
    add(db, cfg, "a", "New story", "https://a.si/new")
    add(db, cfg, "b", "New story too", "https://b.si/new")
    assert feed_mod._new_sources(db, 14, set(), NOW) == {"b"}


def _front_item(name, category="AI", read=False, image=True):
    from types import SimpleNamespace
    return SimpleNamespace(name=name, category=category, card=SimpleNamespace(read=read, image="x.jpg" if image else None))


def _names(package):
    return [it.name for it in package.items]


def test_front_page_leads_with_the_best_unread_story_then_top_stories_then_categories():
    from newsnest.feed import TOP_STORIES, front_page
    items = [_front_item("read-best", read=True), _front_item("lead"), _front_item("t1"), _front_item("t2", image=False),
             _front_item("t3"), _front_item("t4"), _front_item("s1", "Security"), _front_item("a1"),
             _front_item("s2", "Security", image=False), _front_item("x1", "Space"), _front_item("a2")]
    front = front_page(items)
    assert front.lead.name == "lead"   # unread first: the read best story only fills up
    assert [p.title for p in front.packages] == [TOP_STORIES, "Security", "AI"]
    assert _names(front.packages[0]) == ["t1", "t2", "t3", "t4"]
    assert _names(front.packages[1]) == ["s1", "s2"]
    # Every story at most once; Space has a single story left, too few for a package
    assert _names(front.packages[2]) == ["a1", "a2", "read-best"]
    shown = [front.lead.name] + [n for p in front.packages for n in _names(p)]
    assert len(shown) == len(set(shown)) and "x1" not in shown


def test_front_page_puts_a_photo_first_and_skips_lone_or_unscored_categories():
    from newsnest.feed import front_page
    items = [_front_item("lead"), _front_item("t1", image=False), _front_item("t2")]
    assert _names(front_page(items).packages[0]) == ["t2", "t1"]   # the large slot wants a photo
    unscored = [_front_item(f"u{i}", "Unscored") for i in range(8)]
    assert [p.title for p in front_page(unscored).packages] == ["Top stories"]
    # A category view: every story shares the category, so no package repeats it
    one = [_front_item(f"a{i}") for i in range(10)]
    assert [p.title for p in front_page(one).packages] == ["Top stories"]
    assert front_page([]).lead is None and front_page([_front_item("only")]).packages == []


def test_swipe_deck_asks_about_close_calls_first(db, cfg):
    from newsnest.feed import swipe_deck
    for sid in "abc":  # every source has older stories, so none is on trial as a new source
        add(db, cfg, sid, f"Stara novica {sid}", f"https://{sid}.si/old", NOW - timedelta(days=30))
    cut = cfg.digest.min_final
    sure_yes = add(db, cfg, "a", "Vlada sprejela proračun", "https://a.si/1", topic=9, importance=9, personal=9)
    sure_no = add(db, cfg, "a", "Nogometni rezultati tedna", "https://a.si/2", topic=1, importance=1, personal=1)
    on_cut = add(db, cfg, "b", "Nova kolesarska steza v Mariboru", "https://b.si/1",
                 topic=cut, importance=cut, personal=cut)
    just_below = add(db, cfg, "b", "Cene elektrike rahlo navzgor", "https://b.si/2",
                     topic=cut - 1, importance=cut - 1, personal=cut - 1)
    just_above = add(db, cfg, "c", "Zdravniki opozarjajo na gripo", "https://c.si/1",
                     topic=cut + 1.2, importance=cut + 1.2, personal=cut + 1.2)
    unscored = add(db, cfg, "c", "Poplave na Koroškem", "https://c.si/2")
    rated = add(db, cfg, "a", "Muzej odpira novo razstavo", "https://a.si/3", topic=cut, importance=cut, personal=cut)
    db.execute("UPDATE items SET rating = -1 WHERE id = ?", (rated,))

    def deck(skip=frozenset()):
        return [it.card.lead.id for it in swipe_deck(db, cfg, SOURCES, skip)]

    # Closest to the cutoff first, on either side (the feed hides the one just below), then unscored;
    # the stories it is sure about and the one already rated are not asked about
    assert deck() == [on_cut, just_below, just_above, unscored]
    assert sure_yes not in deck() and sure_no not in deck()
    assert just_below not in ids_in(build_feed(db, cfg, SOURCES))
    assert deck({str(on_cut)}) == [just_below, just_above, unscored]  # swiped up: later


def test_swipe_deck_includes_new_sources_whatever_they_score(db, cfg):
    from newsnest.feed import swipe_deck
    add(db, cfg, "a", "Stara novica", "https://a.si/old", NOW - timedelta(days=30))
    trial = add(db, cfg, "d", "Prva novica novega vira", "https://d.si/1", topic=9, importance=9, personal=9)
    settled = add(db, cfg, "a", "Vlada sprejela proračun", "https://a.si/1", topic=9, importance=9, personal=9)
    ids = [it.card.lead.id for it in swipe_deck(db, cfg, SOURCES)]
    assert trial in ids and settled not in ids
