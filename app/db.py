"""SQLite storage. One row per day; the whole database is a single file in DATA_DIR.

With DAYSCORE_KEY set, the file is encrypted with SQLCipher (AES-256): someone who copies the
data volume, or mounts it in another container, sees only random bytes. Without a key it's a
plain SQLite file, as in older versions; an existing plain file is encrypted on the first start
with a key.

The schema is versioned (PRAGMA user_version) and upgraded step by step on start.
"""

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from .config import settings

try:
    import sqlcipher3 as _cipher
except ImportError:  # only needed when a key is set
    _cipher = None

log = logging.getLogger("dayscore.db")

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS entries (
    day         TEXT PRIMARY KEY,          -- YYYY-MM-DD
    raw_text    TEXT NOT NULL,             -- what you wrote, any language
    score       INTEGER NOT NULL,          -- 0-100
    title       TEXT NOT NULL,             -- short English headline
    summary     TEXT NOT NULL,             -- English summary
    activities  TEXT NOT NULL,             -- JSON [{text, category}]
    reason      TEXT NOT NULL,             -- why this score
    tip         TEXT NOT NULL,             -- one suggestion for tomorrow
    model       TEXT NOT NULL,
    source      TEXT NOT NULL,             -- telegram | web
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS todoist_done (
    task_id      TEXT NOT NULL,
    day          TEXT NOT NULL,            -- the DayScore day it belongs to
    completed_at TEXT NOT NULL,            -- ISO timestamp
    source       TEXT NOT NULL,            -- dayscore (matched from a note) | todoist (done in the app)
    status       TEXT NOT NULL,            -- done | failed
    task         TEXT NOT NULL,            -- JSON snapshot: content, description, priority, labels, due...
    PRIMARY KEY (task_id, day)
);
CREATE TABLE IF NOT EXISTS todoist_suggestions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT NOT NULL,
    task_id     TEXT NOT NULL,
    task        TEXT NOT NULL,             -- JSON snapshot of the open task
    status      TEXT NOT NULL,             -- pending | accepted | rejected
    selected    INTEGER NOT NULL DEFAULT 1,-- Telegram toggle state while pending
    UNIQUE (day, task_id)
);
CREATE TABLE IF NOT EXISTS todoist_created (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT NOT NULL,             -- the day whose note asked for it
    task_id     TEXT NOT NULL,
    content     TEXT NOT NULL,
    due         TEXT NOT NULL,             -- YYYY-MM-DD or ''
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reminders_sent (
    day  TEXT NOT NULL,
    kind TEXT NOT NULL,
    PRIMARY KEY (day, kind)
);
"""

# Schema upgrades: version -> statements. Never edit a released step; add a new one.
MIGRATIONS: dict[int, list[str]] = {
    2: [
        # Older versions also copied tasks completed directly in Todoist; History no longer shows them.
        "DELETE FROM todoist_done WHERE source = 'todoist'",
        "ALTER TABLE entries ADD COLUMN mood INTEGER",                       # 1-5, optional
        "ALTER TABLE entries ADD COLUMN rubric TEXT NOT NULL DEFAULT ''",    # scoring rules version
    ],
}
SCHEMA_VERSION = max(MIGRATIONS)


class Locked(Exception):
    """The database can't be opened: wrong or missing key, or a newer schema."""


# ---------- keys ----------

def _derive(secret: str) -> bytes:
    # The user's key may be a passphrase; stretch it once at start (not per connection).
    return hashlib.scrypt(secret.encode(), salt=b"dayscore-data-key-v1", n=2**14, r=8, p=1, dklen=32)


_KEY: bytes | None = _derive(settings.data_key) if settings.data_key else None
_lock = threading.RLock()   # held by every connection, so a restore can swap the file safely


def encrypted() -> bool:
    return _KEY is not None


def subkey(purpose: str) -> bytes | None:
    """A separate key for another use (session signing, local backups), or None without a key."""
    return hmac.new(_KEY, purpose.encode(), hashlib.sha256).digest() if _KEY else None


def _driver():
    if _KEY is not None and _cipher is None:
        raise Locked("DAYSCORE_KEY is set, but SQLCipher (the sqlcipher3 package) isn't installed.")
    return _cipher or sqlite3


def _keyed(path: Path):
    """Open a database file with the data key (or plain without one)."""
    driver = _driver()
    conn = driver.connect(path, timeout=15)
    if _KEY is not None:
        conn.execute(f"PRAGMA key = \"x'{_KEY.hex()}'\"")
    conn.row_factory = driver.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def _quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _is_plain(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(16) == b"SQLite format 3\x00"


def _drop_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


@contextmanager
def connect():
    with _lock:
        conn = _keyed(settings.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


# ---------- start-up: encrypt, open, upgrade ----------

def _encrypt_plain_file(path: Path) -> None:
    """Turn an existing plain database into an encrypted one (first start with a key)."""
    tmp = path.with_name(path.name + ".encrypting")
    tmp.unlink(missing_ok=True)
    src = _driver().connect(path)
    try:
        src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        version = src.execute("PRAGMA user_version").fetchone()[0]
        src.execute(f"ATTACH DATABASE {_quote(tmp)} AS enc KEY \"x'{_KEY.hex()}'\"")
        src.execute("SELECT sqlcipher_export('enc')")
        src.execute(f"PRAGMA enc.user_version = {int(version)}")
        src.execute("DETACH DATABASE enc")
    finally:
        src.close()
    os.chmod(tmp, 0o600)
    _drop_sidecars(path)
    os.replace(tmp, path)
    log.warning("Encrypted the existing database with DAYSCORE_KEY.")


def _migrate(conn) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise Locked("This data is from a newer DayScore version. Update DayScore to open it.")
    if version == 0:   # a new database, or one from before versioning
        conn.executescript(SCHEMA_V1)
        version = 1
    for target in range(version + 1, SCHEMA_VERSION + 1):
        for statement in MIGRATIONS[target]:
            try:
                conn.execute(statement)
            except Exception as e:
                if "duplicate column" not in str(e):   # tolerate a half-applied step
                    raise
        conn.execute(f"PRAGMA user_version = {target}")
        conn.commit()
        log.info("Database upgraded to version %d", target)


def init() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path = settings.db_path
    with _lock:
        if path.exists() and path.stat().st_size:
            plain = _is_plain(path)
            if plain and _KEY is not None:
                _encrypt_plain_file(path)
            elif not plain and _KEY is None:
                raise Locked("Your data is encrypted, but DAYSCORE_KEY isn't set. Add the key "
                             "you used before (see the README) and restart.")
        try:
            with connect() as conn:
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
                conn.execute("PRAGMA journal_mode=WAL")
                _migrate(conn)
        except Locked:
            raise
        except Exception as e:
            if _KEY is not None and "not a database" in str(e):
                raise Locked("DAYSCORE_KEY doesn't match your data. Use the key you set up "
                             "DayScore with and restart.") from e
            raise
    if path.exists():
        os.chmod(path, 0o600)


def schema_version_of(path: Path) -> int:
    """Schema version of a plain (unencrypted) database file, e.g. an unpacked backup."""
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


# ---------- plain copies for backups, and restoring one ----------

def export_plain(dest: Path) -> None:
    """Write a consistent, unencrypted copy of the live database to dest (for backups; the
    caller puts it in RAM-only scratch space and encrypts the result)."""
    dest.unlink(missing_ok=True)
    with connect() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if _KEY is not None:
            conn.execute(f"ATTACH DATABASE {_quote(dest)} AS plaintext KEY ''")
            conn.execute("SELECT sqlcipher_export('plaintext')")
            conn.execute(f"PRAGMA plaintext.user_version = {int(version)}")
            conn.execute("DETACH DATABASE plaintext")
        else:
            _plain_copy(settings.db_path, dest)


def _plain_copy(src_path: Path, dest: Path) -> None:
    src, dst = sqlite3.connect(src_path), sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


def replace_with(plain: Path) -> None:
    """Replace the live database with a plain database file (a verified backup), encrypting it
    with the data key, then upgrade it to the current schema."""
    path = settings.db_path
    new = path.with_name(path.name + ".restoring")
    with _lock:
        new.unlink(missing_ok=True)
        _drop_sidecars(new)
        if _KEY is not None:
            src = _driver().connect(plain)
            try:
                version = src.execute("PRAGMA user_version").fetchone()[0]
                src.execute(f"ATTACH DATABASE {_quote(new)} AS enc KEY \"x'{_KEY.hex()}'\"")
                src.execute("SELECT sqlcipher_export('enc')")
                src.execute(f"PRAGMA enc.user_version = {int(version)}")
                src.execute("DETACH DATABASE enc")
            finally:
                src.close()
        else:
            _plain_copy(plain, new)
        os.chmod(new, 0o600)
        if path.exists():
            live = _keyed(path)
            try:
                live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                live.close()
        _drop_sidecars(path)
        os.replace(new, path)
        _drop_sidecars(new)
        with connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            _migrate(conn)


def _row_to_entry(row: sqlite3.Row) -> dict:
    entry = dict(row)
    entry["activities"] = json.loads(entry["activities"])
    return entry


def get_entry(day: date) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM entries WHERE day = ?", (day.isoformat(),)).fetchone()
    return _row_to_entry(row) if row else None


def list_entries(since: date | None = None) -> list[dict]:
    with connect() as conn:
        if since:
            rows = conn.execute(
                "SELECT * FROM entries WHERE day >= ? ORDER BY day", (since.isoformat(),)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM entries ORDER BY day").fetchall()
    return [_row_to_entry(r) for r in rows]


def recent_entries(before: date, limit: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM entries WHERE day < ? ORDER BY day DESC LIMIT ?",
            (before.isoformat(), limit),
        ).fetchall()
    return [_row_to_entry(r) for r in reversed(rows)]


def save_entry(day: date, raw_text: str, result: dict, model: str, source: str,
               rubric: str = "", mood: int | None = None) -> dict:
    """Insert or update a day. A mood of None keeps the day's saved mood."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO entries (day, raw_text, score, title, summary, activities, reason, tip,
                                 model, source, created_at, updated_at, rubric, mood)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
                raw_text = excluded.raw_text, score = excluded.score, title = excluded.title,
                summary = excluded.summary, activities = excluded.activities,
                reason = excluded.reason, tip = excluded.tip, model = excluded.model,
                updated_at = excluded.updated_at, rubric = excluded.rubric,
                mood = COALESCE(excluded.mood, entries.mood)
            """,
            (
                day.isoformat(), raw_text, result["score"], result["title"], result["summary"],
                json.dumps(result["activities"], ensure_ascii=False), result["reason"],
                result["tip"], model, source, now, now, rubric, mood,
            ),
        )
    return get_entry(day)


def set_mood(day: date, mood: int | None) -> dict | None:
    with connect() as conn:
        conn.execute("UPDATE entries SET mood = ? WHERE day = ?", (mood, day.isoformat()))
    return get_entry(day)


def delete_entry(day: date) -> bool:
    """Remove a day and everything recorded for it in DayScore (Todoist itself is untouched)."""
    with connect() as conn:
        cur = conn.execute("DELETE FROM entries WHERE day = ?", (day.isoformat(),))
        for table in ("todoist_done", "todoist_suggestions", "todoist_created"):
            conn.execute(f"DELETE FROM {table} WHERE day = ?", (day.isoformat(),))
        return cur.rowcount == 1


def get_setting(key: str, default: str | None = None) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def delete_setting(key: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def mark_reminder(day: date, kind: str) -> bool:
    """Record a reminder as sent. Returns False if it was already sent."""
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO reminders_sent (day, kind) VALUES (?, ?)",
            (day.isoformat(), kind),
        )
        return cur.rowcount == 1


# ---------- Todoist ----------

def save_todoist_done(task_id: str, day: date, completed_at: str, source: str, status: str,
                      task: dict, replace: bool = True) -> int:
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    with connect() as conn:
        cur = conn.execute(
            f"{verb} INTO todoist_done (task_id, day, completed_at, source, status, task) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, day.isoformat(), completed_at, source, status, json.dumps(task, ensure_ascii=False)),
        )
        return cur.rowcount


def _todoist_row(row: sqlite3.Row) -> dict:
    return {**json.loads(row["task"]), "task_id": row["task_id"], "day": row["day"],
            "completed_at": row["completed_at"], "source": row["source"], "status": row["status"]}


def todoist_for_day(day: date) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM todoist_done WHERE day = ? ORDER BY completed_at", (day.isoformat(),)).fetchall()
    return [_todoist_row(r) for r in rows]


def todoist_by_day() -> dict[str, list[dict]]:
    """Tasks DayScore ticked off (or tried to), per day."""
    result: dict[str, list[dict]] = {}
    with connect() as conn:
        for row in conn.execute("SELECT * FROM todoist_done WHERE source = 'dayscore' ORDER BY completed_at"):
            result.setdefault(row["day"], []).append(_todoist_row(row))
    return result


# ---------- Todoist suggestions (tasks the note seems to finish, waiting for confirmation) ----------

def add_suggestion(day: date, task: dict) -> None:
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO todoist_suggestions (day, task_id, task, status) VALUES (?, ?, ?, 'pending')",
                     (day.isoformat(), task["id"], json.dumps(task, ensure_ascii=False)))


def _suggestion_row(row: sqlite3.Row) -> dict:
    return {**json.loads(row["task"]), "suggestion_id": row["id"], "day": row["day"],
            "status": row["status"], "selected": bool(row["selected"])}


def suggestions(day: date | None = None, status: str | None = None) -> list[dict]:
    query, args = "SELECT * FROM todoist_suggestions WHERE 1=1", []
    if day:
        query += " AND day = ?"; args.append(day.isoformat())
    if status:
        query += " AND status = ?"; args.append(status)
    with connect() as conn:
        return [_suggestion_row(r) for r in conn.execute(query + " ORDER BY id", args)]


def suggested_task_ids(day: date) -> set[str]:
    """Every task already suggested for this day, whatever the answer was."""
    with connect() as conn:
        return {r["task_id"] for r in conn.execute("SELECT task_id FROM todoist_suggestions WHERE day = ?", (day.isoformat(),))}


def set_suggestion(suggestion_id: int, *, status: str | None = None, selected: bool | None = None) -> None:
    with connect() as conn:
        if status is not None:
            conn.execute("UPDATE todoist_suggestions SET status = ? WHERE id = ?", (status, suggestion_id))
        if selected is not None:
            conn.execute("UPDATE todoist_suggestions SET selected = ? WHERE id = ?", (int(selected), suggestion_id))


# ---------- Todoist tasks created from notes ----------

def add_created(day: date, task_id: str, content: str, due: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO todoist_created (day, task_id, content, due, created_at) VALUES (?, ?, ?, ?, ?)",
                     (day.isoformat(), task_id, content, due, datetime.now(timezone.utc).isoformat(timespec="seconds")))


def created(day: date | None = None) -> list[dict]:
    with connect() as conn:
        if day:
            rows = conn.execute("SELECT * FROM todoist_created WHERE day = ? ORDER BY id", (day.isoformat(),))
        else:
            rows = conn.execute("SELECT * FROM todoist_created ORDER BY id")
        return [dict(r) for r in rows]
