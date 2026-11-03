import pytest
from fastapi.testclient import TestClient

from newsnest import catalog, icons
from newsnest.config import ICONS, SectionCfg, load_config
from newsnest.db import iso, utcnow
from newsnest.discovery import parse_opml
from newsnest.packs import LIFESTYLE_PACK
from newsnest.textmatch import normalize_url


def feed(name: str) -> catalog.CatalogFeed:
    return next(f for t in catalog.load_catalog() for f in t.feeds if f.name == name)


def topic(key: str) -> catalog.Topic:
    return next(t for t in catalog.load_catalog() if t.key == key)


@pytest.fixture
def web(env, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    import newsnest.web as web
    monkeypatch.setattr(web, "start_check_thread", lambda cfg: None)         # no network in tests
    monkeypatch.setattr(catalog, "start_icon_thread", lambda cfg: None)
    return web


def test_the_catalog_is_well_formed():
    topics = catalog.load_catalog()
    feeds = [f for t in topics for f in t.feeds]
    assert len(feeds) >= 180 and all(len(t.feeds) >= 8 for t in topics)
    assert all(f.url.startswith("https://") and 0 < len(f.description) <= 60 for f in feeds)
    assert len({f.icon_id for f in feeds}) == len(feeds)
    for t in topics:   # every topic can name the section it would create
        assert t.section_label and t.icon in ICONS
    assert not {f.key for f in feeds} & {normalize_url(p.url) for p in LIFESTYLE_PACK}   # that pack lives on Feeds
    # plain OPML: it opens in any reader, and in our own import
    assert len(parse_opml(catalog.CATALOG.read_bytes())) == len(feeds)


@pytest.mark.parametrize("outline, problem", [
    ('<outline xmlUrl="http://a.example/feed" text="A"/>', "https only"),
    ('<outline xmlUrl="https://a.example/feed" text="A"/><outline xmlUrl="https://www.a.example/feed/" text="B"/>', "twice"),
])
def test_a_broken_catalog_is_refused(tmp_path, monkeypatch, outline, problem):
    path = tmp_path / "catalog.opml"
    path.write_text(f'<opml><body><outline text="T" key="t" section="world">{outline}</outline></body></opml>')
    monkeypatch.setattr(catalog, "CATALOG", path)
    catalog.load_catalog.cache_clear()
    try:
        with pytest.raises(ValueError, match=problem):
            catalog.load_catalog()
    finally:
        catalog.load_catalog.cache_clear()


def test_a_new_section_needs_a_name_and_a_known_icon(tmp_path, monkeypatch):
    path = tmp_path / "catalog.opml"
    path.write_text('<opml><body><outline text="T" key="t" section="sport" section_label="Sport" icon="rocket">'
                    '<outline xmlUrl="https://a.example/feed" text="A"/></outline></body></opml>')
    monkeypatch.setattr(catalog, "CATALOG", path)
    catalog.load_catalog.cache_clear()
    try:
        with pytest.raises(ValueError, match="section_label and an icon"):
            catalog.load_catalog()
    finally:
        catalog.load_catalog.cache_clear()


def test_feed_states(conn, cfg):
    bbc, nature, wired, quanta = (feed(n) for n in ("BBC News: World", "Nature", "Wired", "Quanta Magazine"))
    for url, status in ((nature.url, "candidate"), (wired.url, "retired"), (quanta.url, "promoted")):
        conn.execute("INSERT INTO candidates (source_id, url, title, category, origin, status, added_at) "
                     "VALUES (?, ?, 'x', 'world', 'test', ?, ?)", (url[-20:], url, status, iso(utcnow())))
    states = catalog.feed_states(conn, cfg)
    assert states[bbc.key] == "yours"          # in config.example
    assert states[nature.key] == "trial" and states[wired.key] == "tried" and states[quanta.key] == "yours"
    assert feed("Stratechery").key not in states


def test_a_topic_goes_to_the_readers_section(cfg):
    sections = cfg.all_sections   # Local, World, Tech
    assert catalog.destination(topic("ai"), sections) == "tech"
    assert catalog.destination(topic("world"), sections) == "world"
    assert catalog.destination(topic("sport"), sections) is None   # the page asks
    own = [*sections, SectionCfg(key="wissenschaft", label="science")]   # one they made, matched by its name
    assert catalog.destination(topic("health"), own) == "wissenschaft"


def test_find_keeps_catalog_feeds_once():
    nature = feed("Nature")
    found = catalog.find([nature.url, nature.url.replace("https://www.", "https://") + "/", "https://evil.example/feed", ""])
    assert [(t.key, f.name) for t, f in found] == [("science", "Nature")]


def test_logos_are_looked_up_once_per_site_and_kept(conn, cfg, monkeypatch):
    looked: list[str] = []

    def fake_discover(client, meta, feed_url):
        looked.append(meta.site_url)
        return icons.Icon(b"png-bytes", "image/png", 64, 64, meta.site_url + "/icon.png") if "nytimes" in meta.site_url else None

    monkeypatch.setattr(icons, "discover", fake_discover)
    sites = {f.site for t in catalog.load_catalog() for f in t.feeds}
    assert catalog.warm_icons(cfg, client=object()) == len(sites)
    assert sorted(looked) == sorted(sites)          # the NYT's six feeds share one look
    found = catalog.found_icons(conn)
    nyt = [f for t in catalog.load_catalog() for f in t.feeds if "nytimes" in f.url]
    assert len(nyt) > 1 and set(found) == {f.icon_id for f in nyt}
    assert all(found[f.icon_id].startswith(f"/icon/{f.icon_id}?v=") for f in nyt)
    # nothing is due again until the retry time has passed
    assert catalog.warm_icons(cfg, client=object()) == 0
    assert catalog.icons_due(conn, utcnow() + icons.RETRY_MISSING) and not catalog.icons_due(conn, utcnow())


def test_the_catalog_page(conn, cfg, web):
    with TestClient(web.app) as client:
        discover = client.get("/settings/discover").text
        assert "How trials work" in discover and 'href="/settings/discover/catalog"' in discover
        assert f"{catalog.catalog_size()} checked feeds" in discover
        assert f"{catalog.catalog_size()} feeds" in client.get("/settings/feeds").text   # Feeds points there too
        page = client.get("/settings/discover/catalog").text
        assert 'action="/discovery/catalog"' in page and 'href="#topic-sport"' in page
        world = page.split('id="topic-world"')[1].split("</section>")[0]
        assert "Already yours" in world and world.index("CBC World") < world.index("Already yours")   # what you have comes last
        assert 'name="section.sport"' in page and "New section: Sport" in page
        # A topic whose section the reader has still asks, with that section picked
        ai = page.split('name="section.ai"')[1].split("</select>")[0]
        assert '<option value="tech" selected>' in ai and 'value="world"' in ai and "New section" not in ai
        nyt = feed("NYT Science")
        assert f'data-icon="{nyt.icon_id}"' in page   # initials until the logo is found
        conn.execute("INSERT INTO source_icons (source_id, site_url, feed_images, checked_at, mime, data, hash) "
                     "VALUES (?, 'https://www.nytimes.com', '[]', ?, 'image/png', x'00', 'abc')", (nyt.icon_id, iso(utcnow())))
        assert f'src="/icon/{nyt.icon_id}?v=abc"' in client.get("/settings/discover/catalog").text
        assert client.get("/settings/discover/catalog/icons").json() == {
            "running": False, "icons": {nyt.icon_id: f"/icon/{nyt.icon_id}?v=abc"}}


def test_picks_go_on_trial_in_their_sections(conn, cfg, env, web):
    science, climate, culture, sport, ai = (feed(n) for n in ("Nature", "Carbon Brief", "Aeon", "BBC Sport", "Interconnects"))
    conn.execute("INSERT INTO source_icons (source_id, site_url, feed_images, checked_at, mime, data, hash) "
                 "VALUES (?, 'https://www.bbc.com', '[]', ?, 'image/png', x'00', 'abc')", (sport.icon_id, iso(utcnow())))
    with TestClient(web.app) as client:
        r = client.post("/discovery/catalog", data={
            "url": [science.url, climate.url, culture.url, sport.url, ai.url, "https://evil.example/feed"],
            "section.science": "new", "section.climate": "new", "section.culture": "new", "section.sport": "new",
            "section.ai": "new",   # Tech exists: no second one
        }, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings/discover?saved=discovery&added=5"
    rows = {r["url"]: r for r in conn.execute("SELECT * FROM candidates")}
    assert set(rows) == {science.url, climate.url, culture.url, sport.url, ai.url}
    assert {u: r["status"] for u, r in rows.items()} == dict.fromkeys(rows, "checking")
    assert [rows[f.url]["category"] for f in (science, climate, culture, sport, ai)] == ["science", "science", "culture", "sport", "tech"]
    assert {r["origin"] for r in rows.values()} == {"catalog"}
    sections = {s.key: s for s in load_config(env.config).config.all_sections}
    assert list(sections) == ["news", "world", "tech", "science", "culture", "sport"]   # Science once, though three topics wanted it
    assert sections["science"].label == "Science" and sections["science"].icon == "flask"
    assert sections["culture"].tab and not sections["sport"].tab   # the dock is full: Sport is a chip under For you
    # the logo found for the card comes along to the trial source
    sid = rows[sport.url]["source_id"]
    assert conn.execute("SELECT hash FROM source_icons WHERE source_id = ?", (sid,)).fetchone()["hash"] == "abc"


def test_a_pick_can_go_to_world_or_an_existing_section(conn, cfg, web):
    with TestClient(web.app) as client:
        client.post("/discovery/catalog", data={"url": [feed("BBC Sport").url, feed("Polygon").url],
                                                "section.sport": "world", "section.gaming": "tech"})
        assert client.post("/discovery/catalog", data={"url": ["https://evil.example/feed"]},
                           follow_redirects=False).headers["location"] == "/settings/discover/catalog"
    cats = dict(conn.execute("SELECT url, category FROM candidates").fetchall())
    assert cats == {feed("BBC Sport").url: "world", feed("Polygon").url: "tech"}


def test_a_topic_with_a_home_can_go_elsewhere(conn, cfg, web):
    """World news picks are not stuck in World: the topic's "Goes to" can name another section."""
    with TestClient(web.app) as client:
        client.post("/discovery/catalog", data={"url": [feed("CBC World").url, feed("Interconnects").url],
                                                "section.world": "tech", "section.ai": "news"})
    cats = dict(conn.execute("SELECT url, category FROM candidates").fetchall())
    assert cats == {feed("CBC World").url: "tech", feed("Interconnects").url: "news"}
