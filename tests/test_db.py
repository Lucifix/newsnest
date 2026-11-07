"""Schema migrations: a backup before every upgrade, and a database from a newer release is refused."""

import sqlite3

import pytest

from newsnest.db import BACKUPS_KEPT, MIGRATIONS, SchemaTooNew, _run_script, connect, migrate, transaction


def version(conn) -> int:
    return conn.execute("SELECT version FROM schema_version").fetchone()[0]


def schema(conn) -> list[str]:
    return sorted(r[0] for r in conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))


def at_previous_version(env):
    """A database one release behind: all but the last migration applied, with a story in it.
    Built forward from the migrations, so it stays right whichever migration is added next."""
    conn = connect()
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    with transaction(conn):
        for sql in MIGRATIONS[:-1]:
            _run_script(conn, sql)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (len(MIGRATIONS) - 1,))
    conn.execute("INSERT INTO items (source_id, category, url, url_norm, title, fetched_at) "
                 "VALUES ('a', 'news', 'https://a.si/x', 'a.si/x', 'Kept', '2026-11-01T08:00:00+00:00')")
    return conn


def backups(env):
    return sorted((env.data / "backups").glob("*.db")) if (env.data / "backups").exists() else []


def test_a_fresh_install_takes_no_backup(env):
    conn = connect()
    migrate(conn)
    migrate(conn)  # a restart with nothing pending
    assert backups(env) == []
    conn.close()


def test_an_upgrade_backs_up_the_database_as_it_was(env):
    conn = at_previous_version(env)
    before = schema(conn)
    assert migrate(conn) == len(MIGRATIONS)
    assert version(conn) == len(MIGRATIONS)
    [backup] = backups(env)
    assert backup.name.endswith(f"-v{len(MIGRATIONS) - 1}.db")
    copy = sqlite3.connect(backup)
    assert copy.execute("SELECT version FROM schema_version").fetchone()[0] == len(MIGRATIONS) - 1
    assert copy.execute("SELECT title FROM items").fetchall() == [("Kept",)]
    assert schema(copy) == before != schema(conn)  # the layout before the upgrade, not after
    copy.close()
    migrate(conn)  # nothing pending now: no second copy
    assert len(backups(env)) == 1
    conn.close()


def test_only_the_newest_backups_are_kept(env):
    folder = env.data / "backups"
    folder.mkdir()
    for day in range(1, BACKUPS_KEPT + 2):
        (folder / f"newsnest-202601{day:02d}-120000-v9.db").write_bytes(b"")
    (folder / "mine.db").write_bytes(b"")  # not ours: left alone
    conn = at_previous_version(env)
    migrate(conn)
    kept = [p.name for p in backups(env) if p.name != "mine.db"]
    assert len(kept) == BACKUPS_KEPT
    assert kept[-1].endswith(f"-v{len(MIGRATIONS) - 1}.db")  # the new one survives
    assert "newsnest-20260101-120000-v9.db" not in kept  # the oldest went
    assert (folder / "mine.db").exists()
    conn.close()


def test_a_failed_backup_stops_the_upgrade(env):
    conn = at_previous_version(env)
    (env.data / "backups").write_text("a file where the folder should be")
    with pytest.raises(OSError):
        migrate(conn)
    assert version(conn) == len(MIGRATIONS) - 1
    conn.close()


def test_a_database_from_a_newer_release_is_refused(env):
    conn = connect()
    migrate(conn)
    conn.execute("UPDATE schema_version SET version = ?", (len(MIGRATIONS) + 1,))
    with pytest.raises(SchemaTooNew, match=f"version {len(MIGRATIONS) + 1}.*up to {len(MIGRATIONS)}"):
        migrate(conn)
    assert version(conn) == len(MIGRATIONS) + 1  # untouched
    assert backups(env) == []
    conn.close()


def test_the_app_will_not_start_on_a_newer_database(env, monkeypatch):
    from fastapi.testclient import TestClient

    from newsnest.web import app
    monkeypatch.setenv("NEWSNEST_SCHEDULER", "off")
    conn = connect()
    migrate(conn)
    conn.execute("UPDATE schema_version SET version = ?", (len(MIGRATIONS) + 1,))
    conn.close()
    with pytest.raises(SchemaTooNew):
        with TestClient(app):
            pass


def test_an_in_memory_database_upgrades_without_a_backup():
    from newsnest.db import _run_script, transaction
    conn = connect(":memory:")
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    with transaction(conn):
        _run_script(conn, MIGRATIONS[0])
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
    assert migrate(conn) == len(MIGRATIONS)
    assert version(conn) == len(MIGRATIONS)
    conn.close()
