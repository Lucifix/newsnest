"""Installs that predate the rename keep working: old env var names, the old database file name."""

import logging

from newsnest import bootstrap
from newsnest.bootstrap import adopt_legacy_database, config_dir, data_dir, env


def test_new_env_name_wins_over_the_old_one(monkeypatch):
    monkeypatch.setenv("NEWSNEST_PASSWORD", "new")
    monkeypatch.setenv("RADAR_PASSWORD", "old")
    assert env("PASSWORD") == "new"


def test_old_env_name_still_read_and_warned_once(monkeypatch, caplog):
    monkeypatch.delenv("NEWSNEST_PASSWORD", raising=False)
    monkeypatch.setenv("RADAR_PASSWORD", "old")
    monkeypatch.setattr(bootstrap, "_warned_legacy", set())
    with caplog.at_level(logging.WARNING, logger="newsnest.bootstrap"):
        assert env("PASSWORD") == "old"
        assert env("PASSWORD") == "old"
    assert caplog.text.count("RADAR_PASSWORD") == 1
    assert "NEWSNEST_PASSWORD" in caplog.text


def test_env_default_when_neither_is_set(monkeypatch):
    monkeypatch.delenv("NEWSNEST_SCHEDULER", raising=False)
    monkeypatch.delenv("RADAR_SCHEDULER", raising=False)
    assert env("SCHEDULER") is None
    assert env("SCHEDULER", "on") == "on"


def test_old_dir_variables_still_locate_the_folders(monkeypatch, tmp_path):
    monkeypatch.delenv("NEWSNEST_CONFIG_DIR", raising=False)
    monkeypatch.delenv("NEWSNEST_DATA_DIR", raising=False)
    monkeypatch.setenv("RADAR_CONFIG_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("RADAR_DATA_DIR", str(tmp_path / "d"))
    assert config_dir() == tmp_path / "c" and data_dir() == tmp_path / "d"


def test_old_database_is_renamed_with_its_wal_files(tmp_path):
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"radar.db{suffix}").write_text(f"old{suffix}")
    assert adopt_legacy_database(tmp_path)
    assert (tmp_path / "newsnest.db").read_text() == "old"
    assert (tmp_path / "newsnest.db-wal").read_text() == "old-wal"
    assert not list(tmp_path.glob("radar.db*"))


def test_existing_new_database_is_never_overwritten(tmp_path):
    (tmp_path / "radar.db").write_text("old")
    (tmp_path / "newsnest.db").write_text("current")
    assert not adopt_legacy_database(tmp_path)
    assert (tmp_path / "newsnest.db").read_text() == "current"
    assert (tmp_path / "radar.db").read_text() == "old"


def test_nothing_to_adopt_on_a_fresh_install(tmp_path):
    assert not adopt_legacy_database(tmp_path)


def test_start_up_adopts_the_old_database_and_keeps_its_data(env):
    import sqlite3
    old = sqlite3.connect(env.data / "radar.db")
    old.execute("CREATE TABLE marker (v TEXT)")
    old.execute("INSERT INTO marker VALUES ('kept')")
    old.commit()
    old.close()
    bootstrap.ensure_dirs()
    from newsnest.db import connect
    assert connect().execute("SELECT v FROM marker").fetchone()[0] == "kept"
