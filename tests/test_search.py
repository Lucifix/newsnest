from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from newsnest.db import MIGRATIONS, connect, dumps, migrate
from newsnest.feed import search_query, search_stories
from tests.test_feed import NOW, SOURCES, add, db  # noqa: F401  (db is a fixture)


def titles(cards):
    return [c.lead.row["title"] for c in cards]


def found(db, cfg, q, **kw):
    return titles(search_stories(db, cfg, SOURCES, q, **kw)[0])


def test_migration_builds_index_from_existing_items(env):
    conn = connect()
    migrate_to = len(MIGRATIONS) - 1  # everything before the search migration
    from newsnest.db import _run_script, transaction
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    for version, sql in enumerate(MIGRATIONS[:migrate_to], start=1):
        with transaction(conn):
            _run_script(conn, sql)
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    conn.execute("INSERT INTO items (source_id, category, url, url_norm, title, fetched_at) "
                 "VALUES ('a', 'news', 'https://a.si/x', 'a.si/x', 'Koncept fiat multiplay', '2026-10-24T08:00:00+00:00')")
    migrate(conn)
    triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert {"items_fts_insert", "items_fts_update", "items_fts_delete"} <= triggers
    assert conn.execute("SELECT rowid FROM items_fts WHERE items_fts MATCH 'multiplay'").fetchall()
    assert migrate(conn) == len(MIGRATIONS)  # running again is a no-op
    conn.close()


def test_new_and_edited_items_are_indexed(db, cfg):
    item = add(db, cfg, "a", "Koncept fiat multiplay se iz družinskega avtomobila spreminja v poltovornjak", "https://a.si/1")
    assert found(db, cfg, "multiplay") == [db.execute("SELECT title FROM items WHERE id = ?", (item,)).fetchone()[0]]
    db.execute("UPDATE items SET title = 'Nov naslov brez avtomobila' WHERE id = ?", (item,))
    assert found(db, cfg, "multiplay") == []
    assert found(db, cfg, "naslov") == ["Nov naslov brez avtomobila"]
    db.execute("DELETE FROM items WHERE id = ?", (item,))
    assert db.execute("SELECT COUNT(*) FROM items_fts").fetchone()[0] == 0


def test_key_points_count_only_when_written(db, cfg):
    item = add(db, cfg, "a", "Vlada sprejela proračun", "https://a.si/1")
    db.execute("UPDATE items SET summary_status = 'error', summary = 'timeout reading article' WHERE id = ?", (item,))
    assert found(db, cfg, "timeout") == []
    db.execute("UPDATE items SET summary = 'Več denarja za ceste', summary_points = ?, summary_status = 'done' WHERE id = ?",
               (dumps(["Obnova železnice do Kopra"]), item))
    assert found(db, cfg, "ceste") == ["Vlada sprejela proračun"]
    assert found(db, cfg, "zeleznice") == ["Vlada sprejela proračun"]


def test_whole_words_accents_and_prefix_while_typing(db, cfg):
    add(db, cfg, "a", "Šport: Olimpija premagala Celje", "https://a.si/1")
    add(db, cfg, "b", "Zapora ceste v Ljubljani", "https://b.si/2")
    assert found(db, cfg, "sport") == ["Šport: Olimpija premagala Celje"]
    assert found(db, cfg, "ŠPORT olimpija") == ["Šport: Olimpija premagala Celje"]
    assert found(db, cfg, "ljubljana") == []  # no Slovene word forms (roadmap)
    assert found(db, cfg, "ljublj") == ["Zapora ceste v Ljubljani"]  # the last word is a prefix
    assert found(db, cfg, "ljublj ceste") == []  # earlier words are whole words
    assert found(db, cfg, "olimpija sport celje") == ["Šport: Olimpija premagala Celje"]


@pytest.mark.parametrize("q", ['"', "*", 'a"b', "NEAR(", "title:x", "- OR AND", "***", "   "])
def test_fts_syntax_in_input_is_harmless(db, cfg, q):
    add(db, cfg, "a", "Šport: Olimpija premagala Celje", "https://a.si/1")
    search_stories(db, cfg, SOURCES, q)


def test_punctuation_words_do_not_empty_results(db, cfg):
    add(db, cfg, "a", "Koncept fiat multiplay", "https://a.si/1")
    assert found(db, cfg, "fiat -") == ["Koncept fiat multiplay"]
    assert found(db, cfg, "fiat – multiplay") == ["Koncept fiat multiplay"]
    assert found(db, cfg, "fiat & multiplay!") == ["Koncept fiat multiplay"]


def test_search_query():
    assert search_query("fiat multipl") == '"fiat" "multipl"*'
    assert search_query('a"b *c') == '"ab" "c"*'
    assert search_query("  ") is None


def test_fourteen_days_then_older(db, cfg):
    add(db, cfg, "a", "Koncept fiat multiplay", "https://a.si/new", NOW - timedelta(days=2))
    add(db, cfg, "b", "Fiat predstavil panda", "https://b.si/old", NOW - timedelta(days=40))
    cards, older = search_stories(db, cfg, SOURCES, "fiat")
    assert titles(cards) == ["Koncept fiat multiplay"] and older
    cards, older = search_stories(db, cfg, SOURCES, "fiat", days=None)
    assert titles(cards) == ["Koncept fiat multiplay", "Fiat predstavil panda"] and not older
    assert search_stories(db, cfg, SOURCES, "multiplay")[1] is False


def test_hidden_stories_still_found(db, cfg):
    item = add(db, cfg, "a", "Koncept fiat multiplay", "https://a.si/1")
    db.execute("UPDATE items SET hidden_at = ? WHERE id = ?", (NOW.isoformat(), item))
    assert found(db, cfg, "fiat") == ["Koncept fiat multiplay"]


def test_search_page_and_fragment(db, cfg, monkeypatch):
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    add(db, cfg, "a", "Koncept fiat multiplay", "https://a.si/new", NOW - timedelta(days=2))
    add(db, cfg, "b", "Fiat predstavil panda", "https://b.si/old", NOW - timedelta(days=40))
    from newsnest.web import app
    with TestClient(app) as client:
        page = client.get("/search", params={"q": "fiat"}).text
        assert "<html" in page and "Koncept fiat multiplay" in page and "Fiat predstavil panda" not in page
        assert "Show older results" in page and "/search?q=fiat&amp;all=1" in page
        fragment = client.get("/search", params={"q": "fiat"}, headers={"X-Fragment": "1"}).text
        assert "<html" not in fragment and "Koncept fiat multiplay" in fragment
        everything = client.get("/search", params={"q": "fiat", "all": 1}, headers={"X-Fragment": "1"}).text
        assert "Fiat predstavil panda" in everything and "Show older results" not in everything
        assert "Nothing in the last 14 days" in client.get("/search", params={"q": "zzz"}, headers={"X-Fragment": "1"}).text
        assert "Search titles" in client.get("/search", headers={"X-Fragment": "1"}).text
        # search results are not impressions: nothing on them is tracked as seen
        assert "data-track-seen" not in fragment
        assert 'class="docksearch"' in client.get("/saved").text
