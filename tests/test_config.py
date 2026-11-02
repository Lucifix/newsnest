import logging

from newsnest.config import load_config
from newsnest.db import connect, migrate


def test_removed_sections_are_ignored_not_rejected(env, caplog):
    path = env.config / "config.yaml"
    path.write_text(path.read_text() + """
google_news:
  enabled: true
  queries:
    - q: "Ljubljana when:1d"
listings:
  enabled: false
  searches: []
""".replace("\n", "\n"))
    with caplog.at_level(logging.WARNING, logger="newsnest.config"):
        loaded = load_config(env.config)
    assert not loaded.errors, loaded.errors
    assert not any(s.type == "gnews" for s in loaded.sources)
    assert "google_news" in caplog.text


def test_removed_heartbeat_option_is_ignored(env):
    path = env.config / "config.yaml"
    path.write_text(path.read_text().replace("news_empty_hours: 3", "news_empty_hours: 3\n  listing_failed_runs: 2"))
    assert not load_config(env.config).errors


def test_unknown_keys_are_still_rejected(env):
    path = env.config / "config.yaml"
    path.write_text(path.read_text() + "\ntypo_section: 1\n")
    assert load_config(env.config).errors


def test_migration_drops_listing_tables(env):
    conn = connect()
    migrate(conn)
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "listings" not in tables and "listing_events" not in tables
    assert "items" in tables
    conn.close()


def test_region_is_optional(env):
    cfg = load_config(env.config).config
    assert cfg.region.place == ""


def test_region_loads_and_normalises(env):
    path = env.config / "config.yaml"
    path.write_text(path.read_text() + "\nregion: {country: Slovenia, city: Ljubljana, tld: SI}\n")
    loaded = load_config(env.config)
    assert not loaded.errors, loaded.errors
    assert loaded.config.region.place == "Ljubljana, Slovenia"
    assert loaded.config.region.tld == ".si"


def test_unknown_time_zone_is_rejected(env):
    path = env.config / "config.yaml"
    path.write_text(path.read_text().replace("timezone: UTC", "timezone: Mars/Base"))
    errors = load_config(env.config).errors
    assert errors and "time zone" in " ".join(errors)


def test_local_pack_lookup_and_add():
    from newsnest.packs import SLOVENIA, add_pack, pack_for
    assert pack_for("slovenia") is SLOVENIA and pack_for("SI") is SLOVENIA and pack_for("Atlantis") is None
    assert pack_for("USA").code == "us" and pack_for("Deutschland").code == "de" and pack_for("England").code == "uk"
    assert pack_for("Österreich").code == "at" and pack_for("Hrvatska").code == "hr" and pack_for("Italia").code == "it"
    assert pack_for("France").code == "fr" and pack_for("canada").code == "ca" and pack_for("AU").code == "au"
    feeds = [{"id": "rtvslo", "url": "https://img.rtvslo.si/feeds/00.xml", "category": "world"}]
    added, moved = add_pack(feeds, SLOVENIA.feeds, "news")
    assert (added, moved) == (len(SLOVENIA.feeds) - 1, 1)
    by_name = {f["name"]: f for f in feeds[1:]}
    assert feeds[0]["category"] == "news"
    assert by_name["Slo-Tech"]["category"] == "tech" and by_name["Slo-Tech"]["https_links"] is True
    assert add_pack(feeds, SLOVENIA.feeds, "news") == (0, 0)


def test_local_packs_are_well_formed():
    from zoneinfo import ZoneInfo

    from newsnest.packs import LOCAL_PACKS, pack_for
    from newsnest.textmatch import normalize_url
    for pack in LOCAL_PACKS.values():
        ZoneInfo(pack.timezone)
        for name in (pack.code, pack.country, *pack.aliases):
            assert pack_for(name) is pack, name
        urls = [normalize_url(f.url) for f in pack.feeds]
        assert len(urls) == len(set(urls)), pack.code
        assert all(f.url.startswith("https://") for f in pack.feeds), pack.code
