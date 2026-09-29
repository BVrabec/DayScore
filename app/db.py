"""SQLite storage. One row per day; the whole database is a single file in DATA_DIR."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone

from .config import settings

SCHEMA = """
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
CREATE TABLE IF NOT EXISTS reminders_sent (
    day  TEXT NOT NULL,
    kind TEXT NOT NULL,
    PRIMARY KEY (day, kind)
);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")


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


def save_entry(day: date, raw_text: str, result: dict, model: str, source: str) -> dict:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO entries (day, raw_text, score, title, summary, activities, reason, tip,
                                 model, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(day) DO UPDATE SET
                raw_text = excluded.raw_text, score = excluded.score, title = excluded.title,
                summary = excluded.summary, activities = excluded.activities,
                reason = excluded.reason, tip = excluded.tip, model = excluded.model,
                updated_at = excluded.updated_at
            """,
            (
                day.isoformat(), raw_text, result["score"], result["title"], result["summary"],
                json.dumps(result["activities"], ensure_ascii=False), result["reason"],
                result["tip"], model, source, now, now,
            ),
        )
    return get_entry(day)


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
    result: dict[str, list[dict]] = {}
    with connect() as conn:
        for row in conn.execute("SELECT * FROM todoist_done ORDER BY completed_at"):
            result.setdefault(row["day"], []).append(_todoist_row(row))
    return result


def todoist_closed_by_dayscore_near(task_id: str, completed: datetime, hours: int = 2) -> bool:
    """True if DayScore itself closed this task around that time."""
    with connect() as conn:
        rows = conn.execute("SELECT completed_at FROM todoist_done WHERE task_id = ? AND source = 'dayscore'",
                            (task_id,)).fetchall()
    return any(abs((datetime.fromisoformat(r["completed_at"]) - completed).total_seconds()) < hours * 3600
               for r in rows)
