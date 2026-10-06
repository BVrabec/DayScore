"""Data at rest: someone with only the data volume must not be able to read anything."""

import sqlite3
from pathlib import Path

import pytest

from app import backups, db, prefs, service

from .conftest import data_dir


def all_bytes(folder: Path) -> bytes:
    return b"".join(p.read_bytes() for p in folder.rglob("*") if p.is_file())


def test_nothing_readable_on_the_volume(user):
    secret = "my very private diary entry"
    user.post("/api/entries", json={"day": service.today().isoformat(), "text": secret})
    prefs.put("anthropic_api_key", "sk-ant-SECRET-KEY-123")
    user.post("/api/backups/run")
    blob = all_bytes(data_dir())
    for needle in (secret.encode(), b"sk-ant-SECRET-KEY-123", b"SQLite format 3", b"auth.password_hash"):
        assert needle not in blob, needle


def test_plain_database_is_encrypted_on_first_start_with_a_key(tmp_path, monkeypatch):
    plain = tmp_path / "dayscore.db"
    conn = sqlite3.connect(plain)
    conn.executescript(db.SCHEMA_V1)
    conn.execute("INSERT INTO settings VALUES ('cfg.timezone', 'Europe/Ljubljana')")
    conn.commit()
    conn.close()
    # Run the real start-up code against that file.
    monkeypatch.setattr(type(db.settings), "db_path", property(lambda self: plain))
    db.init()
    assert not plain.read_bytes().startswith(b"SQLite format 3")
    assert db.get_setting("cfg.timezone") == "Europe/Ljubljana"
    assert db.get_setting("missing") is None


def test_wrong_key_is_refused(monkeypatch):
    monkeypatch.setattr(db, "_KEY", db._derive("another key"))
    with pytest.raises(db.Locked, match="doesn't match"):
        db.init()


def test_missing_key_is_refused(monkeypatch):
    monkeypatch.setattr(db, "_KEY", None)
    with pytest.raises(db.Locked, match="isn't set"):
        db.init()


def test_newer_schema_is_refused():
    with db.connect() as conn:
        conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    with pytest.raises(db.Locked, match="newer"):
        db.init()


def test_schema_is_current():
    with db.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        cols = {r[1] for r in conn.execute("PRAGMA table_info(entries)")}
    assert {"mood", "rubric"} <= cols


def test_old_database_is_upgraded(tmp_path):
    old = tmp_path / "old.db"
    conn = sqlite3.connect(old)
    conn.executescript(db.SCHEMA_V1)   # a version-1 database, as older releases made it
    conn.execute("INSERT INTO todoist_done VALUES ('1', '2026-01-01', 'x', 'todoist', 'done', '{}')")
    conn.commit()
    conn.close()
    db.replace_with(old)
    with db.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert conn.execute("SELECT count(*) FROM todoist_done").fetchone()[0] == 0


def test_snapshot_never_touches_the_volume(user):
    raw, digest = backups.snapshot()
    assert raw.startswith(b"SQLite format 3") and len(digest) == 64
    assert not any(p.suffix == ".db" and p.name != "dayscore.db" for p in data_dir().rglob("*"))
