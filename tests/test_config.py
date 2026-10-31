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
